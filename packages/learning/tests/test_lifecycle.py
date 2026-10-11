"""Hook dispatch, semantic reminders, isolation and anti-loop behavior."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

from tests._support import ServiceTestCase
from study_agent.learning import LearningService

HOOK = Path(__file__).resolve().parents[1] / 'hooks/lifecycle.py'
spec = importlib.util.spec_from_file_location('study_lifecycle_test', HOOK)
lifecycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lifecycle)


class LifecycleTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.tmpdir) / 'semester'
        self.root.mkdir()
        self.learning = LearningService(self.conn)

    def call(self, event, **extra):
        payload = {'hook_event_name': event, 'session_id': 'test-session',
                   'turn_id': 'turn-one', 'cwd': str(self.root)}
        payload.update(extra)
        return lifecycle.run(payload, root=self.root, database=self.db_path)

    def test_normal_stop_is_silent_and_prompt_contains_self_check(self):
        prompt = self.call('UserPromptSubmit')
        self.assertIn('正式最终答复之前', prompt['hookSpecificOutput']['additionalContext'])
        self.assertIn('用户说了但尚未登记', prompt['hookSpecificOutput']['additionalContext'])
        self.assertEqual(self.call('Stop'), {})

    def test_due_memory_reminder_survives_ordinary_updates_without_stop_loop(self):
        path = self.root / '.study-system/AGENT_MEMORY.md'
        path.parent.mkdir()
        path.write_text('Unresolved action')
        state = self.learning.memory_status(self.root)
        self.learning.rewrite_memory(self.root, 'Unresolved action\nNew action', state['content_hash'])
        for turn in ('turn-one', 'turn-two'):
            result = self.call('UserPromptSubmit', turn_id=turn)
            self.assertIn('memory compact --summary', result['hookSpecificOutput']['additionalContext'])
            self.assertEqual(self.call('Stop', turn_id=turn), {})

    def test_new_unresolved_state_blocks_once_even_without_active_flag(self):
        self.call('UserPromptSubmit')
        state = {'stale': [{'course': 'X', 'ledger': 'a', 'document': 'b'}],
                 'processing': [], 'pending': [], 'memory_due': False}
        with patch.object(lifecycle, '_state', return_value=state):
            self.call('PostToolUse')
            self.assertEqual(self.call('Stop')['decision'], 'block')
            self.assertEqual(self.call('Stop'), {})
            self.assertEqual(self.call('Stop', stop_hook_active=True), {})
            self.call('UserPromptSubmit', turn_id='turn-two')
            self.assertEqual(self.call('Stop', turn_id='turn-two'), {})

    def test_pending_inbox_does_not_block_but_new_claim_does(self):
        state = {'stale': [], 'processing': [], 'pending': ['event'], 'memory_due': False}
        with patch.object(lifecycle, '_state', return_value=state):
            self.call('UserPromptSubmit')
            self.assertEqual(self.call('Stop'), {})
            state['processing'] = ['event']
            self.assertEqual(self.call('Stop')['decision'], 'block')

    def test_resolved_state_does_not_block(self):
        self.call('UserPromptSubmit')
        state = {'stale': [], 'processing': [], 'pending': [], 'memory_due': False}
        with patch.object(lifecycle, '_state', return_value=state):
            self.assertEqual(self.call('Stop'), {})

    def test_prompt_and_compact_restore_are_model_context(self):
        prompt = self.call('UserPromptSubmit', prompt='Private feedback should not be logged')
        self.assertEqual(prompt['hookSpecificOutput']['hookEventName'], 'UserPromptSubmit')
        self.assertIn('建新任务前先查询', prompt['hookSpecificOutput']['additionalContext'])
        compact = self.call('SessionStart', source='compact')
        self.assertIn('恢复', compact['hookSpecificOutput']['additionalContext'])
        saved = json.dumps([dict(r) for r in self.conn.execute('SELECT * FROM system_checkpoints')])
        self.assertNotIn('Private feedback', saved)

    def test_post_tool_deduplicates_but_detects_new_feedback(self):
        self.service.add_course('COMP1023', 'Python')
        folder = self.root / 'COMP1023'
        folder.mkdir()
        path = folder / 'COURSE.md'
        path.write_text('Current state')
        task = self.make_open_task('Reading', course='COMP1023')
        state = self.learning.course_state_status('COMP1023', path)
        self.learning.review_course_state(self.root, 'COMP1023', state['ledger_hash'],
                                         state['course_hash'], 'Known state')
        self.call('UserPromptSubmit')
        self.assertEqual(self.call('PostToolUse'), {})
        self.service.add_evidence(task.id, 'note', 'A real blocker')
        result = self.call('PostToolUse')
        self.assertIn('COMP1023', result['hookSpecificOutput']['additionalContext'])
        self.assertIn('同一阅读、练习或检测成果', result['hookSpecificOutput']['additionalContext'])
        self.assertEqual(self.call('PostToolUse'), {})

    def test_interrupt_never_restarts_and_resume_reminds(self):
        self.assertEqual(self.call('Interrupt'), {})
        result = self.call('SessionStart', source='resume')
        self.assertIn('中断记录', result['hookSpecificOutput']['additionalContext'])
        again = self.call('SessionStart', source='resume')
        self.assertNotIn('中断记录', again['hookSpecificOutput']['additionalContext'])

    def test_other_semester_and_unknown_event_are_ignored(self):
        self.assertEqual(self.call('Stop', cwd=str(self.root.parent / 'other-semester')), {})
        self.assertEqual(self.call('NotAnEvent'), {})

    def test_unresolved_state_warns_without_another_continuation(self):
        self.service.add_course('COMP1023', 'Python')
        folder = self.root / 'COMP1023'
        folder.mkdir()
        (folder / 'COURSE.md').write_text('Unreviewed')
        result = self.call('Stop', stop_hook_active=True)
        self.assertEqual(result, {})
        self.assertNotIn('decision', result)

    def test_delivery_metadata_has_event_and_turn_but_no_chat(self):
        self.call('Stop', last_assistant_message='Private answer')
        checkpoint = self.learning.get_checkpoint('hook.last_delivery:Stop')['value']
        self.assertEqual(checkpoint['turn_id'], 'turn-one')
        self.assertEqual(checkpoint['output'], 'silent')
        self.assertNotIn('Private answer', json.dumps(checkpoint))
