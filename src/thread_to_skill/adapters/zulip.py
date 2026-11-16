"""Zulip export adapter.

Zulip groups conversations by topic, which the export stores in ``subject``.
A Zulip export is a JSON message list, or an object whose ``messages`` key
holds that list. There are no reply ids, so a quiet-gap window would merge
unrelated topics into one conversation.

Each non-empty subject becomes one thread, and the subject becomes the title.
The stream name comes from ``display_recipient``. A direct message carries a
recipient list there instead, so it becomes ``direct`` and falls back to the
quiet-gap grouping that the other adapters use.

Zulip content is Markdown with Zulip extensions: ``@**Name**`` mentions,
``@_**Name**_`` silent mentions, and ``#**stream**`` links. Those markers are
removed for readable text. Fenced code blocks and inline code stay verbatim,
because they carry the commands a skill must keep.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import Message, Thread
from .common import first_line, group_messages, make_thread

SOURCE = "zulip"

# Detection markers. The scan is cheap, so a large export is not parsed twice.
DETECT_MARKERS = ('"sender_full_name"', '"subject"')

FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
SILENT_MENTION_RE = re.compile(r"@_\*\*(.+?)\*\*_")
MENTION_RE = re.compile(r"@\*\*(.+?)\*\*")
STREAM_RE = re.compile(r"#\*\*(.+?)\*\*")
LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")


def is_zulip_json_text(text: str) -> bool:
    """Marker scan for large exports, so detection never parses a full file."""
    low = (text or "").lower()
    if not all(marker in low for marker in DETECT_MARKERS):
        return False
    return '"timestamp"' in low and '"content"' in low


def render_content(text: str) -> str:
    """Strip Zulip Markdown markers, but keep code verbatim."""
    if not text:
        return ""
    protected: list[str] = []

    def stash(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"\x00{len(protected) - 1}\x00"

    body = FENCE_RE.sub(stash, text)
    body = INLINE_CODE_RE.sub(stash, body)
    body = SILENT_MENTION_RE.sub(lambda match: "@" + match.group(1), body)
    body = MENTION_RE.sub(lambda match: "@" + match.group(1), body)
    body = STREAM_RE.sub(lambda match: "#" + match.group(1), body)
    body = LINK_RE.sub(lambda match: f"{match.group(1)} ({match.group(2)})", body)
    body = BOLD_RE.sub(lambda match: match.group(1), body)
    body = ITALIC_RE.sub(lambda match: match.group(1), body)
    for index, value in enumerate(protected):
        body = body.replace(f"\x00{index}\x00", value)
    return body


def _iso_utc(value: object) -> str:
    """Zulip timestamps are seconds since the epoch, always UTC."""
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _channel_of(raw: dict[str, Any], fallback: str) -> str:
    recipient = raw.get("display_recipient")
    if isinstance(recipient, str) and recipient.strip():
        return recipient.strip()
    if isinstance(recipient, list):
        return "direct"
    return fallback


def _author_of(raw: dict[str, Any]) -> str:
    name = str(raw.get("sender_full_name") or "").strip()
    if name:
        return name
    email = str(raw.get("sender_email") or "").strip()
    if email:
        return email.split("@", 1)[0] or email
    return "unknown"


def _records(data: Any) -> list[dict[str, Any]]:
    """Messages from a raw list, or from the messages list of an object."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        value = data.get("messages")
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _message_from_export(raw: dict[str, Any], index: int, channel: str) -> Message:
    ident = str(raw.get("id") or "").strip()
    subject = str(raw.get("subject") or "").strip()
    return Message(
        id=ident or f"zulip-message-{index}",
        ts=_iso_utc(raw.get("timestamp")),
        author=_author_of(raw),
        text=render_content(str(raw.get("content") or "")),
        meta={
            "zulip_subject": subject,
            "zulip_type": str(raw.get("type") or ""),
            "zulip_channel": channel,
        },
    )


def load_zulip(
    path: Path,
    gap_minutes: int,
    channel_override: str = "",
    title: str = "",
) -> list[Thread]:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    records = _records(data)
    if not records:
        return []

    fallback_channel = channel_override or path.stem
    by_channel: dict[str, list[Message]] = {}
    for index, raw in enumerate(records):
        channel = channel_override or _channel_of(raw, fallback_channel)
        by_channel.setdefault(channel, []).append(_message_from_export(raw, index, channel))

    topic_count = sum(
        1 for messages in by_channel.values() for message in messages if message.meta["zulip_subject"]
    )
    untopiced_count = sum(
        1 for messages in by_channel.values() for message in messages if not message.meta["zulip_subject"]
    )
    shared_extra = {
        "zulip_message_count": len(records),
        "zulip_topic_count": topic_count,
        "zulip_untopiced_count": untopiced_count,
    }

    threads: list[Thread] = []
    windows: dict[str, list[Message]] = {}
    for channel, items in by_channel.items():
        topics: dict[str, list[Message]] = {}
        untopiced: list[Message] = []
        for message in items:
            subject = str(message.meta["zulip_subject"] or "")
            if subject:
                topics.setdefault(subject, []).append(message)
            else:
                untopiced.append(message)
        for subject, group in topics.items():
            group.sort(key=lambda item: (item.ts or "", item.id))
            threads.append(
                make_thread(
                    source=SOURCE,
                    channel=channel,
                    thread_key=f"{channel}:topic:{subject}",
                    messages=group,
                    title=title or subject,
                    raw_path=str(path),
                    extra=dict(shared_extra, zulip_topic=subject),
                )
            )
        if untopiced:
            windows[channel] = untopiced

    for channel, items in windows.items():
        for index, group in enumerate(group_messages(items, gap_minutes), start=1):
            threads.append(
                make_thread(
                    source=SOURCE,
                    channel=channel,
                    thread_key=f"{channel}:window:{group[0].id}:{index}",
                    messages=group,
                    title=title or first_line(group[0].text) or f"{channel} conversation",
                    raw_path=str(path),
                    extra=dict(shared_extra, window=index),
                )
            )

    threads.sort(key=lambda thread: (thread.started_at or "", thread.thread_key))
    return [thread for thread in threads if thread.messages]