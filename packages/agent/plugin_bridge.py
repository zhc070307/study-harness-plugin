"""Packaged Harness adapter: per-session semesters, file import and shared evidence.

The on-disk learning layout remains compatible with the standalone application.
Context reads use SQLite read-only and never initialize or migrate a database.
"""
from __future__ import annotations
import hashlib
import json
import mimetypes
import sqlite3
import sys
from pathlib import Path
import file_lock
from agreements import read_all, read_only
from workspace import Workspace, MAX_FILE, MAX_AUDIO
from worker import Store, encoded
from study_agent import db
import transcription
from plugin_transcription import PluginTranscription
transcription.Transcription = PluginTranscription


def load(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def semester_root(root, session, explicit=None, persist=True):
    index = load(root/'.semesters/index.json', {'active': 'initial', 'items': [{'id': 'initial', 'name': '当前学期', 'status': 'open'}]})
    path = root/'.semesters/plugin-sessions.json'
    saved = load(path, {})
    identifier = explicit or saved.get(session) or index['active']
    item = next((v for v in index['items'] if v['id'] == identifier), None)
    if not item or item['status'] == 'removed':
        raise ValueError('会话绑定的学期不存在或已移除；请明确选择其他学期')
    target = root if identifier == 'initial' else root/'.semesters'/identifier
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('学期数据目录不能位于项目之外')
    if persist and session:
        path.parent.mkdir(parents=True, exist_ok=True)
        with (path.parent/'plugin-sessions.lock').open('a') as lock:
            file_lock.flock(lock, file_lock.LOCK_EX)
            saved = load(path, {})
            # A new session pins the current semester once; existing sessions keep theirs.
            if session not in saved or explicit:
                saved[session] = identifier
                db.write_text_atomic(path, encoded(saved))
            else:
                return semester_root(root, session, persist=False)
    return target


def readonly_context(root, session):
    target = semester_root(root, session, persist=False)
    result = {'semester': 'initial' if target == root else target.name, 'courses': [], 'tasks': [], 'pending': [], 'agreements': read_all(target)['references'], 'memory': '', 'materials': []}
    memory = root/'.semesters/shared/.study-system/AGENT_MEMORY.md'
    if memory.exists(): result['memory'] = memory.read_text(encoding='utf-8')[:16000]
    database = target/'.study-system/data/task_memory.sqlite3'
    if database.exists():
        conn = sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'courses' in tables: result['courses'] = [dict(r) for r in conn.execute('SELECT id,code,name FROM courses ORDER BY code LIMIT 100')]
            if 'tasks' in tables: result['tasks'] = [dict(r) for r in conn.execute("SELECT task_key,title,course_id,status FROM tasks WHERE status='open' LIMIT 100")]
            if 'flow_runs' in tables: result['pending'] = [dict(r) for r in conn.execute("SELECT id,course,session,status FROM flow_runs WHERE status='open' LIMIT 50")]
            if 'desktop_files' in tables:
                result['materials'] = [{k: v.get(k) for k in ('id','name','sha256','course','course_path')} for (raw,) in conn.execute('SELECT value FROM desktop_files LIMIT 100') if not (v := json.loads(raw)).get('removed')]
        finally: conn.close()
    notes = load(target/'.study-system/project-context.json', {'version': 0, 'items': {}})
    result['shared_context'] = []
    for item in notes['items'].values():
        sources = []
        for source in item.get('sources', []):
            path = (target/source['path']).resolve()
            valid = path.is_relative_to(target.resolve()) and path.is_file()
            current = hashlib.sha256(path.read_bytes()).hexdigest() if valid else None
            sources.append(dict(source, current_sha256=current, stale=current != source['sha256']))
        result['shared_context'].append(dict(item, sources=sources, stale=any(s['stale'] for s in sources)))
    return result


def remember(target, args, operation):
    if read_only(target): raise ValueError('已归档学期不能修改项目记忆')
    key, summary = args.get('key'), args.get('summary')
    if not isinstance(key, str) or not key.strip() or not isinstance(summary, str) or not summary.strip() or len(summary) > 16000:
        raise ValueError('请保存明确主题及不超过16000字的真实材料摘要')
    sources = []
    for source in args.get('sources', []):
        path = (target/source['path']).resolve()
        if not path.is_relative_to(target.resolve()) or not path.is_file(): raise ValueError('摘要来源必须是学期内实际文件')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if source.get('sha256') != digest: raise ValueError('摘要来源版本已变化，请先读取原文')
        sources.append({'path': str(path.relative_to(target)), 'sha256': digest})
    folder = target/'.study-system'
    folder.mkdir(parents=True, exist_ok=True)
    with (folder/'project-context.lock').open('a') as lock:
        file_lock.flock(lock, file_lock.LOCK_EX)
        path = folder/'project-context.json'
        state = load(path, {'version': 0, 'items': {}, 'operations': {}})
        fingerprint = hashlib.sha256(encoded(args).encode()).hexdigest()
        if operation in state['operations']:
            prior = state['operations'][operation]
            if prior['fingerprint'] != fingerprint: raise ValueError('同一记忆操作的内容不能变化')
            return prior['result']
        if args.get('expected') != state['version']: raise ValueError('项目记忆已有变化，请先读取当前版本')
        item = {'key': key, 'summary': summary, 'sources': sources, 'session': args.get('session')}
        state['items'][key] = item
        state['version'] += 1
        result = {'version': state['version'], 'saved': item}
        state['operations'][operation] = {'fingerprint': fingerprint, 'result': result}
        db.write_text_atomic(path, encoded(state))
        return result


def execute(root, request):
    action, args = request['action'], request.get('args', {})
    session, operation = args.get('session'), request.get('id', '')
    if action == 'context':
        result = readonly_context(root, session)
        result['version'] = load(semester_root(root, session, persist=False)/'.study-system/project-context.json', {'version': 0})['version']
        return result
    if action == 'select':
        target = semester_root(root, session, args['semester'])
        return {'semester': 'initial' if target == root else target.name, 'session': session}
    target = semester_root(root, session, persist=True)
    if action == 'remember': return remember(target, args, operation)
    if action == 'calendar':
        from plugin_calendar import execute as calendar_execute
        return calendar_execute(target, args, operation, request.get('calendar'))
    if action == 'transcribe':
        # Native host runs locally and owns GPU work; no external HTTP service is needed.
        tool = PluginTranscription(target)
        return {'status': tool.status, 'check': tool.resources, 'start': lambda: tool.start(args), 'stop': lambda: tool.stop(args)}[args['action']]()
    if action == 'attachment_import':
        import base64
        data = base64.b64decode(args['data'], validate=True)
        digest = hashlib.sha256(data).hexdigest()
        if 'sha256:'+digest != args['attachment_id'] or (args.get('expected') and args['expected'] != digest):
            raise ValueError('附件版本校验失败')
        store = Workspace(target)
        try:
            return store.perform('create', {'name':args['name'], 'data':args['data'], 'origin':{'attachment':args['attachment_id']}}, 'attachment:'+digest+':'+args['name'])
        finally: store.close()
    if action == 'import':
        path = (Path(request['project'])/args['path']).resolve()
        if not path.is_relative_to(Path(request['project']).resolve()) or not path.is_file(): raise ValueError('只导入当前项目内的真实文件')
        mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        maximum = MAX_AUDIO if mime.startswith('audio/') else MAX_FILE
        if path.stat().st_size > maximum: raise ValueError('文件超过支持的大小')
        data = path.read_bytes()
        if len(data) > maximum: raise ValueError('文件超过支持的大小')
        digest = hashlib.sha256(data).hexdigest()
        if args.get('expected') and args['expected'] != digest: raise ValueError('源文件已变化，请重新读取')
        import base64
        store = Workspace(target)
        try:
            # Stable source/version identity makes repeated imports from different chats safe.
            identity = 'import:'+hashlib.sha256(str(path).encode()+digest.encode()).hexdigest()
            return store.perform('create', {'name': path.name, 'mime': mime, 'data': base64.b64encode(data).decode(), 'origin': {'project_path':str(path.relative_to(Path(request['project']).resolve())), 'sha256':digest}}, identity)
        finally: store.close()
    if request.get('desktop'):
        if action == 'semester' and args.get('action') == 'select':
            return execute(root, {'action': 'select', 'args': args})
        store = Workspace(target)
        try: return store.perform(action, args, operation)
        finally: store.close()
    store = Store(target)
    try: return store.execute(request)
    finally: store.conn.close()


if __name__ == '__main__':
    try: print(encoded({'ok': True, 'result': execute(Path(sys.argv[1]).resolve(), json.load(sys.stdin))}))
    except Exception as error:
        print(encoded({'ok': False, 'error': str(error)}))
        sys.exit(1)
