"""Course workflow configuration: read, validate strictly, instantiate idempotently.

Each course has one JSON file under ``.study-system/config/course-workflows/``.
The config states which workflows are already authorized by course rules and by
decisions the student has confirmed; it never stores a task instance, current
progress, live dates or completion state.

Unknown fields are hard errors: a silent typo in ``depends_on`` would otherwise
disable a real dependency.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .errors import ConflictError, NotFoundError, ValidationError
from .models import ACTORS, Clock, compact_json, to_iso
from .uuid7 import uuid7

SCHEMA_VERSION = 1
CONFIG_KEYS = {"schema_version", "course", "root", "calendar_aliases", "ignore_globs",
               "workflows", "retest_gap_days"}
WORKFLOW_KEYS = {"id", "authorized", "description", "steps"}
STEP_KEYS = {"id", "actor", "action", "requires", "depends_on"}
_REQUIREMENT_KINDS = {"slides", "recording", "transcript", "note", "assignment", "result"}

_SLUG = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass
class WorkflowStep:
    id: str
    actor: str
    action: str
    requires: List[str] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)


@dataclass
class Workflow:
    id: str
    authorized: bool
    description: str
    steps: List[WorkflowStep]

    def step(self, step_id: str) -> WorkflowStep:
        for item in self.steps:
            if item.id == step_id:
                return item
        raise NotFoundError("workflow %s has no step %s" % (self.id, step_id))


@dataclass
class CourseWorkflowConfig:
    schema_version: int
    course: str
    root: str
    calendar_aliases: List[str]
    ignore_globs: List[str]
    workflows: List[Workflow]
    retest_gap_days: Optional[int]
    path: Optional[Path] = None

    def workflow(self, workflow_id: str) -> Workflow:
        for item in self.workflows:
            if item.id == workflow_id:
                return item
        raise NotFoundError("course %s has no workflow %s" % (self.course, workflow_id))

    def summary(self) -> Dict[str, Any]:
        return {
            "course": self.course,
            "root": self.root,
            "calendar_aliases": list(self.calendar_aliases),
            "workflows": [
                {
                    "id": item.id,
                    "authorized": item.authorized,
                    "steps": [
                        {
                            "id": step.id,
                            "actor": step.actor,
                            "requires": list(step.requires),
                            "depends_on": list(step.depends_on),
                        }
                        for step in item.steps
                    ],
                }
                for item in self.workflows
            ],
        }


def _require_keys(payload: Dict[str, Any], allowed: set, where: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValidationError("unknown field(s) in %s: %s" % (where, ", ".join(unknown)))


def _require_string(payload: Dict[str, Any], key: str, where: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s.%s must be a non-empty string" % (where, key))
    return value.strip()


def parse_config(payload: Any, *, path: Optional[Path] = None) -> CourseWorkflowConfig:
    """Validate one course config strictly and return the parsed object."""
    where = str(path) if path else "course workflow config"
    if not isinstance(payload, dict):
        raise ValidationError("%s must contain a JSON object" % where)
    _require_keys(payload, CONFIG_KEYS, where)

    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValidationError(
            "%s has schema_version %r; expected %d" % (where, version, SCHEMA_VERSION)
        )
    course = _require_string(payload, "course", where)
    root = _require_string(payload, "root", where)

    aliases = payload.get("calendar_aliases")
    if not isinstance(aliases, list) or not aliases:
        raise ValidationError("%s.calendar_aliases must be a non-empty list" % where)
    for alias in aliases:
        if not isinstance(alias, str) or not alias.strip():
            raise ValidationError("%s.calendar_aliases entries must be non-empty strings" % where)

    ignore_globs = payload.get("ignore_globs", [])
    if not isinstance(ignore_globs, list) or any(not isinstance(item, str) for item in ignore_globs):
        raise ValidationError("%s.ignore_globs must be a list of strings" % where)

    retest_gap = payload.get("retest_gap_days")
    if retest_gap is not None and (not isinstance(retest_gap, int) or retest_gap < 0):
        raise ValidationError("%s.retest_gap_days must be a non-negative integer" % where)

    raw_workflows = payload.get("workflows")
    if not isinstance(raw_workflows, list) or not raw_workflows:
        raise ValidationError("%s.workflows must be a non-empty list" % where)

    workflows: List[Workflow] = []
    seen_workflows = set()
    for index, raw in enumerate(raw_workflows):
        label = "%s.workflows[%d]" % (where, index)
        if not isinstance(raw, dict):
            raise ValidationError("%s must be an object" % label)
        _require_keys(raw, WORKFLOW_KEYS, label)
        workflow_id = _require_string(raw, "id", label)
        if not _SLUG.match(workflow_id):
            raise ValidationError("%s.id must be a lowercase slug, got %r" % (label, workflow_id))
        if workflow_id in seen_workflows:
            raise ValidationError("%s repeats workflow id %r" % (where, workflow_id))
        seen_workflows.add(workflow_id)
        authorized = raw.get("authorized")
        if not isinstance(authorized, bool):
            raise ValidationError("%s.authorized must be a boolean" % label)
        description = raw.get("description", "")

        raw_steps = raw.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValidationError("%s.steps must be a non-empty list" % label)

        steps: List[WorkflowStep] = []
        seen_steps = set()
        for step_index, raw_step in enumerate(raw_steps):
            step_label = "%s.steps[%d]" % (label, step_index)
            if not isinstance(raw_step, dict):
                raise ValidationError("%s must be an object" % step_label)
            _require_keys(raw_step, STEP_KEYS, step_label)
            step_id = _require_string(raw_step, "id", step_label)
            if not _SLUG.match(step_id):
                raise ValidationError("%s.id must be a lowercase slug, got %r" % (step_label, step_id))
            if step_id in seen_steps:
                raise ValidationError("%s repeats step id %r" % (label, step_id))
            seen_steps.add(step_id)
            actor = _require_string(raw_step, "actor", step_label)
            if actor not in ACTORS:
                raise ValidationError(
                    "%s.actor must be one of %s, got %r" % (step_label, ", ".join(ACTORS), actor)
                )
            action = _require_string(raw_step, "action", step_label)

            requires = raw_step.get("requires", [])
            if not isinstance(requires, list) or any(not isinstance(item, str) for item in requires):
                raise ValidationError("%s.requires must be a list of strings" % step_label)
            unknown_requirements = sorted(set(requires) - _REQUIREMENT_KINDS)
            if unknown_requirements:
                raise ValidationError(
                    "%s.requires has unknown kind(s): %s" % (step_label, ", ".join(unknown_requirements))
                )

            depends_on = raw_step.get("depends_on", [])
            if not isinstance(depends_on, list) or any(not isinstance(item, str) for item in depends_on):
                raise ValidationError("%s.depends_on must be a list of strings" % step_label)

            steps.append(
                WorkflowStep(
                    id=step_id,
                    actor=actor,
                    action=action,
                    requires=list(requires),
                    depends_on=list(depends_on),
                )
            )

        known = {step.id for step in steps}
        for step in steps:
            missing = [item for item in step.depends_on if item not in known]
            if missing:
                raise ValidationError(
                    "%s: step %r depends on unknown step(s): %s"
                    % (label, step.id, ", ".join(sorted(missing)))
                )
            if step.id in step.depends_on:
                raise ValidationError("%s: step %r depends on itself" % (label, step.id))
        _assert_acyclic(label, steps)

        workflows.append(
            Workflow(
                id=workflow_id,
                authorized=authorized,
                description=description if isinstance(description, str) else "",
                steps=steps,
            )
        )

    extras = {
        "course_integrity_notes": payload.get("course_integrity_notes"),
    }
    del extras
    return CourseWorkflowConfig(
        schema_version=SCHEMA_VERSION,
        course=course,
        root=root,
        calendar_aliases=[alias.strip() for alias in aliases],
        ignore_globs=list(ignore_globs),
        workflows=workflows,
        retest_gap_days=retest_gap,
        path=path,
    )


def _assert_acyclic(label: str, steps: Sequence[WorkflowStep]) -> None:
    by_id = {step.id: step for step in steps}
    visiting: set = set()
    done: set = set()

    def walk(step_id: str) -> None:
        if step_id in done:
            return
        if step_id in visiting:
            raise ValidationError("%s: dependency cycle through step %r" % (label, step_id))
        visiting.add(step_id)
        for parent in by_id[step_id].depends_on:
            walk(parent)
        visiting.discard(step_id)
        done.add(step_id)

    for step in steps:
        walk(step.id)


def load_config(path: Path) -> CourseWorkflowConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise NotFoundError("course workflow config not found: %s" % path)
    except json.JSONDecodeError as exc:
        raise ValidationError("%s is not valid JSON: %s" % (path, exc))
    return parse_config(raw, path=Path(path))


def default_config_dir(project_root: Path) -> Path:
    return Path(project_root) / ".study-system" / "config" / "course-workflows"


def load_all(config_dir: Path) -> List[CourseWorkflowConfig]:
    directory = Path(config_dir)
    if not directory.is_dir():
        raise NotFoundError("course workflow config directory not found: %s" % directory)
    configs: List[CourseWorkflowConfig] = []
    seen_courses = set()
    for entry in sorted(directory.glob("*.json")):
        config = load_config(entry)
        if entry.stem != config.course:
            raise ValidationError(
                "%s must be named after its course (%s.json)" % (entry.name, config.course)
            )
        if config.course in seen_courses:
            raise ValidationError("duplicate course config: %s" % config.course)
        seen_courses.add(config.course)
        configs.append(config)
    if not configs:
        raise ValidationError("no course workflow configs found in %s" % directory)
    return configs


def normalize_calendar_title(title: str) -> str:
    """Fold a calendar title for alias matching: lowercase, alphanumerics only."""
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())


def match_course(configs: Sequence[CourseWorkflowConfig], title: str) -> Optional[CourseWorkflowConfig]:
    """Match a calendar event title to a course through its registered aliases."""
    folded = normalize_calendar_title(title)
    if not folded:
        return None
    best: Optional[CourseWorkflowConfig] = None
    best_length = 0
    for config in configs:
        candidates = list(config.calendar_aliases) + [config.course]
        for alias in candidates:
            needle = normalize_calendar_title(alias)
            if needle and needle in folded and len(needle) > best_length:
                best = config
                best_length = len(needle)
    return best


def occurrence_key(course: str, local_start_iso: str, normalized_title: str) -> str:
    """Build a stable occurrence key that does not depend on any folder name."""
    return "%s|%s|%s" % (course, local_start_iso, normalized_title)


class WorkflowRegistry:
    """Idempotent creation of workflow instances and their step tasks."""

    def __init__(self, conn: sqlite3.Connection, clock: Optional[Clock] = None):
        self.conn = conn
        self.clock = clock or Clock()
        self.conn.row_factory = sqlite3.Row

    def existing(self, course_id: str, workflow_id: str, occurrence: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM workflow_instances
            WHERE course_id = ? AND workflow_id = ? AND occurrence_key = ?
            """,
            (course_id, workflow_id, occurrence),
        ).fetchone()

    def ensure_instance(
        self,
        course_id: str,
        workflow_id: str,
        occurrence: str,
        *,
        run_id: Optional[str] = None,
    ) -> str:
        """Return the instance id, creating it only the first time.

        Rescanning the same course event or the same material must reuse the
        existing instance instead of duplicating the whole post-class chain.
        """
        row = self.existing(course_id, workflow_id, occurrence)
        stamp = to_iso(self.clock.now())
        if row is not None:
            self.conn.execute(
                "UPDATE workflow_instances SET updated_at = ? WHERE id = ?", (stamp, row["id"])
            )
            return row["id"]
        instance_id = str(uuid7(self.clock.now_ms()))
        self.conn.execute(
            """
            INSERT INTO workflow_instances (
                id, course_id, workflow_id, occurrence_key, status, created_by_run_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?)
            """,
            (instance_id, course_id, workflow_id, occurrence, run_id, stamp, stamp),
        )
        return instance_id

    def link_observation(self, instance_id: str, observation_id: str) -> bool:
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO workflow_instance_observations (instance_id, observation_id)
            VALUES (?, ?)
            """,
            (instance_id, observation_id),
        )
        return bool(cursor.rowcount)

    def link_task(self, instance_id: str, step_id: str, task_id: str) -> bool:
        """Attach a task to a workflow step; one task per step per instance."""
        existing = self.conn.execute(
            "SELECT task_id FROM workflow_tasks WHERE instance_id = ? AND step_id = ?",
            (instance_id, step_id),
        ).fetchone()
        if existing is not None:
            return existing["task_id"] == task_id
        self.conn.execute(
            "INSERT INTO workflow_tasks (instance_id, step_id, task_id) VALUES (?, ?, ?)",
            (instance_id, step_id, task_id),
        )
        return True

    def instance_rows(self, instance_id: str) -> Dict[str, str]:
        rows = self.conn.execute(
            "SELECT step_id, task_id FROM workflow_tasks WHERE instance_id = ? ORDER BY step_id",
            (instance_id,),
        ).fetchall()
        return {row["step_id"]: row["task_id"] for row in rows}

    def open_instances(self) -> List[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT i.*, c.code AS course_code
            FROM workflow_instances i
            JOIN courses c ON c.id = i.course_id
            WHERE i.status = 'open'
            ORDER BY i.created_at, i.id
            """
        ).fetchall()

    def refresh_status(self, instance_id: str) -> str:
        """Mark the instance completed once every linked task is closed."""
        rows = self.conn.execute(
            """
            SELECT t.status FROM workflow_tasks wt
            JOIN tasks t ON t.id = wt.task_id
            WHERE wt.instance_id = ?
            """,
            (instance_id,),
        ).fetchall()
        if not rows:
            return "open"
        statuses = {row["status"] for row in rows}
        finished = {"completed", "cancelled", "superseded"}
        status = "completed" if statuses <= finished else "open"
        self.conn.execute(
            "UPDATE workflow_instances SET status = ?, updated_at = ? WHERE id = ?",
            (status, to_iso(self.clock.now()), instance_id),
        )
        return status
