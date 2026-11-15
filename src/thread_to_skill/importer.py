"""Adopt existing Agent Skills into the local workspace.

The importer reads a hand-written skill directory, validates it, fills the
fields that the generator would normally supply, and indexes the result so
`list`, `suggest` and `feedback` can see it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .models import Skill
from .taxonomy import normalize_category, normalize_tags, score_category
from .util import ensure_dir, keyword_counts, now_iso
from .validate import parse_frontmatter, validate_skill_dir

IMPORTED_SOURCE = "imported"
IMPORT_RENDERER = "thread-to-skill/import"
MAX_IMPORTED_TAGS = 8


class SkillImportError(RuntimeError):
    """Raised when a path cannot be imported."""


def discover_skill_dirs(path: Path | str) -> list[Path]:
    """Return every directory under path that holds a SKILL.md."""
    root = Path(path).expanduser().resolve()
    if not root.exists():
        raise SkillImportError(f"{root}: path does not exist")
    if root.is_file():
        raise SkillImportError(f"{root}: expected a directory, not a file")
    if (root / "SKILL.md").is_file():
        return [root]
    found = sorted(
        child for child in root.iterdir() if child.is_dir() and (child / "SKILL.md").is_file()
    )
    if not found:
        raise SkillImportError(f"{root}: no SKILL.md in this directory or its child directories")
    return found


def read_metadata(skill_dir: Path | str) -> dict[str, Any]:
    """Read metadata.json when it exists and holds a JSON object."""
    path = Path(skill_dir) / "metadata.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def is_imported(skill_dir: Path | str) -> bool:
    return read_metadata(skill_dir).get("source") == IMPORTED_SOURCE


def _title_from_body(body: str, fallback: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or fallback
    return fallback


def build_metadata_stub(skill_dir: Path | str) -> dict[str, Any]:
    """Build a metadata.json stub from SKILL.md. Never mutates the directory."""
    root = Path(skill_dir)
    frontmatter, body = parse_frontmatter((root / "SKILL.md").read_text(encoding="utf-8"))
    previous = read_metadata(root)
    data = previous.get("data") if isinstance(previous.get("data"), dict) else {}
    data = dict(data or {})
    declared = frontmatter.get("metadata") if isinstance(frontmatter.get("metadata"), dict) else {}

    slug = str(frontmatter.get("name") or root.name)
    description = str(frontmatter.get("description") or "")
    category = str(declared.get("category") or data.get("category") or "").strip()
    category_inferred = False
    if not category:
        category, _scores = score_category(f"{slug} {description}\n{body}")
        category_inferred = True

    tags = [str(item) for item in (declared.get("tags") or data.get("tags") or []) if str(item).strip()]
    tags_inferred = False
    if not tags:
        tags = [word for word, _count in keyword_counts(body, limit=MAX_IMPORTED_TAGS)]
        tags_inferred = True

    triggers = data.get("triggers") or declared.get("triggers") or []
    stub = dict(previous)
    stub.update(
        {
            "schema_version": 1,
            "slug": slug,
            "renderer": IMPORT_RENDERER,
            "source": IMPORTED_SOURCE,
            "version": int(previous.get("version") or 1),
            "created_at": str(previous.get("created_at") or now_iso()),
            "updated_at": now_iso(),
            "threads": [],
        }
    )
    stub["data"] = {
        **data,
        "title": str(data.get("title") or _title_from_body(body, slug)),
        "summary": str(data.get("summary") or description),
        "category": normalize_category(category),
        "tags": normalize_tags(tags),
        "triggers": [str(item) for item in triggers],
        "confidence": data.get("confidence", declared.get("confidence")),
        "imported": {
            "category_inferred": category_inferred,
            "tags_inferred": tags_inferred,
        },
    }
    return stub


def _skill_from_stub(
    slug: str,
    path: Path,
    frontmatter: dict[str, Any],
    body: str,
    stub: dict[str, Any],
    previous: Skill | None,
) -> Skill:
    data = stub.get("data") if isinstance(stub.get("data"), dict) else {}
    raw_confidence = (data or {}).get("confidence")
    try:
        confidence = 0.0 if raw_confidence is None else max(0.0, min(1.0, float(raw_confidence)))
    except (TypeError, ValueError):
        confidence = 0.0
    return Skill(
        slug=slug,
        name=str(frontmatter.get("name") or slug),
        title=str((data or {}).get("title") or slug),
        description=str(frontmatter.get("description") or ""),
        summary=str((data or {}).get("summary") or ""),
        category=normalize_category(str((data or {}).get("category") or "")),
        tags=normalize_tags([str(item) for item in ((data or {}).get("tags") or [])]),
        triggers=[str(item) for item in ((data or {}).get("triggers") or [])],
        version=int(stub.get("version") or 1),
        confidence=confidence,
        status=previous.status if previous else "active",
        path=str(path),
        source_threads=[],
        created_at=str(stub.get("created_at") or now_iso()),
        updated_at=str(stub.get("updated_at") or now_iso()),
        suggested=previous.suggested if previous else 0,
        accepted=previous.accepted if previous else 0,
        worked=previous.worked if previous else 0,
        failed=previous.failed if previous else 0,
        body=body,
        metadata=stub,
    )


def import_skills(
    config: Any,
    store: Any,
    path: Path | str,
    *,
    link: bool = False,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Adopt one skill directory, or every skill directory under a parent."""
    results: list[dict[str, Any]] = []
    for skill_dir in discover_skill_dirs(path):
        results.append(
            _import_one(
                config,
                store,
                skill_dir,
                link=link,
                dry_run=dry_run,
                force=force,
            )
        )
    imported = [row["slug"] for row in results if row["status"] in {"imported", "linked", "unchanged"}]
    rejected = [row for row in results if row["status"] in {"rejected", "conflict"}]
    return {"imported": imported, "results": results, "errors": rejected}


def _import_one(
    config: Any,
    store: Any,
    skill_dir: Path,
    *,
    link: bool,
    dry_run: bool,
    force: bool,
) -> dict[str, Any]:
    issues = validate_skill_dir(skill_dir, imported=True)
    errors = [issue.message for issue in issues if issue.level == "error"]
    if errors:
        return {"slug": skill_dir.name, "status": "rejected", "errors": errors}

    frontmatter, body = parse_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
    slug = str(frontmatter.get("name") or skill_dir.name)
    stub = build_metadata_stub(skill_dir)
    existing = store.get_skill(slug)

    if link:
        if dry_run:
            return {
                "slug": slug,
                "status": "would-link",
                "path": str(skill_dir),
                "category": stub["data"]["category"],
                "tags": stub["data"]["tags"],
            }
        stub["external_path"] = str(skill_dir)
        skill = _skill_from_stub(slug, skill_dir, frontmatter, body, stub, existing)
        store.save_skill(skill)
        return {"slug": slug, "status": "linked", "path": str(skill_dir)}

    tools_dir = Path(config.skills_dir)
    target = tools_dir / slug
    same_dir = target.resolve() == skill_dir.resolve()
    if same_dir:
        return {"slug": slug, "status": "unchanged", "path": str(target)}
    already_imported = is_imported(target) or is_imported(skill_dir)
    if target.exists() and not already_imported and not force:
        return {
            "slug": slug,
            "status": "conflict",
            "errors": [f"{target} already exists; pass --force to replace it"],
        }
    if dry_run:
        return {
            "slug": slug,
            "status": "would-import",
            "path": str(target),
            "category": stub["data"]["category"],
            "tags": stub["data"]["tags"],
            "replace": target.exists(),
        }

    ensure_dir(tools_dir)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(skill_dir, target)
    (target / "metadata.json").write_text(
        json.dumps(stub, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    skill = _skill_from_stub(slug, target, frontmatter, body, stub, existing)
    store.save_skill(skill)
    return {"slug": slug, "status": "imported", "path": str(target), "replace": bool(existing)}