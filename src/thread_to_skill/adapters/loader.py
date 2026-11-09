"""Format detection and adapter dispatch."""

from __future__ import annotations

import json
import re
import tempfile
import zipfile
from pathlib import Path

from ..models import Thread
from .discord import load_discord
from .generic import load_json, load_jsonl, load_markdown, load_text
from .html_transcript import load_html
from .slack import load_slack
from .whatsapp import ANDROID_RE, IOS_RE, load_whatsapp

FORMATS = {"auto", "slack", "discord", "whatsapp", "telegram", "jsonl", "json", "markdown", "text", "html"}


def _read_prefix(path: Path, limit: int = 200_000) -> str:
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def detect_format(path: Path) -> str:
    if path.is_dir():
        if (path / "channels.json").exists() or (path / "users.json").exists():
            return "slack"
        if any(child.is_dir() and any(child.glob("*.json")) for child in path.iterdir()):
            return "slack"
        if any(path.glob("*.json")):
            return "json"
        return "unknown"
    suffix = path.suffix.lower()
    if suffix == ".zip":
        return "zip"
    if suffix in {".html", ".htm"}:
        return "html"
    if suffix == ".jsonl" or suffix == ".ndjson":
        return "jsonl"
    if suffix in {".md", ".markdown", ".mdx"}:
        return "markdown"
    if suffix == ".txt":
        prefix = _read_prefix(path, 30_000)
        for line in prefix.splitlines()[:40]:
            if IOS_RE.match(line) or ANDROID_RE.match(line):
                return "whatsapp"
        return "text"
    if suffix == ".json":
        try:
            data = json.loads(_read_prefix(path))
        except (json.JSONDecodeError, ValueError):
            return "json"
        if isinstance(data, dict) and isinstance(data.get("messages"), list):
            if data.get("guild") is not None:
                return "discord"
            channel = data.get("channel")
            if isinstance(channel, dict) and channel.get("id"):
                return "discord"
            if any(key in data for key in ("name", "type", "chat")) and any(
                isinstance(item, dict) and ("from" in item or "from_id" in item)
                for item in data["messages"][:5]
            ):
                return "telegram"
        return "json"
    return "unknown"


def _zip_extract(path: Path, destination: Path) -> Path:
    with zipfile.ZipFile(path) as archive:
        archive.extractall(destination)
    children = [child for child in destination.iterdir() if child.name not in {"__MACOSX"}]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return destination


def load_path(
    path: Path,
    *,
    fmt: str = "auto",
    gap_minutes: int = 60,
    channel: str = "",
    title: str = "",
) -> list[Thread]:
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Input not found: {path}")
    if fmt == "auto":
        fmt = detect_format(path)
    if fmt == "zip":
        with tempfile.TemporaryDirectory(prefix="t2s-zip-") as temp:
            extracted = _zip_extract(path, Path(temp))
            return load_path(extracted, fmt="auto", gap_minutes=gap_minutes, channel=channel, title=title)
    if fmt not in FORMATS:
        raise ValueError(f"Unknown format: {fmt}")
    if fmt == "slack":
        threads = load_slack(path, gap_minutes, channel, title)
    elif fmt == "discord":
        threads = load_discord(path, gap_minutes, channel, title)
    elif fmt == "whatsapp":
        threads = load_whatsapp(path, gap_minutes, channel, title)
    elif fmt == "telegram":
        from .generic import load_telegram

        threads = load_telegram(path, gap_minutes, channel, title)
    elif fmt == "jsonl":
        threads = load_jsonl(path, gap_minutes, channel, title)
    elif fmt == "json":
        threads = load_json(path, gap_minutes, channel, title)
    elif fmt == "markdown":
        threads = load_markdown(path, gap_minutes, channel, title)
    elif fmt == "text":
        threads = load_text(path, gap_minutes, channel, title)
    elif fmt == "html":
        threads = load_html(path, gap_minutes, channel, title)
    else:
        raise ValueError(f"Unsupported format: {fmt}")
    return [thread for thread in threads if thread.messages]