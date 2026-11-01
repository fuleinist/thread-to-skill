"""Local-first redaction of secrets and personal data before distillation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Message, Thread

# Ordered from most specific to least specific. Each pattern is paired with a
# replacement label; counts are reported so users can see what was removed.
BUILTIN_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "private_key",
        "[redacted-private-key]",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----", re.S),
    ),
    (
        "github_token",
        "[redacted-github-token]",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    (
        "slack_token",
        "[redacted-slack-token]",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ),
    (
        "aws_access_key",
        "[redacted-aws-key]",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ),
    (
        "google_api_key",
        "[redacted-google-key]",
        re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ),
    (
        "openai_key",
        "[redacted-api-key]",
        re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "anthropic_key",
        "[redacted-api-key]",
        re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "jwt",
        "[redacted-jwt]",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    (
        "bearer_token",
        "Bearer [redacted-token]",
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*\b", re.I),
    ),
    (
        "basic_auth_url",
        "[redacted-url-with-credentials]",
        re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@[^\s]+"),
    ),
    (
        "email",
        "[redacted-email]",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ),
    (
        "credit_card",
        "[redacted-card]",
        re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
    ),
    (
        "phone",
        "[redacted-phone]",
        re.compile(r"(?<!\w)(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?){2,4}\d{2,4}(?!\w)"),
    ),
    (
        "private_ip",
        "[redacted-ip]",
        re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"),
    ),
]

# Phrases that look like phone numbers but are usually dates, versions, or IDs.
_PHONE_FALSE_POSITIVES = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|\d+\.\d+\.\d+|\d{1,3}(?:\.\d{1,3}){3}|v?\d+(?:\.\d+)+)$"
)


@dataclass
class RedactionReport:
    counts: dict[str, int] = field(default_factory=dict)
    total: int = 0

    def add(self, label: str, count: int = 1) -> None:
        if count <= 0:
            return
        self.counts[label] = self.counts.get(label, 0) + count
        self.total += count

    def merge(self, other: "RedactionReport") -> None:
        for label, count in other.counts.items():
            self.counts[label] = self.counts.get(label, 0) + count
        self.total += other.total

    def summary(self) -> str:
        if not self.total:
            return "no sensitive data detected"
        parts = [f"{count} {label}" for label, count in sorted(self.counts.items())]
        return ", ".join(parts)


def _luhn_like(value: str) -> bool:
    digits = [int(char) for char in value if char.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def redact_text(text: str, custom_patterns: list[str] | None = None) -> tuple[str, RedactionReport]:
    report = RedactionReport()
    result = text or ""

    for label, replacement, pattern in BUILTIN_PATTERNS:
        if label == "credit_card":
            def card_repl(match: re.Match[str]) -> str:
                if _luhn_like(match.group(0)):
                    report.add(label)
                    return replacement
                return match.group(0)
            result = pattern.sub(card_repl, result)
            continue
        if label == "phone":
            def phone_repl(match: re.Match[str]) -> str:
                value = match.group(0).strip()
                digits = re.sub(r"\D", "", value)
                if len(digits) < 8 or _PHONE_FALSE_POSITIVES.match(value):
                    return match.group(0)
                report.add(label)
                return replacement
            result = pattern.sub(phone_repl, result)
            continue
        def replacement_func(match: re.Match[str], label: str = label, replacement: str = replacement) -> str:
            report.add(label)
            return replacement
        result = pattern.sub(replacement_func, result)

    for index, raw_pattern in enumerate(custom_patterns or [], start=1):
        try:
            pattern = re.compile(raw_pattern)
        except re.error:
            continue
        label = f"custom-{index}"
        def custom_repl(match: re.Match[str], label: str = label) -> str:
            report.add(label)
            return f"[redacted-{label}]"
        result = pattern.sub(custom_repl, result)

    return result, report


def redact_thread(thread: Thread, custom_patterns: list[str] | None = None) -> tuple[Thread, RedactionReport]:
    report = RedactionReport()
    messages: list[Message] = []
    for message in thread.messages:
        cleaned_text, text_report = redact_text(message.text, custom_patterns)
        report.merge(text_report)
        attachments: list[str] = []
        for attachment in message.attachments:
            cleaned_attachment, attachment_report = redact_text(attachment, custom_patterns)
            report.merge(attachment_report)
            attachments.append(cleaned_attachment)
        messages.append(
            Message(
                id=message.id,
                ts=message.ts,
                author=message.author,
                text=cleaned_text,
                reply_to=message.reply_to,
                reactions=message.reactions,
                attachments=attachments,
                meta=message.meta,
            )
        )
    return (
        Thread(
            id=thread.id,
            source=thread.source,
            channel=thread.channel,
            thread_key=thread.thread_key,
            title=thread.title,
            messages=messages,
            started_at=thread.started_at,
            ended_at=thread.ended_at,
            participants=thread.participants,
            raw_path=thread.raw_path,
            extra=thread.extra,
        ),
        report,
    )