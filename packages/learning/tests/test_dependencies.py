"""Dependency blocking, self-dependency rejection and cycle detection."""

from __future__ import annotations

import unittest

from tests._support import ServiceTestCase
from study_agent.errors import ConflictError, NotFoundError, ValidationError


class DependencyTests(ServiceTestCase):
    def test_blocked_then_ready(self) -> None:
        prerequisite = self.make_open_task("prerequisite")
        dependent = self.make_open_task("dependent")
        self.service.add_dependency(dependent.task_key, prerequisite.task_key)

        result = self.service.readiness(dependent.task_key)
        self.assertFalse(result["ready"])
        self.assertEqual([item["task_key"] for item in result["blocked_by"]], [prerequisite.task_key])

        self.service.add_evidence(prerequisite.task_key, "student_confirmation", "done")
        self.service.complete(prerequisite.task_key)

        result = self.service.readiness(dependent.task_key)
        self.assertTrue(result["ready"])
        self.assertEqual(result["blocked_by"], [])

    def test_self_dependency_rejected(self) -> None:
        task = self.make_open_task("self")
        with self.assertRaises(ValidationError):
            self.service.add_dependency(task.task_key, task.task_key)

    def test_dependency_cycle_rejected(self) -> None:
        a = self.make_open_task("a")
        b = self.make_open_task("b")
        c = self.make_open_task("c")
        self.service.add_dependency(b.task_key, a.task_key)
        self.service.add_dependency(c.task_key, b.task_key)
        with self.assertRaises(ConflictError):
            self.service.add_dependency(a.task_key, c.task_key)

    def test_duplicate_dependency_rejected(self) -> None:
        a = self.make_open_task("a")
        b = self.make_open_task("b")
        self.service.add_dependency(b.task_key, a.task_key)
        with self.assertRaises(ConflictError):
            self.service.add_dependency(b.task_key, a.task_key)

    def test_remove_dependency(self) -> None:
        a = self.make_open_task("a")
        b = self.make_open_task("b")
        self.service.add_dependency(b.task_key, a.task_key)
        self.service.remove_dependency(b.task_key, a.task_key)
        self.assertTrue(self.service.readiness(b.task_key)["ready"])
        with self.assertRaises(NotFoundError):
            self.service.remove_dependency(b.task_key, a.task_key)

    def test_dependency_on_proposed_task_still_blocks(self) -> None:
        proposal = self.service.create_task("proposal")
        dependent = self.make_open_task("dependent")
        self.service.add_dependency(dependent.task_key, proposal.task_key)
        self.assertFalse(self.service.readiness(dependent.task_key)["ready"])

    def test_readiness_applicable_flag_for_closed_tasks(self) -> None:
        task = self.make_open_task("title")
        self.assertTrue(self.service.readiness(task.task_key)["applicable"])
        self.service.add_evidence(task.task_key, "student_confirmation", "confirmed")
        self.service.complete(task.task_key)
        result = self.service.readiness(task.task_key)
        self.assertFalse(result["applicable"])
        self.assertFalse(result["ready"])


if __name__ == "__main__":
    unittest.main()
