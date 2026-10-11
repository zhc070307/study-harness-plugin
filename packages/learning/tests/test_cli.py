"""CLI surface: text/JSON output, stable exit codes, view and check commands."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path

from tests._support import ServiceTestCase
from study_agent import cli


class CliTestCase(ServiceTestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue() + err.getvalue()

    @property
    def base(self):
        return ["--db", str(self.db_path)]


class CliBasicsTests(CliTestCase):
    def test_explicit_compaction_and_archived_memory_restore(self):
        root = Path(self.tmpdir)
        path = root / '.study-system/AGENT_MEMORY.md'
        path.parent.mkdir()
        path.write_text('Old record\nUnresolved action')
        draft = root / 'draft.md'
        draft.write_text('Unresolved action')
        code, out = self.run_cli(*self.base, '--json', 'memory', 'status', '--root', str(root))
        state = json.loads(out)['data']
        code, out = self.run_cli(*self.base, '--json', 'memory', 'compact', '--root', str(root),
                                 '--from-file', str(draft), '--expected-hash', state['content_hash'],
                                 '--summary', 'Removed obsolete record; preserved unresolved action')
        self.assertEqual(code, 0, out)
        state = json.loads(out)['data']
        self.assertFalse(state['review_due'])
        reference = state['last_review']['value']['backup']
        import os
        os.utime(reference, (0, 0))
        code, out = self.run_cli(*self.base, 'backup', '--maintain', '--root', str(root))
        self.assertEqual(code, 0, out)
        code, out = self.run_cli(*self.base, 'memory', 'restore', '--root', str(root),
                                 '--backup', reference, '--expected-hash', state['content_hash'])
        self.assertEqual(code, 0, out)
        self.assertEqual(path.read_text(), 'Old record\nUnresolved action')

    def test_schema_version_text_and_json(self) -> None:
        code, out = self.run_cli(*self.base, "schema-version")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "4")

        code, out = self.run_cli(*self.base, "--json", "schema-version")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"]["schema_version"], 4)

    def test_init_new_database(self) -> None:
        target = Path(self.tmpdir) / "fresh" / "tm.sqlite3"
        code, out = self.run_cli("--db", str(target), "init")
        self.assertEqual(code, 0)
        self.assertIn("schema v4", out)
        self.assertTrue(target.exists())

    def test_missing_db_is_not_found(self) -> None:
        code, out = self.run_cli("--db", str(Path(self.tmpdir) / "nope.sqlite3"), "task", "list")
        self.assertEqual(code, 3)

    def test_missing_db_option_is_validation_error(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(["--db", "", "task", "list"])
        self.assertEqual(code, 4)

    def test_check_integrity_and_foreign_keys(self) -> None:
        code, out = self.run_cli(*self.base, "check", "integrity")
        self.assertEqual(code, 0)
        self.assertIn("ok", out)
        code, out = self.run_cli(*self.base, "check", "foreign-keys")
        self.assertEqual(code, 0)


class CliTaskTests(CliTestCase):
    def test_course_and_task_text_flow(self) -> None:
        code, out = self.run_cli(*self.base, "course", "add", "--code", "COMP 1023", "--name", "Python")
        self.assertEqual(code, 0)
        self.assertIn("comp-1023", out)

        code, out = self.run_cli(
            *self.base,
            "task",
            "create",
            "--title",
            "read notes",
            "--course",
            "COMP 1023",
            "--status",
            "open",
            "--criterion",
            "read once",
            "--source",
            "COMP 1023/COURSE.md",
        )
        self.assertEqual(code, 0)
        self.assertIn("T-000001", out)

        code, out = self.run_cli(*self.base, "task", "list", "--open")
        self.assertEqual(code, 0)
        self.assertIn("read notes", out)
        self.assertIn("COMP 1023", out)

        code, out = self.run_cli(*self.base, "task", "show", "T-000001")
        self.assertEqual(code, 0)
        self.assertIn("read once", out)

    def test_json_task_create_and_list(self) -> None:
        self.run_cli(*self.base, "course", "add", "--code", "MATH1014", "--name", "Calculus")
        code, out = self.run_cli(
            *self.base,
            "--json",
            "task",
            "create",
            "--title",
            "hw2",
            "--course",
            "MATH1014",
            "--status",
            "open",
            "--due",
            "2026-09-20T23:59:00+08:00",
        )
        self.assertEqual(code, 0)
        created = json.loads(out)["data"]
        self.assertEqual(created["task_key"], "T-000001")
        self.assertEqual(created["due_at"], "2026-09-20T15:59:00+00:00")

        code, out = self.run_cli(*self.base, "--json", "task", "list", "--open")
        self.assertEqual(code, 0)
        data = json.loads(out)["data"]
        self.assertEqual(data[0]["course_code"], "MATH1014")

    def test_update_task_actor(self) -> None:
        self.run_cli(*self.base, "task", "create", "--title", "a", "--status", "open")
        code, out = self.run_cli(*self.base, "--json", "task", "update", "T-000001", "--actor", "agent")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"]["actor"], "agent")

    def test_schedule_and_readiness_commands(self) -> None:
        self.run_cli(*self.base, "task", "create", "--title", "a", "--status", "open")
        self.run_cli(*self.base, "task", "create", "--title", "b", "--status", "open")
        code, out = self.run_cli(*self.base, "task", "schedule", "T-000001", "--date", "2026-09-09", "--priority", "must")
        self.assertEqual(code, 2)
        code, out = self.run_cli(*self.base, 'pool', 'add', 'T-000001', '--week', '2026-09-07', '--priority', 'must')
        self.assertEqual(code, 0)
        code, out = self.run_cli(*self.base, "task", "depend", "add", "T-000002", "--on", "T-000001")
        self.assertEqual(code, 0)
        code, out = self.run_cli(*self.base, "task", "readiness", "T-000002")
        self.assertEqual(code, 0)
        self.assertIn("blocked", out)
        self.assertIn("T-000001", out)

    def test_exit_code_3_unknown_task(self) -> None:
        code, out = self.run_cli(*self.base, "task", "show", "T-999999")
        self.assertEqual(code, 3)

    def test_exit_code_4_complete_without_evidence(self) -> None:
        self.run_cli(*self.base, "task", "create", "--title", "a", "--status", "open")
        code, out = self.run_cli(*self.base, "task", "complete", "T-000001")
        self.assertEqual(code, 4)
        self.assertIn("evidence", out.lower() + out)

    def test_exit_code_5_duplicate_course(self) -> None:
        self.run_cli(*self.base, "course", "add", "--code", "X", "--name", "X")
        code, out = self.run_cli(*self.base, "course", "add", "--code", "X", "--name", "X")
        self.assertEqual(code, 5)

    def test_json_error_document(self) -> None:
        code, out = self.run_cli(*self.base, "--json", "task", "show", "T-000404")
        self.assertEqual(code, 3)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_backup_command(self) -> None:
        self.run_cli(*self.base, "task", "create", "--title", "a", "--status", "open")
        target = Path(self.tmpdir) / "backups" / "copy.sqlite3"
        code, out = self.run_cli(*self.base, "backup", "--out", str(target))
        self.assertEqual(code, 0)
        self.assertTrue(target.exists())

    def test_view_commands(self) -> None:
        self.run_cli(*self.base, "course", "add", "--code", "COMP 1023", "--name", "Python")
        self.run_cli(*self.base, "task", "create", "--title", "a", "--course", "COMP 1023", "--status", "open")
        today = Path(self.tmpdir) / "preview" / "TODAY.md"
        daily = Path(self.tmpdir) / "preview" / "DAILY_LOG.md"
        # Retired planning commands are absent; views come from the undated pool.
        code, out = self.run_cli(
            *self.base, "plan", "audit", "--date", "2026-09-09", "--lenient"
        )
        self.assertEqual(code, 2)
        from study_agent.learning import LearningService
        learning = LearningService(self.conn, self.clock)
        learning.put_in_pool('T-000001', '2026-09-07', 'must')
        root = Path(self.tmpdir)
        folder = root / 'COMP1023'
        folder.mkdir()
        path = folder / 'COURSE.md'
        path.write_text('Current state', encoding='utf-8')
        state = learning.course_state_status('COMP 1023', path)
        learning.review_course_state(root, 'COMP 1023', state['ledger_hash'], state['course_hash'], 'Reviewed')
        learning.select_today(['T-000001'], '2026-09-09')
        code, out = self.run_cli(*self.base, "view", "working", "--date", "2026-09-09", "--out", str(today))
        self.assertEqual(code, 0)
        content = today.read_text(encoding="utf-8")
        self.assertIn("## COMP 1023", content)
        self.assertIn("### Must Do", content)
        self.assertIn("【你】a", content)
        self.assertNotIn("T-000001", content)
        code, out = self.run_cli(*self.base, "view", "daily", "--out", str(daily))
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
