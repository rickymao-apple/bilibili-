"""Short-lived transcript exports. Each result has its own unguessable directory."""
import html
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from transcriber import Transcript

_export_directory = tempfile.TemporaryDirectory(prefix='bili-exports-')
EXPORT_ROOT = Path(_export_directory.name)


def count_chars(text: str) -> str:
    return f'共 {sum(not char.isspace() for char in text)} 字'


def _markdown(text):
    text = html.escape(text, quote=False)
    return re.sub(r'([\\`*_{}\[\]()#+.!|>~-])', r'\\\1', text)


def _timestamp(seconds):
    milliseconds = round(seconds * 1000)
    seconds, ms = divmod(milliseconds, 1000)
    minutes, second = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    return f'{hour:02}:{minute:02}:{second:02},{ms:03}'


def export_transcript(result: Transcript) -> dict:
    identifier = uuid.uuid4().hex
    directory = EXPORT_ROOT / identifier
    directory.mkdir(mode=0o700)
    try:
        stem = re.sub(r'[^\w\u4e00-\u9fff.-]+', '_', result.title).strip('._')[:60] or 'transcript'
        txt = directory / f'{stem}.txt'
        md = directory / f'{stem}.md'
        txt.write_text(f'{result.title}\n来源：{result.url}\n转写方式：{result.source}\n\n{result.text}\n', encoding='utf-8')
        paragraphs = result.text.replace('\r', '').split('\n')
        md.write_text(f'# {_markdown(result.title)}\n\n来源：{result.url}\n\n转写方式：{_markdown(result.source)}\n\n'
                      + '\n\n'.join(_markdown(p) for p in paragraphs if p.strip()) + '\n', encoding='utf-8')
        files = [str(txt), str(md)]
        if result.segments:
            srt = directory / f'{stem}.srt'
            srt.write_text('\n\n'.join(f'{i}\n{_timestamp(s.start)} --> {_timestamp(s.end)}\n{s.text}'
                           for i, s in enumerate(result.segments, 1)) + '\n', encoding='utf-8')
            files.append(str(srt))
        return dict(id=identifier, title=result.title, text=result.text, source=result.source,
                    url=result.url, files=files, directory=str(directory),
                    created_at=datetime.now(timezone.utc).isoformat())
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def cleanup_history(history):
    for record in history or []:
        directory = Path(record.get('directory', '')).resolve()
        if directory.parent == EXPORT_ROOT.resolve():
            shutil.rmtree(directory, ignore_errors=True)


def add_to_history(record, history):
    history = list(history or [])
    cleanup_history(history[4:])
    return [record, *history[:4]]
