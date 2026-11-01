"""Install generated skills into the native locations used by agent harnesses."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .models import Skill
from .taxonomy import category_root
from .util import ensure_dir

DEFAULT_HARNESSES = ["agents", "claude", "cursor", "openclaw", "hermes", "deepseek"]
ALL_HARNESSES = ["agents", "claude", "cursor", "openclaw", "hermes", "deepseek", "codex", "copilot"]
MANAGED_MARKER = ".thread2skill-install.json"


@dataclass
class InstallResult:
    harness: str
    path: Path
    status: str
    detail: str = ""

    def line(self) -> str:
        label = f"{self.status:<8}"
        return f"{label} {self.harness:<10} {self.path}"


def _home() -> Path:
    return Path.home()


def _root(scope: str, project_root: Path | None) -> Path:
    return Path(project_root or Path.cwd()) if scope == "project" else _home()


def harness_skill_dir(harness: str, scope: str, category: str, project_root: Path | None = None) -> Path:
    root = _root(scope, project_root)
    if harness == "agents":
        return root / ".agents" / "skills" if scope == "project" else root / ".agents" / "skills"
    if harness == "claude":
        return root / ".claude" / "skills"
    if harness == "openclaw":
        if scope == "project":
            return root / "skills"
        state = Path(os.environ.get("OPENCLAW_STATE_DIR", str(root / ".openclaw"))).expanduser()
        return state / "skills"
    if harness == "hermes":
        if scope == "project":
            return root / ".hermes" / "skills" / category_root(category)
        hermes_home = Path(os.environ.get("HERMES_HOME", str(root / ".hermes"))).expanduser()
        return hermes_home / "skills" / category_root(category)
    if harness == "deepseek":
        if scope == "project":
            return root / ".dsh" / "skills"
        dsh_home = Path(os.environ.get("DSH_HOME", str(root / ".dsh"))).expanduser()
        return dsh_home / "skills"
    if harness == "codex":
        return root / ".codex" / "skills"
    if harness == "copilot":
        return root / ".copilot" / "skills"
    if harness == "cursor":
        return root / ".cursor" / "rules"
    raise ValueError(f"Unknown harness: {harness}")


def _copy_skill(source: Path, destination: Path, *, force: bool) -> str:
    if destination.exists():
        marker = destination / MANAGED_MARKER
        if not force and not marker.exists():
            return "skipped"
        if destination.is_dir():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    ensure_dir(destination.parent)
    shutil.copytree(source, destination)
    return "installed"


def _write_marker(destination: Path, skill: Skill, harness: str) -> None:
    (destination / MANAGED_MARKER).write_text(
        json.dumps(
            {
                "managed_by": "thread-to-skill",
                "slug": skill.slug,
                "version": skill.version,
                "harness": harness,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _cursor_rule(skill: Skill, destination_dir: Path, *, force: bool) -> InstallResult:
    source = Path(skill.path) / "SKILL.md"
    content = source.read_text(encoding="utf-8")
    if content.startswith("---") and "\n---" in content[3:]:
        _, frontmatter, body = content.split("---", 2)
    else:
        frontmatter, body = "", content
    description = skill.description.replace("\n", " ").strip()
    rule = (
        "---\n"
        f"description: {json.dumps(description)}\n"
        "globs:\n"
        "alwaysApply: false\n"
        "---\n\n"
        f"<!-- Managed by thread-to-skill. Source: {skill.slug} v{skill.version}. -->\n\n"
        + body.lstrip()
    )
    destination_dir = ensure_dir(destination_dir)
    destination = destination_dir / f"{skill.slug}.mdc"
    if destination.exists() and not force:
        return InstallResult("cursor", destination, "skipped", "rule exists; use --force")
    destination.write_text(rule, encoding="utf-8", newline="\n")
    return InstallResult("cursor", destination, "installed", "Cursor project/user rule")


def install_skill(
    skill: Skill,
    harness: str,
    *,
    scope: str = "user",
    project_root: Path | None = None,
    force: bool = False,
) -> InstallResult:
    source = Path(skill.path)
    if not source.is_dir() or not (source / "SKILL.md").exists():
        raise FileNotFoundError(f"Skill bundle missing: {source}")
    category = skill.category
    destination_dir = harness_skill_dir(harness, scope, category, project_root)
    if harness == "cursor":
        return _cursor_rule(skill, destination_dir, force=force)
    destination = destination_dir / skill.slug
    status = _copy_skill(source, destination, force=force)
    if status == "installed":
        _write_marker(destination, skill, harness)
    return InstallResult(harness, destination, status, "" if status == "installed" else "already exists; use --force")


def install_many(
    skill: Skill,
    harnesses: list[str],
    *,
    scope: str = "user",
    project_root: Path | None = None,
    force: bool = False,
) -> list[InstallResult]:
    results: list[InstallResult] = []
    for harness in harnesses:
        try:
            results.append(install_skill(skill, harness, scope=scope, project_root=project_root, force=force))
        except Exception as exc:  # keep one harness failure from aborting the rest
            results.append(InstallResult(harness, Path(""), "error", str(exc)))
    return results