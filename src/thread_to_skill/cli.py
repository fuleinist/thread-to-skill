"""Command-line interface for thread-to-skill."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .adapters import load_path
from .config import Config, ConfigError, find_home
from .distill import DistillError, DuplicateSkill, ManualPromptReady, distill_threads, save_skill
from .feedback import apply_feedback, feedback_summary
from .hooks import ALL_HOOK_HARNESSES, install_hooks
from .install import ALL_HARNESSES, DEFAULT_HARNESSES, install_many
from .models import Thread
from .providers import ProviderError, get_provider
from .redact import redact_thread
from .search import format_suggestions, suggest_skills
from .store import Store
from .taxonomy import all_category_keys, category_label
from .util import atomic_write_json, human_number, now_iso, safe_console_text, truncate, unique_slug
from .validate import validate_config_skills, validate_skill_dir


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


def cmd_ingest(args: argparse.Namespace) -> int:
    config, store = _open(args)
    total_threads = 0
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
            atomic_write_json(_normalized_path(config, thread.id), thread.to_dict())
            store.add_thread(thread)
            total_threads += 1
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
                }
            )
    if args.json:
        _print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    rows = [
        [item["id"], item["source"], item["channel"], str(item["messages"]), truncate(item["title"], 52)]
        for item in results
    ]
    _print(_table(["THREAD", "SOURCE", "CHANNEL", "MSGS", "TITLE"], rows))
    _print("")
    redactions = {
        item["redactions"]
        for item in results
        if item["redactions"] not in {"disabled", "none", "no sensitive data detected"}
    }
    _print(
        f"{green('ingested')} {human_number(total_threads)} thread(s) · "
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
        _print("")
        _print(f"Read {dim(str(Path(skill.path) / 'SKILL.md'))} or use --raw to print it.")
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
    _print(f"{bold('Workspace')} {config.home}")
    _print(f"  threads {stats['threads']} · skills {stats['skills']}")
    if stats["by_status"]:
        _print("  status  " + ", ".join(f"{key}={value}" for key, value in stats["by_status"].items()))
    if stats["by_category"]:
        _print("")
        _print(bold("By category"))
        rows = [[category, category_label(category), str(count)] for category, count in stats["by_category"].items()]
        _print(_table(["CATEGORY", "LABEL", "SKILLS"], rows))
    recent = store.list_events(limit=8)
    if recent:
        _print("")
        _print(bold("Recent events"))
        for event in recent:
            suffix = f" · {event['skill_slug']}" if event["skill_slug"] else ""
            _print(f"  {event['ts']}  {event['kind']}{suffix}")
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
    parser.add_argument("--version", action="version", version=f"thread-to-skill {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create a local workspace")
    init.add_argument("--provider", default="auto", choices=["auto", "offline", "openai", "anthropic", "ollama", "command", "manual"])
    init.add_argument("--no-redact", action="store_true", help="Disable built-in redaction")
    init.add_argument("--force", action="store_true", help="Update an existing workspace's settings")
    init.set_defaults(func=cmd_init)

    ingest = sub.add_parser("ingest", help="Load conversation exports into the local index")
    ingest.add_argument("paths", nargs="+", help="Files, directories, or ZIP exports")
    ingest.add_argument("--format", default="auto", choices=["auto", "slack", "discord", "whatsapp", "telegram", "jsonl", "json", "markdown", "text"])
    ingest.add_argument("--gap-minutes", type=int, default=0, help="Split unthreaded messages on this quiet gap")
    ingest.add_argument("--channel", default="", help="Override the channel name")
    ingest.add_argument("--title", default="", help="Override the thread title")
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
    distill.add_argument("--on-duplicate", default="warn", choices=["warn", "skip", "merge", "new"])
    distill.add_argument("--install", default="", help="Comma-separated harnesses, or 'all'")
    distill.add_argument("--scope", default="user", choices=["user", "project"])
    distill.add_argument("--project-root", default="")
    distill.add_argument("--force", action="store_true")
    distill.add_argument("--dry-run", action="store_true")
    distill.add_argument("--json", action="store_true")
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

    hooks = sub.add_parser("hooks", help="Install proactive suggestion hooks")
    hooks.add_argument("action", choices=["install"])
    hooks.add_argument("--harness", default="all", help="Comma-separated harnesses, or 'all'")
    hooks.add_argument("--scope", default="user", choices=["user", "project"])
    hooks.add_argument("--project-root", default="")
    hooks.add_argument("--dry-run", action="store_true")
    hooks.add_argument("--json", action="store_true")
    hooks.set_defaults(func=cmd_hooks)

    validate = sub.add_parser("validate", help="Validate generated skills")
    validate.add_argument("slug", nargs="?")
    validate.add_argument("--all", action="store_true")
    validate.set_defaults(func=cmd_validate)

    stats = sub.add_parser("stats", help="Show workspace statistics")
    stats.add_argument("--json", action="store_true")
    stats.set_defaults(func=cmd_stats)

    doctor = sub.add_parser("doctor", help="Check the local environment")
    doctor.add_argument("--json", action="store_true")
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
        return int(args.func(args))
    except (ConfigError, DistillError, DuplicateSkill, ProviderError, FileNotFoundError, ValueError, KeyError) as exc:
        _print(red(f"error: {exc}"))
        return 1
    except KeyboardInterrupt:
        _print("cancelled")
        return 130
    finally:
        _close_stores()


if __name__ == "__main__":
    raise SystemExit(main())