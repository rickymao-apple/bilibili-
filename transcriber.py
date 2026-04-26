"""
transcriber.py — B 站视频转文字核心模块

改进点：
- 字幕优先：有内嵌字幕直接用，跳过 ASR，快 10 倍+
- 懒加载模型：第一次转写时才加载，不拖慢启动
- 临时文件隔离：每个请求用独立 UUID 目录，并发安全
- 自动清理：转写完毕删除音频，不留垃圾
- 进度回调：通过 generator yield 实时反馈进度
- 友好报错：中文提示常见错误原因
"""

import os
import uuid
import shutil
import tempfile
import logging
from pathlib import Path
from typing import Generator

import yt_dlp

logger = logging.getLogger(__name__)

# ---------- 懒加载 ASR 模型 ----------
_asr_model = None

def _get_model():
    global _asr_model
    if _asr_model is None:
        logger.info("首次使用，正在加载 ASR 模型（约需 30 秒）...")
        from funasr import AutoModel
        _asr_model = AutoModel(
            model="iic/speech_paraformer-large-vad-punc",
            disable_update=True,
        )
        logger.info("模型加载完毕")
    return _asr_model


# ---------- 字幕提取（优先） ----------
def _try_get_subtitle(url: str) -> str | None:
    """
    尝试提取 B 站内嵌字幕（cc 字幕）。
    成功返回纯文本，失败返回 None。
    """
    ydl_opts = {
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["zh-Hans", "zh", "zh-CN"],
        "skip_download": True,
        "quiet": True,
        "outtmpl": os.path.join(tempfile.gettempdir(), "%(id)s.%(ext)s"),
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            subs = info.get("subtitles", {}) or {}
            auto_subs = info.get("automatic_captions", {}) or {}

            # 优先手动字幕，其次自动字幕
            for lang in ["zh-Hans", "zh", "zh-CN"]:
                for source in [subs, auto_subs]:
                    if lang in source:
                        # 下载并解析 vtt/json3
                        ydl_opts2 = {**ydl_opts, "skip_download": False}
                        with yt_dlp.YoutubeDL(ydl_opts2) as ydl2:
                            ydl2.download([url])
                        # 找到下载的字幕文件
                        vid_id = info.get("id", "")
                        for ext in [".zh-Hans.vtt", ".zh.vtt", ".zh-CN.vtt",
                                    ".zh-Hans.json3", ".zh.json3"]:
                            path = Path(tempfile.gettempdir()) / f"{vid_id}{ext}"
                            if path.exists():
                                text = _parse_subtitle_file(path)
                                path.unlink(missing_ok=True)
                                return text
    except Exception as e:
        logger.debug(f"字幕提取失败（将 fallback 到 ASR）: {e}")
    return None


def _parse_subtitle_file(path: Path) -> str:
    """解析 .vtt 或 .json3 字幕文件，返回纯文本。"""
    content = path.read_text(encoding="utf-8", errors="ignore")

    if path.suffix == ".json3":
        import json
        data = json.loads(content)
        lines = []
        for event in data.get("events", []):
            for seg in event.get("segs", []):
                t = seg.get("utf8", "").strip()
                if t and t != "\n":
                    lines.append(t)
        return "".join(lines)

    # vtt 格式：去掉时间戳行和 WEBVTT 头
    lines = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("WEBVTT") or "-->" in line:
            continue
        if line.isdigit():
            continue
        lines.append(line)
    # 去重相邻重复行（B 站字幕常有）
    deduped = []
    prev = ""
    for ln in lines:
        if ln != prev:
            deduped.append(ln)
            prev = ln
    return "\n".join(deduped)


# ---------- 音频下载 + ASR ----------
def _download_audio(url: str, out_dir: str) -> str:
    """下载最优音频到独立目录，返回文件路径。"""
    out_tmpl = os.path.join(out_dir, "audio.%(ext)s")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_tmpl,
        "quiet": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "128",
        }],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    # 找到实际下载的文件
    for f in Path(out_dir).iterdir():
        if f.suffix in (".mp3", ".m4a", ".opus", ".webm"):
            return str(f)
    raise FileNotFoundError("音频下载失败，未找到音频文件")


def _asr_transcribe(audio_path: str) -> str:
    """用 FunASR Paraformer 转写音频，返回文本。"""
    model = _get_model()
    result = model.generate(input=audio_path)
    if not result:
        raise ValueError("ASR 模型返回空结果")
    return result[0]["text"]


# ---------- 主入口（generator，支持进度回调） ----------
def transcribe_video(url: str) -> Generator[str, None, None]:
    """
    转写 B 站视频，以 generator 方式逐步 yield 进度和结果。

    调用方示例：
        for msg in transcribe_video(url):
            print(msg)  # 最后一条即为最终文本
    """
    url = url.strip()
    if not url:
        yield "❌ 请输入视频链接"
        return

    if "bilibili.com" not in url and "b23.tv" not in url:
        yield "❌ 请输入有效的 B 站链接（bilibili.com 或 b23.tv）"
        return

    # 1. 尝试字幕
    yield "⏳ 正在检查视频字幕..."
    subtitle = _try_get_subtitle(url)
    if subtitle:
        yield "✅ 找到内嵌字幕，直接提取（比 ASR 快得多）\n\n" + subtitle
        return

    # 2. 下载音频
    tmp_dir = os.path.join(tempfile.gettempdir(), f"bili_{uuid.uuid4().hex[:8]}")
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        yield "⏳ 未找到字幕，正在下载音频..."
        audio_path = _download_audio(url, tmp_dir)
        file_mb = os.path.getsize(audio_path) / 1024 / 1024
        yield f"✅ 音频下载完毕（{file_mb:.1f} MB），正在 ASR 识别，请稍候..."

        # 3. ASR
        text = _asr_transcribe(audio_path)
        yield text

    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Private" in msg or "login" in msg.lower():
            yield "❌ 该视频需要登录才能访问，暂不支持"
        elif "unavailable" in msg.lower():
            yield "❌ 视频不存在或已被删除"
        else:
            yield f"❌ 下载失败：{msg}"
    except Exception as e:
        logger.exception("转写异常")
        yield f"❌ 转写出错：{e}"
    finally:
        # 清理临时文件
        shutil.rmtree(tmp_dir, ignore_errors=True)
