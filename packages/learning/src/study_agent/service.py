"""Task Service: the only supported business write path for Task Memory v2.

Every mutation runs inside a short ``BEGIN IMMEDIATE`` transaction that updates
the current-state tables and appends a ``task_events`` row together. A failed
operation rolls back both, so no half-written state can survive.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import db
from .errors import ConflictError, NotFoundError, ValidationError
from .models import (
    ACTIVE_STATUSES,
    ACTORS,
    COMPLETION_EVIDENCE_KINDS,
    EVIDENCE_KINDS,
    OPEN_STATUSES,
    PLACEMENT_KINDS,
    PRIORITIES,
    TASK_STATUSES,
    Clock,
    Course,
    Criterion,
    Event,
    Evidence,
    Placement,
    Schedule,
    Task,
    compact_json,
    completion_evidence_kinds,
    dataclass_dict,
    dataclass_list,
    normalize_course_id,
    parse_date,
    parse_due_at,
    to_iso,
    week_start_of,
)
from .uuid7 import uuid7


class TaskService:
    """Validated, transactional operations over the task memory database."""

    def __init__(self, conn: sqlite3.Connection, clock: Optional[Clock] = None):
        self.conn = conn
        self.clock = clock or Clock()
        self.conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------------ core
    def _now(self) -> str:
        return to_iso(self.clock.now())

    def _new_id(self) -> str:
        return str(uuid7(self.clock.now_ms()))

    @contextmanager
    def _write(self):
        with db.atomic(self.conn):
            yield

    def _record_event(
        self,
        task_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        operator: str = "agent",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO task_events (id, task_id, event_type, actor, occurred_at, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (self._new_id(), task_id, event_type, operator, self._now(), compact_json(payload or {})),
        )

    @staticmethod
    def _validate_choice(value: str, allowed: Sequence[str], label: str) -> str:
        if value not in allowed:
            raise ValidationError("invalid %s %r; expected one of %s" % (label, value, ", ".join(allowed)))
        return value

    def _resolve_task_row(self, ref: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE task_key = ? OR id = ?", (ref, ref)
        ).fetchone()
        if row is None:
            raise NotFoundError("task not found: %s" % ref)
        return row

    def _resolve_course_row(self, ref: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM courses WHERE id = ? OR code = ?", (ref, ref)
        ).fetchone()
        if row is None:
            raise NotFoundError("course not found: %s" % ref)
        return row

    def _resolve_criterion_row(self, task_id: str, criterion_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM task_criteria WHERE task_id = ? AND (id = ? OR position = ?)",
            (task_id, criterion_id, _as_int(criterion_id)),
        ).fetchone()
        if row is None:
            raise NotFoundError("criterion not found on this task: %s" % criterion_id)
        return row

    # --------------------------------------------------------------- courses
    def add_course(self, code: str, name: str, course_id: Optional[str] = None) -> Course:
        if not code or not code.strip():
            raise ValidationError("course code must not be empty")
        if not name or not name.strip():
            raise ValidationError("course name must not be empty")
        resolved_id = (course_id or normalize_course_id(code)).strip()
        if not resolved_id:
            raise ValidationError("course id must not be empty")
        stamp = self._now()
        with self._write():
            existing = self.conn.execute(
                "SELECT * FROM courses WHERE id = ? OR code = ?", (resolved_id, code.strip())
            ).fetchone()
            if existing is not None:
                raise ConflictError("course already registered: %s" % code.strip())
            self.conn.execute(
                "INSERT INTO courses (id, code, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (resolved_id, code.strip(), name.strip(), stamp, stamp),
            )
            row = self.conn.execute("SELECT * FROM courses WHERE id = ?", (resolved_id,)).fetchone()
        return Course.from_row(row)

    def list_courses(self) -> List[Course]:
        rows = self.conn.execute("SELECT * FROM courses ORDER BY code").fetchall()
        return [Course.from_row(row) for row in rows]

    # ----------------------------------------------------------------- tasks
    def create_task(
        self,
        title: str,
        *,
        course: Optional[str] = None,
        actor: str = "student",
        status: str = "proposed",
        description: Optional[str] = None,
        due_at: Optional[str] = None,
        source: Optional[str] = None,
        criteria: Optional[Sequence[str]] = None,
        operator: str = "agent",
    ) -> Task:
        if not title or not title.strip():
            raise ValidationError("task title must not be empty")
        self._validate_choice(actor, ACTORS, "actor")
        self._validate_choice(status, OPEN_STATUSES, "initial status")
        normalized_due = parse_due_at(due_at)
        with self._write():
            course_id = None
            if course is not None:
                course_id = self._resolve_course_row(course)["id"]
            row = self.conn.execute(
                "SELECT COALESCE(MAX(CAST(substr(task_key, 3) AS INTEGER)), 0) AS n FROM tasks"
            ).fetchone()
            task_key = "T-%06d" % (int(row["n"]) + 1)
            task_id = self._new_id()
            stamp = self._now()
            self.conn.execute(
                """
                INSERT INTO tasks (
                    id, task_key, course_id, title, description, actor, status,
                    due_at, source, superseded_by, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL)
                """,
                (
                    task_id,
                    task_key,
                    course_id,
                    title.strip(),
                    description.strip() if description else None,
                    actor,
                    status,
                    normalized_due,
                    source.strip() if source else None,
                    stamp,
                    stamp,
                ),
            )
            criterion_texts = [text.strip() for text in (criteria or []) if text and text.strip()]
            for position, text in enumerate(criterion_texts, start=1):
                self.conn.execute(
                    """
                    INSERT INTO task_criteria
                        (id, task_id, position, text, required, satisfied, satisfied_at, created_at)
                    VALUES (?, ?, ?, ?, 1, 0, NULL, ?)
                    """,
                    (self._new_id(), task_id, position, text, stamp),
                )
            self._record_event(
                task_id,
                "task_created",
                {
                    "task_key": task_key,
                    "status": status,
                    "actor": actor,
                    "course": course,
                    "due_at": normalized_due,
                    "criteria": criterion_texts,
                },
                operator,
            )
            created = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return Task.from_row(created)

    def get_task(self, ref: str) -> Task:
        return Task.from_row(self._resolve_task_row(ref))

    def list_tasks(
        self,
        *,
        status: Optional[str] = None,
        course: Optional[str] = None,
        actor: Optional[str] = None,
        due_before: Optional[str] = None,
        scheduled_date: Optional[str] = None,
        open_only: bool = False,
    ) -> List[Task]:
        clauses: List[str] = []
        params: List[Any] = []
        if status is not None:
            self._validate_choice(status, TASK_STATUSES, "status")
            clauses.append("t.status = ?")
            params.append(status)
        if open_only:
            clauses.append("t.status IN ('proposed', 'open')")
        if course is not None:
            clauses.append("t.course_id = ?")
            params.append(self._resolve_course_row(course)["id"])
        if actor is not None:
            self._validate_choice(actor, ACTORS, "actor")
            clauses.append("t.actor = ?")
            params.append(actor)
        if due_before is not None:
            parse_date(due_before)
            clauses.append("t.due_at IS NOT NULL AND substr(t.due_at, 1, 10) <= ?")
            params.append(due_before)
        if scheduled_date is not None:
            parse_date(scheduled_date)
            clauses.append(
                "EXISTS (SELECT 1 FROM task_schedules s WHERE s.task_id = t.id "
                "AND s.active = 1 AND s.scheduled_date = ?)"
            )
            params.append(scheduled_date)
        sql = "SELECT t.* FROM tasks t"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY t.task_key"
        rows = self.conn.execute(sql, params).fetchall()
        return [Task.from_row(row) for row in rows]

    def update_task(
        self,
        ref: str,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        course: Optional[str] = None,
        actor: Optional[str] = None,
        due_at: Optional[str] = None,
        clear_due: bool = False,
        source: Optional[str] = None,
        operator: str = "agent",
    ) -> Task:
        with self._write():
            row = self._resolve_task_row(ref)
            if row["status"] not in ACTIVE_STATUSES:
                raise ConflictError(
                    "task %s is %s; only proposed/open tasks can be edited"
                    % (row["task_key"], row["status"])
                )
            changes: Dict[str, Any] = {}
            if title is not None:
                if not title.strip():
                    raise ValidationError("task title must not be empty")
                changes["title"] = title.strip()
            if description is not None:
                changes["description"] = description.strip() or None
            if course is not None:
                changes["course_id"] = self._resolve_course_row(course)["id"]
            if actor is not None:
                self._validate_choice(actor, ACTORS, "actor")
                changes["actor"] = actor
            if clear_due and due_at is not None:
                raise ValidationError("cannot pass both due_at and clear_due")
            if clear_due:
                changes["due_at"] = None
            elif due_at is not None:
                changes["due_at"] = parse_due_at(due_at)
            if source is not None:
                changes["source"] = source.strip() or None
            if not changes:
                raise ValidationError("no changes supplied")
            actual = {key: value for key, value in changes.items() if row[key] != value}
            if not actual:
                raise ConflictError("task %s already has the requested values" % row["task_key"])
            assignments = ", ".join("%s = ?" % key for key in actual)
            params = list(actual.values()) + [self._now(), row["id"]]
            self.conn.execute(
                "UPDATE tasks SET %s, updated_at = ? WHERE id = ?" % assignments, params
            )
            self._record_event(
                row["id"],
                "task_updated",
                {"fields": sorted(actual.keys()), "values": _jsonable(actual)},
                operator,
            )
            updated = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
        return Task.from_row(updated)

    def authorize(self, ref: str, *, operator: str = "agent") -> Task:
        with self._write():
            row = self._resolve_task_row(ref)
            if row["status"] != "proposed":
                raise ConflictError(
                    "task %s is %s; only proposed tasks can be authorized"
                    % (row["task_key"], row["status"])
                )
            self.conn.execute(
                "UPDATE tasks SET status = 'open', updated_at = ? WHERE id = ?",
                (self._now(), row["id"]),
            )
            self._record_event(row["id"], "proposal_authorized", {}, operator)
            updated = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
        return Task.from_row(updated)

    # -------------------------------------------------------------- criteria
    def add_criterion(
        self, ref: str, text: str, *, required: bool = True, operator: str = "agent"
    ) -> Criterion:
        if not text or not text.strip():
            raise ValidationError("criterion text must not be empty")
        with self._write():
            row = self._resolve_task_row(ref)
            if row["status"] not in ACTIVE_STATUSES:
                raise ConflictError(
                    "task %s is %s; criteria can only be added to proposed/open tasks"
                    % (row["task_key"], row["status"])
                )
            position_row = self.conn.execute(
                "SELECT COALESCE(MAX(position), 0) AS p FROM task_criteria WHERE task_id = ?",
                (row["id"],),
            ).fetchone()
            position = int(position_row["p"]) + 1
            criterion_id = self._new_id()
            stamp = self._now()
            self.conn.execute(
                """
                INSERT INTO task_criteria
                    (id, task_id, position, text, required, satisfied, satisfied_at, created_at)
                VALUES (?, ?, ?, ?, ?, 0, NULL, ?)
                """,
                (criterion_id, row["id"], position, text.strip(), 1 if required else 0, stamp),
            )
            self._record_event(
                row["id"],
                "criterion_added",
                {"criterion_id": criterion_id, "text": text.strip(), "required": bool(required)},
                operator,
            )
            created = self.conn.execute(
                "SELECT * FROM task_criteria WHERE id = ?", (criterion_id,)
            ).fetchone()
        return Criterion.from_row(created)

    def satisfy_criterion(self, ref: str, criterion: str, *, operator: str = "agent") -> Criterion:
        with self._write():
            row = self._resolve_task_row(ref)
            if row["status"] != "open":
                raise ConflictError(
                    "task %s is %s; criteria can only be satisfied on open tasks"
                    % (row["task_key"], row["status"])
                )
            criterion_row = self._resolve_criterion_row(row["id"], criterion)
            if criterion_row["satisfied"]:
                raise ConflictError("criterion is already satisfied: %s" % criterion_row["text"])
            stamp = self._now()
            self.conn.execute(
                "UPDATE task_criteria SET satisfied = 1, satisfied_at = ? WHERE id = ?",
                (stamp, criterion_row["id"]),
            )
            self.conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (stamp, row["id"]))
            self._record_event(
                row["id"],
                "criterion_satisfied",
                {"criterion_id": criterion_row["id"], "text": criterion_row["text"]},
                operator,
            )
            updated = self.conn.execute(
                "SELECT * FROM task_criteria WHERE id = ?", (criterion_row["id"],)
            ).fetchone()
        return Criterion.from_row(updated)

    def reopen_criterion(self, ref: str, criterion: str, *, operator: str = "agent") -> Criterion:
        with self._write():
            row = self._resolve_task_row(ref)
            if row["status"] != "open":
                raise ConflictError(
                    "task %s is %s; criteria can only be reopened on open tasks"
                    % (row["task_key"], row["status"])
                )
            criterion_row = self._resolve_criterion_row(row["id"], criterion)
            if not criterion_row["satisfied"]:
                raise ConflictError("criterion is not satisfied: %s" % criterion_row["text"])
            stamp = self._now()
            self.conn.execute(
                "UPDATE task_criteria SET satisfied = 0, satisfied_at = NULL WHERE id = ?",
                (criterion_row["id"],),
            )
            self.conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (stamp, row["id"]))
            self._record_event(
                row["id"],
                "criterion_reopened",
                {"criterion_id": criterion_row["id"], "text": criterion_row["text"]},
                operator,
            )
            updated = self.conn.execute(
                "SELECT * FROM task_criteria WHERE id = ?", (criterion_row["id"],)
            ).fetchone()
        return Criterion.from_row(updated)

    # ---------------------------------------------------------- dependencies
    def _assert_no_dependency_cycle(self, task_id: str, depends_on_id: str) -> None:
        stack = [depends_on_id]
        seen = set()
        while stack:
            current = stack.pop()
            if current == task_id:
                raise ConflictError("dependency would create a cycle")
            if current in seen:
                continue
            seen.add(current)
            rows = self.conn.execute(
                "SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ?", (current,)
            ).fetchall()
            stack.extend(row["depends_on_task_id"] for row in rows)

    def add_dependency(
        self, ref: str, depends_on: str, *, operator: str = "agent"
    ) -> Tuple[Task, Task]:
        with self._write():
            row = self._resolve_task_row(ref)
            if row["status"] not in ACTIVE_STATUSES:
                raise ConflictError(
                    "task %s is %s; dependencies can only be added to proposed/open tasks"
                    % (row["task_key"], row["status"])
                )
            target = self._resolve_task_row(depends_on)
            if target["id"] == row["id"]:
                raise ValidationError("a task cannot depend on itself")
            existing = self.conn.execute(
                "SELECT 1 FROM task_dependencies WHERE task_id = ? AND depends_on_task_id = ?",
                (row["id"], target["id"]),
            ).fetchone()
            if existing is not None:
                raise ConflictError(
                    "dependency already exists: %s -> %s" % (row["task_key"], target["task_key"])
                )
            self._assert_no_dependency_cycle(row["id"], target["id"])
            stamp = self._now()
            self.conn.execute(
                "INSERT INTO task_dependencies (task_id, depends_on_task_id, created_at) VALUES (?, ?, ?)",
                (row["id"], target["id"], stamp),
            )
            self.conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (stamp, row["id"]))
            self._record_event(
                row["id"],
                "dependency_added",
                {"depends_on": target["task_key"], "depends_on_id": target["id"]},
                operator,
            )
            updated = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
        return Task.from_row(updated), Task.from_row(target)

    def remove_dependency(self, ref: str, depends_on: str, *, operator: str = "agent") -> Task:
        with self._write():
            row = self._resolve_task_row(ref)
            if row["status"] not in ACTIVE_STATUSES:
                raise ConflictError(
                    "task %s is %s; dependencies can only be changed on proposed/open tasks"
                    % (row["task_key"], row["status"])
                )
            target = self._resolve_task_row(depends_on)
            existing = self.conn.execute(
                "SELECT 1 FROM task_dependencies WHERE task_id = ? AND depends_on_task_id = ?",
                (row["id"], target["id"]),
            ).fetchone()
            if existing is None:
                raise NotFoundError(
                    "dependency does not exist: %s -> %s" % (row["task_key"], target["task_key"])
                )
            stamp = self._now()
            self.conn.execute(
                "DELETE FROM task_dependencies WHERE task_id = ? AND depends_on_task_id = ?",
                (row["id"], target["id"]),
            )
            self.conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (stamp, row["id"]))
            self._record_event(
                row["id"],
                "dependency_removed",
                {"depends_on": target["task_key"], "depends_on_id": target["id"]},
                operator,
            )
            updated = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
        return Task.from_row(updated)

    # ------------------------------------------------------------- schedules
    def schedule(
        self,
        ref: str,
        scheduled_date: str,
        priority: str,
        *,
        note: Optional[str] = None,
        operator: str = "agent",
    ) -> Schedule:
        parse_date(scheduled_date)
        self._validate_choice(priority, PRIORITIES, "priority")
        with self._write():
            row = self._resolve_task_row(ref)
            if row["status"] != "open":
                raise ConflictError(
                    "task %s is %s; only open tasks can be scheduled"
                    % (row["task_key"], row["status"])
                )
            active_rows = self.conn.execute(
                "SELECT * FROM task_schedules WHERE task_id = ? AND active = 1 "
                "ORDER BY scheduled_date",
                (row["id"],),
            ).fetchall()
            same_date = [item for item in active_rows if item["scheduled_date"] == scheduled_date]
            stamp = self._now()
            if same_date:
                current = same_date[0]
                if current["priority"] == priority and (current["note"] or None) == (note or None):
                    raise ConflictError(
                        "task %s is already scheduled on %s with priority %s"
                        % (row["task_key"], scheduled_date, priority)
                    )
                self.conn.execute(
                    "UPDATE task_schedules SET active = 0, deactivated_at = ? WHERE id = ?",
                    (stamp, current["id"]),
                )
                schedule_id = self._new_id()
                self.conn.execute(
                    """
                    INSERT INTO task_schedules
                        (id, task_id, scheduled_date, priority, note, active, created_at, deactivated_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?, NULL)
                    """,
                    (schedule_id, row["id"], scheduled_date, priority, note or None, stamp),
                )
                self._record_event(
                    row["id"],
                    "priority_changed",
                    {
                        "date": scheduled_date,
                        "from": current["priority"],
                        "to": priority,
                        "note": note or None,
                    },
                    operator,
                )
            elif active_rows:
                previous_dates = [item["scheduled_date"] for item in active_rows]
                for item in active_rows:
                    self.conn.execute(
                        "UPDATE task_schedules SET active = 0, deactivated_at = ? WHERE id = ?",
                        (stamp, item["id"]),
                    )
                schedule_id = self._new_id()
                self.conn.execute(
                    """
                    INSERT INTO task_schedules
                        (id, task_id, scheduled_date, priority, note, active, created_at, deactivated_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?, NULL)
                    """,
                    (schedule_id, row["id"], scheduled_date, priority, note or None, stamp),
                )
                self._record_event(
                    row["id"],
                    "rescheduled",
                    {
                        "from": previous_dates,
                        "to": scheduled_date,
                        "priority": priority,
                        "note": note or None,
                    },
                    operator,
                )
            else:
                schedule_id = self._new_id()
                self.conn.execute(
                    """
                    INSERT INTO task_schedules
                        (id, task_id, scheduled_date, priority, note, active, created_at, deactivated_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?, NULL)
                    """,
                    (schedule_id, row["id"], scheduled_date, priority, note or None, stamp),
                )
                self._record_event(
                    row["id"],
                    "scheduled",
                    {"date": scheduled_date, "priority": priority, "note": note or None},
                    operator,
                )
            self.conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (stamp, row["id"]))
            created = self.conn.execute(
                "SELECT * FROM task_schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
        return Schedule.from_row(created)

    # ------------------------------------------------------------- placements
    def placement_for(self, ref: str) -> Optional[Placement]:
        row = self._resolve_task_row(ref)
        placed = self.conn.execute(
            "SELECT * FROM task_placements WHERE task_id = ?", (row["id"],)
        ).fetchone()
        return Placement.from_row(placed) if placed else None

    def list_placements(self) -> List[Placement]:
        rows = self.conn.execute("SELECT * FROM task_placements").fetchall()
        return [Placement.from_row(row) for row in rows]

    def set_placement(
        self,
        ref: str,
        *,
        kind: str,
        scheduled_date: Optional[str] = None,
        priority: Optional[str] = None,
        waiting_reason: Optional[str] = None,
        available_at: Optional[str] = None,
        next_check_at: Optional[str] = None,
        week_start: Optional[str] = None,
        commitment: Optional[str] = None,
        last_plan_run_id: Optional[str] = None,
        change_reason: Optional[str] = None,
        operator: str = "agent",
        now: Optional[str] = None,
    ) -> Placement:
        """Create or replace the single current destination of an open task.

        Every change also appends the matching task event (``scheduled``,
        ``rescheduled``, ``priority_changed`` or a new ``task_updated`` carrying
        the waiting state) so the event history stays the audit trail.
        """
        self._validate_choice(kind, PLACEMENT_KINDS, "placement kind")
        if kind == "scheduled":
            if scheduled_date is None or priority is None:
                raise ValidationError("scheduled placement needs both a date and a priority")
            parse_date(scheduled_date)
            self._validate_choice(priority, PRIORITIES, "priority")
            if waiting_reason is not None or next_check_at is not None:
                raise ValidationError("scheduled placement must not carry waiting fields")
        else:
            if not waiting_reason or not waiting_reason.strip():
                raise ValidationError("waiting placement needs a non-empty reason")
            if not next_check_at:
                raise ValidationError("waiting placement needs a next_check_at")
            if scheduled_date is not None or priority is not None:
                raise ValidationError("waiting placement must not carry a date or priority")
        if commitment is not None and week_start is None:
            raise ValidationError("commitment requires a week_start")
        if week_start is not None:
            parsed = parse_date(week_start)
            if parsed.weekday() != 0:
                raise ValidationError("week_start must be a Monday, got %s" % week_start)
        if available_at is not None:
            parse_date(available_at)

        with self._write():
            row = self._resolve_task_row(ref)
            if row["status"] != "open":
                raise ConflictError(
                    "task %s is %s; only open tasks can have a destination"
                    % (row["task_key"], row["status"])
                )
            previous_row = self.conn.execute(
                "SELECT * FROM task_placements WHERE task_id = ?", (row["id"],)
            ).fetchone()
            stamp = now or self._now()
            payload = {
                "date": scheduled_date,
                "priority": priority,
                "waiting_reason": waiting_reason.strip() if waiting_reason else None,
                "next_check_at": next_check_at,
                "available_at": available_at,
                "week_start": week_start,
                "commitment": commitment,
                "change_reason": change_reason.strip() if change_reason else None,
            }
            if previous_row is None:
                self.conn.execute(
                    """
                    INSERT INTO task_placements (
                        task_id, kind, scheduled_date, priority, waiting_reason,
                        available_at, next_check_at, week_start, commitment,
                        deferral_count, last_confirmed_miss_at, last_plan_run_id,
                        version, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, 1, ?)
                    """,
                    (
                        row["id"],
                        kind,
                        scheduled_date,
                        priority,
                        waiting_reason.strip() if waiting_reason else None,
                        available_at,
                        next_check_at,
                        week_start,
                        commitment,
                        last_plan_run_id,
                        stamp,
                    ),
                )
                event_type = "scheduled" if kind == "scheduled" else "task_updated"
                self._record_event(row["id"], event_type, payload, operator)
            else:
                self.conn.execute(
                    """
                    UPDATE task_placements
                    SET kind = ?, scheduled_date = ?, priority = ?, waiting_reason = ?,
                        available_at = ?, next_check_at = ?, week_start = ?,
                        commitment = ?, last_plan_run_id = ?, version = version + 1,
                        updated_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        kind,
                        scheduled_date,
                        priority,
                        waiting_reason.strip() if waiting_reason else None,
                        available_at,
                        next_check_at,
                        week_start,
                        commitment,
                        last_plan_run_id,
                        stamp,
                        row["id"],
                    ),
                )
                if kind == "waiting" or previous_row["kind"] == "waiting":
                    event_type = "task_updated" if kind == "waiting" else "rescheduled"
                elif previous_row["priority"] != priority and previous_row["scheduled_date"] == scheduled_date:
                    event_type = "priority_changed"
                    payload["from"] = previous_row["priority"]
                    payload["to"] = priority
                elif previous_row["scheduled_date"] != scheduled_date:
                    event_type = "rescheduled"
                    payload["from"] = (
                        [previous_row["scheduled_date"]] if previous_row["scheduled_date"] else []
                    )
                    payload["to"] = scheduled_date
                else:
                    event_type = "scheduled"
                self._record_event(row["id"], event_type, payload, operator)
            self.conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (stamp, row["id"]))
            placed = self.conn.execute(
                "SELECT * FROM task_placements WHERE task_id = ?", (row["id"],)
            ).fetchone()
        return Placement.from_row(placed)

    def clear_placement(self, task_id: str) -> None:
        """Drop the destination of a task that is leaving the open pool."""
        self.conn.execute("DELETE FROM task_placements WHERE task_id = ?", (task_id,))

    def _assert_placement_ok_for_open(self, task_id: str, task_key: str) -> None:
        placed = self.conn.execute(
            "SELECT 1 FROM task_placements WHERE task_id = ?", (task_id,)
        ).fetchone()
        if placed is None:
            raise ConflictError(
                "task %s would return to open without a destination; set a placement in the "
                "same proposal" % task_key
            )

    # -------------------------------------------------------------- evidence
    def add_evidence(
        self,
        ref: str,
        kind: str,
        summary: str,
        *,
        reference: Optional[str] = None,
        operator: str = "agent",
    ) -> Evidence:
        self._validate_choice(kind, EVIDENCE_KINDS, "evidence kind")
        if not summary or not summary.strip():
            raise ValidationError("evidence summary must not be empty")
        with self._write():
            row = self._resolve_task_row(ref)
            if row["status"] != "open":
                raise ConflictError(
                    "task %s is %s; evidence can only be added to open tasks"
                    % (row["task_key"], row["status"])
                )
            evidence_id = self._new_id()
            stamp = self._now()
            self.conn.execute(
                """
                INSERT INTO task_evidence
                    (id, task_id, kind, summary, reference, recorded_by, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (evidence_id, row["id"], kind, summary.strip(), reference, operator, stamp),
            )
            self.conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (stamp, row["id"]))
            self._record_event(
                row["id"],
                "evidence_added",
                {"kind": kind, "summary": summary.strip(), "reference": reference},
                operator,
            )
            created = self.conn.execute(
                "SELECT * FROM task_evidence WHERE id = ?", (evidence_id,)
            ).fetchone()
        return Evidence.from_row(created)

    # ------------------------------------------------------- lifecycle close
    def complete(self, ref: str, *, note: Optional[str] = None, operator: str = "agent") -> Task:
        with self._write():
            row = self._resolve_task_row(ref)
            if row["status"] != "open":
                raise ConflictError(
                    "task %s is %s; only open tasks can be completed"
                    % (row["task_key"], row["status"])
                )
            required = self.conn.execute(
                "SELECT COUNT(*) AS n FROM task_criteria WHERE task_id = ? AND required = 1",
                (row["id"],),
            ).fetchone()["n"]
            unsatisfied = self.conn.execute(
                """
                SELECT text FROM task_criteria
                WHERE task_id = ? AND required = 1 AND satisfied = 0
                ORDER BY position
                """,
                (row["id"],),
            ).fetchall()
            if unsatisfied:
                raise ValidationError(
                    "cannot complete %s; unsatisfied required criteria: %s"
                    % (row["task_key"], "; ".join(item["text"] for item in unsatisfied))
                )
            evidence_count = self.conn.execute(
                "SELECT COUNT(*) AS n FROM task_evidence WHERE task_id = ? AND kind IN (%s)"
                % ", ".join("?" for _ in completion_evidence_kinds(row["actor"])),
                (row["id"],) + completion_evidence_kinds(row["actor"]),
            ).fetchone()["n"]
            if evidence_count == 0:
                raise ValidationError(
                    "cannot complete %s; a %s evidence entry is required for a %s task"
                    % (
                        row["task_key"],
                        " or ".join(completion_evidence_kinds(row["actor"])),
                        row["actor"],
                    )
                )
            stamp = self._now()
            self.conn.execute(
                """
                UPDATE tasks
                SET status = 'completed', completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (stamp, stamp, row["id"]),
            )
            self._record_event(
                row["id"],
                "completed",
                {
                    "required_criteria": int(required),
                    "evidence": int(evidence_count),
                    "note": note or None,
                },
                operator,
            )
            self.clear_placement(row["id"])
            updated = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
        return Task.from_row(updated)

    def cancel(
        self, ref: str, *, reason: Optional[str] = None, operator: str = "agent"
    ) -> Task:
        with self._write():
            row = self._resolve_task_row(ref)
            if row["status"] not in OPEN_STATUSES:
                raise ConflictError(
                    "task %s is %s; only proposed/open tasks can be cancelled"
                    % (row["task_key"], row["status"])
                )
            stamp = self._now()
            self.conn.execute(
                "UPDATE tasks SET status = 'cancelled', updated_at = ? WHERE id = ?",
                (stamp, row["id"]),
            )
            self._record_event(row["id"], "cancelled", {"reason": reason or None}, operator)
            self.clear_placement(row["id"])
            updated = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
        return Task.from_row(updated)

    def supersede(
        self,
        ref: str,
        by: str,
        *,
        reason: Optional[str] = None,
        operator: str = "agent",
    ) -> Task:
        with self._write():
            row = self._resolve_task_row(ref)
            if row["status"] not in OPEN_STATUSES:
                raise ConflictError(
                    "task %s is %s; only proposed/open tasks can be superseded"
                    % (row["task_key"], row["status"])
                )
            target = self._resolve_task_row(by)
            if target["id"] == row["id"]:
                raise ValidationError("a task cannot supersede itself")
            if target["status"] not in OPEN_STATUSES:
                raise ValidationError(
                    "replacement task %s must be proposed or open (it is %s)"
                    % (target["task_key"], target["status"])
                )
            stamp = self._now()
            self.conn.execute(
                """
                UPDATE tasks
                SET status = 'superseded', superseded_by = ?, updated_at = ?
                WHERE id = ?
                """,
                (target["id"], stamp, row["id"]),
            )
            self._record_event(
                row["id"],
                "superseded",
                {"superseded_by": target["task_key"], "reason": reason or None},
                operator,
            )
            self.clear_placement(row["id"])
            updated = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
        return Task.from_row(updated)

    def reopen(
        self,
        ref: str,
        reason: str,
        *,
        placement: Optional[Dict[str, Any]] = None,
        operator: str = "agent",
    ) -> Task:
        """Return a completed/cancelled task to open with a fresh destination.

        A reopened task re-enters the open pool, so it needs exactly one current
        placement. Pass it in the same call so the transition and its destination
        commit together.
        """
        if not reason or not reason.strip():
            raise ValidationError("reopening requires a non-empty reason")
        with self._write():
            row = self._resolve_task_row(ref)
            if row["status"] not in ("completed", "cancelled"):
                raise ConflictError(
                    "task %s is %s; only completed/cancelled tasks can be reopened "
                    "(superseded tasks are never reopened directly)" % (row["task_key"], row["status"])
                )
            previous = row["status"]
            stamp = self._now()
            self.conn.execute(
                """
                UPDATE tasks
                SET status = 'open', completed_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (stamp, row["id"]),
            )
            self._record_event(
                row["id"],
                "reopened",
                {"from_status": previous, "reason": reason.strip()},
                operator,
            )
            if placement is not None:
                self.set_placement(ref, now=stamp, operator=operator, **placement)
            self._assert_placement_ok_for_open(row["id"], row["task_key"])
            updated = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
        return Task.from_row(updated)

    # ---------------------------------------------------------------- reading
    def readiness(self, ref: str) -> Dict[str, Any]:
        row = self._resolve_task_row(ref)
        blockers = self.conn.execute(
            """
            SELECT t.task_key, t.title, t.status
            FROM task_dependencies d
            JOIN tasks t ON t.id = d.depends_on_task_id
            WHERE d.task_id = ? AND t.status <> 'completed'
            ORDER BY t.task_key
            """,
            (row["id"],),
        ).fetchall()
        blocker_list = [dict(item) for item in blockers]
        applicable = row["status"] in ("proposed", "open")
        return {
            "task_key": row["task_key"],
            "status": row["status"],
            "applicable": applicable,
            "ready": applicable and not blocker_list,
            "blocked_by": blocker_list,
        }

    def criteria_for(self, task_id: str) -> List[Criterion]:
        rows = self.conn.execute(
            "SELECT * FROM task_criteria WHERE task_id = ? ORDER BY position", (task_id,)
        ).fetchall()
        return [Criterion.from_row(row) for row in rows]

    def dependencies_for(self, task_id: str) -> List[Task]:
        rows = self.conn.execute(
            """
            SELECT t.* FROM task_dependencies d
            JOIN tasks t ON t.id = d.depends_on_task_id
            WHERE d.task_id = ? ORDER BY t.task_key
            """,
            (task_id,),
        ).fetchall()
        return [Task.from_row(row) for row in rows]

    def dependents_for(self, task_id: str) -> List[Task]:
        rows = self.conn.execute(
            """
            SELECT t.* FROM task_dependencies d
            JOIN tasks t ON t.id = d.task_id
            WHERE d.depends_on_task_id = ? ORDER BY t.task_key
            """,
            (task_id,),
        ).fetchall()
        return [Task.from_row(row) for row in rows]

    def schedules_for(self, task_id: str) -> List[Schedule]:
        rows = self.conn.execute(
            "SELECT * FROM task_schedules WHERE task_id = ? ORDER BY created_at, id", (task_id,)
        ).fetchall()
        return [Schedule.from_row(row) for row in rows]

    def evidence_for(self, task_id: str) -> List[Evidence]:
        rows = self.conn.execute(
            "SELECT * FROM task_evidence WHERE task_id = ? ORDER BY recorded_at, id", (task_id,)
        ).fetchall()
        return [Evidence.from_row(row) for row in rows]

    def events_for(self, ref: str) -> List[Event]:
        row = self._resolve_task_row(ref)
        rows = self.conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY occurred_at, id", (row["id"],)
        ).fetchall()
        return [Event.from_row(row) for row in rows]

    def task_detail(self, ref: str) -> Dict[str, Any]:
        row = self._resolve_task_row(ref)
        course = None
        if row["course_id"]:
            course_row = self.conn.execute(
                "SELECT * FROM courses WHERE id = ?", (row["course_id"],)
            ).fetchone()
            course = dataclass_dict(Course.from_row(course_row)) if course_row else None
        return {
            "task": dataclass_dict(Task.from_row(row)),
            "course": course,
            "readiness": self.readiness(row["task_key"]),
            "criteria": dataclass_list(self.criteria_for(row["id"])),
            "dependencies": dataclass_list(self.dependencies_for(row["id"])),
            "dependents": dataclass_list(self.dependents_for(row["id"])),
            "schedules": dataclass_list(self.schedules_for(row["id"])),
            "evidence": dataclass_list(self.evidence_for(row["id"])),
            "events": [dataclass_dict(event) for event in self.events_for(row["task_key"])],
        }


def _as_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _jsonable(changes: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in changes.items()}
