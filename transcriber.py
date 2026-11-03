"""Subtitle-first Bilibili transcription with isolated files and optional local ASR."""
from __future__ import annotations

import copy
import html
import importlib.util
import json
import logging
import math
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import yt_dlp

logger = logging.getLogger(__name__)
MAX_AUDIO_BYTES = 300 * 1024 * 1024
MAX_ASR_SECONDS = 2 * 60 * 60
MAX_SUBTITLE_BYTES = 4 * 1024 * 1024
VIDEO_HOSTS = {'bilibili.com', 'www.bilibili.com', 'm.bilibili.com'}
SHORT_HOSTS = {'b23.tv', 'www.b23.tv'}
SUBTITLE_DOMAINS = ('bilibili.com', 'hdslb.com', 'bilivideo.com')
_asr_model = None
_model_lock = threading.RLock()


class TranscriptionError(Exception):
    """A user-facing failure without raw URLs, cookies or local paths."""


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Transcript:
    title: str
    url: str
    text: str
    source: str
    segments: tuple[Segment, ...] = ()


@dataclass(frozen=True)
class Progress:
    stage: str
    message: str
    result: Transcript | None = None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _checked_url(url: str, *, subtitle=False) -> str:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').lower()
        port = parts.port
    except ValueError:
        raise TranscriptionError('链接格式无效') from None
    hosts_ok = host in VIDEO_HOSTS | SHORT_HOSTS
    if subtitle:
        hosts_ok = any(host == d or host.endswith('.' + d) for d in SUBTITLE_DOMAINS)
    if parts.scheme not in {'http', 'https'} or not hosts_ok or parts.username or parts.password or port not in (None, 80, 443):
        raise TranscriptionError('只支持 bilibili.com 视频链接或 b23.tv 短链')
    return urlunsplit(('https', host, parts.path or '/', parts.query, ''))


def _request_checked(url: str, *, subtitle=False) -> tuple[str, bytes]:
    """Validate every redirect before opening it, and bound response size."""
    opener = build_opener(_NoRedirect)
    limit = MAX_SUBTITLE_BYTES if subtitle else 64 * 1024
    for _ in range(6):
        url = _checked_url(url, subtitle=subtitle)
        request = Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'})
        try:
            with opener.open(request, timeout=20) as response:
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise TranscriptionError('字幕或链接响应过大，请更换视频后重试')
                return url, data
        except HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get('Location'):
                url = urljoin(url, exc.headers['Location'])
                exc.close()
                # A video redirect can finish at the canonical URL without downloading its page.
                target = _checked_url(url, subtitle=subtitle)
                if not subtitle and urlsplit(target).hostname in VIDEO_HOSTS:
                    return target, b''
                continue
            exc.close()
            raise TranscriptionError('B 站暂时拒绝访问或链接已失效，请稍后重试') from None
        except (URLError, TimeoutError, OSError):
            raise TranscriptionError('网络连接失败，请检查网络后重试') from None
    raise TranscriptionError('短链跳转次数过多，请使用视频页面的完整链接')


def normalize_video_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TranscriptionError('请输入 B 站视频链接或 BV 号')
    value = value.strip()
    if len(value) > 4096:
        raise TranscriptionError('链接或分享文字过长，请只粘贴视频链接')
    if re.fullmatch(r'BV[0-9A-Za-z]{10}|av[0-9]+', value):
        value = 'https://www.bilibili.com/video/' + value
    matches = re.findall(r'https?://[^\s<>"\u3000]+', value)
    if len(matches) != 1:
        raise TranscriptionError('请提供一个视频链接；也可以粘贴包含链接的分享文字')
    url = _checked_url(matches[0].rstrip('，。；、！!）)]}'))
    if urlsplit(url).hostname in SHORT_HOSTS:
        url, _ = _request_checked(url)
    parts = urlsplit(url)
    if parts.hostname not in VIDEO_HOSTS:
        raise TranscriptionError('短链未指向 B 站视频，请使用完整视频链接')
    match = re.fullmatch(r'/video/(BV[0-9A-Za-z]{10}|av[0-9]+)/?', parts.path)
    if not match:
        raise TranscriptionError('只支持普通 BV/av 视频；暂不支持直播、合集或番剧页面')
    params = parse_qs(parts.query)
    page = params.get('p', ['1'])
    if len(page) != 1 or not re.fullmatch(r'[0-9]{1,3}', page[0]) or not 1 <= int(page[0]) <= 500:
        raise TranscriptionError('分 P 参数无效，请从对应分 P 页面复制链接')
    query = urlencode({'p': int(page[0])}) if int(page[0]) > 1 else ''
    return urlunsplit(('https', 'www.bilibili.com', '/video/' + match[1], query, ''))


def _clean_text(text: str) -> str:
    text = re.sub(r'<(?:/?(?:b|i|u|c|v|lang|ruby|rt)(?:[.\s][^>]*)?|\d{2}:\d{2}[^>]*)>', '', str(text))
    return html.unescape(text).strip()


def _segment(start, end, text) -> Segment | None:
    try:
        start, end = float(start), float(end)
    except (ValueError, TypeError):
        return None
    text = _clean_text(text)
    if not text or not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        return None
    return Segment(start, end, text)


def _seconds(value: str) -> float:
    fields = value.replace(',', '.').split(':')
    return sum(float(n) * (60 ** i) for i, n in enumerate(reversed(fields)))


def parse_subtitle(content: str, extension: str) -> tuple[Segment, ...]:
    """Parse SRT/VTT and Bilibili/YouTube JSON without mixing cue IDs into text."""
    result = []
    extension = extension.lower().lstrip('.')
    if extension in {'json', 'json3'}:
        data = json.loads(content)
        if 'body' in data:
            items = ((s.get('from'), s.get('to'), s.get('content', '')) for s in data['body'])
        else:
            items = ((s.get('tStartMs', 0) / 1000,
                      (s.get('tStartMs', 0) + s.get('dDurationMs', 0)) / 1000,
                      ''.join(x.get('utf8', '') for x in s.get('segs', []))) for s in data.get('events', []))
        result = [_segment(*item) for item in items]
    elif extension in {'srt', 'vtt'}:
        normalized = content.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
        stamp = r'((?:\d+:)?\d{2}:\d{2}[.,]\d{3})'
        for block in re.split(r'\n\s*\n', normalized):
            lines = block.splitlines()
            if not lines or re.match(r'^(NOTE|STYLE|REGION)(\s|$)', lines[0]):
                continue
            for index, line in enumerate(lines):
                timing = re.match(stamp + r'\s*-->\s*' + stamp, line)
                if timing:
                    result.append(_segment(_seconds(timing[1]), _seconds(timing[2]), '\n'.join(lines[index + 1:])))
                    break
    else:
        raise ValueError('Unsupported subtitle format')
    # Drop duplicate cues only, preserving legitimately repeated speech at different times.
    return tuple(sorted(dict.fromkeys(s for s in result if s is not None), key=lambda s: s.start))


def _subtitle_candidates(info):
    formats = {'srt': 0, 'vtt': 1, 'json': 2, 'json3': 3}
    candidates = []
    for kind in ('subtitles', 'automatic_captions'):
        for language, tracks in (info.get(kind) or {}).items():
            lang = language.lower()
            if lang == 'danmaku':
                continue
            group = 0 if 'zh' in lang or lang.startswith('cmn') else 1 if lang.startswith('en') else 2
            automatic = kind == 'automatic_captions' or lang.startswith('ai-')
            for track in tracks:
                ext = track.get('ext', '').lower()
                if ext in formats:
                    candidates.append(((group, automatic, formats[ext]), language, automatic, track))
    return sorted(candidates, key=lambda c: c[0])


def _extract_subtitle(info) -> tuple[tuple[Segment, ...], str] | None:
    for _, lang, automatic, track in _subtitle_candidates(info):
        try:
            content = track.get('data')
            if content is None:
                url = track.get('url', '')
                if url.startswith('//'):
                    url = 'https:' + url
                _, payload = _request_checked(url, subtitle=True)
                content = payload.decode('utf-8-sig')
            if not isinstance(content, str) or len(content.encode('utf-8')) > MAX_SUBTITLE_BYTES:
                continue
            segments = parse_subtitle(content, track['ext'])
            if segments:
                return segments, ('自动字幕' if automatic else '视频字幕') + ' · ' + lang
        except (ValueError, TypeError, KeyError, UnicodeError, TranscriptionError):
            logger.info('跳过一个不可用的字幕轨道')
    return None


class _QuietLogger:
    def debug(self, message):
        pass

    def info(self, message):
        pass

    def warning(self, message):
        logger.info('视频提取器提示：部分字幕或格式可能需要登录')

    def error(self, message):
        pass


def _ydl_options():
    return dict(quiet=True, no_warnings=True, logger=_QuietLogger(), noplaylist=True,
                socket_timeout=20, retries=2, fragment_retries=2, format='bestaudio',
                writesubtitles=True, writeautomaticsub=True, skip_download=True,
                cachedir=False, restrictfilenames=True)


def _download_audio(info: dict, folder: Path) -> Path:
    def bounded_download(progress):
        size = progress.get('downloaded_bytes', 0)
        total = progress.get('total_bytes') or progress.get('total_bytes_estimate') or 0
        if max(size, total) > MAX_AUDIO_BYTES:
            raise TranscriptionError('音频超过 300 MB 限制，请选择较短的分 P')
    options = _ydl_options() | dict(skip_download=False, writesubtitles=False, writeautomaticsub=False,
        outtmpl=str(folder / 'audio.%(ext)s'), max_filesize=MAX_AUDIO_BYTES, progress_hooks=[bounded_download])
    with yt_dlp.YoutubeDL(options) as ydl:
        # Reuse extracted metadata instead of fetching the same video info again.
        ydl.process_ie_result(copy.deepcopy(info), download=True)
    files = [p for p in folder.glob('audio.*') if p.is_file() and p.suffix not in {'.part', '.ytdl', '.json'}]
    if len(files) != 1 or files[0].stat().st_size == 0:
        raise TranscriptionError('未获取到单个有效音频，请检查视频是否可以公开播放')
    if files[0].stat().st_size > MAX_AUDIO_BYTES:
        raise TranscriptionError('音频超过 300 MB 限制，请选择较短的分 P')
    return files[0]


def _prepare_wav(audio: Path, folder: Path) -> Path:
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise TranscriptionError('语音识别需要 FFmpeg，请先安装 FFmpeg；字幕提取不需要它')
    wav = folder / 'speech.wav'
    try:
        subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-i', str(audio),
            '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', '-t', str(MAX_ASR_SECONDS + 1),
            '-y', str(wav)], check=True, capture_output=True, timeout=600)
    except (subprocess.SubprocessError, OSError):
        raise TranscriptionError('音频转换失败，请确认 FFmpeg 可用且视频音轨完整') from None
    import wave
    try:
        with wave.open(str(wav), 'rb') as stream:
            duration = stream.getnframes() / stream.getframerate()
            if duration <= 0 or duration > MAX_ASR_SECONDS:
                raise TranscriptionError('音频为空或超过两小时，请选择较短的分 P')
    except (wave.Error, OSError):
        raise TranscriptionError('音频转换没有生成有效的 WAV 文件') from None
    return wav


def _get_model():
    global _asr_model
    with _model_lock:
        if _asr_model is None:
            from funasr import AutoModel
            _asr_model = AutoModel(model='paraformer-zh', vad_model='fsmn-vad', punc_model='ct-punc',
                                   device='cpu', disable_update=True)
        return _asr_model


def _asr_transcribe(audio: Path) -> tuple[str, tuple[Segment, ...]]:
    with _model_lock:
        result = _get_model().generate(input=str(audio), batch_size_s=60, merge_vad=True, merge_length_s=15)
    texts = [str(part.get('text', '')).strip() for part in (result or [])]
    text = '\n\n'.join(t for t in texts if t)
    if not text:
        raise TranscriptionError('没有识别出有效语音，请检查视频音轨或改用字幕模式')
    sentences = [s for part in result for s in (part.get('sentence_info') or [])]
    try:
        segments = [_segment(float(s.get('start', -1)) / 1000, float(s.get('end', -1)) / 1000, s.get('text', '')) for s in sentences]
    except (ValueError, TypeError):
        segments = [None]
    # Never invent timestamps or export a partial timed transcript.
    timed = tuple(s for s in segments if s is not None) if all(s is not None for s in segments) else ()
    return text, timed


def _download_error_message(exc: Exception) -> str:
    message = str(exc).lower()
    if any(s in message for s in ('412', '429', '403', 'captcha')):
        return 'B 站暂时拒绝访问，请稍后重试或更换可公开播放的视频'
    if any(s in message for s in ('login', 'private video', 'premium', 'members-only')):
        return '该视频或字幕需要登录或访问权限；本工具不会绕过访问限制'
    if any(s in message for s in ('unavailable', 'deleted', 'geo-restricted', '404')):
        return '视频不可用、已删除或当前地区无法播放'
    return '视频信息或音频下载失败，请检查链接、网络，并更新 yt-dlp 后重试'


def transcribe_video(value: str, allow_asr: bool = True) -> Iterator[Progress]:
    try:
        url = normalize_video_url(value)
        yield Progress('metadata', '正在读取视频信息与可用字幕…')
        with yt_dlp.YoutubeDL(_ydl_options()) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info or info.get('_type') in {'playlist', 'multi_video'}:
            raise TranscriptionError('请选择一个普通视频或明确的分 P，不支持整套合集批量下载')
        title = ' '.join(str(info.get('title') or 'B站视频').split())[:200]
        yield Progress('subtitles', '正在提取字幕；此步骤不会下载视频…')
        subtitle = _extract_subtitle(info)
        if subtitle:
            segments, source = subtitle
            text = '\n'.join(s.text for s in segments)
            yield Progress('done', '字幕提取完成', Transcript(title, url, text, source, segments))
            return
        if not allow_asr:
            raise TranscriptionError('未找到可读取的字幕。可开启本地语音识别后重试')
        if importlib.util.find_spec('funasr') is None:
            raise TranscriptionError('未找到字幕；语音识别依赖尚未安装，请按 README 安装 requirements-asr.txt')
        if not shutil.which('ffmpeg'):
            raise TranscriptionError('未找到字幕；语音识别需要 FFmpeg，请先安装后重试')
        duration = info.get('duration')
        if not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 < duration <= MAX_ASR_SECONDS:
            raise TranscriptionError('语音识别仅支持时长明确且不超过两小时的视频，请选择较短的分 P')
        with tempfile.TemporaryDirectory(prefix='bili-audio-') as folder_name:
            folder = Path(folder_name)
            yield Progress('download', '未找到字幕，正在下载音频（不会下载画面）…')
            audio = _download_audio(info, folder)
            yield Progress('convert', '正在转换为单声道 16 kHz 音频…')
            wav = _prepare_wav(audio, folder)
            yield Progress('asr', '正在进行本地语音识别；首次使用会下载模型，请耐心等待…')
            text, segments = _asr_transcribe(wav)
            result = Transcript(title, url, text, '本地语音识别', segments)
        # Delete media before exposing the final result to a paused consumer.
        yield Progress('done', '语音识别完成', result)
    except TranscriptionError as exc:
        yield Progress('error', str(exc))
    except yt_dlp.utils.DownloadError as exc:
        yield Progress('error', _download_error_message(exc))
    except (ImportError, ModuleNotFoundError):
        yield Progress('error', '语音识别组件缺失或不兼容，请按 README 安装可选依赖')
    except GeneratorExit:
        raise
    except Exception:
        logger.warning('转写失败，请检查网络、可选依赖或模型配置')
        yield Progress('error', '处理失败，请检查网络与依赖后重试；正文不会包含内部错误信息')
