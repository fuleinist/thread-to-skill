import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from thread_to_skill.cli import main

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SLACK = FIXTURES / "slack-export"


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / ".thread2skill"
        self.project = self.root / "project"
        self.project.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--home", str(self.home), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def init_and_distill(self):
        code, _, _ = self.run_cli("init", "--provider", "offline")
        self.assertEqual(code, 0)
        code, output, _ = self.run_cli("ingest", str(SLACK), "--json")
        self.assertEqual(code, 0)
        threads = json.loads(output)
        self.assertEqual(len(threads), 1)
        thread_id = threads[0]["id"]
        code, output, _ = self.run_cli("distill", thread_id, "--provider", "offline", "--json")
        self.assertEqual(code, 0)
        return thread_id, json.loads(output)

    def test_init_creates_workspace(self):
        code, output, _ = self.run_cli("init", "--provider", "offline")
        self.assertEqual(code, 0)
        self.assertTrue((self.home / "config.json").exists())
        self.assertTrue((self.home / "skills").is_dir())
        self.assertIn("initialized", output)

    def test_init_is_idempotent_without_force(self):
        self.run_cli("init")
        code, output, _ = self.run_cli("init")
        self.assertEqual(code, 0)
        self.assertIn("already exists", output)

    def test_ingest_redacts_and_indexes(self):
        self.run_cli("init", "--provider", "offline")
        code, output, _ = self.run_cli("ingest", str(SLACK), "--json")
        self.assertEqual(code, 0)
        threads = json.loads(output)
        self.assertEqual(threads[0]["messages"], 10)
        normalized = self.home / "threads" / f"{threads[0]['id']}.json"
        content = normalized.read_text(encoding="utf-8")
        self.assertNotIn("ghp_FAKE", content)
        self.assertNotIn("priya.nair@acme.example", content)
        self.assertIn("[redacted-github-token]", content)

    def test_end_to_end(self):
        thread_id, skill = self.init_and_distill()
        self.assertEqual(skill["slug"], skill["name"])
        self.assertEqual(skill["version"], 1)
        self.assertIn("engineering/", skill["category"])
        self.assertTrue((Path(skill["path"]) / "SKILL.md").exists())

        code, output, _ = self.run_cli("validate", "--all")
        self.assertEqual(code, 0)
        self.assertIn("0 error", output)

        code, output, _ = self.run_cli("list", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(output)), 1)

        code, output, _ = self.run_cli("show", skill["slug"], "--raw")
        self.assertEqual(code, 0)
        self.assertIn("## Procedure", output)
        self.assertNotIn("ghp_FAKE", output)

        code, output, _ = self.run_cli("suggest", "integration tests are flaky with postgres", "--threshold", "0.0")
        self.assertEqual(code, 0)
        self.assertIn(skill["slug"], output)

        code, output, _ = self.run_cli("feedback", skill["slug"], "--outcome", "worked")
        self.assertEqual(code, 0)
        self.assertIn("confidence", output)

        code, output, _ = self.run_cli("search", "integration")
        self.assertEqual(code, 0)
        self.assertIn(skill["slug"], output)

        code, output, _ = self.run_cli("stats", "--json")
        self.assertEqual(code, 0)
        stats = json.loads(output)
        self.assertEqual(stats["threads"], 1)
        self.assertEqual(stats["skills"], 1)

    def test_duplicate_merge_increments_version(self):
        thread_id, first = self.init_and_distill()
        code, output, _ = self.run_cli(
            "distill", thread_id, "--provider", "offline", "--on-duplicate", "merge", "--json"
        )
        self.assertEqual(code, 0)
        merged = json.loads(output)
        self.assertEqual(merged["slug"], first["slug"])
        self.assertEqual(merged["version"], 2)
        code, output, _ = self.run_cli("list", "--json")
        self.assertEqual(len(json.loads(output)), 1)

    def test_duplicate_skip(self):
        thread_id, first = self.init_and_distill()
        code, output, _ = self.run_cli(
            "distill", thread_id, "--provider", "offline", "--on-duplicate", "skip"
        )
        self.assertEqual(code, 2)
        self.assertIn("skipped", output)
        self.assertIn(first["slug"], output)

    def test_install_project_scope(self):
        _, skill = self.init_and_distill()
        code, output, _ = self.run_cli(
            "install",
            skill["slug"],
            "--harness",
            "claude",
            "--scope",
            "project",
            "--project-root",
            str(self.project),
        )
        self.assertEqual(code, 0)
        target = self.project / ".claude" / "skills" / skill["slug"] / "SKILL.md"
        self.assertTrue(target.exists())
        self.assertIn("installed", output)

    def test_hooks_dry_run(self):
        code, output, _ = self.run_cli(
            "hooks",
            "install",
            "--harness",
            "claude",
            "--scope",
            "project",
            "--project-root",
            str(self.project),
            "--dry-run",
        )
        self.assertEqual(code, 0)
        self.assertIn("dry-run", output)
        self.assertFalse((self.project / ".claude" / "settings.json").exists())

    def test_hooks_accept_comma_separated_harnesses(self):
        code, output, _ = self.run_cli(
            "hooks",
            "install",
            "--harness",
            "claude,cursor",
            "--scope",
            "project",
            "--project-root",
            str(self.project),
        )
        self.assertEqual(code, 0)
        self.assertTrue((self.project / ".claude" / "settings.json").exists())
        self.assertTrue((self.project / ".cursor" / "hooks.json").exists())
        self.assertIn("claude", output)
        self.assertIn("cursor", output)

    def test_hooks_preserve_existing_settings(self):
        settings = self.project / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(
            json.dumps({"hooks": {"UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "echo existing"}]}]}}),
            encoding="utf-8",
        )
        code, _, _ = self.run_cli(
            "hooks", "install", "--harness", "claude", "--scope", "project", "--project-root", str(self.project)
        )
        self.assertEqual(code, 0)
        data = json.loads(settings.read_text(encoding="utf-8"))
        entries = data["hooks"]["UserPromptSubmit"]
        self.assertTrue(any("echo existing" in json.dumps(entry) for entry in entries))
        self.assertTrue(any("t2s suggest" in json.dumps(entry) for entry in entries))
        # Re-running must not duplicate the managed hook.
        self.run_cli(
            "hooks", "install", "--harness", "claude", "--scope", "project", "--project-root", str(self.project)
        )
        data = json.loads(settings.read_text(encoding="utf-8"))
        count = sum(1 for entry in data["hooks"]["UserPromptSubmit"] if "t2s suggest" in json.dumps(entry))
        self.assertEqual(count, 1)

    def test_hook_formats_are_valid_json(self):
        self.init_and_distill()
        for fmt in ("json", "claude", "cursor", "deepseek"):
            code, output, _ = self.run_cli("suggest", "flaky integration tests", "--format", fmt, "--threshold", "0.0")
            self.assertEqual(code, 0)
            parsed = json.loads(output)
            if fmt == "claude" or fmt == "deepseek":
                self.assertIn("hookSpecificOutput", parsed)
            elif fmt == "cursor":
                self.assertTrue(parsed["continue"])
                self.assertIn("user_message", parsed)
                self.assertNotIn("agent_message", parsed)
            else:
                self.assertIn("suggestions", parsed)

    def test_unknown_thread_returns_error(self):
        self.run_cli("init")
        code, output, _ = self.run_cli("distill", "does-not-exist")
        self.assertEqual(code, 1)
        self.assertIn("Unknown thread", output)

    def test_doctor(self):
        self.run_cli("init")
        code, output, _ = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("python", output)
        self.assertIn("workspace", output)


if __name__ == "__main__":
    unittest.main()