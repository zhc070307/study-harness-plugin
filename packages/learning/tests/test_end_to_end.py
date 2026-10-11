"""End-to-end path required by the implementation brief.

init -> register course -> create task -> criteria -> placement -> evidence ->
complete -> regenerate the views -> integrity/foreign-key checks -> backup and
restore verification.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tests._support import db
from study_agent import db as db_module
from study_agent.models import Clock
from study_agent.service import TaskService
from study_agent.views import generate_daily
from study_agent.views_v3 import generate_view


class EndToEndTests(unittest.TestCase):
    def test_full_workflow(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="study-agent-e2e-") as tmp:
            root = Path(tmp)
            db_path = root / ".study-system" / "data" / "task_memory.sqlite3"
            preview = root / "preview"
            preview.mkdir()

            conn = db.initialize(db_path)
            try:
                service = TaskService(conn, clock=Clock())
                course = service.add_course("COMP 1023", "Introduction to Python Programming")
                self.assertEqual(course.id, "comp-1023")

                task = service.create_task(
                    "阅读 9/4 课堂笔记并记录疑问",
                    course="COMP 1023",
                    status="open",
                    due_at="2026-09-09T15:00:00+08:00",
                    source="DAILY_LOG.md 2026-09-09",
                    criteria=["完整读完笔记", "记录仍然不清楚的问题"],
                )
                self.assertEqual(task.task_key, "T-000001")

                placed = service.set_placement(
                    task.task_key,
                    kind="scheduled",
                    scheduled_date="2026-09-09",
                    priority="must",
                    week_start="2026-09-07",
                )
                self.assertEqual(placed.kind, "scheduled")

                # Cannot complete before criteria and evidence are satisfied.
                try:
                    service.complete(task.task_key)
                    self.fail("completion should have been rejected")
                except Exception:
                    pass

                service.satisfy_criterion(task.task_key, "1")
                service.satisfy_criterion(task.task_key, "2")
                service.add_evidence(
                    task.task_key,
                    "student_confirmation",
                    "学生确认已读完并记录疑问",
                    reference="对话确认 2026-09-09",
                )
                completed = service.complete(task.task_key)
                self.assertEqual(completed.status, "completed")
                self.assertIsNotNone(completed.completed_at)

                # Views are regenerated from the same database.
                today_path = preview / "TODAY.md"
                daily_path = preview / "DAILY_LOG.md"
                generate_view(conn, "today", today_path, target="2026-09-09", clock=Clock(), db_path=str(db_path), revision=1)
                generate_daily(conn, daily_path, clock=Clock(), db_path=str(db_path))
                self.assertIn("系统内部诊断视图", today_path.read_text(encoding="utf-8"))
                self.assertIn("标记完成", daily_path.read_text(encoding="utf-8"))
                # A completed task leaves the board and appears in the history.
                today_text = today_path.read_text(encoding="utf-8")
                self.assertNotIn("完整读完笔记", today_text)
                self.assertNotIn("T-000001", today_text)

                self.assertEqual(db_module.integrity_check(conn), "ok")
                self.assertEqual(db_module.foreign_key_check(conn), [])

                counts = {
                    table: db_module.count_rows(conn, table)
                    for table in (
                        "courses",
                        "tasks",
                        "task_criteria",
                        "task_placements",
                        "task_evidence",
                        "task_events",
                    )
                }
                self.assertEqual(counts["courses"], 1)
                self.assertEqual(counts["tasks"], 1)
                self.assertEqual(counts["task_criteria"], 2)
                # A closed task leaves the open pool, so its placement is gone.
                self.assertEqual(counts["task_placements"], 0)
                self.assertEqual(counts["task_evidence"], 1)
                self.assertEqual(counts["task_events"], 6)

                backup_path = root / ".study-system" / "backups" / "e2e.sqlite3"
                db_module.backup(conn, backup_path)
            finally:
                conn.close()

            restored_path = root / "restored.sqlite3"
            backup_conn = db.connect(backup_path, read_only=True)
            try:
                db_module.backup(backup_conn, restored_path)
                self.assertEqual(db_module.integrity_check(backup_conn), "ok")
            finally:
                backup_conn.close()

            restored = db.connect(restored_path, read_only=True)
            try:
                self.assertEqual(db_module.integrity_check(restored), "ok")
                self.assertEqual(db_module.foreign_key_check(restored), [])
                row = restored.execute("SELECT status FROM tasks WHERE task_key = 'T-000001'").fetchone()
                self.assertEqual(row["status"], "completed")
                event_count = restored.execute("SELECT COUNT(*) AS n FROM task_events").fetchone()["n"]
                self.assertEqual(event_count, 6)
            finally:
                restored.close()


if __name__ == "__main__":
    unittest.main()
