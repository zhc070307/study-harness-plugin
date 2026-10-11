"""Planning layer: destinations, invariants, weekly commitment and audited batch apply.

The Task Service owns task lifecycle. This module owns *where every open task
currently is* and refuses to publish a plan that would leave an open task with
no destination, in the past, or waiting without a review point.

``PlanningService`` is the only new service class in v3. Everything else lives
in pure functions and small data classes so the layers stay separable.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import db
from .errors import ConflictError, NotFoundError, ValidationError
from .models import (
    DESTINATIONS,
    PLANNING_CONFIRMATIONS,
    PLANNING_RUN_KINDS,
    PLANNING_RUN_STATUSES,
    PLANNING_TRIGGERS,
    PRIORITIES,
    Clock,
    Placement,
    Task,
    compact_json,
    dataclass_dict,
    dataclass_list,
    parse_date,
    to_iso,
    week_end_of,
    week_start_of,
)
from .service import TaskService
from .uuid7 import uuid7

#: The number of explicit misses after which a task may no longer be quietly carried over.
MAX_DEFERRALS = 2

#: Assessment issues are retested after a short gap; the course config decides
#: whether that default applies, it is not a global hard rule.
DEFAULT_RETEST_GAP_DAYS = 3


def destination_of(
    placement: Optional[Placement], query_date: date
) -> str:
    """Classify one task's current destination relative to ``query_date``."""
    if placement is None:
        return "backlog"
    if placement.kind == "waiting":
        return "waiting"
    scheduled = parse_date(placement.scheduled_date)
    if scheduled < query_date:
        return "overdue"
    if scheduled == query_date:
        return "today"
    if scheduled <= week_end_of(query_date):
        return "this_week"
    return "future"


@dataclass
class AuditEntry:
    task_id: str
    task_key: str
    title: str
    course: Optional[str]
    actor: str
    destination: str
    scheduled_date: Optional[str] = None
    priority: Optional[str] = None
    waiting_reason: Optional[str] = None
    next_check_at: Optional[str] = None
    deferral_count: int = 0
    week_start: Optional[str] = None
    commitment: Optional[str] = None
    version: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return dataclass_dict(self)


@dataclass
class AuditReport:
    date: str
    entries: List[AuditEntry] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def by_destination(self) -> Dict[str, List[AuditEntry]]:
        grouped: Dict[str, List[AuditEntry]] = {}
        for entry in self.entries:
            grouped.setdefault(entry.destination, []).append(entry)
        return grouped

    def as_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "ok": self.ok,
            "problems": self.problems,
            "counts": {
                name: len(items) for name, items in sorted(self.by_destination().items())
            },
            "entries": [entry.as_dict() for entry in self.entries],
        }


class PlanningService:
    """Destination management, invariant auditing and audited batch commits."""

    def __init__(self, conn: sqlite3.Connection, clock: Optional[Clock] = None):
        self.conn = conn
        self.clock = clock or Clock()
        self.tasks = TaskService(conn, clock=self.clock)
        self.conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------------ audit
    def _open_task_rows(self) -> List[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT t.*, c.code AS course_code
            FROM tasks t
            LEFT JOIN courses c ON c.id = t.course_id
            WHERE t.status = 'open'
            ORDER BY t.task_key
            """
        ).fetchall()

    def audit(self, query_date: Optional[str] = None, *, strict: bool = True) -> AuditReport:
        """Report the destination of every open task and any invariant breach.

        ``strict`` additionally treats a second explicit miss as a blocking
        problem, because the plan is then not allowed to silently carry the task
        forward again.
        """
        day = parse_date(query_date) if query_date else self.clock.today()
        report = AuditReport(date=day.isoformat())
        placement_rows = {
            row["task_id"]: row
            for row in self.conn.execute("SELECT * FROM task_placements").fetchall()
        }
        open_ids = set()
        for row in self._open_task_rows():
            open_ids.add(row["id"])
            placed_row = placement_rows.get(row["id"])
            placement = Placement.from_row(placed_row) if placed_row else None
            destination = destination_of(placement, day)
            entry = AuditEntry(
                task_id=row["id"],
                task_key=row["task_key"],
                title=row["title"],
                course=row["course_code"],
                actor=row["actor"],
                destination=destination,
                scheduled_date=placement.scheduled_date if placement else None,
                priority=placement.priority if placement else None,
                waiting_reason=placement.waiting_reason if placement else None,
                next_check_at=placement.next_check_at if placement else None,
                deferral_count=placement.deferral_count if placement else 0,
                week_start=placement.week_start if placement else None,
                commitment=placement.commitment if placement else None,
                version=placement.version if placement else 0,
            )
            report.entries.append(entry)
            if destination == "backlog":
                report.problems.append(
                    "%s「%s」没有当前去向（BACKLOG）" % (row["task_key"], row["title"])
                )
            elif destination == "overdue":
                report.problems.append(
                    "%s「%s」仍安排在已过去的 %s（OVERDUE）"
                    % (row["task_key"], row["title"], entry.scheduled_date)
                )
            elif destination == "waiting" and not entry.next_check_at:
                report.problems.append(
                    "%s「%s」处于等待状态但没有复查时间" % (row["task_key"], row["title"])
                )
            if strict and entry.deferral_count >= MAX_DEFERRALS:
                report.problems.append(
                    "%s「%s」已第 %d 次明确未完成，不能再次简单结转，必须改变做法或由学生决定"
                    % (row["task_key"], row["title"], entry.deferral_count)
                )

        orphans = set(placement_rows) - open_ids
        for task_id in sorted(orphans):
            row = self.conn.execute(
                "SELECT task_key, title, status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            report.problems.append(
                "%s「%s」已不属于开放任务（状态 %s），但仍有 placement"
                % (row["task_key"], row["title"], row["status"])
            )
        return report

    def assert_publishable(self, query_date: Optional[str] = None) -> AuditReport:
        report = self.audit(query_date, strict=True)
        if report.problems:
            raise ValidationError(
                "plan cannot be published with open problems: %s" % "; ".join(report.problems)
            )
        return report

    # ------------------------------------------------------------- week rules
    def week_commitment(self, week_start: str) -> List[AuditEntry]:
        parse_date(week_start)
        rows = self.conn.execute(
            """
            SELECT t.task_key, t.title, c.code AS course_code, p.*
            FROM task_placements p
            JOIN tasks t ON t.id = p.task_id
            LEFT JOIN courses c ON c.id = t.course_id
            WHERE t.status = 'open' AND p.week_start = ?
            ORDER BY CASE p.priority WHEN 'must' THEN 0 WHEN 'should' THEN 1 ELSE 2 END,
                     t.task_key
            """,
            (week_start,),
        ).fetchall()
        entries = []
        for row in rows:
            entries.append(
                AuditEntry(
                    task_id=row["task_id"],
                    task_key=row["task_key"],
                    title=row["title"],
                    course=row["course_code"],
                    actor="",
                    destination=destination_of(Placement.from_row(row), parse_date(week_start)),
                    scheduled_date=row["scheduled_date"],
                    priority=row["priority"],
                    waiting_reason=row["waiting_reason"],
                    next_check_at=row["next_check_at"],
                    deferral_count=row["deferral_count"],
                    week_start=row["week_start"],
                    commitment=row["commitment"],
                    version=row["version"],
                )
            )
        return entries

    def confirm_week(self, week_start: str, *, run_id: Optional[str] = None,
                     operator: str = "agent") -> int:
        """Mark every placement in the week as a confirmed commitment."""
        parse_date(week_start)
        with self.tasks._write():
            rows = self.conn.execute(
                """
                SELECT p.task_id FROM task_placements p
                JOIN tasks t ON t.id = p.task_id
                WHERE t.status = 'open' AND p.week_start = ? AND p.commitment <> 'confirmed'
                """,
                (week_start,),
            ).fetchall()
            stamp = to_iso(self.clock.now())
            for row in rows:
                self.conn.execute(
                    """
                    UPDATE task_placements
                    SET commitment = 'confirmed', version = version + 1, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (stamp, row["task_id"]),
                )
            db.set_planning_state(
                self.conn,
                current_week_start=week_start,
                current_week_run_id=run_id,
                updated_at=stamp,
            )
        return len(rows)

    def mark_provisional(self, week_start: str) -> int:
        """Label an unconfirmed week's placements as a provisional plan.

        Placements that already carry a commitment keep it: a provisional run
        must not silently downgrade a week the student already confirmed.
        """
        parse_date(week_start)
        with self.tasks._write():
            cursor = self.conn.execute(
                """
                UPDATE task_placements
                SET commitment = 'provisional', version = version + 1, updated_at = ?
                WHERE week_start = ? AND commitment IS NULL
                """,
                (to_iso(self.clock.now()), week_start),
            )
        return cursor.rowcount

    def adopt_week(self, week_start: str, *, run_id: Optional[str] = None) -> None:
        """Attach this week to the run without changing any placement's state."""
        parse_date(week_start)
        with self.tasks._write():
            db.set_planning_state(
                self.conn,
                current_week_start=week_start,
                current_week_run_id=run_id,
                updated_at=to_iso(self.clock.now()),
            )

    def assert_within_week_commitment(
        self,
        task_id: str,
        new_date: str,
        *,
        confirmed: bool = False,
    ) -> None:
        """Refuse to push a confirmed commitment out of its week without consent."""
        row = self.conn.execute(
            """
            SELECT p.*, t.task_key FROM task_placements p
            JOIN tasks t ON t.id = p.task_id WHERE p.task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None or row["commitment"] != "confirmed":
            return
        week = parse_date(row["week_start"])
        target = parse_date(new_date)
        if not (week <= target <= week_end_of(week)) and not confirmed:
            raise ValidationError(
                "task %s is a confirmed commitment for the week of %s; moving it to %s "
                "needs explicit student confirmation"
                % (row["task_key"], row["week_start"], new_date)
            )

    # ------------------------------------------------------------- deferrals
    def record_miss(
        self,
        ref: str,
        *,
        confirmed: bool,
        reason: Optional[str] = None,
        operator: str = "agent",
    ) -> Placement:
        """Record that a task was explicitly reported unfinished.

        An unreported task keeps its state and is never counted as a failure.
        The first explicit miss may be rescheduled inside the week; the second
        raises the ``stuck`` risk and forbids another silent carry-forward.
        """
        with self.tasks._write():
            row = self.tasks._resolve_task_row(ref)
            placement_row = self.conn.execute(
                "SELECT * FROM task_placements WHERE task_id = ?", (row["id"],)
            ).fetchone()
            if placement_row is None:
                raise NotFoundError("task %s has no current destination" % row["task_key"])
            stamp = to_iso(self.clock.now())
            count = int(placement_row["deferral_count"]) + 1 if confirmed else int(placement_row["deferral_count"])
            self.conn.execute(
                """
                UPDATE task_placements
                SET deferral_count = ?, last_confirmed_miss_at = ?, version = version + 1,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (count, stamp if confirmed else placement_row["last_confirmed_miss_at"], stamp, row["id"]),
            )
            self.tasks._record_event(
                row["id"],
                "task_updated",
                {
                    "miss_confirmed": bool(confirmed),
                    "deferral_count": count,
                    "reason": reason,
                    "stuck": count >= MAX_DEFERRALS,
                },
                operator,
            )
            updated = self.conn.execute(
                "SELECT * FROM task_placements WHERE task_id = ?", (row["id"],)
            ).fetchone()
        return Placement.from_row(updated)

    def assert_deferral_allowed(self, task_id: str) -> None:
        row = self.conn.execute(
            """
            SELECT p.deferral_count, t.task_key FROM task_placements p
            JOIN tasks t ON t.id = p.task_id WHERE p.task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is not None and int(row["deferral_count"]) >= MAX_DEFERRALS:
            raise ValidationError(
                "%s has already been explicitly missed %d times; repeating the same "
                "carry-forward is not a real change. Split it, change the method or "
                "material, lower the scope, or let the student decide."
                % (row["task_key"], int(row["deferral_count"]))
            )

    # ---------------------------------------------------------------- runners
    def start_run(
        self,
        kind: str,
        *,
        run_key: Optional[str] = None,
        trigger: str = "manual",
        confirmation: str = "none",
        period_start: Optional[str] = None,
        period_end: Optional[str] = None,
        scheduled_for: Optional[str] = None,
        proposal: Optional[Dict[str, Any]] = None,
        base_revision: Optional[int] = None,
    ) -> str:
        if kind not in PLANNING_RUN_KINDS:
            raise ValidationError("invalid planning run kind: %r" % kind)
        if trigger not in PLANNING_TRIGGERS:
            raise ValidationError("invalid planning trigger: %r" % trigger)
        if confirmation not in PLANNING_CONFIRMATIONS:
            raise ValidationError("invalid confirmation state: %r" % confirmation)
        run_key = run_key or str(uuid7(self.clock.now_ms()))
        stamp = to_iso(self.clock.now())
        if base_revision is None:
            base_revision = db.planning_revision(self.conn)
        with self.tasks._write():
            existing = self.conn.execute(
                "SELECT id, status FROM planning_runs WHERE run_key = ?", (run_key,)
            ).fetchone()
            if existing is not None:
                return existing["id"]
            run_id = self._new_id()
            self.conn.execute(
                """
                INSERT INTO planning_runs (
                    id, run_key, kind, status, confirmation, period_start, period_end,
                    scheduled_for, started_at, completed_at, trigger, base_revision,
                    result_revision, input_digest, proposal_digest, proposal_json,
                    result_json, error_json
                ) VALUES (?, ?, ?, 'prepared', ?, ?, ?, ?, ?, NULL, ?, ?, NULL, NULL, NULL,
                          ?, NULL, NULL)
                """,
                (
                    run_id,
                    run_key,
                    kind,
                    confirmation,
                    period_start,
                    period_end,
                    scheduled_for,
                    stamp,
                    trigger,
                    base_revision,
                    compact_json(proposal) if proposal is not None else None,
                ),
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> None:
        if status not in PLANNING_RUN_STATUSES:
            raise ValidationError("invalid planning run status: %r" % status)
        stamp = to_iso(self.clock.now())
        revision = db.planning_revision(self.conn)
        with self.tasks._write():
            self.conn.execute(
                """
                UPDATE planning_runs
                SET status = ?, completed_at = ?, result_revision = ?, result_json = ?,
                    error_json = ?
                WHERE id = ?
                """,
                (
                    status,
                    stamp,
                    revision,
                    compact_json(result) if result is not None else None,
                    compact_json(error) if error is not None else None,
                    run_id,
                ),
            )

    def run_by_key(self, run_key: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM planning_runs WHERE run_key = ?", (run_key,)
        ).fetchone()
        return dict(row) if row else None

    def published_run(self, run_key: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM planning_runs WHERE run_key = ? AND status = 'published'",
            (run_key,),
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------ apply batch
    def apply_batch(self, operations: Sequence[Dict[str, Any]], *,
                    run_id: Optional[str] = None, operator: str = "agent") -> Dict[str, Any]:
        """Apply a whole validated proposal inside one outer transaction.

        Every operation runs through the Task Service on the same connection, so
        a failure anywhere rolls back tasks, criteria, evidence, placements,
        observations and the planning run together. The revision only advances
        when the outermost transaction commits.
        """
        summary: Dict[str, Any] = {"created": [], "updated": [], "placements": []}
        with self.tasks._write():
            for operation in operations:
                op = operation.get("op")
                if op == "create_task":
                    task = self._apply_create_task(operation, operator)
                    summary["created"].append(task.task_key)
                    if operation.get("placement") is not None:
                        self._apply_placement(task.task_key, operation["placement"], run_id, operator)
                        summary["placements"].append(task.task_key)
                elif op == "set_placement":
                    task = self.tasks._resolve_task_row(operation["task"])
                    self._apply_placement(task["task_key"], operation, run_id, operator)
                    summary["placements"].append(task["task_key"])
                elif op == "record_miss":
                    task = self.tasks._resolve_task_row(operation["task"])
                    self.record_miss(
                        task["task_key"],
                        confirmed=bool(operation.get("confirmed")),
                        reason=operation.get("reason"),
                        operator=operator,
                    )
                    summary["updated"].append(task["task_key"])
                elif op == "complete_task":
                    task = self.tasks._resolve_task_row(operation["task"])
                    for criterion in operation.get("satisfy_criteria", []):
                        self.tasks.satisfy_criterion(task["task_key"], criterion, operator=operator)
                    if operation.get("evidence") is not None:
                        evidence = operation["evidence"]
                        self.tasks.add_evidence(
                            task["task_key"],
                            evidence["kind"],
                            evidence["summary"],
                            reference=evidence.get("reference"),
                            operator=operator,
                        )
                    self.tasks.complete(task["task_key"], note=operation.get("note"), operator=operator)
                    summary["updated"].append(task["task_key"])
                else:
                    raise ValidationError("unsupported apply operation: %r" % (op,))
        return summary

    def _apply_create_task(self, operation: Dict[str, Any], operator: str) -> Task:
        status = operation.get("status", "proposed")
        if status not in ("proposed", "open"):
            raise ValidationError("a plan may only create proposed or open tasks")
        if status == "open" and operation.get("authorization_source") is None:
            raise ValidationError(
                "creating an open task %r needs an authorization source: an approved "
                "workflow step, a confirmed fact or an explicit student request"
                % operation.get("title")
            )
        task = self.tasks.create_task(
            operation["title"],
            course=operation.get("course"),
            actor=operation.get("actor", "student"),
            status=status,
            description=operation.get("description"),
            due_at=operation.get("due_at"),
            source=operation.get("source"),
            criteria=operation.get("criteria"),
            operator=operator,
        )
        for depends_on in operation.get("depends_on", []) or []:
            self.tasks.add_dependency(task.task_key, depends_on, operator=operator)
        return task

    def _apply_placement(
        self, task_ref: str, spec: Dict[str, Any], run_id: Optional[str], operator: str
    ) -> Placement:
        row = self.tasks._resolve_task_row(task_ref)
        if spec.get("kind") == "scheduled" and spec.get("scheduled_date"):
            self.assert_within_week_commitment(
                row["id"],
                spec["scheduled_date"],
                confirmed=bool(spec.get("student_confirmed")),
            )
            self.assert_deferral_allowed(row["id"])
        week_start = spec.get("week_start")
        if week_start is None and spec.get("kind") == "scheduled" and spec.get("scheduled_date"):
            week_start = week_start_of(parse_date(spec["scheduled_date"])).isoformat()
        return self.tasks.set_placement(
            task_ref,
            kind=spec["kind"],
            scheduled_date=spec.get("scheduled_date"),
            priority=spec.get("priority"),
            waiting_reason=spec.get("waiting_reason"),
            available_at=spec.get("available_at"),
            next_check_at=spec.get("next_check_at"),
            week_start=week_start,
            commitment=spec.get("commitment"),
            last_plan_run_id=run_id,
            change_reason=spec.get("change_reason"),
            operator=operator,
        )

    def _new_id(self) -> str:
        return str(uuid7(self.clock.now_ms()))
