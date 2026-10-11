"""Shared test helpers: path bootstrap, fixed clock and a service test case."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from study_agent import db  # noqa: E402
from study_agent.models import FixedClock  # noqa: E402
from study_agent.service import TaskService  # noqa: E402

# 2026-09-09 12:00:00 HKT
START = datetime(2026, 9, 9, 4, 0, 0, tzinfo=timezone.utc)


class ServiceTestCase(unittest.TestCase):
    """Base case with a fresh file-backed database and deterministic clock."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="study-agent-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.db_path = Path(self.tmpdir) / "task_memory.sqlite3"
        self.conn = db.initialize(self.db_path)
        self.addCleanup(self.conn.close)
        self.clock = FixedClock(START)
        self.service = TaskService(self.conn, clock=self.clock)

    def make_open_task(self, title: str = "Task", course: str = None, **kwargs):
        return self.service.create_task(title, course=course, status="open", **kwargs)

    def count_events(self, task_key: str, event_type: str = None) -> int:
        task = self.service.get_task(task_key)
        if event_type is None:
            return len(self.service.events_for(task.task_key))
        rows = self.conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? AND event_type = ?",
            (task.id, event_type),
        ).fetchone()
        return int(rows["n"])
