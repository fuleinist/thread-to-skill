"""Synthesis of several related threads into one skill.

Every case builds threads from the model objects, the way the other tests do.
The conflict cases carry the weight: the point of `--synthesize` is that a
contradiction survives into the bundle instead of being averaged away.
"""

import tempfile
import unittest
from pathlib import Path

from thread_to_skill.config import Config
from thread_to_skill.distill import _complete_synthesis, distill_threads, merge_data, normalize_data
from thread_to_skill.models import Message, Skill, Thread
from thread_to_skill.offline import offline_distill
from thread_to_skill.providers import OfflineProvider
from thread_to_skill.render import render_skill_md, write_skill_bundle
from thread_to_skill.synthesis import (
    cluster_threads,
    similarity,
    step_polarity,
    steps_conflict,
    synthesize,
)

CREATED = "2026-11-14T00:00:00Z"


def build_thread(key, pairs, *, day=1, channel="engineering"):
    messages = [
        Message(
            id=f"{key}-{index}",
            ts=f"2026-11-{day:02d}T09:0{index}:00Z",
            author=author,
            text=text,
        )
        for index, (author, text) in enumerate(pairs)
    ]
    return Thread.create(
        source="slack",
        channel=channel,
        thread_key=key,
        messages=messages,
        title=f"{key} thread",
    )


def retry_thread(key="retry-a", *, day=1):
    return build_thread(
        key,
        [
            ("ana", "We should always retry on 5xx. The fix is to add jitter to the backoff."),
            ("bo", "Make sure to verify the retry count in the metrics dashboard."),
        ],
        day=day,
    )


def retry_thread_variant(key="retry-b", *, day=2):
    return build_thread(
        key,
        [
            ("cy", "We should always retry on 5xx errors. The fix is to add jitter to the backoff delay."),
            ("di", "Make sure to verify the retry count in the metrics dashboard after deploy."),
        ],
        day=day,
    )


def contradiction_thread(key="retry-c", *, day=3):
    return build_thread(
        key,
        [
            ("ed", "Never retry on 5xx."),
            ("fi", "Make sure to verify the retry count in the metrics dashboard."),
        ],
        day=day,
    )


def unrelated_thread(key="ios-release", *, day=4):
    return build_thread(
        key,
        [
            ("gu", "We should write release notes for the iOS build. Make sure the changelog is signed off."),
            ("ha", "Run the store submission check before the freeze window."),
        ],
        day=day,
    )


def bundle_body(data, threads, *, slug="synthesized-retry-policy"):
    with tempfile.TemporaryDirectory() as temp:
        path, body, metadata = write_skill_bundle(
            Path(temp),
            slug,
            data,
            threads,
            version=1,
            created_at=CREATED,
            updated_at=CREATED,
        )
        assert (path / "SKILL.md").exists()
    return body, metadata


class ClusteringTests(unittest.TestCase):
    def test_similar_threads_cluster_and_unrelated_stays_out(self):
        first = retry_thread()
        second = retry_thread_variant()
        other = unrelated_thread()
        self.assertGreater(similarity(first, second), 0.2)
        self.assertLess(similarity(first, other), 0.1)
        clusters = cluster_threads([first, second, other])
        self.assertEqual([[thread.id for thread in cluster] for cluster in clusters][0], [first.id, second.id])
        self.assertEqual(len(clusters), 2)

    def test_single_thread_is_one_cluster(self):
        thread = retry_thread()
        self.assertEqual(len(cluster_threads([thread])), 1)


class PolarityTests(unittest.TestCase):
    def test_polarity_reads_raw_text(self):
        self.assertEqual(step_polarity("Never retry on 5xx"), "negative")
        self.assertEqual(step_polarity("Do not retry on 5xx"), "negative")
        self.assertEqual(step_polarity("Always retry on 5xx"), "positive")
        self.assertEqual(step_polarity("Retry on 5xx"), "neutral")

    def test_conflict_needs_opposite_polarity_and_same_subject(self):
        self.assertTrue(
            steps_conflict(
                {"step": "Always retry on 5xx errors"},
                {"step": "Never retry on 5xx"},
            )
        )
        # Different subject: not a conflict.
        self.assertFalse(
            steps_conflict(
                {"step": "Always log every request"},
                {"step": "Never retry on 5xx"},
            )
        )
        # Same polarity: not a conflict.
        self.assertFalse(
            steps_conflict(
                {"step": "Always retry on 5xx"},
                {"step": "Always retry on 503"},
            )
        )


class SynthesizeTests(unittest.TestCase):
    def test_near_duplicate_threads_merge_into_one_step(self):
        first = retry_thread()
        second = retry_thread_variant()
        steps, conflicts, clusters = synthesize([first, second])
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["threads"], 2)
        self.assertEqual(conflicts, [])
        jitter = [step for step in steps if "jitter" in step["step"].lower()]
        self.assertEqual(len(jitter), 1, [step["step"] for step in steps])
        self.assertEqual(sorted(jitter[0]["source_threads"]), sorted([first.id, second.id]))

    def test_contradiction_is_reported_not_merged(self):
        first = retry_thread()
        second = retry_thread_variant()
        third = contradiction_thread()
        steps, conflicts, clusters = synthesize([first, second, third])
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(conflicts), 1)
        conflict = conflicts[0]
        self.assertIn("retry on 5xx", conflict["step"].lower())
        self.assertIn("retry on 5xx", conflict["conflicts_with"].lower())
        self.assertIn("never", conflict["conflicts_with"].lower())
        # The newer thread is named, because all three carry timestamps.
        self.assertEqual(conflict["newer_thread"], third.id)
        # Both sides survive as their own step.
        texts = [step["step"].lower() for step in steps]
        self.assertTrue(any("always retry" in text for text in texts), texts)
        self.assertTrue(any("never retry" in text for text in texts), texts)

    def test_single_thread_has_no_conflicts_and_full_attribution(self):
        thread = retry_thread()
        steps, conflicts, clusters = synthesize([thread])
        self.assertEqual(conflicts, [])
        self.assertEqual(len(clusters), 1)
        self.assertTrue(steps)
        for step in steps:
            self.assertEqual(step["source_threads"], [thread.id])

    def test_unrelated_threads_stay_separate(self):
        first = retry_thread()
        other = unrelated_thread()
        steps, conflicts, clusters = synthesize([first, other])
        self.assertEqual(len(clusters), 2)
        self.assertEqual(conflicts, [])
        sources = {source for step in steps for source in step["source_threads"]}
        self.assertIn(first.id, sources)
        self.assertIn(other.id, sources)

    def test_missing_timestamps_leave_newer_thread_unknown(self):
        first = retry_thread()
        third = contradiction_thread()
        first.ended_at = ""
        third.ended_at = ""
        steps, conflicts, _ = synthesize([first, third])
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["newer_thread"], "")


class RenderTests(unittest.TestCase):
    def test_conflicts_render_into_the_bundle(self):
        threads = [retry_thread(), retry_thread_variant(), contradiction_thread()]
        data = normalize_data(offline_distill(threads), threads)
        data = _complete_synthesis(data, threads)
        body, metadata = bundle_body(data, threads)
        self.assertIn("## Conflicts", body)
        self.assertIn("conflicts with", body)
        self.assertIn("is the newer thread", body)
        stored = metadata["data"]["conflicts"]
        self.assertEqual(len(stored), 1)
        self.assertEqual(metadata["data"]["synthesis"]["conflicts"], 1)

    def test_steps_render_their_source_threads(self):
        threads = [retry_thread(), retry_thread_variant()]
        data = normalize_data(offline_distill(threads), threads)
        data = _complete_synthesis(data, threads)
        body, _ = bundle_body(data, threads)
        self.assertIn("*Sources:", body)

    def test_single_thread_has_no_conflict_section(self):
        threads = [retry_thread()]
        data = normalize_data(offline_distill(threads), threads)
        data = _complete_synthesis(data, threads)
        body, _ = bundle_body(data, threads)
        self.assertNotIn("## Conflicts", body)


class DefaultModeTests(unittest.TestCase):
    def test_default_distill_gains_no_synthesis_keys(self):
        threads = [retry_thread(), retry_thread_variant(), contradiction_thread()]
        with tempfile.TemporaryDirectory() as temp:
            config = Config(home=Path(temp))
            data = distill_threads(threads, OfflineProvider(), config=config)
        self.assertNotIn("conflicts", data)
        self.assertNotIn("synthesis", data)
        for step in data["procedure"]:
            self.assertNotIn("source_threads", step)
        body = render_skill_md("plain", data, version=1, created_at=CREATED, updated_at=CREATED)
        self.assertNotIn("## Conflicts", body)
        self.assertNotIn("*Sources:", body)

    def test_default_distill_matches_the_offline_extractor(self):
        threads = [retry_thread(), retry_thread_variant()]
        with tempfile.TemporaryDirectory() as temp:
            config = Config(home=Path(temp))
            through_cli = distill_threads(threads, OfflineProvider(), config=config)
        direct = offline_distill(threads)
        self.assertEqual(
            [step["step"] for step in through_cli["procedure"]],
            [step["step"] for step in direct["procedure"]],
        )
        self.assertEqual(through_cli["confidence"], normalize_data(direct, threads)["confidence"])

    def test_prompt_only_carries_synthesis_guidance_on_request(self):
        from thread_to_skill.distill import build_prompt, SCHEMA_HINT

        threads = [retry_thread(), retry_thread_variant()]
        plain = build_prompt(threads)
        synthesized = build_prompt(threads, synthesize=True)
        self.assertNotIn("CLUSTER", plain)
        self.assertNotIn("Synthesis rules", plain)
        self.assertIn("CLUSTER 1", synthesized)
        self.assertIn("Synthesis rules", synthesized)
        # The base schema is still present in both.
        self.assertIn(SCHEMA_HINT, plain)
        self.assertIn(SCHEMA_HINT, synthesized)


class LlmPathTests(unittest.TestCase):
    def test_model_steps_gain_sources_and_model_conflicts_win(self):
        threads = [retry_thread(), retry_thread_variant(), contradiction_thread()]
        model_conflict = {
            "step": "Always retry on 5xx errors",
            "source_threads": [threads[0].id],
            "conflicts_with": "Never retry on 5xx",
            "conflicting_source_threads": [threads[2].id],
            "reason": "the model said so",
            "newer_thread": threads[2].id,
        }
        data = {
            "procedure": [{"step": "Always retry on 5xx errors", "details": ""}],
            "conflicts": [model_conflict],
        }
        result = _complete_synthesis(data, threads)
        self.assertEqual(result["conflicts"][0]["reason"], "the model said so")
        self.assertTrue(result["procedure"][0]["source_threads"])
        self.assertEqual(result["synthesis"]["steps"], 1)

    def test_detected_conflicts_fill_in_when_model_returns_none(self):
        threads = [retry_thread(), contradiction_thread()]
        data = {"procedure": [{"step": "Always retry on 5xx"}]}
        result = _complete_synthesis(data, threads)
        self.assertEqual(len(result["conflicts"]), 1)


class MergeDataTests(unittest.TestCase):
    def test_merge_keeps_both_source_threads_on_the_same_step(self):
        existing = Skill(
            slug="retry-policy",
            name="retry-policy",
            title="Retry policy",
            description="Retry policy",
            summary="Retry policy",
            category="engineering/engineering",
            tags=[],
            triggers=[],
            version=1,
            confidence=0.5,
            status="active",
            path="",
            source_threads=["t1"],
            created_at=CREATED,
            updated_at=CREATED,
            metadata={
                "data": {
                    "title": "Retry policy",
                    "summary": "Retry policy",
                    "category": "engineering/engineering",
                    "tags": [],
                    "triggers": [],
                    "when_to_use": [],
                    "when_not_to_use": [],
                    "procedure": [
                        {"step": "Add jitter to the backoff", "details": "", "evidence": [], "source_threads": ["t1"]}
                    ],
                    "decision_rules": [],
                    "pitfalls": [],
                    "verification": [],
                    "examples": [],
                    "open_questions": [],
                    "confidence": 0.5,
                    "provenance": {"message_ids": [], "participants": [], "thread_ids": ["t1"], "sources": []},
                }
            },
        )
        incoming = {
            "tags": [],
            "triggers": [],
            "when_to_use": [],
            "when_not_to_use": [],
            "procedure": [
                {"step": "Add jitter to the backoff", "details": "", "evidence": [], "source_threads": ["t2"]}
            ],
            "pitfalls": [],
            "decision_rules": [],
            "examples": [],
            "verification": [],
            "open_questions": [],
            "confidence": 0.5,
            "provenance": {"message_ids": [], "thread_ids": ["t2"], "sources": []},
        }
        merged = merge_data(existing, incoming, [retry_thread(), retry_thread_variant()])
        steps = merged["procedure"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(sorted(steps[0]["source_threads"]), ["t1", "t2"])


if __name__ == "__main__":
    unittest.main()