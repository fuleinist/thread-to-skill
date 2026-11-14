"""Tests for portable skill bundles: deterministic export and verified import."""

import io
import json
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from thread_to_skill.cli import main

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SLACK = FIXTURES / "slack-export"


def read_members(path: Path) -> list[tuple[str, bytes]]:
    """Every regular member of a bundle, in archive order."""
    members = []
    with tarfile.open(path, mode="r:gz") as archive:
        for info in archive.getmembers():
            if info.isdir():
                continue
            handle = archive.extractfile(info)
            members.append((info.name, handle.read()))
    return members


def rewrite_bundle(
    source: Path,
    target: Path,
    *,
    mutate=None,
    extra: list[tuple[str, bytes]] | None = None,
) -> None:
    """Rebuild a bundle so a test can tamper with one member."""
    members = [
        (name, mutate(name, data) if mutate else data) for name, data in read_members(source)
    ]
    members.extend(extra or [])
    with tarfile.open(target, mode="w:gz") as archive:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / ".thread2skill"
        self.out = self.root / "skills.tar.gz"

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args, home=None):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--home", str(home or self.home), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def seed(self, home=None):
        """A workspace with one offline-distilled skill."""
        home = home or self.home
        for args in (
            ("init",),
            ("ingest", str(SLACK)),
            ("distill", "latest", "--provider", "offline"),
        ):
            code, out, err = self.run_cli(*args, home=home)
            self.assertEqual(code, 0, f"{args} failed: {out}{err}")
        return home

    def list_slugs(self, home=None) -> list[str]:
        code, out, err = self.run_cli("list", "--json", home=home)
        self.assertEqual(code, 0, err)
        return [item["slug"] for item in json.loads(out)]

    def skill_category(self, slug: str, home=None) -> str:
        meta = json.loads((Path(home or self.home) / "skills" / slug / "metadata.json").read_text("utf-8"))
        return str((meta.get("data") or {}).get("category") or "")

    # ---------------------------------------------------------------- export

    def test_export_is_deterministic(self):
        """Two exports of an unchanged library are byte-identical."""
        self.seed()
        first = self.root / "first.tar.gz"
        second = self.root / "second.tar.gz"
        code, out, err = self.run_cli("export", "--out", str(first))
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("export", "--out", str(second))
        self.assertEqual(code, 0, err)
        self.assertEqual(first.read_bytes(), second.read_bytes())

        members = read_members(first)
        self.assertEqual([name for name, _ in members], sorted(name for name, _ in members))
        with tarfile.open(first, mode="r:gz") as archive:
            stamps = {info.mtime for info in archive.getmembers()}
        self.assertEqual(stamps, {0})

    def test_manifest_lists_every_file_with_sha256(self):
        """Every shipped file except the manifest carries a digest and a size."""
        import hashlib

        self.seed()
        code, out, err = self.run_cli("export", "--out", str(self.out))
        self.assertEqual(code, 0, err)

        members = read_members(self.out)
        manifest = json.loads(dict(members)["manifest.json"].decode("utf-8"))
        listed = {entry["path"]: entry for entry in manifest["files"]}

        self.assertEqual(manifest["schema"], "t2s-bundle-v1")
        self.assertEqual(manifest["tool_version"], "0.1.0")
        shipped = {name for name, _ in members} - {"manifest.json"}
        self.assertEqual(set(listed), shipped)
        for name, data in members:
            if name == "manifest.json":
                continue
            self.assertEqual(listed[name]["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(listed[name]["size"], len(data))
        for row in manifest["skills"]:
            self.assertIn(f"skills/{row['slug']}/SKILL.md", listed)

    def test_references_are_opt_in(self):
        """The default bundle carries no transcripts; the flag adds them."""
        self.seed()
        default = self.root / "default.tar.gz"
        code, _, err = self.run_cli("export", "--out", str(default))
        self.assertEqual(code, 0, err)
        names = [name for name, _ in read_members(default)]
        self.assertFalse([name for name in names if "/references/" in name])
        self.assertTrue([name for name in names if name.endswith("/SKILL.md")])

        wide = self.root / "wide.tar.gz"
        code, _, err = self.run_cli("export", "--out", str(wide), "--include-references")
        self.assertEqual(code, 0, err)
        names = [name for name, _ in read_members(wide)]
        self.assertTrue([name for name in names if name.endswith("/references/thread.md")])

    def test_filters_select_requested_skills(self):
        """--slug and --category narrow the bundle and reject unknown input."""
        self.seed()
        slug = self.list_slugs()[0]
        category = self.skill_category(slug)

        code, out, err = self.run_cli("export", "--out", str(self.out), "--slug", slug)
        self.assertEqual(code, 0, err)
        manifest = json.loads(dict(read_members(self.out))["manifest.json"].decode("utf-8"))
        self.assertEqual([row["slug"] for row in manifest["skills"]], [slug])

        code, out, err = self.run_cli("export", "--out", str(self.out), "--category", category)
        self.assertEqual(code, 0, err)
        manifest = json.loads(dict(read_members(self.out))["manifest.json"].decode("utf-8"))
        self.assertEqual([row["slug"] for row in manifest["skills"]], [slug])

        code, out, err = self.run_cli("export", "--out", str(self.out), "--slug", "does-not-exist")
        self.assertEqual(code, 1)
        self.assertIn("unknown skill", out + err)

        code, out, err = self.run_cli("export", "--out", str(self.out), "--category", "design/content")
        if category != "design/content":
            self.assertEqual(code, 1)
            self.assertIn("no skills matched", out + err)

    def test_archived_skill_still_ships(self):
        """Archived skills keep their files, so they stay in the bundle."""
        self.seed()
        slug = self.list_slugs()[0]
        code, _, err = self.run_cli("archive", slug)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.list_slugs(), [])

        code, _, err = self.run_cli("export", "--out", str(self.out))
        self.assertEqual(code, 0, err)
        manifest = json.loads(dict(read_members(self.out))["manifest.json"].decode("utf-8"))
        self.assertEqual([row["slug"] for row in manifest["skills"]], [slug])
        self.assertEqual(manifest["skills"][0]["status"], "archived")

    # ---------------------------------------------------------------- import

    def test_round_trip_imports_a_working_library(self):
        """An imported bundle answers list and suggest."""
        self.seed()
        slug = self.list_slugs()[0]
        code, _, err = self.run_cli("export", "--out", str(self.out))
        self.assertEqual(code, 0, err)

        other = self.root / "other" / ".thread2skill"
        code, out, err = self.run_cli("init", home=other)
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("import-bundle", str(self.out), home=other)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.list_slugs(home=other), [slug])

        code, out, err = self.run_cli("suggest", slug.replace("-", " "), "--format", "json", home=other)
        self.assertEqual(code, 0, err)
        self.assertIn(slug, out)

    def test_tampered_bundle_is_refused(self):
        """One changed byte fails the import and leaves the workspace empty."""
        self.seed()
        code, _, err = self.run_cli("export", "--out", str(self.out))
        self.assertEqual(code, 0, err)

        def mutate(name: str, data: bytes) -> bytes:
            if name.endswith("/SKILL.md"):
                return data.replace(b"## ", b"##  ", 1)
            return data

        tampered = self.root / "tampered.tar.gz"
        rewrite_bundle(self.out, tampered, mutate=mutate)

        other = self.root / "other" / ".thread2skill"
        self.run_cli("init", home=other)
        code, out, err = self.run_cli("import-bundle", str(tampered), home=other)
        self.assertEqual(code, 1)
        self.assertIn("sha256 mismatch", out + err)
        self.assertEqual(self.list_slugs(home=other), [])
        self.assertEqual(list((other / "skills").iterdir()), [])

    def test_unlisted_member_is_refused(self):
        """A member the manifest does not describe fails the import."""
        self.seed()
        code, _, err = self.run_cli("export", "--out", str(self.out))
        self.assertEqual(code, 0, err)
        polluted = self.root / "polluted.tar.gz"
        rewrite_bundle(self.out, polluted, extra=[("skills/extra/notes.txt", b"hello")])

        other = self.root / "other" / ".thread2skill"
        self.run_cli("init", home=other)
        code, out, err = self.run_cli("import-bundle", str(polluted), home=other)
        self.assertEqual(code, 1)
        self.assertIn("unlisted member", out + err)
        self.assertEqual(self.list_slugs(home=other), [])

    def test_path_traversal_is_refused(self):
        """A member that escapes the bundle root fails before anything is written."""
        self.seed()
        code, _, err = self.run_cli("export", "--out", str(self.out))
        self.assertEqual(code, 0, err)
        escaped = self.root / "escaped.tar.gz"
        rewrite_bundle(self.out, escaped, extra=[("skills/../../escape.txt", b"pwned")])

        other = self.root / "other" / ".thread2skill"
        self.run_cli("init", home=other)
        code, out, err = self.run_cli("import-bundle", str(escaped), home=other)
        self.assertEqual(code, 1)
        self.assertIn("unsafe path", out + err)
        self.assertFalse((self.root / "escape.txt").exists())

    def test_conflict_needs_force(self):
        """A second import is refused, and --force replaces instead of duplicating."""
        self.seed()
        slug = self.list_slugs()[0]
        code, _, err = self.run_cli("export", "--out", str(self.out))
        self.assertEqual(code, 0, err)

        other = self.root / "other" / ".thread2skill"
        self.run_cli("init", home=other)
        code, _, err = self.run_cli("import-bundle", str(self.out), home=other)
        self.assertEqual(code, 0, err)

        code, out, err = self.run_cli("import-bundle", str(self.out), home=other)
        self.assertEqual(code, 1)
        self.assertIn("already installed", out + err)

        code, out, err = self.run_cli("import-bundle", str(self.out), "--force", home=other)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.list_slugs(home=other), [slug])


if __name__ == "__main__":
    unittest.main()