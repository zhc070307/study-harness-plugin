"""Compaction timing, portable recovery and lossless backup rotation."""
import hashlib
import shutil
from pathlib import Path
from unittest.mock import patch

from tests._support import ServiceTestCase
from study_agent.backup_store import maintain_backups, read_backup
from study_agent.course_memory import snapshot, restore
from study_agent.errors import ConflictError, ValidationError
from study_agent.learning import LearningService


class MemoryMaintenanceTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.tmpdir) / 'workspace'
        self.root.mkdir()
        self.learning = LearningService(self.conn, self.clock)
        self.memory = self.root / '.study-system/AGENT_MEMORY.md'
        self.memory.parent.mkdir()
        self.memory.write_text('Old operation\nUnresolved action')

    def write(self, text, summary=None):
        state = self.learning.memory_status(self.root)
        return self.learning.rewrite_memory(self.root, text, state['content_hash'], summary)

    def save_course(self, content='Agreed method'):
        self.service.add_course('COMP1023', 'Python')
        folder = self.root / 'COMP1023'
        folder.mkdir(exist_ok=True)
        (folder / 'COURSE.md').write_text(content)
        with patch('study_agent.course_memory.Clock', return_value=self.clock):
            return snapshot(self.conn, self.root, 'COMP1023')

    def test_updates_cannot_postpone_weekly_compaction(self):
        self.write('Unresolved action', 'Removed obsolete operations')
        self.clock.advance(seconds=6 * 86400)
        self.assertFalse(self.write('Unresolved action\nNew action')['review_due'])
        self.clock.advance(seconds=2 * 86400)
        state = self.write('Unresolved action\nAnother action')
        self.assertTrue(state['review_due'])
        self.assertNotEqual(state['last_update']['updated_at'], state['last_review']['updated_at'])

    def test_legacy_save_is_not_a_verified_compaction(self):
        self.learning.checkpoint('memory.last_review', {'after_characters': 20})
        self.assertTrue(self.write('Still unresolved')['review_due'])
        with self.assertRaises(ValidationError):
            self.write('Still unresolved', ' ')
        self.assertFalse(self.write('Still unresolved', 'Retained the unresolved action')['review_due'])

    def test_archived_course_restores_after_project_move(self):
        saved = self.save_course()
        self.clock.advance(seconds=8 * 86400)
        maintain_backups(self.conn, self.root, self.clock)
        row = self.conn.execute('SELECT * FROM course_context_snapshots WHERE id=?', (saved['id'],)).fetchone()
        self.assertIn('::', row['snapshot_path'])
        self.assertEqual(read_backup(self.root, saved['snapshot_path'], saved['content_hash']), 'Agreed method')
        moved = Path(self.tmpdir) / 'moved'
        shutil.move(str(self.root), str(moved))
        (moved / 'COMP1023/COURSE.md').write_text('Changed method')
        restore(self.conn, moved, saved['id'])
        self.assertEqual((moved / 'COMP1023/COURSE.md').read_text(), 'Agreed method')
        self.assertFalse(self.root.exists())

    def test_moved_duplicate_records_keep_identity_and_share_content(self):
        saved = self.save_course()
        original = self.root / saved['snapshot_path']
        duplicate = original.with_name('duplicate.md')
        duplicate.write_bytes(original.read_bytes())
        legacy = '/missing/previous/project/' + str(duplicate.relative_to(self.root))
        self.conn.execute('INSERT INTO course_context_snapshots VALUES (?,?,?,?,?,?,?)',
                          ('duplicate', saved['course_id'], '/missing/COURSE.md', legacy,
                           saved['content_hash'], saved['created_at'], 'old moved copy'))
        result = maintain_backups(self.conn, self.root, self.clock)
        rows = list(self.conn.execute('SELECT * FROM course_context_snapshots'))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['snapshot_path'], rows[1]['snapshot_path'])
        self.assertEqual(result['duplicate_loose_copies_removed'], 1)
        self.assertEqual(read_backup(self.root, rows[1]['snapshot_path'], saved['content_hash']), 'Agreed method')
        with patch('study_agent.course_memory.Clock', return_value=self.clock):
            snapshot(self.conn, self.root, 'COMP1023')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM course_context_snapshots').fetchone()[0], 2)

    def test_corrupt_original_is_not_removed_or_restored(self):
        saved = self.save_course()
        backup = self.root / saved['snapshot_path']
        backup.write_text('Corrupt content')
        self.clock.advance(seconds=8 * 86400)
        with self.assertRaises(ConflictError):
            maintain_backups(self.conn, self.root, self.clock)
        with self.assertRaises(ConflictError):
            restore(self.conn, self.root, saved['id'])
        self.assertTrue(backup.exists())
        self.assertEqual((self.root / 'COMP1023/COURSE.md').read_text(), 'Agreed method')

    def test_memory_archive_preserves_original_reference(self):
        state = self.write('Unresolved action', 'Removed old operation')
        reference = state['last_review']['value']['backup']
        import os
        os.utime(reference, (self.clock.now().timestamp(), self.clock.now().timestamp()))
        self.clock.advance(seconds=8 * 86400)
        maintain_backups(self.conn, self.root, self.clock)
        self.assertFalse(Path(reference).exists())
        self.assertEqual(read_backup(self.root, reference), 'Old operation\nUnresolved action')
        self.assertEqual(self.memory.read_text(), 'Unresolved action')

    def test_archive_failure_keeps_index_and_original(self):
        saved = self.save_course()
        self.clock.advance(seconds=8 * 86400)
        with patch('study_agent.backup_store._archive', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                maintain_backups(self.conn, self.root, self.clock)
        row = self.conn.execute('SELECT * FROM course_context_snapshots WHERE id=?', (saved['id'],)).fetchone()
        self.assertEqual(row['snapshot_path'], saved['snapshot_path'])
        self.assertTrue((self.root / saved['snapshot_path']).exists())

    def test_daily_maintenance_is_idempotent(self):
        self.save_course()
        maintain_backups(self.conn, self.root, self.clock)
        self.assertTrue(maintain_backups(self.conn, self.root, self.clock)['skipped'])
        self.clock.advance(seconds=86400)
        self.assertNotIn('skipped', maintain_backups(self.conn, self.root, self.clock))

    def test_recent_course_and_memory_are_archived_without_losing_recovery(self):
        saved = self.save_course()
        state = self.write('Current memory')
        reference = state['last_update']['value']['backup']
        result = maintain_backups(self.conn, self.root, self.clock, force=True)
        self.assertEqual(result['retention_days'], 0)
        self.assertFalse((self.root / saved['snapshot_path']).exists())
        self.assertEqual(read_backup(self.root, saved['snapshot_path'], saved['content_hash']), 'Agreed method')
        self.assertFalse(Path(reference).exists())
        self.assertEqual(read_backup(self.root, reference), 'Old operation\nUnresolved action')

    def test_new_memory_backups_keep_the_recorded_bytes_on_every_platform(self):
        state = self.write('Current memory')
        backup = Path(state['last_update']['value']['backup'])
        self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), backup.stem)
        self.assertNotIn(b'\r\n', backup.read_bytes())

    def test_legacy_windows_memory_backup_survives_archiving_and_relocation(self):
        state = self.write('Current memory')
        backup = Path(state['last_update']['value']['backup'])
        backup.write_bytes(b'Old operation\r\nUnresolved action')
        legacy = 'C:/old/semester/' + str(backup.relative_to(self.root)).replace('/', '\\')
        self.assertEqual(read_backup(self.root, legacy, backup.stem), 'Old operation\nUnresolved action')
        maintain_backups(self.conn, self.root, self.clock, force=True)
        self.assertFalse(backup.exists())
        self.assertEqual(read_backup(self.root, legacy, backup.stem), 'Old operation\nUnresolved action')

    def test_legacy_windows_course_backup_does_not_accept_changed_text(self):
        saved = self.save_course('Line one\nLine two')
        backup = self.root / saved['snapshot_path']
        backup.write_bytes(b'Line one\r\nLine two')
        legacy = 'C:/old/semester/' + str(backup.relative_to(self.root)).replace('/', '\\')
        self.assertEqual(read_backup(self.root, legacy, saved['content_hash']), 'Line one\nLine two')
        backup.write_bytes(b'Changed line\r\nLine two')
        with self.assertRaises(ConflictError):
            read_backup(self.root, legacy, saved['content_hash'])
