"""Render database state into system and student-facing Markdown views.

The detailed TODAY view may carry inline machine markers.  The student-facing
TODAY_WORKING view never does: its identities live in an internal sidecar so the
Markdown source itself remains a clean action panel.

Only two edits are meaningful: turning ``[ ]`` into ``[x]``, and filling in a
``备注：`` line. Anything else is refused as a batch rather than silently
overwritten — a view conflict must never cost the student a tick.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import ConflictError, NotFoundError, ValidationError
from .models import (
    DESTINATION_LABELS,
    HKT,
    PRIORITIES,
    PRIORITY_LABELS,
    PRIORITY_ORDER,
    Clock,
    Placement,
    compact_json,
    format_due,
    local_date_of,
    parse_date,
    to_iso,
    week_end_of,
    week_start_of,
)
from .planning import destination_of
from .reconciliation import is_weekend
from .service import TaskService
from .workflows import WorkflowRegistry

TASK_MARKER = "study-task"
CRITERION_MARKER = "study-criterion"
HEADER_MARKER = "study-view"

ACTOR_LABELS = {"student": "你", "agent": "Codex", "external": "外部"}
ACTOR_ORDER = {"student": 0, "agent": 1, "external": 2}

NO_EDIT_NOTICE = (
    "本文件是系统内部诊断视图，禁止手工编辑；"
    "完成任务请使用项目根目录 TODAY_WORKING.md。"
)

TASK_MARKER_RE = re.compile(r"^<!--\s*%s\s+(\{.*\})\s*-->\s*$" % TASK_MARKER)
CRITERION_MARKER_RE = re.compile(r"^<!--\s*%s\s+(\{.*\})\s*-->\s*$" % CRITERION_MARKER)
HEADER_MARKER_RE = re.compile(r"^<!--\s*%s\s+(\{.*\})\s*-->\s*$" % HEADER_MARKER)
CHECKBOX_RE = re.compile(r"^(\s*)-\s*\[([ xX])\]\s*(.*)$")
NOTE_RE = re.compile(r"^(\s*)备注：(.*)$")

WORKING_PRIORITY_LABELS = {
    "must": "重点做",
    "should": "接下来做",
    "if_time": "有余力做",
}


def _marker(kind: str, payload: Dict[str, Any]) -> str:
    """Emit a marker with a fixed field order so rendering is deterministic."""
    return "<!-- %s %s -->" % (kind, json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


@dataclass
class RenderTask:
    task_id: str
    task_key: str
    title: str
    actor: str
    course: Optional[str]
    due_at: Optional[str]
    priority: str
    placement_version: int
    version_key: str
    reason: Optional[str] = None
    criteria: List[Dict[str, Any]] = field(default_factory=list)
    blocked_by: List[str] = field(default_factory=list)


def _kind_helpers(conn: sqlite3.Connection) -> Tuple[Dict[str, int], Dict[str, str]]:
    """One prefetch of criteria and blockers; no per-task queries."""
    satisfied: Dict[str, int] = {}
    total: Dict[str, int] = {}
    for row in conn.execute(
        """
        SELECT task_id, SUM(CASE WHEN satisfied = 1 THEN 1 ELSE 0 END) AS done,
               COUNT(*) AS total
        FROM task_criteria WHERE required = 1 GROUP BY task_id
        """
    ).fetchall():
        satisfied[row["task_id"]] = int(row["done"] or 0)
        total[row["task_id"]] = int(row["total"] or 0)

    blockers: Dict[str, List[str]] = {}
    for row in conn.execute(
        """
        SELECT d.task_id, t.title, t.status
        FROM task_dependencies d
        JOIN tasks t ON t.id = d.depends_on_task_id
        WHERE t.status <> 'completed'
        ORDER BY t.task_key
        """
    ).fetchall():
        blockers.setdefault(row["task_id"], []).append(row["title"])
    return satisfied, total, blockers  # type: ignore[return-value]


def _load_render_tasks(
    conn: sqlite3.Connection,
    *,
    day: str,
    week_start: str,
    week_end: str,
    scope: str,
) -> List[RenderTask]:
    """Load the tasks that belong in a TODAY or WEEK view."""
    if scope == "today":
        clause = "p.kind = 'scheduled' AND p.scheduled_date = ?"
        params: List[Any] = [day]
    else:
        clause = "p.kind = 'scheduled' AND p.scheduled_date BETWEEN ? AND ?"
        params = [week_start, week_end]
    rows = conn.execute(
        """
        SELECT t.id, t.task_key, t.title, t.actor, t.due_at, c.code AS course_code,
               p.priority, p.version AS placement_version, p.scheduled_date,
               p.commitment, p.deferral_count
        FROM task_placements p
        JOIN tasks t ON t.id = p.task_id
        LEFT JOIN courses c ON c.id = t.course_id
        WHERE t.status = 'open' AND %s
        ORDER BY t.task_key
        """ % clause,
        params,
    ).fetchall()

    satisfied, total, blockers = _kind_helpers(conn)
    items: List[RenderTask] = []
    for row in rows:
        criteria_rows = conn.execute(
            "SELECT id, position, text, required, satisfied, satisfied_at, created_at "
            "FROM task_criteria WHERE task_id = ? ORDER BY position",
            (row["id"],),
        ).fetchall()
        criteria = []
        for criterion in criteria_rows:
            criteria.append(
                {
                    "id": criterion["id"],
                    "position": criterion["position"],
                    "text": criterion["text"],
                    "required": bool(criterion["required"]),
                    "satisfied": bool(criterion["satisfied"]),
                    "version": criterion["satisfied_at"] or criterion["created_at"],
                }
            )
        reason = None
        if row["commitment"] == "provisional":
            reason = "临时计划（尚未确认）"
        items.append(
            RenderTask(
                task_id=row["id"],
                task_key=row["task_key"],
                title=row["title"],
                actor=row["actor"],
                course=row["course_code"],
                due_at=row["due_at"],
                priority=row["priority"] or "should",
                placement_version=int(row["placement_version"]),
                version_key=row["scheduled_date"],
                reason=reason,
                criteria=criteria,
                blocked_by=blockers.get(row["id"], []),
            )
        )
    return items


def _render_task_block(
    item: RenderTask,
    *,
    indent: str = "",
    indicators: Sequence[str] = (),
    working: bool = False,
) -> List[str]:
    lines: List[str] = []
    parent_done = all(
        criterion["satisfied"] for criterion in item.criteria if criterion["required"]
    ) if item.criteria else False
    checked = "x" if parent_done else " "
    extras = list(indicators)
    if item.due_at:
        extras.append("截止 %s" % format_due(item.due_at))
    extras.extend(item.blocked_by and ["阻塞：等待「%s」完成" % "」、「".join(item.blocked_by)] or [])
    suffix = "".join("｜%s" % text for text in extras if text)
    marker_payload = {
        "id": item.task_id,
        "version": item.placement_version,
        "rendered_checked": bool(parent_done),
        "placement_version": item.placement_version,
    }
    if working:
        marker_payload["view_kind"] = "working"
    lines.append(
        "%s%s" % (
            indent,
            _marker(TASK_MARKER, marker_payload),
        )
    )
    actor = ACTOR_LABELS.get(item.actor, item.actor)
    prefix = "" if working and item.actor == "student" else "【%s】" % actor
    lines.append(
        "%s- [%s] %s%s%s"
        % (indent, checked, prefix, item.title, suffix)
    )
    required = [c for c in item.criteria if c["required"]]
    if len(required) > 1:
        done = sum(1 for c in required if c["satisfied"])
        if done == len(required):
            lines.append("%s  - 进度 %d/%d" % (indent, done, len(required)))
            for criterion in required:
                lines.append(
                    "%s  %s"
                    % (
                        indent,
                        _marker(
                            CRITERION_MARKER,
                            {
                                "id": criterion["id"],
                                "version": criterion["version"],
                                "rendered_checked": True,
                            },
                        ),
                    )
                )
        else:
            for criterion in required:
                lines.append(
                    "%s  %s"
                    % (
                        indent,
                        _marker(
                            CRITERION_MARKER,
                            {
                                "id": criterion["id"],
                                "version": criterion["version"],
                                "rendered_checked": bool(criterion["satisfied"]),
                            },
                        ),
                    )
                )
                lines.append(
                    "%s  - [%s] %s"
                    % (indent, "x" if criterion["satisfied"] else " ", criterion["text"])
                )
    elif len(required) == 1:
        criterion = required[0]
        lines.append(
            "%s  %s"
            % (
                indent,
                _marker(
                    CRITERION_MARKER,
                    {
                        "id": criterion["id"],
                        "version": criterion["version"],
                        "rendered_checked": bool(criterion["satisfied"]),
                    },
                ),
            )
        )
    return lines


def _header(
    *,
    kind: str,
    title: str,
    generated_at: str,
    db_path: Optional[str],
    revision: int,
    extra: Sequence[str] = (),
) -> List[str]:
    lines = [
        "# %s" % title,
        "",
        _marker(
            HEADER_MARKER,
            {"kind": kind, "generated_at": generated_at, "revision": revision, "db": db_path or ""},
        ),
        "> 数据来源：`%s`（SQLite Task Service，唯一任务来源）" % (db_path or "(未指定)"),
        "> 生成时间：%s" % generated_at,
        "> %s" % NO_EDIT_NOTICE,
    ]
    for line in extra:
        lines.append("> %s" % line)
    lines.append("")
    return lines


def working_sidecar_path(path: Path) -> Path:
    """Keep working-view identity data out of the human-facing Markdown."""
    target = Path(path).resolve()
    project_system = target.parent / ".study-system"
    if project_system.is_dir():
        return project_system / "runtime" / "views" / (target.stem + ".state.json")
    return target.parent / ("." + target.name + ".state.json")


def _change_reasons(conn: sqlite3.Connection) -> Dict[str, str]:
    """Latest recorded change_reason per task, taken from published proposals.

    ``task_placements`` stores where a task is, not why it moved there, and the
    placement event payload carries the old and new dates without the reason.
    The reason therefore comes from the proposal that performed the move.
    """
    reasons: Dict[str, str] = {}
    for row in conn.execute(
        "SELECT e.task_id, t.task_key, e.payload FROM task_events e "
        "JOIN tasks t ON t.id = e.task_id ORDER BY e.occurred_at DESC, e.id DESC"
    ).fetchall():
        if row["task_id"] in reasons:
            continue
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        reason = (payload.get("change_reason") or "").strip()
        if reason:
            reasons[row["task_id"]] = reason
            reasons[row["task_key"]] = reason
    rows = conn.execute(
        """
        SELECT proposal_json FROM planning_runs
        WHERE status = 'published' AND proposal_json IS NOT NULL
        ORDER BY completed_at DESC LIMIT 20
        """
    ).fetchall()
    for row in rows:
        try:
            proposal = json.loads(row["proposal_json"])
        except (TypeError, ValueError):
            continue
        for operation in proposal.get("operations", []) or []:
            if operation.get("op") != "set_placement":
                continue
            reason = (operation.get("change_reason") or "").strip()
            ref = operation.get("task")
            if ref and reason and ref not in reasons:
                reasons[ref] = reason
    return reasons


def _forward_placement_lines(
    conn: sqlite3.Connection,
    day: date,
) -> List[str]:
    """Disclose tomorrow and the rest of the week at the end of TODAY.

    The point is honesty about density: a short list today must not hide the
    fact that the work was pushed onto the weekend. Counts and titles are real,
    and a weekend placement shows why it could not happen earlier.
    """
    tomorrow = day + timedelta(days=1)
    week_end = week_end_of(day)
    rows = conn.execute(
        """
        SELECT t.id, t.task_key, t.title, t.actor, p.scheduled_date, p.priority
        FROM task_placements p
        JOIN tasks t ON t.id = p.task_id
        WHERE t.status = 'open' AND p.kind = 'scheduled'
          AND p.scheduled_date > ? AND p.scheduled_date <= ?
        ORDER BY p.scheduled_date, t.task_key
        """,
        (day.isoformat(), week_end.isoformat()),
    ).fetchall()

    reasons = _change_reasons(conn)

    def reason_for(row) -> str:
        return reasons.get(row["task_key"]) or reasons.get(row["id"]) or ""

    lines: List[str] = []
    lines.append("## 接下来两天")
    lines.append("")
    tomorrow_rows = [row for row in rows if row["scheduled_date"] == tomorrow.isoformat()]
    if tomorrow_rows:
        lines.append("明天 %s：%d 项" % (tomorrow.isoformat(), len(tomorrow_rows)))
        lines.append("")
        for row in tomorrow_rows:
            lines.append("- %s" % row["title"])
    else:
        lines.append("明天 %s：暂无安排" % tomorrow.isoformat())
    lines.append("")

    weekend_rows = [
        row for row in rows
        if row["scheduled_date"] > tomorrow.isoformat()
        and is_weekend(parse_date(row["scheduled_date"]))
    ]
    if weekend_rows:
        lines.append("周末（%d 项）" % len(weekend_rows))
        lines.append("")
        for row in weekend_rows:
            reason = reason_for(row).strip()
            suffix = "｜原因：%s" % reason if reason else "｜未说明更早日期为何不可行"
            lines.append(
                "- %s %s%s" % (row["scheduled_date"], row["title"], suffix)
            )
        lines.append("")

    later_weekday_rows = [
        row for row in rows
        if row["scheduled_date"] > tomorrow.isoformat()
        and not is_weekend(parse_date(row["scheduled_date"]))
    ]
    if later_weekday_rows:
        lines.append("本周其余工作日：%d 项" % len(later_weekday_rows))
        lines.append("")
        for row in later_weekday_rows:
            lines.append("- %s %s" % (row["scheduled_date"], row["title"]))
        lines.append("")
    return lines


def render_today(
    conn: sqlite3.Connection,
    target_date: str,
    *,
    generated_at: str,
    db_path: Optional[str] = None,
    revision: int = 0,
) -> str:
    """Course, then Must/Should/If Time, with the actor inline on the action."""
    day = parse_date(target_date)
    items = _load_render_tasks(
        conn,
        day=target_date,
        week_start=week_start_of(day).isoformat(),
        week_end=week_end_of(day).isoformat(),
        scope="today",
    )
    lines = _header(
        kind="today",
        title="今日任务｜%s" % target_date,
        generated_at=generated_at,
        db_path=db_path,
        revision=revision,
    )
    if not items:
        lines.append("（今天没有已安排的开放任务）")
        lines.append("")
    else:
        by_course: Dict[str, List[RenderTask]] = {}
        for item in items:
            by_course.setdefault(item.course or "跨课程", []).append(item)

        for course in sorted(
            by_course,
            key=lambda name: (
                min(PRIORITY_ORDER.get(entry.priority, 9) for entry in by_course[name]),
                name,
            ),
        ):
            lines.append("## %s" % course)
            lines.append("")
            for priority in ("must", "should", "if_time"):
                bucket = [entry for entry in by_course[course] if entry.priority == priority]
                if not bucket:
                    continue
                lines.append("### %s" % PRIORITY_LABELS[priority])
                lines.append("")
                for entry in bucket:
                    indicators = [entry.reason] if entry.reason else []
                    if entry.due_at:
                        indicators = [item for item in indicators if item]
                    lines.extend(_render_task_block(entry, indicators=[entry.reason] if entry.reason else []))
                lines.append("")

    lines.extend(_forward_placement_lines(conn, day))
    return "\n".join(lines).rstrip() + "\n"


def _render_working_document(
    conn: sqlite3.Connection,
    target_date: str,
    *,
    generated_at: str,
    db_path: Optional[str] = None,
    revision: int = 0,
) -> Tuple[str, Dict[str, Any]]:
    """Return clean Markdown plus its private checkbox identity map."""
    day = parse_date(target_date)
    items = _load_render_tasks(
        conn,
        day=target_date,
        week_start=week_start_of(day).isoformat(),
        week_end=week_end_of(day).isoformat(),
        scope="today",
    )
    lines = [
        "# 今日任务｜%s" % target_date,
        "",
        "> 完成后直接勾选；下次与 Codex 交互时自动同步。",
        "",
    ]
    state: Dict[str, Any] = {
        "kind": "working",
        "target_date": target_date,
        "generated_at": generated_at,
        "revision": revision,
        "db": db_path or "",
        "tasks": [],
    }
    by_course: Dict[str, List[RenderTask]] = {}
    for item in items:
        by_course.setdefault(item.course or "跨课程", []).append(item)
    registered = [row["code"] for row in conn.execute("SELECT code FROM courses ORDER BY code")]
    course_names = list(registered)
    for name in by_course:
        if name not in course_names:
            course_names.append(name)

    for course in course_names:
        lines.append("## %s" % course)
        lines.append("")
        course_items = by_course.get(course, [])
        for priority in ("must", "should", "if_time"):
            lines.append("### %s" % WORKING_PRIORITY_LABELS[priority])
            lines.append("")
            bucket = [entry for entry in course_items if entry.priority == priority]
            if not bucket:
                lines.append("- 暂无")
            else:
                for entry in bucket:
                    due = "｜截止 %s" % format_due(entry.due_at) if entry.due_at else ""
                    if entry.actor == "student":
                        visible = "%s%s" % (entry.title, due)
                        lines.append("- [ ] %s" % visible)
                        state["tasks"].append(
                            {
                                "line": len(lines),
                                "text": visible,
                                "marker": {
                                    "id": entry.task_id,
                                    "version": entry.placement_version,
                                    "placement_version": entry.placement_version,
                                    "rendered_checked": False,
                                    "view_kind": "working",
                                },
                            }
                        )
                    else:
                        lines.append(
                            "- 【%s】%s%s"
                            % (ACTOR_LABELS.get(entry.actor, entry.actor), entry.title, due)
                        )
            lines.append("")

    lines.extend(_forward_placement_lines(conn, day))
    return "\n".join(lines).rstrip() + "\n", state


def render_working(
    conn: sqlite3.Connection,
    target_date: str,
    *,
    generated_at: str,
    db_path: Optional[str] = None,
    revision: int = 0,
) -> str:
    content, _state = _render_working_document(
        conn,
        target_date,
        generated_at=generated_at,
        db_path=db_path,
        revision=revision,
    )
    return content


def render_week(
    conn: sqlite3.Connection,
    week_start: str,
    *,
    generated_at: str,
    db_path: Optional[str] = None,
    revision: int = 0,
) -> str:
    """Only this week's commitments, plus a short risk summary at the end."""
    start = parse_date(week_start)
    if start.weekday() != 0:
        raise ValidationError("week view needs a Monday, got %s" % week_start)
    end = week_end_of(start)
    items = _load_render_tasks(
        conn,
        day=start.isoformat(),
        week_start=start.isoformat(),
        week_end=end.isoformat(),
        scope="week",
    )
    waiting = conn.execute(
        """
        SELECT t.task_key, t.title, p.waiting_reason, p.next_check_at
        FROM task_placements p JOIN tasks t ON t.id = p.task_id
        WHERE p.kind = 'waiting' AND t.status = 'open'
        ORDER BY p.next_check_at
        """
    ).fetchall()
    future = conn.execute(
        """
        SELECT COUNT(*) AS n FROM task_placements p JOIN tasks t ON t.id = p.task_id
        WHERE p.kind = 'scheduled' AND t.status = 'open' AND p.scheduled_date > ?
        """,
        (end.isoformat(),),
    ).fetchone()["n"]
    provisional = [item for item in items if item.reason]

    lines = [
        "# 本周安排｜%s 至 %s" % (start.isoformat(), end.isoformat()),
        "",
        "> 只读周视图；完成任务请使用 TODAY_WORKING.md。",
    ]
    if provisional:
        lines.append("> 部分安排尚未确认。")
    lines.append("")
    if not items:
        lines.append("（本周没有已承诺的开放任务）")
        lines.append("")
    else:
        by_course: Dict[str, List[RenderTask]] = {}
        for item in items:
            by_course.setdefault(item.course or "跨课程", []).append(item)
        for course in sorted(
            by_course,
            key=lambda name: (
                min(PRIORITY_ORDER.get(entry.priority, 9) for entry in by_course[name]),
                name,
            ),
        ):
            lines.append("## %s" % course)
            lines.append("")
            for priority in ("must", "should", "if_time"):
                bucket = [entry for entry in by_course[course] if entry.priority == priority]
                if not bucket:
                    continue
                lines.append("### %s" % WORKING_PRIORITY_LABELS[priority])
                lines.append("")
                for entry in bucket:
                    actor = "" if entry.actor == "student" else "【%s】" % ACTOR_LABELS.get(entry.actor, entry.actor)
                    due = "｜截止 %s" % format_due(entry.due_at) if entry.due_at else ""
                    lines.append("- %s｜%s%s%s" % (entry.version_key, actor, entry.title, due))
                lines.append("")

    lines.append("## 摘要")
    lines.append("")
    lines.append("- 等待中：%d 项" % len(waiting))
    lines.append("- 本周之后：%d 项" % int(future))
    return "\n".join(lines).rstrip() + "\n"


def generate_view(
    conn: sqlite3.Connection,
    kind: str,
    path,
    *,
    target: str,
    clock: Optional[Clock] = None,
    db_path: Optional[str] = None,
    revision: int = 0,
) -> Path:
    from .db import write_text_atomic

    clock = clock or Clock()
    generated_at = to_iso(clock.now())
    if kind == "today":
        content = render_today(
            conn, target, generated_at=generated_at, db_path=db_path, revision=revision
        )
    elif kind == "working":
        content, state = _render_working_document(
            conn, target, generated_at=generated_at, db_path=db_path, revision=revision
        )
        write_text_atomic(
            working_sidecar_path(Path(path)),
            json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        )
    elif kind == "week":
        content = render_week(
            conn, target, generated_at=generated_at, db_path=db_path, revision=revision
        )
    else:
        raise ValidationError("unknown view kind: %r" % kind)
    return write_text_atomic(path, content)
