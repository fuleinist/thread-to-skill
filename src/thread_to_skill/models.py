"""Normalized conversation and skill data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .util import content_hash, parse_timestamp, stable_id


@dataclass
class Message:
    id: str
    ts: str
    author: str
    text: str
    reply_to: str | None = None
    reactions: list[dict[str, Any]] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        return cls(
            id=str(data.get("id", "")),
            ts=parse_timestamp(data.get("ts", "")),
            author=str(data.get("author", "unknown")),
            text=str(data.get("text", "")),
            reply_to=(str(data["reply_to"]) if data.get("reply_to") not in (None, "") else None),
            reactions=list(data.get("reactions") or []),
            attachments=[str(item) for item in (data.get("attachments") or [])],
            meta=dict(data.get("meta") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "author": self.author,
            "text": self.text,
            "reply_to": self.reply_to,
            "reactions": self.reactions,
            "attachments": self.attachments,
            "meta": self.meta,
        }

    def render(self) -> str:
        attachment_note = ""
        if self.attachments:
            attachment_note = " [attachments: " + ", ".join(self.attachments) + "]"
        return f"[{self.id}] {self.author} ({self.ts}): {self.text}{attachment_note}"


@dataclass
class Thread:
    id: str
    source: str
    channel: str
    thread_key: str
    title: str
    messages: list[Message]
    started_at: str = ""
    ended_at: str = ""
    participants: list[str] = field(default_factory=list)
    raw_path: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        source: str,
        channel: str,
        thread_key: str,
        messages: list[Message],
        title: str = "",
        raw_path: str = "",
        extra: dict[str, Any] | None = None,
    ) -> "Thread":
        ordered = sorted(
            [message for message in messages if message.text.strip() or message.attachments],
            key=lambda message: (message.ts or "", message.id),
        )
        participants = sorted({message.author for message in ordered if message.author})
        started = ordered[0].ts if ordered else ""
        ended = ordered[-1].ts if ordered else ""
        if not title:
            first = ordered[0].text.strip().splitlines()[0] if ordered else ""
            title = first[:80] or f"{channel or source} conversation"
        return cls(
            id=stable_id(source, channel, thread_key),
            source=source,
            channel=channel,
            thread_key=thread_key,
            title=title,
            messages=ordered,
            started_at=started,
            ended_at=ended,
            participants=participants,
            raw_path=raw_path,
            extra=dict(extra or {}),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Thread":
        return cls(
            id=data["id"],
            source=data.get("source", "unknown"),
            channel=data.get("channel", ""),
            thread_key=data.get("thread_key", ""),
            title=data.get("title", ""),
            messages=[Message.from_dict(item) for item in data.get("messages", [])],
            started_at=data.get("started_at", ""),
            ended_at=data.get("ended_at", ""),
            participants=list(data.get("participants") or []),
            raw_path=data.get("raw_path", ""),
            extra=dict(data.get("extra") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "channel": self.channel,
            "thread_key": self.thread_key,
            "title": self.title,
            "messages": [message.to_dict() for message in self.messages],
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "participants": self.participants,
            "raw_path": self.raw_path,
            "extra": self.extra,
        }

    @property
    def message_count(self) -> int:
        return len(self.messages)

    def content_text(self) -> str:
        return "\n".join(message.render() for message in self.messages)

    def content_hash(self) -> str:
        return content_hash(self.content_text())

    def speaker_list(self, limit: int = 6) -> str:
        if not self.participants:
            return "unknown"
        if len(self.participants) <= limit:
            return ", ".join(self.participants)
        return ", ".join(self.participants[:limit]) + f" +{len(self.participants) - limit}"

    def summary_line(self) -> str:
        return (
            f"{self.source} · {self.channel or 'direct'} · "
            f"{self.message_count} messages · {len(self.participants)} speakers"
        )


@dataclass
class Skill:
    slug: str
    name: str
    title: str
    description: str
    summary: str
    category: str
    tags: list[str]
    triggers: list[str]
    version: int
    confidence: float
    status: str
    path: str
    source_threads: list[str]
    created_at: str
    updated_at: str
    last_used_at: str = ""
    suggested: int = 0
    accepted: int = 0
    worked: int = 0
    failed: int = 0
    body: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Any) -> "Skill":
        import json

        def loads(value: Any, fallback: Any) -> Any:
            if not value:
                return fallback
            try:
                return json.loads(value)
            except (TypeError, ValueError):
                return fallback

        return cls(
            slug=row["slug"],
            name=row["name"],
            title=row["title"],
            description=row["description"],
            summary=row["summary"],
            category=row["category"],
            tags=loads(row["tags"], []),
            triggers=loads(row["triggers"], []),
            version=int(row["version"] or 1),
            confidence=float(row["confidence"] or 0.0),
            status=row["status"] or "active",
            path=row["path"],
            source_threads=loads(row["source_threads"], []),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_used_at=row["last_used_at"] or "",
            suggested=int(row["suggested"] or 0),
            accepted=int(row["accepted"] or 0),
            worked=int(row["worked"] or 0),
            failed=int(row["failed"] or 0),
            body=row["body"] or "",
            metadata=loads(row["metadata"], {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "summary": self.summary,
            "category": self.category,
            "tags": self.tags,
            "triggers": self.triggers,
            "version": self.version,
            "confidence": round(self.confidence, 3),
            "status": self.status,
            "path": self.path,
            "source_threads": self.source_threads,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_used_at": self.last_used_at,
            "usage": {
                "suggested": self.suggested,
                "accepted": self.accepted,
                "worked": self.worked,
                "failed": self.failed,
            },
        }