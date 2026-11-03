"""Offline regressions: fixtures exercise failures without downloading videos or models."""
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import transcriber as t
from exports import EXPORT_ROOT, add_to_history, cleanup_history, count_chars, export_transcript

URL = 'https://www.bilibili.com/video/BV1GJ411x7h7'
SRT = '1\n00:00:01,200 --> 00:00:03,400\n你好 &amp; 世界\n\n2\n00:00:04,000 --> 00:00:05,000\n123\n'


def info(**extra):
    return dict(id='fixture', title='示例课程', duration=60,
                subtitles={'zh-CN':[{'ext':'srt', 'data':SRT}]}) | extra


class UrlTests(unittest.TestCase):
    def test_bv_av_share_text_and_part(self):
        cases = [(URL,URL), ('BV1GJ411x7h7',URL), ('av123','https://www.bilibili.com/video/av123'),
                 ('【课程】 ' + URL + '/?p=2&share_source=copy。', URL+'?p=2')]
        for value,expected in cases:
            with self.subTest(value=value):
                self.assertEqual(t.normalize_video_url(value),expected)

    def test_reject_domain_spoofing_and_untrusted_inputs(self):
        values = ['', None, 'https://evil.test/?bilibili.com', 'https://bilibili.com.evil.test/video/BV1GJ411x7h7',
                  'https://bilibili.com@127.0.0.1/video/BV1GJ411x7h7', 'file:///bilibili.com',
                  URL+' '+URL, 'https://www.bilibili.com/live/1', 'https://www.bilibili.com:123/video/BV1GJ411x7h7']
        for value in values:
            with self.subTest(value=value), self.assertRaises(t.TranscriptionError):
                t.normalize_video_url(value)

    def test_invalid_part(self):
        for query in ['p=0','p=-1','p=501','p=nan','p=1&p=2','p=abc']:
            with self.subTest(query=query), self.assertRaises(t.TranscriptionError):
                t.normalize_video_url(URL+'?'+query)

    def test_short_link_preserves_selected_part(self):
        with patch.object(t, '_request_checked', return_value=(URL+'?p=2',b'')) as request:
            self.assertEqual(t.normalize_video_url('https://b23.tv/abcd'),URL+'?p=2')
            request.assert_called_once()

    def test_short_redirect_is_validated_before_request(self):
        error=HTTPError('https://b23.tv/a',302,'redirect',{'Location':'https://127.0.0.1/private'},io.BytesIO())
        opener=Mock()
        opener.open.side_effect=error
        with patch.object(t,'build_opener',return_value=opener), self.assertRaises(t.TranscriptionError):
            t._request_checked('https://b23.tv/a')
        self.assertEqual(opener.open.call_count,1)

    def test_subtitle_redirect_rejects_arbitrary_hosts(self):
        error=HTTPError('https://i.hdslb.com/s.json',302,'redirect',{'Location':'https://evil.test/secret'},io.BytesIO())
        opener=Mock()
        opener.open.side_effect=error
        with patch.object(t,'build_opener',return_value=opener), self.assertRaises(t.TranscriptionError):
            t._request_checked('https://i.hdslb.com/s.json',subtitle=True)
        self.assertEqual(opener.open.call_count,1)


class SubtitleTests(unittest.TestCase):
    def test_srt_preserves_spoken_numbers_and_times(self):
        segments=t.parse_subtitle(SRT,'srt')
        self.assertEqual([s.text for s in segments],['你好 & 世界','123'])
        self.assertEqual((segments[0].start,segments[0].end),(1.2,3.4))

    def test_vtt_cues_ignore_metadata_and_markup(self):
        data='WEBVTT\n\nNOTE ignore this\nnot spoken\n\nSTYLE\n::cue {}\n\ncaption-id\n00:01.000 --> 00:02.500 align:start\n<v 老师><b>课文</b></v>\n第二行\n'
        self.assertEqual(t.parse_subtitle(data,'vtt'),(t.Segment(1,2.5,'课文\n第二行'),))

    def test_bilibili_json(self):
        data=json.dumps({'body':[{'from':1,'to':2,'content':'你好'}]})
        self.assertEqual(t.parse_subtitle(data,'json'),(t.Segment(1,2,'你好'),))

    def test_json3_keeps_english_word_spacing(self):
        data=json.dumps({'events':[{'tStartMs':1000,'dDurationMs':1000,'segs':[{'utf8':'Hello '},{'utf8':'world'}]}]})
        self.assertEqual(t.parse_subtitle(data,'json3')[0].text,'Hello world')

    def test_invalid_timestamps_and_exact_duplicate_cues(self):
        data=json.dumps({'body':[{'from':1,'to':2,'content':'重复'}, {'from':1,'to':2,'content':'重复'},
                                 {'from':3,'to':4,'content':'重复'}, {'from':-1,'to':1,'content':'bad'},
                                 {'from':5,'to':4,'content':'bad'}, {'from':float('nan'),'to':4,'content':'bad'}]})
        self.assertEqual(len(t.parse_subtitle(data,'json')),2)

    def test_manual_chinese_before_auto_and_danmaku_not_transcript(self):
        fixture=info(subtitles={'ai-zh':[{'ext':'srt','data':SRT.replace('你好','自动')}],
                               'zh':[{'ext':'srt','data':SRT.replace('你好','人工')}],
                               'danmaku':[{'ext':'xml','url':'https://comment.bilibili.com/1.xml'}]})
        segments,source=t._extract_subtitle(fixture)
        self.assertIn('人工',segments[0].text)
        self.assertIn('视频字幕',source)

    def test_auto_chinese_before_unrelated_language(self):
        fixture=info(subtitles={'en':[{'ext':'srt','data':SRT.replace('你好','English')}]},
                     automatic_captions={'zh':[{'ext':'srt','data':SRT}]})
        self.assertIn('自动字幕',t._extract_subtitle(fixture)[1])

    def test_inline_subtitle_requires_no_second_request(self):
        with patch.object(t,'_request_checked') as request:
            self.assertIsNotNone(t._extract_subtitle(info()))
            request.assert_not_called()

    def test_malformed_track_does_not_hide_usable_fallback(self):
        fixture=info(subtitles={'zh':[{'ext':'json','data':'{broken'}, {'ext':'srt','data':SRT}]})
        self.assertIsNotNone(t._extract_subtitle(fixture))

    def test_danmaku_only_is_not_subtitle(self):
        self.assertIsNone(t._extract_subtitle(info(subtitles={'danmaku':[{'ext':'xml','data':'<i>弹幕</i>'}]})))


class PipelineTests(unittest.TestCase):
    def fake_ydl(self, fixture):
        downloader=Mock()
        downloader.extract_info.return_value=fixture
        context=Mock()
        context.__enter__=Mock(return_value=downloader)
        context.__exit__=Mock(return_value=False)
        return context,downloader

    def test_subtitle_path_never_downloads_media_or_loads_asr(self):
        context,downloader=self.fake_ydl(info())
        with patch.object(t.yt_dlp,'YoutubeDL',return_value=context) as constructor, patch.object(t,'_download_audio') as audio, patch.object(t,'_get_model') as model:
            events=list(t.transcribe_video(URL))
            self.assertEqual(events[-1].stage,'done')
            self.assertEqual(events[-1].result.text,'你好 & 世界\n123')
            self.assertNotIn('完成',events[-1].result.text)
            downloader.extract_info.assert_called_once_with(URL,download=False)
            downloader.download.assert_not_called()
            audio.assert_not_called()
            model.assert_not_called()
            self.assertTrue(constructor.call_args.args[0]['noplaylist'])
            self.assertTrue(constructor.call_args.args[0]['skip_download'])

    def test_disabled_asr_does_not_download(self):
        context,_=self.fake_ydl(info(subtitles={}))
        with patch.object(t.yt_dlp,'YoutubeDL',return_value=context), patch.object(t,'_download_audio') as audio:
            events=list(t.transcribe_video(URL,allow_asr=False))
            self.assertEqual(events[-1].stage,'error')
            audio.assert_not_called()

    def test_missing_optional_dependencies_detected_before_download(self):
        context,_=self.fake_ydl(info(subtitles={}))
        with patch.object(t.yt_dlp,'YoutubeDL',return_value=context), patch.object(t.importlib.util,'find_spec',return_value=None), patch.object(t,'_download_audio') as audio:
            events=list(t.transcribe_video(URL))
            self.assertIn('requirements-asr.txt',events[-1].message)
            audio.assert_not_called()

    def test_playlist_is_rejected(self):
        context,_=self.fake_ydl(info(_type='playlist'))
        with patch.object(t.yt_dlp,'YoutubeDL',return_value=context):
            self.assertEqual(list(t.transcribe_video(URL))[-1].stage,'error')

    def test_duration_limit_applies_before_asr_download(self):
        for duration in [None,0,-1,7201,float('nan')]:
            context,_=self.fake_ydl(info(subtitles={},duration=duration))
            with self.subTest(duration=duration), patch.object(t.yt_dlp,'YoutubeDL',return_value=context), patch.object(t.importlib.util,'find_spec',return_value=Mock()), patch.object(t.shutil,'which',return_value='/fake/ffmpeg'), patch.object(t,'_download_audio') as audio:
                self.assertEqual(list(t.transcribe_video(URL))[-1].stage,'error')
                audio.assert_not_called()

    def test_audio_files_cleaned_before_final_yield(self):
        context,_=self.fake_ydl(info(subtitles={}))
        seen=[]
        def audio(info,folder):
            seen.append(folder)
            result=folder/'audio.m4a'
            result.write_bytes(b'fixture')
            return result
        with patch.object(t.yt_dlp,'YoutubeDL',return_value=context), patch.object(t.importlib.util,'find_spec',return_value=Mock()), patch.object(t.shutil,'which',return_value='/fake/ffmpeg'), patch.object(t,'_download_audio',side_effect=audio), patch.object(t,'_prepare_wav',side_effect=lambda a,f:a), patch.object(t,'_asr_transcribe',return_value=('语音正文',())):
            events=list(t.transcribe_video(URL))
            self.assertEqual(events[-1].result.text,'语音正文')
            self.assertFalse(seen[0].exists())

    def test_audio_files_cleaned_on_failure_and_generator_close(self):
        context,_=self.fake_ydl(info(subtitles={}))
        seen=[]
        def audio(info,folder):
            seen.append(folder)
            path=folder/'audio.m4a';path.write_bytes(b'fixture')
            return path
        with patch.object(t.yt_dlp,'YoutubeDL',return_value=context), patch.object(t.importlib.util,'find_spec',return_value=Mock()), patch.object(t.shutil,'which',return_value='/fake/ffmpeg'), patch.object(t,'_download_audio',side_effect=audio), patch.object(t,'_prepare_wav',side_effect=RuntimeError('private-path-never-exposed')):
            events=list(t.transcribe_video(URL))
            self.assertEqual(events[-1].stage,'error')
            self.assertNotIn('private-path',events[-1].message)
            self.assertFalse(seen[-1].exists())
            generator=t.transcribe_video(URL)
            while next(generator).stage != 'convert':
                pass
            folder=seen[-1]
            self.assertTrue(folder.exists())
            generator.close()
            self.assertFalse(folder.exists())

    def test_download_reuses_metadata_and_only_requests_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            context,downloader=self.fake_ydl(info())
            downloader.process_ie_result.side_effect=lambda *args,**kw:(folder/'audio.m4a').write_bytes(b'audio')
            with patch.object(t.yt_dlp,'YoutubeDL',return_value=context) as constructor:
                self.assertEqual(t._download_audio(info(),folder).name,'audio.m4a')
                downloader.extract_info.assert_not_called()
                self.assertTrue(downloader.process_ie_result.call_args.kwargs['download'])
                options=constructor.call_args.args[0]
                self.assertEqual(options['format'],'bestaudio')
                self.assertFalse(options['writesubtitles'])
                with self.assertRaises(t.TranscriptionError):
                    options['progress_hooks'][0]({'downloaded_bytes':t.MAX_AUDIO_BYTES+1})

    def test_asr_shared_model_is_serialized_and_all_text_returned(self):
        active=0;maximum=0
        guard=threading.Lock()
        def generate(**kwargs):
            nonlocal active,maximum
            with guard:
                active+=1;maximum=max(maximum,active)
            time.sleep(.01)
            with guard:
                active-=1
            return [{'text':'第一段'},{'text':'第二段'}]
        model=Mock();model.generate.side_effect=generate
        with patch.object(t,'_asr_model',model):
            results=[]
            threads=[threading.Thread(target=lambda:results.append(t._asr_transcribe(Path('mock.wav')))) for _ in range(3)]
            for thread in threads:thread.start()
            for thread in threads:thread.join()
            self.assertEqual(maximum,1)
            self.assertEqual(results[0],('第一段\n\n第二段',()))

    def test_download_error_does_not_leak_raw_url(self):
        context,downloader=self.fake_ydl(info())
        downloader.extract_info.side_effect=t.yt_dlp.utils.DownloadError('HTTP 403 https://private?token=secret')
        with patch.object(t.yt_dlp,'YoutubeDL',return_value=context):
            event=list(t.transcribe_video(URL))[-1]
            self.assertNotIn('secret',event.message)
            self.assertIn('拒绝',event.message)


class ExportTests(unittest.TestCase):
    def tearDown(self):
        for directory in EXPORT_ROOT.iterdir():
            if directory.is_dir():
                cleanup_history([{'directory':str(directory)}])

    def transcript(self,timed=True):
        return t.Transcript('../../示例 / 课文',URL,'正文 ![外链](https://evil.test)\n第二段','视频字幕',
                            (t.Segment(1.2,3.4,'正文'),) if timed else ())

    def test_exports_stay_in_own_directory_and_have_utf8_text(self):
        record=export_transcript(self.transcript())
        self.assertEqual(len(record['files']),3)
        for file in record['files']:
            self.assertEqual(Path(file).parent.parent,EXPORT_ROOT)
        self.assertIn('正文',Path(record['files'][0]).read_text())
        self.assertIn('00:00:01,200 --> 00:00:03,400',Path(record['files'][2]).read_text())
        self.assertNotIn('![外链]',Path(record['files'][1]).read_text())

    def test_asr_without_timestamps_does_not_fabricate_srt(self):
        record=export_transcript(self.transcript(timed=False))
        self.assertEqual([Path(p).suffix for p in record['files']],['.txt','.md'])

    def test_session_history_is_bounded_and_evicted_exports_removed(self):
        history=[]
        first=export_transcript(self.transcript())
        history=add_to_history(first,history)
        for _ in range(5):
            history=add_to_history(export_transcript(self.transcript()),history)
        self.assertEqual(len(history),5)
        self.assertFalse(Path(first['directory']).exists())
        self.assertTrue(all(Path(r['directory']).exists() for r in history))

    def test_cleanup_never_deletes_outside_export_root(self):
        with tempfile.TemporaryDirectory() as directory:
            cleanup_history([{'directory':directory}])
            self.assertTrue(Path(directory).exists())

    def test_count_characters_excludes_all_whitespace(self):
        self.assertEqual(count_chars('甲 乙\n丙\t丁\u3000'),'共 4 字')


class UiTests(unittest.TestCase):
    def test_progress_and_errors_do_not_enter_transcript(self):
        from main import process
        progress=[t.Progress('download','正在下载'),t.Progress('error','下载失败')]
        with patch('main.transcribe_video',return_value=iter(progress)):
            updates=list(process(URL,False,[]))
        self.assertEqual(updates[0][1],'')
        self.assertEqual(updates[-1][0],'下载失败')
        self.assertNotEqual(updates[-1][1],'下载失败')

    def test_complete_result_and_history_restore(self):
        from main import process,restore_history
        result=t.Transcript('课程',URL,'纯正文','视频字幕')
        with patch('main.transcribe_video',return_value=iter([t.Progress('done','完成',result)])):
            updates=list(process(URL,False,[]))
        last=updates[-1]
        self.assertEqual(last[1],'纯正文')
        self.assertEqual(last[2],'共 3 字')
        self.assertEqual(restore_history(last[4][0]['id'],last[4])[1],'纯正文')
        cleanup_history(last[4])

    def test_ui_builds_without_importing_funasr(self):
        import sys
        from main import create_demo
        demo=create_demo()
        self.assertNotIn('funasr',sys.modules)
        self.assertTrue(demo.enable_queue)
        demo.close()


if __name__ == '__main__':
    unittest.main()
