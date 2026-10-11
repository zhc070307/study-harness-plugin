"""Quiz attempts, learning issues, grouped retests, pass/fail, history retention."""

from __future__ import annotations

import unittest

from tests._support_v3 import PlanningTestCase
from study_agent import db
from study_agent.errors import ValidationError


class AssessmentRecordingTests(PlanningTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.service.add_course("MGMT2110", "Organizational Behavior")

    def _attempt(self, **overrides):
        operation = {
            "op": "record_assessment",
            "course": "MGMT2110",
            "source_kind": "textbook_quiz",
            "session_key": "Chapter 2",
            "attempt_type": "initial",
            "occurred_at": "2026-09-10T04:00:00+00:00",
            "score_text": "8/10",
            "summary": "Chapters 2、5 首轮英文 MC",
            "issues": [
                {"concept": "MARS model", "issue_type": "wrong"},
                {"concept": "contingency variable", "issue_type": "guessed"},
            ],
            "retest_gap_days": 3,
        }
        operation.update(overrides)
        return operation

    def _record(self, **overrides):
        operation = self._attempt(**overrides)
        with self.service._write():
            return self.reconcile._record_assessment(operation, run_id="")

    def test_attempt_and_issues_are_saved(self) -> None:
        attempt_id, issue_ids = self._record()
        self.assertEqual(len(issue_ids), 2)
        self.assertEqual(db.count_rows(self.conn, "assessment_attempts"), 1)
        self.assertEqual(db.count_rows(self.conn, "learning_issues"), 2)

    def test_issues_start_pending_with_a_retest_date(self) -> None:
        _, issue_ids = self._record()
        rows = self.conn.execute("SELECT * FROM learning_issues").fetchall()
        for row in rows:
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["next_retest_on"], "2026-09-13")
            self.assertIsNone(row["resolved_at"])

    def test_each_issue_links_back_to_its_attempt(self) -> None:
        attempt_id, issue_ids = self._record()
        rows = self.conn.execute(
            "SELECT * FROM assessment_issue_results WHERE attempt_id = ?", (attempt_id,)
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["result"] == "failed" for row in rows))

    def test_only_real_problems_are_stored(self) -> None:
        """A correct answer must never become a long-term issue."""
        attempt_id, issue_ids = self._record(issues=[])
        self.assertEqual(issue_ids, [])
        self.assertEqual(db.count_rows(self.conn, "learning_issues"), 0)

    def test_a_retest_gap_can_be_omitted_for_courses_without_one(self) -> None:
        _, issue_ids = self._record(retest_gap_days=None)
        row = self.conn.execute(
            "SELECT next_retest_on FROM learning_issues WHERE id = ?", (issue_ids[0],)
        ).fetchone()
        self.assertIsNone(row["next_retest_on"])


class RetestTests(PlanningTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.service.add_course("MGMT2110", "Organizational Behavior")
        operation = {
            "op": "record_assessment",
            "course": "MGMT2110",
            "source_kind": "textbook_quiz",
            "attempt_type": "initial",
            "occurred_at": "2026-09-10T04:00:00+00:00",
            "summary": "first attempt",
            "issues": [
                {"concept": "MARS model", "issue_type": "wrong"},
                {"concept": "contingency variable", "issue_type": "guessed"},
            ],
            "retest_gap_days": 3,
        }
        with self.service._write():
            self.attempt_id, self.issue_ids = self.reconcile._record_assessment(operation, run_id="")
        self.retest_task = self.service.create_task(
            "复测 Chapter 2 的错误概念", course="MGMT2110", status="open",
            criteria=["MARS model 能独立解释", "contingency variable 能判断出"],
        )
        self.place(self.retest_task.task_key, date="2026-09-13")

    def _result(self, issue_id, result):
        with self.service._write():
            self.reconcile._record_issue_result(
                {"op": "record_issue_result", "issue_id": issue_id,
                 "result": result, "task": self.retest_task.task_key}
            )

    def test_passing_an_issue_resolves_it(self) -> None:
        self._result(self.issue_ids[0], "passed")
        row = self.conn.execute(
            "SELECT * FROM learning_issues WHERE id = ?", (self.issue_ids[0],)
        ).fetchone()
        self.assertEqual(row["status"], "passed")
        self.assertIsNotNone(row["resolved_at"])
        self.assertIsNone(row["next_retest_on"])

    def test_failing_an_issue_keeps_it_pending(self) -> None:
        self._result(self.issue_ids[0], "failed")
        row = self.conn.execute(
            "SELECT * FROM learning_issues WHERE id = ?", (self.issue_ids[0],)
        ).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["resolved_at"])

    def test_uncertain_is_not_a_pass(self) -> None:
        self._result(self.issue_ids[0], "uncertain")
        row = self.conn.execute(
            "SELECT * FROM learning_issues WHERE id = ?", (self.issue_ids[0],)
        ).fetchone()
        self.assertEqual(row["status"], "pending")

    def test_issues_can_pass_independently(self) -> None:
        self._result(self.issue_ids[0], "passed")
        rows = self.conn.execute(
            "SELECT status FROM learning_issues ORDER BY concept"
        ).fetchall()
        statuses = sorted(row["status"] for row in rows)
        self.assertEqual(statuses, ["passed", "pending"])

    def test_every_retest_is_recorded_as_an_attempt(self) -> None:
        self._result(self.issue_ids[0], "passed")
        rows = self.conn.execute(
            "SELECT * FROM assessment_attempts WHERE attempt_type = 'retest'"
        ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_history_is_retained_after_a_pass(self) -> None:
        self._result(self.issue_ids[0], "passed")
        self.assertEqual(db.count_rows(self.conn, "learning_issues"), 2)
        self.assertEqual(db.count_rows(self.conn, "assessment_attempts"), 2)

    def test_retest_task_completes_when_all_issues_pass(self) -> None:
        for index, issue in enumerate(self.issue_ids):
            self._result(issue, "passed")
            self.service.satisfy_criterion(self.retest_task.task_key, str(index + 1))
        self.service.add_evidence(self.retest_task.task_key, "student_confirmation", "复测完成")
        task = self.service.complete(self.retest_task.task_key)
        self.assertEqual(task.status, "completed")

    def test_pending_issues_surface_in_the_context(self) -> None:
        ctx = self.reconcile.prepare(
            configs=self.configs, mode="manual", scan_files=False,
            query_date="2026-09-13", calendar_events=[],
        )
        pending = ctx["pending_learning_issues"]
        concepts = {item["concept"] for item in pending}
        self.assertIn("MARS model", concepts)
        self.assertIn("contingency variable", concepts)

    def test_unknown_issue_is_rejected(self) -> None:
        from study_agent.errors import NotFoundError

        with self.assertRaises(Exception):
            self._result("no-such-issue", "passed")


class AssessmentValidationTests(PlanningTestCase):
    def test_a_quiz_attempt_needs_a_course_and_a_summary(self) -> None:
        self.service.add_course("MGMT2110", "OB")
        proposal = self.proposal([
            {"op": "record_assessment", "source_kind": "textbook_quiz",
             "summary": "x", "occurred_at": "2026-09-10T04:00:00+00:00"}
        ])
        errors = self.reconcile.validate(proposal, configs=self.configs, query_date="2026-09-10")
        self.assertTrue(any("course" in item for item in errors))

    def test_an_unknown_source_kind_is_rejected(self) -> None:
        self.service.add_course("MGMT2110", "OB")
        proposal = self.proposal([
            {"op": "record_assessment", "course": "MGMT2110", "source_kind": "made_up",
             "summary": "x", "occurred_at": "2026-09-10T04:00:00+00:00"}
        ])
        errors = self.reconcile.validate(proposal, configs=self.configs, query_date="2026-09-10")
        self.assertTrue(any("source_kind" in item for item in errors))


if __name__ == "__main__":
    unittest.main()
