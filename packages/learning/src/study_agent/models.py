"""Domain constants, value parsing helpers, clocks and row-backed models."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

try:  # Python 3.9 standard library; falls back to a fixed offset without tzdata.
    from zoneinfo import ZoneInfo

    HKT = ZoneInfo("Asia/Hong_Kong")
except Exception:  # pragma: no cover - only on systems without tzdata
    HKT = timezone(timedelta(hours=8), name="HKT")

UTC = timezone.utc

TASK_STATUSES = ("proposed", "open", "completed", "cancelled", "superseded")
OPEN_STATUSES = ("proposed", "open")
ACTIVE_STATUSES = ("proposed", "open")
ACTORS = ("student", "agent", "external")
PRIORITIES = ("must", "should", "if_time")
PRIORITY_LABELS = {"must": "Must Do", "should": "Should Do", "if_time": "If Time Remains"}
PRIORITY_ORDER = {"must": 0, "should": 1, "if_time": 2}
EVIDENCE_KINDS = ("student_confirmation", "objective_record", "artifact", "note")
# Only these two kinds can justify closing a task; artifacts prove a file exists,
# not that the student read, practised, submitted or mastered anything.
COMPLETION_EVIDENCE_KINDS = ("student_confirmation", "objective_record")
# What may close a task depends on who has to do the work, not on one global list.
# A student's task cannot be closed by a file that merely exists, and an agent's
# task cannot be closed by a checklist tick the student made in a Markdown view.
COMPLETION_EVIDENCE_BY_ACTOR = {
    "student": ("student_confirmation", "objective_record"),
    "agent": ("objective_record", "artifact"),
    "external": ("objective_record",),
}

PLACEMENT_KINDS = ("scheduled", "waiting")
DESTINATIONS = ("today", "this_week", "future", "waiting", "overdue", "backlog")
DESTINATION_LABELS = {
    "today": "今天",
    "this_week": "本周稍后",
    "future": "未来",
    "waiting": "等待",
    "overdue": "已过期",
    "backlog": "无去向",
}
PLANNING_RUN_KINDS = ("weekly", "daily", "catchup", "manual", "view_sync", "cutover")
PLANNING_RUN_STATUSES = ("prepared", "validated", "published", "failed")
PLANNING_CONFIRMATIONS = ("none", "provisional", "confirmed")
PLANNING_TRIGGERS = ("launchd", "startup", "manual", "view_sync")
OBSERVATION_KINDS = ("file", "calendar")
BINDING_STATUSES = ("bound", "ignored", "needs_review")
ARTIFACT_KINDS = (
    "slides",
    "recording",
    "transcript",
    "note",
    "assignment",
    "result",
    "other",
)
ASSESSMENT_SOURCE_KINDS = (
    "textbook_quiz",
    "lecture_quiz",
    "official_practice",
    "official_quiz",
    "tutorial",
    "exam",
    "other",
)
ATTEMPT_TYPES = ("initial", "retest")
ISSUE_TYPES = ("wrong", "guessed", "cannot_explain", "cannot_start", "other")
ISSUE_STATUSES = ("pending", "passed")
ISSUE_RESULTS = ("failed", "passed", "uncertain")


def completion_evidence_kinds(actor: str) -> tuple:
    """Return the evidence kinds that may close a task owned by ``actor``."""
    return COMPLETION_EVIDENCE_BY_ACTOR.get(actor, COMPLETION_EVIDENCE_KINDS)


EVENT_TYPES = (
    "task_created",
    "proposal_authorized",
    "task_updated",
    "criterion_added",
    "criterion_satisfied",
    "criterion_reopened",
    "dependency_added",
    "dependency_removed",
    "scheduled",
    "rescheduled",
    "priority_changed",
    "evidence_added",
    "completed",
    "cancelled",
    "superseded",
    "reopened",
)


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_iso(moment: datetime) -> str:
    """Format an aware datetime as ISO 8601 in UTC with an explicit offset."""
    if moment.tzinfo is None:
        raise ValueError("to_iso requires a timezone-aware datetime")
    return moment.astimezone(UTC).isoformat()


def from_iso(text: str) -> datetime:
    """Parse an ISO 8601 timestamp produced by :func:`to_iso`."""
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError("timestamp lacks a timezone offset: %r" % (text,))
    return moment


def format_local(timestamp: str, fmt: str = "%Y-%m-%d %H:%M %Z") -> str:
    return from_iso(timestamp).astimezone(HKT).strftime(fmt)


def local_date_of(timestamp: str) -> date:
    return from_iso(timestamp).astimezone(HKT).date()


def parse_date(text: str) -> date:
    """Parse ``YYYY-MM-DD`` strictly (no other ISO date forms)."""
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        raise ValueError("expected YYYY-MM-DD, got %r" % (text,))
    return date(int(text[0:4]), int(text[5:7]), int(text[8:10]))


def parse_due_at(text: Optional[str]) -> Optional[str]:
    """Normalise a due value to ``YYYY-MM-DD`` or an ISO datetime with offset.

    A bare date is kept as a date because inventing a clock time would be a
    guess. A datetime must carry an explicit timezone offset.
    """
    if text is None:
        return None
    text = text.strip()
    if not text:
        raise ValueError("due value is empty")
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        parse_date(text)
        return text
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError("due datetime needs an explicit timezone offset: %r" % (text,))
    return to_iso(moment)


def week_start_of(day: date) -> date:
    """Return the Monday of the week containing ``day``."""
    return day - timedelta(days=day.weekday())


def week_end_of(day: date) -> date:
    """Return the Sunday of the week containing ``day``."""
    return week_start_of(day) + timedelta(days=6)


def add_days(day: date, count: int) -> date:
    return day + timedelta(days=count)


def format_due(due_at: Optional[str]) -> str:
    if not due_at:
        return ""
    if len(due_at) == 10:
        return due_at + "（时间待确认）"
    return format_local(due_at, "%Y-%m-%d %H:%M %Z")


class Clock:
    """Injectable time source; tests replace this with a fixed clock."""

    def now(self) -> datetime:
        return now_utc()

    def now_ms(self) -> int:
        return int(self.now().timestamp() * 1000)

    def today(self) -> date:
        from zoneinfo import ZoneInfo
        import os
        return self.now().astimezone(ZoneInfo(os.environ.get('STUDY_TIMEZONE','Asia/Hong_Kong'))).date()


class FixedClock(Clock):
    """Deterministic clock for tests."""

    def __init__(self, moment: datetime):
        if moment.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._moment = moment.astimezone(UTC)

    def now(self) -> datetime:
        return self._moment

    def advance(self, seconds: float = 0.0, milliseconds: float = 0.0) -> None:
        self._moment = self._moment + timedelta(seconds=seconds, milliseconds=milliseconds)


@dataclass
class Course:
    id: str
    code: str
    name: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row) -> "Course":
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})


@dataclass
class Task:
    id: str
    task_key: str
    course_id: Optional[str]
    title: str
    description: Optional[str]
    actor: str
    status: str
    due_at: Optional[str]
    source: Optional[str]
    superseded_by: Optional[str]
    created_at: str
    updated_at: str
    completed_at: Optional[str]

    @classmethod
    def from_row(cls, row) -> "Task":
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})


@dataclass
class Criterion:
    id: str
    task_id: str
    position: int
    text: str
    required: int
    satisfied: int
    satisfied_at: Optional[str]
    created_at: str

    @classmethod
    def from_row(cls, row) -> "Criterion":
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})


@dataclass
class Schedule:
    id: str
    task_id: str
    scheduled_date: str
    priority: str
    note: Optional[str]
    active: int
    created_at: str
    deactivated_at: Optional[str]

    @classmethod
    def from_row(cls, row) -> "Schedule":
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})


@dataclass
class Evidence:
    id: str
    task_id: str
    kind: str
    summary: str
    reference: Optional[str]
    recorded_by: str
    recorded_at: str

    @classmethod
    def from_row(cls, row) -> "Evidence":
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})


@dataclass
class Event:
    id: str
    task_id: str
    event_type: str
    actor: str
    occurred_at: str
    payload: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row) -> "Event":
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            payload = {"raw": row["payload"]}
        return cls(
            id=row["id"],
            task_id=row["task_id"],
            event_type=row["event_type"],
            actor=row["actor"],
            occurred_at=row["occurred_at"],
            payload=payload,
        )


@dataclass
class Placement:
    task_id: str
    kind: str
    scheduled_date: Optional[str]
    priority: Optional[str]
    waiting_reason: Optional[str]
    available_at: Optional[str]
    next_check_at: Optional[str]
    week_start: Optional[str]
    commitment: Optional[str]
    deferral_count: int
    last_confirmed_miss_at: Optional[str]
    last_plan_run_id: Optional[str]
    version: int
    updated_at: str

    @classmethod
    def from_row(cls, row) -> "Placement":
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})


@dataclass
class Observation:
    id: str
    kind: str
    source_key: str
    version_key: str
    course_id: Optional[str]
    binding_status: str
    session_key: Optional[str]
    artifact_kind: Optional[str]
    occurred_at: Optional[str]
    first_seen_at: str
    last_seen_at: str
    payload_json: str

    @classmethod
    def from_row(cls, row) -> "Observation":
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})


@dataclass
class WorkflowInstance:
    id: str
    course_id: str
    workflow_id: str
    occurrence_key: str
    status: str
    created_by_run_id: Optional[str]
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row) -> "WorkflowInstance":
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})


@dataclass
class PlanningRun:
    id: str
    run_key: str
    kind: str
    status: str
    confirmation: str
    period_start: Optional[str]
    period_end: Optional[str]
    scheduled_for: Optional[str]
    started_at: str
    completed_at: Optional[str]
    trigger: str
    base_revision: int
    result_revision: Optional[int]
    input_digest: Optional[str]
    proposal_digest: Optional[str]
    proposal_json: Optional[str]
    result_json: Optional[str]
    error_json: Optional[str]

    @classmethod
    def from_row(cls, row) -> "PlanningRun":
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})


@dataclass
class AssessmentAttempt:
    id: str
    course_id: str
    source_kind: str
    session_key: Optional[str]
    attempt_type: str
    occurred_at: str
    score_text: Optional[str]
    summary: str
    evidence_ref: Optional[str]
    created_at: str

    @classmethod
    def from_row(cls, row) -> "AssessmentAttempt":
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})


@dataclass
class LearningIssue:
    id: str
    course_id: str
    concept: str
    issue_type: str
    status: str
    source_attempt_id: Optional[str]
    next_retest_on: Optional[str]
    criterion_id: Optional[str]
    resolved_at: Optional[str]
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row) -> "LearningIssue":
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_course_id(code: str) -> str:
    """Derive a stable course id such as ``COMP 1023`` -> ``comp-1023``."""
    cleaned = []
    previous_dash = False
    for char in code.strip().lower():
        if char.isalnum():
            cleaned.append(char)
            previous_dash = False
        elif not previous_dash:
            cleaned.append("-")
            previous_dash = True
    normalized = "".join(cleaned).strip("-")
    if not normalized:
        raise ValueError("course code must contain at least one letter or digit")
    return normalized


def dataclass_dict(obj: Any) -> Dict[str, Any]:
    return {key: getattr(obj, key) for key in obj.__dataclass_fields__}


def dataclass_list(items: Iterable[Any]) -> List[Dict[str, Any]]:
    return [dataclass_dict(item) for item in items]
