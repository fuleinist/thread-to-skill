"""Workspace configuration and discovery."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .util import atomic_write_json, ensure_dir, now_iso

HOME_DIRNAME = ".thread2skill"
CONFIG_FILENAME = "config.json"


class ConfigError(RuntimeError):
    pass


@dataclass
class Config:
    home: Path
    data: dict[str, Any] = field(default_factory=dict)
    created: bool = False

    @property
    def skills_dir(self) -> Path:
        return self.home / "skills"

    @property
    def threads_dir(self) -> Path:
        return self.home / "threads"

    @property
    def db_path(self) -> Path:
        return self.home / "index.db"

    @property
    def prompts_dir(self) -> Path:
        return self.home / "prompts"

    @property
    def default_provider(self) -> str:
        return str(self.data.get("provider") or "auto")

    @property
    def redact_enabled(self) -> bool:
        return bool(self.data.get("redact", True))

    @property
    def custom_redactions(self) -> list[str]:
        values = self.data.get("redact_patterns") or []
        return [str(value) for value in values if str(value).strip()]

    @property
    def gap_minutes(self) -> int:
        return int(self.data.get("gap_minutes") or 60)

    def save(self) -> None:
        ensure_dir(self.home)
        atomic_write_json(self.home / CONFIG_FILENAME, self.data)

    @classmethod
    def create(cls, home: Path, *, provider: str = "auto", redact: bool = True) -> "Config":
        home = Path(home).expanduser().resolve()
        ensure_dir(home)
        ensure_dir(home / "skills")
        ensure_dir(home / "threads")
        ensure_dir(home / "prompts")
        config = cls(
            home=home,
            data={
                "version": 1,
                "created_at": now_iso(),
                "provider": provider,
                "redact": redact,
                "gap_minutes": 60,
                "redact_patterns": [],
            },
            created=True,
        )
        config.save()
        return config

    @classmethod
    def load(cls, home: Path) -> "Config":
        home = Path(home).expanduser().resolve()
        config_path = home / CONFIG_FILENAME
        if not config_path.exists():
            raise ConfigError(f"No thread-to-skill workspace at {home}. Run `t2s init` first.")
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Invalid config at {config_path}: {exc}") from exc
        return cls(home=home, data=data)


def find_home(start: Path | None = None, explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env_home = os.environ.get("T2S_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        marker = candidate / HOME_DIRNAME
        if marker.exists():
            return marker
    return current / HOME_DIRNAME


def default_home() -> Path:
    return Path.cwd().resolve() / HOME_DIRNAME