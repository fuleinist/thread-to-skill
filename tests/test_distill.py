import tempfile
import unittest
from pathlib import Path

from thread_to_skill.adapters import load_path
from thread_to_skill.distill import normalize_data
from thread_to_skill.offline import offline_distill
from thread_to_skill.render import write_skill_bundle
from thread_to_skill.validate import validate_skill_dir

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class DistillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.threads = load_path(FIXTURES / "slack-export")
        cls.data = offline_distill(cls.threads)

    def test_extracts_procedure_and_decisions(self):
        self.assertGreaterEqual(len(self.data["procedure"]), 3)
        self.assertTrue(self.data["decision_rules"])
        self.assertTrue(self.data["verification"])
        self.assertTrue(self.data["confidence"] >= 0.2)

    def test_category_is_engineering(self):
        self.assertTrue(self.data["category"].startswith("engineering/"), self.data["category"])

    def test_tags_are_normalized(self):
        self.assertLessEqual(len(self.data["tags"]), 8)
        for tag in self.data["tags"]:
            self.assertRegex(tag, r"^[a-z0-9][a-z0-9-]*$")

    def test_normalize_adds_description_and_clamps(self):
        data = dict(self.data)
        data["confidence"] = 1.4
        normalized = normalize_data(data, self.threads)
        self.assertLessEqual(len(normalized["description"]), 1024)
        self.assertGreaterEqual(normalized["confidence"], 0.05)
        self.assertLessEqual(normalized["confidence"], 0.95)
        self.assertIn(normalized["category"], {"engineering/testing", "engineering/debugging", "engineering/engineering"})
        self.assertNotEqual(normalized["slug_hint"], "untitled-skill")
        self.assertNotRegex(normalized["title"], r"^\[[^\]]+\]")
        self.assertTrue(normalized["triggers"])
        self.assertNotIn("fail", normalized["triggers"])

    def test_generated_bundle_validates(self):
        normalized = normalize_data(self.data, self.threads)
        with tempfile.TemporaryDirectory() as temp:
            path, body, metadata = write_skill_bundle(
                Path(temp),
                "flaky-integration-tests",
                normalized,
                self.threads,
                version=1,
                created_at="2026-10-31T00:00:00Z",
                updated_at="2026-10-31T00:00:00Z",
            )
            self.assertTrue((path / "SKILL.md").exists())
            self.assertTrue((path / "references" / "thread.md").exists())
            self.assertTrue((path / "metadata.json").exists())
            self.assertEqual(metadata["slug"], "flaky-integration-tests")
            issues = validate_skill_dir(path)
            errors = [issue for issue in issues if issue.level == "error"]
            self.assertEqual(errors, [])
            self.assertIn("## Procedure", body)
            self.assertNotIn("ghp_FAKE", body)


if __name__ == "__main__":
    unittest.main()