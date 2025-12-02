
import yt_dlp
from funasr import AutoModel

def download_audio(url, out="audio.mp3"):
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': out,
        'quiet': True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return out

model = AutoModel(model="iic/speech_paraformer-large-vad-punc")

def transcribe_video(url):
    audio = download_audio(url)
    result = model.generate(input=audio)
    return result[0]["text"]
