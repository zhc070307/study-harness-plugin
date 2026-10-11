"""Direct SQL constraint checks: CHECK, UNIQUE, FOREIGN KEY and partial index."""

from __future__ import annotations

import sqlite3
import unittest

from tests._support import ServiceTestCase
from study_agent.models import to_iso


def _insert_task(
    conn,
    task_id,
    task_key,
    *,
    status="open",
    completed_at=None,
    superseded_by=None,
    course_id=None,
    actor="student",
):
    conn.execute(
        """
        INSERT INTO tasks (
            id, task_key, course_id, title, description, actor, status,
            due_at, source, superseded_by, created_at, updated_at, completed_at
        ) VALUES (?, ?, ?, 'title', NULL, ?, ?, NULL, NULL, ?, '2026-09-09T04:00:00+00:00',
                  '2026-09-09T04:00:00+00:00', ?)
        """,
        (task_id, task_key, course_id, actor, status, superseded_by, completed_at),
    )


class SchemaConstraintTests(ServiceTestCase):
    def test_invalid_status_rejected(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_task(self.conn, "id-1", "T-000001", status="doing")

    def test_duplicate_task_key_rejected(self) -> None:
        _insert_task(self.conn, "id-1", "T-000001")
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_task(self.conn, "id-2", "T-000001")

    def test_foreign_key_course_rejected(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_task(self.conn, "id-1", "T-000001", course_id="missing-course")

    def test_completed_requires_completed_at(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_task(self.conn, "id-1", "T-000001", status="completed", completed_at=None)

    def test_non_completed_must_not_have_completed_at(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_task(
                self.conn,
                "id-1",
                "T-000001",
                status="open",
                completed_at="2026-09-09T04:00:00+00:00",
            )

    def test_superseded_requires_target(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_task(self.conn, "id-1", "T-000001", status="superseded")

    def test_self_supersede_rejected(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_task(
                self.conn,
                "id-1",
                "T-000001",
                status="superseded",
                superseded_by="id-1",
            )

    def test_self_dependency_rejected(self) -> None:
        _insert_task(self.conn, "id-1", "T-000001")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO task_dependencies (task_id, depends_on_task_id, created_at) "
                "VALUES ('id-1', 'id-1', '2026-09-09T04:00:00+00:00')"
            )

    def test_duplicate_criterion_position_rejected(self) -> None:
        _insert_task(self.conn, "id-1", "T-000001")
        self.conn.execute(
            "INSERT INTO task_criteria (id, task_id, position, text, required, satisfied, satisfied_at, created_at) "
            "VALUES ('c1', 'id-1', 1, 'a', 1, 0, NULL, '2026-09-09T04:00:00+00:00')"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO task_criteria (id, task_id, position, text, required, satisfied, satisfied_at, created_at) "
                "VALUES ('c2', 'id-1', 1, 'b', 1, 0, NULL, '2026-09-09T04:00:00+00:00')"
            )

    def test_satisfied_requires_timestamp(self) -> None:
        _insert_task(self.conn, "id-1", "T-000001")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO task_criteria (id, task_id, position, text, required, satisfied, satisfied_at, created_at) "
                "VALUES ('c1', 'id-1', 1, 'a', 1, 1, NULL, '2026-09-09T04:00:00+00:00')"
            )

    def test_only_one_active_schedule_per_task_and_date(self) -> None:
        _insert_task(self.conn, "id-1", "T-000001")
        self.conn.execute(
            "INSERT INTO task_schedules (id, task_id, scheduled_date, priority, note, active, created_at, deactivated_at) "
            "VALUES ('s1', 'id-1', '2026-09-09', 'must', NULL, 1, '2026-09-09T04:00:00+00:00', NULL)"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO task_schedules (id, task_id, scheduled_date, priority, note, active, created_at, deactivated_at) "
                "VALUES ('s2', 'id-1', '2026-09-09', 'should', NULL, 1, '2026-09-09T04:00:00+00:00', NULL)"
            )
        self.conn.execute(
            "INSERT INTO task_schedules (id, task_id, scheduled_date, priority, note, active, created_at, deactivated_at) "
            "VALUES ('s3', 'id-1', '2026-09-09', 'should', NULL, 0, '2026-09-09T04:00:00+00:00', "
            "'2026-09-09T04:01:00+00:00')"
        )

    def test_invalid_priority_rejected(self) -> None:
        _insert_task(self.conn, "id-1", "T-000001")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO task_schedules (id, task_id, scheduled_date, priority, note, active, created_at, deactivated_at) "
                "VALUES ('s1', 'id-1', '2026-09-09', 'urgent', NULL, 1, '2026-09-09T04:00:00+00:00', NULL)"
            )

    def test_invalid_event_type_rejected(self) -> None:
        _insert_task(self.conn, "id-1", "T-000001")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO task_events (id, task_id, event_type, actor, occurred_at, payload) "
                "VALUES ('e1', 'id-1', 'exploded', 'agent', '2026-09-09T04:00:00+00:00', '{}')"
            )

    def test_invalid_task_key_format_rejected(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_task(self.conn, "id-1", "X-1")


if __name__ == "__main__":
    unittest.main()
