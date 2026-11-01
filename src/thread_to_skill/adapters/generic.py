"""Generic JSON, JSONL, Markdown, Telegram, and plain-text adapters."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..models import Message, Thread
from ..util import stable_id
from .common import first_line, group_messages, make_thread, message_from_generic


def _message_from_mapping(raw: dict[str, Any], index: int) -> Message:
    ident = raw.get("id") or raw.get("message_id") or raw.get("ts") or raw.get("timestamp") or str(index)
    timestamp = raw.get("ts") or raw.get("timestamp") or raw.get("date") or raw.get("time") or raw.get("created_at") or ""
    author = raw.get("author") or raw.get("user") or raw.get("from") or raw.get("sender") or raw.get("username") or "unknown"
    text = raw.get("text") or raw.get("content") or raw.get("message") or raw.get("body") or ""
    reply_to = raw.get("reply_to") or raw.get("reply_to_message_id") or raw.get("thread_ts")
    attachments = raw.get("attachments") or []
    if isinstance(attachments, list):
        attachments = [item.get("name") if isinstance(item, dict) else str(item) for item in attachments]
    reactions = raw.get("reactions") or []
    return message_from_generic(
        ident=str(ident),
        timestamp=timestamp,
        author=author,
        text=text,
        reply_to=reply_to,
        attachments=attachments,
        reactions=reactions,
        meta={key: value for key, value in raw.items() if key in {"thread_id", "channel", "conversation_id"}},
    )


def _threads_from_message_groups(
    groups: dict[str, list[Message]],
    *,
    source: str,
    channel: str,
    raw_path: str,
    title: str = "",
    extra: dict | None = None,
) -> list[Thread]:
    threads = []
    for key, messages in groups.items():
        if not messages:
            continue
        threads.append(
            make_thread(
                source=source,
                channel=channel,
                thread_key=key,
                messages=messages,
                title=title or first_line(messages[0].text) or f"{channel} conversation",
                raw_path=raw_path,
                extra=extra,
            )
        )
    return threads


def load_jsonl(path: Path, gap_minutes: int, channel_override: str = "", title: str = "") -> list[Thread]:
    raw_messages: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            raw_messages.append(item)
    parsed = [_message_from_mapping(item, index) for index, item in enumerate(raw_messages)]
    by_thread: dict[str, list[Message]] = defaultdict(list)
    ungrouped: list[Message] = []
    for raw, message in zip(raw_messages, parsed):
        thread_id = raw.get("thread_id") or raw.get("conversation_id")
        if thread_id:
            by_thread[str(thread_id)].append(message)
        else:
            ungrouped.append(message)
    if ungrouped:
        for index, group in enumerate(group_messages(ungrouped, gap_minutes), start=1):
            by_thread[f"window:{index}"].extend(group)
    return _threads_from_message_groups(
        by_thread,
        source="jsonl",
        channel=channel_override or path.stem,
        raw_path=str(path),
        title=title,
    )


def load_telegram(path: Path, gap_minutes: int, channel_override: str = "", title: str = "") -> list[Thread]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        raise ValueError("Unsupported Telegram export shape")
    channel = channel_override or str(data.get("name") or (data.get("chat") or {}).get("title") or path.stem)
    parsed: list[Message] = []
    for index, raw in enumerate(data["messages"]):
        if not isinstance(raw, dict):
            continue
        if raw.get("type") not in (None, "message"):
            continue
        author = raw.get("from") or raw.get("from_name") or "unknown"
        text = raw.get("text") or raw.get("caption") or ""
        parsed.append(_message_from_mapping({**raw, "author": author, "text": text}, index))
    by_thread: dict[str, list[Message]] = defaultdict(list)
    ungrouped: list[Message] = []
    by_id = {message.id: message for message in parsed}
    for raw, message in zip([item for item in data["messages"] if isinstance(item, dict) and item.get("type") in (None, "message")], parsed):
        reply_to = raw.get("reply_to_message_id")
        if reply_to and str(reply_to) in by_id:
            by_thread[str(reply_to)].append(message)
        else:
            ungrouped.append(message)
    for index, group in enumerate(group_messages(ungrouped, gap_minutes), start=1):
        by_thread[f"window:{index}"].extend(group)
    return _threads_from_message_groups(
        by_thread,
        source="telegram",
        channel=channel,
        raw_path=str(path),
        title=title,
        extra={"chat_id": data.get("id")},
    )


def load_json(path: Path, gap_minutes: int, channel_override: str = "", title: str = "") -> list[Thread]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        if data.get("guild") is not None or (isinstance(data.get("channel"), dict) and data.get("channel", {}).get("id")):
            from .discord import load_discord

            return load_discord(path, gap_minutes, channel_override, title)
        if any(key in data for key in ("name", "type", "chat")) and any(
            isinstance(item, dict) and ("from" in item or "from_id" in item)
            for item in data["messages"][:5]
        ):
            return load_telegram(path, gap_minutes, channel_override, title)
        raw_messages = data["messages"]
        channel = channel_override or str(data.get("channel") or data.get("name") or path.stem)
        parsed = [_message_from_mapping(item, index) for index, item in enumerate(raw_messages) if isinstance(item, dict)]
        groups: dict[str, list[Message]] = defaultdict(list)
        for raw, message in zip([item for item in raw_messages if isinstance(item, dict)], parsed):
            thread_id = raw.get("thread_id") or raw.get("conversation_id")
            groups[str(thread_id) if thread_id else "conversation"].append(message)
        return _threads_from_message_groups(groups, source="json", channel=channel, raw_path=str(path), title=title)
    if isinstance(data, list):
        raw_messages = [item for item in data if isinstance(item, dict)]
        channel = channel_override or path.stem
        parsed = [_message_from_mapping(item, index) for index, item in enumerate(raw_messages)]
        groups: dict[str, list[Message]] = defaultdict(list)
        for raw, message in zip(raw_messages, parsed):
            thread_id = raw.get("thread_id") or raw.get("conversation_id")
            groups[str(thread_id) if thread_id else "conversation"].append(message)
        if "conversation" in groups and len(groups) == 1:
            groups = {f"window:{index}": group for index, group in enumerate(group_messages(parsed, gap_minutes), start=1)}
        return _threads_from_message_groups(groups, source="json", channel=channel, raw_path=str(path), title=title)
    if isinstance(data, dict) and all(isinstance(value, list) for value in data.values()):
        threads: list[Thread] = []
        for key, values in data.items():
            raw_messages = [item for item in values if isinstance(item, dict)]
            parsed = [_message_from_mapping(item, index) for index, item in enumerate(raw_messages)]
            threads.extend(
                _threads_from_message_groups(
                    {key: parsed}, source="json", channel=channel_override or path.stem, raw_path=str(path), title=title
                )
            )
        return threads
    raise ValueError("Unsupported JSON export shape")


def load_markdown(path: Path, gap_minutes: int, channel_override: str = "", title: str = "") -> list[Thread]:
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    doc_title = title
    if not doc_title:
        for line in lines:
            if line.startswith("# "):
                doc_title = line[2:].strip()
                break
    sections = re.split(r"(?m)^##\s+", text)
    messages: list[Message] = []
    if len(sections) > 1:
        preamble = sections[0]
        if preamble.strip():
            messages.append(
                Message(
                    id="m1",
                    ts="",
                    author="document",
                    text=preamble.strip(),
                )
            )
        for index, section in enumerate(sections[1:], start=2):
            section_lines = section.splitlines()
            heading = section_lines[0].strip() if section_lines else f"Section {index}"
            body = "\n".join(section_lines[1:]).strip()
            messages.append(
                Message(
                    id=f"m{index}",
                    ts="",
                    author=f"section: {heading}",
                    text=(heading + "\n" + body).strip(),
                )
            )
    else:
        messages.append(Message(id="m1", ts="", author="document", text=text.strip()))
    return [
        make_thread(
            source="markdown",
            channel=channel_override or path.stem,
            thread_key="document",
            messages=messages,
            title=doc_title or path.stem,
            raw_path=str(path),
        )
    ]


def load_text(path: Path, gap_minutes: int, channel_override: str = "", title: str = "") -> list[Thread]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return [
        make_thread(
            source="text",
            channel=channel_override or path.stem,
            thread_key="document",
            messages=[Message(id=stable_id(path.name, length=8), ts="", author="document", text=text.strip())],
            title=title or path.stem,
            raw_path=str(path),
        )
    ]