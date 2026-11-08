"""Tests for `t2s hooks doctor` (issue #25).

Every case builds hook files in a temporary home or project directory and drives
the doctor with a fake runner, so each failure mode is simulated without touching
a real host. The one case that uses the real runner calls `python` as the
resolved binary, which is deterministic on any platform.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from thread_to_skill.cli import main
from thread_to_skill.hooks import (
    ALL_HOOK_HARNESSES,
    BROKEN_DOCTOR_STATUSES,
    DOCTOR_SAMPLE_PROMPT,
    doctor_hooks,
    install_hooks,
)

MISSING_BINARY = "no-such-t2s-on-this-machine"
SAMPLE_ANSWER = "[thread-to-skill] Relevant skills:\n- test-gates (testing, 0.9): run the tests first"


def fake_runner(returncode=0, stdout="", stderr="", error=None):
    """A stand-in for the sample `suggest` call. Returns (runner, calls)."""
    calls = []

    def runner(binary, args, cwd, timeout):
        calls.append({"binary": binary, "args": list(args), "cwd": str(cwd), "timeout": timeout})
        if error is not None:
            raise error
        return subprocess.CompletedProcess([binary, *args], returncode, stdout, stderr)

    return runner, calls


def by_harness(results):
    return {result.harness: result for result in results}


class HooksDoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.workspace = self.root / "workspace"
        self.project = self.root / "project"
        self.home.mkdir()
        self.project.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def doctor(self, harness="all", run=None, env=None):
        with patch.dict(os.environ, env or {}):
            return doctor_hooks(
                harness,
                scope="project",
                project_root=self.project,
                home=self.home,
                run=run,
            )

    def run_cli(self, *args):
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = main(["--home", str(self.workspace), *args])
        return code, stdout.getvalue()

    def write(self, relative, text):
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_missing_hooks_are_reported_per_host(self):
        runner, calls = fake_runner(stdout=SAMPLE_ANSWER)
        results = self.doctor(run=runner)
        self.assertEqual([result.harness for result in results], list(ALL_HOOK_HARNESSES))
        self.assertEqual({result.status for result in results}, {"missing"})
        for result in results:
            self.assertIn("t2s hooks install", result.detail)
            self.assertIn(result.event, result.detail)
        # Nothing is installed, so nothing is executed.
        self.assertEqual(calls, [])

    def test_installed_hooks_are_detected_and_reported_ok(self):
        install_hooks("all", scope="project", project_root=self.project)
        runner, calls = fake_runner(stdout=SAMPLE_ANSWER)
        results = self.doctor(run=runner)
        self.assertEqual({result.status for result in results}, {"ok"})
        self.assertIn("test-gates", results[0].detail)
        # One sample call for the whole run, from the working directory the host
        # would use.
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["cwd"], str(self.project))
        self.assertEqual(calls[0]["args"][0], "suggest")
        # Each host's own configured command is still reported.
        for harness, fmt in (
            ("claude", "claude"),
            ("cursor", "cursor"),
            ("deepseek", "deepseek"),
            ("hermes", "text"),
            ("openclaw", "text"),
        ):
            row = by_harness(results)[harness]
            self.assertIn(f"--format {fmt}", row.command)
            self.assertIn("--threshold 0.22", row.command)

    def test_malformed_json_is_reported_without_crashing(self):
        path = self.write(".claude/settings.json", "{ this is not json")
        runner, calls = fake_runner(stdout=SAMPLE_ANSWER)
        results = self.doctor(run=runner)
        row = by_harness(results)["claude"]
        self.assertEqual(row.status, "invalid")
        self.assertIn("not valid JSON", row.detail)
        self.assertEqual(row.files[0]["path"], str(path))
        self.assertFalse(row.files[0]["ok"])
        # A broken host does not stop the others from being checked.
        self.assertEqual(by_harness(results)["cursor"].status, "missing")
        self.assertEqual(calls, [])

    def test_yaml_manifest_validity_is_reported(self):
        install_hooks("hermes", scope="project", project_root=self.project)
        manifest = self.project / ".hermes" / "plugins" / "thread2skill" / "plugin.yaml"
        manifest.write_text("name: thread2skill\n\tbroken: indentation\n", encoding="utf-8")
        results = self.doctor("hermes", run=fake_runner(stdout=SAMPLE_ANSWER))
        self.assertEqual(results[0].status, "invalid")
        self.assertIn("not valid YAML", results[0].detail)

    def test_valid_yaml_manifest_and_module_are_accepted(self):
        install_hooks("hermes", scope="project", project_root=self.project)
        runner, _ = fake_runner(stdout=SAMPLE_ANSWER)
        results = self.doctor("hermes", run=runner)
        self.assertEqual(results[0].status, "ok")
        self.assertTrue(all(entry["ok"] for entry in results[0].files))

    def test_incomplete_plugin_is_reported_as_invalid(self):
        install_hooks("openclaw", scope="project", project_root=self.project)
        (self.project / ".openclaw" / "plugins" / "thread2skill" / "index.js").unlink()
        results = self.doctor("openclaw", run=fake_runner(stdout=SAMPLE_ANSWER))
        self.assertEqual(results[0].status, "invalid")
        self.assertIn("index.js is missing", results[0].detail)

    def test_missing_binary_is_not_reported_as_a_missing_hook(self):
        install_hooks("cursor", scope="project", project_root=self.project)
        runner, calls = fake_runner(stdout=SAMPLE_ANSWER)
        results = self.doctor(run=runner, env={"T2S_BIN": str(self.root / MISSING_BINARY)})
        found = by_harness(results)
        self.assertEqual(found["cursor"].status, "command-not-found")
        self.assertIn(MISSING_BINARY, found["cursor"].detail)
        # A host with no hook file is still reported as missing, and nothing ran.
        self.assertEqual(found["openclaw"].status, "missing")
        self.assertEqual(calls, [])

    def test_binary_that_exists_but_is_not_a_file_is_reported(self):
        install_hooks("deepseek", scope="project", project_root=self.project)
        directory = self.root / "not-a-file"
        directory.mkdir()
        results = self.doctor("deepseek", run=fake_runner(stdout=SAMPLE_ANSWER), env={"T2S_BIN": str(directory)})
        self.assertEqual(results[0].status, "command-not-found")
        self.assertIn("is not a file", results[0].detail)

    def test_t2s_is_resolved_from_path_when_t2s_bin_is_unset(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        stub = bin_dir / ("t2s.bat" if os.name == "nt" else "t2s")
        stub.write_text("echo stub\n", encoding="utf-8")
        os.chmod(str(stub), 0o755)
        install_hooks("claude", scope="project", project_root=self.project)
        runner, _ = fake_runner(stdout=SAMPLE_ANSWER)
        with patch.dict(os.environ, {"PATH": str(bin_dir)}, clear=True):
            results = doctor_hooks(
                "claude",
                scope="project",
                project_root=self.project,
                home=self.home,
                run=runner,
            )
        self.assertEqual(results[0].status, "ok")
        self.assertEqual(os.path.normcase(results[0].binary), os.path.normcase(str(stub)))

    def test_command_that_does_not_call_suggest_is_invalid(self):
        command = "t2s suggest --format nope --threshold 0.22"
        self.write(
            ".cursor/hooks.json",
            json.dumps({"version": 1, "hooks": {"beforeSubmitPrompt": [{"command": command}]}}),
        )
        results = self.doctor("cursor", run=fake_runner(stdout=SAMPLE_ANSWER))
        self.assertEqual(results[0].status, "invalid")
        self.assertIn("--format nope", results[0].detail)

        command = "t2s suggest --format text --threshold 2"
        self.write(
            ".dsh/hooks.json",
            json.dumps({"version": 1, "hooks": {"UserPromptSubmit": [{"command": command}]}}),
        )
        results = self.doctor("deepseek", run=fake_runner(stdout=SAMPLE_ANSWER))
        self.assertEqual(results[0].status, "invalid")
        self.assertIn("outside 0 to 1", results[0].detail)

    def test_sample_call_that_prints_nothing_is_reported_as_no_match(self):
        install_hooks("claude", scope="project", project_root=self.project)
        runner, _ = fake_runner(stdout="")
        results = self.doctor("claude", run=runner)
        self.assertEqual(results[0].status, "no-match")
        self.assertIn("printed nothing", results[0].detail)
        # A quiet hook is working as designed, so this is not a broken hook.
        self.assertNotIn("no-match", BROKEN_DOCTOR_STATUSES)
        self.assertNotIn("missing", BROKEN_DOCTOR_STATUSES)

    def test_sample_call_that_fails_is_reported_as_error(self):
        install_hooks("claude", scope="project", project_root=self.project)
        runner, _ = fake_runner(returncode=1, stderr="No workspace here. Run `t2s init`.")
        results = self.doctor("claude", run=runner)
        self.assertEqual(results[0].status, "error")
        self.assertIn("exited 1", results[0].detail)
        self.assertIn("t2s init", results[0].detail)

    def test_sample_call_that_times_out_is_reported_as_error(self):
        install_hooks("claude", scope="project", project_root=self.project)
        runner, _ = fake_runner(error=subprocess.TimeoutExpired(cmd="t2s", timeout=2))
        results = doctor_hooks(
            "claude",
            scope="project",
            project_root=self.project,
            home=self.home,
            run=runner,
            timeout=2,
        )
        self.assertEqual(results[0].status, "error")
        self.assertIn("timed out", results[0].detail)

    def test_real_runner_executes_the_resolved_binary_in_the_host_directory(self):
        install_hooks("claude", scope="project", project_root=self.project)
        results = self.doctor("claude", env={"T2S_BIN": sys.executable})
        row = results[0]
        # `python suggest ...` cannot run, so this proves the call really executed
        # and that its failure is reported instead of crashing the doctor.
        self.assertEqual(row.status, "error")
        self.assertEqual(row.binary, sys.executable)
        self.assertEqual(row.cwd, str(self.project))
        self.assertEqual(row.prompt, DOCTOR_SAMPLE_PROMPT)

    def test_doctor_does_not_write_anything(self):
        install_hooks("all", scope="project", project_root=self.project)
        before = {
            path: path.read_text(encoding="utf-8")
            for path in [
                self.project / ".claude" / "settings.json",
                self.project / ".cursor" / "hooks.json",
                self.project / ".hermes" / "plugins" / "thread2skill" / "plugin.yaml",
            ]
        }
        runner, _ = fake_runner(stdout=SAMPLE_ANSWER)
        self.doctor(run=runner)
        for path, text in before.items():
            self.assertEqual(path.read_text(encoding="utf-8"), text)

    def test_cli_json_output_exits_zero_when_nothing_is_installed(self):
        code, output = self.run_cli(
            "hooks",
            "doctor",
            "--json",
            "--scope",
            "project",
            "--project-root",
            str(self.project),
        )
        self.assertEqual(code, 0)
        rows = json.loads(output)
        self.assertEqual([row["harness"] for row in rows], list(ALL_HOOK_HARNESSES))
        self.assertEqual({row["status"] for row in rows}, {"missing"})
        self.assertTrue(all("event" in row and "files" in row for row in rows))
        # The doctor never takes the workspace lock or creates a workspace.
        self.assertFalse(self.workspace.exists())

    def test_cli_exits_non_zero_when_a_detected_hook_is_broken(self):
        self.write(".claude/settings.json", "{ this is not json")
        code, output = self.run_cli(
            "hooks",
            "doctor",
            "--json",
            "--harness",
            "claude",
            "--scope",
            "project",
            "--project-root",
            str(self.project),
        )
        self.assertEqual(code, 1)
        rows = json.loads(output)
        self.assertEqual(rows[0]["status"], "invalid")

    def test_cli_human_output_reports_one_line_per_host(self):
        self.write(".claude/settings.json", "{ this is not json")
        code, output = self.run_cli(
            "hooks",
            "doctor",
            "--harness",
            "claude",
            "--scope",
            "project",
            "--project-root",
            str(self.project),
        )
        self.assertEqual(code, 1)
        self.assertIn("invalid", output)
        self.assertIn("UserPromptSubmit", output)
        self.assertIn("1 host(s) checked", output)
        self.assertIn("1 broken", output)

    def test_hooks_install_is_unchanged(self):
        code, output = self.run_cli(
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


if __name__ == "__main__":
    unittest.main()
