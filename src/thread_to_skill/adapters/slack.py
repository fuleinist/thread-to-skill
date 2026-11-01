"""Slack export adapter.

Supports the standard Slack workspace export layout::

    export/
      channels.json
      users.json
      general/
        2026-10-01.json
      engineering/
        2026-10-01.json

Also supports a single channel directory without ``channels.json``.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..models import Message, Thread
from ..util import parse_timestamp, read_json
from .common import clean_mentions, first_line, group_messages, make_thread


def _load_users(root: Path) -> dict[str, str]:
    users = read_json(root / "users.json", default=[]) or []
    mapping: dict[str, str] = {}
    if isinstance(users, list):
        for user in users:
            if not isinstance(user, dict):
                continue
            ident = str(user.get("id", ""))
            profile = user.get("profile") or {}
            name = profile.get("display_name") or profile.get("real_name") or user.get("real_name") or user.get("name") or ident
            if ident:
                mapping[ident] = str(name)
    return mapping


def _load_channels(root: Path) -> list[dict[str, str]]:
    channels = read_json(root / "channels.json", default=[]) or []
    result: list[dict[str, str]] = []
    if isinstance(channels, list):
        for channel in channels:
            if isinstance(channel, dict) and channel.get("id"):
                result.append({"id": str(channel["id"]), "name": str(channel.get("name") or channel["id"])})
    return result


def _channel_dirs(root: Path, channels: list[dict[str, str]]) -> list[tuple[str, str, Path]]:
    found: list[tuple[str, str, Path]] = []
    for channel in channels:
        for candidate in (root / channel["name"], root / channel["id"]):
            if candidate.is_dir():
                found.append((channel["id"], channel["name"], candidate))
                break
    if found:
        return found
    for candidate in sorted(root.iterdir()):
        if candidate.is_dir():
            found.append((candidate.name, candidate.name, candidate))
    return found


def _message(raw: dict[str, Any], users: dict[str, str]) -> Message:
    user = raw.get("user") or raw.get("bot_id") or "unknown"
    author = users.get(str(user), str(user))
    files = raw.get("files") or []
    attachments: list[str] = []
    for file in files:
        if isinstance(file, dict):
            attachments.append(str(file.get("name") or file.get("title") or "attachment"))
    reactions = []
    for reaction in raw.get("reactions") or []:
        if isinstance(reaction, dict):
            reactions.append({"name": reaction.get("name", ""), "count": reaction.get("count", 0)})
    text = clean_mentions(str(raw.get("text") or ""), users)
    subtype = raw.get("subtype")
    if subtype and not text:
        text = f"[{subtype}]"
    return Message(
        id=str(raw.get("ts") or raw.get("client_msg_id") or ""),
        ts=parse_timestamp(raw.get("ts") or ""),
        author=author,
        text=text,
        reply_to=(str(raw.get("thread_ts")) if raw.get("thread_ts") else None),
        reactions=reactions,
        attachments=attachments,
        meta={"subtype": subtype} if subtype else {},
    )


def load_slack(path: Path, gap_minutes: int, channel_override: str = "", title: str = "") -> list[Thread]:
    root = path if path.is_dir() else path.parent
    users = _load_users(root)
    channels = _load_channels(root)
    if path.is_file():
        channel_dirs = [(path.stem, channel_override or path.stem, path.parent)]
    else:
        channel_dirs = _channel_dirs(root, channels)

    threads: list[Thread] = []
    for channel_id, channel_name, channel_dir in channel_dirs:
        if channel_override:
            channel_name = channel_override
        if not channel_dir.is_dir():
            continue
        raw_messages: list[dict[str, Any]] = []
        for json_file in sorted(channel_dir.glob("*.json")):
            data = read_json(json_file, default=[])
            if isinstance(data, list):
                raw_messages.extend(item for item in data if isinstance(item, dict))
        if not raw_messages:
            continue
        parsed = [_message(item, users) for item in raw_messages]
        by_id = {message.id: message for message in parsed}
        roots: dict[str, list[Message]] = defaultdict(list)
        standalone: list[Message] = []
        for message in parsed:
            thread_ts = message.reply_to
            if thread_ts and thread_ts != message.id and thread_ts in by_id:
                roots[thread_ts].append(message)
            else:
                standalone.append(message)
        seen_roots: set[str] = set()
        for root_id, replies in roots.items():
            root_message = by_id.get(root_id)
            if not root_message:
                continue
            seen_roots.add(root_id)
            conversation = [root_message] + replies
            conversation.sort(key=lambda item: (item.ts, item.id))
            threads.append(
                make_thread(
                    source="slack",
                    channel=channel_name,
                    thread_key=f"{channel_id}:{root_id}",
                    messages=conversation,
                    title=title or first_line(root_message.text) or f"#{channel_name} thread",
                    raw_path=str(channel_dir),
                    extra={"channel_id": channel_id, "root_ts": root_id},
                )
            )
        remaining = [message for message in standalone if message.id not in seen_roots]
        for index, group in enumerate(group_messages(remaining, gap_minutes), start=1):
            threads.append(
                make_thread(
                    source="slack",
                    channel=channel_name,
                    thread_key=f"{channel_id}:window:{group[0].id}:{index}",
                    messages=group,
                    title=title or first_line(group[0].text) or f"#{channel_name} conversation",
                    raw_path=str(channel_dir),
                    extra={"channel_id": channel_id, "window": index},
                )
            )
    return [thread for thread in threads if thread.messages]