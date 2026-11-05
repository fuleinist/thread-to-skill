"""Schema versioning and migration tests for the SQLite registry."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from thread_to_skill import store as store_module
from thread_to_skill.store import (
    MIGRATIONS,
    SCHEMA_VERSION,
    Store,
    StoreError,
    inspect_schema,
)


def _add_note_column(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE skills ADD COLUMN note TEXT NOT NULL DEFAULT ''")


def _add_note_column_then_fail(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE skills ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    raise RuntimeError("boom")


def _insert_skill_row(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO skills (slug, name, title, description, summary, category, tags, triggers,
                            version, confidence, status, path, source_threads, created_at,
                            updated_at, last_used_at, suggested, accepted, worked, failed,
                            body, metadata)
        VALUES ('kept', 'kept', 'Kept', 'd', 's', 'general', '[]', '[]', 1, 0.5, 'active',
                'skills/kept', '[]', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '',
                0, 0, 0, 0, 'body', '{}')
        """
    )


def _bump(conn: sqlite3.Connection) -> None:
    """A no-op migration used when only the version number matters."""


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "index.db"
        self._stores: list[Store] = []

    def tearDown(self):
        while self._stores:
            try:
                self._stores.pop().close()
            except Exception:
                pass
        self.temp.cleanup()

    def open(self, **kwargs):
        store = Store(self.db, **kwargs)
        self._stores.append(store)
        return store

    def user_version(self) -> int:
        conn = sqlite3.connect(str(self.db))
        try:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()

    def set_user_version(self, version: int) -> None:
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute(f"PRAGMA user_version = {int(version)}")
            conn.commit()
        finally:
            conn.close()

    def column_names(self, table: str) -> list[str]:
        conn = sqlite3.connect(str(self.db))
        try:
            return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        finally:
            conn.close()

    # ------------------------------------------------------------------ fresh
    def test_fresh_database_gets_current_version(self):
        store = self.open()
        self.assertEqual(self.user_version(), SCHEMA_VERSION)
        self.assertEqual(store.schema_info()["version"], SCHEMA_VERSION)
        self.assertEqual(store.schema_info()["pending"], [])

    def test_fresh_database_is_usable(self):
        store = self.open()
        self.assertEqual(store.count_skills(), 0)
        self.assertEqual(store.count_threads(), 0)
        tables = {
            row["name"]
            for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        self.assertIn("skills", tables)
        self.assertIn("threads", tables)
        self.assertIn("events", tables)

    def test_reopen_keeps_version_and_does_not_back_up(self):
        self.open()
        store = self.open()
        backups = list(self.db.parent.glob("index.db.bak-*"))
        self.assertEqual(backups, [])
        self.assertEqual(store.schema_info()["migrated"], [])

    def test_fresh_database_runs_migrations_up_to_the_target(self):
        with mock.patch.object(store_module, "SCHEMA_VERSION", 2):
            store = self.open(migrations=[(2, _add_note_column)])
        self.assertEqual(self.user_version(), 2)
        self.assertIn("note", self.column_names("skills"))
        self.assertEqual(store.schema_info()["migrated"], [2])

    def test_fresh_database_does_not_write_a_backup(self):
        with mock.patch.object(store_module, "SCHEMA_VERSION", 2):
            store = self.open(migrations=[(2, _add_note_column)])
        self.assertIsNone(store.schema_info()["backup"])
        self.assertEqual(list(self.db.parent.glob("index.db.bak-*")), [])

    # ----------------------------------------------------------------- legacy
    def test_legacy_unversioned_database_is_adopted(self):
        self.open()
        self.set_user_version(0)
        store = self.open()
        self.assertEqual(self.user_version(), SCHEMA_VERSION)
        self.assertEqual(store.schema_info()["version"], SCHEMA_VERSION)
        self.assertEqual(store.schema_info()["migrated"], [])

    def test_legacy_database_keeps_its_rows(self):
        self.open()
        conn = sqlite3.connect(str(self.db))
        try:
            _insert_skill_row(conn)
            conn.commit()
        finally:
            conn.close()
        self.set_user_version(0)
        store = self.open()
        self.assertIsNotNone(store.get_skill("kept"))

    # ---------------------------------------------------------------- upgrade
    def test_upgrade_applies_pending_migration_and_keeps_rows(self):
        self.open()
        conn = sqlite3.connect(str(self.db))
        try:
            _insert_skill_row(conn)
            conn.commit()
        finally:
            conn.close()
        self.assertNotIn("note", self.column_names("skills"))

        with mock.patch.object(store_module, "SCHEMA_VERSION", 2):
            store = self.open(migrations=[(2, _add_note_column)])

        self.assertEqual(self.user_version(), 2)
        self.assertEqual(store.schema_info()["migrated"], [2])
        self.assertIn("note", self.column_names("skills"))
        self.assertIsNotNone(store.get_skill("kept"))

    def test_upgrade_writes_a_backup_next_to_the_database(self):
        self.open()
        with mock.patch.object(store_module, "SCHEMA_VERSION", 2):
            store = self.open(migrations=[(2, _add_note_column)])
        backups = list(self.db.parent.glob("index.db.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(store.schema_info()["backup"], str(backups[0]))

    def test_backup_holds_the_pre_migration_schema(self):
        self.open()
        with mock.patch.object(store_module, "SCHEMA_VERSION", 2):
            self.open(migrations=[(2, _add_note_column)])
        backup = next(self.db.parent.glob("index.db.bak-*"))
        conn = sqlite3.connect(str(backup))
        try:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(skills)")]
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()
        self.assertNotIn("note", columns)
        self.assertEqual(version, 1)

    def test_multiple_pending_migrations_run_in_order(self):
        self.open()
        order: list[int] = []

        def step_two(conn):
            order.append(2)

        def step_three(conn):
            order.append(3)

        with mock.patch.object(store_module, "SCHEMA_VERSION", 3):
            store = self.open(migrations=[(2, step_two), (3, step_three)])

        self.assertEqual(order, [2, 3])
        self.assertEqual(self.user_version(), 3)
        self.assertEqual(store.schema_info()["migrated"], [2, 3])

    def test_skipped_migrations_are_not_replayed(self):
        self.open()
        calls: list[int] = []

        def step_two(conn):
            calls.append(2)

        with mock.patch.object(store_module, "SCHEMA_VERSION", 2):
            self.open(migrations=[(2, step_two)])
        with mock.patch.object(store_module, "SCHEMA_VERSION", 2):
            self.open(migrations=[(2, step_two)])

        self.assertEqual(calls, [2])

    # ------------------------------------------------------------------ guard
    def test_newer_database_is_refused(self):
        self.open()
        self.set_user_version(SCHEMA_VERSION + 5)
        with self.assertRaises(StoreError) as caught:
            Store(self.db)
        message = str(caught.exception)
        self.assertIn("newer", message)
        self.assertIn("Upgrade thread-to-skill", message)

    def test_newer_database_is_not_modified(self):
        self.open()
        self.set_user_version(SCHEMA_VERSION + 5)
        with self.assertRaises(StoreError):
            Store(self.db)
        self.assertEqual(self.user_version(), SCHEMA_VERSION + 5)

    def test_newer_database_is_refused_before_auto_migrate(self):
        self.open()
        self.set_user_version(SCHEMA_VERSION + 1)
        with mock.patch.object(store_module, "SCHEMA_VERSION", SCHEMA_VERSION - 1):
            with self.assertRaises(StoreError):
                Store(self.db)

    # --------------------------------------------------------------- rollback
    def test_failed_migration_rolls_back(self):
        self.open()
        with mock.patch.object(store_module, "SCHEMA_VERSION", 2):
            with self.assertRaises(StoreError) as caught:
                Store(self.db, migrations=[(2, _add_note_column_then_fail)])
        self.assertIn("failed", str(caught.exception))
        self.assertEqual(self.user_version(), 1)
        self.assertNotIn("note", self.column_names("skills"))

    def test_failed_migration_keeps_the_backup(self):
        self.open()
        with mock.patch.object(store_module, "SCHEMA_VERSION", 2):
            with self.assertRaises(StoreError):
                Store(self.db, migrations=[(2, _add_note_column_then_fail)])
        backups = list(self.db.parent.glob("index.db.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertGreater(backups[0].stat().st_size, 0)

    def test_a_later_failure_rolls_back_earlier_migrations(self):
        self.open()

        def add_note(conn):
            conn.execute("ALTER TABLE skills ADD COLUMN note TEXT NOT NULL DEFAULT ''")

        def add_other_then_fail(conn):
            conn.execute("ALTER TABLE skills ADD COLUMN other TEXT NOT NULL DEFAULT ''")
            raise RuntimeError("boom")

        with mock.patch.object(store_module, "SCHEMA_VERSION", 3):
            with self.assertRaises(StoreError):
                Store(self.db, migrations=[(2, add_note), (3, add_other_then_fail)])

        self.assertEqual(self.user_version(), 1)
        columns = self.column_names("skills")
        self.assertNotIn("note", columns)
        self.assertNotIn("other", columns)

    def test_a_rolled_back_upgrade_leaves_the_database_openable(self):
        self.open()

        def add_note_then_fail(conn):
            conn.execute("ALTER TABLE skills ADD COLUMN note TEXT NOT NULL DEFAULT ''")
            raise RuntimeError("boom")

        with mock.patch.object(store_module, "SCHEMA_VERSION", 2):
            with self.assertRaises(StoreError):
                Store(self.db, migrations=[(2, add_note_then_fail)])

        store = self.open()
        self.assertEqual(self.user_version(), 1)
        self.assertEqual(store.schema_info()["version"], 1)
        self.assertEqual(store.schema_info()["pending"], [])
        self.assertEqual(store.count_skills(), 0)
        self.assertEqual(store.count_threads(), 0)

    def test_failed_migration_keeps_existing_rows(self):
        self.open()
        conn = sqlite3.connect(str(self.db))
        try:
            _insert_skill_row(conn)
            conn.commit()
        finally:
            conn.close()
        with mock.patch.object(store_module, "SCHEMA_VERSION", 2):
            with self.assertRaises(StoreError):
                Store(self.db, migrations=[(2, _add_note_column_then_fail)])
        store = self.open()
        self.assertIsNotNone(store.get_skill("kept"))

    # -------------------------------------------------------------- no migrate
    def test_auto_migrate_false_leaves_the_database_alone(self):
        self.open()
        with mock.patch.object(store_module, "SCHEMA_VERSION", 2):
            store = self.open(auto_migrate=False, migrations=[(2, _add_note_column)])
            self.assertEqual(store.schema_info()["version"], 1)
            self.assertEqual(store.schema_info()["pending"], [2])
        self.assertEqual(self.user_version(), 1)
        self.assertNotIn("note", self.column_names("skills"))

    # ---------------------------------------------------------------- inspect
    def test_inspect_schema_reports_a_missing_file(self):
        report = inspect_schema(self.db)
        self.assertFalse(report["exists"])
        self.assertEqual(report["version"], 0)
        self.assertFalse(report["needs_upgrade"])

    def test_inspect_schema_does_not_create_the_file(self):
        inspect_schema(self.db)
        self.assertFalse(self.db.exists())

    def test_inspect_schema_reports_the_current_version(self):
        self.open()
        report = inspect_schema(self.db)
        self.assertTrue(report["exists"])
        self.assertEqual(report["version"], SCHEMA_VERSION)
        self.assertFalse(report["needs_upgrade"])
        self.assertFalse(report["too_new"])

    def test_inspect_schema_reports_a_legacy_database(self):
        self.open()
        self.set_user_version(0)
        report = inspect_schema(self.db)
        self.assertEqual(report["version"], 0)
        self.assertTrue(report["needs_upgrade"])

    def test_inspect_schema_reports_a_newer_database(self):
        self.open()
        self.set_user_version(SCHEMA_VERSION + 1)
        report = inspect_schema(self.db)
        self.assertTrue(report["too_new"])
        self.assertFalse(report["needs_upgrade"])

    def test_inspect_schema_reports_pending_versions(self):
        self.open()
        with mock.patch.object(store_module, "MIGRATIONS", ((2, _bump), (3, _bump))), \
                mock.patch.object(store_module, "SCHEMA_VERSION", 3):
            report = inspect_schema(self.db)
        self.assertEqual(report["pending"], [2, 3])
        self.assertTrue(report["needs_upgrade"])

    def test_default_registry_starts_above_the_baseline(self):
        for version, _ in MIGRATIONS:
            self.assertGreater(version, 1)
        self.assertGreaterEqual(SCHEMA_VERSION, 1)