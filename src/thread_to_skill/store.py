"""SQLite-backed local registry for threads, skills, and usage events."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable

from .models import Skill, Thread
from .util import ensure_dir, now_iso

SCHEMA_VERSION = 1

Migration = Callable[[sqlite3.Connection], None]


class StoreError(RuntimeError):
    """Raised when the registry cannot be opened or upgraded safely."""


BASELINE_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    channel TEXT NOT NULL,
    title TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    message_count INTEGER NOT NULL,
    participants TEXT NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    extra TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skills (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    summary TEXT NOT NULL,
    category TEXT NOT NULL,
    tags TEXT NOT NULL,
    triggers TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    confidence REAL NOT NULL DEFAULT 0.5,
    status TEXT NOT NULL DEFAULT 'active',
    path TEXT NOT NULL,
    source_threads TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL DEFAULT '',
    suggested INTEGER NOT NULL DEFAULT 0,
    accepted INTEGER NOT NULL DEFAULT 0,
    worked INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    body TEXT NOT NULL,
    metadata TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    skill_slug TEXT NOT NULL DEFAULT '',
    thread_id TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_skills_category ON skills(category);
CREATE INDEX IF NOT EXISTS idx_skills_status ON skills(status);
CREATE INDEX IF NOT EXISTS idx_events_skill ON events(skill_slug);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(
    slug UNINDEXED,
    name,
    description,
    tags,
    body
);
"""

TIMELINE_WEEKS = 12
TOP_CHANNELS = 10

# Ordered registry of schema upgrades. Each entry is (target_version, apply fn).
# Version 1 is BASELINE_SCHEMA, so an empty registry is correct for v0.1.
# Append a migration and bump SCHEMA_VERSION; never rewrite an old entry.
MIGRATIONS: tuple[tuple[int, Migration], ...] = ()

# Version snapshots live inside the skill bundle, so nothing extra is needed to
# keep them out of the way. They are plain copies, not registry rows.
SNAPSHOT_DIRNAME = ".versions"
SNAPSHOT_FILES = ("SKILL.md", "metadata.json")
DEFAULT_SNAPSHOT_KEEP = 10


def versions_dir(skill_dir: Path) -> Path:
    """The snapshot directory inside a skill bundle."""
    return Path(skill_dir) / SNAPSHOT_DIRNAME


def snapshot_dir(skill_dir: Path, version: int) -> Path:
    """The stored copy of one version."""
    return versions_dir(skill_dir) / f"v{int(version)}"


def list_snapshots(skill_dir: Path) -> list[int]:
    """Stored snapshot versions, oldest first. Names that are not `vN` are ignored."""
    directory = versions_dir(skill_dir)
    if not directory.is_dir():
        return []
    versions = []
    for entry in directory.iterdir():
        name = entry.name
        if entry.is_dir() and name.startswith("v") and name[1:].isdigit():
            versions.append(int(name[1:]))
    return sorted(versions)


def inspect_schema(path: Path, *, target: int | None = None) -> dict[str, Any]:
    """Read the on-disk schema version without changing the database."""
    target_version = SCHEMA_VERSION if target is None else int(target)
    result: dict[str, Any] = {
        "exists": False,
        "version": 0,
        "target": target_version,
        "pending": [],
        "needs_upgrade": False,
        "too_new": False,
        "error": "",
    }
    db_path = Path(path)
    if not db_path.exists():
        result["needs_upgrade"] = False
        return result
    result["exists"] = True
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute("PRAGMA user_version").fetchone()
            version = int(row[0]) if row else 0
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        result["error"] = str(exc)
        result["needs_upgrade"] = True
        return result
    result["version"] = version
    result["too_new"] = version > target_version
    result["pending"] = [v for v, _ in MIGRATIONS if version < v <= target_version]
    result["needs_upgrade"] = version < target_version
    return result


def _week_monday(day: dt.date) -> dt.date:
    """Monday of the ISO week that contains a local date."""
    return day - dt.timedelta(days=day.isoweekday() - 1)


def _local_date(value: Any) -> dt.date | None:
    """Parse a stored ISO-8601 timestamp (possibly ending in Z) as a local date."""
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone().date()


class Store:
    def __init__(
        self,
        path: Path,
        *,
        auto_migrate: bool = True,
        migrations: Iterable[tuple[int, Migration]] | None = None,
    ):
        self.path = Path(path)
        ensure_dir(self.path.parent)
        self.migrations: tuple[tuple[int, Migration], ...] = (
            MIGRATIONS if migrations is None else tuple(migrations)
        )
        self.schema_from = SCHEMA_VERSION
        self.schema_to = SCHEMA_VERSION
        self.pending_versions: list[int] = []
        self.migrated_versions: list[int] = []
        self.backup_path: Path | None = None
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        try:
            self._prepare_schema(auto_migrate)
            self.fts_enabled = False
            try:
                self.conn.executescript(FTS_SCHEMA)
                self.fts_enabled = True
            except sqlite3.DatabaseError:
                self.fts_enabled = False
            self.conn.commit()
        except Exception:
            self.conn.close()
            raise

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ schema
    def schema_info(self) -> dict[str, Any]:
        """Return the schema state this connection opened with."""
        return {
            "version": self.schema_from,
            "target": self.schema_to,
            "pending": list(self.pending_versions),
            "migrated": list(self.migrated_versions),
            "backup": str(self.backup_path) if self.backup_path else None,
        }

    def _read_version(self) -> int:
        row = self.conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row else 0

    def _has_baseline(self) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'skills'"
        ).fetchone()
        return row is not None

    def _stamp(self, version: int) -> None:
        self.conn.execute(f"PRAGMA user_version = {int(version)}")
        self.conn.commit()

    def _backup(self) -> Path:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = self.path.with_name(f"{self.path.name}.bak-{stamp}")
        suffix = 1
        while target.exists():
            target = self.path.with_name(f"{self.path.name}.bak-{stamp}-{suffix}")
            suffix += 1
        destination = sqlite3.connect(str(target))
        try:
            self.conn.backup(destination)
        finally:
            destination.close()
        return target

    def _prepare_schema(self, auto_migrate: bool) -> None:
        current = self._read_version()
        target = SCHEMA_VERSION
        self.schema_from = current
        self.schema_to = target
        fresh = not self._has_baseline()
        if fresh:
            # A new or empty file: create the baseline, then migrate it forward
            # like any other database. BASELINE_SCHEMA is always version 1.
            self.conn.executescript(BASELINE_SCHEMA)
            self._stamp(1)
            current = 1
            self.schema_from = 1
        if current > target:
            raise StoreError(
                f"{self.path.name} is newer than this build: schema version {current}, "
                f"but version {target} is supported. Upgrade thread-to-skill before you "
                "open this workspace: pip install -U thread-to-skill"
            )
        self.pending_versions = [v for v, _ in self.migrations if current < v <= target]
        if current == target:
            return
        if not auto_migrate:
            return
        self._apply_migrations(current, target, backup=not fresh)

    def _apply_migrations(self, current: int, target: int, *, backup: bool = True) -> None:
        pending = [(v, fn) for v, fn in self.migrations if current < v <= target]
        if not pending:
            # Legacy database from before version stamping: adopt the baseline.
            self._stamp(target)
            self.schema_from = target
            self.pending_versions = []
            return
        if backup:
            self.backup_path = self._backup()
        previous_isolation = self.conn.isolation_level
        self.conn.isolation_level = None
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            for version, apply in pending:
                apply(self.conn)
                self.conn.execute(f"PRAGMA user_version = {int(version)}")
                self.migrated_versions.append(version)
            self.conn.execute("COMMIT")
        except Exception as exc:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise StoreError(
                f"Schema migration from version {current} to {target} failed: {exc}. "
                "No changes were applied. "
                f"Backup kept at {self.backup_path}"
            ) from exc
        finally:
            self.conn.isolation_level = previous_isolation
        self.schema_from = target
        self.pending_versions = []

    # ------------------------------------------------------------------ threads
    def add_thread(self, thread: Thread) -> None:
        """Insert or upsert one thread. Callers decide whether to write at all."""
        self.conn.execute(
            """
            INSERT INTO threads (id, source, channel, title, started_at, ended_at, message_count,
                                 participants, path, content_hash, ingested_at, extra)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                started_at=excluded.started_at,
                ended_at=excluded.ended_at,
                message_count=excluded.message_count,
                participants=excluded.participants,
                path=excluded.path,
                content_hash=excluded.content_hash,
                ingested_at=excluded.ingested_at,
                extra=excluded.extra
            """,
            (
                thread.id,
                thread.source,
                thread.channel,
                thread.title,
                thread.started_at,
                thread.ended_at,
                thread.message_count,
                json.dumps(thread.participants, ensure_ascii=False),
                thread.raw_path,
                thread.content_hash(),
                now_iso(),
                json.dumps(thread.extra, ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def get_thread_row(self, thread_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()

    def thread_content_hash(self, thread_id: str) -> str | None:
        """Return the stored content hash, or None when the thread is not indexed yet."""
        row = self.conn.execute(
            "SELECT content_hash FROM threads WHERE id = ?", (thread_id,)
        ).fetchone()
        return None if row is None else str(row["content_hash"])

    def get_thread(self, thread_id: str, threads_dir: Path) -> Thread | None:
        row = self.get_thread_row(thread_id)
        if not row:
            return None
        path = Path(row["path"])
        # `path` in the DB is the raw export path; normalized threads live in threads_dir.
        normalized = threads_dir / f"{thread_id}.json"
        if normalized.exists():
            return Thread.from_dict(json.loads(normalized.read_text(encoding="utf-8")))
        return None

    def list_threads(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM threads ORDER BY ingested_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def count_threads(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS count FROM threads").fetchone()
        return int(row["count"]) if row else 0

    def find_threads_by_prefix(self, prefix: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM threads WHERE id LIKE ? ORDER BY id", (prefix + "%",)
        ).fetchall()

    # ------------------------------------------------------------------- skills
    def save_skill(self, skill: Skill) -> None:
        self.conn.execute(
            """
            INSERT INTO skills (slug, name, title, description, summary, category, tags, triggers,
                                version, confidence, status, path, source_threads, created_at, updated_at,
                                last_used_at, suggested, accepted, worked, failed, body, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                name=excluded.name,
                title=excluded.title,
                description=excluded.description,
                summary=excluded.summary,
                category=excluded.category,
                tags=excluded.tags,
                triggers=excluded.triggers,
                version=excluded.version,
                confidence=excluded.confidence,
                status=excluded.status,
                path=excluded.path,
                source_threads=excluded.source_threads,
                updated_at=excluded.updated_at,
                last_used_at=excluded.last_used_at,
                suggested=excluded.suggested,
                accepted=excluded.accepted,
                worked=excluded.worked,
                failed=excluded.failed,
                body=excluded.body,
                metadata=excluded.metadata
            """,
            (
                skill.slug,
                skill.name,
                skill.title,
                skill.description,
                skill.summary,
                skill.category,
                json.dumps(skill.tags, ensure_ascii=False),
                json.dumps(skill.triggers, ensure_ascii=False),
                skill.version,
                skill.confidence,
                skill.status,
                skill.path,
                json.dumps(skill.source_threads, ensure_ascii=False),
                skill.created_at,
                skill.updated_at,
                skill.last_used_at,
                skill.suggested,
                skill.accepted,
                skill.worked,
                skill.failed,
                skill.body,
                json.dumps(skill.metadata, ensure_ascii=False),
            ),
        )
        if self.fts_enabled:
            self.conn.execute("DELETE FROM skills_fts WHERE slug = ?", (skill.slug,))
            self.conn.execute(
                "INSERT INTO skills_fts (slug, name, description, tags, body) VALUES (?, ?, ?, ?, ?)",
                (skill.slug, skill.name, skill.description, " ".join(skill.tags), skill.body),
            )
        self.conn.commit()

    def get_skill(self, slug: str) -> Skill | None:
        row = self.conn.execute("SELECT * FROM skills WHERE slug = ?", (slug,)).fetchone()
        return Skill.from_row(row) if row else None

    def list_skills(
        self,
        *,
        category: str = "",
        tag: str = "",
        status: str = "",
        limit: int = 500,
        include_archived: bool = False,
    ) -> list[Skill]:
        clauses = []
        params: list[Any] = []
        if category:
            clauses.append("category = ?")
            params.append(category)
        if tag:
            clauses.append("tags LIKE ?")
            params.append(f'%"{tag}"%')
        if status:
            clauses.append("status = ?")
            params.append(status)
        elif not include_archived:
            clauses.append("status <> 'archived'")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM skills{where} ORDER BY confidence DESC, updated_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [Skill.from_row(row) for row in rows]

    def all_slugs(self) -> list[str]:
        rows = self.conn.execute("SELECT slug FROM skills").fetchall()
        return [row["slug"] for row in rows]

    def count_skills(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS count FROM skills").fetchone()
        return int(row["count"]) if row else 0

    def search_skills(self, query: str, limit: int = 10) -> list[tuple[Skill, float]]:
        query = (query or "").strip()
        if not query:
            return [(skill, skill.confidence) for skill in self.list_skills(limit=limit)]
        tokens = [token for token in query.replace('"', " ").split() if token]
        if not tokens:
            return []
        if self.fts_enabled:
            match = " OR ".join(f'"{token}"' for token in tokens)
            try:
                rows = self.conn.execute(
                    """
                    SELECT s.*, bm25(skills_fts) AS rank
                    FROM skills_fts
                    JOIN skills s ON s.slug = skills_fts.slug
                    WHERE skills_fts MATCH ? AND s.status <> 'archived'
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (match, limit * 3),
                ).fetchall()
                results = []
                for row in rows:
                    skill = Skill.from_row(row)
                    rank = float(row["rank"] or 0.0)
                    lexical = 1.0 / (1.0 + abs(rank))
                    results.append((skill, lexical))
                results.sort(key=lambda item: (item[1], item[0].confidence), reverse=True)
                return results[:limit]
            except sqlite3.DatabaseError:
                pass
        like = "%" + query + "%"
        rows = self.conn.execute(
            """
            SELECT * FROM skills
            WHERE (name LIKE ? OR description LIKE ? OR tags LIKE ? OR body LIKE ?)
              AND status <> 'archived'
            ORDER BY confidence DESC
            LIMIT ?
            """,
            (like, like, like, like, limit),
        ).fetchall()
        return [(Skill.from_row(row), 0.5) for row in rows]

    def update_skill_fields(self, slug: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "confidence", "status", "last_used_at", "suggested", "accepted", "worked",
            "failed", "version", "updated_at", "path", "metadata",
        }
        updates = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "metadata":
                value = json.dumps(value, ensure_ascii=False)
            updates.append(f"{key} = ?")
            params.append(value)
        if not updates:
            return
        params.append(slug)
        self.conn.execute(f"UPDATE skills SET {', '.join(updates)} WHERE slug = ?", params)
        if self.fts_enabled and "status" not in fields:
            row = self.conn.execute("SELECT * FROM skills WHERE slug = ?", (slug,)).fetchone()
            if row:
                self.conn.execute("DELETE FROM skills_fts WHERE slug = ?", (slug,))
                self.conn.execute(
                    "INSERT INTO skills_fts (slug, name, description, tags, body) VALUES (?, ?, ?, ?, ?)",
                    (slug, row["name"], row["description"], row["tags"], row["body"]),
                )
        self.conn.commit()

    def archive_skill(self, slug: str) -> None:
        self.update_skill_fields(slug, status="archived", updated_at=now_iso())

    def delete_skill(self, slug: str) -> bool:
        """Remove one skill row and its search index row. Returns False for an unknown slug."""
        row = self.conn.execute("SELECT slug FROM skills WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            return False
        if self.fts_enabled:
            self.conn.execute("DELETE FROM skills_fts WHERE slug = ?", (slug,))
        self.conn.execute("DELETE FROM skills WHERE slug = ?", (slug,))
        self.conn.commit()
        return True

    def delete_thread(self, thread_id: str) -> bool:
        """Remove one thread row. Returns False for an unknown thread."""
        row = self.conn.execute("SELECT id FROM threads WHERE id = ?", (thread_id,)).fetchone()
        if row is None:
            return False
        self.conn.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
        self.conn.commit()
        return True

    def thread_referenced_by_other_skill(self, thread_id: str, slug: str) -> bool:
        """True when another skill still lists this thread in its provenance."""
        for row in self.conn.execute("SELECT slug, source_threads FROM skills WHERE slug <> ?", (slug,)):
            if thread_id in json.loads(row["source_threads"]):
                return True
        return False

    # ------------------------------------------------------------------- events
    def record_event(self, kind: str, *, skill_slug: str = "", thread_id: str = "", data: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            "INSERT INTO events (ts, kind, skill_slug, thread_id, data) VALUES (?, ?, ?, ?, ?)",
            (now_iso(), kind, skill_slug, thread_id, json.dumps(data or {}, ensure_ascii=False)),
        )
        self.conn.commit()

    def list_events(self, skill_slug: str = "", limit: int = 50) -> list[dict[str, Any]]:
        if skill_slug:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE skill_slug = ? ORDER BY id DESC LIMIT ?", (skill_slug, limit)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        skills = self.conn.execute(
            "SELECT status, COUNT(*) AS count FROM skills GROUP BY status"
        ).fetchall()
        categories = self.conn.execute(
            "SELECT category, COUNT(*) AS count FROM skills GROUP BY category ORDER BY count DESC"
        ).fetchall()
        thread_origin: dict[str, tuple[str, str]] = {}
        thread_counts: dict[tuple[str, str], int] = {}
        ingested_dates: list[dt.date] = []
        for row in self.conn.execute("SELECT id, source, channel, ingested_at FROM threads"):
            origin = (row["source"], row["channel"])
            thread_origin[row["id"]] = origin
            thread_counts[origin] = thread_counts.get(origin, 0) + 1
            ingested = _local_date(row["ingested_at"])
            if ingested is not None:
                ingested_dates.append(ingested)
        skills_by_source: dict[str, set[str]] = {}
        skills_by_channel: dict[tuple[str, str], set[str]] = {}
        created_dates: list[dt.date] = []
        for row in self.conn.execute("SELECT slug, source_threads, created_at FROM skills"):
            try:
                ids = json.loads(row["source_threads"])
            except (TypeError, ValueError):
                continue
            if not isinstance(ids, list):
                continue
            row_sources: set[str] = set()
            row_channels: set[tuple[str, str]] = set()
            for thread_id in ids:
                origin = thread_origin.get(str(thread_id))
                if origin is None:
                    continue
                row_sources.add(origin[0])
                row_channels.add(origin)
            for source in row_sources:
                skills_by_source.setdefault(source, set()).add(row["slug"])
            for channel in row_channels:
                skills_by_channel.setdefault(channel, set()).add(row["slug"])
            created = _local_date(row["created_at"])
            if created is not None:
                created_dates.append(created)
        by_source = [
            {
                "source": source,
                "threads": sum(count for (row_source, _), count in thread_counts.items() if row_source == source),
                "skills": len(skills_by_source.get(source, set())),
            }
            for source in {key[0] for key in thread_counts}
        ]
        by_source.sort(key=lambda item: (-item["threads"], item["source"]))
        by_channel = [
            {
                "source": source,
                "channel": channel,
                "threads": count,
                "skills": len(skills_by_channel[(source, channel)]),
            }
            for (source, channel), count in thread_counts.items()
            if skills_by_channel.get((source, channel))
        ]
        by_channel.sort(key=lambda item: (-item["skills"], -item["threads"], item["channel"]))
        by_channel = by_channel[:TOP_CHANNELS]
        now_local = dt.datetime.now(dt.timezone.utc).astimezone()
        current_week = _week_monday(now_local.date())
        weeks = [current_week - dt.timedelta(days=7 * (TIMELINE_WEEKS - 1 - index)) for index in range(TIMELINE_WEEKS)]
        ingests = {week: 0 for week in weeks}
        distills = {week: 0 for week in weeks}
        for day in ingested_dates:
            week = _week_monday(day)
            if week in ingests:
                ingests[week] += 1
        for day in created_dates:
            week = _week_monday(day)
            if week in distills:
                distills[week] += 1
        timeline = [
            {"week_start": week.isoformat(), "ingests": ingests[week], "distills": distills[week]}
            for week in weeks
        ]
        return {
            "threads": self.count_threads(),
            "skills": self.count_skills(),
            "by_status": {row["status"]: row["count"] for row in skills},
            "by_category": {row["category"]: row["count"] for row in categories},
            "by_source": by_source,
            "by_channel": by_channel,
            "timeline": timeline,
        }