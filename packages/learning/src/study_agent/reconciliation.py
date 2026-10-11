"""Build the fact package Codex reasons over, and compute what was missed.

This module is deliberately dumb about meaning. It scans registered course roots,
hashes only files that look new or changed, normalizes Calendar events into a
stable fingerprint, and reports the interval since the last successful
reconciliation. Deciding *what a file is* belongs to Codex, not here.

Directory names are never treated as a source of truth: a class folder called
``2026-09-08-tutorial`` and a file sitting directly in a course root are both
ordinary observations.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import db
from .errors import ConflictError, NotFoundError, ValidationError
from .models import (
    HKT,
    Clock,
    compact_json,
    from_iso,
    local_date_of,
    parse_date,
    to_iso,
    week_end_of,
    week_start_of,
)
from .planning import destination_of
from .service import TaskService
from .uuid7 import uuid7
from .workflows import (
    CourseWorkflowConfig,
    WorkflowRegistry,
    match_course,
    normalize_calendar_title,
)


def is_weekend(day: date) -> bool:
    """Saturday and Sunday are the last overflow position, not the default one."""
    return day.weekday() >= 5


def task_is_executable(
    placement: Optional[Dict[str, Any]],
    *,
    day: date,
    unmet_dependencies: Sequence[str] = (),
) -> bool:
    """Whether a task can actually be worked on now.

    Executable means nothing upstream blocks it: no unsatisfied dependency and
    no ``available_at`` still in the future. A waiting placement is never
    executable, because it is parked on a material, a date or an external event.

    This is a fact the planner can reuse rather than re-derive. It says nothing
    about whether there is room for the task today; that stays a judgement call.
    """
    if unmet_dependencies:
        return False
    if placement is None:
        return True
    if placement.get("kind") == "waiting":
        return False
    available_at = placement.get("available_at")
    if available_at and parse_date(available_at) > day:
        return False
    return True


def calendar_fingerprint(event: Dict[str, Any]) -> Tuple[str, str]:
    """Return ``(source_key, version_key)`` for one Calendar event.

    Apple Calendar exposes no stable event id, so identity comes from the
    normalized fields that describe the occurrence. Title and date alone would
    collide on repeated sessions of the same course.
    """
    calendar = (event.get("calendar") or "").strip()
    title = normalize_calendar_title(event.get("title") or "")
    start = (event.get("start") or "").strip()
    end = (event.get("end") or "").strip()
    location = normalize_calendar_title(event.get("location") or "")
    payload = {
        "calendar": calendar,
        "title": title,
        "start": start,
        "end": end,
        "location": location,
    }
    source_key = "calendar|%s" % hashlib.sha256(
        compact_json(payload).encode("utf-8")
    ).hexdigest()
    version_key = hashlib.sha256(compact_json(payload).encode("utf-8")).hexdigest()
    return source_key, version_key


def file_version_key(path: Path) -> str:
    """Hash file content; callers must re-check size and mtime afterwards."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_ignored(relative: str, name: str, globs: Sequence[str]) -> bool:
    for pattern in globs:
        if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern):
            return True
        # ``**/x`` should also match ``x`` at the root.
        if pattern.startswith("**/") and fnmatch.fnmatch(relative, pattern[3:]):
            return True
    return False


@dataclass
class FileCandidate:
    relative_path: str
    absolute_path: str
    size: int
    mtime_ns: int
    content_hash: str
    stable: bool
    artifact_kind: Optional[str] = None


@dataclass
class ScanResult:
    candidates: List[FileCandidate] = field(default_factory=list)
    unstable: List[str] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=dict)


_ARTIFACT_SUFFIXES = {
    ".pdf": "slides",
    ".ppt": "slides",
    ".pptx": "slides",
    ".key": "slides",
    ".m4a": "recording",
    ".mp3": "recording",
    ".wav": "recording",
    ".mp4": "recording",
    ".mov": "recording",
    ".tex": "note",
    ".md": "note",
    ".txt": "transcript",
    ".docx": "assignment",
    ".doc": "assignment",
    ".xlsx": "assignment",
    ".csv": "result",
    ".py": "other",
    ".webarchive": "other",
}


def guess_artifact_kind(name: str) -> Optional[str]:
    """Best-effort label from the extension; Codex may correct it in its proposal."""
    lowered = name.lower()
    if "原稿" in name or "transcript" in lowered:
        return "transcript"
    if "笔记" in name or "notes" in lowered:
        return "note"
    if "slides" in lowered or "beamer" in lowered or "handout" in lowered:
        return "slides"
    return _ARTIFACT_SUFFIXES.get(Path(name).suffix.lower())


def scan_course_root(
    root: Path,
    ignore_globs: Sequence[str],
    *,
    known_versions: Optional[Dict[str, Tuple[int, int, str]]] = None,
) -> ScanResult:
    """Walk one registered course root and hash only new or changed files.

    ``known_versions`` maps a relative path to ``(size, mtime_ns, content_hash)``
    from previous observations. Files whose size and mtime already match are
    skipped, so a full ``stat`` walk stays cheap. A file whose size or mtime
    changes between the pre-hash and post-hash stat is still being written and is
    reported as unstable instead of being bound to anything.
    """
    result = ScanResult()
    known = known_versions or {}
    base = Path(root)
    if not base.is_dir():
        raise NotFoundError("course root not found: %s" % base)
    seen = 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(
            name for name in dirnames
            if not _is_ignored(
                os.path.relpath(os.path.join(dirpath, name), base).replace(os.sep, "/"),
                name,
                ignore_globs,
            )
        )
        for filename in sorted(filenames):
            absolute = Path(dirpath) / filename
            relative = os.path.relpath(absolute, base).replace(os.sep, "/")
            if _is_ignored(relative, filename, ignore_globs):
                continue
            seen += 1
            try:
                before = absolute.stat()
            except OSError:
                continue
            if not absolute.is_file():
                continue
            previous = known.get(relative)
            if previous is not None and previous[0] == before.st_size and previous[1] == before.st_mtime_ns:
                result.candidates.append(
                    FileCandidate(
                        relative_path=relative,
                        absolute_path=str(absolute),
                        size=before.st_size,
                        mtime_ns=before.st_mtime_ns,
                        content_hash=previous[2],
                        stable=True,
                        artifact_kind=guess_artifact_kind(filename),
                    )
                )
                continue
            try:
                content_hash = file_version_key(absolute)
            except OSError:
                result.unstable.append(relative)
                continue
            try:
                after = absolute.stat()
            except OSError:
                result.unstable.append(relative)
                continue
            if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
                result.unstable.append(relative)
                continue
            result.candidates.append(
                FileCandidate(
                    relative_path=relative,
                    absolute_path=str(absolute),
                    size=after.st_size,
                    mtime_ns=after.st_mtime_ns,
                    content_hash=content_hash,
                    stable=True,
                    artifact_kind=guess_artifact_kind(filename),
                )
            )
    result.stats = {"files_seen": seen, "candidates": len(result.candidates), "unstable": len(result.unstable)}
    return result


class Reconciler:
    """Observation storage, scan orchestration and the prepare context."""

    def __init__(self, conn: sqlite3.Connection, clock: Optional[Clock] = None):
        self.conn = conn
        self.clock = clock or Clock()
        self.tasks = TaskService(conn, clock=self.clock)
        self.registry = WorkflowRegistry(conn, clock=self.clock)
        self.conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------ intervals
    def last_successful(self) -> Optional[str]:
        state = db.planning_state(self.conn)
        return state.get("last_successful_reconciliation_at")

    def missed_interval(self, target: str) -> Tuple[str, str]:
        """Return the ``(start, end)`` interval this run must cover."""
        previous = self.last_successful()
        end = target
        start = previous or to_iso(self.clock.now() - timedelta(days=1))
        return start, end

    def needs_catchup(self, target: str) -> bool:
        previous = self.last_successful()
        if previous is None:
            return True
        return from_iso(previous) < from_iso(target)

    def latest_due_scheduled_run(self, target: str, hour: int = 23) -> str:
        """Return the latest daily run that should already have happened.

        ``freshness`` is a startup gate, not a comparison with wall-clock time.
        Before today's scheduled hour, today's run is still in the future, so
        the required watermark is yesterday at that hour.  At and after the
        scheduled hour, today's scheduled run is due.
        """
        local_now = from_iso(target).astimezone(HKT)
        due = local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if local_now < due:
            due -= timedelta(days=1)
        return to_iso(due)

    # --------------------------------------------------------- observations
    def known_file_versions(self, course_id: str) -> Dict[str, Tuple[int, int, str]]:
        rows = self.conn.execute(
            """
            SELECT source_key, payload_json FROM observations
            WHERE kind = 'file' AND course_id = ?
            """,
            (course_id,),
        ).fetchall()
        known: Dict[str, Tuple[int, int, str]] = {}
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                continue
            size = payload.get("size")
            mtime_ns = payload.get("mtime_ns")
            version = payload.get("content_hash")
            if isinstance(size, int) and isinstance(mtime_ns, int) and typeof_str(version):
                known[row["source_key"]] = (size, mtime_ns, version)
        return known

    def record_observation(
        self,
        *,
        kind: str,
        source_key: str,
        version_key: str,
        course_id: Optional[str] = None,
        binding_status: str = "needs_review",
        session_key: Optional[str] = None,
        artifact_kind: Optional[str] = None,
        occurred_at: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, bool]:
        """Insert or refresh one observation. Returns ``(id, is_new_version)``."""
        stamp = to_iso(self.clock.now())
        existing = self.conn.execute(
            """
            SELECT id FROM observations
            WHERE kind = ? AND source_key = ? AND version_key = ?
            """,
            (kind, source_key, version_key),
        ).fetchone()
        if existing is not None:
            self.conn.execute(
                """
                UPDATE observations
                SET last_seen_at = ?, course_id = COALESCE(?, course_id),
                    binding_status = ?, session_key = COALESCE(?, session_key),
                    artifact_kind = COALESCE(?, artifact_kind)
                WHERE id = ?
                """,
                (stamp, course_id, binding_status, session_key, artifact_kind, existing["id"]),
            )
            return existing["id"], False
        observation_id = str(uuid7(self.clock.now_ms()))
        self.conn.execute(
            """
            INSERT INTO observations (
                id, kind, source_key, version_key, course_id, binding_status, session_key,
                artifact_kind, occurred_at, first_seen_at, last_seen_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation_id,
                kind,
                source_key,
                version_key,
                course_id,
                binding_status,
                session_key,
                artifact_kind,
                occurred_at,
                stamp,
                stamp,
                compact_json(payload or {}),
            ),
        )
        return observation_id, True

    def observation_rows(
        self, *, status: Optional[str] = None, since: Optional[str] = None, limit: int = 500
    ) -> List[sqlite3.Row]:
        clauses: List[str] = []
        params: List[Any] = []
        if status is not None:
            clauses.append("o.binding_status = ?")
            params.append(status)
        if since is not None:
            clauses.append("o.last_seen_at >= ?")
            params.append(since)
        sql = """
            SELECT o.*, c.code AS course_code
            FROM observations o
            LEFT JOIN courses c ON c.id = o.course_id
        """
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY o.last_seen_at DESC, o.id LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def bind_observation(
        self,
        observation_id: str,
        *,
        course: Optional[str] = None,
        status: str = "bound",
        session_key: Optional[str] = None,
        artifact_kind: Optional[str] = None,
    ) -> None:
        course_id = None
        if course is not None:
            course_id = self.tasks._resolve_course_row(course)["id"]
        cursor = self.conn.execute(
            """
            UPDATE observations
            SET course_id = COALESCE(?, course_id), binding_status = ?,
                session_key = COALESCE(?, session_key),
                artifact_kind = COALESCE(?, artifact_kind)
            WHERE id = ?
            """,
            (course_id, status, session_key, artifact_kind, observation_id),
        )
        if not cursor.rowcount:
            raise NotFoundError("observation not found: %s" % observation_id)

    # ------------------------------------------------------------ scanning
    def scan_files(self, configs: Sequence[CourseWorkflowConfig]) -> Dict[str, Any]:
        """Incrementally scan every registered course root."""
        report: Dict[str, Any] = {"courses": {}, "unstable": [], "new_versions": []}
        with self.tasks._write():
            for config in configs:
                course_row = self.conn.execute(
                    "SELECT id FROM courses WHERE code = ?", (config.course,)
                ).fetchone()
                if course_row is None:
                    report["courses"][config.course] = {"error": "course not registered in Task Service"}
                    continue
                course_id = course_row["id"]
                known = self.known_file_versions(course_id)
                root = self._resolve_root(config)
                if root is None:
                    report["courses"][config.course] = {"error": "root directory missing"}
                    continue
                scan = scan_course_root(root, config.ignore_globs, known_versions=known)
                report["courses"][config.course] = scan.stats
                report["unstable"].extend(
                    "%s/%s" % (config.course, item) for item in scan.unstable
                )
                for candidate in scan.candidates:
                    previous = known.get(candidate.relative_path)
                    if previous is not None and previous[2] == candidate.content_hash:
                        source_key = candidate.relative_path
                        existing = self.conn.execute(
                            """
                            SELECT id FROM observations
                            WHERE kind = 'file' AND source_key = ? AND version_key = ?
                            """,
                            (source_key, candidate.content_hash),
                        ).fetchone()
                        if existing is not None:
                            self.conn.execute(
                                "UPDATE observations SET last_seen_at = ? WHERE id = ?",
                                (to_iso(self.clock.now()), existing["id"]),
                            )
                            continue
                    occurred = datetime.fromtimestamp(
                        candidate.mtime_ns / 1_000_000_000, tz=timezone.utc
                    )
                    _, is_new = self.record_observation(
                        kind="file",
                        source_key=candidate.relative_path,
                        version_key=candidate.content_hash,
                        course_id=course_id,
                        binding_status="needs_review",
                        artifact_kind=candidate.artifact_kind,
                        occurred_at=to_iso(occurred),
                        payload={
                            "course": config.course,
                            "relative_path": candidate.relative_path,
                            "absolute_path": candidate.absolute_path,
                            "size": candidate.size,
                            "mtime_ns": candidate.mtime_ns,
                            "content_hash": candidate.content_hash,
                        },
                    )
                    if is_new:
                        report["new_versions"].append(
                            "%s/%s" % (config.course, candidate.relative_path)
                        )
        return report

    def _resolve_root(self, config: CourseWorkflowConfig) -> Optional[Path]:
        path = Path(config.root)
        if path.is_dir():
            return path
        project_root = Path(__file__).resolve().parents[4]
        candidate = project_root / config.root
        return candidate if candidate.is_dir() else None

    def scan_calendar(
        self,
        events: Sequence[Dict[str, Any]],
        configs: Sequence[CourseWorkflowConfig],
    ) -> Dict[str, Any]:
        """Record Calendar observations and report the ones that already ended."""
        report: Dict[str, Any] = {"new": [], "ended_candidates": []}
        now = self.clock.now().astimezone(HKT)
        with self.tasks._write():
            for event in events:
                source_key, version_key = calendar_fingerprint(event)
                config = match_course(configs, event.get("title") or "")
                course_id = None
                if config is not None:
                    row = self.conn.execute(
                        "SELECT id FROM courses WHERE code = ?", (config.course,)
                    ).fetchone()
                    course_id = row["id"] if row else None
                title_key = normalize_calendar_title(event.get("title") or "")
                observation_id, is_new = self.record_observation(
                    kind="calendar",
                    source_key=source_key,
                    version_key=version_key,
                    course_id=course_id,
                    binding_status="bound" if config is not None else "ignored",
                    artifact_kind="other",
                    occurred_at=event.get("start"),
                    payload={
                        "title": event.get("title"),
                        "normalized_title": title_key,
                        "start": event.get("start"),
                        "end": event.get("end"),
                        "all_day": event.get("all_day"),
                        "calendar": event.get("calendar"),
                        "location": event.get("location"),
                        "course": config.course if config else None,
                    },
                )
                if is_new:
                    report["new"].append(event.get("title"))
                if config is None:
                    continue
                ended = _event_has_ended(event, now)
                if ended:
                    report["ended_candidates"].append(
                        {
                            "observation_id": observation_id,
                            "course": config.course,
                            "title": event.get("title"),
                            "start": event.get("start"),
                            "end": event.get("end"),
                            "normalized_title": title_key,
                        }
                    )
        return report

    # ------------------------------------------------------------- context
    def build_context(
        self,
        *,
        mode: str,
        configs: Sequence[CourseWorkflowConfig],
        period_start: str,
        period_end: str,
        query_date: Optional[str] = None,
        calendar_events: Optional[Sequence[Dict[str, Any]]] = None,
        calendar_error: Optional[str] = None,
        trigger: str = "manual",
        run_id: Optional[str] = None,
        base_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Assemble the fact package. Only paths, metadata and short summaries."""
        day = parse_date(query_date) if query_date else self.clock.today()
        week = week_start_of(day)
        tasks = self.tasks.list_tasks(open_only=True)
        placement_rows = {
            row["task_id"]: row
            for row in self.conn.execute("SELECT * FROM task_placements").fetchall()
        }
        open_tasks = []
        for task in tasks:
            placed_row = placement_rows.get(task.id)
            placement = placement_dict(placed_row) if placed_row else None
            destination = None
            if task.status == "open":
                from .models import Placement

                destination = destination_of(
                    Placement.from_row(placed_row) if placed_row else None, day
                )
            dependency_keys = [item.task_key for item in self.tasks.dependencies_for(task.id)]
            unmet = self._unmet_dependencies(task.id)
            scheduled_on = (placement or {}).get("scheduled_date")
            open_tasks.append(
                {
                    "id": task.id,
                    "task_key": task.task_key,
                    "course": self._course_code(task.course_id),
                    "title": task.title,
                    "actor": task.actor,
                    "status": task.status,
                    "due_at": task.due_at,
                    "source": task.source,
                    "destination": destination,
                    "placement": placement,
                    # Facts for the earliest-feasible-date decision, so the
                    # planner does not have to re-derive them.
                    "executable": task_is_executable(
                        placement, day=day, unmet_dependencies=unmet
                    ),
                    "unmet_dependencies": unmet,
                    "available_at": (placement or {}).get("available_at"),
                    "scheduled_on_weekend": bool(
                        scheduled_on and is_weekend(parse_date(scheduled_on))
                    ),
                    "criteria": [
                        {
                            "position": item.position,
                            "text": item.text,
                            "required": bool(item.required),
                            "satisfied": bool(item.satisfied),
                        }
                        for item in self.tasks.criteria_for(task.id)
                    ],
                    "depends_on": dependency_keys,
                    "evidence": [
                        {"kind": item.kind, "summary": item.summary}
                        for item in self.tasks.evidence_for(task.id)
                    ],
                }
            )

        waiting_due = []
        for row in self.conn.execute(
            """
            SELECT p.*, t.task_key, t.title FROM task_placements p
            JOIN tasks t ON t.id = p.task_id
            WHERE p.kind = 'waiting' AND t.status = 'open' AND p.next_check_at <= ?
            ORDER BY p.next_check_at
            """,
            (day.isoformat(),),
        ).fetchall():
            waiting_due.append(
                {
                    "task_key": row["task_key"],
                    "title": row["title"],
                    "waiting_reason": row["waiting_reason"],
                    "next_check_at": row["next_check_at"],
                }
            )

        context: Dict[str, Any] = {
            "generated_at": to_iso(self.clock.now()),
            "mode": mode,
            "trigger": trigger,
            "run_id": run_id,
            "query_date": day.isoformat(),
            "week_start": week.isoformat(),
            "week_end": (week + timedelta(days=6)).isoformat(),
            "period": {"start": period_start, "end": period_end},
            "last_successful_reconciliation_at": self.last_successful(),
            "base_revision": (
                base_revision if base_revision is not None else db.planning_revision(self.conn)
            ),
            "catchup": self.needs_catchup(period_end),
            "open_tasks": open_tasks,
            "waiting_due": waiting_due,
            "unbound_observations": [
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "source_key": row["source_key"],
                    "course": row["course_code"],
                    "artifact_kind": row["artifact_kind"],
                    "occurred_at": row["occurred_at"],
                    "payload": _safe_json(row["payload_json"]),
                }
                for row in self.observation_rows(status="needs_review", limit=200)
            ],
            "recent_observations": [
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "source_key": row["source_key"],
                    "course": row["course_code"],
                    "binding_status": row["binding_status"],
                    "session_key": row["session_key"],
                    "occurred_at": row["occurred_at"],
                }
                for row in self.observation_rows(since=period_start, limit=200)
            ],
            "open_workflow_instances": [
                {
                    "id": row["id"],
                    "course": row["course_code"],
                    "workflow_id": row["workflow_id"],
                    "occurrence_key": row["occurrence_key"],
                }
                for row in self.registry.open_instances()
            ],
            "pending_learning_issues": [
                {
                    "id": row["id"],
                    "course": self._course_code(row["course_id"]),
                    "concept": row["concept"],
                    "issue_type": row["issue_type"],
                    "next_retest_on": row["next_retest_on"],
                }
                for row in self.conn.execute(
                    "SELECT * FROM learning_issues WHERE status = 'pending' ORDER BY next_retest_on"
                ).fetchall()
            ],
            "course_configs": [config.summary() for config in configs],
        }
        if calendar_events is not None:
            context["calendar"] = {
                "window": {"start": period_start, "end": period_end},
                "events": [
                    {
                        "title": event.get("title"),
                        "start": event.get("start"),
                        "end": event.get("end"),
                        "all_day": event.get("all_day"),
                        "calendar": event.get("calendar"),
                        "location": event.get("location"),
                        "matched_course": (
                            match_course(configs, event.get("title") or "").course
                            if match_course(configs, event.get("title") or "")
                            else None
                        ),
                    }
                    for event in calendar_events
                ],
            }
        if calendar_error is not None:
            context["calendar_error"] = calendar_error
        context["forward_days"] = self._forward_days(day, calendar_events)
        return context

    def _forward_days(
        self,
        day: date,
        calendar_events: Optional[Sequence[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        """Per-day facts for the earliest-feasible-date rule.

        For today through the end of the week: the Calendar events in the way,
        what is already placed that day, and whether the day is a weekend.

        Deliberately no estimated minutes, no capacity score, no "maximum items
        per day". The point is to hand the planner the facts so it can decide,
        not to let a fixed algorithm decide for it.
        """
        week_end = week_end_of(day)
        placed: Dict[str, List[Dict[str, Any]]] = {}
        for row in self.conn.execute(
            """
            SELECT t.task_key, t.title, p.priority, p.scheduled_date
            FROM task_placements p JOIN tasks t ON t.id = p.task_id
            WHERE t.status = 'open' AND p.kind = 'scheduled'
              AND p.scheduled_date BETWEEN ? AND ?
            ORDER BY t.task_key
            """,
            (day.isoformat(), week_end.isoformat()),
        ).fetchall():
            placed.setdefault(row["scheduled_date"], []).append(
                {"task_key": row["task_key"], "title": row["title"], "priority": row["priority"]}
            )

        events_by_day: Dict[str, List[Dict[str, Any]]] = {}
        if calendar_events:
            for event in calendar_events:
                start = event.get("start")
                if not start:
                    continue
                event_day = local_date_of(start).isoformat()
                events_by_day.setdefault(event_day, []).append(
                    {
                        "title": event.get("title"),
                        "start": start,
                        "end": event.get("end"),
                        "all_day": event.get("all_day"),
                        "location": event.get("location"),
                    }
                )

        days: List[Dict[str, Any]] = []
        cursor = day
        while cursor <= week_end:
            key = cursor.isoformat()
            days.append(
                {
                    "date": key,
                    "weekday": cursor.strftime("%a"),
                    "is_weekend": is_weekend(cursor),
                    "events": events_by_day.get(key, []),
                    "scheduled_tasks": placed.get(key, []),
                }
            )
            cursor += timedelta(days=1)
        return days

    def _course_code(self, course_id: Optional[str]) -> Optional[str]:
        if not course_id:
            return None
        row = self.conn.execute("SELECT code FROM courses WHERE id = ?", (course_id,)).fetchone()
        return row["code"] if row else None

    def _unmet_dependencies(self, task_id: str) -> List[str]:
        """Keys of dependencies that are not finished yet."""
        rows = self.conn.execute(
            """
            SELECT d.depends_on_task_id, t.task_key, t.status
            FROM task_dependencies d
            JOIN tasks t ON t.id = d.depends_on_task_id
            WHERE d.task_id = ? AND t.status <> 'completed'
            ORDER BY t.task_key
            """,
            (task_id,),
        ).fetchall()
        return [row["task_key"] for row in rows]

    def mark_reconciled(self, *, completed_at: Optional[str] = None,
                        week_start: Optional[str] = None, run_id: Optional[str] = None) -> None:
        stamp = completed_at or to_iso(self.clock.now())
        fields: Dict[str, Any] = {"last_successful_reconciliation_at": stamp, "updated_at": stamp}
        if week_start:
            fields["current_week_start"] = week_start
        if run_id:
            fields["current_week_run_id"] = run_id
        with self.tasks._write():
            db.set_planning_state(self.conn, **fields)


def typeof_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def placement_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "kind": row["kind"],
        "scheduled_date": row["scheduled_date"],
        "priority": row["priority"],
        "waiting_reason": row["waiting_reason"],
        "available_at": row["available_at"],
        "next_check_at": row["next_check_at"],
        "week_start": row["week_start"],
        "commitment": row["commitment"],
        "deferral_count": row["deferral_count"],
        "version": row["version"],
    }


def _safe_json(text: Optional[str]) -> Any:
    try:
        return json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}


def _event_has_ended(event: Dict[str, Any], now: datetime) -> bool:
    end = event.get("end")
    if not end:
        return False
    try:
        return from_iso(end) < now
    except ValueError:
        try:
            local = datetime.fromisoformat(end)
        except ValueError:
            return False
        if local.tzinfo is None:
            local = local.replace(tzinfo=HKT)
        return local < now
