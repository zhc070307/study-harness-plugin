"""Task lifecycle, identity stability, criteria, evidence and transitions."""

from __future__ import annotations

import unittest

from tests._support import ServiceTestCase
from study_agent.errors import ConflictError, NotFoundError, ValidationError


class CourseAndTaskTests(ServiceTestCase):
    def test_course_registration_and_listing(self) -> None:
        course = self.service.add_course("COMP 1023", "Introduction to Python Programming")
        self.assertEqual(course.id, "comp-1023")
        self.assertEqual([item.code for item in self.service.list_courses()], ["COMP 1023"])
        with self.assertRaises(ConflictError):
            self.service.add_course("COMP 1023", "Duplicate")

    def test_unknown_course_rejected(self) -> None:
        with self.assertRaises(NotFoundError):
            self.make_open_task("x", course="MISSING")

    def test_task_key_sequence_and_identity_stability(self) -> None:
        self.service.add_course("COMP 1023", "Python")
        self.service.add_course("MATH1014", "Calculus")
        first = self.make_open_task("first", course="COMP 1023")
        second = self.make_open_task("second", course="MATH1014")
        self.assertEqual(first.task_key, "T-000001")
        self.assertEqual(second.task_key, "T-000002")

        updated = self.service.update_task(
            first.task_key,
            title="renamed",
            course="MATH1014",
            actor="agent",
            due_at="2026-09-20T23:59:00+08:00",
        )
        self.assertEqual(updated.id, first.id)
        self.assertEqual(updated.task_key, first.task_key)
        self.assertEqual(updated.title, "renamed")
        self.assertEqual(updated.course_id, second.course_id)
        self.assertEqual(updated.actor, "agent")

    def test_update_rejects_invalid_actor(self) -> None:
        task = self.make_open_task("title")
        with self.assertRaises(ValidationError):
            self.service.update_task(task.task_key, actor="nobody")

    def test_update_noop_and_same_value_conflict(self) -> None:
        task = self.make_open_task("title")
        with self.assertRaises(ValidationError):
            self.service.update_task(task.task_key)
        with self.assertRaises(ConflictError):
            self.service.update_task(task.task_key, title="title")

    def test_update_completed_rejected(self) -> None:
        task = self.make_open_task("title")
        self.service.add_evidence(task.task_key, "student_confirmation", "done")
        self.service.complete(task.task_key)
        with self.assertRaises(ConflictError):
            self.service.update_task(task.task_key, title="later")

    def test_proposal_authorization(self) -> None:
        task = self.service.create_task("proposal")
        self.assertEqual(task.status, "proposed")
        opened = self.service.authorize(task.task_key)
        self.assertEqual(opened.status, "open")
        self.assertEqual(self.count_events(task.task_key, "proposal_authorized"), 1)
        with self.assertRaises(ConflictError):
            self.service.authorize(task.task_key)

    def test_creation_event_and_key_are_written_in_same_transaction(self) -> None:
        task = self.make_open_task("title", criteria=["c1", "c2"])
        events = self.service.events_for(task.task_key)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "task_created")
        self.assertEqual(events[0].payload["criteria"], ["c1", "c2"])
        criteria = self.service.criteria_for(task.id)
        self.assertEqual([item.position for item in criteria], [1, 2])


class CriteriaAndCompletionTests(ServiceTestCase):
    def test_satisfy_and_reopen_criterion(self) -> None:
        task = self.make_open_task("title", criteria=["required"])
        satisfied = self.service.satisfy_criterion(task.task_key, "1")
        self.assertEqual(satisfied.satisfied, 1)
        self.assertIsNotNone(satisfied.satisfied_at)
        with self.assertRaises(ConflictError):
            self.service.satisfy_criterion(task.task_key, "1")
        reopened = self.service.reopen_criterion(task.task_key, "1")
        self.assertEqual(reopened.satisfied, 0)
        with self.assertRaises(ConflictError):
            self.service.reopen_criterion(task.task_key, "1")

    def test_criterion_position_is_scoped_to_its_task(self) -> None:
        first = self.make_open_task("first", criteria=["first criterion"])
        second = self.make_open_task("second", criteria=["second criterion"])
        satisfied = self.service.satisfy_criterion(second.task_key, "1")
        self.assertEqual(satisfied.task_id, second.id)
        self.assertEqual(self.service.criteria_for(first.id)[0].satisfied, 0)
        self.assertEqual(self.service.criteria_for(second.id)[0].satisfied, 1)

    def test_completion_requires_required_criteria(self) -> None:
        task = self.make_open_task("title", criteria=["must do", "must also do"])
        self.service.add_evidence(task.task_key, "student_confirmation", "confirmed")
        with self.assertRaises(ValidationError):
            self.service.complete(task.task_key)
        self.assertEqual(self.service.get_task(task.task_key).status, "open")
        self.service.satisfy_criterion(task.task_key, "1")
        self.service.satisfy_criterion(task.task_key, "2")
        completed = self.service.complete(task.task_key)
        self.assertEqual(completed.status, "completed")
        self.assertIsNotNone(completed.completed_at)

    def test_optional_criteria_do_not_block_completion(self) -> None:
        task = self.make_open_task("title", criteria=["required"])
        self.service.add_criterion(task.task_key, "optional", required=False)
        self.service.satisfy_criterion(task.task_key, "1")
        self.service.add_evidence(task.task_key, "objective_record", "submission record")
        completed = self.service.complete(task.task_key)
        self.assertEqual(completed.status, "completed")

    def test_completion_requires_evidence(self) -> None:
        task = self.make_open_task("title", criteria=["done"])
        self.service.satisfy_criterion(task.task_key, "1")
        with self.assertRaises(ValidationError):
            self.service.complete(task.task_key)

    def test_artifact_evidence_cannot_close_task(self) -> None:
        task = self.make_open_task("title", criteria=["done"])
        self.service.satisfy_criterion(task.task_key, "1")
        self.service.add_evidence(task.task_key, "artifact", "notes.pdf exists")
        with self.assertRaises(ValidationError):
            self.service.complete(task.task_key)

    def test_evidence_kind_validation(self) -> None:
        task = self.make_open_task("title")
        with self.assertRaises(ValidationError):
            self.service.add_evidence(task.task_key, "vibes", "looks good")

    def test_complete_twice_rejected(self) -> None:
        task = self.make_open_task("title")
        self.service.add_evidence(task.task_key, "student_confirmation", "confirmed")
        self.service.complete(task.task_key)
        with self.assertRaises(ConflictError):
            self.service.complete(task.task_key)


class LifecycleTransitionTests(ServiceTestCase):
    def _completable(self, title="title"):
        task = self.make_open_task(title)
        self.service.add_evidence(task.task_key, "student_confirmation", "confirmed")
        return task

    def test_cancel_and_reopen(self) -> None:
        task = self.make_open_task("title")
        self.service.set_placement(task.task_key, kind="scheduled", scheduled_date="2026-09-09", priority="must")
        cancelled = self.service.cancel(task.task_key, reason="no longer needed")
        self.assertEqual(cancelled.status, "cancelled")
        self.assertIsNone(self.service.placement_for(task.task_key))
        reopened = self.service.reopen(
            task.task_key,
            "still needed",
            placement={"kind": "scheduled", "scheduled_date": "2026-09-10", "priority": "must"},
        )
        self.assertEqual(reopened.status, "open")
        placed = self.service.placement_for(task.task_key)
        self.assertEqual(placed.scheduled_date, "2026-09-10")
        event = self.service.events_for(task.task_key)[-1]
        self.assertEqual(event.event_type, "scheduled")
        reopened_events = [item for item in self.service.events_for(task.task_key) if item.event_type == "reopened"]
        self.assertEqual(reopened_events[-1].payload["reason"], "still needed")

    def test_reopen_without_destination_is_rejected(self) -> None:
        task = self._completable()
        self.service.complete(task.task_key)
        with self.assertRaises(ConflictError):
            self.service.reopen(task.task_key, "no destination yet")

    def test_reopen_requires_reason(self) -> None:
        task = self._completable()
        self.service.complete(task.task_key)
        with self.assertRaises(ValidationError):
            self.service.reopen(task.task_key, "   ")

    def test_reopen_completed_task(self) -> None:
        task = self._completable()
        self.service.complete(task.task_key)
        reopened = self.service.reopen(
            task.task_key,
            "wrongly closed",
            placement={
                "kind": "waiting",
                "waiting_reason": "awaiting materials",
                "next_check_at": "2026-09-12",
            },
        )
        self.assertEqual(reopened.status, "open")
        self.assertIsNone(reopened.completed_at)
        self.assertEqual(self.count_events(task.task_key, "reopened"), 1)

    def test_reopen_open_task_rejected(self) -> None:
        task = self.make_open_task("title")
        with self.assertRaises(ConflictError):
            self.service.reopen(task.task_key, "not closed")

    def test_supersede_requires_valid_replacement(self) -> None:
        first = self.make_open_task("old")
        second = self.make_open_task("new")
        superseded = self.service.supersede(first.task_key, second.task_key, reason="replaced")
        self.assertEqual(superseded.status, "superseded")
        self.assertEqual(superseded.superseded_by, second.id)
        with self.assertRaises(ConflictError):
            self.service.reopen(first.task_key, "cannot reopen superseded")

    def test_supersede_by_completed_target_rejected(self) -> None:
        first = self.make_open_task("old")
        second = self._completable("new")
        self.service.complete(second.task_key)
        with self.assertRaises(ValidationError):
            self.service.supersede(first.task_key, second.task_key)

    def test_supersede_self_rejected(self) -> None:
        task = self.make_open_task("self")
        with self.assertRaises(ValidationError):
            self.service.supersede(task.task_key, task.task_key)

    def test_cancel_completed_rejected(self) -> None:
        task = self._completable()
        self.service.complete(task.task_key)
        with self.assertRaises(ConflictError):
            self.service.cancel(task.task_key)

    def test_evidence_on_completed_rejected(self) -> None:
        task = self._completable()
        self.service.complete(task.task_key)
        with self.assertRaises(ConflictError):
            self.service.add_evidence(task.task_key, "student_confirmation", "again")


if __name__ == "__main__":
    unittest.main()
