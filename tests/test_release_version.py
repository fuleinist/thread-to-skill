"""Tests for scripts/check_release_version.py."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_release_version.py"
SPEC = importlib.util.spec_from_file_location("check_release_version", SCRIPT)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - defensive
    raise RuntimeError(f"cannot load {SCRIPT}")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def write_pyproject(directory: str, body: str) -> Path:
    path = Path(directory) / "pyproject.toml"
    path.write_text(body, encoding="utf-8")
    return path


class NormalizeTagTests(unittest.TestCase):
    def test_plain_version(self) -> None:
        self.assertEqual(release.normalize_tag("0.1.0"), "0.1.0")

    def test_leading_v(self) -> None:
        self.assertEqual(release.normalize_tag("v0.1.0"), "0.1.0")

    def test_full_ref(self) -> None:
        self.assertEqual(release.normalize_tag("refs/tags/v1.2.3"), "1.2.3")

    def test_surrounding_whitespace(self) -> None:
        self.assertEqual(release.normalize_tag("  v1.2.3\n"), "1.2.3")

    def test_only_first_v_is_removed(self) -> None:
        self.assertEqual(release.normalize_tag("vv1.0.0"), "v1.0.0")


class ReadProjectVersionTests(unittest.TestCase):
    def test_reads_project_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_pyproject(tmp, '[project]\nname = "x"\nversion = "2.3.4"\n')
            self.assertEqual(release.read_project_version(path), "2.3.4")

    def test_ignores_version_outside_the_project_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_pyproject(tmp, '[tool.other]\nversion = "9.9.9"\n')
            with self.assertRaises(ValueError):
                release.read_project_version(path)

    def test_rejects_empty_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_pyproject(tmp, '[project]\nversion = ""\n')
            with self.assertRaises(ValueError):
                release.read_project_version(path)


class CheckTests(unittest.TestCase):
    def test_match_returns_empty_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_pyproject(tmp, '[project]\nversion = "0.1.0"\n')
            self.assertEqual(release.check("v0.1.0", path), "")

    def test_mismatch_returns_a_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_pyproject(tmp, '[project]\nversion = "0.1.0"\n')
            message = release.check("v0.2.0", path)
            self.assertIn("0.2.0", message)
            self.assertIn("0.1.0", message)

    def test_prerelease_suffix_must_match_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_pyproject(tmp, '[project]\nversion = "1.0.0"\n')
            self.assertNotEqual(release.check("v1.0.0-rc1", path), "")


class MainTests(unittest.TestCase):
    def test_usage_error(self) -> None:
        self.assertEqual(release.main(["prog"]), 2)

    def test_too_many_arguments(self) -> None:
        self.assertEqual(release.main(["prog", "v1", "extra"]), 2)


if __name__ == "__main__":
    unittest.main()