"""Harness operations that reuse the existing learning services."""
from pathlib import Path

from study_agent import db
from study_agent.assessment import AssessmentService
from study_agent.course_memory import snapshot
from study_agent.learning import build_now_context
from study_agent.models import Clock, parse_date, week_start_of


def required(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + '不能为空')
    return value.strip()


def context(store, run, step, inputs, operation):
    day = inputs.get('day') or Clock().today().isoformat()
    # Reuse synchronization, scans and backup maintenance; Calendar is an
    # optional platform capability, never evidence that teaching occurred.
    state = build_now_context(store.conn, store.root, day, include_calendar=bool(inputs.get('calendar')))
    state['agent_memory']=store.agent_memory()['content']
    state['memory_maintenance']=store.agent_memory()
    state['open_tasks'] = [dict(row) for row in store.conn.execute("SELECT * FROM tasks WHERE status='open' ORDER BY task_key")]
    state['issues'] = [dict(row) for row in store.conn.execute(
        "SELECT * FROM learning_issues WHERE status='pending' ORDER BY next_retest_on,id")]
    if not inputs.get('calendar'):
        state['calendar'] = {'available': False, 'reason': '日历由宿主提供；本次未查询，不能据此推断出勤或授课。'}
    return state


def course_state(store, run, step, inputs, operation):
    reviews = inputs.get('reviews')
    if not isinstance(reviews, list):
        raise ValueError('提供已实际核对的课程记录列表')
    prepared, seen = [], set()
    for review in reviews:
        course = store.course(review.get('course') or run['course'])
        if run['course'] and course['id'] != run['course']:
            raise ValueError('只能核对本课')
        if course['id'] in seen:
            raise ValueError('同一次核对不能重复提交一门课程')
        seen.add(course['id'])
        path = store.learning.course_roots(store.root)[course['id']][1] / 'COURSE.md'
        state = store.learning.course_state_status(course['id'], path)
        if review.get('ledger_hash') != state['ledger_hash'] or review.get('course_hash') != state['course_hash']:
            raise ValueError('课程事实或文档已改变，请重新核对')
        required(review.get('summary'), '事实核对说明')
        if 'content' in review:
            required(review['content'], '课程文档')
        if 'map_summary' in review:
            required(review['map_summary'], '材料地图核对说明')
        if review.get('map_review_on'):
            parse_date(review['map_review_on'])
        prepared.append((course, path, review))
    # Check all inputs first. Preserve the prior document before any rewrite.
    result = []
    for course, path, review in prepared:
        if 'content' in review:
            snapshot(store.conn, store.root, course['id'], review['summary'])
            store.stage_text(path, review['content'], operation)
            db.write_text_atomic(path, review['content'])
        current = store.learning.course_state_status(course['id'], path)
        reviewed = store.learning.review_course_state(store.root, course['id'],
            current['ledger_hash'], current['course_hash'], review['summary'])
        if review.get('map_summary'):
            store.learning.checkpoint('course.map_review:' + course['id'], {'course': course['code'],
                'summary': review['map_summary'], 'review_on': review.get('map_review_on'),
                'reviewed_at': Clock().now().isoformat()})
        result.append(reviewed)
    return {'courses': result}


def daily(store, run, step, inputs, operation):
    reason = required(inputs.get('reason'), '计划选择依据')
    context(store, run, step, inputs, operation)
    day = inputs.get('day') or Clock().today().isoformat()
    week = week_start_of(parse_date(day)).isoformat()
    for item in inputs.get('pool', []):
        required(item.get('reason'), '任务优先级依据')
        store.learning.put_in_pool(item['task'], week, item['priority'], item['reason'])
    for identifier in inputs.get('remove', []):
        store.learning.remove_from_pool(identifier)
    selected = inputs.get('selected')
    if selected is not None:
        if not isinstance(selected, list):
            raise ValueError('今日选择必须是任务列表')
        for identifier in selected:
            if not store.tasks.readiness(identifier)['ready']:
                raise ValueError('所选任务存在实际未完成依赖，请重新选择')
        store.learning.select_today(selected, day)
    return {'day': day, 'week': week, 'reason': reason,
            'pool': store.learning.list_pool(week), 'today': store.learning.list_today(day)}


def retest(store, run, step, inputs, operation):
    answers = required(inputs.get('answers'), '原始复测作答')
    summary = required(inputs.get('summary'), '复测批改说明')
    results = inputs.get('results')
    if not isinstance(results, list) or not results:
        raise ValueError('复测需要对应错点的结果')
    seen = set()
    for result in results:
        issue = store.conn.execute('SELECT * FROM learning_issues WHERE id=?', (result['issue'],)).fetchone()
        if issue is None or issue['course_id'] != run['course'] or result['issue'] in seen:
            raise ValueError('复测错点必须属于本课且不重复')
        seen.add(result['issue'])
    evidence = store.save_evidence(operation, {'answers': answers, 'summary': summary, 'source': inputs.get('source')})
    assessments = AssessmentService(store.conn)
    attempt, _ = assessments.record_assessment({'course': run['course'], 'source_kind': inputs.get('source_kind', 'other'),
        'attempt_type': 'retest', 'summary': summary, 'evidence_ref': str(evidence)})
    for result in results:
        assessments.record_issue_result({'issue_id': result['issue'], 'result': result['result'], 'attempt_id': attempt,
                                        'next_retest_on': result.get('next_retest_on')})
    return {'attempt': attempt, 'results': results, 'evidence': str(evidence), 'tasks_completed': False}


def memory(store, run, step, inputs, operation):
    content = required(inputs.get('content'), '长期约定')
    expected = required(inputs.get('expected'), '读取的记忆版本')
    summary = required(inputs.get('summary'), '记忆整理依据')
    from agreements import library_root
    root=store.root if store.root.name=='shared' and store.root.parent.name=='.semesters' else library_root(store.root)/'.semesters/shared'
    if root!=store.root:
        from worker import Store
        shared=Store(root,inherit=False)
        try:return shared.execute({'action':'memory_save','args':inputs,'id':'memory:'+operation})
        finally:shared.conn.close()
    current = store.learning.memory_status(store.root)
    if current['content_hash'] != expected:
        raise ValueError('行动记忆已改变，请重新读取')
    if len(content) > current['limit']:
        raise ValueError('行动记忆超过长度限制')
    path = Path(current['path'])
    store.stage_text(path, content, operation)
    return store.learning.rewrite_memory(store.root, content, expected, review_summary=summary)


HANDLERS = {'context': context, 'course_state': course_state, 'daily': daily, 'retest': retest, 'memory': memory}
