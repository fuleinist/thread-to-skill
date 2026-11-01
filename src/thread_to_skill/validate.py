"""Validate generated skills against the Agent Skills format and local rules."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .redact import redact_text

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REQUIRED_SECTIONS = ("## When to use", "## Procedure")
MAX_SKILL_BYTES = 64_000


@dataclass
class Issue:
    level: str
    message: str

    def line(self) -> str:
        return f"{self.level:<7} {self.message}"


def _parse_scalar(raw: str):
    raw = raw.strip()
    if not raw:
        return ""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip('"').strip("'")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if text.startswith("\ufeff"):
        text = text[1:]
    if not text.startswith("---"):
        raise ValueError("SKILL.md must start with YAML frontmatter (---)")
    lines = text.splitlines()
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        raise ValueError("SKILL.md frontmatter is not closed with ---")
    data: dict = {}
    current: dict | None = None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  ") and current is not None:
            key, _, value = line.strip().partition(":")
            current[key.strip()] = _parse_scalar(value)
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not value:
            current = {}
            data[key] = current
        else:
            current = None
            data[key] = _parse_scalar(value)
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return data, body


def validate_skill_dir(path: Path | str) -> list[Issue]:
    skill_dir = Path(path)
    issues: list[Issue] = []
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return [Issue("error", f"{skill_dir}: missing SKILL.md")]
    text = skill_md.read_text(encoding="utf-8")
    if len(text.encode("utf-8")) > MAX_SKILL_BYTES:
        issues.append(Issue("warning", f"SKILL.md is larger than {MAX_SKILL_BYTES // 1000} KB"))
    try:
        frontmatter, body = parse_frontmatter(text)
    except ValueError as exc:
        return [Issue("error", str(exc))]

    name = str(frontmatter.get("name") or "")
    description = str(frontmatter.get("description") or "")
    if not name:
        issues.append(Issue("error", "frontmatter.name is required"))
    elif len(name) > 64:
        issues.append(Issue("error", f"frontmatter.name is {len(name)} chars; max 64"))
    elif not NAME_RE.match(name):
        issues.append(Issue("error", "frontmatter.name must be lowercase letters, numbers, and single hyphens"))
    if name and name != skill_dir.name:
        issues.append(Issue("error", f"frontmatter.name ({name}) does not match directory ({skill_dir.name})"))
    if not description:
        issues.append(Issue("error", "frontmatter.description is required"))
    elif len(description) > 1024:
        issues.append(Issue("error", f"frontmatter.description is {len(description)} chars; max 1024"))
    if not body.strip():
        issues.append(Issue("error", "SKILL.md body is empty"))
    for section in REQUIRED_SECTIONS:
        if section.lower() not in body.lower():
            issues.append(Issue("warning", f"missing recommended section: {section}"))

    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, dict):
        issues.append(Issue("warning", "frontmatter.metadata is missing; category and tags are unavailable"))
    else:
        category = str(metadata.get("category") or "")
        if not category:
            issues.append(Issue("warning", "metadata.category is missing"))
        tags = metadata.get("tags")
        if not isinstance(tags, list):
            issues.append(Issue("warning", "metadata.tags should be a list"))
        confidence = metadata.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
            issues.append(Issue("warning", "metadata.confidence should be a number between 0 and 1"))

    reference = skill_dir / "references" / "thread.md"
    if not reference.exists():
        issues.append(Issue("warning", "references/thread.md is missing; provenance is incomplete"))

    meta_path = skill_dir / "metadata.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            issues.append(Issue("error", f"metadata.json is invalid JSON: {exc}"))
            meta = {}
        if isinstance(meta, dict) and meta.get("slug") not in (None, name):
            issues.append(Issue("error", "metadata.json slug does not match frontmatter.name"))
    else:
        issues.append(Issue("warning", "metadata.json is missing"))

    _, report = redact_text(body)
    if report.total:
        issues.append(Issue("warning", f"possible unredacted sensitive data in SKILL.md: {report.summary()}"))

    if not any(issue.level == "error" for issue in issues):
        issues.append(Issue("ok", f"{name}: valid Agent Skill"))
    return issues


def validate_config_skills(skills_dir: Path) -> dict[str, list[Issue]]:
    results: dict[str, list[Issue]] = {}
    if not Path(skills_dir).is_dir():
        return results
    for child in sorted(Path(skills_dir).iterdir()):
        if child.is_dir() and (child / "SKILL.md").exists():
            results[child.name] = validate_skill_dir(child)
    return results