"""Two-connection concurrency: busy_timeout, serialized writes, unique task keys."""

from __future__ import annotations

import sqlite3
import threading
import time
import unittest

from tests._support import ServiceTestCase, db
from study_agent.models import Clock
from study_agent.service import TaskService


class ConcurrencyTests(ServiceTestCase):
    def test_second_writer_fails_fast_when_busy_timeout_is_small(self) -> None:
        holder = db.connect(self.db_path)
        self.addCleanup(holder.close)
        other = db.connect(self.db_path, busy_timeout_ms=100)
        self.addCleanup(other.close)

        holder.execute("BEGIN IMMEDIATE")
        holder.execute(
            "INSERT INTO courses (id, code, name, created_at, updated_at) "
            "VALUES ('x', 'X', 'X', '2026-09-09T04:00:00+00:00', '2026-09-09T04:00:00+00:00')"
        )
        with self.assertRaises(sqlite3.OperationalError):
            other.execute("BEGIN IMMEDIATE")
        holder.execute("ROLLBACK")

    def test_second_writer_waits_then_succeeds(self) -> None:
        holder = db.connect(self.db_path, check_same_thread=False)
        self.addCleanup(holder.close)
        waiter = db.connect(self.db_path, busy_timeout_ms=5000)
        self.addCleanup(waiter.close)

        holder.execute("BEGIN IMMEDIATE")
        holder.execute(
            "INSERT INTO courses (id, code, name, created_at, updated_at) "
            "VALUES ('a', 'A', 'A', '2026-09-09T04:00:00+00:00', '2026-09-09T04:00:00+00:00')"
        )

        def release() -> None:
            time.sleep(0.3)
            holder.execute("COMMIT")

        thread = threading.Thread(target=release)
        thread.start()
        started = time.time()
        waiter.execute("BEGIN IMMEDIATE")
        waiter.execute(
            "INSERT INTO courses (id, code, name, created_at, updated_at) "
            "VALUES ('b', 'B', 'B', '2026-09-09T04:00:00+00:00', '2026-09-09T04:00:00+00:00')"
        )
        waiter.execute("COMMIT")
        elapsed = time.time() - started
        thread.join()
        self.assertGreaterEqual(elapsed, 0.2)
        count = self.conn.execute("SELECT COUNT(*) AS n FROM courses").fetchone()["n"]
        self.assertEqual(count, 2)

    def test_parallel_task_creation_keeps_keys_unique(self) -> None:
        errors = []

        def worker(prefix: str) -> None:
            try:
                conn = db.connect(self.db_path, busy_timeout_ms=10000)
                try:
                    service = TaskService(conn, clock=Clock())
                    for index in range(15):
                        service.create_task("%s-%d" % (prefix, index), status="open")
                finally:
                    conn.close()
            except Exception as exc:  # pragma: no cover - failure reporting
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=("w%d" % i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        rows = self.conn.execute("SELECT task_key FROM tasks").fetchall()
        keys = [row["task_key"] for row in rows]
        self.assertEqual(len(keys), 30)
        self.assertEqual(len(set(keys)), 30)
        event_count = self.conn.execute("SELECT COUNT(*) AS n FROM task_events").fetchone()["n"]
        self.assertEqual(event_count, 30)


if __name__ == "__main__":
    unittest.main()
