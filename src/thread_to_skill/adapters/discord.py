"""DiscordChatExporter JSON adapter.

A plain channel export is a message list, so its threads come from reply links
and quiet-gap windows. Forum and thread channels describe themselves in the
channel metadata instead: `channel.type` says what the file is, and
`channel.parentId` names the parent. A file that is one thread channel, or one
forum post, already holds exactly one conversation, so no reply link and no
quiet gap is needed to rebuild it, and the channel name is the thread name.

A forum file can hold several posts. Each message then names its own post
through `threadId`, `thread_id`, `meta.thread_id`, or `reference.channelId`, and
each post becomes one thread. A forum file whose messages carry no post key is
still one thread, never a set of quiet-gap windows.

A bundle is a dict whose `channels` or `threads` key holds entries, each with
its own messages. One entry is one thread. Text channels keep the reply-root
and quiet-gap path they had before.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import Message, Thread
from .common import clean_mentions, first_line, group_messages, make_thread

# Channel type names after the Guild prefix is dropped.
THREAD_TYPES = {"publicthread", "privatethread", "announcementthread", "thread"}
FORUM_TYPES = {"forum", "media"}


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


def _channel_kind(channel: dict[str, Any]) -> str:
    """Classify a channel as thread, forum, or text from its metadata."""
    raw_type = channel.get("type") if isinstance(channel, dict) else None
    name = str(raw_type or "").strip()
    if name.startswith("Guild"):
        # Exports write GuildPublicThread; the kind is the type after the prefix.
        name = name[len("Guild"):]
    name = name.lower()
    if name in THREAD_TYPES:
        return "thread"
    if name in FORUM_TYPES:
        return "forum"
    return "text"


def _post_key(raw: dict[str, Any]) -> str:
    """The post a message belongs to, from the first field that names it."""
    for value in (raw.get("threadId"), raw.get("thread_id")):
        if value:
            return str(value)
    meta = raw.get("meta")
    if isinstance(meta, dict) and meta.get("thread_id"):
        return str(meta["thread_id"])
    reference = raw.get("reference")
    if isinstance(reference, dict) and reference.get("channelId"):
        return str(reference["channelId"])
    return ""


def _post_name(raw: dict[str, Any]) -> str:
    """The name the export gives this message's post, when it carries one."""
    if raw.get("threadName"):
        return str(raw["threadName"])
    thread = raw.get("thread")
    if isinstance(thread, dict) and thread.get("name"):
        return str(thread["name"])
    return ""


def _ordered(messages: list[Message]) -> list[Message]:
    return sorted(messages, key=lambda message: (message.ts or "", message.id))


def _single_file_thread(
    *,
    path: Path,
    channel_name: str,
    channel_id: str,
    channel_type: Any,
    parent_id: str | None,
    parsed: list[Message],
    title: str,
) -> list[Thread]:
    """One file that is one thread channel or one forum post yields one thread."""
    ordered = _ordered(parsed)
    if not ordered:
        return []
    return [
        make_thread(
            source="discord",
            channel=channel_name,
            thread_key=f"{channel_id}:thread",
            messages=ordered,
            title=title or channel_name or first_line(ordered[0].text) or f"{channel_name} conversation",
            raw_path=str(path),
            extra={
                "channel_id": channel_id,
                "channel_type": channel_type,
                "parent_id": parent_id,
                "thread": True,
            },
        )
    ]


def _post_groups(records: list[dict[str, Any]], parsed: list[Message]) -> dict[str, list[tuple[dict[str, Any], Message]]]:
    groups: dict[str, list[tuple[dict[str, Any], Message]]] = {}
    for raw, message in zip(records, parsed):
        key = _post_key(raw)
        if key:
            groups.setdefault(key, []).append((raw, message))
    return groups


def _forum_threads(
    *,
    path: Path,
    channel_name: str,
    channel_id: str,
    channel_type: Any,
    parent_id: str | None,
    groups: dict[str, list[tuple[dict[str, Any], Message]]],
    title: str,
) -> list[Thread]:
    threads = []
    ordered_groups = sorted(
        groups.items(),
        key=lambda item: min((message.ts or "", message.id) for _, message in item[1]),
    )
    for post_id, group in ordered_groups:
        post_name = ""
        for raw, _ in group:
            post_name = _post_name(raw)
            if post_name:
                break
        threads.append(
            make_thread(
                source="discord",
                channel=channel_name,
                thread_key=f"{channel_id}:{post_id}",
                messages=_ordered([message for _, message in group]),
                title=title or post_name or f"{channel_name} post {post_id}",
                raw_path=str(path),
                extra={
                    "channel_id": channel_id,
                    "channel_type": channel_type,
                    "parent_id": parent_id,
                    "post_id": post_id,
                    "thread": True,
                },
            )
        )
    return threads


def _bundle_entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Entries of a bundle-shaped export, which has no top-level messages list."""
    for key in ("channels", "threads"):
        raw_entries = data.get(key)
        if not isinstance(raw_entries, list):
            continue
        entries = [
            item
            for item in raw_entries
            if isinstance(item, dict) and isinstance(item.get("messages"), list)
        ]
        if entries:
            return entries
    return []


def _bundle_threads(
    path: Path,
    entries: list[dict[str, Any]],
    channel_override: str,
    title: str,
) -> list[Thread]:
    threads = []
    for entry in entries:
        parsed = [_message(item) for item in entry.get("messages") or [] if isinstance(item, dict)]
        if not parsed:
            continue
        entry_id = str(entry.get("id") or entry.get("name") or path.stem)
        entry_name = str(entry.get("name") or "") or channel_override or path.stem
        ordered = _ordered(parsed)
        threads.append(
            make_thread(
                source="discord",
                channel=entry_name,
                thread_key=f"{entry_id}:channel",
                messages=ordered,
                title=title or entry_name or first_line(ordered[0].text) or f"{entry_name} conversation",
                raw_path=str(path),
                extra={"channel_id": entry_id, "channel_type": entry.get("type")},
            )
        )
    return threads


def load_discord(path: Path, gap_minutes: int, channel_override: str = "", title: str = "") -> list[Thread]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    channel = {}
    if isinstance(data, dict):
        # A bundle carries no top-level messages list, so check it first.
        entries = _bundle_entries(data)
        if entries:
            return [thread for thread in _bundle_threads(path, entries, channel_override, title) if thread.messages]
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
    records = [item for item in raw_messages if isinstance(item, dict)]
    parsed = [_message(item) for item in records]
    channel_type = channel.get("type") if isinstance(channel, dict) else None
    parent_id = str(channel.get("parentId")) if isinstance(channel, dict) and channel.get("parentId") else None

    kind = _channel_kind(channel)
    if kind == "thread":
        return _single_file_thread(
            path=path,
            channel_name=channel_name,
            channel_id=channel_id,
            channel_type=channel_type,
            parent_id=parent_id,
            parsed=parsed,
            title=title,
        )
    if kind == "forum":
        groups = _post_groups(records, parsed)
        if groups:
            return _forum_threads(
                path=path,
                channel_name=channel_name,
                channel_id=channel_id,
                channel_type=channel_type,
                parent_id=parent_id,
                groups=groups,
                title=title,
            )
        # A forum file with no post key is one post, so it is one thread.
        return _single_file_thread(
            path=path,
            channel_name=channel_name,
            channel_id=channel_id,
            channel_type=channel_type,
            parent_id=parent_id,
            parsed=parsed,
            title=title,
        )

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
