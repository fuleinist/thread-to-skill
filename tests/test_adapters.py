import unittest
import warnings
from pathlib import Path

from thread_to_skill.adapters import detect_format, load_path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class AdapterTests(unittest.TestCase):
    def test_detects_slack_export(self):
        self.assertEqual(detect_format(FIXTURES / "slack-export"), "slack")

    def test_detects_discord_json(self):
        self.assertEqual(detect_format(FIXTURES / "discord" / "incidents.json"), "discord")

    def test_detects_whatsapp_text(self):
        self.assertEqual(detect_format(FIXTURES / "whatsapp" / "release-checklist.txt"), "whatsapp")

    def test_detects_jsonl(self):
        self.assertEqual(detect_format(FIXTURES / "generic" / "threads.jsonl"), "jsonl")

    def test_loads_slack_thread(self):
        threads = load_path(FIXTURES / "slack-export")
        self.assertEqual(len(threads), 1)
        thread = threads[0]
        self.assertEqual(thread.source, "slack")
        self.assertEqual(thread.channel, "engineering")
        self.assertEqual(thread.message_count, 10)
        self.assertEqual(len(thread.participants), 3)
        self.assertEqual(thread.messages[0].author, "Alice Chen")
        self.assertTrue(thread.messages[0].ts.startswith("2026-"))
        self.assertIn("checkout", thread.title.lower())

    def test_loads_discord_thread(self):
        threads = load_path(FIXTURES / "discord" / "incidents.json")
        self.assertEqual(len(threads), 1)
        thread = threads[0]
        self.assertEqual(thread.source, "discord")
        self.assertEqual(thread.channel, "incidents")
        self.assertEqual(thread.message_count, 7)
        self.assertEqual(thread.participants, ["Ana Ruiz", "Mira Sol", "Sam Ito"])

    def test_loads_whatsapp_thread(self):
        threads = load_path(FIXTURES / "whatsapp" / "release-checklist.txt")
        self.assertEqual(len(threads), 1)
        thread = threads[0]
        self.assertEqual(thread.source, "whatsapp")
        self.assertEqual(thread.message_count, 7)
        self.assertEqual(thread.participants, ["Alice", "Ben", "Priya"])
        self.assertIn("release checklist", thread.messages[0].text.lower())

    def test_loads_jsonl_threads(self):
        threads = load_path(FIXTURES / "generic" / "threads.jsonl")
        self.assertEqual(len(threads), 2)
        by_key = {thread.thread_key: thread for thread in threads}
        self.assertIn("oncall-rotation", by_key)
        self.assertIn("key-rotation", by_key)
        self.assertEqual(by_key["oncall-rotation"].message_count, 5)
        self.assertEqual(by_key["key-rotation"].message_count, 4)

    def test_split_on_gap(self):
        threads = load_path(FIXTURES / "whatsapp" / "release-checklist.txt", gap_minutes=1)
        self.assertGreaterEqual(len(threads), 1)
        self.assertEqual(sum(thread.message_count for thread in threads), 7)

    def test_missing_path_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_path(FIXTURES / "nope")

    def test_detects_html(self):
        self.assertEqual(detect_format(FIXTURES / "html" / "structured.html"), "html")
        self.assertEqual(detect_format(FIXTURES / "html" / "unstructured.html"), "html")
        # Detection is by suffix, so an .htm file is the same format.
        self.assertEqual(detect_format((FIXTURES / "html" / "unstructured.html").with_suffix(".htm")), "html")

    def test_loads_html_message_blocks(self):
        threads = load_path(FIXTURES / "html" / "structured.html")
        self.assertGreaterEqual(len(threads), 1)
        messages = [message for thread in threads for message in thread.messages]
        self.assertGreaterEqual(len(messages), 3)
        for thread in threads:
            self.assertEqual(thread.source, "html")
            self.assertEqual(thread.extra["html_heuristic"], "blocks")
            self.assertGreaterEqual(thread.extra["html_block_count"], 3)
        for message in messages:
            self.assertTrue(message.author)
            self.assertTrue(message.ts)
            self.assertTrue(message.ts.startswith("2026-"))
            self.assertTrue(message.text)
            self.assertTrue(message.id.startswith("html-"))
        transcript = "\n".join(message.text for thread in threads for message in thread.messages)
        self.assertNotIn("chrome", transcript.lower())
        self.assertIn("nightly rebuild", transcript.lower())

    def test_unstructured_html_stays_one_best_effort_document(self):
        with self.assertWarns(RuntimeWarning) as caught:
            threads = load_path(FIXTURES / "html" / "unstructured.html")
        self.assertIn("best-effort", str(caught.warning))
        self.assertEqual(len(threads), 1)
        thread = threads[0]
        self.assertEqual(thread.source, "html")
        self.assertEqual(thread.message_count, 1)
        self.assertEqual(thread.extra["html_heuristic"], "single-document")
        self.assertLess(thread.extra["html_block_count"], 3)
        message = thread.messages[0]
        self.assertEqual(message.id, "html-0")
        self.assertEqual(message.ts, "")
        self.assertIn("rebuilding the widget cache", message.text.lower())
        self.assertNotIn("chrome", message.text.lower())


if __name__ == "__main__":
    unittest.main()