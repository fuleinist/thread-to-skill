"""Check that a release tag matches the project version.

Usage:
    python scripts/check_release_version.py v0.1.0

The release workflow runs this before it builds. A tag that does not match
``pyproject.toml`` stops the run before any upload.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:  # Python 3.11 and newer
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None  # type: ignore[assignment]

VERSION_LINE = re.compile(r'^\s*version\s*=\s*"([^"]+)"\s*$', re.MULTILINE)


def read_project_version(pyproject: Path) -> str:
    """Return the version from the [project] table of pyproject.toml."""
    if tomllib is not None:
        with pyproject.open("rb") as handle:
            data = tomllib.load(handle)
        version = data.get("project", {}).get("version")
        if isinstance(version, str) and version:
            return version
        raise ValueError(f"no [project] version in {pyproject}")

    text = pyproject.read_text(encoding="utf-8")
    match = VERSION_LINE.search(text)
    if match is None:
        raise ValueError(f"no [project] version in {pyproject}")
    return match.group(1)


def normalize_tag(tag: str) -> str:
    """Turn refs/tags/v1.2.3 into 1.2.3."""
    value = tag.strip()
    prefix = "refs/tags/"
    if value.startswith(prefix):
        value = value[len(prefix) :]
    if value.startswith("v"):
        value = value[1:]
    return value


def check(tag: str, pyproject: Path) -> str:
    """Return an error message, or an empty string when the tag matches."""
    expected = read_project_version(pyproject)
    actual = normalize_tag(tag)
    if actual != expected:
        return f"tag {tag!r} does not match the project version {expected!r}"
    return ""


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_release_version.py <tag>", file=sys.stderr)
        return 2
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    error = check(argv[1], pyproject)
    if error:
        print(error, file=sys.stderr)
        return 1
    print(f"tag {normalize_tag(argv[1])} matches the project version")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))