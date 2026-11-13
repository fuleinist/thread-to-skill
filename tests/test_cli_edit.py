import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import thread_to_skill.cli as cli_module
from thread_to_skill.cli import main

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SLACK = FIXTURES / "slack-export"
MARKER = "zebrafish"
BODY_NOTE = "Always check the zebrafish release before you rerun the suite."


class EditTests(unittest.TestCase):
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
        thread_id = json.loads(output)["threads"][0]["id"]
        code, output, _ = self.run_cli("distill", thread_id, "--provider", "offline", "--json")
        self.assertEqual(code, 0)
        return json.loads(output)

    def skill_dir(self, slug):
        return self.home / "skills" / slug

    def skill_md(self, slug):
        return self.skill_dir(slug) / "SKILL.md"

    def show(self, slug):
        code, output, _ = self.run_cli("show", slug, "--json")
        self.assertEqual(code, 0)
        return json.loads(output)

    def search_slugs(self, query):
        code, output, _ = self.run_cli("search", query, "--json")
        self.assertEqual(code, 0)
        return [item["slug"] for item in json.loads(output)]

    def edit_with(self, text_writer, *args):
        """Run `t2s edit` with a stand-in editor that rewrites SKILL.md."""

        def fake_editor(command, path):
            path.write_text(text_writer(path.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")
            return 0

        with mock.patch.object(cli_module, "_run_editor", fake_editor):
            return self.run_cli("edit", *args)

    def test_field_edit_updates_file_version_and_metadata(self):
        skill = self.init_and_distill()
        slug = skill["slug"]
        self.assertEqual(skill["version"], 1)

        code, output, _ = self.run_cli("edit", slug, "--field", f"description=Rewritten for {MARKER} coverage")
        self.assertEqual(code, 0)
        self.assertIn("edited", output)

        text = self.skill_md(slug).read_text(encoding="utf-8")
        self.assertIn(f"Rewritten for {MARKER} coverage", text)

        payload = self.show(slug)
        self.assertEqual(payload["version"], 2)

        metadata = json.loads((self.skill_dir(slug) / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["version"], 2)
        self.assertIn(f"Rewritten for {MARKER} coverage", metadata["data"]["description"])

    def test_field_edit_reindexes_search(self):
        skill = self.init_and_distill()
        slug = skill["slug"]
        self.assertEqual(self.search_slugs(MARKER), [])

        code, _, _ = self.run_cli("edit", slug, "--field", f"description=Rewritten for {MARKER} coverage")
        self.assertEqual(code, 0)

        self.assertIn(slug, self.search_slugs(MARKER))

    def test_body_edit_reindexes_search(self):
        skill = self.init_and_distill()
        slug = skill["slug"]

        code, _, _ = self.edit_with(
            lambda text: text.rstrip("\n") + f"\n\n## Notes\n\n{BODY_NOTE}\n",
            slug,
        )
        self.assertEqual(code, 0)
        self.assertIn(BODY_NOTE, self.skill_md(slug).read_text(encoding="utf-8"))
        self.assertIn(slug, self.search_slugs(MARKER))

    def test_invalid_frontmatter_aborts_and_preserves_bytes(self):
        skill = self.init_and_distill()
        slug = skill["slug"]
        before = self.skill_md(slug).read_bytes()

        code, output, _ = self.edit_with(lambda text: "no frontmatter here\n", slug)

        self.assertEqual(code, 1)
        self.assertIn("frontmatter", output)
        self.assertEqual(self.skill_md(slug).read_bytes(), before)
        self.assertEqual(self.show(slug)["version"], 1)
        self.assertFalse((self.skill_dir(slug) / ".versions").exists())

    def test_no_version_bump_keeps_version(self):
        skill = self.init_and_distill()
        slug = skill["slug"]

        code, _, _ = self.run_cli(
            "edit", slug, "--field", f"description=Typo fix for {MARKER}", "--no-version-bump"
        )

        self.assertEqual(code, 0)
        self.assertIn(f"Typo fix for {MARKER}", self.skill_md(slug).read_text(encoding="utf-8"))
        self.assertEqual(self.show(slug)["version"], 1)

    def test_version_bump_snapshots_the_previous_text(self):
        skill = self.init_and_distill()
        slug = skill["slug"]
        before = self.skill_md(slug).read_text(encoding="utf-8")

        code, _, _ = self.run_cli("edit", slug, "--field", f"description=Rewritten for {MARKER} coverage")
        self.assertEqual(code, 0)

        snapshot = self.skill_dir(slug) / ".versions" / "v1" / "SKILL.md"
        self.assertTrue(snapshot.exists())
        self.assertEqual(snapshot.read_text(encoding="utf-8"), before)

    def test_tag_flag_keeps_existing_tags(self):
        skill = self.init_and_distill()
        slug = skill["slug"]

        code, _, _ = self.run_cli("edit", slug, "--field", "tags=alpha,beta")
        self.assertEqual(code, 0)
        self.assertEqual(self.show(slug)["tags"], ["alpha", "beta"])

        code, _, _ = self.run_cli("edit", slug, "--tag", "zebrafish-tag")
        self.assertEqual(code, 0)

        tags = self.show(slug)["tags"]
        self.assertIn("zebrafish-tag", tags)
        for tag in ("alpha", "beta"):
            self.assertIn(tag, tags)

    def test_unknown_field_is_rejected(self):
        skill = self.init_and_distill()
        slug = skill["slug"]

        code, output, _ = self.run_cli("edit", slug, "--field", "nonsense=1")

        self.assertEqual(code, 1)
        self.assertIn("Unsupported --field key", output)
        self.assertEqual(self.show(slug)["version"], 1)

    def test_editor_failure_leaves_file_unchanged(self):
        skill = self.init_and_distill()
        slug = skill["slug"]
        before = self.skill_md(slug).read_bytes()

        with mock.patch.object(cli_module, "_run_editor", lambda command, path: 2):
            code, _, err = self.run_cli("edit", slug)

        self.assertEqual(code, 1)
        self.assertIn("editor exited with 2", err)
        self.assertEqual(self.skill_md(slug).read_bytes(), before)

    def test_no_change_reports_and_keeps_version(self):
        skill = self.init_and_distill()
        slug = skill["slug"]

        code, output, _ = self.edit_with(lambda text: text, slug)

        self.assertEqual(code, 0)
        self.assertIn("no changes", output)
        self.assertEqual(self.show(slug)["version"], 1)


if __name__ == "__main__":
    unittest.main()