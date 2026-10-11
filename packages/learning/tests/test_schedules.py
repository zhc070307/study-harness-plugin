"""Scheduling, rescheduling, priority changes and carry-forward semantics."""

from __future__ import annotations

import unittest

from tests._support import ServiceTestCase
from study_agent.errors import ConflictError, ValidationError


class ScheduleTests(ServiceTestCase):
    def test_schedule_creates_active_row_and_event(self) -> None:
        task = self.make_open_task("title")
        schedule = self.service.schedule(task.task_key, "2026-09-09", "must")
        self.assertEqual(schedule.scheduled_date, "2026-09-09")
        self.assertEqual(schedule.active, 1)
        self.assertEqual(self.count_events(task.task_key, "scheduled"), 1)

    def test_priority_change_replaces_active_row(self) -> None:
        task = self.make_open_task("title")
        self.service.schedule(task.task_key, "2026-09-09", "must")
        self.service.schedule(task.task_key, "2026-09-09", "if_time")
        rows = self.service.schedules_for(task.id)
        self.assertEqual(len(rows), 2)
        active = [row for row in rows if row.active]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].priority, "if_time")
        self.assertEqual(self.count_events(task.task_key, "priority_changed"), 1)

    def test_identical_schedule_is_a_conflict(self) -> None:
        task = self.make_open_task("title")
        self.service.schedule(task.task_key, "2026-09-09", "must")
        with self.assertRaises(ConflictError):
            self.service.schedule(task.task_key, "2026-09-09", "must")

    def test_reschedule_moves_task_and_records_history(self) -> None:
        task = self.make_open_task("title")
        self.service.schedule(task.task_key, "2026-09-09", "must")
        self.service.schedule(task.task_key, "2026-09-10", "should", note="结转")
        rows = self.service.schedules_for(task.id)
        active = [row for row in rows if row.active]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].scheduled_date, "2026-09-10")
        event = self.service.events_for(task.task_key)[-1]
        self.assertEqual(event.event_type, "rescheduled")
        self.assertEqual(event.payload["from"], ["2026-09-09"])
        self.assertEqual(event.payload["to"], "2026-09-10")
        self.assertEqual(event.payload["note"], "结转")

    def test_only_open_tasks_can_be_scheduled(self) -> None:
        proposal = self.service.create_task("proposal")
        with self.assertRaises(ConflictError):
            self.service.schedule(proposal.task_key, "2026-09-09", "must")
        completed = self.make_open_task("completed")
        self.service.add_evidence(completed.task_key, "student_confirmation", "done")
        self.service.complete(completed.task_key)
        with self.assertRaises(ConflictError):
            self.service.schedule(completed.task_key, "2026-09-09", "must")

    def test_invalid_date_rejected(self) -> None:
        task = self.make_open_task("title")
        with self.assertRaises(ValueError):
            self.service.schedule(task.task_key, "2026-02-30", "must")
        with self.assertRaises(ValueError):
            self.service.schedule(task.task_key, "09/09/2026", "must")

    def test_invalid_priority_rejected(self) -> None:
        task = self.make_open_task("title")
        with self.assertRaises(ValidationError):
            self.service.schedule(task.task_key, "2026-09-09", "urgent")


if __name__ == "__main__":
    unittest.main()
