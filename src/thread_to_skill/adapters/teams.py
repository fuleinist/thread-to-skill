"""Microsoft Teams export adapter.

Two offline artifacts are supported:

1. The HTML "Download history" export. This is the primary target, because it
   needs no tenant admin access. Teams markup varies by tenant and client
   version, so containers, authors, and timestamps are found by marker and by
   shape, never by one hard-coded class name.
2. Graph API JSON. A ``/messages`` list, a ``/replies`` list, or one list where
   each message carries its own ``replies`` array.

Both paths normalize into ``Message`` rows first. One shared builder then maps
``reply_to`` to the thread root and groups the remaining messages on the quiet
gap, so the two shapes cannot drift apart.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import warnings
from pathlib import Path
from typing import Any, Iterator

from ..models import Attachment, Message, Thread
from ..util import parse_timestamp
from .common import first_line, group_messages, make_thread
from .html_transcript import BLOCK_TAGS, _Node, _TreeBuilder, _collapse, _looks_timestamp

SOURCE = "teams"

# A page counts as a Teams export when one of these markers is present. The
# markers cover the legacy ``ts-message`` classes and the newer data-tid tree.
HTML_MARKERS = (
    "ts-message",
    'data-tid="message-',
    "data-tid='message-",
    "message-author-name",
    "teams.microsoft.com",
)

# Class tokens and data-tid values that mark one message container.
CONTAINER_TOKENS = {
    "message",
    "ts-message",
    "message-container",
    "chat-message",
    "message-block",
    "message-item",
    "message-thread-item",
}

# A quoted reply repeats earlier text. It is dropped from the body so the
# transcript keeps one copy of each message.
QUOTE_HINT_RE = re.compile(r"(quote|reply-preview|replypreview|original-message)", re.I)

# Node hints for the author line and the timestamp line.
AUTHOR_HINT_RE = re.compile(
    r"(author|sender|displayname|display-name|from-name|username|user-name|message-from)", re.I
)
STAMP_HINT_RE = re.compile(r"(time|timestamp|date|stamp)", re.I)

SPACE_RE = re.compile(r"\s+")
BREAK_RE = re.compile(r"(?i)<\s*(?:br|/p|/div|/li|/tr)\s*/?>")
TAG_RE = re.compile(r"<[^>]+>")


def is_teams_html(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in HTML_MARKERS)


def is_teams_json_text(text: str) -> bool:
    """Marker scan for large exports, so detection never parses a full file."""
    low = (text or "").lower()
    if '"createddatetime"' not in low:
        return False
    return '"replytoid"' in low or '"messagetype"' in low


def _clean_id(value: object) -> str:
    return str(value or "").strip().strip("<>").strip()


def _attrs_text(node: _Node) -> str:
    return " ".join(
        [
            str(node.attrs.get("class") or ""),
            str(node.attrs.get("id") or ""),
            str(node.attrs.get("data-tid") or ""),
        ]
    )


def _walk(node: _Node) -> Iterator[_Node]:
    for child in node.children:
        if isinstance(child, str) or child.skip:
            continue
        yield child
        yield from _walk(child)


def _is_quote_node(node: _Node) -> bool:
    return bool(QUOTE_HINT_RE.search(_attrs_text(node)))


def _is_container(node: _Node) -> bool:
    tokens = {token for token in str(node.attrs.get("class") or "").split() if token}
    if tokens & CONTAINER_TOKENS:
        return True
    if node.attrs.get("data-message-id"):
        return True
    return str(node.attrs.get("data-tid") or "") in CONTAINER_TOKENS


def _find_containers(root: _Node) -> list[_Node]:
    found: list[_Node] = []

    def visit(node: _Node) -> None:
        for child in node.children:
            if isinstance(child, str) or child.skip:
                continue
            if _is_container(child):
                found.append(child)
            else:
                visit(child)

    visit(root)
    return found


def _find_author(container: _Node) -> tuple[_Node | None, str]:
    for node in _walk(container):
        text = node.text()
        if not text or len(text) > 60 or "\n" in text:
            continue
        if _looks_timestamp(text):
            continue
        if AUTHOR_HINT_RE.search(_attrs_text(node)):
            return node, text
    # Defensive fallback for a tenant that keeps the author in a plain label.
    for node in _walk(container):
        if node.tag not in {"span", "b", "strong", "a"}:
            continue
        text = node.text()
        if text and len(text) <= 40 and not _looks_timestamp(text) and not text.endswith("."):
            return node, text
    return None, ""


def _find_stamp(container: _Node) -> tuple[str, str]:
    for node in _walk(container):
        value = str(node.attrs.get("datetime") or node.attrs.get("data-datetime") or "")
        if value:
            return value, parse_timestamp(value)
        if node.tag == "time" or STAMP_HINT_RE.search(_attrs_text(node)):
            text = node.text()
            if _looks_timestamp(text):
                return text, parse_timestamp(text)
    for node in _walk(container):
        text = node.text()
        if _looks_timestamp(text):
            return text, parse_timestamp(text)
    return "", ""


def _text_without(node: _Node, excluded_nodes: list[_Node], excluded_texts: set[str]) -> str:
    """Text under this node without the author line, timestamp, and quotes."""
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            text = _collapse(child)
            if text and text not in excluded_texts:
                parts.append(text)
            continue
        if child.skip or child in excluded_nodes or _is_quote_node(child):
            continue
        text = _text_without(child, excluded_nodes, excluded_texts)
        if text:
            parts.append(text)
    separator = "\n" if node.tag in BLOCK_TAGS else " "
    return separator.join(part for part in parts if part).strip()


def _html_message(container: _Node, index: int) -> Message:
    author_node, author = _find_author(container)
    stamp_text, stamp = _find_stamp(container)
    excluded_nodes = [author_node] if author_node is not None else []
    excluded_texts = {author} if author else set()
    if stamp_text:
        excluded_texts.add(stamp_text)
    body = _text_without(container, excluded_nodes, excluded_texts)
    ident = _clean_id(
        container.attrs.get("data-message-id") or container.attrs.get("id") or ""
    ) or f"teams-html-{index}"
    reply_to = _clean_id(
        container.attrs.get("data-reply-to")
        or container.attrs.get("data-replyto")
        or container.attrs.get("data-parent-id")
        or ""
    )
    meta = {"teams_shape": "html"}
    if stamp_text:
        meta["raw_timestamp"] = stamp_text
    return Message(
        id=ident,
        ts=stamp,
        author=author or "unknown",
        text=body,
        reply_to=reply_to or None,
        meta=meta,
    )


def _html_to_text(value: str) -> str:
    text = BREAK_RE.sub("\n", value or "")
    text = TAG_RE.sub(" ", text)
    text = html_lib.unescape(text)
    lines = [_collapse_part(line) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _collapse_part(text: str) -> str:
    return SPACE_RE.sub(" ", text).strip()


def _plain_text(value: str) -> str:
    lines = [_collapse_part(line) for line in (value or "").splitlines()]
    return "\n".join(line for line in lines if line)


def _members(data: Any) -> dict[str, str]:
    """Display names keyed by user id, from any participant list shape."""
    mapping: dict[str, str] = {}
    if not isinstance(data, dict):
        return mapping
    for key in ("participants", "members", "users"):
        value = data.get(key)
        if isinstance(value, dict):
            for ident, item in value.items():
                if isinstance(item, str):
                    mapping[str(ident)] = item
                elif isinstance(item, dict):
                    label = item.get("displayName") or item.get("name")
                    if label:
                        mapping[str(ident)] = str(label)
        elif isinstance(value, list):
            for item in value:
                if not isinstance(item, dict):
                    continue
                ident = str(item.get("id") or item.get("userId") or "")
                label = item.get("displayName") or item.get("name")
                if ident and label:
                    mapping[ident] = str(label)
    return mapping


def _looks_graph_message(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if not any(key in item for key in ("createdDateTime", "replyToId", "messageType")):
        return False
    return "body" in item or "from" in item


def _graph_items(data: Any) -> list[dict[str, Any]]:
    """Graph message objects from a list, a value envelope, or a messages key."""
    if isinstance(data, list):
        items = [item for item in data if isinstance(item, dict)]
        return items if any(_looks_graph_message(item) for item in items[:5]) else []
    if isinstance(data, dict):
        for key in ("value", "messages"):
            value = data.get(key)
            if isinstance(value, list):
                items = [item for item in value if isinstance(item, dict)]
                if any(_looks_graph_message(item) for item in items[:5]):
                    return items
    return []


def _flatten_graph(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One flat list, with nested replies carrying their parent id."""
    ordered: dict[str, dict[str, Any]] = {}

    def visit(item: dict[str, Any], parent_id: str = "") -> None:
        ident = _clean_id(item.get("id") or "")
        if not ident or ident in ordered:
            return
        copy = dict(item)
        if parent_id and not _clean_id(item.get("replyToId") or ""):
            copy["replyToId"] = parent_id
        ordered[ident] = copy
        for reply in item.get("replies") or []:
            if isinstance(reply, dict):
                visit(reply, ident)

    for item in items:
        visit(item)
    return list(ordered.values())


def _graph_author(raw: dict[str, Any], members: dict[str, str]) -> str:
    sender = raw.get("from")
    if isinstance(sender, dict):
        user = sender.get("user") if isinstance(sender.get("user"), dict) else None
        if user:
            name = user.get("displayName")
            if name:
                return str(name)
            ident = _clean_id(user.get("id") or "")
            if ident:
                return members.get(ident, ident)
        name = sender.get("displayName") or sender.get("name") or sender.get("application")
        if name:
            return str(name)
    if isinstance(sender, str) and sender:
        return members.get(sender, sender)
    return "unknown"


def _graph_message(raw: dict[str, Any], members: dict[str, str], index: int) -> Message:
    ident = _clean_id(raw.get("id") or "") or f"teams-graph-{index}"
    body = raw.get("body") if isinstance(raw.get("body"), dict) else {}
    content = str(body.get("content") or "")
    content_type = str(body.get("contentType") or "").lower()
    text = _plain_text(content) if content_type == "text" else _html_to_text(content)
    attachments: list[Attachment] = []
    for item in raw.get("attachments") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        url = str(item.get("contentUrl") or "").strip()
        if not (name or url):
            continue
        attachments.append(
            Attachment(
                kind=str(item.get("contentType") or "").strip().lower() or "file",
                name=name or url,
                url=url,
                text=str(item.get("content") or "").strip(),
            )
        )
    meta: dict[str, Any] = {"teams_shape": "graph"}
    if raw.get("messageType"):
        meta["teams_message_type"] = str(raw.get("messageType"))
    return Message(
        id=ident,
        ts=parse_timestamp(str(raw.get("createdDateTime") or "")),
        author=_graph_author(raw, members),
        text=text,
        reply_to=_clean_id(raw.get("replyToId") or "") or None,
        attachments=attachments,
        meta=meta,
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
    normalized: list[Message] = []
    for message in messages:
        message.reply_to = _clean_id(message.reply_to or "") or None
        normalized.append(message)
    by_id = {message.id: message for message in normalized}
    parent_of = {message.id: (message.reply_to or "") for message in normalized}

    grouped: dict[str, list[Message]] = {}
    root_ids: set[str] = set()
    standalone: list[Message] = []
    for message in normalized:
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


def _channel_name(path: Path) -> str:
    name = path.stem
    for prefix in ("Chat history - ", "Microsoft Teams - ", "Teams - "):
        if name.startswith(prefix):
            name = name[len(prefix) :]
    return name or path.stem


def _document_title(root: _Node) -> str:
    for node in _walk(root):
        if node.tag == "title":
            text = node.text().strip()
            for prefix in ("Chat history - ", "Microsoft Teams - ", "Teams - "):
                if text.startswith(prefix):
                    text = text[len(prefix) :]
            return text
    return ""


def load_teams_html(
    path: Path,
    gap_minutes: int,
    channel_override: str = "",
    title: str = "",
) -> list[Thread]:
    path = Path(path)
    builder = _TreeBuilder()
    builder.feed(path.read_text(encoding="utf-8-sig", errors="replace"))
    builder.close()
    root = builder.root
    containers = _find_containers(root)
    channel = channel_override or _document_title(root) or _channel_name(path)
    channel_id = path.stem
    raw_path = str(path)
    extra = {"teams_shape": "html", "teams_container_count": len(containers)}

    if not containers:
        text = root.renders()
        warnings.warn(
            "Teams HTML input did not show a message container; "
            "keeping the cleaned page text as one best-effort document.",
            RuntimeWarning,
        )
        return [
            make_thread(
                source=SOURCE,
                channel=channel,
                thread_key="document",
                messages=[Message(id="teams-html-0", ts="", author="document", text=text)],
                title=title or first_line(text) or channel,
                raw_path=raw_path,
                extra=dict(extra, teams_heuristic="single-document"),
            )
        ]

    messages = [_html_message(container, index) for index, container in enumerate(containers)]
    return _threads_from_messages(
        messages,
        channel=channel,
        channel_id=channel_id,
        raw_path=raw_path,
        gap_minutes=gap_minutes,
        title=title,
        extra=extra,
    )


def load_teams_json(
    path: Path,
    gap_minutes: int,
    channel_override: str = "",
    title: str = "",
) -> list[Thread]:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    items = _graph_items(data)
    if not items:
        from .generic import load_json

        warnings.warn(
            "Teams JSON input held no Graph message object; "
            "falling back to the generic JSON adapter.",
            RuntimeWarning,
        )
        return load_json(path, gap_minutes, channel_override, title)
    members = _members(data)
    flat = _flatten_graph(items)
    messages = [_graph_message(item, members, index) for index, item in enumerate(flat)]
    channel = channel_override or _channel_name(path)
    return _threads_from_messages(
        messages,
        channel=channel,
        channel_id=path.stem,
        raw_path=str(path),
        gap_minutes=gap_minutes,
        title=title,
        extra={"teams_shape": "graph", "teams_message_count": len(messages)},
    )


def load_teams(
    path: Path,
    gap_minutes: int,
    channel_override: str = "",
    title: str = "",
) -> list[Thread]:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in {".html", ".htm"}:
        return load_teams_html(path, gap_minutes, channel_override, title)
    if suffix == ".json":
        return load_teams_json(path, gap_minutes, channel_override, title)
    prefix = path.read_text(encoding="utf-8-sig", errors="replace")[:20_000]
    stripped = prefix.lstrip()
    if stripped.startswith("<"):
        return load_teams_html(path, gap_minutes, channel_override, title)
    return load_teams_json(path, gap_minutes, channel_override, title)