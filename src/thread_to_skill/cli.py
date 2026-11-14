"""Command-line interface for thread-to-skill."""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__, usage
from .adapters import load_path
from .config import Config, ConfigError, find_home
from .distill import DistillError, DuplicateSkill, ManualPromptReady, distill_threads, save_skill
from .feedback import apply_feedback, feedback_summary
from .filters import MessageFilters
from .hooks import (
    ALL_HOOK_HARNESSES,
    BROKEN_DOCTOR_STATUSES,
    DOCTOR_SAMPLE_PROMPT,
    DOCTOR_TIMEOUT_SECONDS,
    doctor_hooks,
    install_hooks,
)
from .install import ALL_HARNESSES, DEFAULT_HARNESSES, install_many
from .locking import DEFAULT_WAIT_SECONDS, LockError, acquire, release
from .models import Thread
from .providers import ProviderError, get_provider
from .redact import redact_thread
from .render import prune_snapshots, snapshot_bundle
from .review import apply_fixes, reason_counts, review_payload, review_skills
from .search import format_suggestions, suggest_skills
from .store import DEFAULT_SNAPSHOT_KEEP, Store, StoreError, inspect_schema, list_snapshots, snapshot_dir
from .taxonomy import all_category_keys, category_label, normalize_category, normalize_tags
from .util import (
    atomic_write_json,
    atomic_write_text,
    clamp,
    ensure_dir,
    human_number,
    now_iso,
    read_json,
    safe_console_text,
    truncate,
    unique_slug,
)
from .validate import parse_frontmatter, validate_config_skills, validate_skill_dir


# --------------------------------------------------------------------------- output


def _use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    if not _use_color():
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(text: str) -> str:
    return _c(text, "1")


def dim(text: str) -> str:
    return _c(text, "2")


def green(text: str) -> str:
    return _c(text, "32")


def yellow(text: str) -> str:
    return _c(text, "33")


def red(text: str) -> str:
    return _c(text, "31")


def cyan(text: str) -> str:
    return _c(text, "36")


def _print(text: str = "") -> None:
    print(safe_console_text(text))


def _print_err(text: str) -> None:
    print(safe_console_text(text), file=sys.stderr)


def _table(headers: list[str], rows: list[list[str]], max_width: int = 48) -> str:
    string_rows = [[truncate(str(cell), max_width) for cell in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in string_rows:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], len(cell))
    lines = []
    header_line = "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)).rstrip()
    lines.append(bold(header_line))
    lines.append(dim("  ".join("─" * width for width in widths)))
    for row in string_rows:
        lines.append("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


# --------------------------------------------------------------------------- helpers


def _home_path(args: argparse.Namespace) -> Path:
    return find_home(explicit=getattr(args, "home", None))


def _config(args: argparse.Namespace) -> Config:
    return Config.load(_home_path(args))


# Read-only commands never take the workspace lock. `version` touches no
# workspace, so it needs no lock either.
READ_ONLY_COMMANDS = {"list", "show", "search", "suggest", "stats", "review", "version"}


def _lock_path(args: argparse.Namespace) -> Path:
    """The lock file for the workspace. The workspace itself may not exist yet."""
    return Config(home=_home_path(args)).lock_path


def _takes_lock(args: argparse.Namespace) -> bool:
    if getattr(args, "action", "") == "doctor":
        # `t2s hooks doctor` only reads hook files and runs one sample call.
        return False
    if args.command == "review":
        # `t2s review` only reads. `--fix` writes, so only that path takes the lock.
        return bool(getattr(args, "fix", False))
    return args.command not in READ_ONLY_COMMANDS


_OPEN_STORES: list[Store] = []


def _open(args: argparse.Namespace) -> tuple[Config, Store]:
    config = _config(args)
    store = Store(config.db_path)
    _OPEN_STORES.append(store)
    return config, store


def _close_stores() -> None:
    while _OPEN_STORES:
        store = _OPEN_STORES.pop()
        try:
            store.close()
        except Exception:
            pass


def _normalized_path(config: Config, thread_id: str) -> Path:
    return config.threads_dir / f"{thread_id}.json"


def _resolve_threads(store: Store, config: Config, tokens: list[str]) -> list[Thread]:
    resolved: list[Thread] = []
    for token in tokens:
        if token in {"latest", "last"}:
            rows = store.list_threads(1)
            if not rows:
                raise ValueError("No threads ingested yet. Run `t2s ingest <path>` first.")
            row = rows[0]
        else:
            row = store.get_thread_row(token)
            if row is None:
                matches = store.find_threads_by_prefix(token)
                if len(matches) == 1:
                    row = matches[0]
                elif len(matches) > 1:
                    ids = ", ".join(match["id"] for match in matches[:8])
                    raise ValueError(f"Thread prefix '{token}' is ambiguous: {ids}")
                else:
                    raise ValueError(f"Unknown thread: {token}")
        normalized = _normalized_path(config, row["id"])
        if not normalized.exists():
            raise ValueError(
                f"Thread {row['id']} has no normalized copy at {normalized}. Re-run `t2s ingest`."
            )
        thread = Thread.from_dict(json.loads(normalized.read_text(encoding="utf-8")))
        if not any(existing.id == thread.id for existing in resolved):
            resolved.append(thread)
    if not resolved:
        raise ValueError("No threads selected")
    return resolved


def _resolve_skill(store: Store, token: str):
    skill = store.get_skill(token)
    if skill:
        return skill
    matches = [slug for slug in store.all_slugs() if slug.startswith(token)]
    if len(matches) == 1:
        return store.get_skill(matches[0])
    if len(matches) > 1:
        raise ValueError(f"Skill prefix '{token}' is ambiguous: {', '.join(sorted(matches)[:8])}")
    raise ValueError(f"Unknown skill: {token}")


def _split(value: str, default: list[str] | None = None) -> list[str]:
    if not value:
        return list(default or [])
    return [item.strip() for item in value.split(",") if item.strip()]


def _skill_payload(skill) -> dict[str, Any]:
    payload = skill.to_dict()
    payload["metadata"] = skill.metadata
    return payload


# ------------------------------------------------------------------ version snapshots


def _stored_versions(skill_dir: Path) -> list[int]:
    """Snapshot versions with a readable SKILL.md, oldest first."""
    return [
        version
        for version in list_snapshots(skill_dir)
        if (snapshot_dir(skill_dir, version) / "SKILL.md").exists()
    ]


def _available_versions(skill) -> list[int]:
    """Stored snapshots plus the current bundle, oldest first."""
    skill_dir = Path(skill.path)
    versions = _stored_versions(skill_dir)
    if (skill_dir / "SKILL.md").exists():
        versions.append(int(skill.version))
    return sorted(versions)


def _read_version_text(skill, version: int) -> str:
    """Read one version's SKILL.md, from its snapshot or from the live bundle."""
    skill_dir = Path(skill.path)
    snapshot = snapshot_dir(skill_dir, version) / "SKILL.md"
    if snapshot.exists():
        return snapshot.read_text(encoding="utf-8")
    if version == int(skill.version):
        return (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    available = ", ".join(f"v{item}" for item in _available_versions(skill))
    raise ValueError(f"Unknown version v{version} for {skill.slug}. Available: {available or 'none'}")


def _version_label(skill, version: int) -> str:
    """The path shown in a diff header."""
    skill_dir = Path(skill.path)
    if version == int(skill.version):
        return f"{skill.slug}/SKILL.md"
    return f"{skill.slug}/.versions/v{version}/SKILL.md"


def _split_sections(text: str) -> dict[str, list[str]]:
    """Group body lines under their top-level `##` heading.

    Lines before the first heading are not attributed to a section.
    """
    sections: dict[str, list[str]] = {}
    current = ""
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    return sections


def _change_counts(old: list[str], new: list[str]) -> tuple[int, int]:
    """Count added and removed lines between two lists of lines."""
    added = 0
    removed = 0
    matcher = difflib.SequenceMatcher(None, old, new)
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed += old_end - old_start
        added += new_end - new_start
    return added, removed


def _diff_counts(old_text: str, new_text: str) -> dict[str, Any]:
    """Whole-file counts plus per-section counts, sections in the order printed."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    added, removed = _change_counts(old_lines, new_lines)
    old_sections = _split_sections(old_text)
    new_sections = _split_sections(new_text)
    names = list(new_sections) + [name for name in old_sections if name not in new_sections]
    sections = []
    for name in names:
        section_added, section_removed = _change_counts(old_sections.get(name, []), new_sections.get(name, []))
        sections.append({"name": name, "added": section_added, "removed": section_removed})
    return {"added": added, "removed": removed, "sections": sections}


def _version_range(skill) -> tuple[int, int]:
    """The two newest versions to compare, oldest first."""
    available = _available_versions(skill)
    if len(available) < 2:
        raise ValueError(
            f"No snapshot to compare for {skill.slug}. A first render writes none; "
            "re-distill the thread to keep it."
        )
    return available[-2], available[-1]


def _version_list(versions: list[int]) -> str:
    """Versions as `v1, v2`, used in pruning messages."""
    return ", ".join(f"v{int(version)}" for version in versions)


# ------------------------------------------------------------------ manual edits

# Keys `t2s edit --field` may change. The value is the frontmatter mapping that
# holds the key; an empty parent means the key sits at the top level.
EDIT_FIELD_KEYS = {
    "name": "",
    "description": "",
    "category": "metadata",
    "tags": "metadata",
    "triggers": "metadata",
    "confidence": "metadata",
}
LIST_FIELD_KEYS = ("tags", "triggers")
MAX_TRIGGERS = 12
MAX_FIELD_PRINT = 60


def _editor_command() -> str:
    """The editor to open: T2S_EDITOR, then VISUAL, then EDITOR, then the platform default."""
    for name in ("T2S_EDITOR", "VISUAL", "EDITOR"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return "notepad" if os.name == "nt" else "vi"


def _run_editor(command: str, path: Path) -> int:
    argv = shlex.split(command, posix=os.name != "nt")
    if not argv:
        raise ValueError("No editor configured. Set T2S_EDITOR, VISUAL, or EDITOR.")
    argv.append(str(path))
    return subprocess.run(argv).returncode


def _metadata_block(frontmatter: dict[str, Any]) -> dict[str, Any]:
    block = frontmatter.get("metadata")
    if not isinstance(block, dict):
        block = {}
        frontmatter["metadata"] = block
    return block


def _field_target(frontmatter: dict[str, Any], key: str) -> dict[str, Any]:
    """The mapping that holds an editable key: the top level, or the metadata block."""
    parent = EDIT_FIELD_KEYS[key]
    return frontmatter if not parent else _metadata_block(frontmatter)


def _field_value(frontmatter: dict[str, Any], key: str) -> Any:
    """Read an editable key from wherever the format keeps it."""
    block = frontmatter.get("metadata")
    if isinstance(block, dict) and key in block:
        return block[key]
    return frontmatter.get(key, "")


def _apply_edit_field(frontmatter: dict[str, Any], key: str, value: str) -> None:
    target = _field_target(frontmatter, key)
    if key in LIST_FIELD_KEYS:
        items = _split(value)
        if not items:
            raise ValueError(f"--field {key} needs a value")
        if key == "tags":
            items = normalize_tags(items)
        else:
            items = items[:MAX_TRIGGERS]
        target[key] = items
    elif key == "confidence":
        try:
            target[key] = round(clamp(float(value)), 3)
        except ValueError:
            raise ValueError(f"--field confidence must be a number between 0 and 1, got {value}") from None
    else:
        text = value.strip()
        if not text:
            raise ValueError(f"--field {key} needs a value")
        target[key] = normalize_category(text) if key == "category" else text


def _edit_frontmatter_text(frontmatter: dict[str, Any]) -> str:
    """Rebuild the frontmatter block from parsed values, keeping the key order."""
    lines = ["---"]
    for key, value in frontmatter.items():
        if isinstance(value, dict):
            lines.append(f"{key}:")
            for sub_key, sub_value in value.items():
                lines.append(f"  {sub_key}: {json.dumps(sub_value, ensure_ascii=False)}")
        else:
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def _document_title(body: str, fallback: str) -> str:
    """The heading the body opens with, which is what `t2s show` prints."""
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback


# --------------------------------------------------------------------------- commands


def cmd_init(args: argparse.Namespace) -> int:
    home = _home_path(args)
    if (home / "config.json").exists() and not args.force:
        _print(f"Workspace already exists: {home}")
        _print("Use --force to reset provider settings without deleting skills or threads.")
        return 0
    config = Config.create(home, provider=args.provider, redact=not args.no_redact)
    if args.force:
        existing = Config.load(home)
        existing.data.update(config.data)
        existing.save()
        config = existing
    _print(f"{green('initialized')} {config.home}")
    _print(f"  skills  {config.skills_dir}")
    _print(f"  threads {config.threads_dir}")
    _print(f"  index   {config.db_path}")
    _print(f"  provider {config.default_provider} · redaction {'on' if config.redact_enabled else 'off'}")
    _print("")
    _print("Next: `t2s ingest ./path/to/export` then `t2s distill latest`.")
    return 0


INGEST_STATUSES = ("added", "updated", "skipped")


def _ingest_thread(store: Store, config: Config, thread: Thread, *, force: bool) -> str:
    """Write one normalized thread. Return `added`, `updated`, or `skipped`.

    The stored hash comes from the redacted thread, so a match means the
    conversation did not change and nothing needs to be written. A match only
    counts while the normalized copy exists, so a lost `threads/` file heals.
    """
    target = _normalized_path(config, thread.id)
    stored_hash = store.thread_content_hash(thread.id)
    if (
        not force
        and stored_hash is not None
        and stored_hash == thread.content_hash()
        and target.exists()
    ):
        return "skipped"
    atomic_write_json(target, thread.to_dict())
    store.add_thread(thread)
    return "added" if stored_hash is None else "updated"


def cmd_ingest(args: argparse.Namespace) -> int:
    config, store = _open(args)
    counts = {status: 0 for status in INGEST_STATUSES}
    results: list[dict[str, Any]] = []
    for raw_path in args.paths:
        threads = load_path(
            Path(raw_path),
            fmt=args.format,
            gap_minutes=args.gap_minutes or config.gap_minutes,
            channel=args.channel,
            title=args.title,
        )
        for thread in threads:
            redaction = None
            if config.redact_enabled:
                thread, redaction = redact_thread(thread, config.custom_redactions)
            status = _ingest_thread(store, config, thread, force=args.force)
            counts[status] += 1
            results.append(
                {
                    "id": thread.id,
                    "source": thread.source,
                    "channel": thread.channel,
                    "title": thread.title,
                    "messages": thread.message_count,
                    "participants": len(thread.participants),
                    "started_at": thread.started_at,
                    "redactions": redaction.summary() if redaction else "disabled",
                    "status": status,
                }
            )
    if args.json:
        _print(
            json.dumps(
                {
                    "added": counts["added"],
                    "updated": counts["updated"],
                    "skipped": counts["skipped"],
                    "threads": results,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    changed = [item for item in results if item["status"] != "skipped"]
    if results and not changed:
        _print("no changes")
    else:
        rows = [
            [item["id"], item["source"], item["channel"], str(item["messages"]), truncate(item["title"], 52)]
            for item in changed
        ]
        _print(_table(["THREAD", "SOURCE", "CHANNEL", "MSGS", "TITLE"], rows))
    _print("")
    redactions = {
        item["redactions"]
        for item in results
        if item["redactions"] not in {"disabled", "none", "no sensitive data detected"}
    }
    _print(
        f"{green(human_number(counts['added']) + ' added')} · "
        f"{human_number(counts['updated'])} updated · "
        f"{human_number(counts['skipped'])} skipped · "
        f"{human_number(sum(item['messages'] for item in results))} messages"
    )
    if redactions:
        _print(dim("redaction: " + "; ".join(sorted(redactions))))
    _print(f"Indexed in {config.db_path}")
    return 0


def cmd_distill(args: argparse.Namespace) -> int:
    config, store = _open(args)
    threads = _resolve_threads(store, config, args.threads)
    provider_name = args.provider or config.default_provider
    provider = get_provider(
        provider_name,
        model=args.model,
        base_url=args.base_url,
        api_key_env=args.api_key_env,
        command=args.command,
        timeout=args.timeout,
    )
    try:
        data = distill_threads(
            threads,
            provider,
            config=config,
            category=args.category,
            tags=_split(args.tags),
            slug=args.slug,
            filters=MessageFilters(
                since=args.since,
                until=args.until,
                participants=tuple(_split(args.participants)),
                max_messages=args.max_messages,
                exclude_bots=args.exclude_bots,
            ),
            synthesize=args.synthesize,
            max_cost=args.max_cost,
        )
    except ManualPromptReady as exc:
        _print(f"{yellow('manual')} Distillation prompt written to {exc.path}")
        _print("Send that file to your agent, then run `t2s distill ... --provider command` or rerun with an API provider.")
        return 2
    if args.json and args.dry_run:
        _print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if args.dry_run:
        _print(f"{bold(data['title'])}")
        _print(f"category: {data['category']}  confidence: {data['confidence']}")
        _print(f"tags: {', '.join(data['tags']) or '—'}")
        _print(f"steps: {len(data['procedure'])}  rules: {len(data['decision_rules'])}  pitfalls: {len(data['pitfalls'])}")
        if args.synthesize:
            _print(f"conflicts: {len(data.get('conflicts') or [])}")
        return 0
    try:
        skill = save_skill(config, store, data, threads, on_duplicate=args.on_duplicate)
    except DuplicateSkill as exc:
        _print(f"{yellow('skipped')} {exc}")
        _print(f"Existing skill: {exc.existing.path}")
        _print("Use --on-duplicate merge to fold this thread into it, or new to create a separate skill.")
        return 2

    installed = []
    if args.install:
        harnesses = _split(args.install, DEFAULT_HARNESSES)
        if "all" in harnesses:
            harnesses = list(ALL_HARNESSES)
        installed = install_many(
            skill,
            harnesses,
            scope=args.scope,
            project_root=Path(args.project_root) if args.project_root else None,
            force=args.force,
        )
    if args.json:
        payload = _skill_payload(skill)
        payload["installed"] = [
            {"harness": result.harness, "path": str(result.path), "status": result.status, "detail": result.detail}
            for result in installed
        ]
        _print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    _print(f"{green('created')} {bold(skill.slug)} v{skill.version}")
    _print(f"  title      {skill.title}")
    _print(f"  category   {skill.category} ({category_label(skill.category)})")
    _print(f"  tags       {', '.join(skill.tags) or '—'}")
    _print(f"  confidence {skill.confidence:.2f}")
    _print(f"  sources    {', '.join(skill.source_threads)}")
    _print(f"  path       {skill.path}")
    for result in installed:
        _print(f"  {result.line()}")
    if not installed:
        _print(dim("Install with `t2s install <slug> --harness all`."))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    _, store = _open(args)
    skills = store.list_skills(
        category=args.category,
        tag=args.tag,
        status=args.status,
        limit=args.limit,
    )
    if args.json:
        _print(json.dumps([_skill_payload(skill) for skill in skills], ensure_ascii=False, indent=2))
        return 0
    if not skills:
        _print("No skills yet. Run `t2s ingest` and `t2s distill latest`.")
        return 0
    rows = [
        [
            skill.slug,
            skill.category,
            f"{skill.confidence:.2f}",
            f"v{skill.version}",
            skill.status,
            str(skill.worked + skill.failed),
            truncate(skill.title, 46),
        ]
        for skill in skills
    ]
    _print(_table(["SLUG", "CATEGORY", "CONF", "VER", "STATUS", "USES", "TITLE"], rows))
    _print("")
    _print(dim(f"{len(skills)} skill(s) · {store.count_threads()} thread(s) indexed"))
    return 0


def _diff_summary(skill) -> str:
    """One line describing the latest merge. Empty when nothing was kept."""
    versions = _available_versions(skill)
    if len(versions) < 2:
        return ""
    counts = _diff_counts(_read_version_text(skill, versions[-2]), _read_version_text(skill, versions[-1]))
    return (
        f"last change  v{versions[-2]} → v{versions[-1]}  "
        f"+{counts['added']} added · -{counts['removed']} removed"
    )


def cmd_show(args: argparse.Namespace) -> int:
    _, store = _open(args)
    skill = _resolve_skill(store, args.slug)
    if skill is None:
        raise ValueError(f"Unknown skill: {args.slug}")
    if args.json:
        _print(json.dumps(_skill_payload(skill), ensure_ascii=False, indent=2))
        return 0
    if args.raw:
        _print((Path(skill.path) / "SKILL.md").read_text(encoding="utf-8"))
    else:
        _print(f"{bold(skill.title)}  {dim(skill.slug)}")
        _print(f"{skill.description}")
        _print("")
        _print(f"category   {skill.category} ({category_label(skill.category)})")
        _print(f"tags       {', '.join(skill.tags) or '—'}")
        _print(f"triggers   {'; '.join(skill.triggers) or '—'}")
        _print(f"confidence {skill.confidence:.2f}   version {skill.version}   status {skill.status}")
        _print(f"usage      suggested {skill.suggested} · accepted {skill.accepted} · worked {skill.worked} · failed {skill.failed}")
        _print(f"path       {skill.path}")
        summary = _diff_summary(skill)
        if summary:
            _print(summary)
        _print("")
        _print(f"Read {dim(str(Path(skill.path) / 'SKILL.md'))} or use --raw to print it.")
    return 0


def _print_diff(lines: list[str]) -> None:
    for line in lines:
        if line.startswith("+++"):
            _print(bold(line))
        elif line.startswith("---"):
            _print(dim(line))
        elif line.startswith("@"):
            _print(cyan(line))
        elif line.startswith("+"):
            _print(green(line))
        elif line.startswith("-"):
            _print(red(line))
        else:
            _print(line)


def cmd_diff(args: argparse.Namespace) -> int:
    _, store = _open(args)
    skill = _resolve_skill(store, args.slug)
    if skill is None:
        raise ValueError(f"Unknown skill: {args.slug}")
    from_version = args.from_version
    to_version = args.to_version
    if from_version is None or to_version is None:
        newest_first, newest_last = _version_range(skill)
        from_version = from_version if from_version is not None else newest_first
        to_version = to_version if to_version is not None else newest_last
    old_text = _read_version_text(skill, from_version)
    new_text = _read_version_text(skill, to_version)
    counts = _diff_counts(old_text, new_text)
    pruned = prune_snapshots(Path(skill.path), args.keep)
    store.record_event(
        "diff",
        skill_slug=skill.slug,
        data={
            "from": from_version,
            "to": to_version,
            "added": counts["added"],
            "removed": counts["removed"],
            "pruned": pruned,
        },
    )
    payload = {
        "slug": skill.slug,
        "from": from_version,
        "to": to_version,
        "sections": counts["sections"],
        "added": counts["added"],
        "removed": counts["removed"],
    }
    if args.json:
        _print(json.dumps(payload, ensure_ascii=False, indent=2))
        if pruned:
            _print_err(f"pruned {len(pruned)} snapshot(s): {_version_list(pruned)}")
        return 0
    _print(f"{bold(skill.slug)}  {dim(f'v{from_version} → v{to_version}')}")
    _print("")
    if args.stat:
        rows = [[item["name"], str(item["added"]), str(item["removed"])] for item in payload["sections"]]
        rows.append(["Total", str(payload["added"]), str(payload["removed"])])
        _print(_table(["SECTION", "ADDED", "REMOVED"], rows))
        _print("")
    _print_diff(
        list(
            difflib.unified_diff(
                old_text.splitlines(),
                new_text.splitlines(),
                fromfile=_version_label(skill, from_version),
                tofile=_version_label(skill, to_version),
                lineterm="",
            )
        )
    )
    if pruned:
        _print("")
        _print(dim(f"pruned {len(pruned)} snapshot(s): {_version_list(pruned)}"))
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    _, store = _open(args)
    skill = _resolve_skill(store, args.slug)
    skill_dir = Path(skill.path)
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        raise ValueError(f"Skill {skill.slug} has no SKILL.md at {skill_md}. Re-run `t2s distill`.")

    original_bytes = skill_md.read_bytes()
    original_text = original_bytes.decode("utf-8")
    changed: dict[str, str] = {}
    editor = ""

    if args.field or args.tag:
        frontmatter, body = parse_frontmatter(original_text)
        for raw in args.field:
            key, _, value = raw.partition("=")
            if key not in EDIT_FIELD_KEYS:
                supported = ", ".join(sorted(EDIT_FIELD_KEYS))
                raise ValueError(f"Unsupported --field key: {key}. Supported: {supported}")
            if not value.strip():
                raise ValueError(f"--field needs KEY=VALUE, got {raw}")
            before = _field_value(frontmatter, key)
            _apply_edit_field(frontmatter, key, value)
            after = _field_value(frontmatter, key)
            if after != before:
                changed[key] = ", ".join(after) if isinstance(after, list) else str(after)
        if args.tag:
            target = _field_target(frontmatter, "tags")
            before = [str(item) for item in (target.get("tags") or [])]
            merged = normalize_tags(before, args.tag)
            if merged != before:
                changed["tags"] = ", ".join(merged)
            target["tags"] = merged
        text = _edit_frontmatter_text(frontmatter) + body.rstrip("\n") + "\n"
    else:
        editor = _editor_command()
        code = _run_editor(editor, skill_md)
        if code != 0:
            _print_err(f"editor exited with {code}; {skill.slug}/SKILL.md was left unchanged")
            return 1
        text = skill_md.read_text(encoding="utf-8")

    if text == original_text:
        _print(f"{skill.slug}: no changes")
        return 0

    previous_version = int(skill.version)
    version = previous_version if args.no_version_bump else previous_version + 1
    updated_at = now_iso()

    # Keep the live bundle before it changes, so `t2s diff` still shows this version.
    snapshot_root = snapshot_dir(skill_dir, previous_version)
    made_snapshot = False
    if version != previous_version and not snapshot_root.exists():
        made_snapshot = snapshot_bundle(skill_dir, previous_version) is not None

    atomic_write_text(skill_md, text)
    issues = validate_skill_dir(skill_dir)
    errors = [issue for issue in issues if issue.level == "error"]
    if errors:
        skill_md.write_bytes(original_bytes)
        if made_snapshot:
            shutil.rmtree(snapshot_root, ignore_errors=True)
            try:
                snapshot_root.parent.rmdir()
            except OSError:
                pass
        for issue in errors:
            _print(red(issue.line()))
        return 1

    frontmatter, body = parse_frontmatter(text)

    block = _metadata_block(frontmatter)
    block["version"] = version
    block["updated_at"] = updated_at
    if "category" in changed:
        block["category_label"] = category_label(str(block.get("category") or "uncategorized"))
    text = _edit_frontmatter_text(frontmatter) + body.rstrip("\n") + "\n"

    skill.name = str(_field_value(frontmatter, "name") or skill.slug)
    skill.title = _document_title(body, skill.title)
    skill.description = str(_field_value(frontmatter, "description") or "")
    skill.category = normalize_category(str(_field_value(frontmatter, "category") or skill.category))
    skill.tags = normalize_tags([str(item) for item in (_field_value(frontmatter, "tags") or [])])
    skill.triggers = [str(item) for item in (_field_value(frontmatter, "triggers") or [])][:MAX_TRIGGERS]
    try:
        skill.confidence = round(clamp(float(_field_value(frontmatter, "confidence"))), 3)
    except (TypeError, ValueError):
        pass  # keep the stored value when the frontmatter has no usable number
    skill.version = version
    skill.updated_at = updated_at
    skill.body = text

    if version != previous_version:
        prune_snapshots(skill_dir)

    metadata = read_json(skill_dir / "metadata.json", {})
    if not isinstance(metadata, dict):
        metadata = {}
    data = metadata.get("data") if isinstance(metadata.get("data"), dict) else {}
    data.update({
        "title": skill.title,
        "description": skill.description,
        "category": skill.category,
        "tags": skill.tags,
        "triggers": skill.triggers,
        "confidence": skill.confidence,
    })
    when_to_use = _field_value(frontmatter, "when_to_use")
    if isinstance(when_to_use, list) and when_to_use:
        data["when_to_use"] = [str(item) for item in when_to_use]
    metadata.update({"slug": skill.slug, "version": version, "updated_at": updated_at, "data": data})

    atomic_write_text(skill_md, text)
    atomic_write_json(skill_dir / "metadata.json", metadata)
    skill.metadata = metadata
    # save_skill writes the row and refreshes the skills_fts entry in one step.
    store.save_skill(skill)
    store.record_event(
        "edited",
        skill_slug=skill.slug,
        data={"version": version, "fields": sorted(changed), "editor": editor or "none"},
    )

    label = f"v{previous_version} → v{version}" if version != previous_version else f"v{version}"
    _print(f"{green('edited')} {bold(skill.slug)} {label}")
    if changed:
        for key in sorted(changed):
            _print(f"  {key:<12} {truncate(changed[key], 60)}")
    elif editor:
        _print(f"  {'editor':<12} {truncate(editor, 60)}")
    for issue in issues:
        if issue.level == "warning":
            _print(f"  {yellow('warning')} {issue.message}")
    return 0


def _trash_root(config: Config, slug: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    root = config.trash_dir / f"{slug}-{stamp}"
    suffix = 1
    while root.exists():
        suffix += 1
        root = config.trash_dir / f"{slug}-{stamp}-{suffix}"
    return root


def _move_to_trash(source: Path, target: Path) -> bool:
    if not source.exists():
        return False
    ensure_dir(target.parent)
    shutil.move(str(source), str(target))
    return True


def _confirm_delete(slug: str) -> None:
    if not sys.stdin.isatty():
        raise ValueError("Refusing to delete without --yes in a non-interactive shell.")
    _print(f"This moves {slug} to the workspace trash and removes its index row.")
    answer = input(f"Type {slug} to confirm: ").strip()
    if answer != slug:
        raise ValueError("Cancelled. Nothing was deleted.")


def cmd_archive(args: argparse.Namespace) -> int:
    _, store = _open(args)
    skill = _resolve_skill(store, args.slug)
    if skill is None:
        raise ValueError(f"Unknown skill: {args.slug}")
    if skill.status == "archived":
        if args.json:
            _print(json.dumps({"slug": skill.slug, "status": "archived", "changed": False}, ensure_ascii=False))
            return 0
        _print(f"{skill.slug} is already archived")
        return 0
    store.archive_skill(skill.slug)
    store.record_event("archived", skill_slug=skill.slug)
    if args.json:
        _print(json.dumps({"slug": skill.slug, "status": "archived", "changed": True}, ensure_ascii=False))
        return 0
    _print(f"{green('archived')} {skill.slug} - bundle kept, hidden from list and suggest")
    return 0


def cmd_unarchive(args: argparse.Namespace) -> int:
    _, store = _open(args)
    skill = _resolve_skill(store, args.slug)
    if skill is None:
        raise ValueError(f"Unknown skill: {args.slug}")
    if skill.status != "archived":
        if args.json:
            _print(json.dumps({"slug": skill.slug, "status": skill.status, "changed": False}, ensure_ascii=False))
            return 0
        _print(f"{skill.slug} is not archived (status {skill.status})")
        return 0
    store.update_skill_fields(skill.slug, status="active", updated_at=now_iso())
    store.record_event("unarchived", skill_slug=skill.slug)
    if args.json:
        _print(json.dumps({"slug": skill.slug, "status": "active", "changed": True}, ensure_ascii=False))
        return 0
    _print(f"{green('unarchived')} {skill.slug} - restored to active")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    config, store = _open(args)
    skill = _resolve_skill(store, args.slug)
    if skill is None:
        raise ValueError(f"Unknown skill: {args.slug}")
    if not args.yes:
        _confirm_delete(skill.slug)
    bundle = Path(skill.path)
    trash_root = _trash_root(config, skill.slug)
    bundle_moved = _move_to_trash(bundle, trash_root / bundle.name)
    removed_threads: list[str] = []
    if not args.keep_thread:
        for thread_id in skill.source_threads:
            if store.thread_referenced_by_other_skill(thread_id, skill.slug):
                continue
            normalized = _normalized_path(config, thread_id)
            _move_to_trash(normalized, trash_root / "threads" / normalized.name)
            if store.delete_thread(thread_id):
                removed_threads.append(thread_id)
    removed = store.delete_skill(skill.slug)
    store.record_event(
        "deleted",
        skill_slug=skill.slug,
        data={"trash": str(trash_root), "threads": removed_threads, "keep_thread": bool(args.keep_thread)},
    )
    if args.json:
        _print(
            json.dumps(
                {
                    "slug": skill.slug,
                    "deleted": removed,
                    "bundle_moved": bundle_moved,
                    "trash": str(trash_root),
                    "threads_removed": removed_threads,
                },
                ensure_ascii=False,
            )
        )
        return 0
    _print(f"{green('deleted')} {skill.slug} - bundle moved to {trash_root}")
    if removed_threads:
        _print(dim(f"  {len(removed_threads)} source thread(s) moved to trash with it"))
    elif args.keep_thread:
        _print(dim("  source threads kept (--keep-thread)"))
    else:
        _print(dim("  no source threads to remove"))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    _, store = _open(args)
    results = store.search_skills(args.query, limit=args.limit)
    if args.json:
        _print(json.dumps([{**_skill_payload(skill), "score": round(score, 3)} for skill, score in results], ensure_ascii=False, indent=2))
        return 0
    if not results:
        _print(f"No skills matched {args.query!r}.")
        return 0
    rows = [[skill.slug, skill.category, f"{skill.confidence:.2f}", truncate(skill.title, 56)] for skill, _ in results]
    _print(_table(["SLUG", "CATEGORY", "CONF", "TITLE"], rows))
    return 0


def _hook_json(fmt: str, text: str, count: int) -> str:
    if fmt in {"claude", "deepseek"}:
        return json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": text,
                }
            },
            ensure_ascii=False,
        )
    if fmt == "cursor":
        # Cursor's beforeSubmitPrompt output supports only `continue` and
        # `user_message`; `agent_message` belongs to other hook events.
        return json.dumps({"continue": True, "user_message": text}, ensure_ascii=False)
    raise ValueError(f"Unknown hook format: {fmt}")


def cmd_suggest(args: argparse.Namespace) -> int:
    config, store = _open(args)
    prompt = " ".join(args.prompt).strip()
    if not prompt:
        if sys.stdin.isatty():
            raise ValueError("Provide a prompt, or pipe one on stdin.")
        prompt = sys.stdin.read().strip()
    if not prompt:
        return 0
    results = suggest_skills(
        store,
        prompt,
        limit=args.limit,
        threshold=args.threshold,
        category=args.category,
    )
    text = format_suggestions(results, markdown=False)
    if args.format == "json":
        _print(json.dumps({"prompt": prompt, "suggestions": results}, ensure_ascii=False, indent=2))
        return 0
    if args.format in {"claude", "cursor", "deepseek"}:
        if text:
            _print(_hook_json(args.format, text, len(results)))
        return 0
    if not results:
        _print("No matching skills.")
        return 0
    _print(text)
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    _, store = _open(args)
    skill = _resolve_skill(store, args.slug)
    if skill is None:
        raise ValueError(f"Unknown skill: {args.slug}")
    updated = apply_feedback(store, skill.slug, args.outcome, args.note or "")
    summary = feedback_summary(updated)
    if args.json:
        _print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    _print(f"{green('recorded')} {updated.slug} · {args.outcome}")
    _print(
        f"  confidence {summary['confidence']:.2f} · success {summary['success_rate']:.2f} · "
        f"status {summary['status']}"
    )
    _print(
        f"  suggested {summary['suggested']} · accepted {summary['accepted']} · "
        f"worked {summary['worked']} · failed {summary['failed']}"
    )
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    config, store = _open(args)
    skill = _resolve_skill(store, args.slug)
    if skill is None:
        raise ValueError(f"Unknown skill: {args.slug}")
    harnesses = _split(args.harness, DEFAULT_HARNESSES)
    if "all" in harnesses:
        harnesses = list(ALL_HARNESSES)
    unknown = [name for name in harnesses if name not in ALL_HARNESSES]
    if unknown:
        raise ValueError(f"Unknown harness: {', '.join(unknown)}")
    results = install_many(
        skill,
        harnesses,
        scope=args.scope,
        project_root=Path(args.project_root) if args.project_root else None,
        force=args.force,
    )
    if args.json:
        _print(json.dumps([result.__dict__ for result in results], ensure_ascii=False, indent=2, default=str))
        return 0
    for result in results:
        _print(result.line())
    installed = sum(1 for result in results if result.status == "installed")
    _print("")
    _print(f"{green('installed')} {installed}/{len(results)} target(s)")
    return 0 if installed or all(result.status == "skipped" for result in results) else 1


def cmd_hooks(args: argparse.Namespace) -> int:
    if getattr(args, "action", "install") == "doctor":
        return cmd_hooks_doctor(args)
    harnesses = _split(args.harness, ["all"])
    unknown = [name for name in harnesses if name not in ALL_HOOK_HARNESSES and name != "all"]
    if unknown:
        raise ValueError(f"Unknown hook harness: {', '.join(unknown)}")
    if "all" in harnesses:
        harnesses = ["all"]
    results = []
    for harness in harnesses:
        results.extend(
            install_hooks(
                harness,
                scope=args.scope,
                project_root=Path(args.project_root) if args.project_root else None,
                dry_run=args.dry_run,
            )
        )
    if args.json:
        _print(json.dumps([result.__dict__ for result in results], ensure_ascii=False, indent=2, default=str))
        return 0
    for result in results:
        _print(result.line())
    if not args.dry_run:
        _print("")
        _print(dim("Hooks call `t2s suggest`; keep `t2s` on PATH or set T2S_BIN."))
    return 0


def cmd_hooks_doctor(args: argparse.Namespace) -> int:
    harnesses = _split(args.harness, ["all"])
    unknown = [name for name in harnesses if name not in ALL_HOOK_HARNESSES and name != "all"]
    if unknown:
        raise ValueError(f"Unknown hook harness: {', '.join(unknown)}")
    results = doctor_hooks(
        args.harness,
        scope=args.scope,
        project_root=Path(args.project_root) if args.project_root else None,
        prompt=args.prompt or DOCTOR_SAMPLE_PROMPT,
        timeout=float(args.timeout),
    )
    broken = sum(1 for result in results if result.status in BROKEN_DOCTOR_STATUSES)
    detected = sum(1 for result in results if result.status != "missing")
    if args.json:
        _print(json.dumps([result.__dict__ for result in results], ensure_ascii=False, indent=2, default=str))
        return 1 if broken else 0
    for result in results:
        if result.status == "ok":
            _print(green(result.line()))
        elif result.status in BROKEN_DOCTOR_STATUSES:
            _print(red(result.line()))
        elif result.status == "no-match":
            _print(yellow(result.line()))
        else:
            _print(dim(result.line()))
    _print("")
    _print(f"{len(results)} host(s) checked · {detected} hook(s) detected · {broken} broken")
    sample = next((result.sample_command for result in results if result.sample_command), "")
    if sample:
        _print(dim(f"one sample call: {sample}"))
    _print(dim("The doctor reads hook files and runs one sample call. It never runs the host."))
    return 1 if broken else 0


def cmd_validate(args: argparse.Namespace) -> int:
    config, store = _open(args)
    targets: list[Path] = []
    if args.all or not args.slug:
        targets = [Path(skill.path) for skill in store.list_skills(limit=5000)]
    else:
        skill = _resolve_skill(store, args.slug)
        if skill is None:
            raise ValueError(f"Unknown skill: {args.slug}")
        targets = [Path(skill.path)]
    if not targets:
        _print("No skills to validate.")
        return 0
    errors = 0
    for target in targets:
        issues = validate_skill_dir(target)
        for issue in issues:
            if issue.level == "error":
                errors += 1
                _print(red(issue.line()))
            elif issue.level == "ok":
                _print(green(issue.line()))
            elif issue.level == "warning":
                _print(yellow(issue.line()))
            else:
                _print(issue.line())
    _print("")
    _print(f"{len(targets)} skill(s) checked · {errors} error(s)")
    return 1 if errors else 0


def cmd_stats(args: argparse.Namespace) -> int:
    config, store = _open(args)
    stats = store.stats()
    if args.json:
        _print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0
    if stats["threads"] == 0 and stats["skills"] == 0:
        _print("No threads ingested yet. Run `t2s ingest <path>` to add threads.")
        return 0
    _print(f"{bold('Workspace')} {config.home}")
    _print(f"  threads {stats['threads']} · skills {stats['skills']}")
    if stats["by_status"]:
        _print("  status  " + ", ".join(f"{key}={value}" for key, value in stats["by_status"].items()))
    if stats["by_category"]:
        _print("")
        _print(bold("By category"))
        rows = [[category, category_label(category), str(count)] for category, count in stats["by_category"].items()]
        _print(_table(["CATEGORY", "LABEL", "SKILLS"], rows))
    if args.by_source:
        _print("")
        if not stats["by_source"]:
            _print("No skills per source yet.")
        else:
            _print(bold("By source"))
            _print(
                _table(
                    ["SOURCE", "THREADS", "SKILLS"],
                    [[row["source"], str(row["threads"]), str(row["skills"])] for row in stats["by_source"]],
                )
            )
    if args.by_channel:
        _print("")
        if not stats["by_channel"]:
            _print("No channels with skills yet.")
        else:
            _print(bold("Top channels"))
            _print(
                _table(
                    ["SOURCE", "CHANNEL", "THREADS", "SKILLS"],
                    [
                        [row["source"], row["channel"], str(row["threads"]), str(row["skills"])]
                        for row in stats["by_channel"]
                    ],
                )
            )
    if args.timeline:
        _print("")
        if not stats["timeline"]:
            _print("Nothing in the last 12 weeks.")
        else:
            _print(bold("Timeline"))
            _print(
                _table(
                    ["WEEK", "INGESTS", "DISTILLS"],
                    [
                        [entry["week_start"], str(entry["ingests"]), str(entry["distills"])]
                        for entry in stats["timeline"]
                    ],
                )
            )
    if args.cost:
        _print("")
        _print(bold("Cost"))
        events = [event for event in store.list_events(limit=2000) if event["kind"] == "distilled"]
        models: dict[str, dict[str, float]] = {}
        unknown_runs = 0
        unrecorded = 0
        priced_runs = 0
        total_usd = 0.0
        for event in events:
            payload = event.get("data") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    payload = {}
            if not isinstance(payload, dict):
                payload = {}
            record = payload.get("usage") or {}
            if not isinstance(record, dict) or not record:
                unrecorded += 1
                continue
            cost = payload.get("cost") or {}
            if not isinstance(cost, dict):
                cost = {}
            model = str(cost.get("model") or record.get("model") or "unknown")
            bucket = models.setdefault(
                model, {"runs": 0.0, "prompt": 0.0, "completion": 0.0, "usd": 0.0, "estimated": 0.0}
            )
            bucket["runs"] += 1
            bucket["prompt"] += float(record.get("prompt_tokens") or 0)
            bucket["completion"] += float(record.get("completion_tokens") or 0)
            if record.get("estimated"):
                bucket["estimated"] += 1
            if cost.get("status") == "known" and cost.get("usd") is not None:
                bucket["usd"] += float(cost["usd"])
                total_usd += float(cost["usd"])
                priced_runs += 1
            else:
                unknown_runs += 1
        if not models:
            _print("No recorded distillation usage yet.")
        else:
            rows = [
                [
                    model,
                    str(int(values["runs"])),
                    str(int(values["prompt"])),
                    str(int(values["completion"])),
                    f"{values['usd']:.4f}" if values["usd"] else "unknown",
                ]
                for model, values in sorted(models.items())
            ]
            _print(_table(["MODEL", "RUNS", "PROMPT", "OUTPUT", "USD"], rows))
            _print(f"  total {total_usd:.4f} USD across {priced_runs} priced run(s)")
            if unknown_runs:
                _print(f"  {unknown_runs} run(s) used a model with no known price")
            if unrecorded:
                _print(f"  {unrecorded} distillation(s) recorded no usage")
            estimated = sum(int(values["estimated"]) for values in models.values())
            if estimated:
                _print(f"  {estimated} run(s) reported estimated token counts, not provider counts")
        _print(f"  price table last updated {usage.PRICE_TABLE_LAST_UPDATED}")
    recent = store.list_events(limit=8)
    if recent:
        _print("")
        _print(bold("Recent events"))
        for event in recent:
            suffix = f" · {event['skill_slug']}" if event["skill_slug"] else ""
            _print(f"  {event['ts']}  {event['kind']}{suffix}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    config, store = _open(args)
    stale_days = args.stale_days if args.stale_days is not None else config.review_stale_days
    confidence = (
        args.confidence if args.confidence is not None else config.review_confidence_threshold
    )
    duplicate_threshold = (
        args.duplicate_threshold
        if args.duplicate_threshold is not None
        else config.review_duplicate_threshold
    )
    conflict_threshold = (
        args.conflict_threshold
        if args.conflict_threshold is not None
        else config.review_conflict_threshold
    )
    findings = review_skills(
        store,
        stale_days=stale_days,
        confidence_threshold=confidence,
        duplicate_threshold=duplicate_threshold,
        conflict_threshold=conflict_threshold,
    )
    checked = len(store.list_skills(limit=1000))
    applied: dict[str, Any] = {"archived": [], "grouped": []}
    if args.fix:
        applied = apply_fixes(store, findings, allow_duplicate_groups=args.yes)

    if args.json:
        payload = review_payload(
            findings,
            stale_days=stale_days,
            confidence_threshold=confidence,
            duplicate_threshold=duplicate_threshold,
            conflict_threshold=conflict_threshold,
            checked=checked,
        )
        payload["applied"] = applied
        _print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if checked == 0:
        _print("No skills to review yet. Run `t2s distill <thread>` first.")
        return 0
    if not findings:
        _print(green(f"No skills need review ({checked} checked, stale after {stale_days} days)."))
        return 0

    _print(bold(f"Review: {len(findings)} of {checked} skills need attention"))
    rows = [
        [
            finding.slug,
            ", ".join(finding.reasons),
            f"{finding.age_days}d" if finding.age_days is not None else "-",
            finding.action,
        ]
        for finding in findings
    ]
    _print(_table(["SKILL", "REASONS", "AGE", "ACTION"], rows))
    counts = reason_counts(findings)
    _print("")
    _print("  ".join(f"{reason}={count}" for reason, count in counts.items() if count))
    _print("")
    for finding in findings:
        _print(f"  {finding.slug}: {finding.detail}")
    _print("")
    if args.fix:
        archived = applied["archived"]
        if archived:
            _print(green(f"archived {len(archived)} skill(s): " + ", ".join(archived)))
        else:
            _print("Nothing to archive.")
        if args.yes:
            _print(f"grouped {len(applied['grouped'])} duplicate(s) in skill metadata")
        else:
            _print(dim("duplicate groups were not recorded; add --yes to record them"))
    else:
        _print(dim("Read-only. `t2s review --fix` archives needs-review skills with zero successes."))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    home = _home_path(args)
    checks: list[tuple[str, bool, str]] = []
    checks.append(("python", sys.version_info >= (3, 10), sys.version.split()[0]))
    checks.append(("workspace", (home / "config.json").exists(), str(home)))
    config = None
    if (home / "config.json").exists():
        try:
            config = Config.load(home)
            checks.append(("sqlite", config.db_path.exists(), str(config.db_path)))
        except ConfigError as exc:
            checks.append(("config", False, str(exc)))
    migrations_applied: list[int] = []
    backup_path = ""
    schema_note = ""
    if config and config.db_path.exists():
        report = inspect_schema(config.db_path)
        if args.migrate and report["needs_upgrade"]:
            store = Store(config.db_path)
            _OPEN_STORES.append(store)
            info = store.schema_info()
            migrations_applied = info["migrated"]
            backup_path = info["backup"] or ""
            updated = inspect_schema(config.db_path)
            if not migrations_applied and updated["version"] != report["version"]:
                schema_note = f"adopted existing index at schema v{updated['version']}"
            report = updated
        if report["too_new"]:
            detail = (
                f"v{report['version']} is newer than v{report['target']}; "
                "upgrade thread-to-skill"
            )
            checks.append(("schema", False, detail))
        elif report["needs_upgrade"]:
            detail = f"v{report['version']} -> v{report['target']}; run `t2s doctor --migrate`"
            checks.append(("schema", False, detail))
        else:
            checks.append(("schema", True, f"v{report['version']}"))
    checks.append(("t2s on PATH", shutil.which("t2s") is not None, shutil.which("t2s") or "not found"))
    for env_name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        checks.append((env_name, bool(os.environ.get(env_name)), "set" if os.environ.get(env_name) else "not set"))
    if config:
        skills = len([child for child in config.skills_dir.iterdir() if child.is_dir()]) if config.skills_dir.exists() else 0
        checks.append(("skills", True, str(skills)))
    if args.json:
        _print(json.dumps([{"check": name, "ok": ok, "detail": detail} for name, ok, detail in checks], indent=2))
        return 0
    for name, ok, detail in checks:
        marker = green("ok") if ok else yellow("warn")
        _print(f"{marker:<6} {name:<18} {detail}")
    if migrations_applied or backup_path or schema_note:
        _print("")
    if migrations_applied:
        versions = ", ".join(f"v{version}" for version in migrations_applied)
        _print(f"applied migrations: {versions}")
    if schema_note:
        _print(schema_note)
    if backup_path:
        _print(f"backup: {backup_path}")
    _print("")
    _print(dim("Run `t2s validate --all` to check generated skills."))
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    _print(f"thread-to-skill {__version__}")
    _print("https://github.com/fuleinist/thread-to-skill")
    return 0


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="t2s",
        description="Turn Slack, Discord, and WhatsApp threads into reusable agent skills.",
    )
    parser.add_argument("--home", help="Workspace directory (default: ./.thread2skill or T2S_HOME)")
    parser.add_argument(
        "--wait",
        type=float,
        metavar="SECONDS",
        default=DEFAULT_WAIT_SECONDS,
        help="Seconds to wait for the workspace write lock before failing. Read-only commands never wait.",
    )
    parser.add_argument("--version", action="version", version=f"thread-to-skill {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create a local workspace")
    init.add_argument("--provider", default="auto", choices=["auto", "offline", "openai", "anthropic", "ollama", "command", "manual"])
    init.add_argument("--no-redact", action="store_true", help="Disable built-in redaction")
    init.add_argument("--force", action="store_true", help="Update an existing workspace's settings")
    init.set_defaults(func=cmd_init)

    ingest = sub.add_parser("ingest", help="Load conversation exports into the local index")
    ingest.add_argument("paths", nargs="+", help="Files, directories, or ZIP exports")
    ingest.add_argument("--format", default="auto", choices=["auto", "slack", "discord", "whatsapp", "telegram", "jsonl", "json", "markdown", "text", "html"])
    ingest.add_argument("--gap-minutes", type=int, default=0, help="Split unthreaded messages on this quiet gap")
    ingest.add_argument("--channel", default="", help="Override the channel name")
    ingest.add_argument("--title", default="", help="Override the thread title")
    ingest.add_argument("--force", action="store_true", help="Rewrite every thread even when it did not change")
    ingest.add_argument("--json", action="store_true")
    ingest.set_defaults(func=cmd_ingest)

    distill = sub.add_parser("distill", help="Turn thread(s) into a skill")
    distill.add_argument("threads", nargs="+", help="Thread IDs, prefixes, or 'latest'")
    distill.add_argument("--provider", default="", choices=["", "auto", "offline", "openai", "openai-compatible", "anthropic", "ollama", "command", "manual"])
    distill.add_argument("--model", default="")
    distill.add_argument("--base-url", default="")
    distill.add_argument("--api-key-env", default="")
    distill.add_argument("--command", default="", help="Command provider: receives the prompt on stdin")
    distill.add_argument("--timeout", type=int, default=120)
    distill.add_argument("--category", default="", choices=[""] + all_category_keys())
    distill.add_argument("--tags", default="", help="Comma-separated extra tags")
    distill.add_argument("--slug", default="")
    distill.add_argument("--since", default="", help="Keep messages dated on or after YYYY-MM-DD")
    distill.add_argument("--until", default="", help="Keep messages dated on or before YYYY-MM-DD")
    distill.add_argument(
        "--participants",
        default="",
        help="Comma-separated authors; case-insensitive substring match",
    )
    distill.add_argument(
        "--max-messages",
        type=int,
        default=0,
        help="Keep the most recent N messages across the selected threads",
    )
    distill.add_argument("--exclude-bots", action="store_true", help="Drop messages from automation accounts")
    distill.add_argument("--on-duplicate", default="warn", choices=["warn", "skip", "merge", "new"])
    distill.add_argument(
        "--synthesize",
        action="store_true",
        help="Cluster related threads, merge them into one procedure, and report conflicts",
    )
    distill.add_argument("--install", default="", help="Comma-separated harnesses, or 'all'")
    distill.add_argument("--scope", default="user", choices=["user", "project"])
    distill.add_argument("--project-root", default="")
    distill.add_argument("--force", action="store_true")
    distill.add_argument("--dry-run", action="store_true")
    distill.add_argument("--json", action="store_true")
    distill.add_argument(
        "--max-cost",
        type=float,
        default=None,
        help="Stop before the request when the prompt estimate is above this many USD",
    )
    distill.set_defaults(func=cmd_distill)

    listing = sub.add_parser("list", help="List skills")
    listing.add_argument("--category", default="")
    listing.add_argument("--tag", default="")
    listing.add_argument("--status", default="")
    listing.add_argument("--limit", type=int, default=200)
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="Show a skill")
    show.add_argument("slug")
    show.add_argument("--raw", action="store_true", help="Print SKILL.md")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_show)

    diff = sub.add_parser("diff", help="Compare two stored versions of a skill")
    diff.add_argument("slug", help="Skill slug or unique prefix")
    diff.add_argument(
        "--from",
        dest="from_version",
        type=int,
        default=None,
        help="Older version to read (default: the previous stored snapshot)",
    )
    diff.add_argument(
        "--to",
        dest="to_version",
        type=int,
        default=None,
        help="Newer version to read (default: the current bundle)",
    )
    diff.add_argument("--stat", action="store_true", help="Print per-section added and removed counts")
    diff.add_argument("--json", action="store_true", help="Machine-readable output")
    diff.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_SNAPSHOT_KEEP,
        help=f"Prune to the newest N snapshots (default {DEFAULT_SNAPSHOT_KEEP})",
    )
    diff.set_defaults(func=cmd_diff)

    edit = sub.add_parser("edit", help="Amend a skill by hand, then re-index it")
    edit.add_argument("slug", help="Skill slug or unique prefix")
    edit.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Set a frontmatter field without an editor (name, description, category, tags, triggers, confidence)",
    )
    edit.add_argument("--tag", action="append", default=[], metavar="VALUE", help="Add a tag without an editor")
    edit.add_argument("--no-version-bump", action="store_true", help="Keep the current version, for typo fixes")
    edit.set_defaults(func=cmd_edit)

    archive = sub.add_parser("archive", help="Archive a skill but keep its files")
    archive.add_argument("slug")
    archive.add_argument("--json", action="store_true")
    archive.set_defaults(func=cmd_archive)

    unarchive = sub.add_parser("unarchive", help="Restore an archived skill to active")
    unarchive.add_argument("slug")
    unarchive.add_argument("--json", action="store_true")
    unarchive.set_defaults(func=cmd_unarchive)

    delete = sub.add_parser("delete", help="Move a skill bundle to the workspace trash and remove its row")
    delete.add_argument("slug")
    delete.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    delete.add_argument("--keep-thread", action="store_true", help="Keep the source threads and provenance")
    delete.add_argument("--json", action="store_true")
    delete.set_defaults(func=cmd_delete)

    search = sub.add_parser("search", help="Full-text search over local skills")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--json", action="store_true")
    search.set_defaults(func=cmd_search)

    suggest = sub.add_parser("suggest", help="Suggest skills for a prompt (used by hooks)")
    suggest.add_argument("prompt", nargs="*")
    suggest.add_argument("--format", default="text", choices=["text", "json", "claude", "cursor", "deepseek"])
    suggest.add_argument("--threshold", type=float, default=0.18)
    suggest.add_argument("--limit", type=int, default=3)
    suggest.add_argument("--category", default="")
    suggest.set_defaults(func=cmd_suggest)

    feedback = sub.add_parser("feedback", help="Record whether a skill worked")
    feedback.add_argument("slug")
    feedback.add_argument("--outcome", required=True, choices=["worked", "failed", "accepted", "suggested"])
    feedback.add_argument("--note", default="")
    feedback.add_argument("--json", action="store_true")
    feedback.set_defaults(func=cmd_feedback)

    install = sub.add_parser("install", help="Install a skill into harness skill roots")
    install.add_argument("slug")
    install.add_argument("--harness", default="agents", help="Comma-separated harnesses, or 'all'")
    install.add_argument("--scope", default="user", choices=["user", "project"])
    install.add_argument("--project-root", default="")
    install.add_argument("--force", action="store_true")
    install.add_argument("--json", action="store_true")
    install.set_defaults(func=cmd_install)

    hooks = sub.add_parser("hooks", help="Install or check proactive suggestion hooks")
    hooks_actions = hooks.add_subparsers(dest="action", required=True)

    hooks_install = hooks_actions.add_parser("install", help="Write proactive-suggestion hooks")
    hooks_install.add_argument("--harness", default="all", help="Comma-separated harnesses, or 'all'")
    hooks_install.add_argument("--scope", default="user", choices=["user", "project"])
    hooks_install.add_argument("--project-root", default="")
    hooks_install.add_argument("--dry-run", action="store_true")
    hooks_install.add_argument("--json", action="store_true")

    hooks_doctor = hooks_actions.add_parser("doctor", help="Check installed hooks without running the host")
    hooks_doctor.add_argument("--harness", default="all", help="Comma-separated harnesses, or 'all'")
    hooks_doctor.add_argument("--scope", default="user", choices=["user", "project"])
    hooks_doctor.add_argument("--project-root", default="")
    hooks_doctor.add_argument("--prompt", default=DOCTOR_SAMPLE_PROMPT, help="Sample prompt for the single `suggest` call")
    hooks_doctor.add_argument("--timeout", type=float, default=DOCTOR_TIMEOUT_SECONDS, help="Seconds before the sample call counts as failed")
    hooks_doctor.add_argument("--json", action="store_true")

    hooks.set_defaults(func=cmd_hooks)

    validate = sub.add_parser("validate", help="Validate generated skills")
    validate.add_argument("slug", nargs="?")
    validate.add_argument("--all", action="store_true")
    validate.set_defaults(func=cmd_validate)

    stats = sub.add_parser("stats", help="Show workspace statistics")
    stats.add_argument("--json", action="store_true")
    stats.add_argument("--by-source", action="store_true", help="Show a table of threads and skills per source")
    stats.add_argument("--by-channel", action="store_true", help="Show the top channels with skills")
    stats.add_argument("--timeline", action="store_true", help="Show activity for the last 12 ISO weeks")
    stats.add_argument(
        "--cost", action="store_true", help="Show recorded token usage and the estimated cost per model"
    )
    stats.set_defaults(func=cmd_stats)

    review = sub.add_parser("review", help="Find stale, failing, unused, duplicate, and conflicting skills")
    review.add_argument("--stale-days", type=int, default=None, help="Days without use before a skill is stale (default 90)")
    review.add_argument("--confidence", type=float, default=None, help="Low-confidence threshold (default 0.4)")
    review.add_argument("--duplicate-threshold", type=float, default=None, help="Similarity for a duplicate pair (default 0.55)")
    review.add_argument("--conflict-threshold", type=float, default=None, help="Similarity for a conflict candidate (default 0.45)")
    review.add_argument("--fix", action="store_true", help="Archive needs-review skills with zero successes")
    review.add_argument("--yes", action="store_true", help="With --fix, also record duplicate groups in metadata")
    review.add_argument("--json", action="store_true")
    review.set_defaults(func=cmd_review)

    doctor = sub.add_parser("doctor", help="Check the local environment")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--migrate", action="store_true", help="Apply pending database migrations")
    doctor.set_defaults(func=cmd_doctor)

    version = sub.add_parser("version", help="Print the version")
    version.set_defaults(func=cmd_version)

    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if not _takes_lock(args):
            return int(args.func(args))
        lock_file = _lock_path(args)
        handle = acquire(lock_file, float(args.wait))
        try:
            return int(args.func(args))
        finally:
            release(lock_file, handle)
    except LockError as exc:
        _print(red(f"error: {exc}"))
        return 1
    except (ConfigError, DistillError, DuplicateSkill, ProviderError, StoreError, FileNotFoundError, ValueError, KeyError) as exc:
        _print(red(f"error: {exc}"))
        return 1
    except KeyboardInterrupt:
        _print("cancelled")
        return 130
    finally:
        _close_stores()


if __name__ == "__main__":
    raise SystemExit(main())