"""WhatsApp text export adapter (iOS and Android formats)."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from ..models import Message, Thread
from ..util import stable_id
from .common import first_line, group_messages, make_thread

IOS_RE = re.compile(
    r"^\[(\d{1,2}/\d{1,2}/\d{2,4}),\s+(\d{1,2}:\d{2}(?::\d{2})?)\s*([AaPp][Mm])?\]\s*([^:]{1,80}):\s?(.*)$"
)
ANDROID_RE = re.compile(
    r"^(\d{1,2}[/.]\d{1,2}[/.]\d{2,4}),\s+(\d{1,2}:\d{2}(?::\d{2})?)\s*([AaPp][Mm])?\s*-\s*([^:]{1,80}):\s?(.*)$"
)
SYSTEM_RE = re.compile(
    r"^\[?(\d{1,2}[/.]\d{1,2}[/.]\d{2,4}),\s+(\d{1,2}:\d{2}(?::\d{2})?)\s*([AaPp][Mm])?\]?\s*-\s*(.*)$"
)
MEDIA_MARKERS = ("<media omitted>", "image omitted", "video omitted", "audio omitted", "document omitted", "sticker omitted", "gif omitted")


def _parse_date(date_text: str, time_text: str, meridiem: str | None) -> str:
    date_text = date_text.replace(".", "/")
    formats = ["%d/%m/%Y", "%d/%m/%y", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%Y/%m/%d"]
    parsed: dt.datetime | None = None
    for fmt in formats:
        try:
            parsed_date = dt.datetime.strptime(date_text, fmt)
            parsed = parsed_date
            break
        except ValueError:
            continue
    if parsed is None:
        return date_text + " " + time_text
    time_formats = ["%H:%M:%S", "%H:%M"] if not meridiem else ["%I:%M:%S %p", "%I:%M %p"]
    for fmt in time_formats:
        try:
            parsed_time = dt.datetime.strptime(time_text + (" " + meridiem.upper() if meridiem else ""), fmt)
            parsed = parsed.replace(hour=parsed_time.hour, minute=parsed_time.minute, second=parsed_time.second)
            break
        except ValueError:
            continue
    return parsed.replace(tzinfo=dt.timezone.utc).isoformat()


def load_whatsapp(path: Path, gap_minutes: int, channel_override: str = "", title: str = "") -> list[Thread]:
    raw = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            raw = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    if raw is None:
        raise ValueError(f"Could not decode {path}")

    messages: list[Message] = []
    current: Message | None = None
    for line in raw.splitlines():
        match = IOS_RE.match(line) or ANDROID_RE.match(line)
        if match:
            date_text, time_text, meridiem, author, text = match.groups()
            author = author.strip()
            text = text.strip()
            attachments = []
            if any(marker in text.lower() for marker in MEDIA_MARKERS):
                attachments = [text]
            current = Message(
                id=stable_id("whatsapp", date_text, time_text, author, text, length=10),
                ts=_parse_date(date_text, time_text, meridiem),
                author=author,
                text=text,
                attachments=attachments,
            )
            messages.append(current)
            continue
        system_match = SYSTEM_RE.match(line)
        if system_match:
            date_text, time_text, meridiem, text = system_match.groups()
            text = text.strip()
            if not text:
                continue
            if "end-to-end encrypted" in text.lower() or "changed the subject" in text.lower():
                continue
            current = Message(
                id=stable_id("whatsapp-system", date_text, time_text, text, length=10),
                ts=_parse_date(date_text, time_text, meridiem),
                author="system",
                text=text,
            )
            messages.append(current)
            continue
        if current is not None and line.strip():
            current.text += "\n" + line.rstrip()
    channel = channel_override or path.stem
    threads: list[Thread] = []
    for index, group in enumerate(group_messages(messages, gap_minutes), start=1):
        threads.append(
            make_thread(
                source="whatsapp",
                channel=channel,
                thread_key=f"window:{group[0].id}:{index}",
                messages=group,
                title=title or first_line(group[0].text) or f"{channel} conversation",
                raw_path=str(path),
                extra={"window": index},
            )
        )
    return threads