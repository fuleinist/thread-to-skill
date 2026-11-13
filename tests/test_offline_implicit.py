"""Implicit decision detection in the offline extractor.

Every case builds one thread from the model objects, the way the other tests do,
and looks only at the decision rules. The heuristic is meant to be conservative,
so the negative cases matter as much as the positive ones.
"""

import unittest
from pathlib import Path

from thread_to_skill.adapters import load_path
from thread_to_skill.distill import normalize_data
from thread_to_skill.models import Message, Thread
from thread_to_skill.offline import offline_distill

FIXTURES = Path(__file__).resolve().parent / "fixtures"

EXPLICIT = 1.0
INFERRED = 0.5
AGREEMENT = "inferred from agreement"
COMMITMENT = "inferred from commitment phrase"

FIXTURE_FILES = (
    "slack-export",
    "generic/threads.jsonl",
    "whatsapp/release-checklist.txt",
    "discord/incidents.json",
    "html/structured.html",
)


def build_thread(pairs, *, thread_key="implicit"):
    messages = [
        Message(
            id=f"m{index}",
            ts=f"2026-11-01T09:{index:02d}:00Z",
            author=author,
            text=text,
        )
        for index, (author, text) in enumerate(pairs)
    ]
    return Thread.create(
        source="slack",
        channel="engineering",
        thread_key=thread_key,
        messages=messages,
        title="Implicit decision check",
    )


def rules_for(pairs) -> list[dict]:
    return offline_distill([build_thread(pairs)])["decision_rules"]


def inferred_only(rules: list[dict]) -> list[dict]:
    return [rule for rule in rules if rule["confidence"] < EXPLICIT]


class CommitmentPhraseTests(unittest.TestCase):
    def test_commitment_phrase_examples_each_yield_one_rule(self):
        # "let's go with" is an existing explicit cue, so that case still reads
        # as an explicit decision; the others are the new inference path.
        cases = [
            ("let's go with the per-worker schema", EXPLICIT),
            ("OK, pin it to 1.4.2 then", INFERRED),
            ("X it is", INFERRED),
            ("ship it with the retry disabled", INFERRED),
        ]
        for text, expected_confidence in cases:
            with self.subTest(text=text):
                rules = rules_for(
                    [
                        ("alice", "what do we want to do about the worker setup here?"),
                        ("bob", text),
                    ]
                )
                self.assertEqual(len(rules), 1, rules)
                self.assertEqual(rules[0]["confidence"], expected_confidence)
                self.assertEqual(sorted(rules[0].keys()), ["because", "confidence", "if", "then"])

    def test_each_listed_phrase_produces_one_rule(self):
        cases = [
            ("we go with the per-worker schema", "go with"),
            ("let's use the shared fixture factory", "shared fixture factory"),
            ("stick with the sqlite store", "stick with"),
            ("the pinned cache dir works for me", "works for me"),
            ("ship it", "ship it"),
            ("do that for the deploy gate", "do that"),
            ("the retry is disabled, sqlite store it is", "it is"),
        ]
        for text, marker in cases:
            with self.subTest(text=text):
                rules = rules_for([("alice", text)])
                self.assertEqual(len(rules), 1, rules)
                self.assertEqual(rules[0]["confidence"], INFERRED)
                self.assertEqual(rules[0]["because"], COMMITMENT)
                self.assertIn(marker, rules[0]["then"].lower())

    def test_negated_commitment_is_not_a_decision(self):
        rules = rules_for([("alice", "we should standardize on uv"), ("bob", "don't do that")])
        self.assertEqual(rules, [])


class ExplicitDecisionTests(unittest.TestCase):
    def test_explicit_cue_keeps_empty_because_and_full_confidence(self):
        rules = rules_for([("alice", "we decided to use the sqlite store")])
        self.assertEqual(len(rules), 1, rules)
        self.assertEqual(rules[0]["because"], "")
        self.assertEqual(rules[0]["confidence"], EXPLICIT)

    def test_inferred_rules_are_appended_after_explicit_ones(self):
        rules = rules_for(
            [
                ("alice", "we decided to use the sqlite store"),
                ("bob", "let's use the shared fixture factory"),
            ]
        )
        self.assertEqual(len(rules), 2, rules)
        self.assertEqual(rules[0]["confidence"], EXPLICIT)
        self.assertEqual(rules[1]["confidence"], INFERRED)
        self.assertEqual(rules[1]["because"], COMMITMENT)

    def test_inferred_rule_is_dropped_when_it_duplicates_an_explicit_one(self):
        rules = rules_for(
            [
                ("alice", "we decided to use the sqlite store"),
                ("bob", "let's use the sqlite store"),
            ]
        )
        self.assertEqual(len(rules), 1, rules)
        self.assertEqual(rules[0]["confidence"], EXPLICIT)

    def test_combined_rules_still_cap_at_five(self):
        pairs = [("alice", "we decided to use the sqlite store")]
        pairs += [
            (author, f"ship it with the {item} disabled")
            for author, item in zip(
                ["bob", "carol", "dave", "erin", "zoe", "yan"],
                ["cache", "queue", "lock", "proxy", "timer", "worker"],
            )
        ]
        rules = rules_for(pairs)
        self.assertEqual(len(rules), 5, rules)
        self.assertEqual(rules[0]["confidence"], EXPLICIT)
        self.assertTrue(all(rule["confidence"] == INFERRED for rule in rules[1:]))


class AgreementTests(unittest.TestCase):
    def test_agreement_from_another_participant_promotes_the_proposal(self):
        rules = rules_for(
            [
                ("alice", "we should keep the fixture factory as-is"),
                ("bob", "sounds good"),
            ]
        )
        self.assertEqual(len(rules), 1, rules)
        self.assertEqual(rules[0]["confidence"], INFERRED)
        self.assertEqual(rules[0]["because"], AGREEMENT)
        self.assertIn("fixture factory", rules[0]["then"].lower())

    def test_agreement_from_the_same_author_does_not_count(self):
        rules = rules_for(
            [
                ("alice", "we should keep the fixture factory as-is"),
                ("alice", "sounds good"),
            ]
        )
        self.assertEqual(rules, [])

    def test_affirmative_with_no_preceding_proposal_produces_nothing(self):
        rules = rules_for([("bob", "sounds good"), ("carol", "the tests pass now")])
        self.assertEqual(rules, [])

    def test_proposal_outside_the_two_message_window_is_not_promoted(self):
        rules = rules_for(
            [
                ("alice", "we should keep the fixture factory as-is"),
                ("carol", "holding until the freeze"),
                ("dave", "the docs are updated"),
                ("bob", "ok"),
            ]
        )
        self.assertEqual(rules, [])

    def test_two_affirmatives_from_different_participants_are_not_a_decision(self):
        rules = rules_for([("bob", "ok"), ("carol", "agreed")])
        self.assertEqual(rules, [])

    def test_all_agreement_words_are_recognized(self):
        for word in ("ok", "okay", "sounds good", "+1", "agreed", "yes", "yep", "sure"):
            with self.subTest(word=word):
                rules = rules_for([("alice", "we should standardize on uv"), ("bob", word)])
                self.assertEqual(len(rules), 1, rules)
                self.assertEqual(rules[0]["because"], AGREEMENT)


class TerseResolutionTests(unittest.TestCase):
    QUESTION = "which flag do we set for the deploy gate?"

    def test_terse_answer_to_a_question_is_detected(self):
        for answer in ("add WIGOLO_GATE=2", "--dist loadscope", "`redis-cli flushall`", "pin it to 1.4.2"):
            with self.subTest(answer=answer):
                rules = rules_for([("alice", self.QUESTION), ("bob", answer)])
                self.assertEqual(len(rules), 1, rules)
                self.assertEqual(rules[0]["confidence"], INFERRED)
                self.assertEqual(rules[0]["because"], COMMITMENT)

    def test_the_same_terse_message_without_a_question_is_not_detected(self):
        for answer in ("add WIGOLO_GATE=2", "--dist loadscope", "`redis-cli flushall`"):
            with self.subTest(answer=answer):
                rules = rules_for([("carol", "the deploy ran"), ("bob", answer)])
                self.assertEqual(rules, [])

    def test_a_long_answer_to_a_question_is_not_a_terse_resolution(self):
        rules = rules_for(
            [
                ("alice", "which flag do we set for the deploy gate?"),
                ("bob", "we keep the gate off for now and add WIGOLO_GATE=2 later when the runner is on 1.4.2 again"),
            ]
        )
        self.assertEqual(rules, [])


class FixturePrecisionTests(unittest.TestCase):
    def test_no_false_positives_on_the_fixture_thread(self):
        threads = load_path(FIXTURES / "slack-export")
        for thread in threads:
            rules = offline_distill([thread])["decision_rules"]
            self.assertTrue(rules, "the fixture thread still yields its explicit decision")
            self.assertEqual(inferred_only(rules), [], thread.title)

    def test_precision_budget_is_one_false_positive_per_fixture_thread(self):
        for name in FIXTURE_FILES:
            threads = load_path(FIXTURES / name)
            self.assertTrue(threads, name)
            for thread in threads:
                rules = offline_distill([thread])["decision_rules"]
                self.assertLessEqual(len(inferred_only(rules)), 1, f"{name}: {thread.title}")

    def test_confidence_survives_normalization(self):
        thread = build_thread([("bob", "ship it with the retry disabled")])
        data = offline_distill([thread])
        normalized = normalize_data(data, [thread])
        rules = normalized["decision_rules"]
        self.assertEqual(len(rules), 1, rules)
        self.assertEqual(rules[0]["confidence"], INFERRED)
        self.assertEqual(rules[0]["because"], COMMITMENT)


if __name__ == "__main__":
    unittest.main()
