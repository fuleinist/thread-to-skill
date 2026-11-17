"""Tests for structured attachments (issue #7).

The old shape stored one string per attachment. The new shape stores a
record with a kind, a name, a URL, and body text. These tests pin both
shapes, the four adapters that fill them, the reference renderer, and the
redaction of signed URLs.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from thread_to_skill.adapters.discord import load_discord
from thread_to_skill.adapters.matrix import load_matrix
from thread_to_skill.adapters.slack import load_slack
from thread_to_skill.adapters.teams import load_teams_json
from thread_to_skill.models import Attachment, Message, Thread
from thread_to_skill.redact import redact_thread
from thread_to_skill.render import render_reference


class AttachmentLoadingTests(unittest.TestCase):
    def test_legacy_string_attachment_loads(self):
        message = Message.from_dict(
            {
                "id": "1",
                "ts": "2026-01-01T00:00:00+00:00",
                "author": "sam",
                "text": "see the runbook",
                "attachments": ["runbook.pdf"],
            }
        )
        self.assertEqual(len(message.attachments), 1)
        attachment = message.attachments[0]
        self.assertEqual(attachment.name, "runbook.pdf")
        self.assertEqual(attachment.url, "")
        self.assertEqual(attachment.kind, "file")

    def test_structured_attachment_round_trips(self):
        original = Attachment(
            kind="image",
            name="diagram.png",
            url="https://files.example/diagram.png",
            text="architecture",
        )
        message = Message(
            id="1",
            ts="2026-01-01T00:00:00+00:00",
            author="sam",
            text="see the diagram",
            attachments=[original],
        )
        restored = Message.from_dict(message.to_dict())
        self.assertEqual(restored.attachments, [original])

    def test_partial_dict_attachment_does_not_raise(self):
        partial = Attachment.from_value({"url": "https://files.example/y.pdf"})
        self.assertEqual(partial.url, "https://files.example/y.pdf")
        self.assertEqual(partial.name, "")
        self.assertEqual(partial.render(), "https://files.example/y.pdf")

    def test_unknown_attachment_value_becomes_empty(self):
        self.assertEqual(Attachment.from_value(None).render(), "")
        self.assertEqual(Attachment.from_value(7).name, "7")

    def test_legacy_thread_file_still_loads(self):
        stored = {
            "id": "thread-1",
            "source": "slack",
            "channel": "engineering",
            "thread_key": "C1:1",
            "title": "deploy",
            "messages": [
                {
                    "id": "1",
                    "ts": "2026-01-01T00:00:00+00:00",
                    "author": "sam",
                    "text": "runbook attached",
                    "attachments": ["runbook.pdf"],
                }
            ],
        }
        thread = Thread.from_dict(stored)
        self.assertEqual(thread.message_count, 1)
        self.assertEqual(thread.messages[0].attachments[0].name, "runbook.pdf")
        reloaded = Thread.from_dict(thread.to_dict())
        self.assertEqual(reloaded.messages[0].attachments[0].name, "runbook.pdf")

    def test_message_render_includes_the_url(self):
        message = Message(
            id="1",
            ts="2026-01-01T00:00:00+00:00",
            author="sam",
            text="runbook",
            attachments=[
                Attachment(kind="file", name="runbook.pdf", url="https://files.example/runbook.pdf")
            ],
        )
        rendered = message.render()
        self.assertIn("runbook.pdf", rendered)
        self.assertIn("https://files.example/runbook.pdf", rendered)


class ReferenceRenderTests(unittest.TestCase):
    def _thread(self, attachments: list[Attachment]) -> Thread:
        message = Message(
            id="1",
            ts="2026-01-01T00:00:00+00:00",
            author="sam",
            text="runbook is here",
            attachments=attachments,
        )
        return Thread.create(
            source="slack",
            channel="engineering",
            thread_key="C1:1",
            messages=[message],
        )

    def test_reference_renders_markdown_link(self):
        thread = self._thread(
            [Attachment(kind="file", name="runbook.pdf", url="https://files.example/runbook.pdf")]
        )
        body = render_reference([thread])
        self.assertIn("- [runbook.pdf](https://files.example/runbook.pdf) (file)", body)

    def test_reference_renders_name_without_url(self):
        thread = self._thread([Attachment(kind="reference", name="plan.md")])
        body = render_reference([thread])
        self.assertIn("- plan.md (reference)", body)
        self.assertNotIn("()", body)


class AttachmentRedactionTests(unittest.TestCase):
    def test_redact_thread_removes_token_from_attachment_url(self):
        url = "https://files.slack.com/files-pri/T1-F1/report.pdf?token=ghp_" + "a" * 30
        thread = Thread.create(
            source="slack",
            channel="engineering",
            thread_key="C1:1",
            messages=[
                Message(
                    id="1",
                    ts="2026-01-01T00:00:00+00:00",
                    author="sam",
                    text="report attached",
                    attachments=[Attachment(kind="file", name="report.pdf", url=url)],
                )
            ],
        )
        redacted, report = redact_thread(thread)
        cleaned = redacted.messages[0].attachments[0]
        self.assertNotIn("ghp_", cleaned.url)
        self.assertIn("[redacted-github-token]", cleaned.url)
        self.assertGreaterEqual(report.total, 1)


class SlackAttachmentTests(unittest.TestCase):
    def test_file_keeps_url_and_image_kind(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "engineering.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "type": "message",
                            "user": "U1",
                            "ts": "1767225600.000100",
                            "text": "diagram attached",
                            "files": [
                                {
                                    "name": "diagram.png",
                                    "title": "Architecture",
                                    "mimetype": "image/png",
                                    "url_private": "https://files.slack.com/files-pri/T1-F1/diagram.png",
                                }
                            ],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            threads = load_slack(path, 45)
        attachment = threads[0].messages[0].attachments[0]
        self.assertEqual(attachment.kind, "image")
        self.assertEqual(attachment.name, "diagram.png")
        self.assertEqual(
            attachment.url, "https://files.slack.com/files-pri/T1-F1/diagram.png"
        )
        self.assertEqual(attachment.text, "Architecture")

    def test_permalink_is_used_when_the_private_url_is_absent(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "engineering.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "type": "message",
                            "user": "U1",
                            "ts": "1767225600.000100",
                            "text": "runbook",
                            "files": [
                                {
                                    "name": "runbook.pdf",
                                    "filetype": "pdf",
                                    "permalink": "https://acme.slack.com/files/U1/runbook.pdf",
                                }
                            ],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            threads = load_slack(path, 45)
        attachment = threads[0].messages[0].attachments[0]
        self.assertEqual(attachment.url, "https://acme.slack.com/files/U1/runbook.pdf")
        self.assertEqual(attachment.kind, "file")


class DiscordAttachmentTests(unittest.TestCase):
    def test_embed_keeps_title_description_and_url(self):
        payload = {
            "channel": {"id": "C1", "name": "incidents", "type": "GuildTextChat"},
            "messages": [
                {
                    "id": "1",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "author": {"name": "sam"},
                    "content": "the runbook is here",
                    "embeds": [
                        {
                            "title": "Failover runbook",
                            "description": "Steps for the failover drill.",
                            "url": "https://docs.example/runbook",
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "incidents.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            threads = load_discord(path, 45)
        attachment = threads[0].messages[0].attachments[0]
        self.assertEqual(attachment.kind, "embed")
        self.assertEqual(attachment.name, "Failover runbook")
        self.assertEqual(attachment.text, "Steps for the failover drill.")
        self.assertEqual(attachment.url, "https://docs.example/runbook")

    def test_file_kind_comes_from_the_content_type(self):
        payload = {
            "channel": {"id": "C1", "name": "incidents", "type": "GuildTextChat"},
            "messages": [
                {
                    "id": "1",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "author": {"name": "sam"},
                    "content": "screenshot",
                    "attachments": [
                        {
                            "fileName": "error.jpg",
                            "url": "https://cdn.example/error.jpg",
                            "content_type": "image/jpeg",
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "incidents.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            threads = load_discord(path, 45)
        attachment = threads[0].messages[0].attachments[0]
        self.assertEqual(attachment.kind, "image")
        self.assertEqual(attachment.name, "error.jpg")
        self.assertEqual(attachment.url, "https://cdn.example/error.jpg")


class TeamsAttachmentTests(unittest.TestCase):
    def test_reference_keeps_name_and_content_url(self):
        payload = [
            {
                "id": "1700000000001",
                "createdDateTime": "2026-11-12T08:00:00Z",
                "messageType": "message",
                "from": {"user": {"id": "29:1abc", "displayName": "Dana Whitfield"}},
                "body": {"contentType": "text", "content": "plan attached"},
                "attachments": [
                    {
                        "name": "plan.md",
                        "contentType": "reference",
                        "contentUrl": "https://contoso.sharepoint.com/plan.md",
                    }
                ],
            }
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "graph.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            threads = load_teams_json(path, 45)
        attachment = threads[0].messages[0].attachments[0]
        self.assertEqual(attachment.name, "plan.md")
        self.assertEqual(attachment.url, "https://contoso.sharepoint.com/plan.md")
        self.assertEqual(attachment.kind, "reference")


class MatrixAttachmentTests(unittest.TestCase):
    def test_image_event_keeps_filename_and_url(self):
        events = [
            {
                "event_id": "$root-1",
                "type": "m.room.message",
                "sender": "@alice:example.org",
                "origin_server_ts": 1794470400000,
                "content": {"body": "deploy window", "msgtype": "m.text"},
            },
            {
                "event_id": "$image-2",
                "type": "m.room.message",
                "sender": "@bob:example.org",
                "origin_server_ts": 1794470460000,
                "content": {
                    "body": "topology.png",
                    "msgtype": "m.image",
                    "filename": "topology.png",
                    "url": "mxc://example.org/topology",
                },
            },
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "room.json"
            path.write_text(json.dumps(events), encoding="utf-8")
            threads = load_matrix(path, 45)
        message = next(
            item for item in threads[0].messages if item.id == "$image-2"
        )
        attachment = message.attachments[0]
        self.assertEqual(attachment.kind, "image")
        self.assertEqual(attachment.name, "topology.png")
        self.assertEqual(attachment.url, "mxc://example.org/topology")


if __name__ == "__main__":
    unittest.main()