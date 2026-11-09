import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from thread_to_skill.distill import DistillError, distill_threads
from thread_to_skill.filters import FilterError, MessageFilters, apply_filters, is_bot_author
from thread_to_skill.models import Message, Thread
from thread_to_skill.render import write_skill_bundle


class _OfflineProvider:
    mode = "offline"
    name = "offline"


def make_thread() -> Thread:
    messages = [
        Message(id="m1", ts="2026-09-01T09:00:00Z", author="alice", text="Older context about the deploy."),
        Message(id="m2", ts="2026-10-01T09:00:00Z", author="alice", text="We should retry the failed job."),
        Message(id="m3", ts="2026-10-02T10:00:00Z", author="bob", text="Retry with a longer timeout."),
        Message(id="m4", ts="2026-10-03T11:00:00Z", author="dependabot[bot]", text="Bump the runner to v2."),
        Message(id="m5", ts="2026-10-04T12:00:00Z", author="carol", text="The retry now passes."),
    ]
    return Thread.create(
        source="slack",
        channel="engineering",
        thread_key="t1",
        messages=messages,
        title="Retry the failing job",
    )


class FilterTests(unittest.TestCase):
    def test_since_and_until_are_inclusive(self):
        thread = make_thread()
        filtered, record = apply_filters(
            [thread], MessageFilters(since="2026-10-01", until="2026-10-03")
        )
        self.assertEqual([m.id for m in filtered[0].messages], ["m2", "m3", "m4"])
        self.assertEqual(record["messages_before"], 5)
        self.assertEqual(record["messages_after"], 3)

    def test_participants_match_case_insensitive_substrings(self):
        thread = make_thread()
        filtered, _ = apply_filters([thread], MessageFilters(participants=("ALI",)))
        self.assertEqual([m.id for m in filtered[0].messages], ["m1", "m2"])

    def test_max_messages_keeps_most_recent_in_order(self):
        thread = make_thread()
        filtered, _ = apply_filters([thread], MessageFilters(max_messages=2))
        self.assertEqual([m.id for m in filtered[0].messages], ["m4", "m5"])
        stamps = [m.ts for m in filtered[0].messages]
        self.assertEqual(stamps, sorted(stamps))

    def test_exclude_bots_drops_automation_authors(self):
        thread = make_thread()
        filtered, _ = apply_filters([thread], MessageFilters(exclude_bots=True))
        self.assertNotIn("m4", [m.id for m in filtered[0].messages])

    def test_bot_detection_ignores_lookalike_surnames(self):
        self.assertTrue(is_bot_author("dependabot[bot]"))
        self.assertTrue(is_bot_author("renovate"))
        self.assertFalse(is_bot_author("Margaret Abbot"))
        self.assertFalse(is_bot_author("alice"))

    def test_combined_filters(self):
        thread = make_thread()
        filtered, record = apply_filters(
            [thread],
            MessageFilters(
                since="2026-10-01",
                until="2026-10-04",
                participants=("alice", "carol"),
                max_messages=2,
                exclude_bots=True,
            ),
        )
        self.assertEqual([m.id for m in filtered[0].messages], ["m2", "m5"])
        self.assertTrue(record["applied"])

    def test_empty_result_raises_clear_error(self):
        with self.assertRaises(FilterError) as caught:
            apply_filters([make_thread()], MessageFilters(since="2027-01-01"))
        self.assertIn("No messages left after filtering", str(caught.exception))

    def test_invalid_date_and_reversed_range(self):
        with self.assertRaises(FilterError):
            apply_filters([make_thread()], MessageFilters(since="01/10/2026"))
        with self.assertRaises(FilterError):
            apply_filters(
                [make_thread()], MessageFilters(since="2026-10-05", until="2026-10-01")
            )

    def test_counts_and_provenance_flow_into_the_skill(self):
        thread = make_thread()
        data = distill_threads(
            [thread],
            _OfflineProvider(),
            config=SimpleNamespace(prompts_dir=Path(".")),
            filters=MessageFilters(since="2026-10-01", exclude_bots=True),
        )
        filters = data["provenance"]["filters"]
        self.assertTrue(filters["applied"])
        self.assertEqual(filters["since"], "2026-10-01")
        self.assertEqual(filters["messages_before"], 5)
        self.assertEqual(filters["messages_after"], 3)
        with tempfile.TemporaryDirectory() as temp:
            path, body, metadata = write_skill_bundle(
                Path(temp),
                "retry-the-failing-job",
                data,
                [thread],
                version=1,
                created_at="2026-10-05T00:00:00Z",
                updated_at="2026-10-05T00:00:00Z",
            )
            self.assertIn("Filtered slice: since 2026-10-01", body)
            self.assertIn("bot messages excluded", body)
            self.assertEqual(
                metadata["data"]["provenance"]["filters"]["messages_after"], 3
            )
            self.assertTrue(path.exists())

    def test_distill_rejects_a_filter_that_removes_everything(self):
        with self.assertRaises(DistillError):
            distill_threads(
                [make_thread()],
                _OfflineProvider(),
                config=SimpleNamespace(prompts_dir=Path(".")),
                filters=MessageFilters(participants=("nobody",)),
            )

    def test_inactive_filters_leave_threads_untouched(self):
        thread = make_thread()
        filtered, record = apply_filters([thread], MessageFilters())
        self.assertEqual(filtered[0].message_count, 5)
        self.assertFalse(record["applied"])


if __name__ == "__main__":
    unittest.main()