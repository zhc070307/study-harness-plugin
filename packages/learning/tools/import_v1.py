#!/usr/bin/env python3
"""One-shot v1 open-task import for Task Memory v2 (stage 5/6 tooling).

Reads ``SEMESTER_MANIFEST.json`` and, when ``--db`` is given, imports it into a
fresh database through the Task Service only. It never edits Markdown files and
refuses to touch a database that already contains tasks.

Usage::

    # validate the manifest and print the plan
    python3 tools/import_v1.py --manifest tools/SEMESTER_MANIFEST.json --dry-run

    # import into a fresh temporary database and write the audit review
    python3 tools/import_v1.py --manifest tools/SEMESTER_MANIFEST.json \\
        --db /tmp/task_memory.sqlite3 --review-out ../../docs/task-memory-v2/TASK_MEMORY_V2_IMPORT_REVIEW.md
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, OrderedDict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from study_agent import db  # noqa: E402
from study_agent.errors import StudyAgentError  # noqa: E402
from study_agent.models import Clock, format_due, format_local, to_iso  # noqa: E402
from study_agent.service import TaskService  # noqa: E402

PRIORITY_LABELS = {"must": "Must Do", "should": "Should Do", "if_time": "If Time Remains"}
DUE_STATUS_LABELS = {
    "confirmed": "已确认",
    "date_confirmed_time_tbd": "日期已确认，时间/地点待确认",
    "date_confirmed": "日期已确认",
    "tbd": "TBD / 待确认",
    "not_applicable": "无固定截止",
}
ACTOR_LABELS = {"student": "学生", "agent": "Agent 系统任务", "external": "外部等待"}


def load_manifest(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    keys = [task["key"] for task in manifest["tasks"]]
    duplicates = [key for key, count in Counter(keys).items() if count > 1]
    if duplicates:
        raise SystemExit("duplicate manifest keys: %s" % duplicates)
    known = set(keys)
    for task in manifest["tasks"]:
        for dependency in task.get("depends_on", []):
            if dependency not in known:
                raise SystemExit("task %s references unknown dependency %s" % (task["key"], dependency))
        due_status = task.get("due_status")
        if due_status in ("tbd", "not_applicable") and task.get("due_at"):
            raise SystemExit("task %s has due_status=%s but a due_at value" % (task["key"], due_status))
        if due_status in ("confirmed", "date_confirmed", "date_confirmed_time_tbd") and not task.get("due_at"):
            raise SystemExit("task %s claims a confirmed due status without due_at" % task["key"])
    return manifest


def _course_by_code(manifest: Dict[str, Any], code: Optional[str]) -> Optional[Dict[str, str]]:
    if code is None:
        return None
    for course in manifest["courses"]:
        if course["code"] == code:
            return course
    raise SystemExit("task references unknown course %r" % code)


def import_manifest(db_path: Path, manifest: Dict[str, Any], operator: str) -> Dict[str, Any]:
    if db_path.exists():
        existing_conn = db.connect(db_path, read_only=True)
        try:
            existing_tasks = db.count_rows(existing_conn, "tasks")
        finally:
            existing_conn.close()
        if existing_tasks > 0:
            raise SystemExit(
                "refusing to import: %s already contains tasks "
                "(one-shot import requires a fresh database)" % db_path
            )
    conn = db.initialize(db_path)
    try:
        service = TaskService(conn, clock=Clock())
        for course in manifest["courses"]:
            existing = conn.execute(
                "SELECT 1 FROM courses WHERE code = ?", (course["code"],)
            ).fetchone()
            if existing is None:
                service.add_course(course["code"], course["name"])
        created: "OrderedDict[str, Any]" = OrderedDict()
        for task in manifest["tasks"]:
            course = _course_by_code(manifest, task.get("course"))
            created_task = service.create_task(
                task["title"],
                course=course["code"] if course else None,
                actor=task.get("actor", "student"),
                status="open",
                description=task.get("description"),
                due_at=task.get("due_at"),
                source=task.get("source"),
                criteria=task.get("criteria", []),
                operator=operator,
            )
            created[task["key"]] = created_task
        for task in manifest["tasks"]:
            target = created[task["key"]]
            for dependency_key in task.get("depends_on", []):
                service.add_dependency(
                    target.task_key, created[dependency_key].task_key, operator=operator
                )
        for task in manifest["tasks"]:
            schedule = task.get("schedule")
            if schedule:
                service.schedule(
                    created[task["key"]].task_key,
                    schedule["date"],
                    schedule["priority"],
                    note=schedule.get("note"),
                    operator=operator,
                )
        db.integrity_check(conn)
        db.assert_foreign_keys(conn)
        counts = {
            table: db.count_rows(conn, table)
            for table in (
                "courses",
                "tasks",
                "task_criteria",
                "task_dependencies",
                "task_schedules",
                "task_evidence",
                "task_events",
            )
        }
        return {"created": created, "counts": counts, "db_path": str(db_path)}
    finally:
        conn.close()


def summary_rows(manifest: Dict[str, Any]) -> List[str]:
    by_course: "OrderedDict[str, Counter]" = OrderedDict()
    for task in manifest["tasks"]:
        course = task.get("course") or "跨课程"
        bucket = by_course.setdefault(course, Counter())
        bucket[task.get("actor", "student")] += 1
    lines = ["| 课程/范围 | 学生任务 | Agent 系统任务 | 合计 |", "|---|---|---|---|"]
    totals = Counter()
    for course, bucket in by_course.items():
        row_total = sum(bucket.values())
        totals.update(bucket)
        lines.append(
            "| %s | %d | %d | %d |"
            % (course, bucket.get("student", 0), bucket.get("agent", 0), row_total)
        )
    lines.append(
        "| **合计** | **%d** | **%d** | **%d** |"
        % (totals.get("student", 0), totals.get("agent", 0), sum(totals.values()))
    )
    return lines


def render_review(manifest: Dict[str, Any], imported: Optional[Dict[str, Any]]) -> str:
    generated = manifest.get("generated_at", "")
    created = imported["created"] if imported else {}
    lines: List[str] = []
    lines.append("# Task Memory v2 导入审计清单")
    lines.append("")
    lines.append(
        "> 本文件既是切换前的审计清单，也是切换后的迁移记录。它由 "
        "`.packages/learning/tools/import_v1.py` 从 `.packages/learning/tools/SEMESTER_MANIFEST.json` 生成；"
        "清单只包含当前开放任务、未来已确认承诺和必要依赖，不迁移已关闭历史。"
    )
    lines.append("")
    lines.append("- 清单版本：v%s" % manifest.get("version", "?"))
    lines.append("- 清单整理时间：%s" % (format_local(generated) if generated else "-"))
    lines.append("- 清单任务总数：%d" % len(manifest["tasks"]))
    lines.append("- 基线来源：%s" % manifest.get("baseline", ""))
    if imported:
        lines.append("- **已导入数据库**：`%s`" % imported["db_path"])
        lines.append("- **记录生成时间**：%s" % format_local(to_iso(Clock().now())))
        lines.append("- **任务数量核对**：%s" % json.dumps(imported["counts"], ensure_ascii=False))
    else:
        lines.append("- 导入状态：**尚未导入**（阶段 5 清单；只有切换安全门槛全部通过后才在阶段 6 导入）")
    lines.append("")

    lines.append("## 数量汇总")
    lines.append("")
    lines.extend(summary_rows(manifest))
    lines.append("")

    lines.append("## 任务清单")
    lines.append("")
    for index, task in enumerate(manifest["tasks"], start=1):
        course = task.get("course") or "跨课程"
        actor = task.get("actor", "student")
        created_task = created.get(task["key"])
        lines.append("### %d. %s" % (index, task["title"]))
        lines.append("")
        lines.append("- 临时编号：`%s`" % task["key"])
        lines.append("- 正式 task_key：%s" % ("`%s`" % created_task.task_key if created_task else "（未导入）"))
        lines.append("- 所属课程/范围：%s" % course)
        lines.append("- 执行者：%s（%s）" % (actor, ACTOR_LABELS.get(actor, actor)))
        lines.append("- 来源：%s" % task.get("source", ""))
        lines.append(
            "- deadline：%s（%s）"
            % (
                format_due(task.get("due_at")) if task.get("due_at") else "无",
                DUE_STATUS_LABELS.get(task.get("due_status", ""), task.get("due_status", "")),
            )
        )
        schedule = task.get("schedule")
        if schedule:
            lines.append(
                "- 导入时安排：%s %s"
                % (schedule["date"], PRIORITY_LABELS.get(schedule["priority"], schedule["priority"]))
            )
        else:
            lines.append("- 导入时安排：未安排")
        dependencies = task.get("depends_on", [])
        lines.append(
            "- 依赖：%s" % ("、".join("`%s`" % item for item in dependencies) if dependencies else "无")
        )
        lines.append("- 完成条件：")
        for criterion in task.get("criteria", []):
            lines.append("  - %s" % criterion)
        lines.append("- 需要核对的歧义/待确认：%s" % (task.get("ambiguity") or "无"))
        lines.append("- 描述：%s" % task.get("description", ""))
        lines.append("")

    lines.append("## 去重、合并与拆分决定")
    lines.append("")
    for item in manifest.get("dedup_decisions", []):
        lines.append("- **%s**：%s" % (item["decision"], item["detail"]))
    lines.append("")

    lines.append("## 未导入的已排除项目")
    lines.append("")
    for item in manifest.get("excluded", []):
        lines.append("- **%s**：%s" % (item["item"], item["reason"]))
    lines.append("")

    ambiguities = [task for task in manifest["tasks"] if task.get("ambiguity")]
    lines.append("## 待核对与 TBD 汇总")
    lines.append("")
    if not ambiguities:
        lines.append("（无）")
    else:
        for task in ambiguities:
            lines.append("- `%s`｜%s：%s" % (task["key"], task["title"], task["ambiguity"]))
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def load_task_keys(db_path: Path) -> Dict[str, str]:
    """Map manifest titles to task keys from an already-imported database."""
    conn = db.connect(db_path, read_only=True)
    try:
        rows = conn.execute("SELECT title, task_key FROM tasks").fetchall()
    finally:
        conn.close()
    mapping: Dict[str, str] = {}
    for row in rows:
        if row["title"] in mapping:
            raise SystemExit("cannot map titles: duplicate title %r" % row["title"])
        mapping[row["title"]] = row["task_key"]
    return mapping


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--db", default=None)
    parser.add_argument("--import", dest="do_import", action="store_true")
    parser.add_argument("--review-out", default=None)
    parser.add_argument("--db-label", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--operator", default="import-v1")
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest).resolve()
    manifest = load_manifest(manifest_path)
    imported = None
    if args.do_import:
        if not args.db:
            raise SystemExit("--import requires --db")
        imported = import_manifest(Path(args.db).resolve(), manifest, args.operator)
        if args.db_label:
            imported["db_path"] = args.db_label
    elif args.review_out and args.db and Path(args.db).exists():
        title_keys = load_task_keys(Path(args.db))
        created = {}
        for task in manifest["tasks"]:
            if task["title"] in title_keys:
                created[task["key"]] = SimpleNamespace(task_key=title_keys[task["title"]])
        conn = db.connect(Path(args.db), read_only=True)
        try:
            counts = {
                table: db.count_rows(conn, table)
                for table in (
                    "courses",
                    "tasks",
                    "task_criteria",
                    "task_dependencies",
                    "task_schedules",
                    "task_evidence",
                    "task_events",
                )
            }
        finally:
            conn.close()
        imported = {
            "created": created,
            "counts": counts,
            "db_path": args.db_label or str(Path(args.db).resolve()),
        }
    if args.review_out:
        review = render_review(manifest, imported)
        db.write_text_atomic(args.review_out, review)
        print("review written: %s" % args.review_out)
    if imported and args.do_import:
        print("imported %d tasks into %s" % (len(imported["created"]), imported["db_path"]))
        print("counts: %s" % json.dumps(imported["counts"], ensure_ascii=False))
    elif imported:
        print("review rendered from existing database: %s" % imported["db_path"])
    else:
        print("manifest valid: %d tasks, %d courses" % (len(manifest["tasks"]), len(manifest["courses"])))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StudyAgentError as exc:
        print("error: %s" % exc, file=sys.stderr)
        raise SystemExit(exc.exit_code)
