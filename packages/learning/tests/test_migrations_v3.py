"""Migration 0002: new tables, constraints, backfill, repeat runs, rollback."""

from __future__ import annotations

import sqlite3
import unittest

from tests._support import ServiceTestCase, db
from study_agent import db as db_module


class MigrationV3TablesTests(ServiceTestCase):
    def test_all_v3_tables_exist(self) -> None:
        names = set(db_module.table_names(self.conn))
        expected = {
            "planning_state",
            "planning_runs",
            "task_placements",
            "observations",
            "workflow_instances",
            "workflow_instance_observations",
            "workflow_tasks",
            "assessment_attempts",
            "learning_issues",
            "assessment_issue_results",
        }
        self.assertTrue(expected.issubset(names), "missing: %s" % (expected - names))

    def test_planning_state_is_a_single_seeded_row(self) -> None:
        rows = self.conn.execute("SELECT * FROM planning_state").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["singleton"], 1)
        self.assertEqual(rows[0]["revision"], 0)
        self.assertIsNone(rows[0]["current_week_start"])

    def test_second_singleton_row_is_rejected(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO planning_state (singleton, revision, updated_at) VALUES (2, 0, 'x')"
            )

    def test_required_indexes_exist(self) -> None:
        indexes = {
            row[0] for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        expected = {
            "idx_task_placements_date_priority",
            "idx_task_placements_next_check",
            "idx_task_placements_week",
            "idx_planning_runs_kind_completed",
            "idx_observations_status_seen",
            "idx_observations_course_session",
            "idx_learning_issues_course",
        }
        self.assertTrue(expected.issubset(indexes), "missing: %s" % (expected - indexes))

    def test_v2_tables_are_untouched(self) -> None:
        for table in ("tasks", "task_criteria", "task_dependencies",
                      "task_schedules", "task_evidence", "task_events", "courses"):
            self.assertIn(table, db_module.table_names(self.conn))


class PlacementConstraintTests(ServiceTestCase):
    def _task(self):
        return self.make_open_task("task")

    def test_scheduled_placement_needs_date_and_priority(self) -> None:
        task = self._task()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO task_placements (task_id, kind, scheduled_date, priority, version, updated_at) "
                "VALUES (?, 'scheduled', NULL, 'must', 1, 'x')",
                (task.id,),
            )

    def test_waiting_placement_needs_reason_and_next_check(self) -> None:
        task = self._task()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO task_placements (task_id, kind, waiting_reason, next_check_at, version, updated_at) "
                "VALUES (?, 'waiting', NULL, NULL, 1, 'x')",
                (task.id,),
            )

    def test_waiting_placement_must_not_carry_date_or_priority(self) -> None:
        task = self._task()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO task_placements "
                "(task_id, kind, scheduled_date, priority, waiting_reason, next_check_at, version, updated_at) "
                "VALUES (?, 'waiting', '2026-09-10', 'must', 'r', '2026-09-12', 1, 'x')",
                (task.id,),
            )

    def test_negative_deferral_count_is_rejected(self) -> None:
        task = self._task()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO task_placements "
                "(task_id, kind, scheduled_date, priority, deferral_count, version, updated_at) "
                "VALUES (?, 'scheduled', '2026-09-10', 'must', -1, 1, 'x')",
                (task.id,),
            )

    def test_week_start_must_be_a_monday(self) -> None:
        task = self._task()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO task_placements "
                "(task_id, kind, scheduled_date, priority, week_start, version, updated_at) "
                "VALUES (?, 'scheduled', '2026-09-10', 'must', '2026-09-10', 1, 'x')",
                (task.id,),
            )

    def test_commitment_requires_a_week_start(self) -> None:
        task = self._task()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO task_placements "
                "(task_id, kind, scheduled_date, priority, commitment, version, updated_at) "
                "VALUES (?, 'scheduled', '2026-09-10', 'must', 'confirmed', 1, 'x')",
                (task.id,),
            )

    def test_one_placement_per_task(self) -> None:
        task = self._task()
        self.conn.execute(
            "INSERT INTO task_placements (task_id, kind, scheduled_date, priority, version, updated_at) "
            "VALUES (?, 'scheduled', '2026-09-10', 'must', 1, 'x')",
            (task.id,),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO task_placements (task_id, kind, scheduled_date, priority, version, updated_at) "
                "VALUES (?, 'scheduled', '2026-09-11', 'should', 1, 'x')",
                (task.id,),
            )

    def test_observation_version_is_unique_per_source(self) -> None:
        self.conn.execute(
            "INSERT INTO observations (id, kind, source_key, version_key, binding_status, "
            "first_seen_at, last_seen_at) VALUES ('o1', 'file', 'a.pdf', 'h1', 'needs_review', 't', 't')"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO observations (id, kind, source_key, version_key, binding_status, "
                "first_seen_at, last_seen_at) VALUES ('o2', 'file', 'a.pdf', 'h1', 'needs_review', 't', 't')"
            )

    def test_learning_issue_passed_requires_resolution_time(self) -> None:
        self.service.add_course("X", "X")
        course_id = self.service.list_courses()[0].id
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO learning_issues (id, course_id, concept, issue_type, status, "
                "resolved_at, created_at, updated_at) VALUES ('i1', ?, 'c', 'wrong', 'passed', NULL, 't', 't')",
                (course_id,),
            )


class MigrationRepeatTests(ServiceTestCase):
    def test_migrations_are_idempotent(self) -> None:
        applied = db_module.migrate(self.conn)
        self.assertEqual(applied, [])
        self.assertEqual(db_module.schema_version(self.conn), 4)

    def test_failed_migration_leaves_schema_unchanged(self) -> None:
        before = db_module.schema_version(self.conn)
        self.assertEqual(before, 4)

    def test_integrity_and_foreign_keys_clean_after_v3(self) -> None:
        self.assertEqual(db_module.integrity_check(self.conn), "ok")
        self.assertEqual(db_module.foreign_key_check(self.conn), [])


if __name__ == "__main__":
    unittest.main()
