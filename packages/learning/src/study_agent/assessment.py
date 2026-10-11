"""Assessment facts and retest results, independent of the retired planner."""
from datetime import timedelta
from typing import Any, Dict, List, Tuple
from .models import Clock, to_iso, parse_date, ISSUE_RESULTS
from .service import TaskService
from .uuid7 import uuid7
from .errors import NotFoundError, ValidationError


class AssessmentService:
    def __init__(self, conn, clock=None):
        self.conn = conn
        self.clock = clock or Clock()
        self.tasks = TaskService(conn, clock=self.clock)

    # ----------------------------------------------------------- assessments
    def record_assessment(self, operation: Dict[str, Any]) -> Tuple[str, List[str]]:
        course_row = self.tasks._resolve_course_row(operation["course"])
        if operation.get('issues') or operation.get('attempt_type') == 'retest':
            evidence = operation.get('evidence_ref')
            if not isinstance(evidence, str) or not evidence.strip():
                raise ValidationError('Issues and retests require original answer evidence')
        stamp = to_iso(self.clock.now())
        attempt_id = str(uuid7(self.clock.now_ms()))
        occurred = operation.get("occurred_at") or stamp
        self.conn.execute(
            """
            INSERT INTO assessment_attempts (
                id, course_id, source_kind, session_key, attempt_type, occurred_at,
                score_text, summary, evidence_ref, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                course_row["id"],
                operation["source_kind"],
                operation.get("session_key"),
                operation.get("attempt_type", "initial"),
                occurred,
                operation.get("score_text"),
                operation["summary"],
                operation.get("evidence_ref"),
                stamp,
            ),
        )
        issue_ids: List[str] = []
        gap = operation.get("retest_gap_days")
        for issue in operation.get("issues", []) or []:
            issue_id = str(uuid7(self.clock.now_ms()))
            next_retest = issue.get("next_retest_on")
            if next_retest is None and isinstance(gap, int) and gap >= 0:
                base = parse_date(occurred[:10])
                next_retest = (base + timedelta(days=gap)).isoformat()
            self.conn.execute(
                """
                INSERT INTO learning_issues (
                    id, course_id, concept, issue_type, status, source_attempt_id,
                    next_retest_on, criterion_id, resolved_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    issue_id,
                    course_row["id"],
                    issue["concept"],
                    issue["issue_type"],
                    attempt_id,
                    next_retest,
                    stamp,
                    stamp,
                ),
            )
            self.conn.execute(
                """
                INSERT INTO assessment_issue_results (attempt_id, issue_id, result, recorded_at)
                VALUES (?, ?, 'failed', ?)
                """,
                (attempt_id, issue_id, stamp),
            )
            issue_ids.append(issue_id)
        return attempt_id, issue_ids

    def record_issue_result(self, operation: Dict[str, Any]) -> None:
        stamp = to_iso(self.clock.now())
        if operation['result'] not in ISSUE_RESULTS:
            raise ValidationError('Unknown retest result')
        if operation.get('next_retest_on') is not None:
            parse_date(operation['next_retest_on'])
        issue = self.conn.execute('SELECT * FROM learning_issues WHERE id=?', (operation['issue_id'],)).fetchone()
        if issue is None:
            raise NotFoundError('learning issue not found')
        if operation.get('task'):
            task = self.tasks._resolve_task_row(operation['task'])
            if task['course_id'] != issue['course_id']:
                raise ValidationError('Retest task must belong to the issue course')
        attempt_id = operation.get("attempt_id")
        if attempt_id:
            attempt = self.conn.execute('SELECT * FROM assessment_attempts WHERE id=?', (attempt_id,)).fetchone()
            if (attempt is None or attempt['course_id'] != issue['course_id'] or
                    attempt['attempt_type'] != 'retest' or not isinstance(attempt['evidence_ref'], str) or not attempt['evidence_ref'].strip()):
                raise ValidationError('Retest requires an evidenced attempt from the issue course')
            if self.conn.execute('SELECT 1 FROM assessment_issue_results WHERE attempt_id=? AND issue_id=?',
                                 (attempt_id, issue['id'])).fetchone():
                raise ValidationError('Keep previous results; record a new retest attempt for a new result')
        else:
            evidence = operation.get('evidence_ref')
            if not isinstance(evidence, str) or not evidence.strip():
                raise ValidationError('Retest requires original answer evidence')
            attempt_id = str(uuid7(self.clock.now_ms()))
            self.conn.execute(
                """
                INSERT INTO assessment_attempts (
                    id, course_id, source_kind, session_key, attempt_type, occurred_at,
                    score_text, summary, evidence_ref, created_at
                ) VALUES (?, ?, 'other', NULL, 'retest', ?, NULL, ?, ?, ?)
                """,
                (
                    attempt_id,
                    issue["course_id"],
                    stamp,
                    operation.get("note") or "复测结果：%s" % operation["result"],
                    evidence.strip(),
                    stamp,
                ),
            )
        self.conn.execute(
            """
            INSERT INTO assessment_issue_results
                (attempt_id, issue_id, result, recorded_at)
            VALUES (?, ?, ?, ?)
            """,
            (attempt_id, operation["issue_id"], operation["result"], stamp),
        )
        if operation["result"] == "passed":
            self.conn.execute(
                """
                UPDATE learning_issues
                SET status = 'passed', resolved_at = ?, next_retest_on = NULL, updated_at = ?
                WHERE id = ?
                """,
                (stamp, stamp, operation["issue_id"]),
            )
        else:
            self.conn.execute(
                """
                UPDATE learning_issues SET status='pending', resolved_at=NULL, next_retest_on=COALESCE(?,next_retest_on), updated_at = ? WHERE id = ?
                """,
                (operation.get("next_retest_on"), stamp, operation["issue_id"]),
            )
