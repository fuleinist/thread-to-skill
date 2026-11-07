import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path

from thread_to_skill.cli import main

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SLACK = FIXTURES / "slack-export"


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / ".thread2skill"
        self.project = self.root / "project"
        self.project.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--home", str(self.home), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def stored_ingested_at(self, thread_id: str) -> str:
        conn = sqlite3.connect(str(self.home / "index.db"))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT ingested_at FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
        finally:
            conn.close()
        return row["ingested_at"]

    def stored_content_hash(self, thread_id: str) -> str:
        conn = sqlite3.connect(str(self.home / "index.db"))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT content_hash FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
        finally:
            conn.close()
        return row["content_hash"]

    def force_stored_ingested_at(self, thread_id: str, value: str) -> None:
        conn = sqlite3.connect(str(self.home / "index.db"))
        try:
            conn.execute("UPDATE threads SET ingested_at = ? WHERE id = ?", (value, thread_id))
            conn.commit()
        finally:
            conn.close()

    def init_and_distill(self):
        code, _, _ = self.run_cli("init", "--provider", "offline")
        self.assertEqual(code, 0)
        code, output, _ = self.run_cli("ingest", str(SLACK), "--json")
        self.assertEqual(code, 0)
        data = json.loads(output)
        threads = data["threads"]
        self.assertEqual(len(threads), 1)
        self.assertEqual(data["added"], 1)
        thread_id = threads[0]["id"]
        code, output, _ = self.run_cli("distill", thread_id, "--provider", "offline", "--json")
        self.assertEqual(code, 0)
        return thread_id, json.loads(output)

    def test_init_creates_workspace(self):
        code, output, _ = self.run_cli("init", "--provider", "offline")
        self.assertEqual(code, 0)
        self.assertTrue((self.home / "config.json").exists())
        self.assertTrue((self.home / "skills").is_dir())
        self.assertIn("initialized", output)

    def test_init_is_idempotent_without_force(self):
        self.run_cli("init")
        code, output, _ = self.run_cli("init")
        self.assertEqual(code, 0)
        self.assertIn("already exists", output)

    def test_ingest_redacts_and_indexes(self):
        self.run_cli("init", "--provider", "offline")
        code, output, _ = self.run_cli("ingest", str(SLACK), "--json")
        self.assertEqual(code, 0)
        threads = json.loads(output)["threads"]
        self.assertEqual(threads[0]["messages"], 10)
        self.assertEqual(threads[0]["status"], "added")
        normalized = self.home / "threads" / f"{threads[0]['id']}.json"
        content = normalized.read_text(encoding="utf-8")
        self.assertNotIn("ghp_FAKE", content)
        self.assertNotIn("priya.nair@acme.example", content)
        self.assertIn("[redacted-github-token]", content)

    def test_reingest_skips_unchanged_threads(self):
        code, output, _ = self.run_cli("init", "--provider", "offline")
        self.assertEqual(code, 0)
        code, output, _ = self.run_cli("ingest", str(SLACK), "--json")
        self.assertEqual(code, 0)
        first = json.loads(output)
        self.assertEqual(first["added"], 1)
        thread_id = first["threads"][0]["id"]
        normalized = self.home / "threads" / f"{thread_id}.json"
        content_before = normalized.read_text(encoding="utf-8")
        # A sentinel proves the row is not rewritten: a real write replaces this
        # with now_iso(), which can match the first write when both run in the
        # same second.
        self.force_stored_ingested_at(thread_id, "2000-01-01T00:00:00Z")
        hash_before = self.stored_content_hash(thread_id)

        code, output, _ = self.run_cli("ingest", str(SLACK), "--json")
        self.assertEqual(code, 0)
        again = json.loads(output)
        self.assertEqual(again["added"], 0)
        self.assertEqual(again["updated"], 0)
        self.assertEqual(again["skipped"], 1)
        self.assertEqual(again["threads"][0]["status"], "skipped")
        self.assertEqual(self.stored_ingested_at(thread_id), "2000-01-01T00:00:00Z")
        self.assertEqual(self.stored_content_hash(thread_id), hash_before)
        self.assertEqual(normalized.read_text(encoding="utf-8"), content_before)

        code, output, _ = self.run_cli("ingest", str(SLACK))
        self.assertEqual(code, 0)
        self.assertIn("no changes", output)

    def test_reingest_updates_changed_threads(self):
        code, _, _ = self.run_cli("init", "--provider", "offline")
        self.assertEqual(code, 0)
        code, output, _ = self.run_cli("ingest", str(SLACK), "--json")
        self.assertEqual(code, 0)
        first = json.loads(output)
        thread_id = first["threads"][0]["id"]
        hash_before = self.stored_content_hash(thread_id)

        copy = self.root / "slack-copy"
        shutil.copytree(SLACK, copy)
        # Edit a middle message so the title and the message count stay the same.
        # A comparison on ids, counts, or titles would miss this change.
        messages = json.loads((copy / "engineering" / "2026-10-30.json").read_text(encoding="utf-8"))
        messages[3]["text"] = "A different message body, same author and the same count."
        (copy / "engineering" / "2026-10-30.json").write_text(
            json.dumps(messages), encoding="utf-8"
        )

        code, output, _ = self.run_cli("ingest", str(copy), "--json")
        self.assertEqual(code, 0)
        again = json.loads(output)
        self.assertEqual(again["updated"], 1)
        self.assertEqual(again["skipped"], 0)
        self.assertEqual(again["added"], 0)
        self.assertEqual(again["threads"][0]["status"], "updated")
        self.assertEqual(again["threads"][0]["id"], thread_id)
        self.assertEqual(again["threads"][0]["messages"], 10)
        self.assertNotEqual(self.stored_content_hash(thread_id), hash_before)

    def test_force_reingests_unchanged_threads(self):
        code, _, _ = self.run_cli("init", "--provider", "offline")
        self.assertEqual(code, 0)
        code, output, _ = self.run_cli("ingest", str(SLACK), "--json")
        self.assertEqual(code, 0)
        thread_id = json.loads(output)["threads"][0]["id"]
        self.force_stored_ingested_at(thread_id, "2000-01-01T00:00:00Z")
        code, output, _ = self.run_cli("ingest", str(SLACK), "--force", "--json")
        self.assertEqual(code, 0)
        again = json.loads(output)
        self.assertEqual(again["updated"], 1)
        self.assertEqual(again["skipped"], 0)
        self.assertEqual(again["threads"][0]["status"], "updated")
        self.assertNotEqual(self.stored_ingested_at(thread_id), "2000-01-01T00:00:00Z")

    def test_reingest_repairs_a_missing_normalized_file(self):
        code, _, _ = self.run_cli("init", "--provider", "offline")
        self.assertEqual(code, 0)
        code, output, _ = self.run_cli("ingest", str(SLACK), "--json")
        self.assertEqual(code, 0)
        thread_id = json.loads(output)["threads"][0]["id"]
        normalized = self.home / "threads" / f"{thread_id}.json"
        normalized.unlink()
        code, output, _ = self.run_cli("ingest", str(SLACK), "--json")
        self.assertEqual(code, 0)
        again = json.loads(output)
        self.assertEqual(again["updated"], 1)
        self.assertEqual(again["skipped"], 0)
        self.assertTrue(normalized.exists())

    def test_ingest_json_reports_status_counts(self):
        code, _, _ = self.run_cli("init", "--provider", "offline")
        self.assertEqual(code, 0)
        code, output, _ = self.run_cli("ingest", str(SLACK), "--json")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(sorted(payload), ["added", "skipped", "threads", "updated"])
        for key in ("added", "updated", "skipped"):
            self.assertIsInstance(payload[key], int)
        self.assertEqual(payload["threads"][0]["status"], "added")

    def test_end_to_end(self):
        thread_id, skill = self.init_and_distill()
        self.assertEqual(skill["slug"], skill["name"])
        self.assertEqual(skill["version"], 1)
        self.assertIn("engineering/", skill["category"])
        self.assertTrue((Path(skill["path"]) / "SKILL.md").exists())

        code, output, _ = self.run_cli("validate", "--all")
        self.assertEqual(code, 0)
        self.assertIn("0 error", output)

        code, output, _ = self.run_cli("list", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(output)), 1)

        code, output, _ = self.run_cli("show", skill["slug"], "--raw")
        self.assertEqual(code, 0)
        self.assertIn("## Procedure", output)
        self.assertNotIn("ghp_FAKE", output)

        code, output, _ = self.run_cli("suggest", "integration tests are flaky with postgres", "--threshold", "0.0")
        self.assertEqual(code, 0)
        self.assertIn(skill["slug"], output)

        code, output, _ = self.run_cli("feedback", skill["slug"], "--outcome", "worked")
        self.assertEqual(code, 0)
        self.assertIn("confidence", output)

        code, output, _ = self.run_cli("search", "integration")
        self.assertEqual(code, 0)
        self.assertIn(skill["slug"], output)

        code, output, _ = self.run_cli("stats", "--json")
        self.assertEqual(code, 0)
        stats = json.loads(output)
        self.assertEqual(stats["threads"], 1)
        self.assertEqual(stats["skills"], 1)

    def test_duplicate_merge_increments_version(self):
        thread_id, first = self.init_and_distill()
        code, output, _ = self.run_cli(
            "distill", thread_id, "--provider", "offline", "--on-duplicate", "merge", "--json"
        )
        self.assertEqual(code, 0)
        merged = json.loads(output)
        self.assertEqual(merged["slug"], first["slug"])
        self.assertEqual(merged["version"], 2)
        code, output, _ = self.run_cli("list", "--json")
        self.assertEqual(len(json.loads(output)), 1)

    def test_duplicate_skip(self):
        thread_id, first = self.init_and_distill()
        code, output, _ = self.run_cli(
            "distill", thread_id, "--provider", "offline", "--on-duplicate", "skip"
        )
        self.assertEqual(code, 2)
        self.assertIn("skipped", output)
        self.assertIn(first["slug"], output)

    def test_install_project_scope(self):
        _, skill = self.init_and_distill()
        code, output, _ = self.run_cli(
            "install",
            skill["slug"],
            "--harness",
            "claude",
            "--scope",
            "project",
            "--project-root",
            str(self.project),
        )
        self.assertEqual(code, 0)
        target = self.project / ".claude" / "skills" / skill["slug"] / "SKILL.md"
        self.assertTrue(target.exists())
        self.assertIn("installed", output)

    def test_hooks_dry_run(self):
        code, output, _ = self.run_cli(
            "hooks",
            "install",
            "--harness",
            "claude",
            "--scope",
            "project",
            "--project-root",
            str(self.project),
            "--dry-run",
        )
        self.assertEqual(code, 0)
        self.assertIn("dry-run", output)
        self.assertFalse((self.project / ".claude" / "settings.json").exists())

    def test_hooks_accept_comma_separated_harnesses(self):
        code, output, _ = self.run_cli(
            "hooks",
            "install",
            "--harness",
            "claude,cursor",
            "--scope",
            "project",
            "--project-root",
            str(self.project),
        )
        self.assertEqual(code, 0)
        self.assertTrue((self.project / ".claude" / "settings.json").exists())
        self.assertTrue((self.project / ".cursor" / "hooks.json").exists())
        self.assertIn("claude", output)
        self.assertIn("cursor", output)

    def test_hooks_preserve_existing_settings(self):
        settings = self.project / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(
            json.dumps({"hooks": {"UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "echo existing"}]}]}}),
            encoding="utf-8",
        )
        code, _, _ = self.run_cli(
            "hooks", "install", "--harness", "claude", "--scope", "project", "--project-root", str(self.project)
        )
        self.assertEqual(code, 0)
        data = json.loads(settings.read_text(encoding="utf-8"))
        entries = data["hooks"]["UserPromptSubmit"]
        self.assertTrue(any("echo existing" in json.dumps(entry) for entry in entries))
        self.assertTrue(any("t2s suggest" in json.dumps(entry) for entry in entries))
        # Re-running must not duplicate the managed hook.
        self.run_cli(
            "hooks", "install", "--harness", "claude", "--scope", "project", "--project-root", str(self.project)
        )
        data = json.loads(settings.read_text(encoding="utf-8"))
        count = sum(1 for entry in data["hooks"]["UserPromptSubmit"] if "t2s suggest" in json.dumps(entry))
        self.assertEqual(count, 1)

    def test_hook_formats_are_valid_json(self):
        self.init_and_distill()
        for fmt in ("json", "claude", "cursor", "deepseek"):
            code, output, _ = self.run_cli("suggest", "flaky integration tests", "--format", fmt, "--threshold", "0.0")
            self.assertEqual(code, 0)
            parsed = json.loads(output)
            if fmt == "claude" or fmt == "deepseek":
                self.assertIn("hookSpecificOutput", parsed)
            elif fmt == "cursor":
                self.assertTrue(parsed["continue"])
                self.assertIn("user_message", parsed)
                self.assertNotIn("agent_message", parsed)
            else:
                self.assertIn("suggestions", parsed)

    def test_unknown_thread_returns_error(self):
        self.run_cli("init")
        code, output, _ = self.run_cli("distill", "does-not-exist")
        self.assertEqual(code, 1)
        self.assertIn("Unknown thread", output)

    def test_doctor(self):
        self.run_cli("init")
        code, output, _ = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("python", output)
        self.assertIn("workspace", output)

    def test_doctor_reports_schema_version(self):
        self.run_cli("init")
        self.run_cli("list")
        code, output, _ = self.run_cli("doctor", "--json")
        self.assertEqual(code, 0)
        checks = {row["check"]: row for row in json.loads(output)}
        self.assertIn("schema", checks)
        self.assertTrue(checks["schema"]["ok"])
        self.assertIn("v1", checks["schema"]["detail"])

    def test_doctor_points_at_migrate_for_a_legacy_database(self):
        self.run_cli("init")
        self.run_cli("list")
        db = self.home / "index.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("PRAGMA user_version = 0")
            conn.commit()
        finally:
            conn.close()
        code, output, _ = self.run_cli("doctor", "--json")
        self.assertEqual(code, 0)
        checks = {row["check"]: row for row in json.loads(output)}
        self.assertFalse(checks["schema"]["ok"])
        self.assertIn("--migrate", checks["schema"]["detail"])

    def test_doctor_migrate_stamps_a_legacy_database(self):
        self.run_cli("init")
        self.run_cli("list")
        db = self.home / "index.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("PRAGMA user_version = 0")
            conn.commit()
        finally:
            conn.close()
        code, output, _ = self.run_cli("doctor", "--migrate")
        self.assertEqual(code, 0)
        conn = sqlite3.connect(str(db))
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(version, 1)
        self.assertIn("schema", output)
        self.assertIn("adopted existing index at schema v1", output)

    def test_newer_database_is_refused_with_a_clean_error(self):
        self.run_cli("init")
        self.run_cli("list")
        db = self.home / "index.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("PRAGMA user_version = 99")
            conn.commit()
        finally:
            conn.close()
        code, output, _ = self.run_cli("list")
        self.assertEqual(code, 1)
        self.assertIn("newer", output)
        self.assertIn("pip install -U thread-to-skill", output)

    def test_stats_breakdowns(self):
        self.init_and_distill()
        code, output, _ = self.run_cli("stats", "--json")
        self.assertEqual(code, 0)
        stats = json.loads(output)
        by_source = [row for row in stats["by_source"] if row["source"] == "slack"]
        self.assertEqual(len(by_source), 1)
        self.assertGreaterEqual(by_source[0]["threads"], 1)
        self.assertIsInstance(stats["by_channel"], list)
        timeline = stats["timeline"]
        self.assertEqual(len(timeline), 12)
        now = datetime.now().astimezone()
        current_week = (now - timedelta(days=now.isoweekday() - 1)).date().isoformat()
        self.assertEqual(timeline[-1]["week_start"], current_week)
        self.assertGreaterEqual(timeline[-1]["ingests"], 1)

    def test_stats_breakdown_flags_render_tables(self):
        self.init_and_distill()
        code, output, _ = self.run_cli("stats", "--by-source")
        self.assertEqual(code, 0)
        self.assertIn("By source", output)
        self.assertIn("SOURCE", output)
        code, output, _ = self.run_cli("stats", "--by-channel")
        self.assertEqual(code, 0)
        self.assertIn("Top channels", output)
        self.assertIn("CHANNEL", output)
        code, output, _ = self.run_cli("stats", "--timeline")
        self.assertEqual(code, 0)
        self.assertIn("Timeline", output)
        self.assertIn("WEEK", output)

    def test_stats_empty_workspace(self):
        code, _, _ = self.run_cli("init", "--provider", "offline")
        self.assertEqual(code, 0)
        code, output, _ = self.run_cli("stats")
        self.assertEqual(code, 0)
        self.assertIn("No threads ingested yet.", output)
        self.assertNotIn("CATEGORY", output)
        code, output, _ = self.run_cli("stats", "--json")
        stats = json.loads(output)
        self.assertEqual(stats["by_source"], [])
        self.assertEqual(stats["by_channel"], [])
        self.assertEqual(len(stats["timeline"]), 12)
        self.assertTrue(all(entry["ingests"] == 0 and entry["distills"] == 0 for entry in stats["timeline"]))

    # ------------------------------------------------- archive, unarchive, delete

    def run_cli_with_stdin(self, stdin, *args):
        original = sys.stdin
        sys.stdin = stdin
        try:
            return self.run_cli(*args)
        finally:
            sys.stdin = original

    def stored_skill_status(self, slug: str) -> str:
        conn = sqlite3.connect(str(self.home / "index.db"))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT status FROM skills WHERE slug = ?", (slug,)).fetchone()
        finally:
            conn.close()
        return row["status"] if row else ""

    def skill_rows(self, slug: str) -> int:
        conn = sqlite3.connect(str(self.home / "index.db"))
        try:
            return conn.execute("SELECT COUNT(*) FROM skills WHERE slug = ?", (slug,)).fetchone()[0]
        finally:
            conn.close()

    def fts_rows(self, slug: str) -> int | None:
        conn = sqlite3.connect(str(self.home / "index.db"))
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'skills_fts'"
            ).fetchone()
            if not exists:
                return None
            return conn.execute("SELECT COUNT(*) FROM skills_fts WHERE slug = ?", (slug,)).fetchone()[0]
        finally:
            conn.close()

    def thread_rows(self) -> int:
        conn = sqlite3.connect(str(self.home / "index.db"))
        try:
            return conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        finally:
            conn.close()

    def test_archive_hides_skill_from_list_and_search(self):
        _, skill = self.init_and_distill()
        slug = skill["slug"]
        code, output, _ = self.run_cli("archive", slug)
        self.assertEqual(code, 0)
        self.assertIn("archived", output)
        self.assertEqual(self.stored_skill_status(slug), "archived")
        code, output, _ = self.run_cli("list", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), [])
        code, output, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertNotIn(slug, output)
        code, output, _ = self.run_cli("list", "--status", "archived", "--json")
        self.assertEqual(code, 0)
        self.assertEqual([entry["slug"] for entry in json.loads(output)], [slug])
        code, output, _ = self.run_cli("search", "postgres")
        self.assertEqual(code, 0)
        self.assertNotIn(slug, output)

    def test_archive_keeps_bundle_and_fts_row(self):
        _, skill = self.init_and_distill()
        slug = skill["slug"]
        bundle = Path(skill["path"]) / "SKILL.md"
        self.assertTrue(bundle.exists())
        before = self.fts_rows(slug)
        code, _, _ = self.run_cli("archive", slug)
        self.assertEqual(code, 0)
        self.assertTrue(bundle.exists())
        self.assertEqual(self.fts_rows(slug), before)
        code, output, _ = self.run_cli("archive", slug)
        self.assertEqual(code, 0)
        self.assertIn("already archived", output)

    def test_suggest_skips_archived_until_unarchived(self):
        _, skill = self.init_and_distill()
        slug = skill["slug"]
        prompt = "integration tests are flaky with postgres"
        code, output, _ = self.run_cli("suggest", prompt, "--threshold", "0.0")
        self.assertEqual(code, 0)
        self.assertIn(slug, output)
        code, _, _ = self.run_cli("archive", slug)
        self.assertEqual(code, 0)
        code, output, _ = self.run_cli("suggest", prompt, "--threshold", "0.0")
        self.assertEqual(code, 0)
        self.assertNotIn(slug, output)
        code, output, _ = self.run_cli("unarchive", slug)
        self.assertEqual(code, 0)
        self.assertIn("active", output)
        self.assertEqual(self.stored_skill_status(slug), "active")
        code, output, _ = self.run_cli("suggest", prompt, "--threshold", "0.0")
        self.assertEqual(code, 0)
        self.assertIn(slug, output)
        code, output, _ = self.run_cli("unarchive", slug)
        self.assertEqual(code, 0)
        self.assertIn("not archived", output)

    def test_delete_moves_bundle_to_trash_and_removes_rows(self):
        _, skill = self.init_and_distill()
        slug = skill["slug"]
        bundle = Path(skill["path"])
        self.assertTrue(bundle.exists())
        code, output, _ = self.run_cli("delete", slug, "--yes")
        self.assertEqual(code, 0)
        self.assertIn("deleted", output)
        self.assertFalse(bundle.exists())
        self.assertEqual(self.skill_rows(slug), 0)
        fts = self.fts_rows(slug)
        if fts is not None:
            self.assertEqual(fts, 0)
        trash = self.home / "trash"
        self.assertTrue(trash.is_dir())
        entries = [entry for entry in trash.iterdir() if entry.is_dir()]
        self.assertEqual(len(entries), 1)
        self.assertTrue((entries[0] / bundle.name / "SKILL.md").exists())
        code, output, _ = self.run_cli("list", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), [])
        code, output, _ = self.run_cli("search", "postgres")
        self.assertEqual(code, 0)
        self.assertNotIn(slug, output)

    def test_delete_refuses_without_yes_in_a_non_interactive_shell(self):
        _, skill = self.init_and_distill()
        slug = skill["slug"]
        bundle = Path(skill["path"])
        code, output, _ = self.run_cli_with_stdin(StringIO(""), "delete", slug)
        self.assertEqual(code, 1)
        self.assertIn("Refusing to delete without --yes", output)
        self.assertTrue(bundle.exists())
        self.assertEqual(self.skill_rows(slug), 1)
        self.assertFalse((self.home / "trash").exists())

    def test_delete_prompt_requires_the_exact_slug(self):
        _, skill = self.init_and_distill()
        slug = skill["slug"]
        bundle = Path(skill["path"])

        class FakeTTY:
            def __init__(self, answer: str):
                self.answer = answer

            def isatty(self) -> bool:
                return True

            def readline(self) -> str:
                return self.answer + "\n"

        code, output, _ = self.run_cli_with_stdin(FakeTTY("not-the-slug"), "delete", slug)
        self.assertEqual(code, 1)
        self.assertIn("Cancelled", output)
        self.assertTrue(bundle.exists())
        self.assertEqual(self.skill_rows(slug), 1)

        code, output, _ = self.run_cli_with_stdin(FakeTTY(slug), "delete", slug)
        self.assertEqual(code, 0)
        self.assertFalse(bundle.exists())
        self.assertEqual(self.skill_rows(slug), 0)

    def test_delete_removes_the_source_thread_by_default(self):
        thread_id, skill = self.init_and_distill()
        normalized = self.home / "threads" / f"{thread_id}.json"
        self.assertTrue(normalized.exists())
        self.assertEqual(self.thread_rows(), 1)
        code, _, _ = self.run_cli("delete", skill["slug"], "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(self.thread_rows(), 0)
        self.assertFalse(normalized.exists())
        trash = self.home / "trash"
        moved = list(trash.rglob(normalized.name))
        self.assertEqual(len(moved), 1)

    def test_delete_keep_thread_retains_provenance(self):
        thread_id, skill = self.init_and_distill()
        normalized = self.home / "threads" / f"{thread_id}.json"
        code, _, _ = self.run_cli("delete", skill["slug"], "--yes", "--keep-thread")
        self.assertEqual(code, 0)
        self.assertEqual(self.thread_rows(), 1)
        self.assertTrue(normalized.exists())
        self.assertEqual(self.skill_rows(skill["slug"]), 0)

    def test_delete_refuses_an_unknown_or_ambiguous_slug(self):
        thread_id, skill = self.init_and_distill()
        code, output, _ = self.run_cli("delete", "no-such-skill", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("Unknown skill", output)
        self.assertEqual(self.skill_rows(skill["slug"]), 1)
        self.assertTrue((Path(skill["path"]) / "SKILL.md").exists())

        for extra_slug in ("alpha-one", "alpha-two"):
            code, _, _ = self.run_cli(
                "distill", thread_id, "--provider", "offline", "--slug", extra_slug, "--on-duplicate", "new"
            )
            self.assertEqual(code, 0)
        code, output, _ = self.run_cli("delete", "alpha", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("ambiguous", output)
        self.assertEqual(self.skill_rows("alpha-one"), 1)
        self.assertEqual(self.skill_rows("alpha-two"), 1)


if __name__ == "__main__":
    unittest.main()