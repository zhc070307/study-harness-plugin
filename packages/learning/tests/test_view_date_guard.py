"""Old-day rendering must not replace the active editable checklist."""
import contextlib
import io
from pathlib import Path
from unittest.mock import patch

from tests._support import ServiceTestCase
from study_agent import cli


class WorkingDateGuardTests(ServiceTestCase):
    def render(self, day, path):
        output = io.StringIO()
        with patch('study_agent.cli.Clock', return_value=self.clock), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            return cli.main(['--db', str(self.db_path), 'view', 'working', '--date', day, '--out', str(path)])

    def test_other_dates_preserve_checklist_and_mapping(self):
        path = Path(self.tmpdir) / 'TODAY_WORKING.md'
        mapping = path.parent / '.study-system/runtime/views/TODAY_WORKING.state.json'
        mapping.parent.mkdir(parents=True)
        path.write_text('Current checklist with unsynced edits')
        mapping.write_text('Current mapping')
        for day in ['2026-09-08', '2026-09-10']:
            self.assertEqual(self.render(day, path), 4)
            self.assertEqual(path.read_text(), 'Current checklist with unsynced edits')
            self.assertEqual(mapping.read_text(), 'Current mapping')

    def test_today_can_generate_active_checklist(self):
        path = Path(self.tmpdir) / 'TODAY_WORKING.md'
        self.assertEqual(self.render('2026-09-09', path), 0)
        self.assertIn('# 今日任务｜2026-09-09', path.read_text())

    def test_historical_view_can_use_another_filename(self):
        path = Path(self.tmpdir) / '2026-09-08.md'
        self.assertEqual(self.render('2026-09-08', path), 0)
        self.assertIn('# 今日任务｜2026-09-08', path.read_text())
