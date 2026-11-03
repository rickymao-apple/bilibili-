"""Local Bilibili transcript UI. No model is imported or downloaded on startup."""
import argparse
import os
os.environ.setdefault('GRADIO_ANALYTICS_ENABLED', 'False')

import gradio as gr
from transcriber import transcribe_video
from exports import add_to_history, cleanup_history, count_chars, export_transcript


def process(url, allow_asr, history):
    yield '正在检查链接…', '', count_chars(''), [], history, gr.skip()
    for progress in transcribe_video(url, allow_asr=allow_asr):
        if progress.result is None:
            yield progress.message, gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip()
            continue
        result = progress.result
        try:
            record = export_transcript(result)
        except OSError:
            yield '转写完成，但导出失败，请先复制正文并检查磁盘空间', result.text, count_chars(result.text), [], gr.skip(), gr.skip()
            continue
        history = add_to_history(record, history)
        choices = [(f'{item["title"][:60]} · {item["source"]}', item['id']) for item in history]
        yield f'完成 · {result.source} · {result.title}', result.text, count_chars(result.text), record['files'], history, gr.Dropdown(choices=choices, value=record['id'])


def restore_history(identifier, history):
    record = next((r for r in (history or []) if r['id'] == identifier), None)
    if record is None:
        return '历史记录已过期，请重新转写', '', count_chars(''), []
    return f'历史记录 · {record["source"]} · {record["title"]}', record['text'], count_chars(record['text']), record['files']


def create_demo():
    with gr.Blocks(title='B站视频转文字', delete_cache=(3600, 3600), analytics_enabled=False) as demo:
        history = gr.State([], time_to_live=3600, delete_callback=cleanup_history)
        gr.Markdown('# B站视频转文字\n把网课整理成可以复制、下载和打印的文字稿。优先提取视频字幕，无字幕时可使用本地语音识别。')
        with gr.Row():
            url = gr.Textbox(label='视频链接或 BV 号', placeholder='粘贴视频链接、b23.tv 短链或分享文字；分 P 请保留 ?p=2', scale=5)
            submit = gr.Button('开始转写', variant='primary', scale=1)
        allow_asr = gr.Checkbox(value=True, label='没有可读字幕时，使用本地语音识别')
        status = gr.Textbox(label='处理状态', value='等待输入。字幕提取无需安装语音模型，也不需要付费 API。', interactive=False)
        output = gr.Textbox(label='文字稿', lines=18, max_lines=36, interactive=False, buttons=['copy'], elem_classes=['transcript'])
        char_count = gr.Markdown(count_chars(''))
        downloads = gr.File(label='下载文字稿（TXT / Markdown；有真实时间轴时另含 SRT）', file_count='multiple', interactive=False)
        recent = gr.Dropdown(label='本次会话最近 5 条结果', choices=[], value=None, interactive=True)
        gr.Markdown('多 P 视频每次只处理链接指定的一集。语音识别目前适合普通话；首次识别会下载模型，需要足够的内存和磁盘。'
                    '结果可能有错字，请对照原视频核查。下载与历史记录是临时文件，请及时保存。')
        parameters = dict(fn=process, inputs=[url, allow_asr, history],
                          outputs=[status, output, char_count, downloads, history, recent],
                          concurrency_limit=1, concurrency_id='transcription', trigger_mode='once', api_name=False)
        submit.click(**parameters)
        url.submit(**parameters)
        recent.input(restore_history, inputs=[recent, history], outputs=[status, output, char_count, downloads], api_name=False)
    return demo.queue(max_size=8, default_concurrency_limit=1)


def main():
    parser = argparse.ArgumentParser(description='B站视频转文字（默认仅本机访问）')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=7860)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('端口必须在 1 到 65535 之间')
    create_demo().launch(server_name=args.host, server_port=args.port, share=False, show_error=False,
                         theme=gr.themes.Soft(), css='.transcript textarea {line-height: 1.8;}')


if __name__ == '__main__':
    main()
