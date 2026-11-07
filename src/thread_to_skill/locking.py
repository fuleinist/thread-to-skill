"""Advisory write locking for one workspace.

SQLite serialises single statements. The application sequence a write command
runs (check duplicate, render, insert, copy) is not atomic, so two `t2s`
processes in the same workspace can produce duplicate slugs, half-written
bundles, or a skill row with no directory. Write commands take an advisory lock
at `<workspace>/.lock` for the whole run; read-only commands never take it.

The lock is a JSON file with `pid`, `hostname`, and `started_at`. A lock whose
process is gone on the same host, or that is older than the stale threshold, is
reclaimed. Nothing here is enforced by the OS: the lock is a convention between
processes that agree to follow it.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .util import ensure_dir, now_iso, read_json

LOCK_FILENAME = ".lock"
DEFAULT_WAIT_SECONDS = 2.0
STALE_AFTER_SECONDS = 900.0
POLL_SECONDS = 0.1


class LockError(RuntimeError):
    pass


class LockConflict(LockError):
    """The workspace is locked by another process."""


def host_name() -> str:
    return socket.gethostname() or "unknown"


def read_lock(path: Path) -> dict[str, Any] | None:
    """The lock record, or None when the file is missing or cannot be read."""
    data = read_json(path)
    return data if isinstance(data, dict) else None


def pid_is_alive(pid: Any) -> bool:
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    try:
        os.kill(number, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists; we are only not allowed to signal it.
        return True
    except OSError:
        return False
    return True


def lock_age_seconds(record: dict[str, Any] | None, path: Path) -> float:
    """Seconds since the lock was taken, measured from `started_at`."""
    started = record.get("started_at") if isinstance(record, dict) else None
    try:
        parsed = dt.datetime.fromisoformat(str(started or "").replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return max(0.0, (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds())
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return 0.0


def is_stale(record: dict[str, Any] | None, path: Path) -> bool:
    """True when the lock can be reclaimed: the holder is gone, or it is old."""
    if lock_age_seconds(record, path) > STALE_AFTER_SECONDS:
        return True
    if not isinstance(record, dict):
        return False
    host = str(record.get("hostname") or "")
    if host and host != host_name():
        # A process on another machine cannot be checked from here.
        return False
    pid = record.get("pid")
    if pid is None:
        return False
    return not pid_is_alive(pid)


def conflict_message(path: Path, record: dict[str, Any] | None, waited: float) -> str:
    if not isinstance(record, dict):
        return f"workspace lock {path} exists but could not be read"
    pid = record.get("pid") or "unknown"
    host = record.get("hostname") or "unknown"
    started = record.get("started_at") or "unknown"
    note = "without waiting" if waited <= 0 else f"after waiting {waited:g}s"
    return f"workspace lock held by pid {pid} on host {host} since {started} ({note})"


def acquire(path: Path, wait: float = DEFAULT_WAIT_SECONDS) -> dict[str, Any]:
    """Take the lock and return the record this process wrote.

    Raises `LockConflict` when a live process holds it. `wait=0` fails at once.
    """
    path = Path(path)
    ensure_dir(path.parent)
    record = {
        "pid": os.getpid(),
        "hostname": host_name(),
        "started_at": now_iso(),
    }
    waited = max(0.0, float(wait))
    deadline = time.monotonic() + waited
    while True:
        holder = read_lock(path)
        if holder is None:
            if create(path, record):
                return record
            # Lost the race, or the file is there and cannot be read.
            holder = read_lock(path)
        if holder is not None and is_stale(holder, path) and release(path, holder):
            continue
        if time.monotonic() >= deadline:
            raise LockConflict(conflict_message(path, holder, waited))
        time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))


def create(path: Path, record: dict[str, Any]) -> bool:
    """Create the lock file exclusively. True when this process took the lock."""
    try:
        handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError as exc:
        raise LockError(f"cannot create workspace lock at {path}: {exc}") from exc
    with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    return True


def is_held_by(record: dict[str, Any], ours: dict[str, Any]) -> bool:
    return str(record.get("pid")) == str(ours.get("pid")) and str(record.get("started_at")) == str(
        ours.get("started_at")
    )


def release(path: Path, ours: dict[str, Any]) -> bool:
    """Delete the lock file, but only while it is still ours."""
    path = Path(path)
    if not path.exists():
        return True
    holder = read_lock(path)
    if holder is None or not is_held_by(holder, ours):
        # Another live process owns it now, so deleting here would steal it.
        return False
    try:
        os.unlink(str(path))
    except OSError:
        return False
    return True


@contextmanager
def workspace_lock(path: Path, wait: float = DEFAULT_WAIT_SECONDS) -> Iterator[dict[str, Any]]:
    """Hold the write lock for the block, and release it on every path out."""
    ours = acquire(path, wait)
    try:
        yield ours
    finally:
        release(path, ours)
