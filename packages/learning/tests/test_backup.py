"""SQLite Backup API and restore verification."""

from __future__ import annotations

import unittest
import sqlite3
from pathlib import Path

from tests._support import ServiceTestCase, db
from study_agent import db as db_module


class BackupTests(ServiceTestCase):
    def test_read_only_connection_rejects_writes_after_wal_checkpoint(self):
        backup_path = Path(self.tmpdir) / "query-only.sqlite3"
        db.backup(self.conn, backup_path)
        probe = db.connect(backup_path, read_only=True)
        try:
            self.assertEqual(probe.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                probe.execute("CREATE TABLE forbidden_write (id INTEGER)")
        finally:
            probe.close()

    def test_backup_is_consistent_and_restorable(self) -> None:
        self.service.add_course("COMP 1023", "Python")
        task = self.make_open_task("backup me", course="COMP 1023")
        self.service.add_criterion(task.task_key, "done")
        self.service.schedule(task.task_key, "2026-09-09", "must")

        backup_path = self.db_path.parent / "backups" / "snapshot.sqlite3"
        db_module.backup(self.conn, backup_path)
        self.assertTrue(backup_path.exists())

        backup_conn = db.connect(backup_path, read_only=True)
        try:
            self.assertEqual(db_module.integrity_check(backup_conn), "ok")
            self.assertEqual(db_module.foreign_key_check(backup_conn), [])
            self.assertEqual(db_module.schema_version(backup_conn), 4)
            self.assertEqual(
                backup_conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"], 1
            )
            self.assertEqual(
                backup_conn.execute("SELECT COUNT(*) AS n FROM task_events").fetchone()["n"], 3
            )
        finally:
            backup_conn.close()

        restored_path = self.db_path.parent / "restored.sqlite3"
        source = db.connect(backup_path, read_only=True)
        try:
            db_module.backup(source, restored_path)
        finally:
            source.close()
        restored = db.connect(restored_path, read_only=True)
        try:
            self.assertEqual(db_module.integrity_check(restored), "ok")
            self.assertEqual(
                restored.execute("SELECT task_key FROM tasks").fetchone()["task_key"],
                task.task_key,
            )
        finally:
            restored.close()

    def test_backup_includes_wal_content(self) -> None:
        task = self.make_open_task("wal content")
        self.service.schedule(task.task_key, "2026-09-09", "should")
        backup_path = self.db_path.parent / "wal-backup.sqlite3"
        db_module.backup(self.conn, backup_path)
        backup_conn = db.connect(backup_path, read_only=True)
        try:
            count = backup_conn.execute("SELECT COUNT(*) AS n FROM task_schedules").fetchone()["n"]
            self.assertEqual(count, 1)
        finally:
            backup_conn.close()


if __name__ == "__main__":
    unittest.main()
