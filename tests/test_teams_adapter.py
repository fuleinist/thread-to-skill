import tempfile
import unittest
from pathlib import Path

from thread_to_skill.adapters import detect_format, load_path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class TeamsDetectionTests(unittest.TestCase):
    def test_detects_teams_html(self):
        self.assertEqual(detect_format(FIXTURES / "teams" / "history.html"), "teams")

    def test_detects_teams_graph_json(self):
        self.assertEqual(detect_format(FIXTURES / "teams" / "graph-messages.json"), "teams")

    def test_generic_html_is_not_teams(self):
        self.assertEqual(detect_format(FIXTURES / "html" / "structured.html"), "html")
        self.assertEqual(detect_format(FIXTURES / "html" / "unstructured.html"), "html")

    def test_discord_json_is_not_teams(self):
        self.assertEqual(detect_format(FIXTURES / "discord" / "incidents.json"), "discord")


class TeamsHtmlTests(unittest.TestCase):
    def setUp(self):
        self.threads = load_path(FIXTURES / "teams" / "history.html")
        self.messages = [message for thread in self.threads for message in thread.messages]

    def test_replies_attach_to_the_thread_root(self):
        self.assertEqual(len(self.threads), 2)
        root = self.threads[0]
        self.assertEqual(root.source, "teams")
        self.assertEqual(root.channel, "Project Falcon")
        self.assertEqual(root.thread_key, "history:m-100")
        self.assertEqual(root.message_count, 3)
        self.assertEqual([message.id for message in root.messages], ["m-100", "m-101", "m-102"])
        self.assertIsNone(root.messages[0].reply_to)
        self.assertEqual(root.messages[1].reply_to, "m-100")
        # A reply to a reply resolves to the same root through the chain.
        self.assertEqual(root.messages[2].reply_to, "m-101")
        self.assertEqual(root.messages[0].ts, "2026-11-10T09:05:00Z")

    def test_standalone_messages_share_one_quiet_gap_window(self):
        second = self.threads[1]
        self.assertEqual(second.message_count, 3)
        self.assertEqual([message.id for message in second.messages], ["m-103", "m-104", "m-105"])
        self.assertTrue(second.thread_key.startswith("history:window:m-103:"))
        self.assertEqual(second.extra["window"], 1)

    def test_small_gap_splits_the_standalone_messages(self):
        threads = load_path(FIXTURES / "teams" / "history.html", gap_minutes=1)
        self.assertEqual(len(threads), 4)
        self.assertEqual(sum(thread.message_count for thread in threads), 6)

    def test_authors_are_display_names(self):
        authors = {message.author for message in self.messages}
        self.assertEqual(authors, {"Dana Whitfield", "Priya Shah", "Ravi Menon"})
        for message in self.messages:
            self.assertNotIn("29:", message.author)

    def test_quoted_reply_and_page_chrome_are_dropped(self):
        transcript = "\n".join(message.text for message in self.messages)
        self.assertEqual(transcript.lower().count("nightly rebuild fails"), 1)
        self.assertNotIn("Microsoft Teams", transcript)
        self.assertNotIn("window.__teams", transcript)
        self.assertIn("The cache volume is full.", transcript)

    def test_html_without_containers_keeps_one_document(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "teams-empty.html"
            path.write_text(
                "<html><body><nav>Teams</nav>"
                '<a href="https://teams.microsoft.com/l/message/1">source</a>'
                "<p>No message blocks here.</p></body></html>",
                encoding="utf-8",
            )
            with self.assertWarns(RuntimeWarning):
                threads = load_path(path)
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0].source, "teams")
        self.assertEqual(threads[0].extra["teams_heuristic"], "single-document")


class TeamsGraphTests(unittest.TestCase):
    def setUp(self):
        self.threads = load_path(FIXTURES / "teams" / "graph-messages.json")
        self.messages = [message for thread in self.threads for message in thread.messages]

    def test_reply_chain_maps_to_one_root_thread(self):
        self.assertEqual(len(self.threads), 2)
        root = self.threads[0]
        self.assertEqual(root.thread_key, "graph-messages:1700000000001")
        self.assertEqual(root.message_count, 3)
        self.assertEqual(
            [message.id for message in root.messages],
            ["1700000000001", "1700000000002", "1700000000003"],
        )

    def test_nested_replies_flatten_once(self):
        self.assertEqual(len(self.messages), 5)
        self.assertEqual(len({message.id for message in self.messages}), 5)

    def test_authors_resolve_through_the_participant_list(self):
        authors = {message.author for message in self.messages}
        self.assertEqual(authors, {"Dana Whitfield", "Priya Shah", "Ravi Menon"})
        for message in self.messages:
            self.assertNotIn("29:1", message.author)

    def test_body_html_is_plain_text_and_attachments_survive(self):
        first = next(message for message in self.messages if message.id == "1700000000001")
        self.assertIn("Thursday & stays", first.text)
        self.assertNotIn("<p>", first.text)
        self.assertEqual([item.name for item in first.attachments], ["change-plan.md"])
        self.assertEqual(first.attachments[0].kind, "reference")
        self.assertEqual(first.meta["teams_message_type"], "message")

    def test_unknown_parent_never_invents_a_parent(self):
        orphan = next(message for message in self.messages if message.id == "1700000000005")
        self.assertIn("Unknown parent", orphan.text)
        self.assertEqual(orphan.reply_to, "1700009999999")
        second = self.threads[1]
        self.assertEqual(second.message_count, 2)
        self.assertEqual(
            [message.id for message in second.messages],
            ["1700000000004", "1700000000005"],
        )

    def test_explicit_format_accepts_both_shapes(self):
        html_threads = load_path(FIXTURES / "teams" / "history.html", fmt="teams")
        graph_threads = load_path(FIXTURES / "teams" / "graph-messages.json", fmt="teams")
        self.assertEqual(len(html_threads), 2)
        self.assertEqual(len(graph_threads), 2)
        self.assertEqual({thread.source for thread in html_threads}, {"teams"})


if __name__ == "__main__":
    unittest.main()