"""Shared helpers for the planning-v3 test modules."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tests._support import ServiceTestCase
from study_agent import db
from study_agent.models import FixedClock
from study_agent.planning import PlanningService
from study_agent.reconcile import ReconcileService
from study_agent.reconciliation import Reconciler
from study_agent.views_v3 import render_today, render_week
from study_agent.workflows import WorkflowRegistry, load_all

HKT = timezone(__import__("datetime").timedelta(hours=8))
# 2026-09-10 20:00 HKT (a Thursday)
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = PROJECT_ROOT / ".study-system" / "config" / "course-workflows"


def configs():
    # Retained assessment tests do not require the retired course workflows.
    return []


class PlanningTestCase(ServiceTestCase):
    """Base case with a fixed 2026-09-10 clock and the real course configs."""

    def setUp(self) -> None:
        super().setUp()
        self.clock = FixedClock(NOW)
        self.conn.close()
        self.tmpdir_path = Path(self.tmpdir)
        self.db_path = self.tmpdir_path / "task_memory.sqlite3"
        self.conn = db.initialize(self.db_path)
        self.addCleanup(self.conn.close)
        from study_agent.service import TaskService

        self.service = TaskService(self.conn, clock=self.clock)
        self.planning = PlanningService(self.conn, clock=self.clock)
        self.reconciler = Reconciler(self.conn, clock=self.clock)
        self.reconcile = ReconcileService(self.conn, clock=self.clock)
        self.registry = WorkflowRegistry(self.conn, clock=self.clock)
        self.configs = configs()

    def place(self, task_ref, date="2026-09-10", priority="must", week_start="2026-09-07", **kwargs):
        return self.planning.apply_batch([
            {
                "op": "set_placement",
                "task": task_ref,
                "kind": "scheduled",
                "scheduled_date": date,
                "priority": priority,
                "week_start": week_start,
                **kwargs,
            }
        ])

    def proposal(self, operations, **extra):
        ctx = self.reconcile.prepare(
            configs=self.configs, mode="manual", scan_files=False,
            query_date="2026-09-10", calendar_events=[],
        )
        payload = {
            "base_revision": ctx["base_revision"],
            "run_key": ctx["run_key"],
            "summary": extra.pop("summary", "test proposal"),
            "operations": operations,
        }
        payload.update(extra)
        return payload
