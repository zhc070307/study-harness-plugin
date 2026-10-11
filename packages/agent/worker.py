"""Small JSON bridge: existing learning ledger plus editable, persistent flows."""
from __future__ import annotations

import difflib
from flow_handler import compile_handler, run_handler as run_data_handler
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sqlite3
import sys
import tempfile
import uuid
import io
import os
from contextlib import redirect_stdout,redirect_stderr

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / 'packages/learning/src'))
from study_agent import db, delivery
from study_agent.assessment import AssessmentService
from study_agent.learning import LearningService, write_learning_view, sync_working_v4
from study_agent.service import TaskService
from study_agent.runtime_cleanup import finish_work
from agreements import library_root, read_all, read_only
from semesters import Semesters


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + '不能为空')
    return value.strip()


def checked_path(root, value):
    path = (root / text(value, '文件路径')).resolve()
    if not path.is_relative_to(root):
        raise ValueError('文件必须在本项目的学习数据目录内')
    return path


def validate(definition):
    text(definition.get('title'), '流程名称')
    if definition.get('scope', 'course') not in ('course', 'workspace'):
        raise ValueError('流程范围必须是课程或全局')
    steps = definition.get('steps')
    if not isinstance(steps, list) or not steps:
        raise ValueError('流程至少需要一个步骤')
    ids = set()
    for step in steps:
        identifier = text(step.get('id'), '步骤名称')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', identifier):
            raise ValueError('步骤标识只使用字母、数字、横线或下划线')
        if identifier in ids:
            raise ValueError('步骤名称重复')
        ids.add(identifier)
        text(step.get('instructions'), '步骤说明')
        if step.get('kind') not in HANDLERS:
            raise ValueError('未知的执行操作；新行为可用 custom 操作和 handler 代码实现')
        course_operations = {'feedback', 'plan', 'review', 'task', 'tasks', 'inspect', 'receipt', 'retest'}
        if step['kind'] in course_operations and definition.get('scope', 'course') != 'course':
            raise ValueError('此操作需要明确的课程范围')
        if step['kind'] in {'daily', 'memory'} and definition.get('scope', 'course') != 'workspace':
            raise ValueError('计划和长期约定使用全局范围')
        if step['kind'] == 'task':
            text(step.get('task'), '任务名称')
        if step['kind'] == 'custom' and not step.get('handler'):
            raise ValueError('自定义操作需要 handler 代码')
        if step.get('handler'):
            compile_handler(text(step['handler'], '执行代码'))
            if not isinstance(step.get('tests'), list) or not step['tests']:
                raise ValueError('新执行代码需要至少一个输入与预期结果示例')
    resolved = set()
    while len(resolved) < len(ids):
        reachable = set()
        for step in steps:
            dependencies = step.get('after', [])
            if not isinstance(dependencies, list) or any(item not in ids for item in dependencies):
                raise ValueError('步骤引用了不存在的前置操作')
            if step['id'] not in resolved and set(dependencies) <= resolved:
                reachable.add(step['id'])
        if not reachable:
            raise ValueError('步骤依赖形成循环')
        resolved.update(reachable)
    by_id = {step['id']: step for step in steps}
    def ancestors(identifier):
        parents = set(by_id[identifier].get('after', []))
        return parents | set().union(*(ancestors(parent) for parent in parents))
    for step in steps:
        required = {'review': 'plan', 'receipt': 'inspect'}.get(step['kind'])
        if required and not any(by_id[parent]['kind'] == required for parent in ancestors(step['id'])):
            raise ValueError('此操作需要先保存相应依据：' + required)
    return definition


def run_handler(step, inputs, context):
    if not step.get('handler'):
        return inputs
    return run_data_handler(step['handler'], inputs, context)


def check_handlers(definition):
    validate(definition)
    for step in definition['steps']:
        for example in step.get('tests', []):
            result = run_handler(step, example['input'], {'course': 'DEMO', 'history': []})
            if result != example['expected']:
                raise ValueError('执行代码未通过示例：' + step['id'])


class Store:
    def __init__(self, root, inherit=True):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.conn = db.initialize(self.root / '.study-system/data/task_memory.sqlite3')
        self.tasks = TaskService(self.conn)
        self.learning = LearningService(self.conn)
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS flow_versions (
              id TEXT PRIMARY KEY, flow TEXT NOT NULL, scope TEXT NOT NULL,
              definition TEXT NOT NULL, parent TEXT, reason TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS flow_active (
              flow TEXT NOT NULL, scope TEXT NOT NULL, version TEXT NOT NULL REFERENCES flow_versions(id),
              PRIMARY KEY(flow,scope));
            CREATE TABLE IF NOT EXISTS flow_runs (
              id TEXT PRIMARY KEY, course TEXT REFERENCES courses(id),
              session TEXT NOT NULL,
              version TEXT NOT NULL REFERENCES flow_versions(id), history TEXT NOT NULL DEFAULT '[]',
              status TEXT NOT NULL DEFAULT 'open');
            CREATE TABLE IF NOT EXISTS flow_operations (
              id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL);
        ''')
        # Older app runs were course-only. Keep their identities and history.
        if next(row for row in self.conn.execute('PRAGMA table_info(flow_runs)') if row['name'] == 'course')['notnull']:
            with db.atomic(self.conn, bump_revision=False):
                self.conn.execute('CREATE TABLE flow_runs_new (id TEXT PRIMARY KEY, course TEXT REFERENCES courses(id), '
                    "session TEXT NOT NULL, version TEXT NOT NULL REFERENCES flow_versions(id), history TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'open')")
                self.conn.execute('INSERT INTO flow_runs_new SELECT * FROM flow_runs')
                self.conn.execute('DROP TABLE flow_runs')
                self.conn.execute('ALTER TABLE flow_runs_new RENAME TO flow_runs')
        self.recovery_warnings = [] if read_only(self.root) else self.recover_files()
        inherit=inherit and not read_only(self.root)
        with db.atomic(self.conn, bump_revision=False):
            for flow, definition in json.loads((PROJECT / 'packages/agent/flows.json').read_text(encoding='utf-8')).items():
                validate(definition)
                version = digest({'flow': flow, 'scope': '*', 'definition': definition})
                self.conn.execute('INSERT OR IGNORE INTO flow_versions VALUES (?,?,?,?,?,?)',
                                  (version, flow, '*', encoded(definition), None, '初始流程'))
                self.conn.execute('INSERT OR IGNORE INTO flow_active VALUES (?,?,?)', (flow, '*', version))
        if inherit:
            shared=Store(library_root(self.root)/'.semesters/shared',inherit=False)
            try:
                self.conn.execute('CREATE TABLE IF NOT EXISTS flow_shared (flow TEXT PRIMARY KEY, version TEXT NOT NULL)')
                with db.atomic(self.conn,bump_revision=False):
                    for row in shared.conn.execute("SELECT flow,version FROM flow_active WHERE scope='*'"):
                        candidate=shared.version(row['version'])
                        current=self.active(row['flow'],'*')
                        previous=self.conn.execute('SELECT version FROM flow_shared WHERE flow=?',(row['flow'],)).fetchone()
                        if current['reason']=='初始流程' or (previous and previous[0]==current['id']):
                            if candidate['reason']!='初始流程' or previous:
                                imported=digest({'shared':candidate['id']})
                                self.conn.execute('INSERT OR IGNORE INTO flow_versions VALUES (?,?,?,?,?,?)',(imported,row['flow'],'*',encoded(candidate['definition']),current['id'],'通用约定'))
                                self.conn.execute("UPDATE flow_active SET version=? WHERE flow=? AND scope='*'",(imported,row['flow']))
                                self.conn.execute('INSERT OR REPLACE INTO flow_shared VALUES (?,?)',(row['flow'],imported))
            finally:shared.conn.close()

    def save_evidence(self, operation, value):
        path = self.root / '.study-system/evidence' / (digest(operation) + '.json')
        db.write_text_atomic(path, encoded(value))
        return path

    def stage_text(self, path, content, operation):
        """Journal file replacements before writing; SQLite owns the commit."""
        path = checked_path(self.root, str(path))
        if any(item.get('path') == str(path) for item in self.recovery_warnings):
            raise ValueError('此文档存在未解决的中断恢复记录')
        journal = self.root / '.study-system/runtime/changes' / (digest(operation) + '.json')
        record = json.loads(journal.read_text(encoding='utf-8')) if journal.exists() else {'operation': operation, 'files': []}
        if any(item['path'] == str(path) for item in record['files']):
            raise ValueError('一次操作不能重复修改同一文档')
        record['files'].append({'path': str(path), 'before': path.read_text(encoding='utf-8') if path.exists() else None,
                               'after': hashlib.sha256(content.encode()).hexdigest()})
        db.write_text_atomic(journal, encoded(record))

    def recover_files(self):
        warnings = []
        # Serializes recovery with every cooperating worker. A committed ledger
        # keeps its new files; an interrupted transaction restores old contents.
        with db.atomic(self.conn, bump_revision=False):
            for journal in (self.root / '.study-system/runtime/changes').glob('*.json'):
                record = json.loads(journal.read_text(encoding='utf-8'))
                committed = self.conn.execute('SELECT 1 FROM flow_operations WHERE id=?', (record['operation'],)).fetchone()
                unresolved = False
                if not committed:
                    for item in record['files']:
                        path = checked_path(self.root, item['path'])
                        current = path.read_text(encoding='utf-8') if path.exists() else None
                        if current == item['before']:
                            continue
                        if current is not None and hashlib.sha256(current.encode()).hexdigest() == item['after']:
                            if item['before'] is None:
                                path.unlink()
                            else:
                                db.write_text_atomic(path, item['before'])
                        else:
                            unresolved = True
                            warnings.append({'path': str(path), 'error': '恢复期间文档已有另外修改，保留恢复记录，请核对后处理。'})
                if not unresolved:
                    journal.unlink()
        return warnings

    def version(self, identifier):
        row = self.conn.execute('SELECT * FROM flow_versions WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise ValueError('找不到流程版本')
        return dict(row, definition=json.loads(row['definition']))

    def active(self, flow, scope):
        row = self.conn.execute('SELECT version FROM flow_active WHERE flow=? AND scope IN (?,?) '
                                'ORDER BY scope=? DESC LIMIT 1', (flow, scope, '*', scope)).fetchone()
        if row is None:
            raise ValueError('找不到流程')
        return self.version(row['version'])

    def course(self, code):
        return self.tasks._resolve_course_row(code)

    def run(self, identifier):
        row = self.conn.execute('SELECT * FROM flow_runs WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise ValueError('找不到这次学习记录')
        return dict(row, history=json.loads(row['history']))

    def status(self, run):
        definition = self.version(run['version'])['definition']
        completed = {item['step'] for item in run['history']}
        available = [item for item in definition['steps'] if item['id'] not in completed
                     and set(item.get('after', [])) <= completed]
        return dict(run, title=definition['title'], available=available,
                    next=available[0] if len(available) == 1 else None)

    def agent_memory(self):
        root=self.root if self.root.name=='shared' and self.root.parent.name=='.semesters' else library_root(self.root)/'.semesters/shared'
        if root==self.root:
            value=self.learning.memory_status(self.root)
            path=Path(value['path']);value['content']=path.read_text(encoding='utf-8') if path.exists() else ''
            return value
        shared=Store(root,inherit=False)
        try:return shared.agent_memory()
        finally:shared.conn.close()

    def read(self, args):
        if args.get('run'):
            return self.status(self.run(args['run']))
        courses = [dict(row) for row in self.conn.execute('SELECT id,code,name FROM courses ORDER BY code')]
        memory = self.agent_memory()
        legacy=self.root/'.study-system/AGENT_MEMORY.md'
        legacy_memory=legacy.read_text(encoding='utf-8') if legacy.exists() and str(legacy)!=memory['path'] else ''
        shared=Store(library_root(self.root)/'.semesters/shared',inherit=False)
        try: learning_flows={r['flow']:shared.version(r['version']) for r in shared.conn.execute("SELECT flow,version FROM flow_active WHERE scope='*'")}
        finally:shared.conn.close()
        from transcription import Transcription
        transcription=Transcription(self.root).status()
        restored=self.root/'.study-system/restored-references.json'
        restored_references=json.loads(restored.read_text(encoding='utf-8')) if restored.exists() else {}
        result = {'legacy_semester_memory':legacy_memory,'restored_references':restored_references,'transcription':transcription,'agreements':read_all(self.root,args.get('course')), 'learning_flows':learning_flows, 'courses': courses, 'memory': memory, 'recovery_warnings': self.recovery_warnings,
            'flows': {flow: self.active(flow, '*') for flow in json.loads((PROJECT / 'packages/agent/flows.json').read_text(encoding='utf-8'))}, 'guide': {
            'study_command': ['study_work', 'action=service'],
            'service_tool': 'study_work action=service, input={arguments:[服务参数]}; 先用 arguments=[--help] 或具体子命令的 --help 查看真实用法，无需终端。',
            'standards': delivery.routes(),
            'implementation': str(PROJECT / 'packages/agent/worker.py'),
            'operations': str(PROJECT / 'packages/agent/services.py'),
            'note': '任务、周池、今日计划、复测、记忆、备份和课程核对使用现有 study 命令与同一账本。学习判断依据课程约定与相应标准。'
        }}
        if args.get('session'):
            result['pending'] = []
            for row in self.conn.execute("SELECT id FROM flow_runs WHERE session=? AND status='open'",
                                         (args['session'],)):
                run = self.status(self.run(row['id']))
                result['pending'].append({key: run[key] for key in ('id', 'course', 'title', 'available')})
        if args.get('course'):
            course = self.course(args['course'])
            result['runs'] = [self.status(self.run(row['id'])) for row in self.conn.execute(
                'SELECT id FROM flow_runs WHERE course=? ORDER BY rowid', (course['id'],))]
            result['tasks'] = [dict(row) for row in self.conn.execute('SELECT * FROM tasks WHERE course_id=?', (course['id'],))]
            result['issues'] = [dict(row) for row in self.conn.execute(
                'SELECT * FROM learning_issues WHERE course_id=? ORDER BY created_at', (course['id'],))]
            result['flows'] = {flow: self.active(flow, course['id']) for flow in json.loads(
                (PROJECT / 'packages/agent/flows.json').read_text(encoding='utf-8'))}
            folder = self.learning.course_roots(self.root)[course['id']][1]
            if (folder / 'COURSE.md').exists():
                result['course_context'] = (folder / 'COURSE.md').read_text(encoding='utf-8')
                result['course_state'] = self.learning.course_state_status(course['id'], folder / 'COURSE.md')
            result['assessments'] = [dict(row) for row in self.conn.execute('SELECT * FROM assessment_attempts WHERE course_id=? ORDER BY created_at', (course['id'],))]
            result['retest_results'] = [dict(row) for row in self.conn.execute('SELECT r.* FROM assessment_issue_results r JOIN learning_issues i ON i.id=r.issue_id WHERE i.course_id=? ORDER BY recorded_at', (course['id'],))]
            if not read_only(self.root):self.learning.scan_materials(self.root)
            result['materials'] = self.learning.pending_events(course['code'])
        if args.get('context') or args.get('day') or args.get('calendar'):
            result['context'] = SERVICE_HANDLERS['context'](self, {}, {}, args, '')
        return result

    def start(self, args):
        flow = text(args.get('flow'), '流程')
        base = self.active(flow, '*')
        course = None
        if base['definition'].get('scope', 'course') == 'course':
            code = text(args.get('course'), '课程').upper()
            if not re.fullmatch(r'[A-Za-z0-9_-]+', code):
                raise ValueError('课程代码只使用字母、数字、横线或下划线')
            row = self.conn.execute('SELECT * FROM courses WHERE code=?', (code,)).fetchone()
            if row is None:
                self.tasks.add_course(code, args.get('name') or code)
            if self.course(code)['id'] not in self.learning.course_roots(self.root):
                self.learning.setup_course_context(self.root, code)
            course = self.course(code)
        version = self.active(flow, course['id'] if course else '*')
        identifier = str(uuid.uuid4())
        self.conn.execute('INSERT INTO flow_runs(id,course,session,version) VALUES (?,?,?,?)',
                          (identifier, course['id'] if course else None, args.get('session') or '', version['id']))
        return self.status(self.run(identifier))

    def advance(self, args, operation):
        run = self.status(self.run(args['run']))
        if run['status'] != 'open' or not run['available']:
            raise ValueError('这次流程已完成')
        step = next((item for item in run['available'] if item['id'] == args.get('step')), None)
        if step is None:
            raise ValueError('操作缺少必要依据或已完成；可执行：' + ', '.join(item['id'] for item in run['available']))
        inputs = args.get('input', {})
        if not isinstance(inputs, dict):
            raise ValueError('步骤输入必须是对象')
        # A handler may add behavior, but does not replace the domain evidence checks.
        transformed = run_handler(step, inputs, {'course': run['course'], 'history': run['history']})
        result = HANDLERS[step['kind']](self, run, step, transformed, operation)
        history = run['history'] + [{'step': step['id'], 'kind': step['kind'], 'input': inputs,
                                     'result': result, 'version': run['version'], 'operation': operation}]
        completed = {item['step'] for item in history}
        definition = self.version(run['version'])['definition']
        status = 'completed' if all(item['id'] in completed for item in definition['steps']) else 'open'
        self.conn.execute('UPDATE flow_runs SET history=?,status=? WHERE id=?',
                          (encoded(history), status, run['id']))
        return self.status(self.run(run['id']))

    def change(self, args):
        flow = text(args.get('flow'), '流程')
        scope = self.course(args['course'])['id'] if args.get('course') else '*'
        current = self.active(flow, scope)
        if scope != '*' and current['definition'].get('scope') == 'workspace':
            raise ValueError('全局操作的流程修改使用全局范围')
        action = args.get('action')
        if action == 'preview':
            reason = text(args.get('reason'), '用户要求')
            definition = validate(args['definition'])
            if definition.get('scope', 'course') != current['definition'].get('scope', 'course'):
                raise ValueError('流程的账本范围不能在版本切换中改变')
            # Handlers are validated data-only functions, not trusted application
            # code. Test examples separately with a bounded execution timeout.
            with tempfile.TemporaryDirectory() as directory:
                checked = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--check'],
                    input=encoded(definition), text=True, capture_output=True, cwd=directory, timeout=5)
            if checked.returncode:
                raise ValueError('修改未通过验证：' + checked.stderr.strip())
            identifier = digest({'flow': flow, 'scope': scope, 'definition': definition, 'parent': current['id']})
            self.conn.execute('INSERT OR IGNORE INTO flow_versions VALUES (?,?,?,?,?,?)',
                (identifier, flow, scope, encoded(definition), current['id'], reason))
            difference = '\n'.join(difflib.unified_diff(
                json.dumps(current['definition'], ensure_ascii=False, indent=2).splitlines(),
                json.dumps(definition, ensure_ascii=False, indent=2).splitlines(), fromfile='当前流程', tofile='修改后'))
            return {'version': identifier, 'current': current['id'], 'diff': difference, 'reason': reason}
        if args.get('expected') != current['id']:
            raise ValueError('流程已有其他修改，请先重新读取')
        if action == 'apply':
            candidate = self.version(args['version'])
            if candidate['flow'] != flow or candidate['scope'] != scope or candidate['parent'] != current['id']:
                raise ValueError('候选修改不属于当前课程或版本')
        elif action == 'rollback':
            if not current['parent']:
                raise ValueError('没有更早的版本可恢复')
            candidate = self.version(current['parent'])
        else:
            raise ValueError('未知的修改操作')
        self.conn.execute('INSERT INTO flow_active VALUES (?,?,?) ON CONFLICT(flow,scope) '
                          'DO UPDATE SET version=excluded.version', (flow, scope, candidate['id']))
        return {'version': candidate['id'], 'definition': candidate['definition']}

    def switch(self, args):
        run = self.run(args['run'])
        if run['status'] != 'open':
            raise ValueError('已完成的学习记录不切换流程')
        reason = text(args.get('reason'), '切换依据')
        old = self.version(run['version'])
        latest = self.active(old['flow'], run['course'] or '*')
        # Preserve past results. A changed completed step is not silently reused.
        previous = {step['id']: step for step in old['definition']['steps']}
        completed = {item['step'] for item in run['history']}
        for step in latest['definition']['steps']:
            if step['id'] in completed and step != previous.get(step['id']):
                raise ValueError('已完成步骤的要求改变，请开始新流程；原证据会保留')
        self.conn.execute('UPDATE flow_runs SET version=? WHERE id=?', (latest['id'], run['id']))
        status = self.status(self.run(run['id']))
        if not status['available']:
            raise ValueError('新流程没有剩余步骤，请开始新流程')
        return dict(status, change_reason=reason)

    def execute(self, request):
        action = request['action']
        args = request.get('args', {})
        if args.get('semester'):
            target=Semesters(library_root(self.root)).directory(args['semester'])
            if target!=self.root:
                selected=Store(target)
                try:return selected.execute(dict(request,args={k:v for k,v in args.items() if k!='semester'}))
                finally:selected.conn.close()
        if action == 'read':
            if args.get('source'):
                paths={'operations':PROJECT/'packages/agent/services.py','implementation':PROJECT/'packages/agent/worker.py','desktop':PROJECT/'packages/agent/workspace.py'}
                path=paths.get(args['source'])
                if not path:raise ValueError('请选择 operations、implementation 或 desktop')
                lines=path.read_text(encoding='utf-8').splitlines()
                start=max(1,int(args.get('start',1)));end=min(len(lines),int(args.get('end',start+199)))
                return {'source':args['source'],'total_lines':len(lines),'start':start,'text':'\n'.join(lines[start-1:end])}
            if args.get('standard'):
                path=delivery.routes().get(args['standard'])
                if not path:raise ValueError('该规范不存在，请使用 standards 中的名称')
                return {'standard':args['standard'],'text':Path(path).read_text(encoding='utf-8')}
            return self.read(args)
        if read_only(self.root):raise ValueError('这个学期已归档，请重新开始后再修改')
        if action=='change' and args.get('scope')=='learning':
            if args.get('course'):raise ValueError('通用流程不属于某一门课程')
            shared=Store(library_root(self.root)/'.semesters/shared',inherit=False)
            try:return shared.execute(dict(request,args={k:v for k,v in args.items() if k!='scope'}))
            finally:shared.conn.close()
        operation = text(request.get('id'), '操作标识')
        fingerprint = digest({'action': action, 'args': args})
        if action=='service':
            arguments=args.get('arguments')
            if not isinstance(arguments,list) or not arguments or any(not isinstance(item,str) for item in arguments):raise ValueError('服务参数必须是非空文字列表')
            if any(item.split('=')[0] in ('--db','--workspace') for item in arguments):raise ValueError('学习服务只能操作当前选择的学期')
            journal=self.root/'.study-system/runtime/service'
            journal.mkdir(parents=True,exist_ok=True)
            import file_lock
            with (journal/'service.lock').open('a+') as lock:
                file_lock.flock(lock,file_lock.LOCK_EX)
                record=journal/(digest(operation)+'.json')
                if record.exists():
                    prior=json.loads(record.read_text(encoding='utf-8'))
                    if prior['fingerprint']!=fingerprint:raise ValueError('同一服务操作不能更改参数')
                    if prior.get('result') is not None:return prior['result']
                    raise ValueError('上次服务操作可能已经完成，请先读取实际记录；不要重复创建任务或证据')
                db.write_text_atomic(record,encoded({'fingerprint':fingerprint,'status':'started'}))
                from study_agent.cli import main as service_main
                output=io.StringIO();previous=Path.cwd()
                try:
                    os.chdir(self.root)
                    with redirect_stdout(output),redirect_stderr(output):
                        try:code=service_main(['--db',str(self.root/'.study-system/data/task_memory.sqlite3'),'--json',*arguments])
                        except SystemExit as stopped:code=stopped.code or 0
                finally:os.chdir(previous)
                value=output.getvalue()
                if code:raise ValueError(value.strip() or '学习服务未完成，请先核对实际记录')
                try:result={'data':json.loads(value)}
                except ValueError:result={'text':value}
                db.write_text_atomic(record,encoded({'fingerprint':fingerprint,'status':'completed','result':result}))
                return result
        try:
            with db.atomic(self.conn):
                prior = self.conn.execute('SELECT * FROM flow_operations WHERE id=?', (operation,)).fetchone()
                if prior:
                    if prior['fingerprint'] != fingerprint:
                        raise ValueError('同一操作不能使用不同输入')
                    return json.loads(prior['result'])
                if action == 'start':
                    result = self.start(args)
                elif action == 'memory_save':
                    result=SERVICE_HANDLERS['memory'](self,{}, {},args,operation)
                elif action == 'advance':
                    result = self.advance(args, operation)
                elif action == 'change':
                    result = self.change(args)
                elif action == 'switch':
                    result = self.switch(args)
                else:
                    raise ValueError('未知操作')
                self.conn.execute('INSERT INTO flow_operations VALUES (?,?,?)', (operation, fingerprint, encoded(result)))
        except BaseException:
            self.recovery_warnings = self.recover_files()
            raise
        warnings = self.cleanup()
        if warnings:
            result = dict(result, recovery_warnings=warnings)
        return result

    def publish_views(self):
        # Recover an interrupted publication from the committed latest plan.
        # Earlier plans cannot overwrite a newer checklist.
        with db.atomic(self.conn, bump_revision=False):
            latest, latest_order = None, -1
            for row in self.conn.execute('SELECT id,rowid FROM flow_runs'):
                run = self.run(row['id'])
                item = next((item for item in reversed(run['history']) if item['kind'] == 'daily'), None)
                if item:
                    operation = self.conn.execute('SELECT rowid FROM flow_operations WHERE id=?',
                                                  (item.get('operation'),)).fetchone()
                    order = operation['rowid'] if operation else row['rowid']
                    if order > latest_order:
                        latest, latest_order = (run, item), order
            if not latest:
                return
            run, item = latest
            marker = self.root / '.study-system/runtime/plan-views' / (run['id'] + '.json')
            if marker.exists() and json.loads(marker.read_text(encoding='utf-8')).get('operation') == item.get('operation'):
                return
            result = item['result']
            if (self.root / 'TODAY_WORKING.md').exists() and (self.root / '.study-system/runtime/views/TODAY_WORKING.state.json').exists():
                synced = sync_working_v4(self.conn, self.root / 'TODAY_WORKING.md')
                if synced['applied']:
                    self.conn.execute('UPDATE planning_state SET revision=revision+1 WHERE singleton=1')
            database = str(self.root / '.study-system/data/task_memory.sqlite3')
            for kind, filename, target in [('working', 'TODAY_WORKING.md', result['day']),
                    ('week', 'WEEK.md', result['week']), ('backlog', 'BACKLOG.md', result['day'])]:
                write_learning_view(self.conn, kind, self.root / filename, target, database)
            db.write_text_atomic(marker, encoded({'run': run['id'], 'day': result['day'], 'operation': item.get('operation')}))

    def backup_ledger(self):
        # Use the existing backup routine, reading through another connection
        # while the write lock keeps the committed snapshot stable.
        with db.atomic(self.conn, bump_revision=False):
            revision = db.planning_revision(self.conn)
            folder = self.root / '.study-system/backups'
            target, marker = folder / 'latest.sqlite3', folder / 'latest.state.json'
            if target.exists() and marker.exists() and json.loads(marker.read_text(encoding='utf-8')).get('revision') == revision:
                return
            source = sqlite3.connect((self.root / '.study-system/data/task_memory.sqlite3').as_uri() + '?mode=ro', uri=True)
            try:
                db.backup(source, target)
            finally:
                source.close()
            db.write_text_atomic(marker, encoded({'revision': revision}))

    def cleanup(self):
        warnings = self.recover_files()
        try:
            self.publish_views()
        except Exception as error:
            warnings.append({'error': '计划已保存，清单生成待恢复：' + str(error)})
        try:
            self.backup_ledger()
        except Exception as error:
            warnings.append({'error': '账本已保存，备份待恢复：' + str(error)})
        for row in self.conn.execute("SELECT id FROM flow_runs WHERE status='completed'"):
            run = self.run(row['id'])
            for item in run['history']:
                result = item['result']
                if item['kind'] == 'review' and Path(result['work']).exists():
                    try:
                        finish_work(self.root, result['work'], result['artifact'], result['receipt'],
                                    trash_root=self.root / '.study-system/retired')
                    except Exception as error:
                        warnings.append({'run': run['id'], 'error': str(error)})
        return warnings


def feedback(store, run, step, inputs, operation):
    answers = text(inputs.get('answers'), '原始作答')
    summary = text(inputs.get('summary'), '批改说明')
    evidence = store.save_evidence(operation, {'answers': answers, 'summary': summary, 'source': inputs.get('source')})
    issues = inputs.get('issues', [])
    for issue in issues:
        text(issue.get('concept'), '具体错点')
    attempt, identifiers = AssessmentService(store.conn).record_assessment({
        'course': run['course'], 'source_kind': inputs.get('source_kind', 'other'),
        'summary': summary, 'evidence_ref': str(evidence), 'issues': issues,
        'score_text': inputs.get('score'),
    })
    return {'attempt': attempt, 'issues': identifiers, 'evidence': str(evidence)}


def plan(store, run, step, inputs, operation):
    course = store.course(run['course'])
    selected = inputs.get('issues', [])
    for identifier in selected:
        row = store.conn.execute('SELECT course_id FROM learning_issues WHERE id=?', (identifier,)).fetchone()
        if row is None or row['course_id'] != run['course']:
            raise ValueError('选题引用了不存在或其他课程的错点')
    proposal = text(inputs.get('plan'), '选题草案')
    path = store.root / '.study-system/PRACTICE_SELECTION.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    content = path.read_text(encoding='utf-8') if path.exists() else ''
    section = '## ' + course['code'] + '\n\n' + proposal + '\n\n错点依据：' + ', '.join(selected) + '\n'
    headings = list(re.finditer(r'^## (.+)$', content, re.M))
    for index, match in enumerate(headings):
        if match[1] == course['code']:
            end = headings[index + 1].start() if index + 1 < len(headings) else len(content)
            content = content[:match.start()] + section + content[end:]
            break
    else:
        content += '\n' + section
    db.write_text_atomic(path, content)
    work = store.root / '.study-system/runtime' / run['id']
    marker = work / '.delivery.json'
    if marker.exists():
        saved = json.loads(marker.read_text(encoding='utf-8'))
        if saved['preparation'] != section.strip() or saved['course_sha256'] != delivery.digest(store.root / course['code'] / 'COURSE.md'):
            raise ValueError('已有写作准备不同，请重新开始，保留原准备记录')
    else:
        delivery.prepare(store.root, work, course['code'], 'worksheet', path)
    return {'plan': proposal, 'issues': selected, 'work': str(work)}


def review(store, run, step, inputs, operation):
    planned = next((item['result'] for item in reversed(run['history']) if item['kind'] == 'plan'), None)
    if planned is None:
        raise ValueError('先保存选题草案')
    artifact = checked_path(store.root, inputs.get('artifact'))
    receipt = store.root / '.study-system/evidence' / (run['id'] + '-' + step['id'] + '-delivery.json')
    delivery.accept(store.root, planned['work'], artifact, receipt, inputs['review'])
    return {'artifact': str(artifact), 'receipt': str(receipt), 'work': planned['work'],
            'issued_only': True}


def task(store, run, step, inputs, operation):
    if inputs.get('task'):
        existing = store.tasks.get_task(inputs['task'])
        if existing.course_id != run['course'] or existing.status != 'open':
            raise ValueError('只能接续本课未完成的任务')
        return {'task': existing.task_key}
    created = store.tasks.create_task(step['task'], course=run['course'], status='open',
        actor=inputs.get('actor', 'student'), description=text(inputs.get('description'), '剩余动作'))
    return {'task': created.task_key}


def tasks(store, run, step, inputs, operation):
    proposals = inputs.get('tasks')
    if not isinstance(proposals, list):
        raise ValueError('按用户需求提供任务列表；无需任务时用空列表并说明原因')
    reason = text(inputs.get('reason'), '任务安排依据')
    identifiers = []
    for proposal in proposals:
        identifiers.append(task(store, run, dict(step, task=text(proposal.get('title'), '任务名称')),
                                proposal, operation)['task'])
    return {'tasks': identifiers, 'reason': reason}


def inspect(store, run, step, inputs, operation):
    text(inputs.get('summary'), '材料检查说明')
    store.learning.scan_materials(store.root)
    event = store.conn.execute('SELECT course_id FROM material_events WHERE id=?', (inputs['event'],)).fetchone()
    if event is None or event['course_id'] != run['course']:
        raise ValueError('只能处理本课材料')
    return store.learning.classify_material(inputs['event'], inputs['kind'],
        reason=inputs['summary'], session=inputs.get('session'))


def receipt(store, run, step, inputs, operation):
    inspected = next((item['result'] for item in reversed(run['history']) if item['kind'] == 'inspect'), None)
    if inspected is None:
        raise ValueError('先读取并分类材料')
    values = dict(inputs)
    notes = values.get('lecture_notes_path')
    if notes:
        values['lecture_notes_path'] = str(checked_path(store.root, notes))
    values.setdefault('lecture_notes_path', None)
    values.setdefault('operator', 'study')
    return store.learning.close_event(inspected['id'], **values)


def custom(store, run, step, inputs, operation):
    return inputs


from services import HANDLERS as SERVICE_HANDLERS

HANDLERS = {'feedback': feedback, 'plan': plan, 'review': review, 'task': task, 'tasks': tasks, 'inspect': inspect, 'receipt': receipt, 'custom': custom}

HANDLERS.update(SERVICE_HANDLERS)

def main():
    request = json.load(sys.stdin)
    if sys.argv[1:] == ['--check']:
        check_handlers(request)
        return
    store = Store(sys.argv[1])
    try:
        warnings = [] if read_only(store.root) else store.cleanup()
        result = store.execute(request)
        if warnings:
            result = dict(result, recovery_warnings=warnings)
        print(encoded({'ok': True, 'result': result}))
    finally:
        store.conn.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        if sys.argv[1:] == ['--check']:
            print(str(error), file=sys.stderr)
            sys.exit(1)
        print(encoded({'ok': False, 'error': str(error)}))
        sys.exit(1)
