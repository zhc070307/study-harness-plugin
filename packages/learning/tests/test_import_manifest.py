"""The v1 import manifest is valid and imports cleanly through the Task Service."""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

from tests._support import ServiceTestCase, db
from study_agent import db as db_module

TOOLS = Path(__file__).resolve().parents[1] / "tools"



def _load_tool():
    spec = importlib.util.spec_from_file_location("import_v1", TOOLS / "import_v1.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ImportManifestTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.manifest = Path(self.tmpdir) / 'manifest.json'
        self.manifest.write_text(json.dumps({
            'version': 1, 'courses': [{'code': 'TEST101', 'name': 'Import fixture'}],
            'tasks': [
                {'key': 'input', 'course': 'TEST101', 'actor': 'agent', 'title': 'Prepare input', 'criteria': ['Input ready']},
                {'key': 'practice', 'course': 'TEST101', 'title': 'Independent attempt', 'criteria': ['Attempt saved'], 'depends_on': ['input'], 'schedule': {'date': '2026-09-09', 'priority': 'should'}},
                {'key': 'external', 'title': 'External requirement', 'criteria': []}
            ]
        }))

    def test_manifest_is_well_formed(self) -> None:
        module = _load_tool()
        manifest = module.load_manifest(self.manifest)
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(len(manifest["tasks"]), 3)
        keys = [task["key"] for task in manifest["tasks"]]
        self.assertEqual(len(keys), len(set(keys)))
        courses = {course["code"] for course in manifest["courses"]}
        for task in manifest["tasks"]:
            if task.get("course"):
                self.assertIn(task["course"], courses)
            for dependency in task.get("depends_on", []):
                self.assertIn(dependency, keys)

    def test_manifest_imports_and_matches_counts(self) -> None:
        module = _load_tool()
        manifest = module.load_manifest(self.manifest)
        target = Path(self.tmpdir) / "imported.sqlite3"
        result = module.import_manifest(target, manifest, "test-import")
        self.assertEqual(len(result["created"]), len(manifest["tasks"]))

        conn = db.connect(target, read_only=True)
        try:
            self.assertEqual(db_module.integrity_check(conn), "ok")
            self.assertEqual(db_module.foreign_key_check(conn), [])
            self.assertEqual(db_module.count_rows(conn, "tasks"), len(manifest["tasks"]))
            self.assertEqual(db_module.count_rows(conn, "courses"), len(manifest["courses"]))
            expected_criteria = sum(len(task.get("criteria", [])) for task in manifest["tasks"])
            self.assertEqual(db_module.count_rows(conn, "task_criteria"), expected_criteria)
            expected_dependencies = sum(len(task.get("depends_on", [])) for task in manifest["tasks"])
            self.assertEqual(db_module.count_rows(conn, "task_dependencies"), expected_dependencies)
            expected_schedules = sum(1 for task in manifest["tasks"] if task.get("schedule"))
            self.assertEqual(db_module.count_rows(conn, "task_schedules"), expected_schedules)
            statuses = {
                row["status"] for row in conn.execute("SELECT DISTINCT status FROM tasks").fetchall()
            }
            self.assertEqual(statuses, {"open"})
        finally:
            conn.close()

    def test_import_refuses_non_empty_database(self) -> None:
        module = _load_tool()
        manifest = module.load_manifest(self.manifest)
        target = Path(self.tmpdir) / "once.sqlite3"
        module.import_manifest(target, manifest, "test-import")
        with self.assertRaises(SystemExit):
            module.import_manifest(target, manifest, "test-import")


if __name__ == "__main__":
    unittest.main()
