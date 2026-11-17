"""Discord forum and thread channel tests.

A forum post or a thread channel describes itself in the channel metadata, so one
file is already one conversation. These tests check that the metadata path wins
over the reply-root and quiet-gap path, that several posts in one file stay
separate, and that ordinary channel exports still use the path they had before.
"""

import json
import tempfile
import unittest
from pathlib import Path

from thread_to_skill.adapters import detect_format, load_path
from thread_to_skill.adapters.discord import _channel_kind, _post_key

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DISCORD = FIXTURES / "discord"


def _write_temp_json(temp_dir: str, name: str, text: str) -> Path:
    path = Path(temp_dir) / name
    path.write_text(text, encoding="utf-8")
    return path


def _by_key(threads):
    return {thread.thread_key: thread for thread in threads}


class DiscordChannelKindTests(unittest.TestCase):
    def test_thread_types_are_threads_after_the_guild_prefix(self):
        for value in (
            "PublicThread",
            "GuildPublicThread",
            "PrivateThread",
            "GuildPrivateThread",
            "AnnouncementThread",
            "GuildAnnouncementThread",
            "thread",
        ):
            self.assertEqual(_channel_kind({"type": value}), "thread")

    def test_forum_and_media_are_forums(self):
        for value in ("Forum", "GuildForum", "Media", "GuildMedia"):
            self.assertEqual(_channel_kind({"type": value}), "forum")

    def test_other_and_missing_types_are_text(self):
        for value in ("TextChannel", "GuildTextChannel", "VoiceChannel", "StageChannel", "", None):
            self.assertEqual(_channel_kind({"type": value}), "text")
        self.assertEqual(_channel_kind({}), "text")


class DiscordPostKeyTests(unittest.TestCase):
    def test_the_first_field_that_names_the_post_wins(self):
        self.assertEqual(_post_key({"threadId": "9310", "reference": {"channelId": "9320"}}), "9310")
        self.assertEqual(_post_key({"thread_id": "9310", "meta": {"thread_id": "9311"}}), "9310")
        self.assertEqual(_post_key({"meta": {"thread_id": "9311"}, "reference": {"channelId": "9320"}}), "9311")
        self.assertEqual(_post_key({"reference": {"channelId": "9320"}}), "9320")

    def test_a_message_that_names_no_post_has_no_post_key(self):
        self.assertEqual(_post_key({"id": "6001"}), "")
        self.assertEqual(_post_key({"meta": {"thread_id": ""}, "reference": {}}), "")


class DiscordThreadChannelTests(unittest.TestCase):
    def setUp(self):
        self.threads = load_path(DISCORD / "forum-thread.json")

    def test_one_thread_channel_file_is_one_thread(self):
        self.assertEqual(len(self.threads), 1)
        thread = self.threads[0]
        self.assertEqual(thread.source, "discord")
        self.assertEqual(thread.thread_key, "9100:thread")
        self.assertEqual(thread.channel, "deploy-window-q4")
        self.assertEqual([message.id for message in thread.messages], ["2001", "2002", "2003", "2004"])

    def test_the_thread_name_becomes_the_title(self):
        self.assertEqual(self.threads[0].title, "deploy-window-q4")

    def test_the_reply_link_survives_inside_the_thread(self):
        self.assertEqual(self.threads[0].messages[2].reply_to, "2001")

    def test_the_quiet_gap_never_splits_a_thread_channel(self):
        threads = load_path(DISCORD / "forum-thread.json", gap_minutes=1)
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0].thread_key, "9100:thread")
        self.assertEqual(threads[0].message_count, 4)

    def test_the_channel_metadata_is_recorded(self):
        extra = self.threads[0].extra
        self.assertEqual(extra["channel_id"], "9100")
        self.assertEqual(extra["channel_type"], "PublicThread")
        self.assertEqual(extra["parent_id"], "9000")
        self.assertTrue(extra["thread"])

    def test_the_title_argument_wins_over_the_thread_name(self):
        threads = load_path(DISCORD / "forum-thread.json", title="Deploy window Q4")
        self.assertEqual(threads[0].title, "Deploy window Q4")
        self.assertEqual(threads[0].channel, "deploy-window-q4")

    def test_the_channel_argument_overrides_the_channel_name(self):
        threads = load_path(DISCORD / "forum-thread.json", channel="release-windows")
        self.assertEqual(threads[0].channel, "release-windows")
        self.assertEqual(threads[0].thread_key, "9100:thread")


class DiscordForumPostTests(unittest.TestCase):
    def setUp(self):
        self.threads = load_path(DISCORD / "forum-post.json")

    def test_one_post_file_is_one_thread(self):
        self.assertEqual(len(self.threads), 1)
        thread = self.threads[0]
        self.assertEqual(thread.thread_key, "9200:thread")
        self.assertEqual(thread.channel, "release-notes")
        self.assertEqual(thread.title, "release-notes")
        self.assertEqual([message.id for message in thread.messages], ["3001", "3002", "3003", "3004", "3005", "3006"])

    def test_a_forum_file_is_never_bucketed_into_windows(self):
        threads = load_path(DISCORD / "forum-post.json", gap_minutes=1)
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0].message_count, 6)

    def test_the_channel_metadata_is_recorded_without_a_parent(self):
        extra = self.threads[0].extra
        self.assertEqual(extra["channel_id"], "9200")
        self.assertEqual(extra["channel_type"], "Forum")
        self.assertIsNone(extra["parent_id"])
        self.assertTrue(extra["thread"])

    def test_a_group_without_a_post_name_falls_back_to_the_channel_and_post_id(self):
        with tempfile.TemporaryDirectory() as temp:
            path = _write_temp_json(
                temp,
                "no-post-name.json",
                json.dumps(
                    {
                        "channel": {"id": "9300", "name": "questions", "type": "Forum"},
                        "messages": [
                            {"id": "6001", "timestamp": "2026-11-10T09:00:00+00:00", "threadId": "9310",
                             "author": {"name": "Bo Kan"}, "content": "First message of the post."},
                            {"id": "6002", "timestamp": "2026-11-10T10:30:00+00:00", "threadId": "9310",
                             "author": {"name": "Dana Cole"}, "content": "A later message in the same post."},
                        ],
                    }
                ),
            )
            threads = load_path(path)
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0].thread_key, "9300:9310")
        self.assertEqual(threads[0].title, "questions post 9310")
        self.assertEqual(threads[0].extra["post_id"], "9310")


class DiscordForumPostsTests(unittest.TestCase):
    def setUp(self):
        self.threads = load_path(DISCORD / "forum-posts.json")

    def test_two_posts_in_one_file_are_two_threads(self):
        self.assertEqual(len(self.threads), 2)
        self.assertEqual({thread.thread_key for thread in self.threads}, {"9300:9310", "9300:9320"})

    def test_the_post_name_becomes_the_title(self):
        by_key = _by_key(self.threads)
        self.assertEqual(by_key["9300:9310"].title, "why did the migration job skip")
        self.assertEqual(by_key["9300:9320"].title, "how do I pin the migration image tag")

    def test_each_post_keeps_only_its_own_messages(self):
        by_key = _by_key(self.threads)
        self.assertEqual([message.id for message in by_key["9300:9310"].messages], ["4001", "4003", "4005", "4007"])
        self.assertEqual([message.id for message in by_key["9300:9320"].messages], ["4002", "4004", "4006", "4008"])

    def test_posts_stay_separate_across_a_small_gap(self):
        threads = load_path(DISCORD / "forum-posts.json", gap_minutes=1)
        self.assertEqual(len(threads), 2)
        self.assertEqual(sum(thread.message_count for thread in threads), 8)

    def test_the_post_key_falls_back_through_the_documented_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            path = _write_temp_json(
                temp,
                "post-keys.json",
                json.dumps(
                    {
                        "channel": {"id": "9400", "name": "misc", "type": "Forum"},
                        "messages": [
                            {"id": "7001", "timestamp": "2026-11-11T09:00:00+00:00", "threadId": "9401",
                             "reference": {"channelId": "9402"}, "author": {"name": "Bo Kan"}, "content": "First."},
                            {"id": "7002", "timestamp": "2026-11-11T09:05:00+00:00", "meta": {"thread_id": "9403"},
                             "reference": {"channelId": "9402"}, "author": {"name": "Bo Kan"}, "content": "Second."},
                            {"id": "7003", "timestamp": "2026-11-11T09:09:00+00:00", "reference": {"channelId": "9404"},
                             "author": {"name": "Bo Kan"}, "content": "Third."},
                        ],
                    }
                ),
            )
            threads = load_path(path)
        by_key = _by_key(threads)
        self.assertEqual(len(threads), 3)
        self.assertIn("9400:9401", by_key)
        self.assertIn("9400:9403", by_key)
        self.assertIn("9400:9404", by_key)
        self.assertEqual(by_key["9400:9401"].extra["post_id"], "9401")

    def test_the_channel_metadata_and_the_post_are_recorded(self):
        for thread in self.threads:
            extra = thread.extra
            self.assertEqual(extra["channel_id"], "9300")
            self.assertEqual(extra["channel_type"], "Forum")
            self.assertIsNone(extra["parent_id"])
            self.assertTrue(extra["thread"])
            self.assertEqual(extra["post_id"], thread.thread_key.split(":")[1])

    def test_the_title_argument_wins_over_the_post_name(self):
        threads = load_path(DISCORD / "forum-posts.json", title="Questions")
        self.assertEqual({thread.title for thread in threads}, {"Questions"})


class DiscordBundleTests(unittest.TestCase):
    def setUp(self):
        self.threads = load_path(DISCORD / "guild-bundle.json")

    def test_the_bundle_shape_is_detected_as_discord(self):
        self.assertEqual(detect_format(DISCORD / "guild-bundle.json"), "discord")

    def test_one_entry_is_one_thread(self):
        self.assertEqual(len(self.threads), 2)
        self.assertEqual({thread.thread_key for thread in self.threads}, {"9400:channel", "9401:channel"})

    def test_the_entry_name_becomes_the_channel_and_the_title(self):
        by_key = _by_key(self.threads)
        self.assertEqual(by_key["9400:channel"].channel, "deploy-notices")
        self.assertEqual(by_key["9400:channel"].title, "deploy-notices")
        self.assertEqual(by_key["9401:channel"].channel, "release-checklist")
        self.assertEqual(by_key["9401:channel"].title, "release-checklist")

    def test_each_entry_keeps_only_its_own_messages(self):
        by_key = _by_key(self.threads)
        self.assertEqual([message.id for message in by_key["9400:channel"].messages], ["5001", "5002", "5003"])
        self.assertEqual([message.id for message in by_key["9401:channel"].messages], ["5101", "5102"])

    def test_the_entry_metadata_is_recorded(self):
        by_key = _by_key(self.threads)
        self.assertEqual(by_key["9400:channel"].extra["channel_id"], "9400")
        self.assertEqual(by_key["9400:channel"].extra["channel_type"], "TextChannel")
        self.assertEqual(by_key["9401:channel"].extra["channel_id"], "9401")

    def test_an_entry_with_no_messages_is_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            path = _write_temp_json(
                temp,
                "empty-entry.json",
                json.dumps(
                    {
                        "guild": {"id": "9001", "name": "Acme"},
                        "channels": [
                            {"id": "9400", "name": "deploy-notices", "type": "TextChannel", "messages": [
                                {"id": "5001", "timestamp": "2026-11-12T01:04:00+00:00",
                                 "author": {"name": "Mira Sol"}, "content": "One notice."}
                            ]},
                            {"id": "9401", "name": "empty-channel", "type": "TextChannel", "messages": []},
                        ],
                    }
                ),
            )
            threads = load_path(path)
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0].thread_key, "9400:channel")

    def test_a_threads_list_is_the_same_shape(self):
        with tempfile.TemporaryDirectory() as temp:
            path = _write_temp_json(
                temp,
                "thread-bundle.json",
                json.dumps(
                    {
                        "guild": {"id": "9001", "name": "Acme"},
                        "threads": [
                            {"id": "9500", "name": "post-one", "type": "Forum", "messages": [
                                {"id": "6001", "timestamp": "2026-11-12T02:00:00+00:00",
                                 "author": {"name": "Bo Kan"}, "content": "First post."}
                            ]},
                            {"id": "9501", "name": "post-two", "type": "Forum", "messages": [
                                {"id": "6002", "timestamp": "2026-11-12T02:04:00+00:00",
                                 "author": {"name": "Dana Cole"}, "content": "Second post."}
                            ]},
                        ],
                    }
                ),
            )
            threads = load_path(path, fmt="discord")
        self.assertEqual(len(threads), 2)
        self.assertEqual({thread.thread_key for thread in threads}, {"9500:channel", "9501:channel"})

    def test_other_json_shapes_are_not_bundles(self):
        with tempfile.TemporaryDirectory() as temp:
            path = _write_temp_json(
                temp,
                "not-a-bundle.json",
                json.dumps({"channels": [{"id": "9400", "name": "deploy-notices"}]}),
            )
            self.assertEqual(detect_format(path), "json")


class DiscordTextChannelTests(unittest.TestCase):
    """Ordinary channel exports keep the reply-root and quiet-gap path."""

    def test_the_incidents_fixture_is_unchanged(self):
        threads = load_path(DISCORD / "incidents.json")
        self.assertEqual(len(threads), 1)
        thread = threads[0]
        self.assertEqual(thread.message_count, 7)
        self.assertEqual(thread.participants, ["Ana Ruiz", "Mira Sol", "Sam Ito"])
        self.assertEqual(thread.thread_key, "9002:1001")
        self.assertEqual(thread.extra["root_id"], "1001")
        self.assertNotIn("thread", thread.extra)
        self.assertNotIn("post_id", thread.extra)

    def test_a_text_channel_still_splits_on_reply_roots_and_gaps(self):
        with tempfile.TemporaryDirectory() as temp:
            path = _write_temp_json(
                temp,
                "text-channel.json",
                json.dumps(
                    {
                        "channel": {"id": "9600", "name": "random", "type": "TextChannel"},
                        "messages": [
                            {"id": "8001", "timestamp": "2026-11-13T09:00:00+00:00",
                             "author": {"name": "Bo Kan"}, "content": "Question about the runbook."},
                            {"id": "8002", "timestamp": "2026-11-13T09:05:00+00:00",
                             "author": {"name": "Dana Cole"}, "content": "Answer: page the on-call first.",
                             "reference": {"messageId": "8001"}},
                            {"id": "8003", "timestamp": "2026-11-13T12:00:00+00:00",
                             "author": {"name": "Bo Kan"}, "content": "Unrelated message after the gap."},
                        ],
                    }
                ),
            )
            threads = load_path(path)
        by_key = _by_key(threads)
        self.assertEqual(len(threads), 2)
        self.assertIn("9600:8001", by_key)
        self.assertIn("9600:window:8003:1", by_key)
        self.assertEqual(by_key["9600:8001"].extra["root_id"], "8001")
        self.assertEqual(by_key["9600:window:8003:1"].extra["window"], 1)
        self.assertEqual(by_key["9600:8001"].messages[1].reply_to, "8001")


if __name__ == "__main__":
    unittest.main()
