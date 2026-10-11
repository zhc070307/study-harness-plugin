"""The reconcile pipeline: prepare -> Codex proposal -> validate -> apply.

``prepare`` only gathers facts. ``validate`` reports every problem at once.
``apply`` re-runs the same validation and then commits the whole proposal in one
outer transaction, so a partially applied plan can never be observed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import db
from .errors import ConflictError, NotFoundError, ValidationError
from .models import (
    HKT,
    Clock,
    compact_json,
    completion_evidence_kinds,
    from_iso,
    parse_date,
    to_iso,
    week_start_of,
)
from .planning import PlanningService
from .proposal import ProposalValidator, validate_paths_within_roots
from .reconciliation import Reconciler, calendar_fingerprint
from .service import TaskService
from .workflows import CourseWorkflowConfig, WorkflowRegistry
from .uuid7 import uuid7

PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _digest(payload: Any) -> str:
    return hashlib.sha256(compact_json(payload).encode("utf-8")).hexdigest()


class ReconcileService:
    """Coordinates one reconciliation run end to end."""

    def __init__(self, conn: sqlite3.Connection, clock: Optional[Clock] = None):
        self.conn = conn
        self.clock = clock or Clock()
        self.tasks = TaskService(conn, clock=self.clock)
        self.planning = PlanningService(conn, clock=self.clock)
        self.reconciler = Reconciler(conn, clock=self.clock)
        self.registry = WorkflowRegistry(conn, clock=self.clock)
        self.conn.row_factory = sqlite3.Row

    # -------------------------------------------------------------- prepare
    def prepare(
        self,
        *,
        configs: Sequence[CourseWorkflowConfig],
        mode: str = "daily",
        trigger: str = "manual",
        calendar_events: Optional[Sequence[Dict[str, Any]]] = None,
        calendar_error: Optional[str] = None,
        query_date: Optional[str] = None,
        scan_files: bool = True,
        scheduled_for: Optional[str] = None,
    ) -> Dict[str, Any]:
        # A missed run does not replay each skipped day; one catchup covers the
        # whole interval since the last successful reconciliation.
        kind = {"weekly": "weekly", "daily": "daily", "catchup": "catchup",
                "manual": "manual", "cutover": "cutover"}.get(mode, "manual")
        if mode == "daily" and self.reconciler.needs_catchup(to_iso(self.clock.now())):
            previous = self.reconciler.last_successful()
            if previous is not None and (self.clock.now() - from_iso(previous)) > timedelta(hours=36):
                kind = "catchup"
        period_start, period_end = self.reconciler.missed_interval(to_iso(self.clock.now()))
        # The proposal is built against the revision the caller will see, so read
        # it before this run's own bookkeeping writes bump it.
        base_revision = db.planning_revision(self.conn)
        run_key = str(uuid7(self.clock.now_ms()))
        run_id = self.planning.start_run(
            kind,
            run_key=run_key,
            trigger=trigger,
            period_start=period_start,
            period_end=period_end,
            scheduled_for=scheduled_for,
            base_revision=base_revision,
        )
        file_report = self.reconciler.scan_files(configs) if scan_files else {"courses": {}}
        calendar_report: Dict[str, Any] = {}
        if calendar_events is not None:
            calendar_report = self.reconciler.scan_calendar(calendar_events, configs)
        context = self.reconciler.build_context(
            mode=mode,
            configs=configs,
            period_start=period_start,
            period_end=period_end,
            query_date=query_date,
            calendar_events=calendar_events,
            calendar_error=calendar_error,
            trigger=trigger,
            run_id=run_id,
            base_revision=base_revision,
        )
        context["base_revision"] = base_revision
        context["run_key"] = run_key
        context["run_id"] = run_id
        context["file_scan"] = file_report
        context["calendar_scan"] = calendar_report
        context["input_digest"] = _digest(
            {
                "revision": context["base_revision"],
                "period": context["period"],
                "files": file_report.get("new_versions", []),
                "calendar": calendar_report.get("new", []),
            }
        )
        with self.tasks._write():
            self.conn.execute(
                "UPDATE planning_runs SET input_digest = ? WHERE id = ?",
                (context["input_digest"], run_id),
            )
            self.conn.execute(
                """
                UPDATE planning_runs SET period_start = ?, period_end = ?, scheduled_for = ?
                WHERE id = ?
                """,
                (period_start, period_end, scheduled_for, run_id),
            )
        self._sync_open_workflow_statuses()
        return context

    def _sync_open_workflow_statuses(self) -> None:
        for row in self.registry.open_instances():
            self.registry.refresh_status(row["id"])

    # ------------------------------------------------------------- validate
    def validate(
        self,
        proposal: Dict[str, Any],
        *,
        configs: Sequence[CourseWorkflowConfig],
        query_date: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> List[str]:
        """Return every problem with the proposal; empty means it can be applied."""
        errors = ProposalValidator(
            self.conn,
            configs,
            query_date=query_date,
            current_revision=self._expected_revision(proposal),
        ).validate(proposal)
        errors.extend(validate_paths_within_roots(proposal, configs, PROJECT_ROOT))
        if run_id is not None:
            existing = self.planning.published_run(proposal["run_key"])
            if existing is not None and existing["id"] != run_id:
                errors.append(
                    "run_key %s was already published by another run; reuse is only for retrying "
                    "the same run" % proposal["run_key"]
                )
        return errors

    def _expected_revision(self, proposal: Dict[str, Any]) -> Optional[int]:
        """The revision the plan was built from.

        A run's own bookkeeping (run row, scan digests) bumps ``planning_state``
        too, so the live revision is not the right yardstick. The run's recorded
        ``base_revision`` is. Only when that is unavailable do we compare against
        a live value, which also catches a proposal staged against another run.
        """
        row = self.planning.run_by_key(proposal["run_key"])
        if row is not None:
            return int(row["base_revision"])
        return db.planning_revision(self.conn)

    def record_failure(self, run_id: str, errors: Sequence[str], *, stage: str) -> None:
        with self.tasks._write():
            self.conn.execute(
                """
                UPDATE planning_runs
                SET status = 'failed', completed_at = ?, error_json = ?
                WHERE id = ?
                """,
                (
                    to_iso(self.clock.now()),
                    compact_json({"stage": stage, "errors": list(errors)}),
                    run_id,
                ),
            )

    # ---------------------------------------------------------------- apply
    def apply(
        self,
        proposal: Dict[str, Any],
        *,
        configs: Sequence[CourseWorkflowConfig],
        query_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate again, then commit every operation in one transaction."""
        run_key = proposal["run_key"]
        already = self.planning.published_run(run_key)
        if already is not None:
            # Re-applying the same run_key returns the first result; it must not
            # create a second copy of anything.
            return {
                "run_id": already["id"],
                "run_key": run_key,
                "duplicate": True,
                "result": json.loads(already["result_json"]) if already["result_json"] else {},
            }

        errors = self.validate(proposal, configs=configs, query_date=query_date)
        if errors:
            raise ValidationError("proposal rejected: %s" % "; ".join(errors))

        run_id = self._run_id_for(proposal)
        confirmation = proposal.get("confirmation", "none")
        result: Dict[str, Any] = {
            "created": [],
            "updated": [],
            "placements": [],
            "observations": [],
            "instances": [],
            "assessments": [],
            "issues": [],
        }

        with self.tasks._write():
            operations = list(proposal.get("operations", []))
            created_ids: Dict[int, str] = {}
            for index, operation in enumerate(operations):
                self._apply_operation(operation, index, run_id, result, created_ids)

            report = self.planning.audit(query_date, strict=True)
            if report.problems:
                raise ValidationError(
                    "plan rejected: the resulting state would be invalid: %s"
                    % "; ".join(report.problems)
                )

            if proposal.get("week_start"):
                if confirmation == "confirmed":
                    self.planning.confirm_week(proposal["week_start"], run_id=run_id)
                elif confirmation == "provisional":
                    # ``provisional`` only describes a week that has not been
                    # confirmed yet. Re-marking an already-confirmed week would
                    # rewrite its visible text on every run for no reason.
                    self.planning.mark_provisional(proposal["week_start"])
                else:
                    self.planning.adopt_week(proposal["week_start"], run_id=run_id)

            week_start = proposal.get("week_start")
            db.set_planning_state(
                self.conn,
                last_successful_reconciliation_at=to_iso(self.clock.now()),
                current_week_start=week_start or None,
                current_week_run_id=run_id,
            )
            self.conn.execute(
                """
                UPDATE planning_runs
                SET status = 'published', confirmation = ?, completed_at = ?,
                    proposal_digest = ?, proposal_json = ?, result_json = ?, error_json = NULL
                WHERE id = ?
                """,
                (
                    confirmation,
                    to_iso(self.clock.now()),
                    _digest(proposal),
                    compact_json(proposal),
                    compact_json(result),
                    run_id,
                ),
            )
            self.conn.execute(
                "UPDATE planning_runs SET result_revision = ? WHERE id = ?",
                (db.planning_revision(self.conn), run_id),
            )
        result["run_id"] = run_id
        result["run_key"] = run_key
        result["revision"] = db.planning_revision(self.conn)
        return result

    def _run_id_for(self, proposal: Dict[str, Any]) -> str:
        row = self.planning.run_by_key(proposal["run_key"])
        if row is not None:
            return row["id"]
        return self.planning.start_run("manual", run_key=proposal["run_key"], trigger="manual")

    def _apply_operation(
        self,
        operation: Dict[str, Any],
        index: int,
        run_id: str,
        result: Dict[str, Any],
        created_ids: Dict[int, str],
    ) -> None:
        op = operation["op"]

        if op == "create_task":
            task = self.planning._apply_create_task(operation, "codex")
            created_ids[index] = task.task_key
            result["created"].append(task.task_key)
            if operation.get("placement") is not None:
                self.planning._apply_placement(task.task_key, operation["placement"], run_id, "codex")
                result["placements"].append(task.task_key)
            return

        if op == "authorize_task":
            task = self.tasks.authorize(operation["task"], operator="codex")
            result["updated"].append(task.task_key)
            if operation.get("placement") is not None:
                self.planning._apply_placement(task.task_key, operation["placement"], run_id, "codex")
                result["placements"].append(task.task_key)
            return

        if op == "update_task":
            payload = {
                key: operation[key]
                for key in ("title", "description", "due_at", "source", "actor")
                if key in operation
            }
            task = self.tasks.update_task(operation["task"], **payload)
            result["updated"].append(task.task_key)
            return

        if op == "supersede_task":
            task = self.tasks.supersede(
                operation["task"], operation["by"], reason=operation.get("reason"), operator="codex"
            )
            result["updated"].append(task.task_key)
            return

        if op == "cancel_task":
            task = self.tasks.cancel(
                operation["task"], reason=operation.get("reason"), operator="codex"
            )
            result["updated"].append(task.task_key)
            return

        if op == "add_criterion":
            criterion = self.tasks.add_criterion(
                operation["task"], operation["criterion"], operator="codex"
            )
            result["updated"].append(criterion.task_id)
            return

        if op == "satisfy_criterion":
            self.tasks.satisfy_criterion(operation["task"], operation["criterion"], operator="codex")
            result["updated"].append(operation["task"])
            return

        if op == "add_evidence":
            evidence = operation["evidence"]
            self.tasks.add_evidence(
                operation["task"],
                evidence["kind"],
                evidence["summary"],
                reference=evidence.get("reference"),
                operator="codex",
            )
            result["updated"].append(operation["task"])
            return

        if op == "complete_task":
            for criterion in operation.get("satisfy_criteria", []) or []:
                self.tasks.satisfy_criterion(operation["task"], criterion, operator="codex")
            evidence = operation.get("evidence")
            if evidence is not None:
                self.tasks.add_evidence(
                    operation["task"],
                    evidence["kind"],
                    evidence["summary"],
                    reference=evidence.get("reference"),
                    operator="codex",
                )
            task = self.tasks.complete(
                operation["task"], note=operation.get("note"), operator="codex"
            )
            result["updated"].append(task.task_key)
            return

        if op == "set_placement":
            placement = self.planning._apply_placement(
                operation["task"], operation, run_id, "codex"
            )
            result["placements"].append(operation["task"])
            return

        if op == "clear_placement":
            row = self.tasks._resolve_task_row(operation["task"])
            self.tasks.clear_placement(row["id"])
            result["updated"].append(row["task_key"])
            return

        if op == "record_miss":
            self.planning.record_miss(
                operation["task"],
                confirmed=bool(operation.get("confirmed")),
                reason=operation.get("reason"),
                operator="codex",
            )
            result["updated"].append(operation["task"])
            return

        if op == "bind_observation":
            self.reconciler.bind_observation(
                operation["observation_id"],
                course=operation.get("course"),
                status=operation.get("binding_status", "bound"),
                session_key=operation.get("session_key"),
                artifact_kind=operation.get("artifact_kind"),
            )
            result["observations"].append(operation["observation_id"])
            return

        if op == "ensure_workflow_instance":
            course_row = self.tasks._resolve_course_row(operation["course"])
            instance_id = self.registry.ensure_instance(
                course_row["id"],
                operation["workflow_id"],
                operation["occurrence_key"],
                run_id=run_id,
            )
            for observation_id in operation.get("observation_ids", []) or []:
                self.registry.link_observation(instance_id, observation_id)
            result["instances"].append(instance_id)
            created_ids[index] = instance_id
            return

        if op == "link_workflow_observation":
            instance_id = operation.get("instance_id")
            observation_ids = operation.get("observation_ids") or [operation.get("observation_id")]
            for observation_id in observation_ids:
                if observation_id:
                    self.registry.link_observation(instance_id, observation_id)
            result["observations"].extend(item for item in observation_ids if item)
            return

        if op == "link_workflow_task":
            instance_id = self._resolve_instance(operation)
            task_id = self.tasks._resolve_task_row(operation["task"])["id"]
            linked = self.registry.link_task(instance_id, operation["step_id"], task_id)
            if not linked:
                raise ConflictError(
                    "workflow step %s already has a different task; reuse the existing one"
                    % operation["step_id"]
                )
            result["instances"].append(instance_id)
            return

        if op == "record_assessment":
            attempt_id, issue_ids = self._record_assessment(operation, run_id)
            result["assessments"].append(attempt_id)
            result["issues"].extend(issue_ids)
            created_ids[index] = attempt_id
            return

        if op == "record_issue_result":
            self._record_issue_result(operation)
            result["issues"].append(operation["issue_id"])
            return

        raise ValidationError("unsupported apply operation: %r" % (op,))

    def _resolve_instance(self, operation: Dict[str, Any]) -> str:
        instance_id = operation.get("instance_id")
        if instance_id:
            return instance_id
        if "created_ids" in operation:
            return operation["created_ids"]
        raise ValidationError("link_workflow_task needs an instance_id")

    # ----------------------------------------------------------- assessments
    def _record_assessment(self, operation: Dict[str, Any], run_id: str) -> Tuple[str, List[str]]:
        course_row = self.tasks._resolve_course_row(operation["course"])
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

    def _record_issue_result(self, operation: Dict[str, Any]) -> None:
        stamp = to_iso(self.clock.now())
        attempt_id = operation.get("attempt_id")
        if not attempt_id:
            course_row = self.tasks._resolve_task_row(operation["task"])
            attempt_id = str(uuid7(self.clock.now_ms()))
            self.conn.execute(
                """
                INSERT INTO assessment_attempts (
                    id, course_id, source_kind, session_key, attempt_type, occurred_at,
                    score_text, summary, evidence_ref, created_at
                ) VALUES (?, ?, 'other', NULL, 'retest', ?, NULL, ?, NULL, ?)
                """,
                (
                    attempt_id,
                    course_row["course_id"],
                    stamp,
                    operation.get("note") or "复测结果：%s" % operation["result"],
                    stamp,
                ),
            )
        self.conn.execute(
            """
            INSERT OR REPLACE INTO assessment_issue_results
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
                UPDATE learning_issues SET updated_at = ? WHERE id = ?
                """,
                (stamp, operation["issue_id"]),
            )
