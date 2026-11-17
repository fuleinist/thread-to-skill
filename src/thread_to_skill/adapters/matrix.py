"""Matrix room export adapter.

A Matrix room export is a JSON event list, or an object whose ``messages`` key
holds that list. Only ``m.room.message`` events become messages. Membership,
topic, state, and reaction events are skipped, because they describe the room
rather than the conversation.

Reply chains come from ``content.m.relates_to.m.in_reply_to.event_id``, which
points at the parent event id. One shared builder maps that parent to the thread
root and groups everything else on the quiet gap, so a reply to a reply lands
on the same root as the other adapters.

Encrypted rooms export ``m.room.encrypted`` events whose content is Megolm
ciphertext. The body is not readable offline, so an encrypted export fails with
an actionable error instead of returning an empty thread.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import Attachment, Message, Thread
from .common import first_line, group_messages, make_thread

SOURCE = "matrix"

# The only event type that carries conversation text.
MESSAGE_TYPE = "m.room.message"

# Encryption markers. A Megolm event carries ciphertext, so the check runs
# before any thread is built.
ENCRYPTED_TYPE = "m.room.encrypted"
MEGOLM_ALGORITHMS = ("m.megolm.v1.aes-sha2",)

ENCRYPTED_ERROR = (
    "Matrix export is encrypted: m.room.encrypted events use the Megolm "
    "algorithm m.megolm.v1.aes-sha2, so the message bodies are ciphertext. "
    "Decrypt the export first - for example with matrix-commander, Element, "
    "or a Matrix Sync API export with the room key backup - then run it again."
)

# Event keys read by the adapter. Detection uses the message and encryption
# markers only, so a large export is never parsed twice.
DETECT_MARKERS = ('"m.room.message"', '"m.room.encrypted"')
REPLY_KEY = "m.relates_to"
IN_REPLY_KEY = "m.in_reply_to"

# Message types that carry a file. The message body is the reader-facing label.
FILE_KINDS = {
    "m.image": "image",
    "m.file": "file",
    "m.audio": "audio",
    "m.video": "video",
}


def _file_attachment(content: dict[str, Any]) -> Attachment | None:
    """One attachment from a file event, or None for a plain text event.

    The body of a file event repeats the filename, so it becomes ``text`` only
    when it says something more than the name.
    """
    msgtype = str(content.get("msgtype") or "").strip().lower()
    kind = FILE_KINDS.get(msgtype, "")
    url = str(content.get("url") or "").strip()
    filename = str(content.get("filename") or "").strip()
    body = str(content.get("body") or "").strip()
    if not (kind or url or filename):
        return None
    name = filename or body
    text = body if body and body != filename else ""
    return Attachment(kind=kind or "file", name=name, url=url, text=text)


def is_matrix_json_text(text: str) -> bool:
    """Marker scan for large exports, so detection never parses a full file."""
    low = (text or "").lower()
    if not any(marker in low for marker in DETECT_MARKERS):
        return False
    return '"origin_server_ts"' in low or '"event_id"' in low


def _clean_id(value: object) -> str:
    return str(value or "").strip()


def _display_name(sender: object) -> str:
    """Matrix user id to display name: @alice:example.org becomes alice."""
    text = str(sender or "").strip()
    if text.startswith("@"):
        text = text[1:]
    local = text.split(":", 1)[0].strip()
    return local or "unknown"


def _iso_utc(value: object) -> str:
    """origin_server_ts is milliseconds since the epoch, always UTC."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return ""
    try:
        return datetime.fromtimestamp(number / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _events(data: Any) -> list[dict[str, Any]]:
    """Events from a raw list, or from the messages list of an object."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        value = data.get("messages")
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _is_encrypted(event: dict[str, Any]) -> bool:
    content = event.get("content")
    if not isinstance(content, dict):
        content = {}
    if str(event.get("type") or "").lower() == ENCRYPTED_TYPE:
        return True
    return str(content.get("algorithm") or "").lower() in MEGOLM_ALGORITHMS


def _event_message(raw: dict[str, Any], index: int) -> Message:
    content = raw.get("content") if isinstance(raw.get("content"), dict) else {}
    relates = content.get(REPLY_KEY) if isinstance(content.get(REPLY_KEY), dict) else {}
    in_reply = relates.get(IN_REPLY_KEY) if isinstance(relates.get(IN_REPLY_KEY), dict) else {}
    ident = _clean_id(raw.get("event_id") or "")
    attachment = _file_attachment(content)
    return Message(
        id=ident or f"matrix-event-{index}",
        ts=_iso_utc(raw.get("origin_server_ts")),
        author=_display_name(raw.get("sender")),
        text=str(content.get("body") or ""),
        reply_to=_clean_id(in_reply.get("event_id") or "") or None,
        attachments=[attachment] if attachment else [],
        meta={"matrix_event_type": str(raw.get("type") or "")},
    )


def _root_of(ident: str, parent_of: dict[str, str], by_id: dict[str, Message]) -> str:
    seen: set[str] = set()
    current = ident
    while True:
        if current in seen:
            return ""
        seen.add(current)
        parent = parent_of.get(current, "")
        if not parent or parent == current or parent not in by_id:
            return current if current in by_id else ""
        current = parent


def _threads_from_messages(
    messages: list[Message],
    *,
    channel: str,
    channel_id: str,
    raw_path: str,
    gap_minutes: int,
    title: str,
    extra: dict[str, Any],
) -> list[Thread]:
    by_id = {message.id: message for message in messages}
    parent_of = {message.id: (message.reply_to or "") for message in messages}

    grouped: dict[str, list[Message]] = {}
    root_ids: set[str] = set()
    standalone: list[Message] = []
    for message in messages:
        parent = message.reply_to or ""
        if parent and parent != message.id and parent in by_id:
            root = _root_of(parent, parent_of, by_id)
            if root:
                grouped.setdefault(root, []).append(message)
                root_ids.add(root)
                continue
        standalone.append(message)

    threads: list[Thread] = []
    for root_id, replies in grouped.items():
        root_message = by_id[root_id]
        conversation = [root_message] + replies
        conversation.sort(key=lambda item: (item.ts or "", item.id))
        threads.append(
            make_thread(
                source=SOURCE,
                channel=channel,
                thread_key=f"{channel_id}:{root_id}",
                messages=conversation,
                title=title or first_line(root_message.text) or f"{channel} thread",
                raw_path=raw_path,
                extra=dict(extra, root_message_id=root_id),
            )
        )

    remaining = [message for message in standalone if message.id not in root_ids]
    for index, group in enumerate(group_messages(remaining, gap_minutes), start=1):
        threads.append(
            make_thread(
                source=SOURCE,
                channel=channel,
                thread_key=f"{channel_id}:window:{group[0].id}:{index}",
                messages=group,
                title=title or first_line(group[0].text) or f"{channel} conversation",
                raw_path=raw_path,
                extra=dict(extra, window=index),
            )
        )

    threads.sort(key=lambda thread: (thread.started_at or "", thread.thread_key))
    return [thread for thread in threads if thread.messages]


def load_matrix(
    path: Path,
    gap_minutes: int,
    channel_override: str = "",
    title: str = "",
) -> list[Thread]:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    events = _events(data)
    if not events:
        raise ValueError("Matrix export held no events. Expected an event list or a messages list.")
    if any(_is_encrypted(event) for event in events):
        raise ValueError(ENCRYPTED_ERROR)

    messages = [
        _event_message(event, index)
        for index, event in enumerate(events)
        if str(event.get("type") or "").lower() == MESSAGE_TYPE
    ]
    if not messages:
        return []

    channel = channel_override or path.stem
    return _threads_from_messages(
        messages,
        channel=channel,
        channel_id=path.stem,
        raw_path=str(path),
        gap_minutes=gap_minutes,
        title=title,
        extra={
            "matrix_event_count": len(events),
            "matrix_message_count": len(messages),
            "matrix_skipped_count": len(events) - len(messages),
        },
    )
