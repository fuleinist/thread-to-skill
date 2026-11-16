import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

from thread_to_skill.adapters import detect_format, load_path
from thread_to_skill.adapters.zulip import is_zulip_json_text, render_content
from thread_to_skill.cli import main

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_PATH = FIXTURES / "zulip" / "messages.json"


def _write_temp_json(temp_dir: str, name: str, text: str) -> Path:
    path = Path(temp_dir) / name
    path.write_text(text, encoding="utf-8")
    return path


class ZulipDetectionTests(unittest.TestCase):
    def test_detects_zulip_message_array(self):
        self.assertEqual(detect_format(FIXTURE_PATH), "zulip")

    def test_detects_object_with_messages_list(self):
        with tempfile.TemporaryDirectory() as temp:
            path = _write_temp_json(
                temp,
                "export-envelope.json",
                '{"realm": "example", "messages": ['
                '{"id": 1, "sender_full_name": "Alice Chen", "timestamp": 1794470400,'
                '"content": "hello", "subject": "Release checklist"}]}',
            )
            self.assertEqual(detect_format(path), "zulip")

    def test_marker_scan_needs_all_markers(self):
        self.assertTrue(
            is_zulip_json_text(
                '[{"sender_full_name": "Alice", "subject": "topic",'
                ' "timestamp": 1, "content": "hi"}]'
            )
        )
        self.assertFalse(is_zulip_json_text('[{"sender_full_name": "Alice", "content": "hi"}]'))
        self.assertFalse(is_zulip_json_text('[{"subject": "topic", "timestamp": 1, "content": "hi"}]'))
        self.assertFalse(is_zulip_json_text('[{"sender_full_name": "Alice", "subject": "topic"}]'))

    def test_other_json_formats_are_not_zulip(self):
        self.assertEqual(detect_format(FIXTURES / "teams" / "graph-messages.json"), "teams")
        self.assertEqual(detect_format(FIXTURES / "matrix" / "room-export.json"), "matrix")
        self.assertEqual(detect_format(FIXTURES / "discord" / "incidents.json"), "discord")
        self.assertEqual(detect_format(FIXTURES / "generic" / "threads.jsonl"), "jsonl")


class ZulipContentTests(unittest.TestCase):
    def test_mentions_render_without_markers(self):
        self.assertEqual(render_content("@**Bob Lee** please review"), "@Bob Lee please review")
        self.assertEqual(render_content("@_**Carol Diaz**_ please review"), "@Carol Diaz please review")
        self.assertEqual(render_content("See #**engineering** for details"), "See #engineering for details")

    def test_bold_and_italic_markers_are_removed(self):
        self.assertEqual(render_content("Run in **staging** now"), "Run in staging now")
        self.assertEqual(render_content("*Reopen* if it fires"), "Reopen if it fires")

    def test_fenced_code_keeps_its_markers(self):
        text = render_content("Done. Logs:\n```\n**all green**\n```")
        self.assertIn("```\n**all green**\n```", text)
        self.assertNotIn("**all green**", text.replace("```\n**all green**\n```", ""))

    def test_inline_code_keeps_its_markers(self):
        self.assertEqual(render_content("Run `npm test` now"), "Run `npm test` now")
        self.assertEqual(render_content("Run `**literal**` now"), "Run `**literal**` now")

    def test_links_become_label_and_url(self):
        self.assertEqual(
            render_content("See [Runbook](https://example.com/runbook) now"),
            "See Runbook (https://example.com/runbook) now",
        )

    def test_empty_content_stays_empty(self):
        self.assertEqual(render_content(""), "")


class ZulipThreadTests(unittest.TestCase):
    def setUp(self):
        self.threads = load_path(FIXTURE_PATH)

    def test_two_topics_do_not_merge_inside_one_quiet_gap(self):
        # Topic A ends at 08:02 UTC and topic B starts at 09:00 UTC. The default
        # 60-minute gap would merge them, so topic grouping must win.
        self.assertEqual(len(self.threads), 3)
        titles = {thread.title for thread in self.threads}
        self.assertIn("Release checklist", titles)
        self.assertIn("Incident review", titles)

    def test_topic_becomes_one_thread_with_all_its_messages(self):
        release = next(thread for thread in self.threads if thread.title == "Release checklist")
        self.assertEqual(release.source, "zulip")
        self.assertEqual(release.channel, "engineering")
        self.assertEqual(release.thread_key, "engineering:topic:Release checklist")
        self.assertEqual([message.id for message in release.messages], ["101", "102", "103"])
        self.assertEqual(release.extra["zulip_topic"], "Release checklist")
        self.assertEqual(release.extra["zulip_message_count"], 7)

    def test_subject_is_stored_on_each_message(self):
        release = next(thread for thread in self.threads if thread.title == "Release checklist")
        self.assertEqual({message.meta["zulip_subject"] for message in release.messages}, {"Release checklist"})
        incident = next(thread for thread in self.threads if thread.title == "Incident review")
        self.assertEqual({message.meta["zulip_subject"] for message in incident.messages}, {"Incident review"})

    def test_export_counters_are_reported(self):
        release = next(thread for thread in self.threads if thread.title == "Release checklist")
        self.assertEqual(release.extra["zulip_topic_count"], 5)
        self.assertEqual(release.extra["zulip_untopiced_count"], 2)

    def test_empty_subject_falls_back_to_a_quiet_gap_window(self):
        direct = [thread for thread in self.threads if thread.channel == "direct"]
        self.assertEqual(len(direct), 1)
        self.assertEqual([message.id for message in direct[0].messages], ["301", "302"])
        self.assertEqual(direct[0].thread_key, "direct:window:301:1")
        self.assertEqual(direct[0].title, "Can you take the handover note?")

    def test_small_gap_splits_the_untopiced_messages(self):
        threads = load_path(FIXTURE_PATH, gap_minutes=1)
        self.assertEqual(len(threads), 4)
        direct = [thread for thread in threads if thread.channel == "direct"]
        self.assertEqual(len(direct), 2)
        self.assertEqual(sum(thread.message_count for thread in direct), 2)

    def test_topic_messages_ignore_the_quiet_gap(self):
        threads = load_path(FIXTURE_PATH, gap_minutes=1)
        release = next(thread for thread in threads if thread.title == "Release checklist")
        self.assertEqual([message.id for message in release.messages], ["101", "102", "103"])

    def test_timestamps_are_iso_utc(self):
        release = next(thread for thread in self.threads if thread.title == "Release checklist")
        self.assertEqual(release.started_at, "2026-11-12T08:00:00+00:00")
        for message in release.messages:
            parsed = datetime.fromisoformat(message.ts)
            self.assertIs(parsed.tzinfo, timezone.utc)
            self.assertEqual(parsed.utcoffset(), timedelta(0))
        self.assertEqual(release.ended_at, "2026-11-12T08:02:00+00:00")

    def test_authors_come_from_the_full_name(self):
        release = next(thread for thread in self.threads if thread.title == "Release checklist")
        self.assertEqual([message.author for message in release.messages], ["Alice Chen", "Bob Lee", "Alice Chen"])

    def test_rendered_bodies_keep_code_and_drop_markers(self):
        release = next(thread for thread in self.threads if thread.title == "Release checklist")
        first, second, third = release.messages
        self.assertEqual(first.text, "@Bob Lee please run `npm test` in staging before the tag.")
        self.assertIn("```\n**all green**\n```", second.text)
        self.assertEqual(third.text, "Tag is `v0.7.0`. Runbook (https://example.com/runbook) updated.")

    def test_channel_and_title_overrides_are_used(self):
        threads = load_path(FIXTURE_PATH, channel="release-planning", title="Release planning")
        self.assertEqual({thread.channel for thread in threads}, {"release-planning"})
        self.assertEqual({thread.title for thread in threads}, {"Release planning"})

    def test_object_with_messages_list_loads_the_same_way(self):
        with tempfile.TemporaryDirectory() as temp:
            path = _write_temp_json(
                temp,
                "export-envelope.json",
                '{"realm": "example", "messages": ['
                '{"id": 1, "sender_full_name": "Alice Chen", "timestamp": 1794470400,'
                '"content": "First note.", "subject": "Handover"},'
                '{"id": 2, "sender_full_name": "Bob Lee", "timestamp": 1794470460,'
                '"content": "Second note.", "subject": "Handover"}]}',
            )
            threads = load_path(path)
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0].title, "Handover")
        self.assertEqual([message.id for message in threads[0].messages], ["1", "2"])

    def test_explicit_format_accepts_the_fixture(self):
        threads = load_path(FIXTURE_PATH, fmt="zulip")
        self.assertEqual(len(threads), 3)
        self.assertEqual({thread.source for thread in threads}, {"zulip"})

    def test_empty_export_returns_no_threads(self):
        with tempfile.TemporaryDirectory() as temp:
            path = _write_temp_json(temp, "empty.json", "[]")
            self.assertEqual(load_path(path, fmt="zulip"), [])

    def test_missing_id_gets_a_stable_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            path = _write_temp_json(
                temp,
                "no-id.json",
                '[{"sender_full_name": "Alice Chen", "timestamp": 1794470400,'
                ' "content": "Note without an id.", "subject": "Handover"}]',
            )
            threads = load_path(path)
        self.assertEqual([message.id for message in threads[0].messages], ["zulip-message-0"])


class ZulipCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / ".thread2skill"

    def tearDown(self):
        self.temp.cleanup()

    def _run(self, *args):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--home", str(self.home), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_accepts_the_zulip_format_and_writes_every_thread(self):
        code, _, stderr = self._run("init", "--provider", "offline")
        self.assertEqual(code, 0, stderr)
        code, output, stderr = self._run("ingest", str(FIXTURE_PATH), "--format", "zulip", "--json")
        self.assertEqual(code, 0, stderr)
        payload = json.loads(output)
        self.assertEqual(len(payload["threads"]), 3)
        self.assertEqual({item["status"] for item in payload["threads"]}, {"added"})
        for item in payload["threads"]:
            self.assertTrue((self.home / "threads" / f"{item['id']}.json").exists())

    def test_cli_format_list_includes_matrix(self):
        code, _, stderr = self._run("init", "--provider", "offline")
        self.assertEqual(code, 0, stderr)
        code, output, stderr = self._run(
            "ingest", str(FIXTURES / "matrix" / "room-export.json"), "--format", "matrix", "--json"
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(json.loads(output)["threads"]), 2)

    def test_cli_rejects_an_unknown_format(self):
        with self.assertRaises(SystemExit) as caught:
            self._run("ingest", str(FIXTURE_PATH), "--format", "nope", "--json")
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()