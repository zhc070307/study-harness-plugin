"""The DAILY_LOG.md history view.

TODAY.md and WEEK.md moved to :mod:`study_agent.views_v3` when planning v3
introduced placements and checkable views. This module keeps the append-only
daily history, which is a projection of the event log and is never ticked.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

from .db import write_text_atomic
from .models import (
    PRIORITY_LABELS,
    PRIORITY_ORDER,
    Clock,
    format_local,
    local_date_of,
    parse_date,
    to_iso,
)

NO_EDIT_NOTICE = "本文件由 study-agent 自动生成，禁止手工编辑；任务状态变更必须通过 Task Service。"
def _source_line(db_path: Optional[str]) -> str:
    return "> 数据来源：`%s`（SQLite Task Service，唯一任务来源）" % (db_path or "(未指定)")


def _generated_line(generated_at: str) -> str:
    return "> 生成时间：%s（%s）" % (format_local(generated_at, "%Y-%m-%d %H:%M %Z"), generated_at)


def _course_label(row: sqlite3.Row) -> str:
    return row["course_code"] if row["course_code"] else "跨课程"


def describe_event(row: sqlite3.Row, task_key: str, task_title: str) -> str:
    """Return a short human sentence for one event row."""
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        payload = {}
    event_type = row["event_type"]
    prefix = "%s｜%s" % (task_key, task_title)
    if event_type == "task_created":
        return "%s｜创建任务（状态 %s）" % (prefix, payload.get("status", "?"))
    if event_type == "proposal_authorized":
        return "%s｜候选任务授权为 open" % prefix
    if event_type == "task_updated":
        return "%s｜更新字段：%s" % (prefix, "、".join(payload.get("fields", [])))
    if event_type == "criterion_added":
        return "%s｜新增完成条件：%s" % (prefix, payload.get("text", ""))
    if event_type == "criterion_satisfied":
        return "%s｜完成条件已满足：%s" % (prefix, payload.get("text", ""))
    if event_type == "criterion_reopened":
        return "%s｜完成条件重新打开：%s" % (prefix, payload.get("text", ""))
    if event_type == "dependency_added":
        return "%s｜新增依赖：%s" % (prefix, payload.get("depends_on", ""))
    if event_type == "dependency_removed":
        return "%s｜移除依赖：%s" % (prefix, payload.get("depends_on", ""))
    if event_type == "scheduled":
        return "%s｜安排到 %s（%s）" % (
            prefix,
            payload.get("date", ""),
            PRIORITY_LABELS.get(payload.get("priority", ""), payload.get("priority", "")),
        )
    if event_type == "rescheduled":
        origin = "、".join(payload.get("from", [])) or "未安排"
        return "%s｜改期/结转：%s → %s（%s）" % (
            prefix,
            origin,
            payload.get("to", ""),
            PRIORITY_LABELS.get(payload.get("priority", ""), payload.get("priority", "")),
        )
    if event_type == "priority_changed":
        return "%s｜%s 优先级：%s → %s" % (
            prefix,
            payload.get("date", ""),
            PRIORITY_LABELS.get(payload.get("from", ""), payload.get("from", "")),
            PRIORITY_LABELS.get(payload.get("to", ""), payload.get("to", "")),
        )
    if event_type == "evidence_added":
        return "%s｜新增证据（%s）：%s" % (prefix, payload.get("kind", ""), payload.get("summary", ""))
    if event_type == "completed":
        return "%s｜标记完成（必需条件 %s 项，完成证据 %s 项）" % (
            prefix,
            payload.get("required_criteria", "?"),
            payload.get("evidence", "?"),
        )
    if event_type == "cancelled":
        reason = payload.get("reason")
        return "%s｜取消%s" % (prefix, "：" + reason if reason else "")
    if event_type == "superseded":
        reason = payload.get("reason")
        return "%s｜被 %s 替代%s" % (
            prefix,
            payload.get("superseded_by", "?"),
            "：" + reason if reason else "",
        )
    if event_type == "reopened":
        return "%s｜从 %s 重新打开：%s" % (
            prefix,
            payload.get("from_status", "?"),
            payload.get("reason", ""),
        )
    return "%s｜%s" % (prefix, event_type)


def render_daily(
    conn: sqlite3.Connection,
    *,
    generated_at: str,
    db_path: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> str:
    """Render the DAILY_LOG.md view as schedule snapshots plus daily events."""
    if from_date:
        parse_date(from_date)
    if to_date:
        parse_date(to_date)

    schedule_dates = {
        row["scheduled_date"]
        for row in conn.execute("SELECT DISTINCT scheduled_date FROM task_schedules").fetchall()
    }
    event_rows = conn.execute(
        """
        SELECT e.*, t.task_key AS task_key, t.title AS task_title
        FROM task_events e
        JOIN tasks t ON t.id = e.task_id
        ORDER BY e.occurred_at, e.id
        """
    ).fetchall()
    events_by_date: Dict[str, List[sqlite3.Row]] = {}
    for row in event_rows:
        day = local_date_of(row["occurred_at"]).isoformat()
        events_by_date.setdefault(day, []).append(row)

    all_dates = sorted(schedule_dates | set(events_by_date))
    if from_date:
        all_dates = [day for day in all_dates if day >= from_date]
    if to_date:
        all_dates = [day for day in all_dates if day <= to_date]

    lines = [
        "# 每日任务记录（生成视图）",
        "",
        _source_line(db_path),
        _generated_line(generated_at),
        "> %s" % NO_EDIT_NOTICE,
        "> 调度快照显示当天仍有效的安排；改期、结转、完成、取消、替代与重新打开见“当日事件”。",
        "",
    ]
    if not all_dates:
        lines.append("（暂无记录）")
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    for day in all_dates:
        lines.append("## %s" % day)
        lines.append("")
        lines.append("### 调度快照")
        lines.append("")
        snapshot = conn.execute(
            """
            SELECT s.priority, s.note, t.task_key, t.title, t.status, c.code AS course_code
            FROM task_schedules s
            JOIN tasks t ON t.id = s.task_id
            LEFT JOIN courses c ON c.id = t.course_id
            WHERE s.active = 1 AND s.scheduled_date = ?
            ORDER BY t.task_key
            """,
            (day,),
        ).fetchall()
        if not snapshot:
            lines.append("（无当前有效安排）")
        else:
            for row in sorted(
                snapshot,
                key=lambda item: (PRIORITY_ORDER.get(item["priority"], 9), item["task_key"]),
            ):
                lines.append(
                    "- %s｜%s｜%s｜%s｜状态：%s"
                    % (
                        PRIORITY_LABELS.get(row["priority"], row["priority"]),
                        row["task_key"],
                        _course_label(row),
                        row["title"],
                        row["status"],
                    )
                )
        lines.append("")
        lines.append("### 当日事件")
        lines.append("")
        day_events = events_by_date.get(day, [])
        if not day_events:
            lines.append("（无事件）")
        else:
            for row in day_events:
                lines.append(
                    "- %s %s（操作者：%s）"
                    % (
                        format_local(row["occurred_at"], "%H:%M"),
                        describe_event(row, row["task_key"], row["task_title"]),
                        row["actor"],
                    )
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def generate_daily(
    conn: sqlite3.Connection,
    path,
    *,
    clock: Optional[Clock] = None,
    db_path: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> Path:
    clock = clock or Clock()
    content = render_daily(
        conn,
        generated_at=to_iso(clock.now()),
        db_path=db_path,
        from_date=from_date,
        to_date=to_date,
    )
    return write_text_atomic(path, content)
