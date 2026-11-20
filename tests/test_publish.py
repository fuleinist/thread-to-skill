"""Tests for `t2s publish`: the plan, the privacy default, and a mocked gh.

No test here talks to GitHub. Every external command goes through a stand-in
runner, and every binary lookup goes through a stand-in lookup, so the suite
runs offline and asserts what would have been sent.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from thread_to_skill.cli import main
from thread_to_skill.models import Skill
from thread_to_skill.publish import (
    GH_BINARY,
    MISSING_CLAWHUB,
    MISSING_GH,
    NOT_AUTHENTICATED,
    PRIVACY_WARNING,
    REFERENCE_FILE,
    PublishError,
    build_plan,
    publish_skill,
)

SKILL_MD = """---
name: {name}
description: Publish one skill and keep the transcript out of the repository.
metadata:
  category: engineering/release
  tags:
    - publishing
    - distribution
  confidence: 0.7
---

# {title}

## When to use it

Use this skill when one local skill must leave the workspace as its own repository.

## Steps

1. Plan the publish and read the file list.
2. Create the repository only when it does not exist.
3. Push the skill files.

## Pitfalls

The transcript in references/thread.md still holds conversation text. It stays
local unless the caller asks for it.
"""


def make_skill(root: Path, slug: str = "release-notes") -> Skill:
    """A valid skill directory plus the Skill row that points at it."""
    skill_dir = root / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        SKILL_MD.format(name=slug, title="Release notes"),
        encoding="utf-8",
    )
    (skill_dir / "metadata.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "category": "engineering/release",
                "tags": ["publishing", "distribution"],
                "confidence": 0.7,
                "version": 1,
            }
        ),
        encoding="utf-8",
    )
    return Skill(
        slug=slug,
        name=slug,
        title="Release notes",
        description="Publish one skill and keep the transcript out of the repository.",
        summary="",
        category="engineering/release",
        tags=["publishing", "distribution"],
        triggers=[],
        version=1,
        confidence=0.7,
        status="active",
        path=str(skill_dir),
        source_threads=[],
        created_at="2026-11-21T00:00:00Z",
        updated_at="2026-11-21T00:00:00Z",
    )


class FakeRunner:
    """Stand-in for subprocess.run: records each call and returns a canned result."""

    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler or (lambda argv: (0, "", ""))

    def __call__(self, argv, cwd=None):
        argv = list(argv)
        self.calls.append((argv, str(cwd) if cwd else None))
        code, out, err = self.handler(argv)
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr=err)

    @property
    def keys(self):
        return [" ".join(argv) for argv, _ in self.calls]


def lookup_only(*present):
    """A binary lookup that finds only the named binaries."""
    present = set(present)
    return lambda name: f"/usr/bin/{name}" if name in present else None


def github_handler(login="octocat", repo_exists=False, create_code=0, push_code=0):
    """A gh and git stand-in with a successful account lookup by default."""

    def handler(argv):
        key = " ".join(argv)
        if key.endswith("api user"):
            return 0, json.dumps({"login": login}), ""
        if "repo view" in key:
            if repo_exists:
                return 0, "", ""
            return 1, "", "GraphQL: Could not resolve to a Repository"
        if "repo create" in key:
            return create_code, "", "" if create_code == 0 else "HTTP 422: name already exists"
        if key.endswith("push -q origin main"):
            if push_code:
                return push_code, "", "fatal: could not read Username for 'https://github.com'"
            return 0, "", ""
        return 0, "", ""

    return handler


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.skill = make_skill(self.root)
        self.staged = []

    def tearDown(self):
        for path in self.staged:
            shutil.rmtree(path, ignore_errors=True)
        self.temp.cleanup()

    def publish(self, **kwargs):
        kwargs.setdefault("which", lookup_only(GH_BINARY, "git"))
        if "run" not in kwargs:
            runner = FakeRunner(github_handler())
            kwargs["run"] = runner
        else:
            runner = kwargs["run"]
        result = publish_skill(self.skill, **kwargs)
        if result.get("staged"):
            self.staged.append(result["staged"])
        return result, runner

    # ------------------------------------------------------------------ plan

    def test_dry_run_makes_no_calls_and_names_the_files(self):
        runner = FakeRunner(github_handler())
        result, _ = self.publish(dry_run=True, run=runner, which=lookup_only())

        self.assertTrue(result["dry_run"])
        self.assertEqual(runner.calls, [], "a dry run must not run anything")
        self.assertEqual(result["written"], [])
        self.assertEqual(result["staged"], "")
        self.assertFalse(result["pushed"])

        paths = [item["path"] for item in result["files"]]
        self.assertEqual(paths, ["SKILL.md", "metadata.json", "README.md"])
        self.assertNotIn(REFERENCE_FILE, paths)
        self.assertEqual(
            [item["origin"] for item in result["files"]],
            ["skill", "skill", "generated"],
        )
        self.assertTrue(result["commands"], "the plan still shows the commands it would run")

    def test_private_is_the_default_and_public_must_be_explicit(self):
        plan = build_plan(self.skill)
        self.assertEqual(plan.visibility, "private")
        self.assertIn("--visibility private", " ".join(plan.commands[2]))

        public = build_plan(self.skill, visibility="public")
        self.assertEqual(public.visibility, "public")
        self.assertIn("--visibility public", " ".join(public.commands[2]))

    def test_reference_transcript_is_opt_in(self):
        without = build_plan(self.skill)
        self.assertNotIn(REFERENCE_FILE, [item.path for item in without.files])
        self.assertEqual(without.warnings, [])
        self.assertIn("is not pushed", " ".join(without.notes))

        (Path(self.skill.path) / "references").mkdir(parents=True, exist_ok=True)
        (Path(self.skill.path) / REFERENCE_FILE).write_text(
            "[1] alice: the deploy key is rotated\n", encoding="utf-8"
        )
        with_reference = build_plan(self.skill, include_references=True)
        self.assertIn(REFERENCE_FILE, [item.path for item in with_reference.files])
        self.assertEqual(with_reference.warnings, [PRIVACY_WARNING])
        readme = next(
            item.content for item in with_reference.files if item.path == "README.md"
        )
        self.assertIn(REFERENCE_FILE, readme)
        self.assertIn("--include-references", readme)

    def test_generated_readme_states_that_the_transcript_is_not_published(self):
        result, _ = self.publish(dry_run=True)
        readme = next(item for item in result["files"] if item["path"] == "README.md")
        self.assertGreater(readme["bytes"], 0)

        plan = build_plan(self.skill)
        text = next(item.content for item in plan.files if item.path == "README.md")
        self.assertIn("Redaction removes secrets, not context", text)
        self.assertIn("not part of this repository", text)

    # --------------------------------------------------------------- running

    def test_publish_writes_the_planned_files_before_pushing(self):
        result, runner = self.publish()

        self.assertTrue(result["pushed"])
        staging = Path(result["staged"])
        self.assertTrue(staging.is_dir())
        for item in result["files"]:
            written = staging / item["path"]
            self.assertTrue(written.is_file(), f"{item['path']} was not written")
        self.assertEqual(
            (staging / "SKILL.md").read_text(encoding="utf-8"),
            (Path(self.skill.path) / "SKILL.md").read_text(encoding="utf-8"),
        )
        self.assertFalse((staging / REFERENCE_FILE).exists())
        self.assertIn("push -q origin main", " ".join(runner.keys))

    def test_owner_comes_from_gh_when_org_is_absent(self):
        result, _ = self.publish()
        self.assertEqual(result["owner"], "octocat")
        self.assertEqual(result["repository"], "octocat/release-notes")
        self.assertEqual(result["url"], "https://github.com/octocat/release-notes")

    def test_explicit_org_and_repo_win_over_the_gh_account(self):
        result, _ = self.publish(org="acme", repo="skill-bank")
        self.assertEqual(result["repository"], "acme/skill-bank")

    def test_an_existing_repository_is_not_created_again(self):
        runner = FakeRunner(github_handler(repo_exists=True))
        result, _ = self.publish(run=runner)
        self.assertTrue(result["pushed"])
        self.assertFalse(any("repo create" in key for key in runner.keys))
        self.assertTrue(
            any("repo create" in " ".join(step) for step in result["skipped"]),
            "repo view succeeded, so repo create must be skipped",
        )

    def test_missing_gh_stops_with_an_actionable_message(self):
        with self.assertRaises(PublishError) as caught:
            self.publish(which=lookup_only("git"))
        self.assertEqual(str(caught.exception), MISSING_GH)
        self.assertIn("gh auth login", str(caught.exception))

    def test_unauthenticated_gh_stops_before_any_write(self):
        runner = FakeRunner(lambda argv: (4, "", "You are not logged into any GitHub hosts."))
        with self.assertRaises(PublishError) as caught:
            self.publish(run=runner)
        self.assertIn(NOT_AUTHENTICATED, str(caught.exception))
        self.assertFalse(any("repo create" in key for key in runner.keys))
        self.assertFalse(any("push" in key for key in runner.keys))

    def test_a_failed_push_reports_that_nothing_was_sent(self):
        runner = FakeRunner(github_handler(push_code=128))
        with self.assertRaises(PublishError) as caught:
            self.publish(run=runner)
        message = str(caught.exception)
        self.assertIn("git push failed with exit 128", message)
        self.assertIn("could not read Username", message)
        self.assertIn("Nothing was sent", message)
        self.assertTrue(any("push -q origin main" in key for key in runner.keys))

    def test_missing_clawhub_stops_with_an_actionable_message(self):
        with self.assertRaises(PublishError) as caught:
            self.publish(target="clawhub", which=lookup_only("gh", "git"))
        self.assertEqual(str(caught.exception), MISSING_CLAWHUB)

    def test_clawhub_delegates_and_reports_success(self):
        runner = FakeRunner(lambda argv: (0, "published", ""))
        result, _ = self.publish(
            target="clawhub", run=runner, which=lookup_only("clawhub")
        )
        self.assertTrue(result["delegated"])
        self.assertTrue(result["pushed"])
        self.assertEqual(runner.keys, ["/usr/bin/clawhub publish " + result["staged"]])

    def test_clawhub_failure_raises_publish_error_not_name_error(self):
        """A defect guard: the failure path must build a PublishError from the result."""
        runner = FakeRunner(lambda argv: (2, "", "clawhub: not signed in"))
        with self.assertRaises(PublishError) as caught:
            self.publish(target="clawhub", run=runner, which=lookup_only("clawhub"))
        message = str(caught.exception)
        self.assertIn("clawhub failed with exit 2", message)
        self.assertIn("not signed in", message)

    def test_a_skill_that_validate_refuses_is_not_published(self):
        (Path(self.skill.path) / "SKILL.md").write_text(
            "---\nname: Wrong-Name\ndescription: broken\n---\n\nbody\n", encoding="utf-8"
        )
        runner = FakeRunner(github_handler())
        with self.assertRaises(PublishError) as caught:
            self.publish(run=runner)
        self.assertIn("validate refused this skill", str(caught.exception))
        self.assertEqual(runner.calls, [])


class PublishCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / ".thread2skill"
        self.skill = make_skill(self.root, slug="release-notes")

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--home", str(self.home), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def seed(self):
        for args in (("init",), ("import", str(Path(self.skill.path)))):
            code, out, err = self.run_cli(*args)
            self.assertEqual(code, 0, f"{args} failed: {out}{err}")

    def test_cli_dry_run_prints_the_plan_and_makes_no_calls(self):
        self.seed()
        code, out, err = self.run_cli("publish", "release-notes", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("would publish", out)
        self.assertIn("private", out)
        self.assertIn("SKILL.md", out)
        self.assertIn("README.md", out)
        self.assertIn("is not pushed", out)

    def test_cli_json_dry_run_is_machine_readable(self):
        self.seed()
        code, out, err = self.run_cli("publish", "release-notes", "--dry-run", "--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["visibility"], "private")
        self.assertEqual(payload["target"], "github")

    def test_cli_unknown_slug_exits_non_zero(self):
        self.seed()
        code, out, err = self.run_cli("publish", "no-such-skill", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("no skill named", out + err)

    def test_cli_public_flag_is_carried_into_the_plan(self):
        self.seed()
        code, out, err = self.run_cli(
            "publish", "release-notes", "--dry-run", "--public", "--json"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["visibility"], "public")


if __name__ == "__main__":
    unittest.main()