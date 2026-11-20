"""Publish one local skill to GitHub, or delegate the publish to the ClawHub CLI.

The pushed layout is exactly what `t2s import` expects on the other side:

    SKILL.md              the skill itself
    metadata.json         category, tags, triggers, confidence, provenance
    README.md             generated here
    references/thread.md  only when asked for

Redaction removes secrets, not context, so `references/thread.md` still carries the
conversation. It is never pushed unless the caller asks for it, and the command
says so when it does. Private is the default: a publish that names no visibility
stays private, and `--public` has to be written out.

Nothing here asks for a token. Publishing reuses the authentication the GitHub CLI
already has, so a missing or logged-out `gh` stops the run with an action the user
can take. `--dry-run` runs nothing at all: no lookup, no subprocess, no network.

External commands go through one runner and one binary lookup so a test can replace
both. No test talks to GitHub.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .models import Skill
from .validate import validate_skill_dir

DEFAULT_TARGET = "github"
PUBLISH_TARGETS = (DEFAULT_TARGET, "clawhub")

PRIVATE = "private"
PUBLIC = "public"

GH_BINARY = "gh"
CLAWHUB_BINARY = "clawhub"
GIT_BINARY = "git"

SKILL_FILE = "SKILL.md"
METADATA_FILE = "metadata.json"
README_FILE = "README.md"
REFERENCES_DIRNAME = "references"
REFERENCE_FILE = f"{REFERENCES_DIRNAME}/thread.md"

UNKNOWN_OWNER = "<gh-account>"
UNKNOWN_STAGING = "<staging>"
PROJECT_URL = "https://github.com/fuleinist/thread-to-skill"

MISSING_GH = (
    "`gh` is not on PATH. Install the GitHub CLI (https://cli.github.com), run "
    "`gh auth login`, or publish with --to clawhub."
)
MISSING_CLAWHUB = "the `clawhub` CLI is not on PATH. Install it, or publish with --to github."
MISSING_GIT = "`git` is not on PATH, and the push needs it. Install git, or publish with --to clawhub."
NOT_AUTHENTICATED = (
    "`gh` is not authenticated. Run `gh auth login` first; t2s never asks for a token."
)
PUSH_HINT = "Nothing was sent. Check the repository name and that `gh` can reach github.com."
PRIVACY_WARNING = (
    "references/thread.md holds conversation text even after redaction: redaction removes "
    "secrets, not context. You asked to publish it."
)

RunCommand = Callable[..., "subprocess.CompletedProcess"]
BinaryLookup = Callable[[str], "str | None"]


class PublishError(RuntimeError):
    """Raised when a skill cannot be published."""


def _default_run(argv: list[str], *, cwd: Path | None = None) -> "subprocess.CompletedProcess":
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )


def _default_lookup(name: str) -> "str | None":
    return shutil.which(name)


# ----------------------------------------------------------------------- plan


@dataclass
class PublishFile:
    """One file a publish writes into the repository."""

    path: str
    origin: str
    source: Path | None = None
    content: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "origin": self.origin,
            "bytes": len(self.content.encode("utf-8")),
        }


@dataclass
class PublishPlan:
    """What a publish would do: the name, the visibility, the files, the commands."""

    target: str
    slug: str
    owner: str
    repo: str
    visibility: str
    title: str = ""
    description: str = ""
    version: int = 1
    files: list[PublishFile] = field(default_factory=list)
    commands: list[list[str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        if self.target != "github":
            return self.repo
        return f"{self.owner or UNKNOWN_OWNER}/{self.repo}"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.full_name}" if self.target == "github" else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "slug": self.slug,
            "repository": self.full_name,
            "url": self.url,
            "visibility": self.visibility,
            "files": [item.to_dict() for item in self.files],
            "commands": [list(command) for command in self.commands],
            "warnings": list(self.warnings),
            "notes": list(self.notes),
        }


def build_readme(plan: PublishPlan) -> str:
    """The README written into the published repository."""
    has_reference = any(item.path == REFERENCE_FILE for item in plan.files)
    lines = [
        f"# {plan.title or plan.slug}",
        "",
        plan.description or f"Published copy of the `{plan.slug}` skill.",
        "",
        "## Install",
        "",
        "```bash",
        f"git clone {plan.url} {plan.slug}" if plan.target == "github" else f"clawhub install {plan.slug}",
        f"t2s import ./{plan.slug}",
        "```",
        "",
        "This is a normal Agent Skills directory, so `t2s import` adopts it into a workspace as "
        "it is.",
        "",
        "## What is here",
        "",
        f"- `{SKILL_FILE}` - the skill",
        f"- `{METADATA_FILE}` - category, tags, triggers, confidence, provenance",
        f"- `{README_FILE}` - this file",
    ]
    if has_reference:
        lines.append(f"- `{REFERENCE_FILE}` - the redacted transcript of the conversation")
    lines += ["", "## Privacy", "", "Redaction removes secrets, not context."]
    if has_reference:
        lines.append(
            "This repository carries the transcript, so the conversation itself is public. It is "
            "here only because the publish ran with --include-references."
        )
    else:
        lines.append(
            "The transcript is not part of this repository. It stays in the local workspace, "
            "because a redacted thread still says who said what."
        )
    lines += ["", f"Published by [thread-to-skill]({PROJECT_URL}). Skill version {plan.version}."]
    return "\n".join(lines) + "\n"


def _skill_files(skill: Skill) -> list[PublishFile]:
    """The two files every published skill must carry, read from disk."""
    skill_dir = Path(skill.path)
    if not skill_dir.is_dir():
        raise PublishError(f"{skill.slug}: no skill directory at {skill_dir}. Run `t2s distill` first.")
    files: list[PublishFile] = []
    for name in (SKILL_FILE, METADATA_FILE):
        path = skill_dir / name
        if not path.is_file():
            raise PublishError(f"{skill.slug}: {path} is missing. Run `t2s distill` before publishing.")
        files.append(
            PublishFile(
                path=name,
                origin="skill",
                source=path,
                content=path.read_text(encoding="utf-8"),
            )
        )
    return files


def _rejected_skill(skill: Skill) -> None:
    """Stop a publish that would push a skill `t2s validate` refuses."""
    errors = [issue.message for issue in validate_skill_dir(skill.path) if issue.level == "error"]
    if errors:
        raise PublishError(f"{skill.slug}: validate refused this skill: " + "; ".join(errors))


def _plan_commands(plan: PublishPlan, staging: str) -> list[list[str]]:
    """The commands a publish runs, in run order."""
    if plan.target == "clawhub":
        return [[CLAWHUB_BINARY, "publish", str(staging)]]
    full = plan.full_name
    git = ["git", str(staging)]
    return [
        [GH_BINARY, "api", "user"],
        [GH_BINARY, "repo", "view", full],
        [GH_BINARY, "repo", "create", full, "--visibility", plan.visibility],
        [*git, "init", "-q", "-b", "main"],
        [*git, "add", "-A"],
        [*git, "commit", "-q", "-m", f"publish {plan.slug}"],
        [*git, "remote", "add", "origin", plan.url],
        [*git, "-c", "credential.helper=!gh auth token", "push", "-q", "origin", "main"],
    ]


def build_plan(
    skill: Skill,
    *,
    target: str = DEFAULT_TARGET,
    org: str = "",
    repo: str = "",
    visibility: str = PRIVATE,
    include_references: bool = False,
    staging: str = UNKNOWN_STAGING,
) -> PublishPlan:
    """Build the plan for one publish. Nothing here runs anything."""
    if target not in PUBLISH_TARGETS:
        raise PublishError(f"unknown publish target: {target}. Use github or clawhub.")
    if visibility not in (PRIVATE, PUBLIC):
        raise PublishError(f"unknown visibility: {visibility}. Use private or public.")

    plan = PublishPlan(
        target=target,
        slug=skill.slug,
        owner=(org or "").strip(),
        repo=(repo or skill.slug).strip(),
        visibility=visibility,
        title=skill.title or skill.slug,
        description=(skill.description or "").strip(),
        version=skill.version,
        files=_skill_files(skill),
    )

    reference = Path(skill.path) / REFERENCE_FILE
    if include_references:
        plan.warnings.append(PRIVACY_WARNING)
        if reference.is_file():
            plan.files.append(
                PublishFile(
                    path=REFERENCE_FILE,
                    origin="skill",
                    source=reference,
                    content=reference.read_text(encoding="utf-8"),
                )
            )
        else:
            plan.notes.append(f"{REFERENCE_FILE} is not present, so nothing extra is pushed.")
    else:
        plan.notes.append(
            f"{REFERENCE_FILE} is not pushed. Add --include-references to publish the transcript."
        )

    plan.files.append(PublishFile(path=README_FILE, origin="generated", content=build_readme(plan)))
    if target == "github":
        plan.notes.append(
            "The repository is created only when it does not exist. An existing repository keeps "
            "the visibility it already has."
        )
    plan.commands = _plan_commands(plan, staging)
    return plan


# -------------------------------------------------------------------- running


def write_staging(plan: PublishPlan, directory: Path) -> list[str]:
    """Write the planned files into one directory and return the paths written."""
    written = []
    for item in plan.files:
        target = Path(directory) / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item.content, encoding="utf-8", newline="\n")
        written.append(str(target))
    return written


def _record(result: dict[str, Any], argv: list[str], completed: "subprocess.CompletedProcess") -> int:
    detail = (completed.stderr or completed.stdout or "").strip()
    result["ran"].append(
        {"command": list(argv), "returncode": int(completed.returncode), "detail": detail}
    )
    return int(completed.returncode)


def _failed(name: str, argv: list[str], completed: "subprocess.CompletedProcess", hint: str = "") -> PublishError:
    detail = (completed.stderr or completed.stdout or "").strip()
    message = f"{name} failed with exit {int(completed.returncode)}: {' '.join(argv)}"
    if detail:
        message = f"{message}: {detail}"
    if hint:
        message = f"{message}. {hint}"
    return PublishError(message)


def _account_output(text: str) -> str:
    """The account name from a `gh api user` response."""
    text = (text or "").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(data, dict):
        return str(data.get("login") or "").strip()
    return text


def publish_skill(
    skill: Skill,
    *,
    target: str = DEFAULT_TARGET,
    org: str = "",
    repo: str = "",
    visibility: str = PRIVATE,
    include_references: bool = False,
    dry_run: bool = False,
    run: RunCommand | None = None,
    which: BinaryLookup | None = None,
) -> dict[str, Any]:
    """Publish one skill, or plan a publish. Returns the plan plus what ran.

    A dry run writes nothing and calls nothing, so it cannot reach the network. A
    real run resolves the GitHub account through `gh`, writes the planned files to
    a staging directory, and pushes them. Every failure raises `PublishError` with
    an action the user can take.
    """
    runner = run or _default_run
    lookup = which or _default_lookup

    _rejected_skill(skill)
    plan = build_plan(
        skill,
        target=target,
        org=org,
        repo=repo,
        visibility=visibility,
        include_references=include_references,
    )
    result = plan.to_dict()
    result.update(
        {
            "dry_run": dry_run,
            "staged": "",
            "ran": [],
            "skipped": [],
            "written": [],
            "pushed": False,
            "delegated": False,
            "exit_code": 0,
        }
    )
    if dry_run:
        return result

    if target == "github":
        if not lookup(GH_BINARY):
            raise PublishError(MISSING_GH)
        if not lookup(GIT_BINARY):
            raise PublishError(MISSING_GIT)
        gate = [GH_BINARY, "api", "user"]
        completed = runner(gate)
        if _record(result, gate, completed) != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise PublishError(NOT_AUTHENTICATED + (f" {detail}" if detail else ""))
        account = _account_output(completed.stdout)
        if account and not plan.owner:
            plan.owner = account
            result["owner"] = account
            result["repository"] = plan.full_name
            result["url"] = plan.url
    else:
        clawhub = lookup(CLAWHUB_BINARY)
        if not clawhub:
            raise PublishError(MISSING_CLAWHUB)

    staging = Path(tempfile.mkdtemp(prefix="t2s-publish-"))
    result["staged"] = str(staging)
    result["written"] = write_staging(plan, staging)

    if target == "clawhub":
        argv = [clawhub, "publish", str(staging)]
        result["delegated"] = True
        result["commands"] = [list(argv)]
        completed = runner(argv)
        code = _record(result, argv, completed)
        result["exit_code"] = code
        if code:
            raise _failed("clawhub", argv, completed)
        result["pushed"] = True
        return result

    full = plan.full_name
    view = [GH_BINARY, "repo", "view", full]
    completed = runner(view, cwd=staging)
    exists = _record(result, view, completed) == 0
    if not exists:
        create = [GH_BINARY, "repo", "create", full, "--visibility", plan.visibility]
        completed = runner(create, cwd=staging)
        if _record(result, create, completed) != 0:
            raise _failed("gh repo create", create, completed)
    else:
        result["skipped"].append([GH_BINARY, "repo", "create", full, "--visibility", plan.visibility])

    steps: list[tuple[str, list[str], str]] = [
        ("init", ["init", "-q", "-b", "main"], ""),
        ("add", ["add", "-A"], ""),
        ("commit", ["commit", "-q", "-m", f"publish {plan.slug}"], ""),
        ("remote add", ["remote", "add", "origin", plan.url], ""),
        (
            "push",
            ["-c", "credential.helper=!gh auth token", "push", "-q", "origin", "main"],
            PUSH_HINT,
        ),
    ]
    for label, args, hint in steps:
        argv = [GIT_BINARY, str(staging), *args]
        completed = runner(argv, cwd=staging)
        code = _record(result, argv, completed)
        if code:
            raise _failed(f"git {label}", argv, completed, hint or "Nothing was sent.")

    result["pushed"] = True
    result["exit_code"] = 0
    return result
