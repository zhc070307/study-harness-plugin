"""View generation: grouping, filtering, blocking reasons, determinism, atomicity."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tests._support import ServiceTestCase
from study_agent.errors import FileError
from study_agent.models import FixedClock, to_iso
from study_agent.views import generate_daily, render_daily

FIXED_TS = "2026-09-09T04:30:00+00:00"


class ViewDatasetTestCase(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.service.add_course("COMP 1023", "Python")
        self.service.add_course("MATH1014", "Calculus")

        self.must = self.make_open_task("must task", course="COMP 1023")
        self.agent_task = self.make_open_task("agent task", course="COMP 1023", actor="agent")
        self.blocked = self.make_open_task("blocked task", course="MATH1014")
        self.blocker = self.make_open_task("blocker task", course="MATH1014")
        self.service.add_dependency(self.blocked.task_key, self.blocker.task_key)
        self.unscheduled = self.make_open_task("unscheduled task", course="COMP 1023")
        self.done = self.make_open_task("done task", course="COMP 1023")
        self.cancelled = self.make_open_task("cancelled task", course="MATH1014")
        self.tomorrow = self.make_open_task("tomorrow task", course="MATH1014")

        self.service.schedule(self.must.task_key, "2026-09-09", "must")
        self.service.schedule(self.agent_task.task_key, "2026-09-09", "should")
        self.service.schedule(self.blocked.task_key, "2026-09-09", "should")
        self.service.schedule(self.done.task_key, "2026-09-09", "should")
        self.service.schedule(self.cancelled.task_key, "2026-09-09", "if_time")
        self.service.schedule(self.tomorrow.task_key, "2026-09-10", "must")

        self.service.add_evidence(self.done.task_key, "student_confirmation", "confirmed")
        self.service.complete(self.done.task_key)
        self.service.cancel(self.cancelled.task_key, reason="dropped")


class DailyViewTests(ViewDatasetTestCase):
    def test_snapshot_and_events(self) -> None:
        content = render_daily(self.conn, generated_at=FIXED_TS, db_path=str(self.db_path))
        self.assertIn("## 2026-09-09", content)
        self.assertIn("### 调度快照", content)
        self.assertIn("### 当日事件", content)
        self.assertIn("创建任务", content)
        self.assertIn("安排到 2026-09-09", content)
        self.assertIn("标记完成", content)
        self.assertIn("取消：dropped", content)
        self.assertIn(self.must.task_key, content)

    def test_deterministic_for_same_database_and_timestamp(self) -> None:
        first = render_daily(self.conn, generated_at=FIXED_TS, db_path=str(self.db_path))
        second = render_daily(self.conn, generated_at=FIXED_TS, db_path=str(self.db_path))
        self.assertEqual(first, second)

    def test_date_range_filter(self) -> None:
        content = render_daily(
            self.conn,
            generated_at=FIXED_TS,
            db_path=str(self.db_path),
            from_date="2026-09-10",
        )
        self.assertNotIn("## 2026-09-09", content)
        self.assertIn("## 2026-09-10", content)


class ViewFileTests(ViewDatasetTestCase):
    def test_daily_write_is_atomic_and_regenerates(self) -> None:
        target = Path(self.tmpdir) / "preview" / "DAILY_LOG.md"
        stamp = FixedClock(datetime(2026, 9, 9, 4, 30, tzinfo=timezone.utc))
        generate_daily(self.conn, target, clock=stamp, db_path=str(self.db_path))
        first = target.read_text(encoding="utf-8")
        self.assertIn("当日事件", first)
        target.unlink()
        self.assertFalse(target.exists())
        generate_daily(self.conn, target, clock=stamp, db_path=str(self.db_path))
        self.assertEqual(target.read_text(encoding="utf-8"), first)

    def test_atomic_write_leaves_no_temp_files(self) -> None:
        target = Path(self.tmpdir) / "preview" / "DAILY_LOG.md"
        generate_daily(self.conn, target, clock=FixedClock(datetime(2026, 9, 9, 4, 30, tzinfo=timezone.utc)), db_path=str(self.db_path))
        leftovers = [name for name in os.listdir(target.parent) if name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_failed_write_does_not_change_database(self) -> None:
        target = Path(self.tmpdir) / "preview" / "as-directory"
        target.mkdir(parents=True)
        before = self.conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        with self.assertRaises(FileError):
            generate_daily(
                self.conn,
                target,
                clock=FixedClock(datetime(2026, 9, 9, 4, 30, tzinfo=timezone.utc)),
                db_path=str(self.db_path),
            )
        after = self.conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
