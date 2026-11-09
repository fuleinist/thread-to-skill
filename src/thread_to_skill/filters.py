"""Message-level filters applied to threads before distillation.

Filters run on normalized thread data, before prompt construction and token
budgeting. The filtered slice is recorded in skill provenance so the skill
states which part of the conversation it came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from .models import Message, Thread


class FilterError(ValueError):
    """Raised when a filter value is invalid or removes every message."""


# Automation accounts. Matching is token-aware, so a surname such as "Abbot"
# is not treated as a bot.
_KNOWN_BOTS = {
    "bot",
    "dependabot",
    "renovate",
    "renovatebot",
    "github-actions",
    "github-actions[bot]",
    "codecov",
    "codecov[bot]",
    "coderabbitai",
    "coderabbitai[bot]",
    "pullfrog-ai",
}


def is_bot_author(author: str) -> bool:
    """Return True when an author name looks like an automation account."""
    name = (author or "").strip().lower()
    if not name:
        return False
    if name in _KNOWN_BOTS:
        return True
    if name.endswith(("[bot]", "-bot", "_bot")):
        return True
    tokens = [token for token in re.split(r"[^a-z0-9]+", name) if token]
    return bool(tokens) and tokens[-1] == "bot"


def parse_date(value: str, *, label: str) -> date:
    """Parse a YYYY-MM-DD filter value, with an actionable error."""
    text = (value or "").strip()
    if not text:
        raise FilterError(f"--{label} needs a date in YYYY-MM-DD form")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise FilterError(f"--{label} is not a valid date: {text!r}. Use YYYY-MM-DD.") from exc


def message_date(message: Message) -> date | None:
    """Return the calendar date of a message, or None when the stamp is unknown."""
    stamp = (message.ts or "").strip()
    if len(stamp) < 10:
        return None
    try:
        return date.fromisoformat(stamp[:10])
    except ValueError:
        return None


@dataclass
class MessageFilters:
    """The message slice requested for one distillation run."""

    since: str = ""
    until: str = ""
    participants: tuple[str, ...] = ()
    max_messages: int = 0
    exclude_bots: bool = False

    def __post_init__(self) -> None:
        self.participants = tuple(
            item.strip() for item in self.participants if item and item.strip()
        )

    @property
    def active(self) -> bool:
        return bool(
            self.since
            or self.until
            or self.participants
            or self.max_messages
            or self.exclude_bots
        )

    def validate(self) -> None:
        start = parse_date(self.since, label="since") if self.since else None
        end = parse_date(self.until, label="until") if self.until else None
        if start and end and start > end:
            raise FilterError(f"--since {self.since} is after --until {self.until}")
        if self.max_messages < 0:
            raise FilterError("--max-messages must be zero or a positive number")


def _keep_message(
    message: Message,
    *,
    start: date | None,
    end: date | None,
    participants: tuple[str, ...],
    exclude_bots: bool,
) -> bool:
    if exclude_bots and is_bot_author(message.author):
        return False
    if participants:
        author = (message.author or "").lower()
        if not any(name in author for name in participants):
            return False
    if start or end:
        day = message_date(message)
        # An unknown timestamp cannot be placed in the window, so keep it.
        if day is not None:
            if start and day < start:
                return False
            if end and day > end:
                return False
    return True


def apply_filters(
    threads: list[Thread],
    spec: MessageFilters,
) -> tuple[list[Thread], dict[str, Any]]:
    """Apply ``spec`` to every thread and return rebuilt threads plus a record.

    Rebuilt threads keep their source, channel, and thread key, so their IDs stay
    stable. ``--max-messages`` keeps the most recent N messages across all
    selected threads and preserves chronological order.
    """
    spec.validate()
    before = sum(thread.message_count for thread in threads)
    if not spec.active:
        return list(threads), {"applied": False}

    start = parse_date(spec.since, label="since") if spec.since else None
    end = parse_date(spec.until, label="until") if spec.until else None
    participants = tuple(name.lower() for name in spec.participants)

    kept_by_thread: list[list[Message]] = []
    for thread in threads:
        kept_by_thread.append(
            [
                message
                for message in thread.messages
                if _keep_message(
                    message,
                    start=start,
                    end=end,
                    participants=participants,
                    exclude_bots=spec.exclude_bots,
                )
            ]
        )

    if spec.max_messages:
        ranked: list[tuple[str, int, int]] = []
        for thread_index, kept in enumerate(kept_by_thread):
            for message_index, message in enumerate(kept):
                ranked.append((message.ts or "", thread_index, message_index))
        ranked.sort()
        allowed = {(thread_index, message_index) for _, thread_index, message_index in ranked[-spec.max_messages:]}
        kept_by_thread = [
            [
                message
                for message_index, message in enumerate(kept)
                if (thread_index, message_index) in allowed
            ]
            for thread_index, kept in enumerate(kept_by_thread)
        ]

    rebuilt: list[Thread] = []
    for thread, kept in zip(threads, kept_by_thread):
        if not kept:
            continue
        rebuilt.append(
            Thread.create(
                source=thread.source,
                channel=thread.channel,
                thread_key=thread.thread_key,
                messages=kept,
                title=thread.title,
                raw_path=thread.raw_path,
                extra=thread.extra,
            )
        )

    after = sum(thread.message_count for thread in rebuilt)
    record: dict[str, Any] = {
        "applied": True,
        "since": spec.since,
        "until": spec.until,
        "participants": list(spec.participants),
        "max_messages": spec.max_messages,
        "exclude_bots": spec.exclude_bots,
        "messages_before": before,
        "messages_after": after,
        "threads_before": len(threads),
        "threads_after": len(rebuilt),
    }
    if after == 0:
        raise FilterError(
            "No messages left after filtering "
            f"({before} messages before the filters). Widen the window or drop a filter."
        )
    return rebuilt, record