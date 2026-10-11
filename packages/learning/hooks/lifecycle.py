"""Short lifecycle reminders, not a learning-policy engine.

No prompt, transcript or tool output is retained. Stop requests at most one
fallback continuation per host turn, only for new unresolved state.
"""
import hashlib
import json
import sys
from pathlib import Path

PROGRAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROGRAM / 'src'))
from study_agent.workspace import workspace_root
from study_agent import db
from study_agent.learning import LearningService
from study_agent.course_memory import snapshot
from study_agent.backup_store import maintain_backups
from study_agent.runtime_cleanup import resume_finished

SELF_CHECK = (
    '课程方法不确定时先查材料，再一次一个问题与学生商议并推荐；不把学科标签当固定流程。'
    '笔记写作与笔记验收读取 study-notes；选题编排与试卷核验读取 study-worksheets；先 delivery_review.py prepare，验收后 accept，再 finish_work。'
    '发送正式最终答复之前，先做本轮语义自检：回看用户本轮反馈与已完成的操作，仅针对本轮涉及的课程或系统工作。'
    '依照相关 COURSE.md 已议定的方法，检查应有成果是否产生、已有常规后续行动是否进入任务池、'
    '完成/分数/错误/卡点是否记录、课程状态与任务是否一致、今日清单是否需要同步。'
    '不要因为数据库没有变化就认为没有遗漏：用户说了但尚未登记的反馈也要检查。'
    '本轮确认的易错点须逐项读回 learning_issues 与关联 assessment 证据；仅写入任务描述不算入库。'
    '任务只留行动与简短主题提示，错因和历史归账本；普通提问不自动记为弱点。'
    '本轮新建或调整任务时，核对同课程同执行者同阶段的开放任务；同性质且无独立后果的工作复用或合并，'
    '不要按每节课、每章或每个错点机械拆分。不同前置条件、正式提交与独立学习阶段保留边界。'
    '学期顶层仅保留课程、任务清单和 AGENTS.md；操作说明与选题记录在 .study-system 内，不重建顶层 tmp、.agents 或程序副本。'
    '本轮若选卷或修卷，读回出卷前的信息汇总与草案，核对成品的范围、重点、练习量和去重；只有题目文件不算落实选题流程。'
    '本轮产物验收与回执落到 evidence 后，运行 finish_work.py 清理专用 runtime 工作目录；未完成工作保留。过程文件不写入长期引用。'
    '该补的直接补；新学习义务或重大方法改变先商议；被打断的任务保留剩余阶段和阻碍。'
    '需要接续的系统操作写入短全局行动记忆；到期时只整理行动记忆，不蒸馏课程目标和方法。'
    '纯问答或与学习无关的工作可判定不适用，不制造任务、不全量扫描全部课程、不重复已完成成果。'
    '例行自检静默完成，再给出包含本轮实质结果的完整最终答复；勿用自检回执替代答复。仍有真实阻碍时如实说明。'
)


CLASS_HANDOFF = (
    '请求打开、刷新今日清单或安排学习时，先同步勾选并运行 context now。'
    '逐一检查新近已结束课程和新课材料：是否已有材料处理、阅读、独立练习的任务承接，'
    '或明确的缺材料／待核实阻碍；复用已有任务，不重复创建。'
    '按课程方法核实实际讲授范围，再更新周池、今日选择并生成清单；'
    '不得只打开旧文件或只重新渲染旧选择。保留学生明确暂缓及依赖条件，'
    '课表结束不代表整份材料已讲完。仅调查或讨论修复时报告缺口，不擅自改学习任务。'
)


TASK_GRANULARITY = (
    '建新任务前先查询同课程、同执行者、同学习阶段的开放任务及其依赖、完成证据。'
    '若只是同一阅读、练习或检测成果的新增范围，沿用原任务身份并改写剩余范围；'
    '只有不同前置条件、不同执行者、正式提交或独立完成后果才另建任务。'
    '课次、章节、题号和错点数量本身不是拆分理由；合并时保留原证据与任务历史。'
    '已讲且题面齐备的练习、概念检测和选卷不等待笔记或教材阅读；检查并清除遗留阅读前置。'
    '按当前课程约定，自组巩固卷统一承接教材或补充练习与常规错题复测，'
    '不要再并列原题单义务；官方待做练习仍用原文件。'
    '生成自组卷前先汇总已讲范围、各能力已练／待练数量、错误与复测证据、具体历年考法；证据不足写未知，不猜错误率。'
    '先在现有 PRACTICE_SELECTION.md 的课程段写出卷草案，说明重点、各能力练习量、题型／小问、排除与去重理由，再选题编排。'
    '发卷前将成品与草案逐项核对，不得直接出题后补理由；草案原位更新，不另建每日文稿或学生任务。'
    '课程采用概念选择题时，按 COURSE.md 复用持续检测入口，'
    '核对已检测范围、未答题与复测触发条件；学生准备作答时才出题，保留明确暂缓。'
)


def _context(event, text):
    return {'hookSpecificOutput': {'hookEventName': event, 'additionalContext': text}}


def _state(service, root):
    stale = []
    for course_id, (course, folder) in service.course_roots(root).items():
        state = service.course_state_status(course_id, folder / 'COURSE.md')
        if state['needs_review']:
            stale.append({'course': course['code'], 'ledger': state['ledger_hash'],
                          'document': state['course_hash']})
    pending = service.pending_events()
    memory = service.memory_status(root)
    mapped = service.course_roots(root)
    setup = [row['code'] for row in service.conn.execute('SELECT id,code FROM courses ORDER BY code') if row['id'] not in mapped]
    return {'setup': setup, 'stale': stale, 'processing': [e['id'] for e in pending if e['status'] == 'processing'],
            'pending': [e['id'] for e in pending], 'memory_due': memory['review_due']}


def _brief(state):
    parts = []
    if state.get('setup'):
        parts.append('已登记课程缺 COURSE.md：' + '、'.join(state['setup']) + '；按共享 Skill 与学生沟通形成课程约定')
    if state['stale']:
        parts.append('课程上下文待核对：' + '、'.join(x['course'] for x in state['stale']))
    if state['processing']:
        parts.append('已领取材料尚未提交回执：' + '、'.join(state['processing']))
    if state['pending']:
        parts.append('材料收件箱待处理 %d 项（不等于本轮都必须处理）' % len(state['pending']))
    if state['memory_due']:
        parts.append('全局行动记忆已到语义整理检查点：本轮结束前运行 memory status，'
                     '核对旧条目的真实接续价值，合并或移出已在课程／账本保存的详情；'
                     '用 memory compact --summary 记录处理依据。普通 rewrite 不会重置整理计时；'
                     '仍未解决的事项保留恢复条件，不改写课程目标与方法')
    return '；'.join(parts)


def run(payload, root=None, database=None):
    if root is None:
        try:
            root = workspace_root(payload.get('cwd') or Path.cwd())
            if not root.is_dir() or not (root / '.study-system/data/task_memory.sqlite3').is_file():
                return {'systemMessage': 'Selected semester directory/database is missing; switch the workspace configuration.'}
        except ValueError:
            return {}
    root = Path(root).resolve()
    cwd = Path(payload.get('cwd') or root).resolve()
    if cwd != root.parent and cwd != root and root not in cwd.parents:
        return {}
    event = payload.get('hook_event_name')
    if event not in {'SessionStart', 'UserPromptSubmit', 'PostToolUse', 'Stop', 'Interrupt'}:
        return {}
    conn = db.connect(database or root / '.study-system/data/task_memory.sqlite3', busy_timeout_ms=500)
    try:
        service = LearningService(conn)
        session = str(payload.get('session_id') or 'unknown')
        identity = hashlib.sha256(session.encode()).hexdigest()
        session_key = 'hook.session:' + identity
        cleanup_results = []
        if event == 'SessionStart':
            cleanup_results = resume_finished(root)
        state = None if event == 'Interrupt' else _state(service, root)
        turn_key = 'hook.turn:' + identity
        if event == 'SessionStart':
            for course_id in service.course_roots(root):
                snapshot(conn, root, course_id, 'Session-start course context checkpoint')
            maintain_backups(conn, root)
            path = root / '.study-system/AGENT_MEMORY.md'
            memory = path.read_text(encoding='utf-8') if path.exists() else ''
            interrupted = service.get_checkpoint('hook.interrupted:' + identity)
            text = ('学习系统启动/恢复：使用共享程序目录 skills/study-system/SKILL.md。'
                    '开始学习操作先同步清单并运行 context now，再按需读相关 COURSE.md。'
                    '每轮结束前检查对话中未入库的反馈与既定学习流程是否落实。\n' + CLASS_HANDOFF + '\n' + _brief(state))
            if interrupted and not interrupted['value'].get('reminded_on_resume'):
                text += '\n此会话有中断记录：检查当时任务剩余阶段，不把中断当完成。'
                service.checkpoint('hook.interrupted:' + identity,
                                   dict(interrupted['value'], reminded_on_resume=True))
            for cleanup in cleanup_results:
                if 'kept' in cleanup:
                    text += '\n收尾清理保留了恢复文件：' + cleanup['kept'] + '；原因：' + cleanup['reason']
            result = _context(event, text + '\n' + TASK_GRANULARITY + '\n' + SELF_CHECK + '\n\n' + memory)
            service.checkpoint(session_key, state)
        elif event == 'UserPromptSubmit':
            result = _context(event, SELF_CHECK + '\n' + CLASS_HANDOFF + '\n'
                              + TASK_GRANULARITY + '\n' + _brief(state))
            service.checkpoint(turn_key, {'turn_id': payload.get('turn_id'),
                                         'baseline': state, 'blocked': False})
            service.checkpoint(session_key, state)
        elif event == 'PostToolUse':
            previous = service.get_checkpoint(session_key)
            changed = previous is None or previous['value'] != state
            result = (_context(event, '操作后的状态检查有变化。仅处理本轮相关事项；' + CLASS_HANDOFF + '\n' + TASK_GRANULARITY + '\n'
                               '若刚更新了任务中的错点，立即读回对应 learning_issues 和关联 assessment，核对具体表现及来源；只有任务记录不算入库。已入库则不重复创建。核对后精简任务中的错误历史，更新后用 course state-reviewed 核对版本。\n' + _brief(state))
                      if changed and _brief(state) else {})
            if changed:
                service.checkpoint(session_key, state)
        elif event == 'Interrupt':
            service.checkpoint('hook.interrupted:' + identity,
                               {'session_id': session, 'turn_id': payload.get('turn_id')})
            result = {}  # Never prevent interruption or restart a turn.
        else:
            checkpoint = service.get_checkpoint(turn_key)
            turn = checkpoint['value'] if checkpoint else {}
            same_turn = turn.get('turn_id') == payload.get('turn_id')
            baseline = turn.get('baseline', {}) if same_turn else {}
            old_stale = {x['course']: x for x in baseline.get('stale', [])}
            unresolved = dict(state,
                stale=[x for x in state['stale'] if old_stale.get(x['course']) != x],
                processing=[x for x in state['processing'] if x not in baseline.get('processing', [])],
                pending=[], memory_due=False)
            already_blocked = payload.get('stop_hook_active') or (same_turn and turn.get('blocked'))
            if (unresolved['stale'] or unresolved['processing']) and not already_blocked:
                result = {'decision': 'block', 'reason':
                          '本轮出现尚未收尾的具体状态：' + _brief(unresolved) + '\n' + SELF_CHECK}
                service.checkpoint(turn_key, dict(turn if same_turn else {},
                    turn_id=payload.get('turn_id'), blocked=True))
            else:
                result = {}  # Normal final answers stay final; never loop on a fallback.
        service.checkpoint('hook.last_delivery:' + event, {
            'session_id': session, 'turn_id': payload.get('turn_id'),
            'event': event, 'output': 'continuation' if result.get('decision') == 'block' else
            'context' if result.get('hookSpecificOutput') else 'warning' if result.get('systemMessage') else 'silent',
            'stop_hook_active': bool(payload.get('stop_hook_active')),
        })
        return result
    finally:
        conn.close()


if __name__ == '__main__':
    try:
        print(json.dumps(run(json.load(sys.stdin)), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'systemMessage': 'Learning hook failed: ' + str(exc)}, ensure_ascii=False))
