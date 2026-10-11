"""Codex-centred learning layer: weekly pool, daily selection and event inbox.

This module deliberately stores facts and cursors, not learning policy.  Codex
reads COURSE.md and the actual materials to make decisions; SQLite merely makes
those decisions durable and keeps material arrivals from being forgotten.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import db
from .errors import ConflictError, NotFoundError, ValidationError
from .models import Clock, format_due, parse_date, to_iso, week_end_of, week_start_of, from_iso, HKT
from .service import TaskService
from .uuid7 import uuid7

PRIORITIES = ("must", "should", "if_time")
PRIORITY_LABELS = {"must": "Must Do", "should": "Should Do", "if_time": "If Time Remains"}
ACTOR_LABELS = {"student": "你", "agent": "Codex", "external": "外部"}
ACTOR_SECTION_ORDER = ("student", "agent", "external")
ACTOR_SECTION_LABELS = {
    "student": "你要做的",
    "agent": "Codex 要做的",
    "external": "外部事项",
}
IGNORED_PARTS = {".git", ".study-system", ".DS_Store", "__pycache__", ".pytest_cache"}
BUILD_SUFFIXES = {'.aux', '.log', '.out', '.xdv', '.fls', '.fdb_latexmk', '.pyc', '.gz'}


def _stamp(clock: Clock) -> str:
    return to_iso(clock.now())


def _normalise_code(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value).upper()


def _task_row(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM tasks WHERE id = ? OR task_key = ?", (ref, ref)
    ).fetchone()
    if row is None:
        raise NotFoundError("task not found: %s" % ref)
    return row


def _course_row(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM courses WHERE id = ? OR code = ?", (ref, ref)
    ).fetchone()
    if row is None:
        normalised = _normalise_code(ref)
        rows = conn.execute("SELECT * FROM courses").fetchall()
        row = next((item for item in rows if _normalise_code(item["code"]) == normalised), None)
    if row is None:
        raise NotFoundError("course not found: %s" % ref)
    return row


class LearningService:
    def __init__(self, conn: sqlite3.Connection, clock: Optional[Clock] = None):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.clock = clock or Clock()
        self.tasks = TaskService(conn, clock=self.clock)

    def _write(self):
        return db.atomic(self.conn)

    # ------------------------------------------------------------ weekly pool
    def put_in_pool(
        self, ref: str, week: str, priority: str, rationale: Optional[str] = None
    ) -> Dict[str, Any]:
        start = parse_date(week)
        if start.weekday() != 0:
            raise ValidationError("week must be a Monday: %s" % week)
        if priority not in PRIORITIES:
            raise ValidationError("unknown priority: %s" % priority)
        task = _task_row(self.conn, ref)
        if task["status"] != "open":
            raise ValidationError("only open tasks can enter the weekly pool")
        now = _stamp(self.clock)
        with self._write():
            existing = self.conn.execute(
                "SELECT version, week_start FROM week_pool_items WHERE task_id = ?", (task["id"],)
            ).fetchone()
            if existing:
                if existing['week_start'] != week:
                    self.conn.execute('DELETE FROM today_selections WHERE task_id=?', (task['id'],))
                self.conn.execute(
                    "UPDATE week_pool_items SET week_start = ?, priority = ?, rationale = ?, "
                    "version = version + 1, updated_at = ? WHERE task_id = ?",
                    (week, priority, rationale, now, task["id"]),
                )
            else:
                self.conn.execute(
                    "INSERT INTO week_pool_items "
                    "(task_id, week_start, priority, rationale, version, added_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 1, ?, ?)",
                    (task["id"], week, priority, rationale, now, now),
                )
        return self.pool_item(task["id"])

    def remove_from_pool(self, ref: str) -> None:
        task = _task_row(self.conn, ref)
        with self._write():
            self.conn.execute("DELETE FROM today_selections WHERE task_id = ?", (task["id"],))
            changed = self.conn.execute(
                "DELETE FROM week_pool_items WHERE task_id = ?", (task["id"],)
            ).rowcount
        if not changed:
            raise NotFoundError("task is not in the weekly pool: %s" % ref)

    def pool_item(self, task_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT w.*, t.task_key, t.title, t.actor, t.due_at, c.code AS course
            FROM week_pool_items w JOIN tasks t ON t.id = w.task_id
            LEFT JOIN courses c ON c.id = t.course_id WHERE w.task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("weekly pool item not found")
        return dict(row)

    def list_pool(self, week: str) -> List[Dict[str, Any]]:
        parse_date(week)
        rows = self.conn.execute(
            """
            SELECT w.*, t.task_key, t.title, t.description, t.actor, t.due_at,
                   t.source, c.code AS course
            FROM week_pool_items w JOIN tasks t ON t.id = w.task_id
            LEFT JOIN courses c ON c.id = t.course_id
            WHERE w.week_start = ? AND t.status = 'open'
            ORDER BY CASE w.priority WHEN 'must' THEN 0 WHEN 'should' THEN 1 ELSE 2 END,
                     c.code, w.added_at, t.task_key
            """,
            (week,),
        ).fetchall()
        return [dict(row) for row in rows]

    def import_placements(self, week: str) -> Dict[str, int]:
        """One-time, lossless bridge from the old dated planner into the pool."""
        start = parse_date(week)
        if start.weekday() != 0:
            raise ValidationError("week must be a Monday: %s" % week)
        end = week_end_of(start).isoformat()
        rows = self.conn.execute(
            """
            SELECT t.id, p.priority, p.scheduled_date, p.waiting_reason
            FROM tasks t JOIN task_placements p ON p.task_id = t.id
            WHERE t.status = 'open' AND (
              (p.kind = 'scheduled' AND p.scheduled_date <= ?) OR
              (p.kind = 'waiting' AND p.next_check_at <= ?)
            )
            """,
            (end, end),
        ).fetchall()
        imported = 0
        for row in rows:
            priority = row["priority"] or "should"
            reason = "由旧系统迁入"
            if row["scheduled_date"]:
                reason += "；原安排 %s" % row["scheduled_date"]
            if row["waiting_reason"]:
                reason += "；原等待原因：%s" % row["waiting_reason"]
            self.put_in_pool(row["id"], week, priority, reason)
            imported += 1
        return {"candidates": len(rows), "imported": imported}

    # ---------------------------------------------------------- daily choice
    def select_today(self, refs: Sequence[str], day: str) -> List[Dict[str, Any]]:
        target = parse_date(day)
        if target > self.clock.now().astimezone(HKT).date():
            raise ValidationError('Future-day previews are read-only; today select cannot pre-assign future work')
        week = week_start_of(target).isoformat()
        tasks = [_task_row(self.conn, ref) for ref in refs]
        if len({row["id"] for row in tasks}) != len(tasks):
            raise ValidationError("the same task was selected more than once")
        for row in tasks:
            in_pool = self.conn.execute(
                "SELECT 1 FROM week_pool_items WHERE task_id = ? AND week_start = ?",
                (row["id"], week),
            ).fetchone()
            if row["status"] != "open" or not in_pool:
                raise ValidationError("%s is not an open task in the %s pool" % (row["task_key"], week))
        now = _stamp(self.clock)
        prior_versions = {
            row["task_id"]: int(row["version"])
            for row in self.conn.execute(
                "SELECT task_id, version FROM today_selections WHERE selected_date = ?", (day,)
            ).fetchall()
        }
        with self._write():
            for course_id in {row['course_id'] for row in tasks if row['course_id']}:
                if self.course_state_status(course_id)['needs_review']:
                    raise ConflictError('Course context is stale; reconcile facts then run course state-reviewed: ' + course_id)
            self.conn.execute("DELETE FROM today_selections WHERE selected_date = ?", (day,))
            for position, row in enumerate(tasks, start=1):
                self.conn.execute("DELETE FROM today_selections WHERE task_id = ?", (row["id"],))
                self.conn.execute(
                    "INSERT INTO today_selections "
                    "(task_id, selected_date, position, version, selected_at) VALUES (?, ?, ?, ?, ?)",
                    (row["id"], day, position, prior_versions.get(row["id"], 0) + 1, now),
                )
        return self.list_today(day)

    def clear_today(self, day: str) -> int:
        parse_date(day)
        with self._write():
            return int(self.conn.execute(
                "DELETE FROM today_selections WHERE selected_date = ?", (day,)
            ).rowcount)

    def list_today(self, day: str) -> List[Dict[str, Any]]:
        parse_date(day)
        rows = self.conn.execute(
            """
            SELECT s.*, w.priority, w.version AS pool_version, t.task_key, t.title,
                   t.description, t.actor, t.due_at, c.code AS course
            FROM today_selections s JOIN week_pool_items w ON w.task_id = s.task_id
            JOIN tasks t ON t.id = s.task_id LEFT JOIN courses c ON c.id = t.course_id
            WHERE s.selected_date = ? AND t.status = 'open' ORDER BY s.position
            """,
            (day,),
        ).fetchall()
        return [dict(row) for row in rows]

    # --------------------------------------------------------- material inbox
    def setup_course_context(self, root: Path, course: str) -> Dict[str, Any]:
        """Create only a course's local entry; never invent a method or task."""
        row = _course_row(self.conn, course)
        root = Path(root).resolve()
        entry = self.course_roots(root).get(row['id'])
        folder = entry[1] if entry else root / _normalise_code(row['code'])
        if folder.is_symlink() or not folder.resolve().is_relative_to(root):
            raise ValidationError('Course directory must belong to the selected semester')
        path = folder / 'COURSE.md'
        created = not path.exists()
        for name in ('materials', 'notes', 'practice', 'assignments'):
            (folder / name).mkdir(parents=True, exist_ok=True)
        if created:
            content = (f"# {row['code']} 课程上下文\n\n"
                       "## 课程要求与能力目标\n\n待核对 syllabus、评核、样题与 AI 边界；未知不视为已确认。\n\n"
                       "## 议定学习方法与例外\n\n待助理阅读实际材料并与学生沟通确定；通用习惯作为参考，不复制旧课程状态。\n\n"
                       "## 材料地图与来源作用\n\n尚未核对；材料按用途保存。\n\n"
                       "## 当前课程上下文\n\n### 当前学习状态\n\n尚无授课、阅读、作答或掌握确认。助理应核对实际要求并沟通，不预建未来课次任务；用户可自由阅读、练习或提问。\n")
            with path.open('x', encoding='utf-8') as handle:
                handle.write(content)
        return {'course': row['code'], 'course_md': str(path), 'created': created,
                'method_confirmed': False if created else None}

    def course_roots(self, root: Path) -> Dict[str, Tuple[sqlite3.Row, Path]]:
        folders = {
            _normalise_code(path.parent.name): path.parent
            for path in root.glob("*/COURSE.md")
            if path.is_file()
        }
        result: Dict[str, Tuple[sqlite3.Row, Path]] = {}
        for course in self.conn.execute("SELECT * FROM courses ORDER BY code"):
            folder = folders.get(_normalise_code(course["code"]))
            if folder:
                result[course["id"]] = (course, folder)
        return result

    def _files(self, folder: Path) -> Iterable[Path]:
        for path in folder.rglob("*"):
            if not path.is_file() or any(part in IGNORED_PARTS for part in path.parts):
                continue
            if path.is_symlink() or path.suffix in BUILD_SUFFIXES or path.name.startswith(".") or path.name == "COURSE.md":
                continue
            yield path

    def scan_materials(self, root: Path, *, baseline: bool = False) -> Dict[str, Any]:
        root = root.resolve()
        now = _stamp(self.clock)
        counts = {"seen": 0, "new": 0, "changed": 0, "events": 0}
        with self._write():
            for course_id, (course, folder) in self.course_roots(root).items():
                for path in self._files(folder):
                    stat = path.stat()
                    relative = path.relative_to(folder)
                    source_key = "%s:%s" % (course["code"], relative.as_posix())
                    version_key = hashlib.sha256(
                        ("%s:%d:%d" % (relative.as_posix(), stat.st_size, stat.st_mtime_ns)).encode("utf-8")
                    ).hexdigest()
                    old = self.conn.execute(
                        "SELECT * FROM material_sources WHERE source_key = ?", (source_key,)
                    ).fetchone()
                    # File scans observe changes; classification is explicit.
                    kind = old["artifact_kind"] if old else "other"
                    session = old["session_key"] if old else relative.as_posix()
                    counts["seen"] += 1
                    if old is None:
                        counts["new"] += 1
                        change = "file_added"
                        self.conn.execute(
                            "INSERT INTO material_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (source_key, course_id, relative.as_posix(), kind, session, version_key,
                             stat.st_size, stat.st_mtime_ns, now, now),
                        )
                    elif old["version_key"] != version_key:
                        counts["changed"] += 1
                        change = "file_changed"
                        self.conn.execute(
                            "UPDATE material_sources SET artifact_kind=?, session_key=?, version_key=?, "
                            "size_bytes=?, modified_ns=?, last_seen_at=? WHERE source_key=?",
                            (kind, session, version_key, stat.st_size, stat.st_mtime_ns, now, source_key),
                        )
                    else:
                        self.conn.execute(
                            "UPDATE material_sources SET last_seen_at=? WHERE source_key=?", (now, source_key)
                        )
                        continue
                    if baseline:
                        continue
                    event_type = "transcript_ready" if kind == "transcript" else change
                    payload = json.dumps(
                        {"path": str(path), "relative_path": relative.as_posix(), "size": stat.st_size},
                        ensure_ascii=False, sort_keys=True,
                    )
                    self.conn.execute(
                        "INSERT OR IGNORE INTO material_events "
                        "(id,event_type,source_key,version_key,course_id,session_key,artifact_kind,status,"
                        "occurred_at,detected_at,processed_at,payload_json) "
                        "VALUES (?,?,?,?,?,?,?,'pending',NULL,?,NULL,?)",
                        (str(uuid7(self.clock.now_ms())), event_type, source_key, version_key,
                         course_id, session, kind, now, payload),
                    )
                    counts["events"] += 1
        counts["baseline"] = bool(baseline)
        return counts

    def classify_material(self, event_id: str, kind: str, *, reason: str,
                          session: Optional[str] = None) -> Dict[str, Any]:
        if kind not in {'slides', 'recording', 'transcript', 'note', 'assignment', 'result', 'other'}:
            raise ValidationError('Unsupported material type')
        if not reason.strip() or (session is not None and not session.strip()):
            raise ValidationError('Classification reason and an explicit session must be nonempty')
        with self._write():
            event = self.conn.execute('SELECT * FROM material_events WHERE id=?', (event_id,)).fetchone()
            if event is None or event['status'] not in ('pending', 'processing'):
                raise ValidationError('Classify an open material event')
            source = self.conn.execute('SELECT * FROM material_sources WHERE source_key=?',
                                       (event['source_key'],)).fetchone()
            if source is None or source['version_key'] != event['version_key']:
                raise ConflictError('Classify the current file version')
            session = session.strip() if session is not None else source['session_key']
            payload = json.loads(event['payload_json'])
            payload['classification'] = {'kind': kind, 'reason': reason.strip(), 'session': session}
            event_type = ('transcript_ready' if kind == 'transcript' else
                          'file_changed' if event['event_type'] == 'transcript_ready' else event['event_type'])
            self.conn.execute('UPDATE material_sources SET artifact_kind=?,session_key=? WHERE source_key=?',
                              (kind, session, event['source_key']))
            self.conn.execute('UPDATE material_events SET artifact_kind=?,session_key=?,event_type=?,payload_json=? WHERE id=?',
                              (kind, session, event_type, json.dumps(payload, ensure_ascii=False), event_id))
        return {'id': event_id, 'kind': kind, 'session': session, 'reason': reason.strip()}

    def pending_events(self, course: Optional[str] = None) -> List[Dict[str, Any]]:
        params: List[Any] = []
        clause = "e.status IN ('pending','processing')"
        if course:
            row = _course_row(self.conn, course)
            clause += " AND e.course_id = ?"
            params.append(row["id"])
        rows = self.conn.execute(
            """
            SELECT e.*, c.code AS course FROM material_events e
            LEFT JOIN courses c ON c.id=e.course_id WHERE %s
            ORDER BY e.detected_at, e.id
            """ % clause,
            params,
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
            result.append(item)
        return result

    def close_event(
        self,
        event_id: str,
        *,
        summary: str,
        task_effect: str,
        lecture_notes_path: Optional[str],
        material_map_updated: bool,
        course_state_reviewed: bool,
        requirements_checked: bool,
        operator: str,
        related_event_ids: Optional[Sequence[str]] = None,
        notes_not_required_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        event = self.conn.execute("SELECT * FROM material_events WHERE id=?", (event_id,)).fetchone()
        if event is None:
            raise NotFoundError("material event not found: %s" % event_id)
        if event["status"] == "closed":
            raise ConflictError("material event is already closed")
        if not summary.strip() or not task_effect.strip():
            raise ValidationError("summary and task-effect are required")
        if notes_not_required_reason is not None:
            if not notes_not_required_reason.strip() or lecture_notes_path:
                raise ValidationError('A no-notes receipt requires a nonempty reason and no lecture-notes path')
            summary = 'No new notes required: ' + notes_not_required_reason.strip() + '\n' + summary
        missing = []
        if not material_map_updated:
            missing.append("material map update")
        if not course_state_reviewed:
            missing.append("course state review")
        if not requirements_checked:
            missing.append("requirements check")
        if event["event_type"] == "transcript_ready":
            if not lecture_notes_path and not notes_not_required_reason:
                missing.append("lecture notes")
            elif lecture_notes_path and (not Path(lecture_notes_path).expanduser().is_file() or Path(lecture_notes_path).expanduser().stat().st_size == 0):
                missing.append("existing lecture notes file")
        if missing:
            raise ValidationError("receipt incomplete: %s" % ", ".join(missing))
        now = _stamp(self.clock)
        with self._write():
            # Only explicitly inspected recordings may share this transcript receipt.
            for related_id in set(related_event_ids or []):
                related = self.conn.execute('SELECT * FROM material_events WHERE id=?', (related_id,)).fetchone()
                if (event['event_type'] != 'transcript_ready' or related is None or
                    related['artifact_kind'] != 'recording' or related['course_id'] != event['course_id'] or
                    related['session_key'] != event['session_key']):
                    raise ValidationError('Related event must be an inspected recording from the same course/session')
                self.close_event(related_id, summary=summary, task_effect=task_effect,
                                 lecture_notes_path=lecture_notes_path, material_map_updated=True,
                                 course_state_reviewed=True, requirements_checked=True, operator=operator)
            self.conn.execute(
                "INSERT INTO material_event_receipts "
                "(event_id,summary,lecture_notes_path,material_map_updated,course_state_reviewed,"
                "requirements_checked,task_effect,closed_by,closed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (event_id, summary.strip(), lecture_notes_path, 1, 1, 1,
                 task_effect.strip(), operator, now),
            )
            self.conn.execute(
                "UPDATE material_events SET status='closed', processed_at=? WHERE id=?", (now, event_id)
            )
        return dict(self.conn.execute(
            "SELECT e.*, r.summary, r.lecture_notes_path, r.task_effect, r.closed_at "
            "FROM material_events e JOIN material_event_receipts r ON r.event_id=e.id WHERE e.id=?",
            (event_id,),
        ).fetchone())

    # ------------------------------------------------------------- checkpoints
    def checkpoint(self, key: str, value: Any) -> None:
        now = _stamp(self.clock)
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
        with self._write():
            self.conn.execute(
                "INSERT INTO system_checkpoints(key,value_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (key, payload, now),
            )

    def get_checkpoint(self, key: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM system_checkpoints WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        return {"key": key, "value": json.loads(row["value_json"]), "updated_at": row["updated_at"]}

    def course_state_status(self, course: str, path: Optional[Path] = None) -> Dict[str, Any]:
        row = _course_row(self.conn, course)
        previous = self.get_checkpoint('course.state_review:' + row['id'])
        signatures = {}
        queries = {
            'tasks': 'SELECT * FROM tasks WHERE course_id=? ORDER BY id',
            'criteria': 'SELECT x.* FROM task_criteria x JOIN tasks t ON t.id=x.task_id WHERE t.course_id=? ORDER BY x.id',
            'evidence': 'SELECT x.* FROM task_evidence x JOIN tasks t ON t.id=x.task_id WHERE t.course_id=? ORDER BY x.id',
            'issues': 'SELECT * FROM learning_issues WHERE course_id=? ORDER BY id',
            'assessments': 'SELECT * FROM assessment_attempts WHERE course_id=? ORDER BY id',
            'receipts': 'SELECT r.* FROM material_event_receipts r JOIN material_events e ON e.id=r.event_id WHERE e.course_id=? ORDER BY e.id',
        }
        for name, query in queries.items():
            values = [dict(item) for item in self.conn.execute(query, (row['id'],))]
            signatures[name] = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        digest = hashlib.sha256(json.dumps(signatures, sort_keys=True).encode()).hexdigest()
        old = previous['value'] if previous else {}
        if path is None and old.get('course_path'):
            path = Path(old['course_path'])
        content_hash = hashlib.sha256(path.read_bytes()).hexdigest() if path and path.is_file() else None
        changed = [key for key in signatures if signatures[key] != old.get('signatures', {}).get(key)]
        return {'course': row['code'], 'course_id': row['id'], 'ledger_hash': digest,
                'course_hash': content_hash, 'signatures': signatures,
                'needs_review': not previous or bool(changed) or content_hash != old.get('course_hash'),
                'changed_fact_groups': changed, 'reviewed_at': previous['updated_at'] if previous else None}

    def review_course_state(self, root: Path, course: str, ledger_hash: str,
                            course_hash: str, summary: str) -> Dict[str, Any]:
        row = _course_row(self.conn, course)
        entry = self.course_roots(root).get(row['id'])
        if not entry or not summary.strip():
            raise ValidationError('Existing COURSE.md and a factual review summary are required')
        path = entry[1] / 'COURSE.md'
        with self._write():
            state = self.course_state_status(course, path)
            if state['ledger_hash'] != ledger_hash or state['course_hash'] != course_hash:
                raise ConflictError('Course facts/context changed during review; reread before acknowledging')
            self.checkpoint('course.state_review:' + row['id'], {
                'ledger_hash': ledger_hash, 'course_hash': course_hash, 'course_path': str(path),
                'signatures': state['signatures'], 'summary': summary.strip()})
        return self.course_state_status(course, path)

    def memory_status(self, root: Path) -> Dict[str, Any]:
        path = root / '.study-system/AGENT_MEMORY.md'
        content = path.read_text(encoding='utf-8') if path.exists() else ''
        previous = self.get_checkpoint('memory.last_compaction')
        age = (self.clock.now() - from_iso(previous['updated_at'])).days if previous else None
        reasons = []
        if not previous or age >= 7:
            reasons.append('weekly_review')
        if len(content) > 6000:
            reasons.append('length_limit')
        return {'path': str(path), 'content_hash': hashlib.sha256(content.encode()).hexdigest(),
                'characters': len(content), 'limit': 6000, 'review_due': bool(reasons),
                'reasons': reasons, 'last_review': previous,
                'last_update': self.get_checkpoint('memory.last_update') or self.get_checkpoint('memory.last_review'),
                'compact_before': (self.clock.today() - timedelta(days=7)).isoformat()}

    def rewrite_memory(self, root: Path, content: str, expected_hash: str,
                       review_summary: Optional[str] = None) -> Dict[str, Any]:
        """Codex supplies the semantic rewrite; never distill course goals or facts."""
        if not content.strip() or len(content) > 6000:
            raise ValidationError('Action memory must be nonempty and at most 6000 characters')
        if review_summary is not None and not review_summary.strip():
            raise ValidationError('Semantic compaction requires a factual review summary')
        path = root / '.study-system/AGENT_MEMORY.md'
        # Serialize cooperating writers and reject stale context; archive before replacement.
        with self._write():
            current = self.memory_status(root)
            if current['content_hash'] != expected_hash:
                raise ConflictError('Action memory changed; reread before distilling')
            backup = root / '.study-system/backups/agent-memory' / (current['content_hash'] + '.md')
            if path.exists():
                db.write_text_atomic(backup, path.read_text(encoding='utf-8'))
            db.write_text_atomic(path, content)
            record = {
                'before_hash': expected_hash, 'after_hash': hashlib.sha256(content.encode()).hexdigest(),
                'before_characters': current['characters'], 'after_characters': len(content),
                'backup': str(backup) if backup.exists() else None}
            self.checkpoint('memory.last_update', record)
            if review_summary is not None:
                self.checkpoint('memory.last_compaction', dict(record, summary=review_summary.strip()))
        return self.memory_status(root)

    def calendar_context(self, root: Path, days: int = 8) -> Dict[str, Any]:
        from .workspace import program_root
        tool = program_root(root) / "tools" / "calendar-events" / "calendar-events"
        previous = self.get_checkpoint("calendar.last_snapshot")
        start = self.clock.today()
        if previous:
            start = min(start, from_iso(previous['updated_at']).astimezone(HKT).date())
        end = self.clock.today() + timedelta(days=days)
        try:
            process = subprocess.run(
                [str(tool), "--from", start.isoformat(), "--to", end.isoformat()],
                check=False, capture_output=True, text=True, timeout=30,
            )
            if process.returncode != 0:
                return {"ok": False, "error": process.stderr.strip(), "previous": previous}
            events = json.loads(process.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": str(exc), "previous": previous}
        digest = hashlib.sha256(json.dumps(events, sort_keys=True).encode("utf-8")).hexdigest()
        changed = previous is None or previous["value"].get("digest") != digest
        value = {"digest": digest, "events": events}
        courses = list(self.conn.execute('SELECT id,code FROM courses'))
        now = _stamp(self.clock)
        with self._write():
            for event in events:
                ended = from_iso(event['end'])
                if ended > self.clock.now():
                    continue
                if previous and ended <= from_iso(previous['updated_at']):
                    continue
                title = _normalise_code(event.get('title', ''))
                course = next((row for row in courses if _normalise_code(row['code']) in title), None)
                if course is None:
                    continue
                source = 'calendar:' + hashlib.sha256(json.dumps(
                    [event.get('calendar'), event.get('title'), event.get('start')], ensure_ascii=False).encode()).hexdigest()
                version = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
                self.conn.execute("INSERT OR IGNORE INTO material_events "
                    "(id,event_type,source_key,version_key,course_id,session_key,status,occurred_at,detected_at,payload_json) "
                    "VALUES (?,'calendar_ended',?,?,?,?,'pending',?,?,?)",
                    (str(uuid7(self.clock.now_ms())), source, version, course['id'], event['start'][:10],
                     event['end'], now, json.dumps(event, ensure_ascii=False)))
        self.checkpoint("calendar.last_snapshot", value)
        return {"ok": True, "changed_since_last_check": changed,
                "previous_checked_at": previous["updated_at"] if previous else None,
                "previous_events": previous['value'].get('events', []) if previous else [], "events": events}


def _criteria(conn: sqlite3.Connection, task_id: str) -> List[Dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        "SELECT * FROM task_criteria WHERE task_id=? AND required=1 ORDER BY position", (task_id,)
    )]


def _view_counts(conn: sqlite3.Connection, day: str) -> Dict[str, int]:
    target = parse_date(day)
    week = week_start_of(target).isoformat()
    open_tasks = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='open'"
    ).fetchone()[0]
    in_pool = conn.execute(
        "SELECT COUNT(*) FROM week_pool_items w JOIN tasks t ON t.id=w.task_id "
        "WHERE w.week_start=? AND t.status='open'",
        (week,),
    ).fetchone()[0]
    ready_in_pool = conn.execute(
        """
        SELECT COUNT(*)
        FROM week_pool_items w JOIN tasks t ON t.id=w.task_id
        WHERE w.week_start=? AND t.status='open'
          AND NOT EXISTS (
              SELECT 1 FROM task_dependencies d
              JOIN tasks prerequisite ON prerequisite.id=d.depends_on_task_id
              WHERE d.task_id=t.id AND prerequisite.status!='completed'
          )
        """,
        (week,),
    ).fetchone()[0]
    selected = conn.execute(
        "SELECT COUNT(*) FROM today_selections s JOIN tasks t ON t.id=s.task_id "
        "WHERE s.selected_date=? AND t.status='open'",
        (day,),
    ).fetchone()[0]
    return {
        "open": int(open_tasks),
        "week": int(in_pool),
        "ready_week": int(ready_in_pool),
        "outside_week": int(open_tasks - in_pool),
        "today": int(selected),
    }


def _actor_sections(items: Sequence[Any]) -> List[Tuple[str, str, List[Any]]]:
    """Return non-empty actor buckets in the task interface's stable order."""
    actors = list(ACTOR_SECTION_ORDER)
    actors.extend(sorted({item["actor"] for item in items if item["actor"] not in actors}))
    return [
        (actor, ACTOR_SECTION_LABELS.get(actor, "%s 要做的" % ACTOR_LABELS.get(actor, actor)), bucket)
        for actor in actors
        if (bucket := [item for item in items if item["actor"] == actor])
    ]


def render_working_v4(
    conn: sqlite3.Connection, day: str, *, generated_at: str, db_path: str
) -> Tuple[str, Dict[str, Any]]:
    service = LearningService(conn)
    items = service.list_today(day)
    counts = _view_counts(conn, day)
    lines = [
        "# 今日任务｜%s" % day,
        "",
        "> 本页按课程、执行者分组，组内按重要性排列；不是跨课程的全局执行顺序，也不代表这些任务一天内能做完。全部开放记录 %d 项｜本周范围 %d 项（当前可开始 %d 项）｜本周外保存 %d 项｜今日队列 %d 项。"
        % (counts["open"], counts["week"], counts["ready_week"], counts["outside_week"], counts["today"]),
        "> 全部开放记录包含未来事项与等待项，完整分类见 [BACKLOG.md](BACKLOG.md)。每个任务只有一个完成框；材料、范围与完成标准只是执行信息，不是额外子任务。",
        "",
    ]
    state: Dict[str, Any] = {"kind": "learning-working", "target_date": day,
                             "generated_at": generated_at, "db": db_path, "items": []}
    by_course: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        by_course.setdefault(item["course"] or "跨课程", []).append(item)
    for course in sorted(by_course):
        lines.extend(["## %s" % course, ""])
        for _actor, actor_label, actor_items in _actor_sections(by_course[course]):
            lines.extend(["### %s" % actor_label, ""])
            for priority in PRIORITIES:
                bucket = [item for item in actor_items if item["priority"] == priority]
                if not bucket:
                    continue
                lines.extend(["#### %s" % PRIORITY_LABELS[priority], ""])
                for item in bucket:
                    due = "｜日期 %s" % format_due(item["due_at"]) if item["due_at"] else ""
                    visible = "**【%s】%s%s**" % (ACTOR_LABELS.get(item["actor"], item["actor"]), item["title"], due)
                    lines.append("- [ ] %s" % visible)
                    state["items"].append({"line": len(lines), "kind": "task", "id": item["task_id"],
                                           "text": visible, "version": item["version"], "checked": False,
                                           "selected_at": item['selected_at'],
                                           "criteria_definition": [(c['id'], c['text']) for c in _criteria(conn, item['task_id'])]})
                    if item["description"]:
                        description_lines = [
                            line.strip() for line in item["description"].splitlines() if line.strip()
                        ]
                        structured = description_lines and all(
                            re.match(r"^[^：:\n]{1,10}[：:]\s*\S", line)
                            for line in description_lines
                        )
                        if structured:
                            lines.extend("  - %s" % line for line in description_lines)
                        else:
                            lines.append("  - 说明：%s" % description_lines[0])
                            lines.extend("    %s" % line for line in description_lines[1:])
                    lines.append("")
    if not items:
        lines.extend(["（Codex 尚未从本周任务池选出今日任务。）", ""])
    return "\n".join(lines).rstrip() + "\n", state


def render_week_v4(conn: sqlite3.Connection, week: str) -> str:
    service = LearningService(conn)
    items = service.list_pool(week)
    counts = _view_counts(conn, week)
    end = week_end_of(parse_date(week)).isoformat()
    open_count = int(conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='open'"
    ).fetchone()[0])
    lines = [
        "# 本周任务池｜%s 至 %s" % (week, end),
        "",
        "> 本页是本周范围。全部开放记录 %d 项｜本周范围 %d 项（当前可开始 %d 项）｜本周外保存 %d 项。"
        % (open_count, len(items), counts["ready_week"], counts["outside_week"]),
        "> 全部开放记录还包含未来事项与等待项，见 [BACKLOG.md](BACKLOG.md)",
        "",
    ]
    by_course: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        by_course.setdefault(item["course"] or "跨课程", []).append(item)
    for course in sorted(by_course):
        lines.extend(["## %s" % course, ""])
        for _actor, actor_label, actor_items in _actor_sections(by_course[course]):
            lines.extend(["### %s" % actor_label, ""])
            for priority in PRIORITIES:
                bucket = [item for item in actor_items if item["priority"] == priority]
                if not bucket:
                    continue
                lines.extend(["#### %s" % PRIORITY_LABELS[priority], ""])
                for item in bucket:
                    due = "｜日期 %s" % format_due(item["due_at"]) if item["due_at"] else ""
                    lines.append("- 【%s】%s%s" % (
                        ACTOR_LABELS.get(item["actor"], item["actor"]), item["title"], due
                    ))
                    if item["rationale"]:
                        lines.append("  - 选择理由：%s" % item["rationale"])
                lines.append("")
    if not items:
        lines.extend(["（本周任务池为空。）", ""])
    return "\n".join(lines).rstrip() + "\n"


def render_backlog_v4(conn: sqlite3.Connection, day: str) -> str:
    """Render every open task once, while preserving week/today context."""
    counts = _view_counts(conn, day)
    week = week_start_of(parse_date(day)).isoformat()
    rows = conn.execute(
        """
        SELECT t.id, t.title, t.actor, t.due_at, c.code AS course,
               w.priority,
               CASE WHEN s.task_id IS NULL THEN 0 ELSE 1 END AS selected_today,
               EXISTS (
                   SELECT 1 FROM task_dependencies d
                   JOIN tasks prerequisite ON prerequisite.id=d.depends_on_task_id
                   WHERE d.task_id=t.id AND prerequisite.status!='completed'
               ) AS blocked
        FROM tasks t
        LEFT JOIN courses c ON c.id=t.course_id
        LEFT JOIN week_pool_items w ON w.task_id=t.id AND w.week_start=?
        LEFT JOIN today_selections s ON s.task_id=t.id AND s.selected_date=?
        WHERE t.status='open'
        ORDER BY COALESCE(c.code, 'ZZZZ'),
                 CASE WHEN w.priority='must' THEN 0 WHEN w.priority='should' THEN 1
                      WHEN w.priority='if_time' THEN 2 ELSE 3 END,
                 CASE WHEN t.due_at IS NULL THEN 1 ELSE 0 END, t.due_at, t.created_at
        """,
        (week, day),
    ).fetchall()
    lines = [
        "# 全部开放记录｜%s" % day,
        "",
        "> 这是任务与未来义务的完整库存，不是积压量或当日工作量。全部开放记录 %d 项｜本周范围 %d 项（当前可开始 %d 项）｜本周外保存 %d 项｜今日队列 %d 项。"
        % (counts["open"], counts["week"], counts["ready_week"], counts["outside_week"], counts["today"]),
        "> 「本周外」应仅用于未来时点、等待输入或学生明确暂缓的事项，不自动进入下周；「等待前置」表示现在不可独立开始。已可执行的重要学习任务不得因容量猜测留在本周外。",
        "",
    ]
    by_course: Dict[str, List[sqlite3.Row]] = {}
    for row in rows:
        by_course.setdefault(row["course"] or "跨课程", []).append(row)
    for course in sorted(by_course):
        lines.extend(["## %s" % course, ""])
        for _actor, actor_label, actor_items in _actor_sections(by_course[course]):
            lines.extend(["### %s" % actor_label, ""])
            for priority in (*PRIORITIES, None):
                bucket = [row for row in actor_items if row["priority"] == priority]
                if not bucket:
                    continue
                priority_label = PRIORITY_LABELS[priority] if priority else "本周外"
                lines.extend(["#### %s" % priority_label, ""])
                for row in bucket:
                    tags = []
                    if row["selected_today"]:
                        tags.append("今日")
                    if row["priority"]:
                        tags.append("本周 %s" % PRIORITY_LABELS[row["priority"]])
                    else:
                        tags.append("本周外")
                    if row["blocked"]:
                        tags.append("等待前置")
                    due = "｜日期 %s" % format_due(row["due_at"]) if row["due_at"] else ""
                    lines.append(
                        "- 【%s】%s｜%s%s"
                        % (ACTOR_LABELS.get(row["actor"], row["actor"]), row["title"], "・".join(tags), due)
                    )
                lines.append("")
    if not rows:
        lines.extend(["（没有未完成任务。）", ""])
    return "\n".join(lines).rstrip() + "\n"


def write_learning_view(conn: sqlite3.Connection, kind: str, path: Path, target: str, db_path: str) -> Path:
    now = _stamp(Clock())
    if kind == "working":
        content, state = render_working_v4(conn, target, generated_at=now, db_path=db_path)
        sidecar = path.parent / ".study-system" / "runtime" / "views" / (path.stem + ".state.json")
        db.write_text_atomic(sidecar, json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n")
    elif kind == "week":
        content = render_week_v4(conn, target)
    elif kind == "backlog":
        content = render_backlog_v4(conn, target)
    else:
        raise ValidationError("unsupported learning view: %s" % kind)
    return db.write_text_atomic(path, content)


def sync_working_v4(conn: sqlite3.Connection, path: Path, operator: str = "student") -> Dict[str, Any]:
    sidecar = path.parent / ".study-system" / "runtime" / "views" / (path.stem + ".state.json")
    if not sidecar.exists():
        raise NotFoundError("working-view sidecar not found: %s" % sidecar)
    state = json.loads(sidecar.read_text(encoding="utf-8"))
    if state.get("kind") != "learning-working":
        raise ValidationError("not a Codex learning-system working view")
    lines = path.read_text(encoding="utf-8").splitlines()
    changes = []
    expected_lines = {int(item['line']) for item in state['items']}
    for number, line in enumerate(lines, 1):
        if re.match(r'^\s*- \[[ xX]\]', line) and number not in expected_lines:
            raise ConflictError('unmapped checkbox on line %d' % number)
    for item in state["items"]:
        number = int(item["line"])
        if number < 1 or number > len(lines):
            raise ConflictError("working view line %d is missing; regenerate the view" % number)
        match = re.match(r"^\s*- \[([ xX])\] (.*)$", lines[number - 1])
        if not match or match.group(2).strip() != item["text"]:
            raise ConflictError("working view line %d was structurally edited; regenerate it" % number)
        checked = match.group(1).lower() == "x"
        if item["checked"] and not checked:
            raise ConflictError("completed checkbox on line %d cannot be unticked" % number)
        if checked and not item["checked"]:
            changes.append(item)
    service = TaskService(conn, clock=Clock())
    applied = []
    with db.atomic(conn):
        for item in [entry for entry in changes if entry["kind"] == "criterion"]:
            row = conn.execute("SELECT * FROM task_criteria WHERE id=?", (item["id"],)).fetchone()
            if row and row['satisfied']:
                continue
            if not row or row['text'] != item['text'] or (row["satisfied_at"] or row["created_at"]) != item["version"]:
                raise ConflictError("criterion changed since the view was generated")
            service.satisfy_criterion(item["task_id"], item["id"], operator=operator)
            applied.append({"criterion": item["id"]})
        for item in [entry for entry in changes if entry["kind"] == "task"]:
            selected = conn.execute("SELECT version,selected_at FROM today_selections WHERE task_id=?", (item["id"],)).fetchone()
            task = _task_row(conn, item["id"])
            if task['status'] == 'completed':
                continue
            due = '｜日期 %s' % format_due(task['due_at']) if task['due_at'] else ''
            visible = '**【%s】%s%s**' % (ACTOR_LABELS[task['actor']], task['title'], due)
            if visible != item['text']:
                raise ConflictError('task definition changed since the view was generated')
            if 'criteria_definition' in item and item['criteria_definition'] != [[c['id'], c['text']] for c in _criteria(conn, task['id'])]:
                raise ConflictError('task criteria changed since the view was generated')
            if selected and item.get('selected_at') and selected['selected_at'] != item['selected_at']:
                raise ConflictError('daily selection changed since the view was generated')
            if not selected or int(selected["version"]) != int(item["version"]) or task["status"] != "open":
                raise ConflictError("task changed since the view was generated")
            for criterion in conn.execute(
                "SELECT id FROM task_criteria WHERE task_id=? AND required=1 AND satisfied=0", (task["id"],)
            ).fetchall():
                service.satisfy_criterion(task["task_key"], criterion["id"], operator=operator)
            if task["actor"] == "student":
                service.add_evidence(task["task_key"], "student_confirmation",
                                     "在 TODAY_WORKING.md 中勾选完成", operator=operator)
            service.complete(task["task_key"], note="TODAY_WORKING.md 勾选", operator=operator)
            applied.append({"task": task["task_key"]})
        for task_id in {entry['task_id'] for entry in changes if entry['kind'] == 'criterion'}:
            task = _task_row(conn, task_id)
            required = _criteria(conn, task_id)
            if task['status'] == 'open' and task['actor'] == 'student' and required and all(c['satisfied'] for c in required):
                if service.readiness(task_id)['ready']:
                    service.add_evidence(task_id, 'student_confirmation', '今日清单的全部必需子动作均已勾选', operator=operator)
                    service.complete(task_id, note='全部子动作完成', operator=operator)
                    applied.append({'task': task['task_key']})
    return {"ok": True, "applied": applied}


def build_now_context(conn: sqlite3.Connection, root: Path, day: str, include_calendar: bool = True) -> Dict[str, Any]:
    from .backup_store import maintain_backups
    backup_maintenance = maintain_backups(conn, root)
    working = root / 'TODAY_WORKING.md'
    sidecar = root / '.study-system/runtime/views/TODAY_WORKING.state.json'
    if working.exists() and sidecar.exists():
        if json.loads(sidecar.read_text(encoding='utf-8')).get('kind') == 'learning-working':
            sync_working_v4(conn, working)
        else:
            from .view_sync import sync_file
            if not sync_file(conn, working)['ok']:
                raise ConflictError('Unresolved working-view changes; resolve before planning')
    service = LearningService(conn)
    target = parse_date(day)
    week = week_start_of(target).isoformat()
    scan = service.scan_materials(root)
    memory_path = root / ".study-system" / "AGENT_MEMORY.md"
    memory = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""
    courses = []
    for course_id, (course, folder) in service.course_roots(root).items():
        course_text = (folder / 'COURSE.md').read_text(encoding='utf-8')
        state_section = re.search(r'^### 当前学习状态\s*\n(.*?)(?=^#{1,3} |\Z)', course_text, re.M | re.S)
        counts = conn.execute(
            "SELECT COUNT(*) AS open_tasks, SUM(CASE WHEN w.task_id IS NOT NULL THEN 1 ELSE 0 END) AS in_pool "
            "FROM tasks t LEFT JOIN week_pool_items w ON w.task_id=t.id AND w.week_start=? "
            "WHERE t.course_id=? AND t.status='open'", (week, course_id)
        ).fetchone()
        courses.append({"id": course_id, "code": course["code"], "course_md": str(folder / "COURSE.md"),
                        "state_summary": state_section.group(1).strip() if state_section else '',
                        "state_sync": service.course_state_status(course_id, folder / 'COURSE.md'),
                        "open_tasks": int(counts["open_tasks"] or 0), "in_week_pool": int(counts["in_pool"] or 0)})
    result = {"generated_at": _stamp(Clock()), "date": day, "week_start": week,
              "agent_memory": memory, "memory_maintenance": service.memory_status(root),
              "backup_maintenance": backup_maintenance, "material_scan": scan,
              "pending_material_events": service.pending_events(),
              "week_pool": service.list_pool(week), "today": service.list_today(day), "courses": courses}
    from .delivery import routes
    result['execution_routes'] = routes()
    mapped = service.course_roots(root)
    result['course_setup_needed'] = [dict(row) for row in conn.execute('SELECT id,code,name FROM courses ORDER BY code') if row['id'] not in mapped]
    result['unfinished_previous_weeks'] = [dict(row) for row in conn.execute(
        "SELECT t.task_key,t.title,w.week_start,w.priority FROM week_pool_items w "
        "JOIN tasks t ON t.id=w.task_id WHERE t.status='open' AND w.week_start < ?", (week,))]
    result['material_map_reviews_due'] = []
    for item in courses:
        review = service.get_checkpoint('course.map_review:' + item['id'])
        if review and review['value'].get('review_on') and review['value']['review_on'] <= day:
            result['material_map_reviews_due'].append(review['value'])
    result['open_outside_current_pool'] = [dict(row) for row in conn.execute(
        "SELECT t.task_key,t.title,t.due_at,c.code AS course FROM tasks t "
        "LEFT JOIN courses c ON c.id=t.course_id LEFT JOIN week_pool_items w ON w.task_id=t.id "
        "WHERE t.status='open' AND (w.task_id IS NULL OR w.week_start<>?)", (week,))]
    if include_calendar:
        result["calendar"] = service.calendar_context(root)
        result['pending_material_events'] = service.pending_events()
    return result
