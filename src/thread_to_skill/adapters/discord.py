"""DiscordChatExporter JSON adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import Message, Thread
from .common import clean_mentions, first_line, group_messages, make_thread


def _text(raw: dict[str, Any]) -> str:
    content = str(raw.get("content") or "")
    content = clean_mentions(content)
    # Custom emoji: <:party:123456> -> :party:
    import re

    content = re.sub(r"<a?:([a-zA-Z0-9_]+):\d+>", r":\1:", content)
    return content


def _message(raw: dict[str, Any]) -> Message:
    author = raw.get("author") or {}
    if isinstance(author, dict):
        author_name = author.get("name") or author.get("nickname") or author.get("id") or "unknown"
    else:
        author_name = str(author)
    attachments = []
    for attachment in raw.get("attachments") or []:
        if isinstance(attachment, dict):
            attachments.append(str(attachment.get("fileName") or attachment.get("filename") or attachment.get("url") or "attachment"))
    reactions = []
    for reaction in raw.get("reactions") or []:
        if isinstance(reaction, dict):
            emoji = reaction.get("emoji") or {}
            name = emoji.get("name") if isinstance(emoji, dict) else str(emoji)
            reactions.append({"name": name or "", "count": reaction.get("count", 0)})
    reference = raw.get("reference") or {}
    reply_to = None
    if isinstance(reference, dict):
        reply_to = reference.get("messageId") or reference.get("message_id")
    return Message(
        id=str(raw.get("id") or ""),
        ts=str(raw.get("timestamp") or raw.get("editedTimestamp") or ""),
        author=str(author_name),
        text=_text(raw),
        reply_to=str(reply_to) if reply_to else None,
        reactions=reactions,
        attachments=attachments,
    )


def load_discord(path: Path, gap_minutes: int, channel_override: str = "", title: str = "") -> list[Thread]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    channel = {}
    if isinstance(data, dict):
        channel = data.get("channel") or {}
        raw_messages = data.get("messages") or []
    elif isinstance(data, list):
        raw_messages = data
    else:
        raise ValueError("Unsupported Discord export shape")
    if not isinstance(raw_messages, list):
        raise ValueError("Discord export has no messages list")
    channel_name = channel_override or (channel.get("name") if isinstance(channel, dict) else "") or path.stem
    channel_id = str(channel.get("id") or channel_name) if isinstance(channel, dict) else channel_name
    parsed = [_message(item) for item in raw_messages if isinstance(item, dict)]
    by_id = {message.id: message for message in parsed}
    children: dict[str, list[Message]] = {}
    attached: set[str] = set()
    for message in parsed:
        if message.reply_to and message.reply_to in by_id and message.reply_to != message.id:
            children.setdefault(message.reply_to, []).append(message)
            attached.add(message.id)
    roots = [message for message in parsed if message.id not in attached]
    threads: list[Thread] = []
    threaded_roots: set[str] = set()
    for root in roots:
        replies = children.get(root.id, [])
        if replies:
            threaded_roots.add(root.id)
            conversation = sorted([root] + replies, key=lambda item: (item.ts, item.id))
            threads.append(
                make_thread(
                    source="discord",
                    channel=channel_name,
                    thread_key=f"{channel_id}:{root.id}",
                    messages=conversation,
                    title=title or first_line(root.text) or f"{channel_name} thread",
                    raw_path=str(path),
                    extra={"channel_id": channel_id, "root_id": root.id},
                )
            )
    remaining = [message for message in roots if message.id not in threaded_roots]
    for index, group in enumerate(group_messages(remaining, gap_minutes), start=1):
        threads.append(
            make_thread(
                source="discord",
                channel=channel_name,
                thread_key=f"{channel_id}:window:{group[0].id}:{index}",
                messages=group,
                title=title or first_line(group[0].text) or f"{channel_name} conversation",
                raw_path=str(path),
                extra={"channel_id": channel_id, "window": index},
            )
        )
    return [thread for thread in threads if thread.messages]