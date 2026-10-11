import json
import sqlite3
from contextlib import closing
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from study_agent.workspace import workspace_root, program_root

PROGRAM = Path(__file__).resolve().parents[1]

class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.a = self.root/'term-a';self.a.mkdir()
        self.b = self.root/'term-b';self.b.mkdir()
        (self.root/'.study-workspace.json').write_text(json.dumps({'program':str(PROGRAM),'workspace':'term-a'}))
    def run_command(self,term,*args):
        return subprocess.run([sys.executable,str(PROGRAM/'study'),'--workspace',str(term),*args],cwd=self.root,capture_output=True,text=True)
    def test_config_paths_are_relative_and_work_from_course_folder(self):
        course=self.a/'course';course.mkdir()
        self.assertEqual(workspace_root(course),self.a)
        self.assertEqual(program_root(course),PROGRAM)
    def test_independent_databases(self):
        for term in [self.a,self.b]:self.assertEqual(self.run_command(term,'init').returncode,0)
        self.assertEqual(self.run_command(self.a,'course','add','--code','TEST101','--name','Test').returncode,0)
        for term,expected in [(self.a,1),(self.b,0)]:
            with closing(sqlite3.connect(term/'.study-system/data/task_memory.sqlite3')) as conn:
                self.assertEqual(conn.execute('select count(*) from courses').fetchone()[0],expected)
    def test_missing_semester_does_not_create_a_database(self):
        missing=self.root/'removed';r=self.run_command(missing,'course','list')
        self.assertNotEqual(r.returncode,0);self.assertFalse(missing.exists())
    def test_archiving_content_leaves_program_usable(self):
        self.assertEqual(self.run_command(self.a,'init').returncode,0)
        self.a.rename(self.root/'archived-term')
        self.assertEqual(self.run_command(self.b,'init').returncode,0)
        self.assertEqual(self.run_command(self.b,'check','integrity').returncode,0)
    def test_non_init_command_rejects_empty_semester(self):
        self.assertNotEqual(self.run_command(self.a,'course','list').returncode,0)
        self.assertFalse((self.a/'.study-system').exists())
