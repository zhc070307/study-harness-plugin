"""Validate Codex proposals against the schema and the planning invariants.

Validation reports *every* problem it can find in one pass. Fixing one field and
rediscovering the next is exactly the loop this is meant to prevent.

A minimal JSON-Schema subset is implemented here on purpose: the project is
standard-library only, and the contract is small and fully under our control.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import ValidationError
from .models import (
    ACTORS,
    ARTIFACT_KINDS,
    ASSESSMENT_SOURCE_KINDS,
    ATTEMPT_TYPES,
    BINDING_STATUSES,
    EVIDENCE_KINDS,
    ISSUE_RESULTS,
    ISSUE_TYPES,
    PLANNING_CONFIRMATIONS,
    PRIORITIES,
    parse_date,
    parse_due_at,
    week_end_of,
    week_start_of,
)
from .planning import destination_of
from .reconciliation import is_weekend
from .workflows import CourseWorkflowConfig

SCHEMA_PATH = Path(__file__).resolve().parent / "proposal.schema.json"

#: A reason for moving a task later that says nothing. "Still in this week" and
#: "the original date passed" describe the old placement, not why an earlier
#: date cannot take the task, so they cannot stand alone as a justification.
EMPTY_DEFERRAL_REASONS = (
    "仍然在本周",
    "保持在本周",
    "维持本周承诺",
    "本周内",
    "原日期已过",
    "原日期已经过去",
    "日期已过",
    "已过期",
    "顺延",
    "延期",
    "推迟",
    "调整",
    "重新安排",
    "still in this week",
    "remain in this week",
    "original date passed",
    "carried forward",
    "postponed",
    "rescheduled",
)

#: A weekend placement has to say why every earlier weekday was unusable.
WEEKDAY_BLOCKING_HINTS = (
    "依赖",
    "available_at",
    "复测",
    "截止",
    "冲突",
    "学生指定",
    "指定",
    "上课",
    "考试",
    "quiz",
    "占用",
    "没有空间",
    "无空间",
    "联调",
    "等待材料",
    "dependency",
    "available_at",
    "retest",
    "deadline",
    "conflict",
    "student",
)

_MIN_REASON_CHARS = 6

WITHIN_WEEK_DEFERRAL_EXCEPTIONS = (
    "学生指定",
    "明确指定",
    "复测",
    "复测间隔",
    "复测日",
    "依赖",
    "available_at",
    "student specified",
    "retest",
    "dependency",
)


def _reason_is_specific(reason: Optional[str]) -> bool:
    """Whether a deferral reason actually justifies the later date.

    A reason is useless when it is empty, too short, or merely restates the old
    placement ("still this week", "the date passed"). Those phrases describe
    where the task was, never why an earlier date cannot hold it.
    """
    if not reason:
        return False
    text = reason.strip()
    if len(text) < _MIN_REASON_CHARS:
        return False
    lowered = text.lower()
    # Strip a leading "因为/由于/because" style connector before comparing.
    for prefix in ("因为", "由于", "原因是", "because ", "since "):
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix):].strip()
    stripped = lowered
    if not stripped:
        return False
    for phrase in EMPTY_DEFERRAL_REASONS:
        needle = phrase.lower()
        if stripped == needle:
            return False
        remainder = stripped.replace(needle, "").strip(" ，,。.；;：:、的和")
        if not remainder:
            return False
        stripped = remainder
    return bool(stripped)


def _weekend_reason_is_specific(reason: Optional[str]) -> bool:
    """Whether a weekend placement names a real weekday-level obstacle."""
    if not _reason_is_specific(reason):
        return False
    lowered = reason.lower()
    return any(hint.lower() in lowered for hint in WEEKDAY_BLOCKING_HINTS)


def _within_week_deferral_is_exception(reason: Optional[str]) -> bool:
    """A busy day is not permission to hide executable work later this week."""
    if not _reason_is_specific(reason):
        return False
    lowered = reason.lower()
    return any(hint.lower() in lowered for hint in WITHIN_WEEK_DEFERRAL_EXCEPTIONS)



class SchemaError(ValidationError):
    """The proposal does not satisfy the published JSON schema."""


def load_schema(path: Optional[Path] = None) -> Dict[str, Any]:
    return json.loads(Path(path or SCHEMA_PATH).read_text(encoding="utf-8"))


def _resolve(schema: Dict[str, Any], root: Dict[str, Any]) -> Dict[str, Any]:
    seen = 0
    while "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            raise SchemaError("unsupported schema reference: %s" % ref)
        node: Any = root
        for part in ref[2:].split("/"):
            node = node[part]
        schema = node
        seen += 1
        if seen > 20:
            raise SchemaError("schema reference loop at %s" % ref)
    return schema


def validate_schema(value: Any, schema: Dict[str, Any], root: Dict[str, Any],
                    path: str, errors: List[str]) -> None:
    """Validate ``value`` against the supported JSON-Schema subset."""
    schema = _resolve(schema, root)

    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not _matches_type(value, types):
            errors.append("%s: expected %s, got %s" % (path, "/".join(types), _type_name(value)))
            return

    if "enum" in schema and value not in schema["enum"]:
        errors.append(
            "%s: %r is not one of %s" % (path, value, ", ".join(repr(item) for item in schema["enum"]))
        )
    if "pattern" in schema and isinstance(value, str):
        if not re.search(schema["pattern"], value):
            errors.append("%s: %r does not match %s" % (path, value, schema["pattern"]))
    if "minLength" in schema and isinstance(value, str) and len(value) < schema["minLength"]:
        errors.append("%s: must be at least %d characters" % (path, schema["minLength"]))
    if "minimum" in schema and isinstance(value, (int, float)) and value < schema["minimum"]:
        errors.append("%s: must be >= %s" % (path, schema["minimum"]))

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                errors.append("%s: missing required field %r" % (path, key))
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            for key in unknown:
                errors.append("%s: unknown field %r" % (path, key))
        for key, item in value.items():
            if key in properties:
                validate_schema(item, properties[key], root, "%s.%s" % (path, key), errors)
    elif isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            validate_schema(item, schema["items"], root, "%s[%d]" % (path, index), errors)


def _matches_type(value: Any, types: Sequence[str]) -> bool:
    for name in types:
        if name == "object" and isinstance(value, dict):
            return True
        if name == "array" and isinstance(value, list):
            return True
        if name == "string" and isinstance(value, str):
            return True
        if name == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if name == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if name == "boolean" and isinstance(value, bool):
            return True
        if name == "null" and value is None:
            return True
    return False


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if value is None:
        return "null"
    return type(value).__name__


def strict_schema(schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return a variant of the proposal schema for provider structured output.

    OpenAI's strict mode rejects a schema whose ``required`` omits a declared
    property, so optional fields are returned as nullable-and-required. The
    checked-in schema stays permissive so a human (or a local caller) can write a
    proposal by hand with only the fields they actually need.
    """
    import copy

    result = copy.deepcopy(schema if schema is not None else load_schema())

    def make_nullable(node: Dict[str, Any]) -> None:
        if "$ref" in node:
            return
        declared = node.get("type")
        if declared is None:
            return
        types = declared if isinstance(declared, list) else [declared]
        if "null" not in types:
            node["type"] = sorted(set(types) | {"null"})

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        properties = node.get("properties")
        if isinstance(properties, dict):
            for value in properties.values():
                if isinstance(value, dict) and "$ref" not in value:
                    make_nullable(value)
                walk(value)
            node["required"] = sorted(properties)
            node["additionalProperties"] = False
        if "items" in node:
            walk(node["items"])
        for key in ("definitions", "$defs"):
            if isinstance(node.get(key), dict):
                for value in node[key].values():
                    walk(value)

    walk(result)
    return result


def write_strict_schema(path) -> Path:
    """Write the strict variant next to the runtime files, for the wrapper."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(strict_schema(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def _drop_nulls(value):
    """Remove explicit nulls in place.

    Provider structured-output mode requires every declared property to be
    present, so optional fields arrive as ``null``. The invariant checks below
    treat "absent" and "null" identically.
    """
    if isinstance(value, dict):
        for key in [k for k, v in value.items() if v is None]:
            del value[key]
        for item in value.values():
            _drop_nulls(item)
    elif isinstance(value, list):
        for item in value:
            _drop_nulls(item)


class ProposalValidator:
    """Schema plus invariant checks for one reconciliation proposal."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        configs: Sequence[CourseWorkflowConfig],
        *,
        query_date: Optional[str] = None,
        current_revision: Optional[int] = None,
    ):
        self.conn = conn
        self.configs = list(configs)
        self.query_date = query_date
        self.current_revision = current_revision
        self.conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------------ entry
    def validate(self, proposal: Dict[str, Any]) -> List[str]:
        """Return every problem found; an empty list means the proposal is usable."""
        errors: List[str] = []
        root = load_schema()
        validate_schema(proposal, root, root, "proposal", errors)
        if errors:
            # Field-level errors make invariant reasoning meaningless.
            return errors

        if self.current_revision is not None and proposal["base_revision"] != self.current_revision:
            errors.append(
                "proposal.base_revision is %s but this plan was built against revision %s; "
                "re-run prepare"
                % (proposal["base_revision"], self.current_revision)
            )
        confirmation = proposal.get("confirmation", "none")
        if confirmation not in PLANNING_CONFIRMATIONS:
            errors.append("proposal.confirmation must be one of %s" % ", ".join(PLANNING_CONFIRMATIONS))

        _drop_nulls(proposal)
        for index, operation in enumerate(proposal.get("operations", [])):
            self._validate_operation(operation, index, errors)

        self._validate_resulting_state(proposal, errors)
        return errors

    # ------------------------------------------------------------- operations
    def _validate_operation(self, operation: Dict[str, Any], index: int, errors: List[str]) -> None:
        label = "proposal.operations[%d]" % index
        op = operation["op"]

        if op == "create_task":
            self._validate_create(operation, label, errors)
        elif op in ("authorize_task", "update_task", "supersede_task", "cancel_task",
                    "add_criterion", "satisfy_criterion", "add_evidence", "complete_task",
                    "clear_placement", "record_miss", "set_placement"):
            if not operation.get("task"):
                errors.append("%s: %s needs a task reference" % (label, op))
            else:
                self._assert_task_exists(operation["task"], label, errors)
        if op == "update_task" and not any(
            field in operation for field in ("title", "description", "due_at", "source", "actor")
        ):
            errors.append("%s: update_task carries no updateable field" % label)
        if op == "supersede_task":
            if not operation.get("by"):
                errors.append("%s: supersede_task needs a replacement task" % label)
            else:
                self._assert_task_exists(operation["by"], label, errors)
            if not operation.get("reason"):
                errors.append("%s: supersede_task needs a reason" % label)
        if op in ("cancel_task", "record_miss") and not operation.get("reason"):
            errors.append("%s: %s needs a reason" % (label, op))

        if op == "set_placement" or (op == "create_task" and operation.get("placement")):
            self._validate_placement_shape(operation, label, errors)

        if op == "add_evidence":
            evidence = operation.get("evidence") or {}
            if evidence.get("kind") not in EVIDENCE_KINDS:
                errors.append("%s: evidence.kind must be one of %s" % (label, ", ".join(EVIDENCE_KINDS)))

        if op == "complete_task":
            self._validate_completion(operation, label, errors)

        if op == "bind_observation":
            if not operation.get("observation_id"):
                errors.append("%s: bind_observation needs an observation_id" % label)
            else:
                self._assert_observation_exists(operation["observation_id"], label, errors)
            if operation.get("binding_status") not in BINDING_STATUSES:
                errors.append(
                    "%s: binding_status must be one of %s" % (label, ", ".join(BINDING_STATUSES))
                )
            if operation.get("artifact_kind") not in (None,) + ARTIFACT_KINDS:
                errors.append("%s: unknown artifact_kind %r" % (label, operation.get("artifact_kind")))
            if operation.get("course") is not None:
                self._assert_course_exists(operation["course"], label, errors)

        if op in ("ensure_workflow_instance", "link_workflow_observation", "link_workflow_task"):
            self._validate_workflow_operation(operation, label, errors)

        if op == "record_assessment":
            self._validate_assessment(operation, label, errors)
        if op == "record_issue_result":
            if operation.get("result") not in ISSUE_RESULTS:
                errors.append("%s: result must be one of %s" % (label, ", ".join(ISSUE_RESULTS)))
            if not operation.get("issue_id"):
                errors.append("%s: record_issue_result needs an issue_id" % label)
            elif self.conn.execute(
                "SELECT 1 FROM learning_issues WHERE id = ?", (operation["issue_id"],)
            ).fetchone() is None:
                errors.append("%s: unknown learning issue %s" % (label, operation["issue_id"]))
            if not operation.get("task"):
                errors.append("%s: record_issue_result needs the retest task" % label)
            else:
                self._assert_task_exists(operation["task"], label, errors)

    def _validate_create(self, operation: Dict[str, Any], label: str, errors: List[str]) -> None:
        if not operation.get("title"):
            errors.append("%s: create_task needs a title" % label)
        actor = operation.get("actor", "student")
        if actor not in ACTORS:
            errors.append("%s: actor must be one of %s" % (label, ", ".join(ACTORS)))
        status = operation.get("status", "proposed")
        if status not in ("proposed", "open"):
            errors.append("%s: a plan may only create proposed or open tasks" % label)
        if status == "open" and not operation.get("authorization_source"):
            errors.append(
                "%s: an open task needs authorization_source (approved workflow step, "
                "confirmed fact or explicit student request); otherwise create it as proposed" % label
            )
        if operation.get("course") is not None:
            self._assert_course_exists(operation["course"], label, errors)
        if operation.get("due_at") is not None:
            try:
                parse_due_at(operation["due_at"])
            except ValueError as exc:
                errors.append("%s: due_at is invalid: %s" % (label, exc))
        for dependency in operation.get("depends_on", []) or []:
            self._assert_task_exists(dependency, label, errors)
        for criterion in operation.get("criteria", []) or []:
            self._validate_criterion_text(criterion, label, errors)

    def _validate_placement_shape(self, operation: Dict[str, Any], label: str, errors: List[str]) -> None:
        placement = operation.get("placement") if operation["op"] == "create_task" else operation
        if not isinstance(placement, dict):
            return
        kind = placement.get("kind")
        if kind not in ("scheduled", "waiting"):
            errors.append("%s: placement.kind must be scheduled or waiting" % label)
            return
        if kind == "scheduled":
            if not placement.get("scheduled_date"):
                errors.append("%s: a scheduled placement needs scheduled_date" % label)
            else:
                try:
                    parse_date(placement["scheduled_date"])
                except ValueError as exc:
                    errors.append("%s: scheduled_date is invalid: %s" % (label, exc))
            if placement.get("priority") not in PRIORITIES:
                errors.append(
                    "%s: a scheduled placement needs priority in %s"
                    % (label, ", ".join(PRIORITIES))
                )
            if placement.get("waiting_reason"):
                errors.append("%s: a scheduled placement must not carry a waiting_reason" % label)
        else:
            if not placement.get("waiting_reason"):
                errors.append("%s: a waiting placement needs a waiting_reason" % label)
            if not placement.get("next_check_at"):
                errors.append("%s: a waiting placement needs next_check_at" % label)
            else:
                try:
                    parse_date(placement["next_check_at"])
                except ValueError as exc:
                    errors.append("%s: next_check_at is invalid: %s" % (label, exc))
            if placement.get("scheduled_date") or placement.get("priority"):
                errors.append("%s: a waiting placement must not carry a date or priority" % label)
        week_start = placement.get("week_start")
        if week_start:
            try:
                if parse_date(week_start).weekday() != 0:
                    errors.append("%s: placement.week_start must be a Monday" % label)
            except ValueError as exc:
                errors.append("%s: week_start is invalid: %s" % (label, exc))
        if placement.get("commitment") and not week_start:
            errors.append("%s: a commitment requires week_start" % label)
        if operation["op"] == "set_placement" and operation.get("task"):
            if not placement.get("change_reason"):
                row = self.conn.execute(
                    "SELECT 1 FROM task_placements p JOIN tasks t ON t.id = p.task_id "
                    "WHERE t.task_key = ? OR t.id = ?",
                    (operation["task"], operation["task"]),
                ).fetchone()
                if row is not None:
                    errors.append(
                        "%s: changing an existing destination requires change_reason" % label
                    )

    def _validate_completion(self, operation: Dict[str, Any], label: str, errors: List[str]) -> None:
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE task_key = ? OR id = ?",
            (operation["task"], operation["task"]),
        ).fetchone()
        if row is None:
            return
        unsatisfied = self.conn.execute(
            """
            SELECT position, text FROM task_criteria
            WHERE task_id = ? AND required = 1 AND satisfied = 0
            """,
            (row["id"],),
        ).fetchall()
        will_satisfy = set(operation.get("satisfy_criteria", []) or [])
        still_open = [
            item["text"] for item in unsatisfied
            if str(item["position"]) not in will_satisfy and item["text"] not in will_satisfy
        ]
        if still_open:
            errors.append(
                "%s: cannot complete %s; required criteria still unsatisfied: %s"
                % (label, row["task_key"], "; ".join(still_open))
            )
        from .models import completion_evidence_kinds

        allowed = set(completion_evidence_kinds(row["actor"]))
        supplied = operation.get("evidence") or {}
        existing = {
            item["kind"]
            for item in self.conn.execute(
                "SELECT kind FROM task_evidence WHERE task_id = ?", (row["id"],)
            ).fetchall()
        }
        if supplied.get("kind") in allowed:
            existing.add(supplied["kind"])
        if not (existing & allowed):
            errors.append(
                "%s: cannot complete %s (%s task); needs evidence of kind %s"
                % (label, row["task_key"], row["actor"], " or ".join(sorted(allowed)))
            )

    def _validate_workflow_operation(self, operation: Dict[str, Any], label: str, errors: List[str]) -> None:
        op = operation["op"]
        config_by_course = {config.course: config for config in self.configs}
        if op == "ensure_workflow_instance":
            course = operation.get("course")
            if course is None:
                errors.append("%s: ensure_workflow_instance needs a course" % label)
                return
            self._assert_course_exists(course, label, errors)
            config = config_by_course.get(course)
            if config is None:
                errors.append("%s: no workflow config for course %s" % (label, course))
                return
            try:
                workflow = config.workflow(operation.get("workflow_id", ""))
            except Exception:
                errors.append(
                    "%s: course %s has no workflow %r" % (label, course, operation.get("workflow_id"))
                )
                return
            if not workflow.authorized:
                errors.append(
                    "%s: workflow %s is not authorized; it may not create open tasks"
                    % (label, workflow.id)
                )
            if not operation.get("occurrence_key"):
                errors.append("%s: ensure_workflow_instance needs an occurrence_key" % label)
        elif op == "link_workflow_task":
            instance = operation.get("instance_id")
            if not instance or self.conn.execute(
                "SELECT 1 FROM workflow_instances WHERE id = ?", (instance,)
            ).fetchone() is None:
                errors.append("%s: unknown workflow instance %r" % (label, instance))
            if not operation.get("step_id"):
                errors.append("%s: link_workflow_task needs a step_id" % label)
            if not operation.get("task"):
                errors.append("%s: link_workflow_task needs a task" % label)
            else:
                self._assert_task_exists(operation["task"], label, errors)
        elif op == "link_workflow_observation":
            instance = operation.get("instance_id")
            if not instance or self.conn.execute(
                "SELECT 1 FROM workflow_instances WHERE id = ?", (instance,)
            ).fetchone() is None:
                errors.append("%s: unknown workflow instance %r" % (label, instance))
            for observation_id in operation.get("observation_ids", []) or [operation.get("observation_id")]:
                if observation_id:
                    self._assert_observation_exists(observation_id, label, errors)

    def _validate_assessment(self, operation: Dict[str, Any], label: str, errors: List[str]) -> None:
        if operation.get("course") is None:
            errors.append("%s: record_assessment needs a course" % label)
        else:
            self._assert_course_exists(operation["course"], label, errors)
        if operation.get("source_kind") not in ASSESSMENT_SOURCE_KINDS:
            errors.append(
                "%s: source_kind must be one of %s" % (label, ", ".join(ASSESSMENT_SOURCE_KINDS))
            )
        if operation.get("attempt_type", "initial") not in ATTEMPT_TYPES:
            errors.append("%s: attempt_type must be one of %s" % (label, ", ".join(ATTEMPT_TYPES)))
        if not operation.get("occurs_at") and not operation.get("occurred_at"):
            errors.append("%s: record_assessment needs occurred_at" % label)
        if not operation.get("summary"):
            errors.append("%s: record_assessment needs a summary" % label)
        for issue in operation.get("issues", []) or []:
            if issue.get("issue_type") not in ISSUE_TYPES:
                errors.append(
                    "%s: issue_type must be one of %s" % (label, ", ".join(ISSUE_TYPES))
                )
            if issue.get("next_retest_on"):
                try:
                    parse_date(issue["next_retest_on"])
                except ValueError as exc:
                    errors.append("%s: next_retest_on is invalid: %s" % (label, exc))

    # -------------------------------------------------------- resulting state
    def _validate_resulting_state(self, proposal: Dict[str, Any], errors: List[str]) -> None:
        """Check the state the proposal would leave behind, not just its steps."""
        closed: set = set()
        placements: Dict[str, Optional[Dict[str, Any]]] = {}
        misses: Dict[str, int] = {}
        reason_required: List[str] = []

        for index, operation in enumerate(proposal.get("operations", [])):
            label = "proposal.operations[%d]" % index
            op = operation["op"]
            ref = operation.get("task")
            task_id = self._task_id(ref) if ref else None

            if op in ("cancel_task", "supersede_task", "complete_task") and task_id:
                closed.add(task_id)
            if op == "create_task":
                placements.setdefault("new:%d" % index, operation.get("placement"))
                continue
            if op == "set_placement" and task_id:
                placements[task_id] = operation
                if self._would_change(task_id, operation):
                    reason_required.append(label)
            if op == "clear_placement" and task_id:
                placements[task_id] = None
            if op == "record_miss" and task_id:
                row = self.conn.execute(
                    "SELECT deferral_count FROM task_placements WHERE task_id = ?", (task_id,)
                ).fetchone()
                base = int(row["deferral_count"]) if row else 0
                if operation.get("confirmed"):
                    misses[task_id] = base + 1
                else:
                    misses[task_id] = base

        for label in reason_required:
            pass  # change_reason is enforced per-operation above; kept for clarity

        # Every open task must end with exactly one destination.
        open_rows = self.conn.execute(
            "SELECT t.id, t.task_key, t.actor, t.status FROM tasks t WHERE t.status = 'open'"
        ).fetchall()
        planned_open = {
            row["id"] for row in open_rows
        } - closed

        for index, operation in enumerate(proposal.get("operations", [])):
            if operation["op"] == "create_task" and operation.get("status", "proposed") == "open":
                if operation.get("placement") is None and "placement" not in operation:
                    errors.append(
                        "proposal.operations[%d]: an open task created here needs a placement "
                        "in the same operation" % index
                    )
            if operation["op"] == "authorize_task" and not operation.get("placement") and not operation.get("placements"):
                task_id = self._task_id(operation.get("task"))
                if task_id and task_id not in placements:
                    existing = self.conn.execute(
                        "SELECT 1 FROM task_placements WHERE task_id = ?", (task_id,)
                    ).fetchone()
                    if existing is None:
                        errors.append(
                            "proposal.operations[%d]: authorizing %s without a placement would "
                            "leave it in BACKLOG" % (index, operation.get("task"))
                        )

        existing_placements = {
            row["task_id"]: dict(row)
            for row in self.conn.execute("SELECT * FROM task_placements").fetchall()
        }
        day = parse_date(self.query_date) if self.query_date else date.today()

        for task_id in sorted(planned_open):
            row = self.conn.execute(
                "SELECT task_key, title, actor FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task_id in placements:
                spec = placements[task_id]
                if spec is not None and spec.get("kind") == "scheduled" and spec.get("scheduled_date"):
                    if parse_date(spec["scheduled_date"]) < day:
                        errors.append(
                            "%s「%s」would be placed on %s, which is already in the past; "
                            "reclassify it to the next truthful action or mark it waiting"
                            % (row["task_key"], row["title"], spec["scheduled_date"])
                        )
                continue
            placed = existing_placements.get(task_id)
            if placed is None:
                errors.append(
                    "%s「%s」is open with no destination (BACKLOG); this proposal must place it "
                    "as scheduled or waiting" % (row["task_key"], row["title"])
                )
                continue
            scheduled = placed["scheduled_date"]
            if placed["kind"] == "scheduled" and scheduled and parse_date(scheduled) < day:
                errors.append(
                    "%s「%s」is still scheduled on the past date %s (OVERDUE); this proposal must "
                    "move it to a real next action or mark it waiting"
                    % (row["task_key"], row["title"], scheduled)
                )

        self._validate_earliest_feasible(
            proposal, day, placements, existing_placements, errors
        )

        # A second explicit miss must produce a real change, not another carry-forward.
        for task_id, count in misses.items():
            if count < 2:
                continue
            row = self.conn.execute(
                "SELECT task_key, title FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            prev = existing_placements.get(task_id)
            spec = placements.get(task_id)
            if spec is None or prev is None:
                continue
            same_priority = spec.get("priority") == prev["priority"]
            only_moved = (
                spec.get("kind") == "scheduled"
                and prev["kind"] == "scheduled"
                and spec.get("scheduled_date") != prev["scheduled_date"]
                and same_priority
            )
            has_real_change = any(
                operation.get("task") in (row["task_key"], task_id)
                and operation["op"] in ("update_task", "add_criterion", "supersede_task", "cancel_task")
                for operation in proposal.get("operations", [])
            )
            lowered_scope = any(
                operation["op"] == "update_task"
                and operation.get("task") in (row["task_key"], task_id)
                and operation.get("description")
                for operation in proposal.get("operations", [])
            )
            if only_moved and not has_real_change and not lowered_scope:
                errors.append(
                    "%s「%s」已第 2 次明确未完成；只把日期继续向后移不是真实变化。必须拆小任务、"
                    "改变方法或材料、降低范围，或由学生确认移出本周/取消"
                    % (row["task_key"], row["title"])
                )

    # -------------------------------------------------- earliest feasible date
    def _validate_earliest_feasible(
        self,
        proposal: Dict[str, Any],
        day: date,
        placements: Dict[str, Optional[Dict[str, Any]]],
        existing_placements: Dict[str, Any],
        errors: List[str],
    ) -> None:
        """Audit every final placement, not merely placements changed this run.

        This deliberately catches unchanged weekend work, newly-created work,
        moves from Sunday to Saturday, and tasks returning from ``waiting``.
        A later date is valid only when its reason survives as audit data.
        """
        operation_reasons: Dict[str, Optional[str]] = {}
        new_tasks: List[Tuple[int, Dict[str, Any]]] = []
        authorized_tasks: List[Tuple[int, Dict[str, Any]]] = []
        for index, operation in enumerate(proposal.get("operations", [])):
            op = operation.get("op")
            if op == "set_placement":
                task_id = self._task_id(operation.get("task"))
                if task_id:
                    operation_reasons[task_id] = operation.get("change_reason")
            elif op == "authorize_task" and isinstance(operation.get("placement"), dict):
                task_id = self._task_id(operation.get("task"))
                if task_id:
                    operation_reasons[task_id] = operation["placement"].get("change_reason")
                    authorized_tasks.append((index, operation))
            elif op == "create_task" and operation.get("status", "proposed") == "open":
                new_tasks.append((index, operation))

        recorded_reasons = self._recorded_placement_reasons()
        closed = {
            self._task_id(operation.get("task"))
            for operation in proposal.get("operations", [])
            if operation.get("op") in ("cancel_task", "supersede_task", "complete_task")
        }

        rows = self.conn.execute(
            "SELECT id, task_key, title FROM tasks WHERE status = 'open'"
        ).fetchall()
        for row in rows:
            task_id = row["id"]
            if task_id in closed:
                continue
            final = placements.get(task_id, existing_placements.get(task_id))
            if final is None or final.get("kind") != "scheduled" or not final.get("scheduled_date"):
                continue
            target = parse_date(final["scheduled_date"])
            if target <= day:
                continue
            available_at = final.get("available_at")
            if available_at and parse_date(available_at) > day:
                continue
            if self._has_unmet_dependency(task_id):
                continue
            reason = operation_reasons.get(task_id, recorded_reasons.get(task_id))
            self._append_earliest_error(
                errors, row["task_key"], row["title"], target, day, reason
            )

        for index, operation in new_tasks:
            placement = operation.get("placement")
            if not isinstance(placement, dict) or placement.get("kind") != "scheduled":
                continue
            scheduled = placement.get("scheduled_date")
            if not scheduled:
                continue
            target = parse_date(scheduled)
            if target <= day:
                continue
            available_at = placement.get("available_at")
            if available_at and parse_date(available_at) > day:
                continue
            self._append_earliest_error(
                errors,
                "proposal.operations[%d]" % index,
                operation.get("title", "新任务"),
                target,
                day,
                placement.get("change_reason"),
            )

        for index, operation in authorized_tasks:
            placement = operation["placement"]
            if placement.get("kind") != "scheduled" or not placement.get("scheduled_date"):
                continue
            target = parse_date(placement["scheduled_date"])
            if target <= day:
                continue
            available_at = placement.get("available_at")
            if available_at and parse_date(available_at) > day:
                continue
            task_id = self._task_id(operation.get("task"))
            if not task_id or self._has_unmet_dependency(task_id):
                continue
            row = self.conn.execute(
                "SELECT task_key, title FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            self._append_earliest_error(
                errors, row["task_key"], row["title"], target, day,
                placement.get("change_reason"),
            )

    def _append_earliest_error(
        self,
        errors: List[str],
        task_key: str,
        title: str,
        target: date,
        day: date,
        reason: Optional[str],
    ) -> None:
        # This invariant governs the current weekly horizon. Later weeks are
        # intentionally planned at the weekly review rather than pulled into
        # every current-day list.
        if target > week_end_of(day):
            return
        if not _reason_is_specific(reason):
            errors.append(
                "%s「%s」可以执行，却被安排到更晚的 %s，且没有说明更早日期为什么不可行；"
                "空理由、“仍在本周”或“原日期已过”不能作为后移依据"
                % (task_key, title, target.isoformat())
            )
            return
        if target <= week_end_of(day) and not _within_week_deferral_is_exception(reason):
            errors.append(
                "%s「%s」本周已经可以执行，却被安排到 %s；课程多、今天忙或希望缩短今日清单"
                "都不能隐藏任务。应把它留在今天并降低优先级，除非存在依赖、复测间隔或学生明确指定日期"
                % (task_key, title, target.isoformat())
            )
            return
        if is_weekend(target) and not _weekend_reason_is_specific(reason):
            errors.append(
                "%s「%s」被安排到周末 %s，但没有说明此前每个工作日（%s）为什么不适合作业；"
                "周末是最后的溢出位置，不是默认的积压位置"
                % (
                    task_key,
                    title,
                    target.isoformat(),
                    "、".join(self._earlier_weekdays(day, target)) or "无",
                )
            )

    def _recorded_placement_reasons(self) -> Dict[str, str]:
        """Return the latest durable reason for each existing task placement."""
        reasons: Dict[str, str] = {}
        rows = self.conn.execute(
            "SELECT task_id, payload FROM task_events ORDER BY occurred_at DESC, id DESC"
        ).fetchall()
        for row in rows:
            if row["task_id"] in reasons:
                continue
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            reason = (payload.get("change_reason") or "").strip()
            if reason:
                reasons[row["task_id"]] = reason

        runs = self.conn.execute(
            "SELECT proposal_json FROM planning_runs "
            "WHERE status = 'published' AND proposal_json IS NOT NULL "
            "ORDER BY completed_at DESC"
        ).fetchall()
        for run in runs:
            try:
                published = json.loads(run["proposal_json"])
            except (TypeError, ValueError):
                continue
            for operation in published.get("operations", []) or []:
                if operation.get("op") != "set_placement":
                    continue
                task_id = self._task_id(operation.get("task"))
                reason = (operation.get("change_reason") or "").strip()
                if task_id and reason and task_id not in reasons:
                    reasons[task_id] = reason
        return reasons

    def _earlier_weekdays(self, day: date, target: date) -> List[str]:
        """Weekdays strictly between ``day`` and ``target``, plus ``day`` itself."""
        result: List[str] = []
        cursor = day
        while cursor < target:
            if not is_weekend(cursor):
                result.append(cursor.isoformat())
            cursor = cursor + timedelta(days=1)
        return result

    def _has_unmet_dependency(self, task_id: str) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM task_dependencies d
            JOIN tasks t ON t.id = d.depends_on_task_id
            WHERE d.task_id = ? AND t.status <> 'completed'
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        return row is not None

    def _would_change(self, task_id: str, operation: Dict[str, Any]) -> bool:
        row = self.conn.execute(
            "SELECT * FROM task_placements WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return False
        kind = operation.get("kind")
        if kind != row["kind"]:
            return True
        if kind == "scheduled":
            return (
                operation.get("scheduled_date") != row["scheduled_date"]
                or operation.get("priority") != row["priority"]
            )
        return operation.get("waiting_reason") != row["waiting_reason"]

    # ------------------------------------------------------------- utilities
    def _task_id(self, ref: Optional[str]) -> Optional[str]:
        if not ref:
            return None
        row = self.conn.execute(
            "SELECT id FROM tasks WHERE task_key = ? OR id = ?", (ref, ref)
        ).fetchone()
        return row["id"] if row else None

    def _assert_task_exists(self, ref: str, label: str, errors: List[str]) -> None:
        if self._task_id(ref) is None:
            errors.append("%s: unknown task %r" % (label, ref))

    def _assert_course_exists(self, ref: str, label: str, errors: List[str]) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM courses WHERE code = ? OR id = ?", (ref, ref)
        ).fetchone()
        if row is None:
            errors.append("%s: unknown course %r" % (label, ref))

    def _assert_observation_exists(self, observation_id: str, label: str, errors: List[str]) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM observations WHERE id = ?", (observation_id,)
        ).fetchone()
        if row is None:
            errors.append("%s: unknown observation %r" % (label, observation_id))

    def _validate_criterion_text(self, text: str, label: str, errors: List[str]) -> None:
        stripped = text.strip()
        if not stripped:
            errors.append("%s: criterion text must not be empty" % label)
            return
        blocked = ("理解", "掌握", "熟悉", "了解", "知道")
        if any(stripped.startswith(prefix) for prefix in blocked):
            errors.append(
                "%s: criterion %r is not independently judgeable; state the observable "
                "action (read / explain / compute / submit) instead" % (label, stripped)
            )


def validate_paths_within_roots(
    proposal: Dict[str, Any], configs: Sequence[CourseWorkflowConfig], project_root: Path
) -> List[str]:
    """Refuse any referenced path that escapes the registered course roots."""
    errors: List[str] = []
    roots = [(project_root / config.root).resolve() for config in configs]
    for index, operation in enumerate(proposal.get("operations", [])):
        reference = operation.get("evidence", {}).get("reference") if operation.get("evidence") else None
        if not reference:
            continue
        candidate = Path(reference)
        if not candidate.is_absolute():
            candidate = project_root / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            errors.append("proposal.operations[%d]: cannot resolve reference %r" % (index, reference))
            continue
        if not any(root == resolved or root in resolved.parents for root in roots):
            errors.append(
                "proposal.operations[%d]: reference %r is outside every registered course root"
                % (index, reference)
            )
    return errors
