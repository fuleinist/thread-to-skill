import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thread_to_skill.adapters import detect_format, load_path
from thread_to_skill.adapters.matrix import is_matrix_json_text

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_PATH = FIXTURES / "matrix" / "room-export.json"


def _write_temp_json(temp_dir: str, name: str, text: str) -> Path:
    path = Path(temp_dir) / name
    path.write_text(text, encoding="utf-8")
    return path


class MatrixDetectionTests(unittest.TestCase):
    def test_detects_matrix_event_array(self):
        self.assertEqual(detect_format(FIXTURE_PATH), "matrix")

    def test_detects_object_with_messages_list(self):
        with tempfile.TemporaryDirectory() as temp:
            path = _write_temp_json(
                temp,
                "room-envelope.json",
                '{"room_id": "!abc:example.org", "messages": ['
                '{"event_id": "$1", "type": "m.room.message",'
                '"sender": "@alice:example.org", "origin_server_ts": 1794470400000,'
                '"content": {"body": "hello"}}]}',
            )
            self.assertEqual(detect_format(path), "matrix")

    def test_marker_scan_needs_both_markers(self):
        self.assertTrue(is_matrix_json_text('{"messages": [{"type": "m.room.message", "event_id": "$1"}]}'))
        self.assertTrue(is_matrix_json_text('[{"type": "m.room.encrypted", "origin_server_ts": 1}]'))
        self.assertFalse(is_matrix_json_text('{"messages": [{"type": "m.room.member", "event_id": "$1"}]}'))

    def test_other_json_formats_are_not_matrix(self):
        self.assertEqual(detect_format(FIXTURES / "teams" / "graph-messages.json"), "teams")
        self.assertEqual(detect_format(FIXTURES / "discord" / "incidents.json"), "discord")
        self.assertEqual(detect_format(FIXTURES / "generic" / "threads.jsonl"), "jsonl")


class MatrixEventTests(unittest.TestCase):
    def setUp(self):
        self.threads = load_path(FIXTURE_PATH)
        self.messages = [message for thread in self.threads for message in thread.messages]

    def test_only_message_events_become_messages(self):
        self.assertEqual(len(self.threads), 2)
        self.assertEqual(len(self.messages), 6)
        skipped = {"$create-100", "$member-102", "$reaction-105", "$topic-106"}
        for message in self.messages:
            self.assertNotIn(message.id, skipped)
            self.assertEqual(message.meta["matrix_event_type"], "m.room.message")
        self.assertEqual(self.threads[0].extra["matrix_skipped_count"], 4)

    def test_reply_chain_reconstructs_from_in_reply_to(self):
        root = self.threads[0]
        self.assertEqual(root.source, "matrix")
        self.assertEqual(root.channel, "room-export")
        self.assertEqual(root.thread_key, "room-export:$root-101")
        self.assertEqual(root.message_count, 3)
        self.assertEqual([message.id for message in root.messages], ["$root-101", "$reply-103", "$reply-104"])
        self.assertIsNone(root.messages[0].reply_to)
        self.assertEqual(root.messages[1].reply_to, "$root-101")
        # A reply to a reply resolves to the same root through the chain.
        self.assertEqual(root.messages[2].reply_to, "$reply-103")

    def test_unknown_parent_keeps_its_message_in_a_window(self):
        orphan = next(message for message in self.messages if message.id == "$msg-109")
        self.assertIn("Unknown parent", orphan.text)
        self.assertEqual(orphan.reply_to, "$missing-999")
        window = self.threads[1]
        self.assertEqual(window.message_count, 3)
        self.assertEqual(
            [message.id for message in window.messages],
            ["$msg-107", "$msg-108", "$msg-109"],
        )
        self.assertTrue(window.thread_key.startswith("room-export:window:$msg-107:"))

    def test_millisecond_timestamps_become_iso_utc(self):
        expected = {
            "$root-101": "2026-11-12T08:00:00+00:00",
            "$reply-103": "2026-11-12T08:04:30+00:00",
            "$reply-104": "2026-11-12T08:06:00+00:00",
            "$msg-107": "2026-11-12T09:30:15+00:00",
            "$msg-108": "2026-11-12T09:36:45+00:00",
            "$msg-109": "2026-11-12T10:12:30+00:00",
        }
        for message in self.messages:
            self.assertEqual(message.ts, expected[message.id])
            parsed = datetime.fromisoformat(message.ts)
            self.assertIs(parsed.tzinfo, timezone.utc)
            self.assertEqual(parsed.utcoffset(), timedelta(0))
        first = next(message for message in self.messages if message.id == "$root-101")
        self.assertEqual(int(datetime.fromisoformat(first.ts).timestamp() * 1000), 1794470400000)
        self.assertEqual(self.threads[0].started_at, "2026-11-12T08:00:00+00:00")

    def test_sender_maps_to_the_display_name(self):
        authors = {message.author for message in self.messages}
        self.assertEqual(authors, {"alice", "bob", "carol"})
        for message in self.messages:
            self.assertNotIn(":", message.author)
            self.assertNotIn("@", message.author)

    def test_small_gap_splits_the_standalone_messages(self):
        threads = load_path(FIXTURE_PATH, gap_minutes=1)
        self.assertEqual(len(threads), 4)
        self.assertEqual(sum(thread.message_count for thread in threads), 6)

    def test_object_with_messages_list_loads_the_same_way(self):
        with tempfile.TemporaryDirectory() as temp:
            path = _write_temp_json(
                temp,
                "room-envelope.json",
                '{"room_id": "!abc:example.org", "messages": ['
                '{"event_id": "$a1", "type": "m.room.message",'
                '"sender": "@alice:example.org", "origin_server_ts": 1794470400000,'
                '"content": {"body": "Root message."}},'
                '{"event_id": "$b2", "type": "m.room.message",'
                '"sender": "@bob:example.org", "origin_server_ts": 1794470460000,'
                '"content": {"body": "Reply.",'
                ' "m.relates_to": {"m.in_reply_to": {"event_id": "$a1"}}}}]}',
            )
            threads = load_path(path)
        self.assertEqual(len(threads), 1)
        self.assertEqual([message.id for message in threads[0].messages], ["$a1", "$b2"])
        self.assertEqual(threads[0].messages[1].reply_to, "$a1")

    def test_channel_override_is_used(self):
        threads = load_path(FIXTURE_PATH, channel="release-planning")
        self.assertEqual({thread.channel for thread in threads}, {"release-planning"})

    def test_explicit_format_accepts_the_fixture(self):
        threads = load_path(FIXTURE_PATH, fmt="matrix")
        self.assertEqual(len(threads), 2)
        self.assertEqual({thread.source for thread in threads}, {"matrix"})


class MatrixEncryptedTests(unittest.TestCase):
    def test_encrypted_export_raises_an_actionable_error(self):
        with tempfile.TemporaryDirectory() as temp:
            path = _write_temp_json(
                temp,
                "encrypted-export.json",
                '[{"event_id": "$enc-1", "type": "m.room.encrypted",'
                '"sender": "@alice:example.org", "origin_server_ts": 1794470400000,'
                '"content": {"algorithm": "m.megolm.v1.aes-sha2",'
                '"body": "NElXYao9Lk0tV2Y3cGhYNXRSdXZ...", "ciphertext": "abc"}}]',
            )
            self.assertEqual(detect_format(path), "matrix")
            with self.assertRaises(ValueError) as caught:
                load_path(path)
        message = str(caught.exception)
        self.assertIn("decrypt", message.lower())
        self.assertIn("m.room.encrypted", message)
        self.assertIn("m.megolm", message)

    def test_mixed_encrypted_export_fails_instead_of_an_empty_thread(self):
        with tempfile.TemporaryDirectory() as temp:
            path = _write_temp_json(
                temp,
                "partly-encrypted.json",
                '[{"event_id": "$enc-1", "type": "m.room.encrypted",'
                '"sender": "@alice:example.org", "origin_server_ts": 1794470400000,'
                '"content": {"algorithm": "m.megolm.v1.aes-sha2", "ciphertext": "abc"}},'
                '{"event_id": "$msg-1", "type": "m.room.message",'
                '"sender": "@alice:example.org", "origin_server_ts": 1794470460000,'
                '"content": {"body": "Plain body next to a ciphertext."}}]',
            )
            threads = None
            with self.assertRaises(ValueError) as caught:
                threads = load_path(path)
        self.assertIsNone(threads)
        self.assertIn("decrypt", str(caught.exception).lower())

    def test_export_without_events_raises(self):
        with tempfile.TemporaryDirectory() as temp:
            path = _write_temp_json(temp, "empty-export.json", '{"room_id": "!abc:example.org", "messages": []}')
            with self.assertRaises(ValueError):
                load_path(path, fmt="matrix")


if __name__ == "__main__":
    unittest.main()
