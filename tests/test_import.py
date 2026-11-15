"""Tests for `t2s import`: adopting existing third-party skills."""

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from thread_to_skill.cli import main

STANDARD_BODY = """## When to use

- A deployment to the cluster fails and the previous revision must return.

## Procedure

1. Read the rollout status.
2. Roll back to the previous revision.
3. Confirm that the pods are healthy.
"""


def write_skill(
    root: Path,
    name: str,
    *,
    description: str = "Roll back a failed Kubernetes deployment to the previous revision.",
    body: str = STANDARD_BODY,
    frontmatter_extra: str = "",
) -> Path:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        "---\n"
        f'name: "{name}"\n'
        f'description: "{description}"\n'
        f"{frontmatter_extra}"
        "---\n\n"
        f"# {name.replace('-', ' ').title()}\n\n"
        f"{body}",
        encoding="utf-8",
    )
    return path


def tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home" / ".thread2skill"
        self.source = self.root / "third-party"
        self.source.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--home", str(self.home), *args])
        return code, out.getvalue(), err.getvalue()

    def init(self):
        code, out, err = self.run_cli("init")
        self.assertEqual(code, 0, out + err)

    def list_json(self):
        code, out, err = self.run_cli("list", "--json")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_import_copies_and_indexes_skill(self):
        self.init()
        write_skill(self.source, "k8s-rollback")
        code, out, err = self.run_cli("import", str(self.source))
        self.assertEqual(code, 0, out + err)
        target = self.home / "skills" / "k8s-rollback"
        self.assertTrue((target / "SKILL.md").exists())
        metadata = json.loads((target / "metadata.json").read_text("utf-8"))
        self.assertEqual(metadata["source"], "imported")
        self.assertEqual(metadata["threads"], [])
        self.assertTrue(metadata["data"]["category"])
        self.assertTrue(metadata["data"]["tags"])
        self.assertIsNone(metadata["data"]["confidence"])
        self.assertIn("k8s-rollback", [item["slug"] for item in self.list_json()])

    def test_imported_skill_appears_in_suggest(self):
        self.init()
        write_skill(self.source, "k8s-rollback")
        code, out, err = self.run_cli("import", str(self.source))
        self.assertEqual(code, 0, out + err)
        code, out, err = self.run_cli(
            "suggest", "the kubernetes deployment rollout failed, roll back", "--format", "json"
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertIn("k8s-rollback", [item["slug"] for item in payload["suggestions"]])

    def test_parent_directory_imports_each_child(self):
        self.init()
        write_skill(self.source, "alpha-skill", description="Alpha procedure.")
        write_skill(self.source, "beta-skill", description="Beta procedure.")
        code, out, err = self.run_cli("import", str(self.source))
        self.assertEqual(code, 0, out + err)
        slugs = [item["slug"] for item in self.list_json()]
        self.assertEqual(sorted(slugs), ["alpha-skill", "beta-skill"])

    def test_invalid_skill_is_rejected_with_validator_errors(self):
        self.init()
        path = self.source / "Bad Name"
        path.mkdir()
        (path / "SKILL.md").write_text(
            '---\nname: "Bad Name"\n---\n\n# Bad\n\nA body without a description.\n',
            encoding="utf-8",
        )
        code, out, err = self.run_cli("import", str(self.source))
        self.assertEqual(code, 1)
        self.assertIn("lowercase", out)
        self.assertIn("description", out)
        self.assertEqual([item["slug"] for item in self.list_json()], [])

    def test_link_leaves_the_source_untouched(self):
        self.init()
        skill_dir = write_skill(self.source, "linked-skill")
        before = tree_snapshot(self.source)
        code, out, err = self.run_cli("import", str(self.source), "--link")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(tree_snapshot(self.source), before)
        self.assertFalse((skill_dir / "metadata.json").exists())
        rows = {item["slug"]: item for item in self.list_json()}
        self.assertEqual(Path(rows["linked-skill"]["path"]).resolve(), skill_dir.resolve())

    def test_reimport_is_idempotent(self):
        self.init()
        write_skill(self.source, "k8s-rollback")
        first_code, first_out, first_err = self.run_cli("import", str(self.source))
        self.assertEqual(first_code, 0, first_out + first_err)
        second_code, second_out, second_err = self.run_cli("import", str(self.source))
        self.assertEqual(second_code, 0, second_out + second_err)
        slugs = [item["slug"] for item in self.list_json()]
        self.assertEqual(slugs.count("k8s-rollback"), 1)
        self.assertEqual(len(slugs), 1)

    def test_dry_run_writes_nothing(self):
        self.init()
        write_skill(self.source, "k8s-rollback")
        code, out, err = self.run_cli("import", str(self.source), "--dry-run")
        self.assertEqual(code, 0, out + err)
        self.assertIn("would import", out)
        self.assertFalse((self.home / "skills" / "k8s-rollback").exists())
        self.assertEqual(self.list_json(), [])

    def test_validate_accepts_imported_skill(self):
        self.init()
        write_skill(self.source, "k8s-rollback")
        code, out, err = self.run_cli("import", str(self.source))
        self.assertEqual(code, 0, out + err)
        code, out, err = self.run_cli("validate", "--all")
        self.assertEqual(code, 0, out + err)
        self.assertIn("0 error(s)", out)
        self.assertNotIn("references/thread.md", out)
        self.assertNotIn("metadata.confidence", out)

    def test_conflict_needs_force(self):
        self.init()
        write_skill(self.source, "k8s-rollback")
        code, out, err = self.run_cli("import", str(self.source))
        self.assertEqual(code, 0, out + err)
        generated = self.home / "skills" / "k8s-rollback" / "metadata.json"
        payload = json.loads(generated.read_text("utf-8"))
        payload.pop("source")
        generated.write_text(json.dumps(payload), encoding="utf-8")
        code, out, err = self.run_cli("import", str(self.source))
        self.assertEqual(code, 1)
        self.assertIn("already exists", out)
        code, out, err = self.run_cli("import", str(self.source), "--force")
        self.assertEqual(code, 0, out + err)

    def test_missing_path_reports_an_error(self):
        self.init()
        code, out, err = self.run_cli("import", str(self.root / "absent"))
        self.assertEqual(code, 1)
        self.assertIn("does not exist", out)


if __name__ == "__main__":
    unittest.main()