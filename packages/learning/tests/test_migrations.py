"""Migration behaviour: first run, repeat run, pragmas and schema version."""

from __future__ import annotations

import unittest

from tests._support import ServiceTestCase, db
from study_agent import db as db_module


class MigrationTests(ServiceTestCase):
    def test_initial_schema_contains_all_tables(self) -> None:
        names = set(db_module.table_names(self.conn))
        expected = {
            "courses",
            "tasks",
            "task_criteria",
            "task_dependencies",
            "task_schedules",
            "task_evidence",
            "task_events",
            "schema_migrations",
        }
        self.assertTrue(expected.issubset(names), "missing tables: %s" % (expected - names))

    def test_schema_version_is_current(self) -> None:
        self.assertEqual(db_module.schema_version(self.conn), 4)

    def test_repeat_migration_is_a_noop(self) -> None:
        applied = db_module.migrate(self.conn)
        self.assertEqual(applied, [])
        self.assertEqual(db_module.schema_version(self.conn), 4)

    def test_foreign_keys_and_wal_enabled(self) -> None:
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        self.assertGreater(self.conn.execute("PRAGMA busy_timeout").fetchone()[0], 0)

    def test_discovered_migrations_are_versioned(self) -> None:
        found = db_module.discover_migrations()
        self.assertEqual([version for version, _, _ in found], [1, 2, 3, 4])

    def test_integrity_and_foreign_keys_are_clean(self) -> None:
        self.assertEqual(db_module.integrity_check(self.conn), "ok")
        self.assertEqual(db_module.foreign_key_check(self.conn), [])


if __name__ == "__main__":
    unittest.main()
