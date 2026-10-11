"""Same-transaction guarantees: state and events commit or roll back together."""

from __future__ import annotations

import unittest

from tests._support import ServiceTestCase


class TransactionTests(ServiceTestCase):
    def test_failed_completion_rolls_back_state_and_event(self) -> None:
        task = self.make_open_task("title", criteria=["done"])
        self.service.satisfy_criterion(task.task_key, "1")
        self.service.add_evidence(task.task_key, "student_confirmation", "confirmed")
        before = len(self.service.events_for(task.task_key))

        original = self.service._record_event

        def explode(*args, **kwargs):
            raise RuntimeError("simulated failure while appending the event")

        self.service._record_event = explode
        try:
            with self.assertRaises(RuntimeError):
                self.service.complete(task.task_key)
        finally:
            self.service._record_event = original

        reloaded = self.service.get_task(task.task_key)
        self.assertEqual(reloaded.status, "open")
        self.assertIsNone(reloaded.completed_at)
        self.assertEqual(len(self.service.events_for(task.task_key)), before)

    def test_failed_dependency_add_leaves_no_event_or_row(self) -> None:
        first = self.make_open_task("first")
        second = self.make_open_task("second")
        self.service.add_dependency(second.task_key, first.task_key)
        before_events = len(self.service.events_for(second.task_key))
        before_rows = self.conn.execute("SELECT COUNT(*) AS n FROM task_dependencies").fetchone()["n"]
        try:
            self.service.add_dependency(second.task_key, first.task_key)
        except Exception:
            pass
        self.assertEqual(len(self.service.events_for(second.task_key)), before_events)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM task_dependencies").fetchone()["n"],
            before_rows,
        )

    def test_every_mutation_appends_exactly_one_event(self) -> None:
        task = self.make_open_task("title")
        self.service.add_criterion(task.task_key, "c1")
        self.service.satisfy_criterion(task.task_key, "1")
        self.service.schedule(task.task_key, "2026-09-09", "must")
        self.service.add_evidence(task.task_key, "student_confirmation", "confirmed")
        self.service.complete(task.task_key)
        event_types = [event.event_type for event in self.service.events_for(task.task_key)]
        self.assertEqual(
            event_types,
            [
                "task_created",
                "criterion_added",
                "criterion_satisfied",
                "scheduled",
                "evidence_added",
                "completed",
            ],
        )

    def test_task_and_event_counts_stay_consistent(self) -> None:
        for index in range(5):
            self.make_open_task("task %d" % index)
        task_count = self.conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        event_count = self.conn.execute("SELECT COUNT(*) AS n FROM task_events").fetchone()["n"]
        self.assertEqual(task_count, 5)
        self.assertEqual(event_count, 5)


if __name__ == "__main__":
    unittest.main()
