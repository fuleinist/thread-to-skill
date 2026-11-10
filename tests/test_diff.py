import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from thread_to_skill.adapters import load_path
from thread_to_skill.cli import main
from thread_to_skill.distill import normalize_data
from thread_to_skill.offline import offline_distill
from thread_to_skill.render import prune_snapshots, write_skill_bundle
from thread_to_skill.store import list_snapshots, snapshot_dir

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SLACK = FIXTURES / "slack-export"
STAMP = "2026-10-31T00:00:00Z"
EXTRA_LINE = "- Check the fixture cache before rerunning the suite."


class DiffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / ".thread2skill"

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
        threads = json.loads(output)["threads"]
        self.assertEqual(len(threads), 1)
        thread_id = threads[0]["id"]
        code, output, _ = self.run_cli("distill", thread_id, "--provider", "offline", "--json")
        self.assertEqual(code, 0)
        return thread_id, json.loads(output)

    def skill_dir(self, slug):
        return self.home / "skills" / slug

    def change_before_next_render(self, skill_dir):
        """Add a line to the current bundle so the next render differs from it.

        The snapshot keeps this version, so the diff reports the line as removed.
        """
        lines = (skill_dir / "SKILL.md").read_text(encoding="utf-8").splitlines()
        heading = lines.index("## When to use")
        lines.insert(heading + 2, EXTRA_LINE)
        (skill_dir / "SKILL.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    def re_render(self, thread_id, slug):
        """Render the same thread again; this keeps the previous version as a snapshot."""
        code, output, _ = self.run_cli(
            "distill", thread_id, "--provider", "offline", "--on-duplicate", "merge", "--json"
        )
        self.assertEqual(code, 0)
        merged = json.loads(output)
        self.assertEqual(merged["slug"], slug)
        self.assertEqual(merged["version"], 2)
        return merged

    def test_first_render_creates_no_snapshot(self):
        _, skill = self.init_and_distill()
        skill_dir = self.skill_dir(skill["slug"])
        self.assertFalse((skill_dir / ".versions").exists())
        self.assertEqual(list_snapshots(skill_dir), [])
        code, output, _ = self.run_cli("diff", skill["slug"])
        self.assertEqual(code, 1)
        self.assertIn("No snapshot to compare", output)

    def test_second_render_keeps_the_previous_skill_md(self):
        threads = load_path(SLACK)
        data = normalize_data(offline_distill(threads), threads)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path, first, _ = write_skill_bundle(
                root,
                "flaky-integration-tests",
                data,
                threads,
                version=1,
                created_at=STAMP,
                updated_at=STAMP,
            )
            self.assertEqual(list_snapshots(path), [])
            changed = dict(data, procedure=list(data["procedure"]) + ["A new step"])
            write_skill_bundle(
                root,
                "flaky-integration-tests",
                changed,
                threads,
                version=2,
                created_at=STAMP,
                updated_at=STAMP,
            )
            self.assertEqual(list_snapshots(path), [1])
            snapshot = snapshot_dir(path, 1) / "SKILL.md"
            self.assertTrue(snapshot.exists())
            self.assertEqual(snapshot.read_text(encoding="utf-8"), first)
            self.assertIn("A new step", (path / "SKILL.md").read_text(encoding="utf-8"))

    def test_diff_prints_the_changed_line(self):
        thread_id, skill = self.init_and_distill()
        skill_dir = self.skill_dir(skill["slug"])
        self.change_before_next_render(skill_dir)
        self.re_render(thread_id, skill["slug"])
        code, output, _ = self.run_cli("diff", skill["slug"])
        self.assertEqual(code, 0)
        self.assertIn(skill["slug"], output)
        self.assertRegex(output, r"v1\s.*v2")
        self.assertIn(f"-{EXTRA_LINE}", output)
        self.assertNotIn("pruned", output)

    def test_diff_stat_prints_section_counts(self):
        thread_id, skill = self.init_and_distill()
        skill_dir = self.skill_dir(skill["slug"])
        self.change_before_next_render(skill_dir)
        self.re_render(thread_id, skill["slug"])
        code, output, _ = self.run_cli("diff", skill["slug"], "--stat")
        self.assertEqual(code, 0)
        self.assertIn("SECTION", output)
        self.assertIn("ADDED", output)
        self.assertIn("REMOVED", output)
        row = next(line for line in output.splitlines() if line.startswith("When to use"))
        self.assertEqual(row.split()[-2:], ["0", "1"])
        total = next(line for line in output.splitlines() if line.startswith("Total"))
        self.assertGreater(int(total.split()[-1]), 0)
        self.assertIn(f"-{EXTRA_LINE}", output)

    def test_diff_json_prints_documented_keys(self):
        thread_id, skill = self.init_and_distill()
        skill_dir = self.skill_dir(skill["slug"])
        self.change_before_next_render(skill_dir)
        self.re_render(thread_id, skill["slug"])
        code, output, _ = self.run_cli("diff", skill["slug"], "--json")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(
            sorted(payload), ["added", "from", "removed", "sections", "slug", "to"]
        )
        self.assertEqual(payload["slug"], skill["slug"])
        self.assertEqual(payload["from"], 1)
        self.assertEqual(payload["to"], 2)
        self.assertGreater(payload["removed"], 0)
        for section in payload["sections"]:
            self.assertEqual(sorted(section), ["added", "name", "removed"])
        changed = next(section for section in payload["sections"] if section["name"] == "When to use")
        self.assertEqual(changed["added"], 0)
        self.assertEqual(changed["removed"], 1)

    def test_diff_unknown_version_exits_nonzero(self):
        thread_id, skill = self.init_and_distill()
        self.re_render(thread_id, skill["slug"])
        code, output, _ = self.run_cli("diff", skill["slug"], "--from", "7", "--to", "2")
        self.assertEqual(code, 1)
        self.assertIn("Unknown version v7", output)
        self.assertIn("Available: v1, v2", output)

    def test_diff_prunes_to_the_kept_number(self):
        thread_id, skill = self.init_and_distill()
        skill_dir = self.skill_dir(skill["slug"])
        for version in range(1, 6):
            target = snapshot_dir(skill_dir, version)
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text(f"v{version}\n", encoding="utf-8")
        code, _, stderr = self.run_cli("diff", skill["slug"], "--from", "5", "--to", "5", "--keep", "2", "--json")
        self.assertEqual(code, 0)
        self.assertIn("pruned 3 snapshot(s): v1, v2, v3", stderr)
        self.assertEqual(list_snapshots(skill_dir), [4, 5])

    def test_prune_snapshots_keeps_newest_and_returns_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            skill_dir = Path(temp) / "flaky-integration-tests"
            for version in range(1, 6):
                target = snapshot_dir(skill_dir, version)
                target.mkdir(parents=True)
                (target / "SKILL.md").write_text(f"v{version}\n", encoding="utf-8")
            self.assertEqual(prune_snapshots(skill_dir, keep=3), [1, 2])
            self.assertEqual(list_snapshots(skill_dir), [3, 4, 5])
            self.assertEqual(prune_snapshots(skill_dir, keep=3), [])
            # The newest snapshot is never removed.
            self.assertEqual(prune_snapshots(skill_dir, keep=1), [3, 4])
            self.assertEqual(list_snapshots(skill_dir), [5])


if __name__ == "__main__":
    unittest.main()
