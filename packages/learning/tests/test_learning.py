"""Behavioral checks for the replacement learning layer."""
import json
from pathlib import Path
from tests._support import ServiceTestCase
from study_agent.learning import LearningService, write_learning_view, sync_working_v4
from study_agent.errors import ConflictError, ValidationError


class LearningTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.learning = LearningService(self.conn, self.clock)
        self.root = Path(self.tmpdir)

    def prepare_view(self, actor='student'):
        task = self.make_open_task('Read and check', actor=actor, criteria=['Read', 'Check'])
        self.learning.put_in_pool(task.id, '2026-09-07', 'must')
        self.learning.select_today([task.id], '2026-09-09')
        path = self.root / 'TODAY_WORKING.md'
        write_learning_view(self.conn, 'working', path, '2026-09-09', str(self.db_path))
        return task, path

    def test_undated_pool_and_completion_exit(self):
        task, path = self.prepare_view()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM task_schedules').fetchone()[0], 0)
        path.write_text(path.read_text().replace('- [ ] **【你】', '- [x] **【你】'), encoding='utf-8')
        sync_working_v4(self.conn, path)
        self.assertEqual(self.service.get_task(task.id).status, 'completed')
        self.assertEqual(self.learning.list_pool('2026-09-07'), [])
        self.assertEqual(sync_working_v4(self.conn, path)['applied'], [])

    def test_requirement_retry_keeps_task_identity(self):
        import contextlib
        import io
        from study_agent.cli import main
        args = ['--db', str(self.db_path), 'task', 'create', '--title', 'Lab submission',
                '--status', 'open', '--origin-key', 'COMP1023:lab1:submission']
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 0)
            self.assertEqual(main(args), 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)

    def test_scope_details_are_not_rendered_as_subtask_checkboxes(self):
        task, path = self.prepare_view()
        content = path.read_text()
        self.assertNotIn('  - [ ] Read', content)
        self.assertNotIn('  - [ ] Check', content)
        sync_working_v4(self.conn, path)
        sync_working_v4(self.conn, path)
        self.assertEqual(self.service.get_task(task.id).status, 'open')
        self.assertFalse(any(c.satisfied for c in self.service.criteria_for(task.id)))

    def test_structured_description_separates_material_scope_and_completion(self):
        task = self.make_open_task(
            'Read notes',
            description='材料：Course notes\n范围：pages 1-12\n完成标准：反馈疑问',
            criteria=['Read'],
        )
        self.learning.put_in_pool(task.id, '2026-09-07', 'must')
        self.learning.select_today([task.id], '2026-09-09')
        path = self.root / 'TODAY_WORKING.md'
        write_learning_view(self.conn, 'working', path, '2026-09-09', str(self.db_path))
        content = path.read_text()
        self.assertIn('  - 材料：Course notes', content)
        self.assertIn('  - 范围：pages 1-12', content)
        self.assertIn('  - 完成标准：反馈疑问', content)
        self.assertNotIn('说明：材料：', content)
        self.assertIn('- [ ] **【你】Read notes**\n  - 材料：Course notes', content)

    def test_working_view_separates_adjacent_tasks(self):
        first = self.make_open_task('First', description='材料：One')
        second = self.make_open_task('Second', description='材料：Two')
        for task in (first, second):
            self.learning.put_in_pool(task.id, '2026-09-07', 'should')
        self.learning.select_today([first.id, second.id], '2026-09-09')
        path = self.root / 'TODAY_WORKING.md'
        write_learning_view(self.conn, 'working', path, '2026-09-09', str(self.db_path))
        self.assertIn('  - 材料：One\n\n- [ ] **【你】Second**', path.read_text())

    def test_week_view_separates_title_from_selection_rationale(self):
        task = self.make_open_task('Read notes')
        self.learning.put_in_pool(task.id, '2026-09-07', 'must', 'Direct prerequisite')
        path = self.root / 'WEEK.md'
        write_learning_view(self.conn, 'week', path, '2026-09-07', str(self.db_path))
        content = path.read_text()
        self.assertIn('- 【你】Read notes\n  - 选择理由：Direct prerequisite', content)
        self.assertNotIn('Read notes｜Direct prerequisite', content)

    def test_agent_tick_needs_artifact_and_rolls_back(self):
        task, path = self.prepare_view('agent')
        path.write_text(path.read_text().replace('- [ ] **【Codex】', '- [x] **【Codex】'), encoding='utf-8')
        with self.assertRaises(ValidationError):
            sync_working_v4(self.conn, path)
        self.assertFalse(any(c.satisfied for c in self.service.criteria_for(task.id)))
        self.service.add_evidence(task.id, 'artifact', 'Verified notes', reference='notes.pdf')
        sync_working_v4(self.conn, path)
        self.assertEqual(self.service.get_task(task.id).status, 'completed')

    def test_unmapped_checkbox_rejected(self):
        _, path = self.prepare_view()
        path.write_text(path.read_text() + '\n- [x] invented\n', encoding='utf-8')
        with self.assertRaises(ConflictError):
            sync_working_v4(self.conn, path)

    def test_selection_requires_current_pool(self):
        task = self.make_open_task()
        with self.assertRaises(ValidationError):
            self.learning.select_today([task.id], '2026-09-09')

    def test_future_selection_cannot_move_or_clear_today(self):
        task, _ = self.prepare_view()
        before = self.learning.list_today('2026-09-09')
        for refs in ([task.id], []):
            with self.assertRaises(ValidationError):
                self.learning.select_today(refs, '2026-09-10')
        self.assertEqual(self.learning.list_today('2026-09-09'), before)
        self.assertEqual(self.learning.list_today('2026-09-10'), [])

    def test_views_do_not_turn_generic_dates_into_deadlines(self):
        task, path = self.prepare_view()
        self.service.update_task(task.id, due_at='2026-09-10')
        for kind, target in [('working', '2026-09-09'), ('week', '2026-09-07'),
                             ('backlog', '2026-09-09')]:
            write_learning_view(self.conn, kind, path, target, str(self.db_path))
            self.assertIn('｜日期 2026-09-10', path.read_text())
            self.assertNotIn('｜截止', path.read_text())
        write_learning_view(self.conn, 'working', path, '2026-09-09', str(self.db_path))
        path.write_text(path.read_text().replace('- [ ] **【你】', '- [x] **【你】'), encoding='utf-8')
        sync_working_v4(self.conn, path)
        self.assertEqual(self.service.get_task(task.id).status, 'completed')

    def test_views_disclose_full_inventory_and_backlog_lists_every_open_task(self):
        selected = self.make_open_task('Selected task', criteria=['Do it'])
        outside = self.make_open_task('Future visible task', actor='agent')
        self.learning.put_in_pool(selected.id, '2026-09-07', 'must')
        self.learning.select_today([selected.id], '2026-09-09')
        working = self.root / 'TODAY_WORKING.md'
        week = self.root / 'WEEK.md'
        backlog = self.root / 'BACKLOG.md'
        write_learning_view(self.conn, 'working', working, '2026-09-09', str(self.db_path))
        write_learning_view(self.conn, 'week', week, '2026-09-07', str(self.db_path))
        write_learning_view(self.conn, 'backlog', backlog, '2026-09-09', str(self.db_path))
        self.assertIn('全部开放记录 2 项｜本周范围 1 项（当前可开始 1 项）｜本周外保存 1 项｜今日队列 1 项', working.read_text())
        self.assertIn('全部开放记录 2 项｜本周范围 1 项（当前可开始 1 项）｜本周外保存 1 项', week.read_text())
        inventory = backlog.read_text()
        self.assertIn('Selected task｜今日・本周 Must Do', inventory)
        self.assertIn('Future visible task｜本周外', inventory)

    def test_views_group_each_course_by_actor_then_priority(self):
        student_should = self.make_open_task('Student should', actor='student')
        student_must = self.make_open_task('Student must', actor='student')
        agent_must = self.make_open_task('Agent must', actor='agent')
        for task, priority in (
            (student_should, 'should'), (student_must, 'must'), (agent_must, 'must')
        ):
            self.learning.put_in_pool(task.id, '2026-09-07', priority)
        self.learning.select_today(
            [agent_must.id, student_should.id, student_must.id], '2026-09-09'
        )

        for kind, target, filename in (
            ('working', '2026-09-09', 'TODAY_WORKING.md'),
            ('week', '2026-09-07', 'WEEK.md'),
            ('backlog', '2026-09-09', 'BACKLOG.md'),
        ):
            path = self.root / filename
            write_learning_view(self.conn, kind, path, target, str(self.db_path))
            content = path.read_text()
            self.assertLess(content.index('### 你要做的'), content.index('### Codex 要做的'))
            self.assertLess(content.index('Student must'), content.index('Student should'))
            self.assertLess(content.index('Student should'), content.index('Agent must'))

    def test_moving_week_invalidates_today_selection(self):
        task, _ = self.prepare_view()
        self.learning.put_in_pool(task.id, '2026-09-14', 'must')
        self.assertEqual(self.learning.list_today('2026-09-09'), [])

    def test_action_memory_review_is_bounded_recoverable_and_weekly(self):
        path = self.root / '.study-system/AGENT_MEMORY.md'
        path.parent.mkdir()
        path.write_text('Old irrelevant operation\nRecent checkpoint', encoding='utf-8')
        course = self.root / 'COURSE.md'
        course.write_text('Agreed goal and method', encoding='utf-8')
        task = self.make_open_task('Durable learning consequence')
        status = self.learning.memory_status(self.root)
        self.assertTrue(status['review_due'])
        revised = self.learning.rewrite_memory(self.root, 'Recent checkpoint', status['content_hash'],
                                               review_summary='Removed obsolete operations; retained unresolved checkpoint.')
        self.assertFalse(revised['review_due'])
        backup = Path(revised['last_review']['value']['backup'])
        self.assertIn('Old irrelevant operation', backup.read_text())
        self.assertEqual(course.read_text(), 'Agreed goal and method')
        self.assertEqual(self.service.get_task(task.id).status, 'open')
        with self.assertRaises(ConflictError):
            self.learning.rewrite_memory(self.root, 'Stale draft', status['content_hash'])
        with self.assertRaises(ValidationError):
            self.learning.rewrite_memory(self.root, 'x' * 6001, revised['content_hash'])
        self.clock.advance(seconds=7 * 86400)
        self.assertTrue(self.learning.memory_status(self.root)['review_due'])

    def test_parent_checkbox_completes_task_without_child_checkboxes(self):
        task, path = self.prepare_view()
        path.write_text(path.read_text().replace('- [ ] **【你】', '- [x] **【你】'), encoding='utf-8')
        sync_working_v4(self.conn, path)
        self.assertEqual(self.service.get_task(task.id).status, 'completed')

    def test_course_snapshot_restore(self):
        from study_agent.course_memory import snapshot, restore
        self.service.add_course('COMP1023', 'Python')
        folder = self.root / 'COMP1023'
        folder.mkdir()
        path = folder / 'COURSE.md'
        path.write_text('Agreed method', encoding='utf-8')
        saved = snapshot(self.conn, self.root, 'COMP1023')
        path.write_text('New state', encoding='utf-8')
        restore(self.conn, self.root, saved['id'])
        self.assertEqual(path.read_text(), 'Agreed method')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM course_context_snapshots').fetchone()[0], 2)

    def test_fact_change_invalidates_course_and_blocks_selection_until_reviewed(self):
        self.service.add_course('COMP1023', 'Python')
        folder = self.root / 'COMP1023'
        folder.mkdir()
        path = folder / 'COURSE.md'
        path.write_text('Not yet read; agreed method unchanged', encoding='utf-8')
        task = self.make_open_task('Read', course='COMP1023')
        self.learning.put_in_pool(task.id, '2026-09-07', 'must')
        def acknowledge():
            state = self.learning.course_state_status('COMP1023', path)
            return self.learning.review_course_state(self.root, 'COMP1023', state['ledger_hash'],
                                                     state['course_hash'], 'Checked actual feedback')
        acknowledge()
        self.learning.select_today([task.id], '2026-09-09')
        previous = self.learning.course_state_status('COMP1023', path)
        self.service.add_evidence(task.id, 'note', 'Student reports a blocker')
        self.assertTrue(self.learning.course_state_status('COMP1023', path)['needs_review'])
        with self.assertRaises(ConflictError):
            self.learning.select_today([task.id], '2026-09-09')
        with self.assertRaises(ConflictError):
            self.learning.review_course_state(self.root, 'COMP1023', previous['ledger_hash'],
                                             previous['course_hash'], 'Stale review')
        path.write_text('Blocker recorded; agreed method unchanged', encoding='utf-8')
        self.assertFalse(acknowledge()['needs_review'])
        self.learning.select_today([task.id], '2026-09-09')
        path.write_text('Unreviewed manual edit', encoding='utf-8')
        self.assertTrue(self.learning.course_state_status('COMP1023', path)['needs_review'])

    def test_calendar_catches_gap_and_preserves_failure_cursor(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        self.service.add_course('COMP1023', 'Python')
        self.learning.checkpoint('calendar.last_snapshot', {'digest': 'old', 'events': []})
        self.clock.advance(seconds=8 * 86400)
        events = [{'title':'COMP1023 Lab', 'start':'2026-09-10T18:00:00+08:00', 'end':'2026-09-10T19:00:00+08:00'}]
        with patch('study_agent.learning.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(events))) as run:
            result = self.learning.calendar_context(self.root)
            self.assertTrue(result['ok'])
            self.assertIn('2026-09-09', run.call_args[0][0])
        self.assertEqual(self.learning.pending_events()[0]['event_type'], 'calendar_ended')
        checkpoint = self.learning.get_checkpoint('calendar.last_snapshot')
        with patch('study_agent.learning.subprocess.run', return_value=SimpleNamespace(returncode=1, stderr='unavailable')):
            self.assertFalse(self.learning.calendar_context(self.root)['ok'])
        self.assertEqual(self.learning.get_checkpoint('calendar.last_snapshot'), checkpoint)

    def test_transcript_arrival_and_receipt(self):
        self.service.add_course('COMP1023', 'Python')
        folder = self.root / 'COMP1023'
        folder.mkdir()
        (folder / 'COURSE.md').write_text('Course map', encoding='utf-8')
        self.learning.scan_materials(self.root, baseline=True)
        (folder / 'transcript.txt').write_text('A new lecture', encoding='utf-8')
        self.assertEqual(self.learning.scan_materials(self.root)['events'], 1)
        self.assertEqual(self.learning.scan_materials(self.root)['events'], 0)
        event = self.learning.pending_events()[0]
        self.assertEqual(event['artifact_kind'], 'other')
        self.learning.classify_material(event['id'], 'transcript', reason='Read actual lecture content')
        event = self.learning.pending_events()[0]
        self.assertEqual(event['event_type'], 'transcript_ready')
        args = dict(summary='Read transcript', task_effect='No new obligation',
                    material_map_updated=True, course_state_reviewed=True,
                    requirements_checked=True, operator='agent')
        with self.assertRaises(ValidationError):
            self.learning.close_event(event['id'], lecture_notes_path=None, **args)
        with self.assertRaises(ValidationError):
            self.learning.close_event(event['id'], lecture_notes_path=None, notes_not_required_reason=' ', **args)
        notes = folder / 'notes.md'
        notes.write_text('Lecture notes', encoding='utf-8')
        self.learning.close_event(event['id'], lecture_notes_path=str(notes), **args)
        self.assertEqual(self.learning.pending_events(), [])

    def test_transcript_no_notes_receipt_keeps_explicit_reason(self):
        self.service.add_course('COMP2711', 'Logic')
        folder = self.root / 'COMP2711'
        folder.mkdir()
        (folder / 'COURSE.md').write_text('Course map')
        (folder / 'transcript.txt').write_text('Revision of existing logic methods')
        self.learning.scan_materials(self.root)
        event = self.learning.pending_events()[0]
        self.learning.classify_material(event['id'], 'transcript', reason='Read tutorial')
        receipt = self.learning.close_event(
            event['id'], summary='Fully reviewed tutorial', task_effect='Reuse existing practice',
            lecture_notes_path=None, notes_not_required_reason='Student authorized judgment; existing notes cover the topic',
            material_map_updated=True, course_state_reviewed=True, requirements_checked=True, operator='agent')
        self.assertIsNone(receipt['lecture_notes_path'])
        self.assertIn('existing notes cover the topic', receipt['summary'])
        self.assertEqual(self.learning.pending_events(), [])

    def test_recording_and_transcript_share_atomic_receipt(self):
        self.service.add_course('COMP1023', 'Python')
        folder = self.root / 'COMP1023'
        folder.mkdir()
        (folder / 'COURSE.md').write_text('Map')
        session = folder / '2026-09-09'
        session.mkdir()
        (session / 'audio.m4a').write_bytes(b'audio')
        self.learning.scan_materials(self.root)
        recording = self.learning.pending_events()[0]
        self.learning.classify_material(recording['id'], 'recording', reason='Listened to lecture', session='lecture-1')
        (session / 'transcript.txt').write_text('Lecture')
        self.learning.scan_materials(self.root)
        transcript = next(e for e in self.learning.pending_events() if e['id'] != recording['id'])
        self.learning.classify_material(transcript['id'], 'transcript', reason='Read lecture', session='lecture-1')
        notes = session / 'notes.md'
        notes.write_text('Actual notes')
        args = dict(summary='Reviewed bundle', task_effect='No new official requirement',
                    lecture_notes_path=str(notes), material_map_updated=True, course_state_reviewed=True,
                    requirements_checked=True, operator='agent')
        with self.assertRaises(ValidationError):
            self.learning.close_event(transcript['id'], related_event_ids=['missing'], **args)
        self.assertEqual(len(self.learning.pending_events()), 2)
        self.learning.close_event(transcript['id'], related_event_ids=[recording['id']], **args)
        self.assertEqual(self.learning.pending_events(), [])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM material_event_receipts').fetchone()[0], 2)

    def test_scheduled_map_review_survives_scans_until_semantic_acknowledgment(self):
        from study_agent.learning import build_now_context
        course = self.service.add_course('COMP1023', 'Python')
        folder = self.root / 'COMP1023'
        folder.mkdir()
        (folder / 'COURSE.md').write_text('Map')
        key = 'course.map_review:' + course.id
        self.learning.checkpoint(key, {'review_on': '2026-09-09', 'course': 'COMP1023'})
        self.assertEqual(build_now_context(self.conn, self.root, '2026-09-08', False)['material_map_reviews_due'], [])
        self.assertEqual(len(build_now_context(self.conn, self.root, '2026-09-09', False)['material_map_reviews_due']), 1)
        self.learning.scan_materials(self.root)
        self.assertEqual(len(build_now_context(self.conn, self.root, '2026-09-10', False)['material_map_reviews_due']), 1)
        self.learning.checkpoint(key, {'review_on': None, 'summary': 'Reviewed semantic structure'})
        self.assertEqual(build_now_context(self.conn, self.root, '2026-09-10', False)['material_map_reviews_due'], [])

    def test_assessment_facts_do_not_need_retired_planning_or_a_retest_task(self):
        from study_agent.assessment import AssessmentService
        self.service.add_course('COMP1023', 'Python')
        service = AssessmentService(self.conn, self.clock)
        with self.service._write():
            _, issues = service.record_assessment({'course': 'COMP1023', 'source_kind': 'other',
                'summary': 'Independent self-test', 'evidence_ref': 'student original answer record', 'issues': [{'concept': 'Types', 'issue_type': 'wrong'}]})
            service.record_issue_result({'issue_id': issues[0], 'result': 'passed', 'evidence_ref': 'student independent answer record'})
        self.assertEqual(self.conn.execute('SELECT status FROM learning_issues WHERE id=?', (issues[0],)).fetchone()[0], 'passed')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 0)

    def test_context_exposes_shared_execution_and_missing_course_setup(self):
        from study_agent.learning import build_now_context
        context = build_now_context(self.conn, self.root, '2026-09-09', include_calendar=False)
        self.assertIn('note', context['execution_routes'])
        registered = {row['code'] for row in self.conn.execute('SELECT code FROM courses')}
        self.assertEqual({row['code'] for row in context['course_setup_needed']}, registered)

    def test_course_setup_preserves_existing_context_and_creates_no_tasks(self):
        self.service.add_course('TEST101', 'Setup fixture')
        result = self.learning.setup_course_context(self.root, 'TEST101')
        self.assertTrue(result['created'])
        path = Path(result['course_md'])
        path.write_text('Student agreed method')
        again = self.learning.setup_course_context(self.root, 'TEST101')
        self.assertFalse(again['created'])
        self.assertEqual(path.read_text(), 'Student agreed method')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 0)
        self.assertIn('test101', self.learning.course_roots(self.root))
