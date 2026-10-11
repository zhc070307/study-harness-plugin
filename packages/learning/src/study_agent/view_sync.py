"""Synchronize student task ticks from TODAY_WORKING.md back into the database.

The rule is all-or-nothing. If any line was edited, any version is stale, any
tick is illegal for its actor, or one checkbox was un-ticked, nothing is written
and every conflict is reported with its file line, visible title and private id —
so the student never loses ticks to a half-applied merge. New working views keep
those ids in a private sidecar; legacy detailed views retain inline markers.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .db import read_text
from .errors import ConflictError, FileError, NotFoundError, ValidationError
from .models import (
    Clock,
    completion_evidence_kinds,
    format_due,
    to_iso,
)
from .uuid7 import uuid7
from .views_v3 import ACTOR_LABELS
from .service import TaskService
from .views_v3 import (
    CHECKBOX_RE,
    CRITERION_MARKER_RE,
    HEADER_MARKER_RE,
    NOTE_RE,
    TASK_MARKER_RE,
    working_sidecar_path,
)


@dataclass
class Conflict:
    line: int
    title: str
    hidden_id: str
    problem: str
    fix: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "line": self.line,
            "title": self.title,
            "id": self.hidden_id,
            "problem": self.problem,
            "fix": self.fix,
        }


@dataclass
class ParsedElement:
    line: int
    checked: bool
    text: str
    marker: Dict[str, Any]
    kind: str  # "task" | "criterion"
    note: Optional[str] = None
    note_line: Optional[int] = None


@dataclass
class SyncPlan:
    file: str
    header: Optional[Dict[str, Any]] = None
    elements: List[ParsedElement] = field(default_factory=list)
    conflicts: List[Conflict] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.conflicts

    def as_dict(self) -> Dict[str, Any]:
        return {
            "file": self.file,
            "ok": self.ok,
            "header": self.header,
            "conflicts": [item.as_dict() for item in self.conflicts],
            "changes": [
                {
                    "kind": item.kind,
                    "id": item.marker.get("id"),
                    "title": item.text,
                    "checked": item.checked,
                }
                for item in self.elements
                if item.checked != item.marker.get("rendered_checked")
            ],
        }


class ViewSync:
    """Parse a view, validate every edit, then apply the whole batch or nothing."""

    def __init__(self, conn: sqlite3.Connection, clock: Optional[Clock] = None):
        self.conn = conn
        self.clock = clock or Clock()
        self.tasks = TaskService(conn, clock=self.clock)
        self.conn.row_factory = sqlite3.Row

    # --------------------------------------------------------------- parsing
    def parse(self, path: Path) -> SyncPlan:
        plan = SyncPlan(file=str(path))
        text = read_text(path)
        sidecar = working_sidecar_path(path)
        if sidecar.exists():
            return self._parse_working(path, text, sidecar, plan)
        current_task: Optional[ParsedElement] = None
        for number, raw in enumerate(text.splitlines(), start=1):
            header = HEADER_MARKER_RE.match(raw.strip())
            if header:
                try:
                    plan.header = json.loads(header.group(1))
                except ValueError:
                    plan.conflicts.append(
                        Conflict(number, "", "", "视图头部标记损坏", "重新生成视图")
                    )
                continue

            marker_match = TASK_MARKER_RE.match(raw.strip())
            if marker_match:
                try:
                    payload = json.loads(marker_match.group(1))
                except ValueError:
                    plan.conflicts.append(
                        Conflict(number, "", "", "任务标记损坏，无法识别", "重新生成视图")
                    )
                    continue
                current_task = ParsedElement(
                    line=number,
                    checked=False,
                    text="",
                    marker=payload,
                    kind="task",
                )
                plan.elements.append(current_task)
                continue

            criterion_match = CRITERION_MARKER_RE.match(raw.strip())
            if criterion_match:
                try:
                    payload = json.loads(criterion_match.group(1))
                except ValueError:
                    plan.conflicts.append(
                        Conflict(number, "", "", "完成条件标记损坏，无法识别", "重新生成视图")
                    )
                    continue
                # A criterion marker always follows its task marker.
                plan.elements.append(
                    ParsedElement(
                        line=number,
                        checked=False,
                        text="",
                        marker=payload,
                        kind="criterion",
                    )
                )
                continue

            checkbox = CHECKBOX_RE.match(raw)
            if checkbox:
                checked = checkbox.group(2).lower() == "x"
                body = checkbox.group(3).strip()
                if body:
                    # Attach to the nearest preceding marker of the same kind.
                    target = self._pending_element(plan, indent=len(checkbox.group(1)))
                    if target is not None:
                        target.checked = checked
                        target.text = body
                    else:
                        plan.reasons.append("第 %d 行有一个无法归属的复选框，已忽略" % number)
                continue

            note = NOTE_RE.match(raw)
            if note:
                # A note belongs to the last visible line, which may be a
                # criterion checkbox rather than the task heading.
                target = self._last_visible(plan) or current_task
                if target is not None:
                    target.note = note.group(2).strip()
                    target.note_line = number
        return plan

    def _parse_working(
        self, path: Path, text: str, sidecar: Path, plan: SyncPlan
    ) -> SyncPlan:
        """Parse a clean working view using identities from its private sidecar."""
        try:
            state = json.loads(read_text(sidecar))
        except (ValueError, FileError):
            plan.conflicts.append(
                Conflict(0, "", "", "工作清单的内部映射损坏", "重新生成工作清单")
            )
            return plan
        if state.get("kind") != "working" or not isinstance(state.get("tasks"), list):
            plan.conflicts.append(
                Conflict(0, "", "", "工作清单的内部映射格式不正确", "重新生成工作清单")
            )
            return plan
        plan.header = {
            "kind": "working",
            "generated_at": state.get("generated_at"),
            "revision": state.get("revision"),
            "db": state.get("db", ""),
        }
        expected = {int(item["line"]): item for item in state["tasks"]}
        seen = set()
        for number, raw in enumerate(text.splitlines(), start=1):
            item = expected.get(number)
            checkbox = CHECKBOX_RE.match(raw)
            if item is not None:
                if checkbox is None:
                    plan.conflicts.append(
                        Conflict(
                            number,
                            item.get("text", ""),
                            item.get("marker", {}).get("id", ""),
                            "任务行被删除、移动或不再是复选框",
                            "恢复该行或重新生成工作清单",
                        )
                    )
                    continue
                seen.add(number)
                plan.elements.append(
                    ParsedElement(
                        line=number,
                        checked=checkbox.group(2).lower() == "x",
                        text=checkbox.group(3).strip(),
                        marker=item["marker"],
                        kind="task",
                    )
                )
            elif checkbox is not None:
                plan.conflicts.append(
                    Conflict(
                        number,
                        checkbox.group(3).strip(),
                        "",
                        "出现不属于本次生成清单的复选框",
                        "删除该行；新增任务请告诉 Codex",
                    )
                )
        for number, item in expected.items():
            if number not in seen and not any(c.line == number for c in plan.conflicts):
                plan.conflicts.append(
                    Conflict(
                        number,
                        item.get("text", ""),
                        item.get("marker", {}).get("id", ""),
                        "找不到生成时的任务行",
                        "重新生成工作清单",
                    )
                )
        return plan

    def _last_visible(self, plan: SyncPlan) -> Optional[ParsedElement]:
        for element in reversed(plan.elements):
            if element.text:
                return element
        return None

    def _pending_element(self, plan: SyncPlan, *, indent: int) -> Optional[ParsedElement]:
        # The most recent marker that has not yet been given a visible line.
        for element in reversed(plan.elements):
            if not element.text:
                return element
        return None

    # ------------------------------------------------------------ validation
    def validate(self, plan: SyncPlan) -> SyncPlan:
        """Check every element against the database; collect all conflicts."""
        if plan.header is None:
            plan.conflicts.append(
                Conflict(0, "", "", "找不到视图头部标记，无法确认版本", "重新生成视图后再同步")
            )
        for element in plan.elements:
            if element.kind == "task":
                self._validate_task(element, plan)
            else:
                self._validate_criterion(element, plan)
        return plan

    def _validate_task(self, element: ParsedElement, plan: SyncPlan) -> None:
        task_id = element.marker.get("id")
        if not task_id:
            plan.conflicts.append(
                Conflict(element.line, element.text, "", "任务标记缺少 id", "重新生成视图")
            )
            return
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            plan.conflicts.append(
                Conflict(
                    element.line,
                    element.text,
                    task_id,
                    "该任务已不在数据库（可能已被删除或替换）",
                    "重新生成视图",
                )
            )
            return
        if not element.text:
            plan.conflicts.append(
                Conflict(element.line, row["title"], task_id, "任务行缺少可见标题，可能被删除", "恢复该行或重新生成视图")
            )
            return
        expected = self._expected_task_text(
            task_id, view_kind=element.marker.get("view_kind")
        )
        if expected is None:
            plan.conflicts.append(
                Conflict(
                    element.line,
                    element.text,
                    task_id,
                    "该任务当前没有正式安排，不在可同步视图中",
                    "重新生成视图",
                )
            )
            return
        if element.text != expected:
            plan.conflicts.append(
                Conflict(
                    element.line,
                    element.text,
                    task_id,
                    "任务行文字被修改（不可改标题、课程、执行者、日期或优先级）",
                    "恢复为「%s」或重新生成视图" % expected,
                )
            )
            return
        placement = self.conn.execute(
            "SELECT version FROM task_placements WHERE task_id = ?", (task_id,)
        ).fetchone()
        if placement is None or int(placement["version"]) != int(element.marker.get("placement_version", -1)):
            plan.conflicts.append(
                Conflict(
                    element.line,
                    element.text,
                    task_id,
                    "视图中的安排版本已过期",
                    "重新生成视图后再勾选，避免覆盖较新的安排",
                )
            )
            return
        if element.checked and not element.marker.get("rendered_checked", False):
            if row["status"] != "open":
                plan.conflicts.append(
                    Conflict(
                        element.line,
                        element.text,
                        task_id,
                        "任务状态为 %s，不能再勾选完成" % row["status"],
                        "使用显式 reopen 后再处理",
                    )
                )
                return
            allowed = completion_evidence_kinds(row["actor"])
            if "student_confirmation" not in allowed:
                existing = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM task_evidence WHERE task_id = ? AND kind IN (%s)"
                    % ", ".join("?" for _ in allowed),
                    (task_id,) + allowed,
                ).fetchone()["n"]
                if not int(existing):
                    plan.conflicts.append(
                        Conflict(
                            element.line,
                            element.text,
                            task_id,
                            "%s 是 %s 任务，手工勾选不能代替%s证据"
                            % (row["task_key"], row["actor"], "或".join(allowed)),
                            "先记录客观记录或已核验的 artifact，再同步",
                        )
                    )

    def _validate_criterion(self, element: ParsedElement, plan: SyncPlan) -> None:
        criterion_id = element.marker.get("id")
        if not criterion_id:
            plan.conflicts.append(
                Conflict(element.line, element.text, "", "完成条件标记缺少 id", "重新生成视图")
            )
            return
        row = self.conn.execute(
            "SELECT * FROM task_criteria WHERE id = ?", (criterion_id,)
        ).fetchone()
        if row is not None and row["satisfied"] and not element.checked:
            plan.conflicts.append(
                Conflict(
                    element.line,
                    element.text or row["text"],
                    criterion_id,
                    "该完成条件已在数据库中满足，但视图显示未勾选（视图已过期）",
                    "重新生成视图后再同步",
                )
            )
            return
        if row is None:
            plan.conflicts.append(
                Conflict(
                    element.line,
                    element.text,
                    criterion_id,
                    "该完成条件已不在数据库",
                    "重新生成视图",
                )
            )
            return
        if element.text:
            if element.text != row["text"]:
                plan.conflicts.append(
                    Conflict(
                        element.line,
                        element.text,
                        criterion_id,
                        "完成条件文字被修改",
                        "恢复为「%s」或重新生成视图" % row["text"],
                    )
                )
                return
        current_version = row["satisfied_at"] or row["created_at"]
        if str(element.marker.get("version")) != str(current_version):
            plan.conflicts.append(
                Conflict(
                    element.line,
                    element.text or row["text"],
                    criterion_id,
                    "完成条件版本已过期",
                    "重新生成视图后再勾选",
                )
            )
            return
        rendered = bool(element.marker.get("rendered_checked", False))
        if rendered and not element.checked:
            plan.conflicts.append(
                Conflict(
                    element.line,
                    element.text or row["text"],
                    criterion_id,
                    "把已勾选的完成条件改回未勾选（取消勾选不等于重开）",
                    "如需重开，请显式使用 task criterion reopen 并说明原因",
                )
            )

    def _expected_task_text(
        self, task_id: str, *, view_kind: Optional[str] = None
    ) -> Optional[str]:
        """Reconstruct the exact visible line the renderer would produce."""
        row = self.conn.execute(
            """
            SELECT t.title, t.actor, t.due_at, p.priority, p.commitment
            FROM task_placements p JOIN tasks t ON t.id = p.task_id
            WHERE t.id = ? AND t.status = 'open' AND p.kind = 'scheduled'
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        extras = []
        if row["commitment"] == "provisional":
            extras.append("临时计划（尚未确认）")
        if row["due_at"]:
            extras.append("截止 %s" % format_due(row["due_at"]))
        blocked = self.conn.execute(
            """
            SELECT t.title FROM task_dependencies d
            JOIN tasks t ON t.id = d.depends_on_task_id
            WHERE d.task_id = ? AND t.status <> 'completed'
            ORDER BY t.task_key
            """,
            (task_id,),
        ).fetchall()
        if blocked:
            extras.append("阻塞：等待「%s」完成" % "」、「".join(item["title"] for item in blocked))
        suffix = "".join("｜%s" % text for text in extras)
        if view_kind == "working" and row["actor"] == "student":
            return "%s%s" % (row["title"], suffix)
        return "【%s】%s%s" % (
            ACTOR_LABELS.get(row["actor"], row["actor"]), row["title"], suffix
        )

    # ----------------------------------------------------------------- apply
    def apply(self, plan: SyncPlan, *, operator: str = "student") -> Dict[str, Any]:
        """Apply all ticks in one transaction, or refuse the whole batch."""
        if plan.conflicts:
            raise ConflictError(
                "view sync refused: %d conflict(s); nothing was written"
                % len(plan.conflicts)
            )
        changes = [
            element
            for element in plan.elements
            if element.checked != bool(element.marker.get("rendered_checked", False))
        ]
        notes = [element for element in plan.elements if element.note]
        if not changes and not notes:
            return {"applied": [], "conflicts": [], "note": "no changes"}

        applied: List[Dict[str, Any]] = []
        with self.tasks._write():
            # Notes are evidence on the owning task, whichever line carried them.
            for element in plan.elements:
                if not element.note:
                    continue
                if element.kind == "task":
                    row = self.conn.execute(
                        "SELECT task_key, status FROM tasks WHERE id = ?",
                        (element.marker.get("id"),),
                    ).fetchone()
                    owner = row["task_key"] if row is not None and row["status"] == "open" else None
                else:
                    owner = self._owner_task(element)
                if owner is not None:
                    self._record_note(owner, element.note)
            for element in changes:
                if element.kind == "criterion":
                    self._apply_criterion(element, applied)
                else:
                    self._apply_task(element, applied, operator)
        return {"applied": applied, "conflicts": []}

    def _apply_criterion(self, element: ParsedElement, applied: List[Dict[str, Any]]) -> None:
        criterion_id = element.marker["id"]
        row = self.conn.execute(
            """
            SELECT tc.*, t.task_key, t.actor, t.status AS task_status, t.id AS task_id
            FROM task_criteria tc JOIN tasks t ON t.id = tc.task_id
            WHERE tc.id = ?
            """,
            (criterion_id,),
        ).fetchone()
        if row["satisfied"]:
            return
        self.conn.execute(
            "UPDATE task_criteria SET satisfied = 1, satisfied_at = ? WHERE id = ?",
            (to_iso(self.clock.now()), criterion_id),
        )
        self.conn.execute(
            """
            INSERT INTO task_evidence (id, task_id, kind, summary, reference, recorded_by, recorded_at)
            VALUES (?, ?, 'student_confirmation', ?, NULL, 'view_sync', ?)
            """,
            (
                str(uuid7(self.clock.now_ms())),
                row["task_id"],
                "学生在 TODAY/WEEK 中勾选：%s" % row["text"],
                to_iso(self.clock.now()),
            ),
        )
        self.tasks._record_event(
            row["task_id"],
            "criterion_satisfied",
            {"criterion_id": criterion_id, "text": row["text"], "source": "view_sync"},
            "view_sync",
        )
        self._maybe_complete(row["task_id"], applied)
        applied.append({"criterion_id": criterion_id, "task": row["task_key"]})

    def _apply_task(self, element: ParsedElement, applied: List[Dict[str, Any]], operator: str) -> None:
        task_id = element.marker["id"]
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        # Ticking a parent claims every still-open required criterion.
        unsatisfied = self.conn.execute(
            """
            SELECT id, text FROM task_criteria
            WHERE task_id = ? AND required = 1 AND satisfied = 0 ORDER BY position
            """,
            (task_id,),
        ).fetchall()
        for criterion in unsatisfied:
            self.conn.execute(
                "UPDATE task_criteria SET satisfied = 1, satisfied_at = ? WHERE id = ?",
                (
                    to_iso(self.clock.now()),
                    criterion["id"],
                ),
            )
            self.tasks._record_event(
                task_id,
                "criterion_satisfied",
                {"criterion_id": criterion["id"], "text": criterion["text"], "source": "view_sync"},
                "view_sync",
            )
        self.tasks.add_evidence(
            row["task_key"],
            "student_confirmation",
            "学生在视图中勾选完成",
            operator="view_sync",
        )
        allowed = completion_evidence_kinds(row["actor"])
        has_evidence = self.conn.execute(
            "SELECT COUNT(*) AS n FROM task_evidence WHERE task_id = ? AND kind IN (%s)"
            % ", ".join("?" for _ in allowed),
            (task_id,) + allowed,
        ).fetchone()["n"]
        if not int(has_evidence):
            raise ConflictError(
                "%s 是 %s 任务，手工勾选不能代替%s证据"
                % (
                    row["task_key"],
                    row["actor"],
                    "或".join(allowed),
                )
            )
        self.tasks.complete(row["task_key"], note="TODAY/WEEK 勾选", operator="view_sync")
        applied.append({"task": row["task_key"]})

    def _owner_task(self, element: ParsedElement) -> Optional[str]:
        row = self.conn.execute(
            """
            SELECT t.task_key, t.status FROM task_criteria tc
            JOIN tasks t ON t.id = tc.task_id WHERE tc.id = ?
            """,
            (element.marker.get("id"),),
        ).fetchone()
        if row is None or row["status"] != "open":
            return None
        return row["task_key"]

    def _record_note(self, task_ref: str, note: str) -> None:
        existing = self.conn.execute(
            "SELECT 1 FROM task_evidence WHERE task_id = (SELECT id FROM tasks WHERE task_key = ?) "
            "AND kind = 'note' AND summary = ?",
            (task_ref, note),
        ).fetchone()
        if existing is not None:
            return
        self.tasks.add_evidence(task_ref, "note", note, operator="view_sync")

    def _maybe_complete(self, task_id: str, applied: List[Dict[str, Any]]) -> None:
        remaining = self.conn.execute(
            "SELECT COUNT(*) AS n FROM task_criteria WHERE task_id = ? AND required = 1 AND satisfied = 0",
            (task_id,),
        ).fetchone()["n"]
        if int(remaining):
            return
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row["status"] != "open":
            return
        allowed = completion_evidence_kinds(row["actor"])
        has_evidence = self.conn.execute(
            "SELECT COUNT(*) AS n FROM task_evidence WHERE task_id = ? AND kind IN (%s)"
            % ", ".join("?" for _ in allowed),
            (task_id,) + allowed,
        ).fetchone()["n"]
        if not int(has_evidence):
            raise ConflictError(
                "%s 是 %s 任务；全部完成条件已满足，但仍缺少%s证据，不能仅凭勾选关闭"
                % (row["task_key"], row["actor"], "或".join(allowed))
            )
        self.tasks.complete(row["task_key"], note="TODAY/WEEK 勾选", operator="view_sync")
        applied.append({"task": row["task_key"]})


def sync_file(conn: sqlite3.Connection, path: Path, *, clock: Optional[Clock] = None) -> Dict[str, Any]:
    """Parse, validate and apply one view file."""
    syncer = ViewSync(conn, clock=clock)
    plan = syncer.parse(Path(path))
    syncer.validate(plan)
    if plan.conflicts:
        return {
            "ok": False,
            "file": str(path),
            "conflicts": [item.as_dict() for item in plan.conflicts],
            "applied": [],
        }
    result = syncer.apply(plan)
    return {"ok": True, "file": str(path), "conflicts": [], **result}
