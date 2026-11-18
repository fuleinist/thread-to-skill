"""Tests for the read-only MCP server, driven through a stub stdio client."""

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from thread_to_skill import mcp
from thread_to_skill.cli import READ_ONLY_COMMANDS, main
from thread_to_skill.config import Config
from thread_to_skill.models import Skill
from thread_to_skill.store import Store

SKILL_BODY = """---
name: deploy-service
description: Deploy the service safely
---

# Deploy the service

Run the deploy script, then watch the logs.
"""


def make_skill(slug: str, **overrides) -> Skill:
    values = {
        "slug": slug,
        "name": slug.replace("-", " "),
        "title": slug.replace("-", " ").title(),
        "description": "A procedure for deploying the service",
        "summary": "summary",
        "category": "dev",
        "tags": [],
        "triggers": [],
        "version": 1,
        "confidence": 0.8,
        "status": "active",
        "path": "",
        "source_threads": [],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-11-09T00:00:00Z",
        "last_used_at": "2026-11-08T00:00:00Z",
        "suggested": 0,
        "accepted": 0,
        "worked": 0,
        "failed": 0,
        "body": "",
        "metadata": {},
    }
    values.update(overrides)
    return Skill(**values)


class McpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / ".thread2skill"
        self.config = Config.create(self.home, provider="offline")
        self.store = Store(self.config.db_path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    # -- helpers --------------------------------------------------------------

    def make_skill_dir(self, slug: str, body: str = SKILL_BODY) -> Path:
        skill_dir = self.root / "skills" / slug
        skill_dir.mkdir(parents=True, exist_ok=True)
        # write_bytes keeps \r\n intact; write_text would translate it on Windows.
        (skill_dir / "SKILL.md").write_bytes(body.encode("utf-8"))
        return skill_dir

    def add_skill(self, slug: str, body: str = SKILL_BODY, **overrides) -> Skill:
        skill_dir = self.make_skill_dir(slug, body)
        overrides.setdefault("path", str(skill_dir))
        skill = make_skill(slug, **overrides)
        self.store.save_skill(skill)
        return skill

    def call(self, name: str, arguments: dict, msg_id: int = 1) -> dict:
        """One tools/call through a stub client. Returns the JSON-RPC response."""
        stdin = StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }
            )
            + "\n"
        )
        stdout = StringIO()
        code = mcp.serve(self.store, stdin=stdin, stdout=stdout)
        self.assertEqual(code, 0)
        return json.loads(stdout.getvalue().strip().splitlines()[-1])

    def call_payload(self, name: str, arguments: dict) -> object:
        """The decoded tool payload, with the isError flag checked by the caller."""
        response = self.call(name, arguments)
        self.assertIn("result", response, response)
        content = response["result"]["content"]
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]["type"], "text")
        return json.loads(content[0]["text"])

    # -- protocol -------------------------------------------------------------

    def test_initialize_returns_protocol_and_server_info(self):
        stdin = StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
                }
            )
            + "\n"
        )
        stdout = StringIO()
        self.assertEqual(mcp.serve(self.store, stdin=stdin, stdout=stdout), 0)
        result = json.loads(stdout.getvalue().strip())["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["serverInfo"]["name"], "thread-to-skill")
        self.assertIn("tools", result["capabilities"])
        self.assertFalse(result["capabilities"]["tools"]["listChanged"])

    def test_unknown_protocol_version_falls_back(self):
        stdin = StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "1999-01-01"},
                }
            )
            + "\n"
        )
        stdout = StringIO()
        mcp.serve(self.store, stdin=stdin, stdout=stdout)
        result = json.loads(stdout.getvalue().strip())["result"]
        self.assertEqual(result["protocolVersion"], mcp.DEFAULT_PROTOCOL_VERSION)

    def test_notifications_get_no_response(self):
        stdin = StringIO(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        stdout = StringIO()
        self.assertEqual(mcp.serve(self.store, stdin=stdin, stdout=stdout), 0)
        self.assertEqual(stdout.getvalue(), "")

    def test_eof_stops_the_server(self):
        """A server that loops on input() and raises EOFError fails this test."""
        stdin = StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n"
        )
        stdout = StringIO()
        self.assertEqual(mcp.serve(self.store, stdin=stdin, stdout=stdout), 0)
        lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 2, lines)

    def test_unknown_method_is_method_not_found(self):
        response = self.call("nope", {})
        self.assertEqual(response["error"]["code"], -32602)

    def test_unknown_tool_is_invalid_params(self):
        response = self.call("nope", {})
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("Unknown tool", response["error"]["message"])

    def test_unknown_method_returns_32601(self):
        stdin = StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "resources/list"}) + "\n"
        )
        stdout = StringIO()
        mcp.serve(self.store, stdin=stdin, stdout=stdout)
        response = json.loads(stdout.getvalue().strip())
        self.assertEqual(response["error"]["code"], -32601)

    def test_malformed_line_is_parse_error(self):
        stdin = StringIO("{not json\n")
        stdout = StringIO()
        self.assertEqual(mcp.serve(self.store, stdin=stdin, stdout=stdout), 0)
        response = json.loads(stdout.getvalue().strip())
        self.assertEqual(response["error"]["code"], -32700)
        self.assertIsNone(response["id"])

    def test_tools_are_read_only_and_stable(self):
        """Pins the tool surface: names, read-only hints, and no write verb."""
        payload = mcp.tools_list_payload()
        names = [tool["name"] for tool in payload["tools"]]
        self.assertEqual(
            names, ["search_skills", "get_skill", "suggest_skills", "list_skills"]
        )
        for tool in payload["tools"]:
            self.assertTrue(tool["annotations"]["readOnlyHint"], tool["name"])
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertNotRegex(
                tool["name"], r"write|delete|install|archive|edit|create|update|remove"
            )
        # A stored skill must survive a full tools/list round trip untouched.
        self.assertEqual(self.store.count_skills(), 0)

    # -- get_skill ------------------------------------------------------------

    def test_get_skill_reads_the_file_not_the_index(self):
        """Mutation probe: answering from skill.body leaves the sentinel behind."""
        skill = self.add_skill("deploy-service", body=SKILL_BODY)
        self.assertEqual(self.store.get_skill("deploy-service").body, "")
        sentinel = "\n<!-- sentinel on disk only -->\n"
        skill_file = Path(skill.path) / "SKILL.md"
        with skill_file.open("a", encoding="utf-8") as handle:
            handle.write(sentinel)

        payload = self.call_payload("get_skill", {"slug": "deploy-service"})
        self.assertEqual(payload["slug"], "deploy-service")
        self.assertIn("sentinel on disk only", payload["body"])
        self.assertEqual(payload["body"], skill_file.read_bytes().decode("utf-8"))
        self.assertEqual(payload["metadata"]["slug"], "deploy-service")
        self.assertEqual(payload["metadata"]["category"], "dev")

    def test_get_skill_preserves_crlf_bytes(self):
        """A text-mode read would translate the line endings and fail here."""
        original = "---\r\nname: crlf\r\n---\r\n\r\nbody line\r\n"
        skill = self.add_skill("crlf-skill", body=original)
        payload = self.call_payload("get_skill", {"slug": "crlf-skill"})
        self.assertEqual(payload["body"], original)
        self.assertEqual(
            payload["body"], (Path(skill.path) / "SKILL.md").read_bytes().decode("utf-8")
        )

    def test_unknown_slug_is_tool_error(self):
        response = self.call("get_skill", {"slug": "missing-skill"})
        self.assertTrue(response["result"]["isError"])
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertIn("Unknown skill", payload["error"])

    def test_missing_skill_file_is_tool_error(self):
        self.add_skill("gone", path=str(self.root / "skills" / "absent"))
        response = self.call("get_skill", {"slug": "gone"})
        self.assertTrue(response["result"]["isError"])

    # -- suggest_skills -------------------------------------------------------

    def test_suggest_does_not_record_usage(self):
        """Mutation probe: reusing the hook path bumps the counter and adds an event."""
        self.add_skill("deploy-service", triggers=["deploy"])
        before = self.store.get_skill("deploy-service")
        self.assertEqual(before.suggested, 0)
        self.assertEqual(self.store.list_events(limit=50), [])

        payload = self.call_payload("suggest_skills", {"prompt": "deploy the service"})
        self.assertTrue(payload, "the prompt should match the skill")
        self.assertEqual(payload[0]["slug"], "deploy-service")
        self.assertTrue(payload[0]["reasons"])

        after = self.store.get_skill("deploy-service")
        self.assertEqual(after.suggested, 0)
        self.assertEqual(self.store.list_events(limit=50), [])

    def test_suggest_threshold_filters_rows(self):
        """A long prompt dilutes the score, so a high threshold must drop the row."""
        self.add_skill("deploy-service", triggers=["deploy"])
        prompt = (
            "deploy alpha bravo charlie delta echo foxtrot golf hotel india juliet"
        )
        kept = self.call_payload("suggest_skills", {"prompt": prompt, "threshold": 0.2})
        self.assertEqual([row["slug"] for row in kept], ["deploy-service"])
        self.assertLess(kept[0]["score"], 0.9)
        dropped = self.call_payload(
            "suggest_skills", {"prompt": prompt, "threshold": 0.9}
        )
        self.assertEqual(dropped, [])

    def test_empty_prompt_is_tool_error(self):
        response = self.call("suggest_skills", {"prompt": "   "})
        self.assertTrue(response["result"]["isError"])

    # -- list_skills ----------------------------------------------------------

    def test_list_skills_filters_and_hides_archived(self):
        self.add_skill("deploy-service", category="dev")
        self.add_skill("write-tests", category="testing")
        self.add_skill("old-skill", category="testing", status="archived")

        default_rows = self.call_payload("list_skills", {})
        slugs = {row["slug"] for row in default_rows}
        self.assertEqual(slugs, {"deploy-service", "write-tests"})

        testing_rows = self.call_payload("list_skills", {"category": "testing"})
        self.assertEqual({row["slug"] for row in testing_rows}, {"write-tests"})

        archived_rows = self.call_payload("list_skills", {"status": "archived"})
        self.assertEqual({row["slug"] for row in archived_rows}, {"old-skill"})

    def test_list_skills_honours_limit(self):
        for index in range(4):
            self.add_skill(f"skill-{index}")
        rows = self.call_payload("list_skills", {"limit": 2})
        self.assertEqual(len(rows), 2)

    # -- search_skills --------------------------------------------------------

    def test_search_scores_are_present_and_ordered(self):
        self.add_skill("deploy-service", description="Deploy the service")
        self.add_skill("deploy-docs", description="Deploy notes for the service")
        rows = self.call_payload("search_skills", {"query": "deploy"})
        self.assertTrue(rows)
        for row in rows:
            self.assertIn("score", row)
            self.assertEqual(set(row), {"slug", "title", "description", "category", "status", "score"})
        scores = [row["score"] for row in rows]
        self.assertEqual(scores, sorted(scores, reverse=True))

    # -- CLI wiring -----------------------------------------------------------

    def test_serve_requires_the_mcp_flag(self):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--home", str(self.home), "serve"])
        self.assertEqual(code, 1)
        self.assertIn("--mcp", stderr.getvalue() + stdout.getvalue())

    def test_serve_is_read_only_so_it_skips_the_lock(self):
        self.assertIn("serve", READ_ONLY_COMMANDS)


if __name__ == "__main__":
    unittest.main()