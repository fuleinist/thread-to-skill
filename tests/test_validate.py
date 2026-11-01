import tempfile
import unittest
from pathlib import Path

from thread_to_skill.validate import parse_frontmatter, validate_skill_dir

GOOD = """---
name: "example-skill"
description: "A valid skill."
metadata:
  category: "engineering/testing"
  tags: ["testing"]
  confidence: 0.7
---

# Example

## When to use

- Always.

## Procedure

1. Do the thing.
"""


class ValidateTests(unittest.TestCase):
    def test_parse_frontmatter(self):
        frontmatter, body = parse_frontmatter(GOOD)
        self.assertEqual(frontmatter["name"], "example-skill")
        self.assertEqual(frontmatter["metadata"]["category"], "engineering/testing")
        self.assertIn("## Procedure", body)

    def test_good_skill_has_no_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "example-skill"
            path.mkdir()
            (path / "SKILL.md").write_text(GOOD, encoding="utf-8")
            (path / "references").mkdir()
            (path / "references" / "thread.md").write_text("# Source\n", encoding="utf-8")
            (path / "metadata.json").write_text('{"slug": "example-skill"}', encoding="utf-8")
            issues = validate_skill_dir(path)
            self.assertEqual([issue for issue in issues if issue.level == "error"], [])
            self.assertTrue(any(issue.level == "ok" for issue in issues))

    def test_bad_name_and_missing_description(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "Example Skill"
            path.mkdir()
            (path / "SKILL.md").write_text("---\nname: \"Example Skill\"\n---\n\n# X\n", encoding="utf-8")
            issues = validate_skill_dir(path)
            errors = [issue.message for issue in issues if issue.level == "error"]
            self.assertTrue(any("lowercase" in message for message in errors))
            self.assertTrue(any("description" in message for message in errors))

    def test_missing_skill_md(self):
        with tempfile.TemporaryDirectory() as temp:
            issues = validate_skill_dir(Path(temp))
            self.assertEqual(issues[0].level, "error")
            self.assertIn("missing SKILL.md", issues[0].message)

    def test_unredacted_secret_is_flagged(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "leaky-skill"
            path.mkdir()
            body = GOOD.replace("name: \"example-skill\"", "name: \"leaky-skill\"")
            body = body.replace("Do the thing.", "Use ghp_FAKEfakeFAKEfakeFAKEfake1234567890")
            (path / "SKILL.md").write_text(body, encoding="utf-8")
            issues = validate_skill_dir(path)
            self.assertTrue(any("sensitive" in issue.message for issue in issues))


if __name__ == "__main__":
    unittest.main()