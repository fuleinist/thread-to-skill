"""Shared adapter helpers."""

from __future__ import annotations

import datetime as dt
import re
from typing import Iterable, Sequence

from ..models import Message, Thread
from ..util import parse_timestamp

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def parse_iso(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except ValueError:
        return None


def group_messages(
    messages: Sequence[Message],
    gap_minutes: int,
) -> list[list[Message]]:
    """Group messages into conversation windows separated by a quiet gap."""
    ordered = sorted(messages, key=lambda message: (message.ts or "", message.id))
    if not ordered:
        return []
    gap = dt.timedelta(minutes=max(1, gap_minutes))
    groups: list[list[Message]] = [[ordered[0]]]
    previous = parse_iso(ordered[0].ts)
    for message in ordered[1:]:
        current = parse_iso(message.ts)
        if previous and current and current - previous > gap:
            groups.append([message])
            previous = current
        else:
            groups[-1].append(message)
            if current:
                previous = current
    return groups


def make_thread(
    *,
    source: str,
    channel: str,
    thread_key: str,
    messages: Sequence[Message],
    title: str,
    raw_path: str,
    extra: dict | None = None,
) -> Thread:
    return Thread.create(
        source=source,
        channel=channel,
        thread_key=thread_key,
        messages=list(messages),
        title=title,
        raw_path=raw_path,
        extra=extra,
    )


def first_line(text: str, limit: int = 80) -> str:
    for line in (text or "").splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned[:limit]
    return ""


def clean_mentions(text: str, users: dict[str, str] | None = None) -> str:
    users = users or {}

    def user_repl(match: re.Match[str]) -> str:
        ident = match.group(1)
        return "@" + users.get(ident, ident)

    text = re.sub(r"<@([A-Z0-9]+)(?:\|[^>]+)?>", user_repl, text)
    text = re.sub(r"<#([A-Z0-9]+)(?:\|([^>]+))?>", lambda m: "#" + (m.group(2) or m.group(1)), text)
    text = re.sub(r"<!([a-zA-Z]+)(?:\|[^>]+)?>", lambda m: "@" + m.group(1), text)
    text = re.sub(r"<((?:https?|mailto):[^>|]+)\|([^>]+)>", lambda m: f"{m.group(2)} ({m.group(1)})", text)
    text = re.sub(r"<((?:https?|mailto):[^>]+)>", lambda m: m.group(1), text)
    return text


def is_system_author(author: str) -> bool:
    low = (author or "").strip().lower()
    return low in {"system", "whatsapp", "telegram", "slackbot", "discord"} or low.endswith(" bot")


def message_from_generic(
    *,
    ident: str,
    timestamp: object,
    author: object,
    text: object,
    reply_to: object = None,
    attachments: Iterable[object] = (),
    reactions: Iterable[object] = (),
    meta: dict | None = None,
) -> Message:
    if isinstance(author, dict):
        author_value = author.get("display_name") or author.get("name") or author.get("username") or "unknown"
    else:
        author_value = str(author or "unknown")
    if isinstance(text, list):
        parts: list[str] = []
        for item in text:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        text_value = "".join(parts)
    else:
        text_value = str(text or "")
    return Message(
        id=str(ident),
        ts=parse_timestamp(timestamp),
        author=author_value,
        text=clean_mentions(text_value),
        reply_to=str(reply_to) if reply_to not in (None, "") else None,
        reactions=[item if isinstance(item, dict) else {"name": str(item)} for item in reactions],
        attachments=[str(item) for item in attachments],
        meta=dict(meta or {}),
    )