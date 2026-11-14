"""Portable skill bundles: deterministic export and verified import.

A bundle is one gzipped tar with a manifest and one directory per skill:

    manifest.json
    skills/<slug>/SKILL.md
    skills/<slug>/metadata.json
    skills/<slug>/references/thread.md    (only with include_references)

Two exports of the same library are byte-identical. Member order, member metadata,
and the gzip header are all fixed, and the manifest carries no timestamp. A
timestamp would make two exports of an unchanged library differ.

`references/` holds raw conversation transcripts, so it stays out of a bundle
unless the caller asks for it. `.versions/` holds local snapshots and never ships.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .models import Skill
from .taxonomy import normalize_category, normalize_tags
from .util import clamp, ensure_dir, now_iso, read_json
from .validate import parse_frontmatter, validate_skill_dir

BUNDLE_SCHEMA = "t2s-bundle-v1"
BUNDLE_TOOL = "thread-to-skill"
MANIFEST_NAME = "manifest.json"
SKILLS_PREFIX = "skills"
REFERENCES_DIRNAME = "references"
EXCLUDED_DIRS = frozenset({".versions"})
FIXED_MTIME = 0
FILE_MODE = 0o644
COMPRESS_LEVEL = 9
DEFAULT_CONFIDENCE = 0.5
MAX_TRIGGERS = 12


class BundleError(RuntimeError):
    """Raised when a bundle cannot be written, verified, or installed."""


# --------------------------------------------------------------------- helpers


def sha256_bytes(data: bytes) -> str:
    """The digest a manifest stores for one member."""
    return hashlib.sha256(data).hexdigest()


def is_safe_member_name(name: str) -> bool:
    """True for a relative POSIX path that stays inside the bundle root."""
    if not name or "\x00" in name:
        return False
    if name.startswith("/") or name.startswith("\\"):
        return False
    if "\\" in name:
        return False
    if ":" in name.split("/")[0]:
        return False
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    return True


def _skill_members(skill_dir: Path, *, include_references: bool) -> list[tuple[str, bytes]]:
    """Every shippable file in one skill directory, as POSIX relative paths."""
    root = Path(skill_dir)
    members: list[tuple[str, bytes]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if any(part in EXCLUDED_DIRS for part in parts):
            continue
        if not include_references and parts and parts[0] == REFERENCES_DIRNAME:
            continue
        members.append((PurePosixPath(*parts).as_posix(), path.read_bytes()))
    members.sort(key=lambda item: item[0])
    return members


# ---------------------------------------------------------------------- export


def build_manifest(
    skills: Iterable[Skill],
    *,
    include_references: bool,
    tool_version: str,
) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    """Return the manifest and the member list for one set of skills.

    Skills arrive in slug order and files in path order, so the result does not
    depend on directory iteration order on any platform.
    """
    rows: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    members: list[tuple[str, bytes]] = []
    for skill in sorted(skills, key=lambda item: item.slug):
        skill_dir = Path(skill.path)
        if not (skill_dir / "SKILL.md").exists():
            raise BundleError(
                f"{skill.slug}: {skill_dir} has no SKILL.md. Run `t2s distill` or delete the row."
            )
        shipped = _skill_members(skill_dir, include_references=include_references)
        if not shipped:
            raise BundleError(f"{skill.slug}: {skill_dir} holds no files to export")
        for relative, data in shipped:
            member = f"{SKILLS_PREFIX}/{skill.slug}/{relative}"
            members.append((member, data))
            files.append({"path": member, "sha256": sha256_bytes(data), "size": len(data)})
        rows.append(
            {
                "slug": skill.slug,
                "name": skill.name,
                "version": int(skill.version),
                "category": skill.category,
                "status": skill.status,
                "confidence": round(float(skill.confidence), 3),
                "updated_at": skill.updated_at,
            }
        )
    if not rows:
        raise BundleError("no skills to export")
    members.sort(key=lambda item: item[0])
    files.sort(key=lambda item: item["path"])
    manifest = {
        "schema": BUNDLE_SCHEMA,
        "tool": BUNDLE_TOOL,
        "tool_version": tool_version,
        "skills": rows,
        "files": files,
    }
    return manifest, members


def manifest_bytes(manifest: dict[str, Any]) -> bytes:
    """The manifest as stable UTF-8 JSON with a trailing newline."""
    return (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_bundle(path: Path, members: list[tuple[str, bytes]]) -> None:
    """Write one deterministic gzipped tar to `path`."""
    target = Path(path)
    buffer = io.BytesIO()
    with gzip.GzipFile(
        filename="", mode="wb", fileobj=buffer, mtime=FIXED_MTIME, compresslevel=COMPRESS_LEVEL
    ) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT) as archive:
            for name, data in members:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mtime = FIXED_MTIME
                info.mode = FILE_MODE
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.type = tarfile.REGTYPE
                archive.addfile(info, io.BytesIO(data))
    ensure_dir(target.parent)
    target.write_bytes(buffer.getvalue())


def export_skills(
    store: Any,
    *,
    out: Path,
    slugs: Iterable[str] = (),
    categories: Iterable[str] = (),
    include_references: bool = False,
    tool_version: str,
) -> dict[str, Any]:
    """Select skills, build the bundle, and return the manifest that was written."""
    available = store.list_skills(limit=1000, include_archived=True)
    by_slug = {skill.slug: skill for skill in available}
    wanted_slugs = [str(item) for item in slugs if str(item).strip()]
    if wanted_slugs:
        unknown = sorted({slug for slug in wanted_slugs if slug not in by_slug})
        if unknown:
            raise BundleError("unknown skill(s): " + ", ".join(unknown))
        selected = [by_slug[slug] for slug in wanted_slugs]
    else:
        selected = list(available)
    wanted_categories = {normalize_category(str(item)) for item in categories if str(item).strip()}
    if wanted_categories:
        selected = [skill for skill in selected if skill.category in wanted_categories]
    if not selected:
        raise BundleError("no skills matched the requested filters; nothing to export")
    manifest, members = build_manifest(
        selected, include_references=include_references, tool_version=tool_version
    )
    write_bundle(Path(out), [(MANIFEST_NAME, manifest_bytes(manifest)), *members])
    return manifest


# ---------------------------------------------------------------------- import


def read_bundle(path: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Read every member of a bundle and parse its manifest. No checksum check here."""
    source = Path(path)
    members: dict[str, bytes] = {}
    try:
        with tarfile.open(source, mode="r:gz") as archive:
            for info in archive.getmembers():
                if info.isdir():
                    continue
                if not info.isfile():
                    raise BundleError(f"{info.name}: only regular files may ship in a bundle")
                if not is_safe_member_name(info.name):
                    raise BundleError(f"{info.name}: unsafe path in bundle")
                handle = archive.extractfile(info)
                if handle is None:
                    raise BundleError(f"{info.name}: cannot read bundle member")
                members[info.name] = handle.read()
    except BundleError:
        raise
    except (tarfile.TarError, OSError) as exc:
        raise BundleError(f"cannot read bundle {source}: {exc}") from exc
    if MANIFEST_NAME not in members:
        raise BundleError(f"{source}: {MANIFEST_NAME} is missing")
    try:
        manifest = json.loads(members[MANIFEST_NAME].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"{source}: {MANIFEST_NAME} is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise BundleError(f"{source}: {MANIFEST_NAME} must be an object")
    if manifest.get("schema") != BUNDLE_SCHEMA:
        raise BundleError(
            f"{source}: unsupported schema {manifest.get('schema')!r}; expected {BUNDLE_SCHEMA}"
        )
    return manifest, members


def verify_bundle(manifest: dict[str, Any], members: dict[str, bytes]) -> list[str]:
    """Check every declared file and return the skill slugs in manifest order."""
    listed = manifest.get("files")
    if not isinstance(listed, list):
        raise BundleError("manifest.files must be a list")
    declared: dict[str, dict[str, Any]] = {}
    for entry in listed:
        if not isinstance(entry, dict):
            raise BundleError("manifest.files entries must be objects")
        name = str(entry.get("path") or "")
        if name == MANIFEST_NAME:
            raise BundleError(f"{MANIFEST_NAME} must not list itself")
        if not is_safe_member_name(name):
            raise BundleError(f"{name}: unsafe path in manifest")
        declared[name] = entry
    for name in sorted(declared):
        entry = declared[name]
        data = members.get(name)
        if data is None:
            raise BundleError(f"{name}: listed in the manifest but missing from the bundle")
        if sha256_bytes(data) != str(entry.get("sha256") or ""):
            raise BundleError(f"{name}: sha256 mismatch; the bundle changed after export")
        size = entry.get("size")
        if size is not None and int(size) != len(data):
            raise BundleError(f"{name}: size mismatch; the bundle changed after export")
    unexpected = sorted(set(members) - set(declared) - {MANIFEST_NAME})
    if unexpected:
        raise BundleError("unlisted member(s) in bundle: " + ", ".join(unexpected))
    rows = manifest.get("skills")
    if not isinstance(rows, list) or not rows:
        raise BundleError("manifest.skills must be a non-empty list")
    slugs: list[str] = []
    for row in rows:
        slug = str(row.get("slug") or "") if isinstance(row, dict) else ""
        if not slug:
            raise BundleError("manifest.skills entries must carry a slug")
        if not is_safe_member_name(f"{SKILLS_PREFIX}/{slug}/SKILL.md"):
            raise BundleError(f"{slug}: unsafe skill slug in manifest")
        if f"{SKILLS_PREFIX}/{slug}/SKILL.md" not in declared:
            raise BundleError(f"{slug}: SKILL.md is missing from the bundle")
        slugs.append(slug)
    if len(set(slugs)) != len(slugs):
        raise BundleError("manifest.skills lists a slug more than once")
    return slugs


def _document_title(body: str, fallback: str) -> str:
    for line in (body or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or fallback
    return fallback


def skill_from_bundle(skill_dir: Path) -> Skill:
    """Rebuild one index row from an installed bundle directory."""
    root = Path(skill_dir)
    text = (root / "SKILL.md").read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(text)
    metadata = read_json(root / "metadata.json", {})
    if not isinstance(metadata, dict):
        metadata = {}
    data = metadata.get("data") if isinstance(metadata.get("data"), dict) else {}
    declared = frontmatter.get("metadata") if isinstance(frontmatter.get("metadata"), dict) else {}
    slug = root.name
    description = str(frontmatter.get("description") or "")
    tags_value = declared.get("tags") if isinstance(declared.get("tags"), list) else data.get("tags")
    triggers_value = (
        declared.get("triggers") if isinstance(declared.get("triggers"), list) else data.get("triggers")
    )
    confidence_value = declared.get("confidence", data.get("confidence"))
    try:
        confidence = round(clamp(float(confidence_value)), 3)
    except (TypeError, ValueError):
        confidence = DEFAULT_CONFIDENCE
    created_at = str(metadata.get("created_at") or now_iso())
    updated_at = str(metadata.get("updated_at") or created_at)
    return Skill(
        slug=slug,
        name=str(frontmatter.get("name") or slug),
        title=_document_title(body, str(data.get("title") or slug)),
        description=description,
        summary=str(data.get("summary") or description),
        category=normalize_category(str(declared.get("category") or data.get("category") or "")),
        tags=normalize_tags([str(item) for item in (tags_value or [])]),
        triggers=[str(item) for item in (triggers_value or [])][:MAX_TRIGGERS],
        version=int(metadata.get("version") or 1),
        confidence=confidence,
        status="active",
        path=str(root),
        source_threads=[],
        created_at=created_at,
        updated_at=updated_at,
        body=body,
        metadata=metadata,
    )


def import_bundle(
    config: Any,
    store: Any,
    path: Path,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Verify a bundle, install every skill, and index it. Fails closed."""
    manifest, members = read_bundle(path)
    slugs = verify_bundle(manifest, members)
    skills_dir = ensure_dir(config.skills_dir)
    targets = {slug: skills_dir / slug for slug in slugs}
    conflicts = sorted(slug for slug, target in targets.items() if target.exists())
    if conflicts and not force:
        raise BundleError(
            "already installed: " + ", ".join(conflicts) + ". Pass --force to replace them."
        )
    staging = Path(tempfile.mkdtemp(prefix=".t2s-import-", dir=str(config.home)))
    try:
        for name, data in members.items():
            if name == MANIFEST_NAME:
                continue
            destination = staging / PurePosixPath(name)
            ensure_dir(destination.parent)
            destination.write_bytes(data)
        problems: list[str] = []
        for slug in slugs:
            for issue in validate_skill_dir(staging / SKILLS_PREFIX / slug):
                if issue.level == "error":
                    problems.append(f"{slug}: {issue.message}")
        if problems:
            raise BundleError("bundle holds invalid skills: " + "; ".join(problems))
        installed: list[str] = []
        statuses = {
            str(row.get("slug")): str(row.get("status") or "active")
            for row in (manifest.get("skills") or [])
            if isinstance(row, dict)
        }
        for slug in slugs:
            source = staging / SKILLS_PREFIX / slug
            target = targets[slug]
            if target.exists():
                shutil.rmtree(target)
            shutil.move(str(source), str(target))
            skill = skill_from_bundle(target)
            if statuses.get(slug) == "archived":
                skill.status = "archived"
            store.save_skill(skill)
            installed.append(slug)
        return {"imported": installed, "skills": manifest.get("skills") or []}
    finally:
        shutil.rmtree(staging, ignore_errors=True)