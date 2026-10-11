"""Command line interface for the Task Service.

Exit codes (stable, documented in README.md):

===== =================================================================
0     success
2     command line usage error (argparse)
3     not found (database, course, task, criterion, dependency)
4     validation / domain rule violation (illegal transition, missing
      completion criteria or completion evidence, malformed date)
5     conflict (duplicate value, already-satisfied criterion, cycle,
      database locked beyond busy_timeout)
6     database error (SQLite failure, integrity or foreign-key failure)
7     file / IO error while writing a view or backup
===== =================================================================
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, List, Optional

from . import __version__, db
from .errors import (
    ConflictError,
    DatabaseError,
    FileError,
    NotFoundError,
    StudyAgentError,
    ValidationError,
)
from .models import (
    ASSESSMENT_SOURCE_KINDS,
    ATTEMPT_TYPES,
    ISSUE_STATUSES,
    ISSUE_RESULTS,
    Clock,
    dataclass_dict,
    dataclass_list,
    format_due,
    format_local,
    parse_date,
    to_iso,
)
from .learning import (
    LearningService,
    build_now_context,
    sync_working_v4,
    write_learning_view,
)
from .service import TaskService
from .view_sync import sync_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="study-agent",
        description="Codex learning system: context, materials, memory and durable tasks.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="path to task_memory.sqlite3 (default: $STUDY_AGENT_DB)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--operator", default="agent", help="actor recorded on events (default: agent)"
    )
    parser.add_argument("--version", action="version", version="study-agent %s" % __version__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database and apply migrations")
    sub.add_parser("migrate", help="apply pending migrations")
    sub.add_parser("schema-version", help="print the current schema version")

    check = sub.add_parser("check", help="database consistency checks")
    check_sub = check.add_subparsers(dest="check_command", required=True)
    check_sub.add_parser("integrity", help="run PRAGMA integrity_check")
    check_sub.add_parser("foreign-keys", help="run PRAGMA foreign_key_check")

    backup = sub.add_parser("backup", help="create a consistent backup with the SQLite Backup API")
    backup_mode = backup.add_mutually_exclusive_group(required=True)
    backup_mode.add_argument("--out", help="destination database path")
    backup_mode.add_argument("--maintain", action="store_true", help="repair, deduplicate and archive context backups")
    backup.add_argument('--root', default='.')

    course = sub.add_parser("course", help="course registry")
    course_sub = course.add_subparsers(dest="course_command", required=True)
    course_add = course_sub.add_parser("add", help="register a course")
    course_add.add_argument("--code", required=True)
    course_add.add_argument("--name", required=True)
    course_add.add_argument("--id", default=None, help="stable course id (default: normalised code)")
    course_sub.add_parser("list", help="list registered courses")
    setup = course_sub.add_parser("setup", help="create the local course entry without choosing a learning method")
    setup.add_argument("ref")
    setup.add_argument("--root", default=".")
    for name in ('state-status', 'state-reviewed'):
        item = course_sub.add_parser(name, help='course context / fact consistency checkpoint')
        item.add_argument('ref')
        item.add_argument('--root', default='.')
        if name == 'state-reviewed':
            item.add_argument('--ledger-hash', required=True)
            item.add_argument('--course-hash', required=True)
            item.add_argument('--summary', required=True)
    review = course_sub.add_parser('map-review', help='schedule a whole-course material-map review')
    review.add_argument('ref')
    review.add_argument('--on', required=True)
    reviewed = course_sub.add_parser('map-reviewed', help='record completed semantic map review')
    reviewed.add_argument('ref')
    reviewed.add_argument('--summary', required=True)
    for name in ('snapshot', 'history', 'restore'):
        item = course_sub.add_parser(name, help='course context version ' + name)
        item.add_argument('ref', help='course code, or snapshot id for restore')
        item.add_argument('--root', default='.')

    task = sub.add_parser("task", help="task operations")
    task_sub = task.add_subparsers(dest="task_command", required=True)

    task_create = task_sub.add_parser("create", help="create a proposed or authorized open task")
    task_create.add_argument("--title", required=True)
    task_create.add_argument("--course", default=None)
    task_create.add_argument("--actor", default="student", choices=["student", "agent", "external"])
    task_create.add_argument("--status", default="proposed", choices=["proposed", "open"])
    task_create.add_argument("--description", default=None)
    task_create.add_argument("--due", default=None, help="YYYY-MM-DD or ISO 8601 with offset")
    task_create.add_argument("--source", default=None)
    task_create.add_argument("--criterion", action="append", default=None)
    task_create.add_argument('--origin-key', default=None, help='stable requirement key; repeated calls return the same task')

    task_list = task_sub.add_parser("list", help="list tasks")
    task_list.add_argument("--status", default=None, choices=["proposed", "open", "completed", "cancelled", "superseded"])
    task_list.add_argument("--course", default=None)
    task_list.add_argument("--actor", default=None, choices=["student", "agent", "external"])
    task_list.add_argument("--due-before", default=None, help="YYYY-MM-DD")
    task_list.add_argument("--open", action="store_true", dest="open_only")

    task_show = task_sub.add_parser("show", help="show one task with criteria, evidence and current pool selection")
    task_show.add_argument("ref")

    task_authorize = task_sub.add_parser("authorize", help="authorize a proposed task")
    task_authorize.add_argument("ref")

    task_update = task_sub.add_parser("update", help="update identity-preserving attributes")
    task_update.add_argument("ref")
    task_update.add_argument("--title", default=None)
    task_update.add_argument("--description", default=None)
    task_update.add_argument("--course", default=None)
    task_update.add_argument("--actor", default=None, choices=["student", "agent", "external"])
    task_update.add_argument("--due", default=None)
    task_update.add_argument("--clear-due", action="store_true")
    task_update.add_argument("--source", default=None)

    criterion = task_sub.add_parser("criterion", help="completion criteria")
    criterion_sub = criterion.add_subparsers(dest="criterion_command", required=True)
    criterion_add = criterion_sub.add_parser("add")
    criterion_add.add_argument("ref")
    criterion_add.add_argument("--text", required=True)
    criterion_add.add_argument("--optional", action="store_true")
    criterion_satisfy = criterion_sub.add_parser("satisfy")
    criterion_satisfy.add_argument("ref")
    criterion_satisfy.add_argument("--criterion", required=True)
    criterion_reopen = criterion_sub.add_parser("reopen")
    criterion_reopen.add_argument("ref")
    criterion_reopen.add_argument("--criterion", required=True)
    criterion_update = criterion_sub.add_parser('update', help='revise an unfinished criterion')
    criterion_update.add_argument('ref')
    criterion_update.add_argument('--criterion', required=True)
    criterion_update.add_argument('--text', required=True)

    depend = task_sub.add_parser("depend", help="task dependencies")
    depend_sub = depend.add_subparsers(dest="depend_command", required=True)
    depend_add = depend_sub.add_parser("add")
    depend_add.add_argument("ref")
    depend_add.add_argument("--on", required=True)
    depend_remove = depend_sub.add_parser("remove")
    depend_remove.add_argument("ref")
    depend_remove.add_argument("--on", required=True)

    evidence = task_sub.add_parser("evidence", help="completion evidence")
    evidence_sub = evidence.add_subparsers(dest="evidence_command", required=True)
    evidence_add = evidence_sub.add_parser("add")
    evidence_add.add_argument("ref")
    evidence_add.add_argument(
        "--kind",
        required=True,
        choices=["student_confirmation", "objective_record", "artifact", "note"],
    )
    evidence_add.add_argument("--summary", required=True)
    evidence_add.add_argument("--reference", default=None)

    task_complete = task_sub.add_parser("complete", help="complete a task (requires criteria and evidence)")
    task_complete.add_argument("ref")
    task_complete.add_argument("--note", default=None)

    task_cancel = task_sub.add_parser("cancel", help="cancel a proposed/open task")
    task_cancel.add_argument("ref")
    task_cancel.add_argument("--reason", default=None)

    task_supersede = task_sub.add_parser("supersede", help="mark a task superseded by another task")
    task_supersede.add_argument("ref")
    task_supersede.add_argument("--by", required=True)
    task_supersede.add_argument("--reason", default=None)

    task_reopen = task_sub.add_parser("reopen", help="reopen a completed/cancelled task")
    task_reopen.add_argument("ref")
    task_reopen.add_argument("--reason", required=True)

    task_readiness = task_sub.add_parser("readiness", help="compute ready/blocked from dependencies")
    task_readiness.add_argument("ref")

    task_events = task_sub.add_parser("events", help="show the event history of one task")
    task_events.add_argument("ref")

    pool = sub.add_parser("pool", help="real undated weekly task pool")
    pool_sub = pool.add_subparsers(dest="pool_command", required=True)
    pool_add = pool_sub.add_parser("add", help="put an open task in a week")
    pool_add.add_argument("ref")
    pool_add.add_argument("--week", required=True, help="Monday, YYYY-MM-DD")
    pool_add.add_argument("--priority", required=True, choices=["must", "should", "if_time"])
    pool_add.add_argument("--rationale", default=None)
    pool_remove = pool_sub.add_parser("remove", help="remove a task from the active pool")
    pool_remove.add_argument("ref")
    pool_list = pool_sub.add_parser("list", help="list one week's pool")
    pool_list.add_argument("--week", required=True)
    pool_import = pool_sub.add_parser("import-placements", help="one-time cutover from old placements")
    pool_import.add_argument("--week", required=True)

    today = sub.add_parser("today", help="Codex daily selection from the weekly pool")
    today_sub = today.add_subparsers(dest="today_command", required=True)
    today_select = today_sub.add_parser("select", help="replace one day's selection in the given order")
    today_select.add_argument("--date", required=True)
    today_select.add_argument("refs", nargs="*")
    today_list = today_sub.add_parser("list")
    today_list.add_argument("--date", required=True)
    today_clear = today_sub.add_parser("clear")
    today_clear.add_argument("--date", required=True)

    material = sub.add_parser("material", help="material map event inbox")
    material_sub = material.add_subparsers(dest="material_command", required=True)
    for name, help_text in (("baseline", "register current files without events"),
                            ("scan", "detect new and changed course material")):
        item = material_sub.add_parser(name, help=help_text)
        item.add_argument("--root", default=".")
    material_pending = material_sub.add_parser("pending", help="list events needing semantic handling")
    material_pending.add_argument("--course", default=None)
    material_inspect = material_sub.add_parser('inspect', help='inspect event and session materials')
    material_inspect.add_argument('event_id')
    classify = material_sub.add_parser('classify', help='record material type after inspecting content')
    classify.add_argument('event_id')
    classify.add_argument('--kind', required=True)
    classify.add_argument('--session', default=None)
    classify.add_argument('--reason', required=True)
    for name in ('claim', 'release'):
        item = material_sub.add_parser(name, help=name + ' semantic handling of an event')
        item.add_argument('event_id')
        if name == 'release':
            item.add_argument('--reason', required=True)
    material_close = material_sub.add_parser("close", help="close an event with a complete receipt")
    material_close.add_argument("event_id")
    material_close.add_argument("--summary", required=True)
    material_close.add_argument("--task-effect", required=True)
    material_close.add_argument("--lecture-notes", default=None)
    material_close.add_argument('--notes-not-required', default=None, help='explicit course/user-authorized reason for processing without new notes')
    material_close.add_argument('--related-event', action='append', default=[], help='inspected recording event in the same session')
    material_close.add_argument("--material-map-updated", action="store_true")
    material_close.add_argument("--course-state-reviewed", action="store_true")
    material_close.add_argument("--requirements-checked", action="store_true")

    memory = sub.add_parser('memory', help='review and distill global action memory only')
    memory_sub = memory.add_subparsers(dest='memory_command', required=True)
    for name in ('status', 'rewrite', 'compact', 'restore'):
        item = memory_sub.add_parser(name)
        item.add_argument('--root', default='.')
        if name in ('rewrite', 'compact'):
            item.add_argument('--from-file', required=True)
            item.add_argument('--expected-hash', required=True)
        if name == 'compact':
            item.add_argument('--summary', required=True)
        if name == 'restore':
            item.add_argument('--backup', required=True)
            item.add_argument('--expected-hash', required=True)

    context = sub.add_parser("context", help="assemble the small first-stage Codex context")
    context_sub = context.add_subparsers(dest="context_command", required=True)
    context_now = context_sub.add_parser("now", help="calendar, changes, pool and action memory")
    context_now.add_argument("--root", default=".")
    context_now.add_argument("--date", required=True)
    context_now.add_argument("--no-calendar", action="store_true")
    context_now.add_argument("--out", default=None)
    context_course = context_sub.add_parser('course', help='second-stage course context')
    context_course.add_argument('course')
    context_course.add_argument('--root', default='.')
    context_course.add_argument('--inventory', action='store_true', help='include live material paths for a full map review')

    config = sub.add_parser("config", help="locate the study system configuration")
    config.add_argument("--root", default=".", help="project root (default: current directory)")

    assessment = sub.add_parser("assessment", help="quiz attempts, learning issues and retests")
    assessment_sub = assessment.add_subparsers(dest="assessment_command", required=True)
    assessment_record = assessment_sub.add_parser("record", help="record one assessment attempt")
    assessment_record.add_argument("--course", required=True)
    assessment_record.add_argument("--source-kind", required=True, choices=list(ASSESSMENT_SOURCE_KINDS))
    assessment_record.add_argument("--summary", required=True)
    assessment_record.add_argument("--session-key", default=None)
    assessment_record.add_argument("--attempt-type", default="initial", choices=list(ATTEMPT_TYPES))
    assessment_record.add_argument("--score", default=None)
    assessment_record.add_argument("--evidence-ref", default=None)
    assessment_record.add_argument("--issue", action="append", default=None,
                                   help="CONCEPT::TYPE (repeatable)")
    assessment_record.add_argument("--retest-gap-days", type=int, default=None)
    assessment_list = assessment_sub.add_parser("list", help="list learning issues")
    assessment_list.add_argument("--course", default=None)
    assessment_list.add_argument("--status", default=None, choices=list(ISSUE_STATUSES))
    assessment_retest = assessment_sub.add_parser("retest", help="record a retest result")
    assessment_retest.add_argument("issue_id")
    assessment_retest.add_argument("--result", required=True, choices=list(ISSUE_RESULTS))
    assessment_retest.add_argument("--task", default=None)
    assessment_retest.add_argument("--note", default=None)
    assessment_retest.add_argument("--evidence-ref", required=True)

    restore = sub.add_parser("restore", help="restore the database from a backup")
    restore.add_argument("--from", dest="source", required=True)

    view = sub.add_parser("view", help="generate Markdown views")
    view_sub = view.add_subparsers(dest="view_command", required=True)
    view_working = view_sub.add_parser(
        "working", help="generate the editable TODAY_WORKING.md for one date"
    )
    view_working.add_argument("--date", required=True, help="YYYY-MM-DD")
    view_working.add_argument("--out", required=True)
    view_week = view_sub.add_parser("week", help="generate WEEK.md for one week")
    view_week.add_argument("--week", required=True, help="Monday of the week")
    view_week.add_argument("--out", required=True)
    view_backlog = view_sub.add_parser("backlog", help="generate complete open-task inventory")
    view_backlog.add_argument("--date", required=True, help="YYYY-MM-DD")
    view_backlog.add_argument("--out", required=True)
    view_sync_cmd = view_sub.add_parser("sync", help="sync tick marks from a view")
    view_sync_cmd.add_argument("--path", required=True)
    return parser


def _fail(args: argparse.Namespace, exc: StudyAgentError) -> int:
    if getattr(args, "json", False):
        payload = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print("error: %s" % exc, file=sys.stderr)
    return exc.exit_code


def _emit(args: argparse.Namespace, data: Any, text: str) -> int:
    if args.json:
        print(json.dumps({"ok": True, "data": data}, ensure_ascii=False, sort_keys=True))
    else:
        print(text)
    return 0


def _db_path(args: argparse.Namespace) -> Path:
    raw = args.db or os.environ.get("STUDY_AGENT_DB")
    if not raw:
        raise ValidationError("--db is required (or set STUDY_AGENT_DB)")
    return Path(raw).expanduser()


def _open_db(args: argparse.Namespace, *, write: bool = True) -> sqlite3.Connection:
    path = _db_path(args)
    if not path.exists():
        raise NotFoundError(
            "database not found: %s; run `study-agent init --db %s` first" % (path, path)
        )
    return db.connect(path, read_only=not write)


def _service(args: argparse.Namespace, *, write: bool = True) -> TaskService:
    return TaskService(_open_db(args, write=write), clock=Clock())


def _render_task_line(task, course_code: Optional[str]) -> str:
    course = "[%s] " % course_code if course_code else ""
    due = " 日期 %s" % format_due(task.due_at) if task.due_at else ""
    return "%s  %-9s %-7s %s%s%s" % (
        task.task_key,
        task.status,
        task.actor,
        course,
        task.title,
        due,
    )


def _run(args: argparse.Namespace) -> int:
    command = args.command
    if command in ('workflow', 'observe', 'reconcile', 'plan', 'freshness'):
        raise ValidationError('Planning v3 entry retired; use context now, material, pool, today and view working')
    if command == 'task' and args.task_command == 'schedule':
        raise ValidationError('Use pool add for weekly work, then today select for a real daily choice')
    if command == 'view' and args.view_command in ('today', 'daily'):
        raise ValidationError('Use view working or view week; legacy daily projections are retired')
    if command == "init":
        path = _db_path(args)
        conn = db.initialize(path)
        version = db.schema_version(conn)
        db.integrity_check(conn)
        db.assert_foreign_keys(conn)
        conn.close()
        return _emit(args, {"db": str(path), "schema_version": version}, "initialized %s (schema v%d)" % (path, version))

    if command == "migrate":
        conn = _open_db(args)
        applied = db.migrate(conn)
        version = db.schema_version(conn)
        conn.close()
        return _emit(args, {"applied": applied, "schema_version": version}, "applied %s (schema v%d)" % (applied or "no migrations", version))

    if command == "schema-version":
        conn = _open_db(args, write=False)
        version = db.schema_version(conn)
        conn.close()
        return _emit(args, {"schema_version": version}, str(version))

    if command == "check":
        conn = _open_db(args, write=False)
        if args.check_command == "integrity":
            result = db.integrity_check(conn)
            conn.close()
            return _emit(args, {"integrity": result}, "integrity: %s" % result)
        violations = db.foreign_key_check(conn)
        conn.close()
        if violations:
            raise DatabaseError("foreign key violations: %s" % (violations,))
        return _emit(args, {"foreign_keys": "ok"}, "foreign keys: ok")

    if command == "backup":
        conn = _open_db(args, write=args.maintain)
        try:
            if args.maintain:
                from .backup_store import maintain_backups
                result = maintain_backups(conn, Path(args.root).expanduser().resolve(), force=True)
                return _emit(args, result, json.dumps(result, ensure_ascii=False, indent=2))
            target = db.backup(conn, args.out)
            return _emit(args, {"backup": str(target)}, "backup written: %s" % target)
        finally:
            conn.close()

    if command == "course":
        conn = _open_db(args, write=args.course_command in ('add', 'snapshot', 'restore', 'map-review', 'map-reviewed', 'state-reviewed'))
        try:
            if args.course_command == 'setup':
                result = LearningService(conn).setup_course_context(Path(args.root), args.ref)
                return _emit(args, result, json.dumps(result, ensure_ascii=False, indent=2))
            if args.course_command in ('state-status', 'state-reviewed'):
                from .learning import _course_row
                service = LearningService(conn)
                root = Path(args.root).resolve()
                course = _course_row(conn, args.ref)
                entry = service.course_roots(root).get(course['id'])
                if not entry:
                    raise NotFoundError('COURSE.md not found')
                result = (service.course_state_status(args.ref, entry[1] / 'COURSE.md')
                          if args.course_command == 'state-status' else
                          service.review_course_state(root, args.ref, args.ledger_hash, args.course_hash, args.summary))
                return _emit(args, result, json.dumps(result, ensure_ascii=False, indent=2))
            if args.course_command in ('map-review', 'map-reviewed'):
                from .learning import _course_row
                service = LearningService(conn)
                course = _course_row(conn, args.ref)
                key = 'course.map_review:' + course['id']
                if args.course_command == 'map-review':
                    parse_date(args.on)
                    value = {'review_on': args.on, 'course': course['code']}
                else:
                    if not args.summary.strip():
                        raise ValidationError('Map review summary must not be empty')
                    value = {'review_on': None, 'course': course['code'], 'summary': args.summary,
                             'reviewed_at': to_iso(Clock().now())}
                service.checkpoint(key, value)
                return _emit(args, value, json.dumps(value, ensure_ascii=False, indent=2))
            if args.course_command in ('snapshot', 'history', 'restore'):
                from .course_memory import snapshot, restore
                from .learning import _course_row
                root = Path(args.root).expanduser().resolve()
                if args.course_command == 'snapshot':
                    result = snapshot(conn, root, args.ref)
                elif args.course_command == 'restore':
                    result = restore(conn, root, args.ref)
                else:
                    course = _course_row(conn, args.ref)
                    result = [dict(row) for row in conn.execute('SELECT * FROM course_context_snapshots WHERE course_id=? ORDER BY created_at DESC', (course['id'],))]
                return _emit(args, result, json.dumps(result, ensure_ascii=False, indent=2))
            service = TaskService(conn, clock=Clock())
            if args.course_command == "add":
                course = service.add_course(args.code, args.name, course_id=args.id)
                return _emit(args, dataclass_dict(course), "registered %s (%s)" % (course.code, course.id))
            courses = service.list_courses()
            text = "\n".join("%s  %s  %s" % (c.id, c.code, c.name) for c in courses) or "(no courses)"
            return _emit(args, dataclass_list(courses), text)
        finally:
            conn.close()

    if command == "task":
        return _run_task(args)

    if command == 'memory':
        conn = _open_db(args, write=args.memory_command != 'status')
        try:
            service = LearningService(conn)
            root = Path(args.root).expanduser().resolve()
            if args.memory_command == 'status':
                result = service.memory_status(root)
            else:
                from .backup_store import read_backup
                content = (read_backup(root, args.backup) if args.memory_command == 'restore'
                           else Path(args.from_file).read_text(encoding='utf-8'))
                result = service.rewrite_memory(root, content, args.expected_hash,
                                               review_summary=args.summary if args.memory_command == 'compact' else None)
            return _emit(args, result, json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            conn.close()

    if command == "pool":
        return _run_pool(args)

    if command == "today":
        return _run_today(args)

    if command == "material":
        return _run_material(args)

    if command == "context":
        return _run_context(args)

    if command == "assessment":
        return _run_assessment(args)

    if command == "config":
        root = Path(args.root).expanduser().resolve()
        from .workspace import program_root
        payload = {"project_root": str(root), "skill": str(program_root(root) / "skills/study-system/SKILL.md"),
                   "memory": str(root / ".study-system/AGENT_MEMORY.md")}
        return _emit(args, payload, json.dumps(payload, ensure_ascii=False, indent=2))

    if command == "restore":
        db.restore(args.source, _db_path(args))
        conn = _open_db(args, write=False)
        try:
            result = db.integrity_check(conn)
        finally:
            conn.close()
        return _emit(args, {"restored": str(_db_path(args)), "integrity": result},
                     "restored %s (integrity: %s)" % (_db_path(args), result))

    if command == "view":
        return _run_view(args)

    raise ValidationError("unknown command: %s" % command)


def _run_task(args: argparse.Namespace) -> int:
    write_commands = {
        "create",
        "authorize",
        "update",
        "criterion",
        "depend",
        "evidence",
        "complete",
        "cancel",
        "supersede",
        "reopen",
    }
    conn = _open_db(args, write=args.task_command in write_commands)
    try:
        service = TaskService(conn, clock=Clock())
        return _dispatch_task(args, service)
    finally:
        conn.close()


def _pool_lines(items: List[Dict[str, Any]]) -> str:
    return "\n".join(
        "%s  %-7s  [%s] %s" % (
            item["task_key"], item["priority"], item.get("course") or "跨课程", item["title"]
        ) for item in items
    ) or "(empty)"


def _run_pool(args: argparse.Namespace) -> int:
    write = args.pool_command in ("add", "remove", "import-placements")
    conn = _open_db(args, write=write)
    try:
        service = LearningService(conn, clock=Clock())
        if args.pool_command == "add":
            item = service.put_in_pool(args.ref, args.week, args.priority, args.rationale)
            return _emit(args, item, "added %s to %s (%s)" % (
                item["task_key"], item["week_start"], item["priority"]
            ))
        if args.pool_command == "remove":
            service.remove_from_pool(args.ref)
            return _emit(args, {"removed": args.ref}, "removed %s from the active pool" % args.ref)
        if args.pool_command == "import-placements":
            result = service.import_placements(args.week)
            return _emit(args, result, "imported %(imported)d old placements" % result)
        items = service.list_pool(args.week)
        return _emit(args, items, _pool_lines(items))
    finally:
        conn.close()


def _run_today(args: argparse.Namespace) -> int:
    write = args.today_command in ("select", "clear")
    conn = _open_db(args, write=write)
    try:
        service = LearningService(conn, clock=Clock())
        if args.today_command == "select":
            items = service.select_today(args.refs, args.date)
            return _emit(args, items, "selected %d task(s) for %s" % (len(items), args.date))
        if args.today_command == "clear":
            count = service.clear_today(args.date)
            return _emit(args, {"cleared": count}, "cleared %d selection(s)" % count)
        items = service.list_today(args.date)
        return _emit(args, items, _pool_lines(items))
    finally:
        conn.close()


def _run_material(args: argparse.Namespace) -> int:
    write = args.material_command in ("baseline", "scan", "close", 'claim', 'release', 'classify')
    conn = _open_db(args, write=write)
    try:
        service = LearningService(conn, clock=Clock())
        if args.material_command == 'classify':
            result = service.classify_material(args.event_id, args.kind, reason=args.reason, session=args.session)
            return _emit(args, result, 'material type recorded')
        if args.material_command in ('claim', 'release'):
            with db.atomic(conn):
                row = conn.execute('SELECT * FROM material_events WHERE id=?', (args.event_id,)).fetchone()
                if row is None or row['status'] not in ('pending', 'processing'):
                    raise ValidationError('event is not pending or processing')
                payload = json.loads(row['payload_json'])
                if args.material_command == 'release':
                    if not args.reason.strip():
                        raise ValidationError('release reason required')
                    payload['blocker'] = args.reason
                status = 'processing' if args.material_command == 'claim' else 'pending'
                conn.execute('UPDATE material_events SET status=?,payload_json=? WHERE id=?',
                             (status, json.dumps(payload, ensure_ascii=False), args.event_id))
            return _emit(args, {'id': args.event_id, 'status': status}, status)
        if args.material_command == 'inspect':
            row = conn.execute('SELECT * FROM material_events WHERE id=?', (args.event_id,)).fetchone()
            if row is None:
                raise NotFoundError('event not found')
            result = dict(row)
            result['session_materials'] = [dict(item) for item in conn.execute(
                'SELECT relative_path,artifact_kind FROM material_sources WHERE course_id=? AND session_key=?',
                (row['course_id'], row['session_key']))]
            result['session_receipts'] = [dict(item) for item in conn.execute(
                'SELECT e.id,e.version_key,e.source_key,r.summary,r.lecture_notes_path,r.task_effect '
                'FROM material_events e JOIN material_event_receipts r ON r.event_id=e.id '
                'WHERE e.course_id=? AND e.session_key=?', (row['course_id'], row['session_key']))]
            return _emit(args, result, json.dumps(result, ensure_ascii=False, indent=2))
        if args.material_command in ("baseline", "scan"):
            result = service.scan_materials(
                Path(args.root).expanduser().resolve(), baseline=args.material_command == "baseline"
            )
            return _emit(args, result, "seen %(seen)d; new %(new)d; changed %(changed)d; events %(events)d" % result)
        if args.material_command == "pending":
            items = service.pending_events(args.course)
            lines = ["%s  %-16s  [%s] %s" % (
                item["id"], item["event_type"], item.get("course") or "?",
                item["payload"].get("relative_path", item["source_key"]),
            ) for item in items]
            return _emit(args, items, "\n".join(lines) or "(no pending material events)")
        result = service.close_event(
            args.event_id, summary=args.summary, task_effect=args.task_effect,
            lecture_notes_path=args.lecture_notes,
            material_map_updated=args.material_map_updated,
            course_state_reviewed=args.course_state_reviewed,
            requirements_checked=args.requirements_checked,
            operator=args.operator,
            related_event_ids=args.related_event,
            notes_not_required_reason=args.notes_not_required,
        )
        return _emit(args, result, "closed material event %s" % args.event_id)
    finally:
        conn.close()


def _run_context(args: argparse.Namespace) -> int:
    conn = _open_db(args, write=True)
    try:
        if args.context_command == 'course':
            from .learning import _course_row
            service = LearningService(conn)
            course = _course_row(conn, args.course)
            root = Path(args.root).expanduser().resolve()
            entry = service.course_roots(root).get(course['id'])
            if not entry:
                raise NotFoundError('COURSE.md not found')
            course_path = entry[1] / 'COURSE.md'
            from .delivery import routes
            result = {'execution_routes': routes(), 'course': dict(course), 'course_path': str(course_path),
                      'state_sync': service.course_state_status(course['id'], course_path),
                      'context': course_path.read_text(encoding='utf-8'),
                      'pending_events': service.pending_events(course['id']),
                      'pending_issues': [dict(row) for row in conn.execute(
                          "SELECT * FROM learning_issues WHERE course_id=? AND status='pending'", (course['id'],))],
                      'material_root': str(entry[1]),
                      'map_review': service.get_checkpoint('course.map_review:' + course['id'])}
            if args.inventory:
                result['materials'] = [str(path.relative_to(entry[1])) for path in service._files(entry[1])]
            return _emit(args, result, json.dumps(result, ensure_ascii=False, indent=2))
        result = build_now_context(
            conn, Path(args.root).expanduser().resolve(), args.date,
            include_calendar=not args.no_calendar,
        )
        if args.out:
            db.write_text_atomic(args.out, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            text = "context written: %s" % args.out
        else:
            text = json.dumps(result, ensure_ascii=False, indent=2)
        return _emit(args, result, text)
    finally:
        conn.close()


def _dispatch_task(args: argparse.Namespace, service: TaskService) -> int:
    operator = args.operator

    if args.task_command == "create":
        learning = LearningService(service.conn)
        with service._write():
            key = 'task.origin:' + args.origin_key if args.origin_key else None
            existing = learning.get_checkpoint(key) if key else None
            if existing:
                task = service.get_task(existing['value']['task_id'])
            else:
                task = service.create_task(
                    args.title, course=args.course, actor=args.actor, status=args.status,
                    description=args.description, due_at=args.due, source=args.source,
                    criteria=args.criterion, operator=operator,
                )
                if key:
                    learning.checkpoint(key, {'task_id': task.id})
        return _emit(args, dataclass_dict(task), "created %s (%s)" % (task.task_key, task.status))

    if args.task_command == "list":
        tasks = service.list_tasks(
            status=args.status,
            course=args.course,
            actor=args.actor,
            due_before=args.due_before,
            open_only=args.open_only,
        )
        codes = {
            row["id"]: row["code"]
            for row in service.conn.execute("SELECT id, code FROM courses").fetchall()
        }
        payload = []
        lines = []
        for task in tasks:
            code = codes.get(task.course_id)
            item = dataclass_dict(task)
            item["course_code"] = code
            payload.append(item)
            lines.append(_render_task_line(task, code))
        return _emit(args, payload, "\n".join(lines) or "(no tasks)")

    if args.task_command == "show":
        detail = service.task_detail(args.ref)
        task = detail["task"]
        readiness = detail["readiness"]
        if not readiness["applicable"]:
            ready_text = "不适用（状态 %s）" % readiness["status"]
        elif readiness["ready"]:
            ready_text = "ready"
        else:
            ready_text = "blocked"
        lines = [
            "%s  %s" % (task["task_key"], task["status"]),
            "标题：%s" % task["title"],
            "课程：%s" % (detail["course"]["code"] if detail["course"] else "跨课程"),
            "执行者：%s" % task["actor"],
            "日期（性质以任务说明与来源为准）：%s" % (format_due(task["due_at"]) or "无"),
            "来源：%s" % (task["source"] or "无"),
            "就绪：%s" % ready_text,
        ]
        if detail["readiness"]["blocked_by"]:
            lines.append("阻塞于：%s" % "、".join(item["task_key"] for item in detail["readiness"]["blocked_by"]))
        if task["description"]:
            lines.append("描述：%s" % task["description"])
        if detail["criteria"]:
            lines.append("内部范围记录（不作为独立子任务）：")
            for criterion in detail["criteria"]:
                state = "已随整项完成确认" if criterion["satisfied"] else "供整项范围核对"
                lines.append("  - %s（%s）" % (criterion["text"], state))
        if detail["dependencies"]:
            lines.append("依赖：%s" % "、".join(item["task_key"] for item in detail["dependencies"]))
        if detail["dependents"]:
            lines.append("被依赖：%s" % "、".join(item["task_key"] for item in detail["dependents"]))
        learning = LearningService(service.conn)
        try:
            pool = learning.pool_item(task['id'])
            lines.append('周任务池：%s（%s）' % (pool['week_start'], pool['priority']))
        except NotFoundError:
            pool = None
        selection = service.conn.execute('SELECT selected_date FROM today_selections WHERE task_id=?', (task['id'],)).fetchone()
        if selection:
            lines.append('今日选择：' + selection['selected_date'])
        detail['legacy_schedules'] = detail.pop('schedules')
        detail['week_pool'] = pool
        detail['today_selection'] = dict(selection) if selection else None
        if detail["evidence"]:
            lines.append("完成证据：")
            for item in detail["evidence"]:
                lines.append("  - %s：%s" % (item["kind"], item["summary"]))
        return _emit(args, detail, "\n".join(lines))

    if args.task_command == "authorize":
        task = service.authorize(args.ref, operator=operator)
        return _emit(args, dataclass_dict(task), "authorized %s" % task.task_key)

    if args.task_command == "update":
        task = service.update_task(
            args.ref,
            title=args.title,
            description=args.description,
            course=args.course,
            actor=args.actor,
            due_at=args.due,
            clear_due=args.clear_due,
            source=args.source,
            operator=operator,
        )
        return _emit(args, dataclass_dict(task), "updated %s" % task.task_key)

    if args.task_command == "criterion":
        if args.criterion_command == 'update':
            if not args.text.strip():
                raise ValidationError('criterion text cannot be empty')
            with service._write():
                task = service.get_task(args.ref)
                row = service._resolve_criterion_row(task.id, args.criterion)
                if task.status != 'open' or row['satisfied']:
                    raise ValidationError('only an unfinished criterion of an open task can be revised')
                service.conn.execute('UPDATE task_criteria SET text=? WHERE id=?', (args.text.strip(), row['id']))
                service._record_event(task.id, 'task_updated', {'criterion_id': row['id'], 'old_text': row['text'], 'new_text': args.text.strip()}, operator)
            return _emit(args, {'criterion_id': row['id']}, 'criterion updated')
        if args.criterion_command == "add":
            criterion = service.add_criterion(args.ref, args.text, required=not args.optional, operator=operator)
            return _emit(args, dataclass_dict(criterion), "criterion %d added" % criterion.position)
        if args.criterion_command == "satisfy":
            criterion = service.satisfy_criterion(args.ref, args.criterion, operator=operator)
            return _emit(args, dataclass_dict(criterion), "criterion satisfied")
        criterion = service.reopen_criterion(args.ref, args.criterion, operator=operator)
        return _emit(args, dataclass_dict(criterion), "criterion reopened")

    if args.task_command == "depend":
        if args.depend_command == "add":
            task, target = service.add_dependency(args.ref, args.on, operator=operator)
            return _emit(args, dataclass_dict(task), "dependency added: %s -> %s" % (task.task_key, target.task_key))
        task = service.remove_dependency(args.ref, args.on, operator=operator)
        return _emit(args, dataclass_dict(task), "dependency removed on %s" % task.task_key)

    if args.task_command == "evidence":
        evidence = service.add_evidence(
            args.ref,
            args.kind,
            args.summary,
            reference=args.reference,
            operator=operator,
        )
        return _emit(args, dataclass_dict(evidence), "evidence added")

    if args.task_command == "complete":
        task = service.complete(args.ref, note=args.note, operator=operator)
        return _emit(args, dataclass_dict(task), "completed %s" % task.task_key)

    if args.task_command == "cancel":
        task = service.cancel(args.ref, reason=args.reason, operator=operator)
        return _emit(args, dataclass_dict(task), "cancelled %s" % task.task_key)

    if args.task_command == "supersede":
        task = service.supersede(args.ref, args.by, reason=args.reason, operator=operator)
        return _emit(args, dataclass_dict(task), "superseded %s" % task.task_key)

    if args.task_command == "reopen":
        task = service.reopen(args.ref, args.reason, operator=operator)
        return _emit(args, dataclass_dict(task), "reopened %s" % task.task_key)

    if args.task_command == "readiness":
        result = service.readiness(args.ref)
        if not result["applicable"]:
            text = "不适用（状态 %s）" % result["status"]
        elif result["ready"]:
            text = "ready"
        else:
            text = "blocked by %s" % "、".join(
                item["task_key"] for item in result["blocked_by"]
            )
        return _emit(args, result, text)

    if args.task_command == "events":
        events = service.events_for(args.ref)
        lines = []
        payload = []
        for event in events:
            row = {
                "id": event.id,
                "task_id": event.task_id,
                "event_type": event.event_type,
                "actor": event.actor,
                "occurred_at": event.occurred_at,
                "payload": event.payload,
            }
            payload.append(row)
            lines.append(
                "%s  %s  %s"
                % (
                    format_local(event.occurred_at),
                    event.event_type,
                    json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
                )
            )
        return _emit(args, payload, "\n".join(lines) or "(no events)")

    raise ValidationError("unknown task command: %s" % args.task_command)


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except StudyAgentError as exc:
        return _fail(args, exc)
    except ValueError as exc:
        return _fail(args, ValidationError(str(exc)))
    except sqlite3.IntegrityError as exc:
        return _fail(args, ConflictError("database constraint rejected the change: %s" % exc))
    except sqlite3.OperationalError as exc:
        return _fail(args, DatabaseError("database operation failed: %s" % exc))
    except sqlite3.Error as exc:
        return _fail(args, DatabaseError("database error: %s" % exc))
    except OSError as exc:
        return _fail(args, FileError(str(exc)))


def _run_assessment(args: argparse.Namespace) -> int:
    write = args.assessment_command in ("record", "retest")
    conn = _open_db(args, write=write)
    try:
        service = TaskService(conn, clock=Clock())
        if args.assessment_command == "record":
            course = service._resolve_course_row(args.course)["id"]
            issues = []
            for item in args.issue or []:
                if "::" not in item:
                    raise ValidationError("--issue must be CONCEPT::TYPE, got %r" % item)
                concept, issue_type = item.split("::", 1)
                issues.append({"concept": concept.strip(), "issue_type": issue_type.strip()})
            from .assessment import AssessmentService

            reconciler = AssessmentService(conn, clock=Clock())
            operation = {
                "op": "record_assessment",
                "course": args.course,
                "source_kind": args.source_kind,
                "session_key": args.session_key,
                "attempt_type": args.attempt_type,
                "occurred_at": to_iso(Clock().now()),
                "score_text": args.score,
                "summary": args.summary,
                "evidence_ref": args.evidence_ref,
                "issues": issues,
                "retest_gap_days": args.retest_gap_days,
            }
            with service._write():
                attempt_id, issue_ids = reconciler.record_assessment(operation)
            return _emit(args, {"attempt_id": attempt_id, "issue_ids": issue_ids},
                         "recorded attempt %s（%d 个问题）" % (attempt_id, len(issue_ids)))
        if args.assessment_command == "retest":
            from .assessment import AssessmentService

            reconciler = AssessmentService(conn, clock=Clock())
            with service._write():
                reconciler.record_issue_result(
                    {"op": "record_issue_result", "issue_id": args.issue_id,
                     "result": args.result, "task": args.task, "note": args.note, "evidence_ref": args.evidence_ref}
                )
            return _emit(args, {"issue_id": args.issue_id, "result": args.result}, "retest recorded")
        clauses, params = [], []
        if args.course:
            clauses.append("li.course_id = ?")
            params.append(service._resolve_course_row(args.course)["id"])
        if args.status:
            clauses.append("li.status = ?")
            params.append(args.status)
        sql = """
            SELECT li.*, c.code AS course_code FROM learning_issues li
            JOIN courses c ON c.id = li.course_id
        """
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY li.status, li.next_retest_on, li.concept"
        rows = conn.execute(sql, params).fetchall()
        payload = [dict(row) for row in rows]
        lines = [
            "%s  %-10s %-12s %s%s" % (
                item["status"], item["course_code"], item["issue_type"], item["concept"],
                "（复测 %s）" % item["next_retest_on"] if item["next_retest_on"] else "",
            )
            for item in payload
        ]
        return _emit(args, payload, "\n".join(lines) or "(no learning issues)")
    finally:
        conn.close()


def _run_view(args: argparse.Namespace) -> int:
    db_path = str(_db_path(args))
    if args.view_command == "sync":
        conn = _open_db(args, write=True)
        try:
            db.assert_foreign_keys(conn)
            target = Path(args.path)
            sidecar = target.parent / ".study-system" / "runtime" / "views" / (target.stem + ".state.json")
            learning_view = False
            if sidecar.exists():
                try:
                    learning_view = json.loads(sidecar.read_text(encoding="utf-8")).get("kind") == "learning-working"
                except (OSError, ValueError):
                    pass
            result = sync_working_v4(conn, target, operator=args.operator) if learning_view else sync_file(conn, target, clock=Clock())
            if not result["ok"]:
                lines = ["视图同步被拒绝，整批未写入："]
                for conflict in result["conflicts"]:
                    lines.append(
                        "  第 %s 行「%s」（%s）：%s → %s"
                        % (conflict["line"], conflict["title"], conflict["id"],
                           conflict["problem"], conflict["fix"])
                    )
                return _emit(args, result, "\n".join(lines))
            count = len(result.get("applied", []))
            return _emit(args, result, "已同步 %d 项" % count) if count else _emit(args, result, "无变化")
        finally:
            conn.close()

    # Existing editable views are synchronized before regeneration.
    conn = _open_db(args, write=True)
    try:
        if args.view_command == "working":
            target_date = parse_date(args.date)
            existing = Path(args.out)
            if existing.name == 'TODAY_WORKING.md' and target_date != Clock().today():
                raise ValidationError('TODAY_WORKING.md must use today in Hong Kong; write historical views or previews to another filename')
            sidecar = existing.parent / ".study-system" / "runtime" / "views" / (existing.stem + ".state.json")
            if existing.exists() and sidecar.exists():
                try:
                    if json.loads(sidecar.read_text(encoding="utf-8")).get("kind") == "learning-working":
                        sync_working_v4(conn, existing, operator=args.operator)
                    else:
                        guard = sync_file(conn, existing, clock=Clock())
                        if not guard['ok']:
                            raise ConflictError('Existing view has unresolved edits; sync before rendering')
                except (OSError, ValueError) as exc:
                    raise ConflictError('Cannot read existing view mapping: %s' % exc)
            path = write_learning_view(conn, "working", existing, args.date, db_path)
            return _emit(args, {"view": str(path)}, "generated %s" % path)
        if args.view_command == "backlog":
            parse_date(args.date)
            existing = Path(args.out)
            path = write_learning_view(conn, "backlog", existing, args.date, db_path)
            return _emit(args, {"view": str(path)}, "generated %s" % path)
        parse_date(args.week)
        existing = Path(args.out)
        path = write_learning_view(conn, "week", existing, args.week, db_path)
        return _emit(args, {"view": str(path)}, "generated %s" % path)
    finally:
        conn.close()



if __name__ == "__main__":
    raise SystemExit(main())
