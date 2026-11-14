import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from thread_to_skill import usage
from thread_to_skill.distill import DistillError, distill_threads
from thread_to_skill.models import Message, Thread

PAYLOAD = {"title": "Retry the job", "summary": "Retry with a longer timeout."}


class _LLMProvider:
    """A reporter that either owns usage counts or reports none at all."""

    mode = "llm"

    def __init__(self, *, name: str = "openai", model: str = "gpt-4o-mini", last_usage=None):
        self.name = name
        self.model = model
        self.last_usage = last_usage
        self.calls = 0

    def complete(self, system: str, prompt: str, *, max_tokens: int = 4096, timeout: int = 120) -> str:
        self.calls += 1
        return json.dumps(PAYLOAD)


def make_thread() -> Thread:
    messages = [
        Message(id="m1", ts="2026-11-01T09:00:00Z", author="alice", text="We should retry the failed job."),
        Message(id="m2", ts="2026-11-01T09:05:00Z", author="bob", text="Retry it with a longer timeout."),
    ]
    return Thread.create(source="slack", channel="ops", thread_key="t31", messages=messages, title="Retry the job")


class CaptureTests(unittest.TestCase):
    def test_reported_usage_is_not_estimated(self):
        provider = _LLMProvider(last_usage={"prompt_tokens": 1200, "completion_tokens": 300})
        record = usage.capture(provider, "gpt-4o-mini", "prompt text")
        self.assertFalse(record["estimated"])
        self.assertEqual(record["prompt_tokens"], 1200)
        self.assertEqual(record["completion_tokens"], 300)
        self.assertEqual(record["model"], "gpt-4o-mini")
        self.assertEqual(record["provider"], "openai")

    def test_missing_usage_is_estimated_and_never_invented(self):
        provider = _LLMProvider(last_usage=None)
        prompt = "x" * 400
        record = usage.capture(provider, "gpt-4o-mini", prompt)
        self.assertTrue(record["estimated"])
        self.assertEqual(record["prompt_tokens"], usage.estimate_tokens(prompt))
        self.assertEqual(record["completion_tokens"], 0)

    def test_estimate_tokens_has_a_floor_of_one(self):
        self.assertEqual(usage.estimate_tokens(""), 1)
        self.assertEqual(usage.estimate_tokens("abcd"), 1)


class CostTests(unittest.TestCase):
    def test_known_model_reports_a_positive_cost(self):
        result = usage.estimate_cost("gpt-4o-mini", 1_000_000, 1_000_000)
        self.assertEqual(result["status"], "known")
        self.assertAlmostEqual(result["usd"], 0.75, places=6)
        self.assertEqual(result["priced_as"], "gpt-4o-mini")

    def test_dated_model_id_is_priced_as_its_base_model(self):
        result = usage.estimate_cost("gpt-4o-mini-2024-07-18", 1_000_000, 0)
        self.assertEqual(result["status"], "known")
        self.assertEqual(result["priced_as"], "gpt-4o-mini")

    def test_unknown_model_is_unknown_and_never_zero(self):
        result = usage.estimate_cost("mystery-model-9", 1000, 1000)
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["usd"])

    def test_price_table_is_dated(self):
        self.assertRegex(usage.PRICE_TABLE_LAST_UPDATED, r"^\d{4}-\d{2}-\d{2}$")


class DistillUsageTests(unittest.TestCase):
    def test_usage_and_cost_land_on_the_distilled_data(self):
        provider = _LLMProvider(last_usage={"prompt_tokens": 2000, "completion_tokens": 500})
        with tempfile.TemporaryDirectory() as temp:
            data = distill_threads([make_thread()], provider, config=SimpleNamespace(prompts_dir=Path(temp)))
        self.assertFalse(data["usage"]["estimated"])
        self.assertEqual(data["usage"]["prompt_tokens"], 2000)
        self.assertEqual(data["cost"]["status"], "known")
        self.assertGreater(data["cost"]["usd"], 0)

    def test_max_cost_blocks_before_the_request(self):
        provider = _LLMProvider(model="gpt-4o-mini")
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(DistillError) as caught:
                distill_threads(
                    [make_thread()],
                    provider,
                    config=SimpleNamespace(prompts_dir=Path(temp)),
                    max_cost=0.0000001,
                )
        self.assertEqual(provider.calls, 0)
        message = str(caught.exception)
        self.assertIn("max-cost", message)
        self.assertIn("USD", message)

    def test_unknown_price_does_not_block_the_request(self):
        provider = _LLMProvider(model="mystery-model-9")
        with tempfile.TemporaryDirectory() as temp:
            data = distill_threads(
                [make_thread()],
                provider,
                config=SimpleNamespace(prompts_dir=Path(temp)),
                max_cost=0.01,
            )
        self.assertEqual(provider.calls, 1)
        self.assertEqual(data["usage"]["model"], "mystery-model-9")
        self.assertEqual(data["cost"]["status"], "unknown")
        self.assertIsNone(data["cost"]["usd"])


if __name__ == "__main__":
    unittest.main()