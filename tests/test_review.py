import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

from thread_to_skill.cli import main
from thread_to_skill.config import Config
from thread_to_skill.models import Skill
from thread_to_skill.review import (
    REASON_ORDER,
    apply_fixes,
    decision_rules,
    reason_counts,
    review_skills,
)
from thread_to_skill.store import Store

NOW = datetime(2026, 11, 9, 0, 0, tzinfo=timezone.utc)


def make_skill(slug: str, **overrides) -> Skill:
    values = {
        "slug": slug,
        "name": slug.replace("-", " "),
        "title": slug.replace("-", " ").title(),
        "description": "A procedure",
        "summary": "summary",
        "category": "dev",
        "tags": [],
        "triggers": [],
        "version": 1,
        "confidence": 0.8,
        "status": "active",
        "path": f"/tmp/skills/{slug}",
        "source_threads": [],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-11-09T00:00:00Z",
        "last_used_at": "2026-11-08T00:00:00Z",
        "suggested": 0,
        "accepted": 0,
        "worked": 0,
        "failed": 0,
        "body": "",
        "metadata": {},
    }
    values.update(overrides)
    return Skill(**values)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / ".thread2skill"
        self.config = Config.create(self.home, provider="offline")
        self.store = Store(self.config.db_path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def run_cli(self, *args):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--home", str(self.home), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def reasons_for(self, findings, slug):
        for finding in findings:
            if finding.slug == slug:
                return finding.reasons
        return []

    # ------------------------------------------------------------------ stale

    def test_stale_uses_last_used_at_not_updated_at(self):
        self.store.save_skill(
            make_skill(
                "old-but-touched",
                last_used_at="2026-01-01T00:00:00Z",
                updated_at="2026-11-09T00:00:00Z",
            )
        )
        findings = review_skills(self.store, stale_days=90, now=NOW)
        self.assertEqual([finding.slug for finding in findings], ["old-but-touched"])
        self.assertEqual(findings[0].reasons, ["stale"])
        self.assertGreaterEqual(findings[0].age_days, 90)

    def test_stale_falls_back_to_updated_at_when_never_used(self):
        self.store.save_skill(
            make_skill("never-used", last_used_at="", updated_at="2026-01-01T00:00:00Z")
        )
        findings = review_skills(self.store, stale_days=90, now=NOW)
        self.assertIn("stale", self.reasons_for(findings, "never-used"))

    # ---------------------------------------------------------------- failing

    def test_failing_requires_failed_greater_than_worked(self):
        self.store.save_skill(make_skill("even-split", worked=3, failed=3))
        findings = review_skills(self.store, now=NOW)
        self.assertEqual(findings, [])

    def test_needs_review_status_is_failing(self):
        self.store.save_skill(make_skill("flagged", status="needs-review", worked=5, failed=0))
        findings = review_skills(self.store, now=NOW)
        self.assertIn("failing", self.reasons_for(findings, "flagged"))

    # ----------------------------------------------------------------- unused

    def test_unused_requires_suggestion_without_acceptance(self):
        self.store.save_skill(make_skill("untouched", suggested=0, accepted=0))
        findings = review_skills(self.store, now=NOW)
        self.assertEqual(findings, [])

    def test_unused_flags_suggested_but_never_accepted(self):
        self.store.save_skill(make_skill("ignored", suggested=4, accepted=0))
        findings = review_skills(self.store, now=NOW)
        self.assertIn("unused", self.reasons_for(findings, "ignored"))

    # -------------------------------------------------------- low-confidence

    def test_low_confidence_ignores_used_skills(self):
        self.store.save_skill(make_skill("used-and-weak", confidence=0.1, worked=2, failed=0))
        findings = review_skills(self.store, now=NOW)
        self.assertEqual(findings, [])

    def test_low_confidence_flags_unused_weak_skill(self):
        self.store.save_skill(make_skill("weak", confidence=0.1))
        findings = review_skills(self.store, now=NOW)
        self.assertIn("low-confidence", self.reasons_for(findings, "weak"))

    # ------------------------------------------------------------- duplicate

    def test_duplicate_points_the_weaker_skill_at_the_keeper(self):
        self.store.save_skill(make_skill("pnpm-a", name="pnpm installs", confidence=0.9))
        self.store.save_skill(make_skill("pnpm-b", name="pnpm installs", confidence=0.5))
        findings = review_skills(self.store, now=NOW)
        follower = [finding for finding in findings if finding.slug == "pnpm-b"]
        self.assertEqual(len(follower), 1)
        self.assertIn("duplicate", follower[0].reasons)
        self.assertEqual(follower[0].duplicate_of, "pnpm-a")

    # ------------------------------------------------------------ conflicting

    def test_conflict_requires_opposite_polarity(self):
        self.store.save_skill(
            make_skill(
                "pnpm-one",
                name="pnpm installs",
                title="pnpm installs",
                summary="pnpm install policy",
                tags=["pnpm", "install"],
                body="- Always use pnpm for installs.",
            )
        )
        self.store.save_skill(
            make_skill(
                "pnpm-two",
                name="pnpm installs",
                title="pnpm installs",
                summary="pnpm install policy",
                tags=["pnpm", "install"],
                body="- Always use pnpm for outbound installs.",
            )
        )
        findings = review_skills(self.store, now=NOW)
        self.assertFalse(any("conflicting" in finding.reasons for finding in findings))

        self.store.save_skill(
            make_skill(
                "pnpm-two",
                name="pnpm installs",
                title="pnpm installs",
                summary="pnpm install policy",
                tags=["pnpm", "install"],
                body="- Never use pnpm for outbound installs.",
            )
        )
        findings = review_skills(self.store, now=NOW)
        self.assertIn("conflicting", self.reasons_for(findings, "pnpm-one"))
        self.assertIn("conflicting", self.reasons_for(findings, "pnpm-two"))

    def test_decision_rules_extracts_polarity(self):
        rules = decision_rules("- Always use pnpm for installs.\n\nProse line.\n")
        self.assertEqual(len(rules), 1)
        polarity, tokens = rules[0]
        self.assertEqual(polarity, "positive")
        self.assertIn("pnpm", tokens)
        self.assertNotIn("always", tokens)

    # ------------------------------------------------------------------ fixes

    def test_fix_archives_only_needs_review_with_zero_successes(self):
        self.store.save_skill(make_skill("keep-me", status="needs-review", worked=1, failed=2))
        self.store.save_skill(make_skill("archive-me", status="needs-review", worked=0, failed=3))
        findings = review_skills(self.store, now=NOW)
        applied = apply_fixes(self.store, findings)
        self.assertEqual(applied["archived"], ["archive-me"])
        self.assertEqual(self.store.get_skill("archive-me").status, "archived")
        self.assertEqual(self.store.get_skill("keep-me").status, "needs-review")

    def test_fix_never_deletes_and_never_merges(self):
        body_one = "- Always use pnpm for installs.\n\nLong body one."
        body_two = "- Never use pnpm for installs.\n\nLong body two."
        self.store.save_skill(
            make_skill(
                "dup-one",
                name="pnpm installs",
                title="pnpm installs",
                summary="pnpm install policy",
                tags=["pnpm", "install"],
                body=body_one,
                confidence=0.9,
            )
        )
        self.store.save_skill(
            make_skill(
                "dup-two",
                name="pnpm installs",
                title="pnpm installs",
                summary="pnpm install policy",
                tags=["pnpm", "install"],
                body=body_two,
                confidence=0.4,
            )
        )
        before = {slug: self.store.get_skill(slug).body for slug in ("dup-one", "dup-two")}
        findings = review_skills(self.store, now=NOW)
        applied = apply_fixes(self.store, findings, allow_duplicate_groups=True)
        self.assertEqual(applied["archived"], [])
        self.assertEqual(self.store.count_skills(), 2)
        for slug, body in before.items():
            self.assertEqual(self.store.get_skill(slug).body, body)
        grouped = {item["slug"]: item["duplicate_of"] for item in applied["grouped"]}
        self.assertEqual(grouped, {"dup-two": "dup-one"})
        self.assertEqual(
            self.store.get_skill("dup-two").metadata["review"]["duplicate_of"], "dup-one"
        )

    def test_fix_without_yes_does_not_group(self):
        self.store.save_skill(make_skill("dup-x", name="pnpm installs", confidence=0.9))
        self.store.save_skill(make_skill("dup-y", name="pnpm installs", confidence=0.4))
        code, output, _ = self.run_cli("review", "--fix", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["applied"]["grouped"], [])
        self.assertNotIn("review", self.store.get_skill("dup-y").metadata)

    # ------------------------------------------------------------------- json

    def test_json_is_stable(self):
        self.store.save_skill(make_skill("stale-one", last_used_at="2026-01-01T00:00:00Z"))
        code, output, _ = self.run_cli("review", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(
            set(payload),
            {
                "checked",
                "stale_days",
                "confidence_threshold",
                "duplicate_threshold",
                "conflict_threshold",
                "counts",
                "findings",
                "applied",
            },
        )
        self.assertEqual(list(payload["counts"]), list(REASON_ORDER))
        self.assertEqual(
            set(payload["findings"][0]),
            {
                "slug",
                "name",
                "status",
                "reasons",
                "age_days",
                "confidence",
                "worked",
                "failed",
                "suggested",
                "accepted",
                "detail",
                "action",
            },
        )
        self.assertEqual(payload["applied"], {"archived": [], "grouped": []})

    def test_reason_counts_include_zeroes_in_order(self):
        counts = reason_counts([])
        self.assertEqual(list(counts), list(REASON_ORDER))
        self.assertTrue(all(value == 0 for value in counts.values()))

    # ----------------------------------------------------------------- config

    def test_stale_threshold_flag_overrides_config(self):
        self.config.data["review"] = {"stale_days": 90}
        self.config.save()
        self.store.save_skill(make_skill("thirty-days", last_used_at="2026-10-10T00:00:00Z"))
        code, output, _ = self.run_cli("review", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["findings"], [])
        code, output, _ = self.run_cli("review", "--stale-days", "10", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["stale_days"], 10)
        self.assertEqual(payload["findings"][0]["reasons"], ["stale"])

    def test_config_defaults_apply_when_flags_are_absent(self):
        code, output, _ = self.run_cli("review", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["stale_days"], 90)
        self.assertEqual(payload["confidence_threshold"], 0.4)

    # ------------------------------------------------------------- boundaries

    def test_archived_skills_are_skipped(self):
        self.store.save_skill(
            make_skill("gone", status="archived", last_used_at="2026-01-01T00:00:00Z")
        )
        findings = review_skills(self.store, now=NOW)
        self.assertEqual(findings, [])

    def test_empty_workspace_prints_hint(self):
        code, output, _ = self.run_cli("review")
        self.assertEqual(code, 0)
        self.assertIn("No skills to review", output)

    def test_healthy_library_has_no_findings(self):
        self.store.save_skill(
            make_skill("healthy", confidence=0.9, worked=5, failed=1, accepted=3, suggested=3)
        )
        code, output, _ = self.run_cli("review")
        self.assertEqual(code, 0)
        self.assertIn("No skills need review", output)


if __name__ == "__main__":
    unittest.main()