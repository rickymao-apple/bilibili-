
import gradio as gr
from transcriber import transcribe_video

def process(url):
    try:
        text = transcribe_video(url)
        return text
    except Exception as e:
        return str(e)

iface = gr.Interface(
    fn=process,
    inputs=gr.Textbox(label="输入B站视频链接"),
    outputs=gr.Textbox(label="转写结果", lines=20),
    title="B站视频转文字（一键版）"
)

iface.launch()
