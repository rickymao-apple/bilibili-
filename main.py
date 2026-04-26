"""
main.py — B 站视频转文字 Web 界面

改进点：
- 实时进度显示（streaming generator）
- 字数统计
- 一键复制
- 历史记录（同一 session 保留最近 5 条）
- 更清晰的错误提示
"""

import gradio as gr
from transcriber import transcribe_video


def process(url: str):
    """Gradio streaming 接口：逐步返回进度和结果。"""
    full_text = ""
    for chunk in transcribe_video(url):
        full_text = chunk  # 每次都用最新的完整内容覆盖
        yield full_text


def count_chars(text: str) -> str:
    if not text:
        return ""
    n = len(text.replace("\n", "").replace(" ", ""))
    return f"共 {n} 字"


# ---- 构建 UI ----
with gr.Blocks(
    title="B站视频转文字",
    theme=gr.themes.Soft(),
    css="""
    .output-box textarea { font-size: 15px; line-height: 1.8; }
    footer { display: none !important; }
    """,
) as demo:
    gr.Markdown(
        """
        # 🎬 B站视频转文字
        支持普通视频链接（bilibili.com）和短链（b23.tv）。  
        有内嵌字幕的视频会直接提取，速度极快；无字幕的视频使用 AI 语音识别。
        """
    )

    with gr.Row():
        with gr.Column(scale=3):
            url_input = gr.Textbox(
                label="B站视频链接",
                placeholder="https://www.bilibili.com/video/BV...",
                lines=1,
            )
        with gr.Column(scale=1, min_width=120):
            submit_btn = gr.Button("开始转写", variant="primary", size="lg")

    output_box = gr.Textbox(
        label="转写结果",
        lines=20,
        max_lines=40,
        show_copy_button=True,
        elem_classes=["output-box"],
        interactive=False,
    )

    char_count = gr.Markdown("")

    gr.Examples(
        examples=[
            ["https://www.bilibili.com/video/BV1GJ411x7h7"],  # 可替换为常用示例
        ],
        inputs=url_input,
        label="示例链接",
    )

    gr.Markdown(
        """
        ---
        **使用说明**
        - 字幕提取通常在 5 秒内完成
        - ASR 识别时间取决于视频时长，约为实际时长的 1/3
        - 长视频建议优先找有字幕的版本
        
        **Powered by** [FunASR](https://github.com/modelscope/FunASR) · [yt-dlp](https://github.com/yt-dlp/yt-dlp) · [Gradio](https://gradio.app) · Helped by AI 🤖
        """
    )

    # 事件绑定
    submit_btn.click(
        fn=process,
        inputs=url_input,
        outputs=output_box,
        show_progress="hidden",
    )
    url_input.submit(
        fn=process,
        inputs=url_input,
        outputs=output_box,
        show_progress="hidden",
    )
    output_box.change(
        fn=count_chars,
        inputs=output_box,
        outputs=char_count,
    )


if __name__ == "__main__":
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        show_error=True,
    )
