"""Persistent desktop over the existing ledger; immutable file versions and real PDFs."""
from __future__ import annotations
import base64
import codecs
import unicodedata
import hashlib
import json
import mimetypes
import os
import pdf_tools
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / 'packages/learning/src'))
from study_agent import db, delivery
from study_agent.service import TaskService
from study_agent.models import dataclass_dict
from study_agent.learning import LearningService
from agreements import read_all, read_only, library_root, save as save_agreement
from semesters import Semesters

MAX_FILE = 100 * 1024 * 1024
MAX_AUDIO = 200 * 1024 * 1024
CATEGORIES={"materials":"课程资料","notes":"笔记","practice":"练习","assignments":"作业"}

def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

def uid():
    return uuid.uuid4().hex

def name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 180:
        raise ValueError('名称不能为空且不能超过 180 字')
    return value.strip()

class Workspace:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.blobs = self.root / '.workspace/blobs'
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.conn = db.initialize(self.root / '.study-system/data/task_memory.sqlite3')
        self.tasks = TaskService(self.conn)
        self.conn.executescript('''
          CREATE TABLE IF NOT EXISTS desktop_publications (path TEXT PRIMARY KEY, file TEXT NOT NULL, desired TEXT NOT NULL, published TEXT);
          CREATE TABLE IF NOT EXISTS desktop_retirements (path TEXT PRIMARY KEY, destination TEXT NOT NULL, sha256 TEXT);
          CREATE TABLE IF NOT EXISTS desktop_meta (id INTEGER PRIMARY KEY, version INTEGER NOT NULL);
          INSERT OR IGNORE INTO desktop_meta VALUES (1,0);
          CREATE TABLE IF NOT EXISTS desktop_files (id TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS desktop_folders (id TEXT PRIMARY KEY, name TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS desktop_history (id TEXT PRIMARY KEY, file TEXT NOT NULL, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS desktop_operations (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS desktop_task_files (task TEXT PRIMARY KEY REFERENCES tasks(id), file TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS desktop_messages (id TEXT PRIMARY KEY, channel TEXT NOT NULL, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS desktop_drafts (channel TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS desktop_answers (id TEXT PRIMARY KEY, file TEXT NOT NULL, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS desktop_compiles (id TEXT PRIMARY KEY, file TEXT NOT NULL, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS desktop_preparations (id TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS desktop_views (compile TEXT NOT NULL, page INTEGER NOT NULL, PRIMARY KEY(compile,page));
        ''')
        self.migrate_folders()
        self.publication_warnings=[] if read_only(self.root) else self.sync_publications()

    def sync_publications(self):
        with db.atomic(self.conn,bump_revision=False):
            return self._sync_publications()

    def _sync_publications(self):
        warnings=[]
        for row in self.conn.execute('SELECT * FROM desktop_publications').fetchall():
            target=(self.root/row['path']).resolve()
            if not target.is_relative_to(self.root) or target.is_relative_to(self.root/'.workspace'):
                warnings.append('课程文件路径变化，保留恢复记录');continue
            actual=hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
            if actual==row['desired']:
                self.conn.execute('UPDATE desktop_publications SET published=desired WHERE path=?',(row['path'],));continue
            if actual!=row['published']:
                warnings.append('课程文件在桌面之外被修改：'+row['path']);continue
            try:
                target.parent.mkdir(parents=True,exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as tmp:
                    tmp.write((self.blobs/row['desired']).read_bytes());temp=Path(tmp.name)
                temp.replace(target)
                self.conn.execute('UPDATE desktop_publications SET published=desired WHERE path=?',(row['path'],))
            except OSError as error:warnings.append(str(error))
        for row in self.conn.execute('SELECT * FROM desktop_retirements').fetchall():
            destination=self.conn.execute('SELECT desired,published FROM desktop_publications WHERE path=?',(row['destination'],)).fetchone()
            if not destination or destination['desired']!=destination['published']:continue
            previous=(self.root/row['path']).resolve()
            if not previous.is_relative_to(self.root) or previous.is_relative_to(self.blobs):
                warnings.append('旧文件路径变化，保留恢复记录');continue
            if previous.exists():
                target=self.root/row['destination']
                if target.exists() and previous.samefile(target):
                    try:previous.rename(target)
                    except OSError:
                        warnings.append('文件名称未能更新，原件已保留：'+row['path']);continue
                    self.conn.execute('DELETE FROM desktop_retirements WHERE path=?',(row['path'],));continue
                if not previous.is_file() or hashlib.sha256(previous.read_bytes()).hexdigest()!=row['sha256']:
                    warnings.append('移动前的原文件已在软件外修改，另行保留：'+row['path']);continue
                try:previous.unlink()
                except OSError:warnings.append('旧文件未能移除，已保留两份：'+row['path']);continue
            self.conn.execute('DELETE FROM desktop_retirements WHERE path=?',(row['path'],))
        return warnings

    def close(self):
        self.conn.close()

    def version(self):
        return self.conn.execute('SELECT version FROM desktop_meta WHERE id=1').fetchone()[0]

    def bump(self):
        self.conn.execute('UPDATE desktop_meta SET version=version+1 WHERE id=1')

    def file(self, identifier):
        row = self.conn.execute('SELECT value FROM desktop_files WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise ValueError('文件不存在')
        return json.loads(row[0])

    def content(self, file):
        return (self.blobs / file['sha256']).read_bytes()

    def blob(self, content, limit=MAX_FILE):
        if len(content) > limit:
            raise ValueError('文件超过 '+str(limit//1024//1024)+' MB')
        digest = hashlib.sha256(content).hexdigest()
        path = self.blobs / digest
        if not path.exists():
            # Write before the DB reference. An interrupted orphan is harmless; committed bytes are immutable.
            with tempfile.NamedTemporaryFile(dir=self.blobs, delete=False) as tmp:
                tmp.write(content)
                tmp.flush()
                import os
                os.fsync(tmp.fileno())
                temp = Path(tmp.name)
            temp.replace(path)
        return digest

    def public(self, file, *, include_text=True):
        result = dict(file, source='/api/file/' + file['id'], url='/api/file/' + file['id'] + '?v=' + file['sha256'])
        result['textual'] = file['mime'].startswith('text/') or Path(file['name']).suffix.lower() in ('.md', '.markdown', '.tex', '.txt')
        if include_text and result['textual']:
            try:
                result['text'] = self.text_content(file)
            except ValueError as error:
                result['text_error'] = str(error)
        return result

    def text_content(self, file):
        content = self.content(file)
        encoding = 'utf-8-sig'
        if content.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
            encoding = 'utf-32'
        elif content.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            encoding = 'utf-16'
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            raise ValueError('无法确认文字编码。原文件已保留；请将文本另存为 UTF-8 后重新上传。') from None

    def save_file(self, file):
        self.conn.execute('INSERT OR REPLACE INTO desktop_files VALUES (?,?)', (file['id'], encoded(file)))
        self.conn.execute('INSERT INTO desktop_history VALUES (?,?,?)', (uid(), file['id'], encoded(file)))

    def snapshot(self, *, include_text=True):
        files = [self.public(json.loads(row[0]), include_text=include_text) for row in self.conn.execute('SELECT value FROM desktop_files ORDER BY rowid')]
        trash=[f for f in files if f.get('removed')]
        files=[f for f in files if not f.get('removed')]
        files.sort(key=lambda file: file.get('order', 0))
        links = dict(self.conn.execute('SELECT task,file FROM desktop_task_files'))
        paths={key:f['id'] for f in files for key in ([f.get('course_path'),str(self.root/f['course_path'])] if f.get('course_path') else [])}
        paths.update({'desktop:'+f['id']:f['id'] for f in files})
        placements={item.task_id:item for item in self.tasks.list_placements()}
        tasks = [dict(id=t.id, title=t.title, file=links.get(t.id) or paths.get(t.source), done=t.status=='completed', status=t.status, actor=t.actor, course=t.course_id,due_at=t.due_at,scheduled_date=placements[t.id].scheduled_date if t.id in placements else None,placement=placements[t.id].kind if t.id in placements else None) for t in self.tasks.list_tasks() if t.status in ('open','proposed','completed')]
        messages = {}
        for row in self.conn.execute('SELECT channel,value FROM desktop_messages ORDER BY rowid'):
            messages.setdefault(row[0], []).append(json.loads(row[1]))
        answers = {}
        for row in self.conn.execute('SELECT file,value FROM desktop_answers ORDER BY rowid'):
            answers[row[0]] = json.loads(row[1])['text']
        return dict(trash=trash,agreements=read_all(self.root),courses=[dict(r) for r in self.conn.execute('SELECT id,code,name FROM courses ORDER BY code')],warnings=self.publication_warnings,version=self.version(), files=files, folders=[dict(row) for row in self.conn.execute('SELECT * FROM desktop_folders ORDER BY rowid')], tasks=tasks, messages=messages, drafts={r[0]:json.loads(r[1]) for r in self.conn.execute('SELECT * FROM desktop_drafts')}, answers=answers)

    def perform(self, action, args, operation=''):
        if action=='semester':
            library=Semesters(library_root(self.root))
            operation_name=args.get('action','list')
            if operation_name=='list':return library.load()
            if not str(args.get('reason','')).strip():raise ValueError('请保留用户要求与整理依据')
            if operation_name=='switch':raise ValueError('用 select 在本次处理结束后切换')
            return library.change(operation_name,dict(args,activate=False),operation)
        if args.get('semester'):
            target=Semesters(library_root(self.root)).directory(args['semester'])
            if target!=self.root:
                store=Workspace(target)
                try:return store.perform(action,{k:v for k,v in args.items() if k!='semester'},operation)
                finally:store.close()
        if action=='carry':
            file=self.file(args['file'])
            target=Semesters(library_root(self.root)).directory(args['target'])
            if target==self.root or not str(args.get('reason','')).strip():raise ValueError('请选择不同学期并保存沿用依据')
            store=Workspace(target)
            try:
                return store.perform('create',{'name':args.get('name') or file['name'],'mime':file['mime'],'data':base64.b64encode(self.content(file)).decode(),'folder':args.get('folder'),'kind':file['kind'],'origin':{'semester':self.root.name if self.root.parent.name=='.semesters' else 'initial','file':file['id'],'sha256':file['sha256'],'reason':args['reason']}},operation)
            finally:store.close()
        if action == 'textbook':
            from textbooks import perform
            return perform(self, args)
        if read_only(self.root) and action not in ('list','read','history','page'):
            raise ValueError('这个学期已归档，请重新开始后再修改')
        if action == 'list':
            return self.snapshot()
        if action == 'read':
            file = self.file(args['file'])
            if args.get('expected', file['sha256']) != file['sha256']:
                raise ValueError('文件版本已变化，请重新读取')
            result = self.public(file)
            if result.get('text_error'):raise ValueError(result['text_error'])
            result['stored_path']=str(self.blobs/file['sha256'])
            if file['mime'] == 'application/pdf':
                offset, limit = args.get('offset', 0), args.get('limit', 100000)
                if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100000:
                    raise ValueError('PDF 文字起点须为非负整数，每次读取 1 到 100000 字')
                with tempfile.TemporaryDirectory() as directory:
                    output = Path(directory) / 'text.txt'
                    if os.environ.get('STUDY_NATIVE')=='1' or os.environ.get('STUDY_PDFIUM')=='1':content=pdf_tools.text(self.blobs/file['sha256'])
                    else:
                        subprocess.run(['pdftotext', str(self.blobs/file['sha256']), str(output)], check=True, timeout=30, capture_output=True)
                        content = output.read_text(encoding='utf-8')
                if offset > len(content):
                    raise ValueError('文字起点超出 PDF 内容')
                end = min(offset + limit, len(content))
                result.update(text=content[offset:end], characters=len(content), offset=offset,
                              next_offset=end if end < len(content) else None, truncated=end < len(content),
                              warning='本次只读取了部分 PDF 文字，请用 next_offset 继续，或通过教材索引及原页定位。' if end < len(content) else None)
            if file['mime'] in ('image/png','image/jpeg','image/webp','image/gif'):
                result['images']=[{'data':base64.b64encode(self.content(file)).decode(),'mimeType':file['mime']}]
            return result
        if action == 'page':
            return self.page(args)
        if action == 'compile':
            return self.compile(args, operation)
        if action == 'history':
            return {'versions':[json.loads(row[0]) for row in self.conn.execute('SELECT value FROM desktop_history WHERE file=? ORDER BY rowid', (args['file'],))]}
        if action=='agreement':
            if args.get('scope')=='course':self.tasks._resolve_course_row(args['course'])
            return save_agreement(self.root,args,operation)
        fingerprint = hashlib.sha256(encoded([action,args]).encode()).hexdigest()
        with db.atomic(self.conn, bump_revision=action in ('task','check_task')):
            if operation:
                existing = self.conn.execute('SELECT fingerprint,result FROM desktop_operations WHERE id=?', (operation,)).fetchone()
                if existing:
                    if existing[0] != fingerprint:
                        raise ValueError('重复操作内容不同')
                    return json.loads(existing[1])
            result = self.mutate(action, args)
            if action not in ('draft','answer'):self.bump()
            if operation:
                self.conn.execute('INSERT INTO desktop_operations VALUES (?,?,?)', (operation, fingerprint, encoded(result)))
        warnings=self.sync_publications()
        if warnings:result['warnings']=warnings
        return result

    def mutate(self, action, args):
        if action=='course':
            import re
            code=str(args.get('code') or 'C'+uid()[:12]).upper()
            if not re.fullmatch(r'[A-Z0-9][A-Z0-9_-]{0,39}',code):raise ValueError('课程内部标识无效')
            if self.storage_name(code)!=code:raise ValueError('课程内部标识与系统保留名称冲突，请换一个标识')
            title=name(args['name'])
            existing=self.conn.execute('SELECT * FROM courses WHERE code=?',(code,)).fetchone()
            if existing and existing['name']!=title:raise ValueError('这个课程标识已被其他课程使用')
            if not existing:self.tasks.add_course(code,title)
            result=LearningService(self.conn).setup_course_context(self.root,code)
            identifier='course-'+code
            self.course_folder(code)
            categories={category:self.category_folder(code,category) for category in CATEGORIES}
            return dict(result,folder=identifier,folders=categories)
        if action in ('remove_file','restore_file'):
            file=self.file(args['file'])
            if args.get('expected')!=file['sha256']:raise ValueError('文件已变化，请重新读取')
            file['removed']=action=='remove_file'
            self.save_file(file)
            return self.public(file)
        if action == 'publish':
            file=self.file(args['file'])
            course=self.tasks._resolve_course_row(args['course'])
            if not (self.root/course['code']/'COURSE.md').is_file():raise ValueError('先建立并核对课程入口，再关联材料')
            relative=Path(args['path'])
            if relative.is_absolute() or '..' in relative.parts or not args.get('reason','').strip():raise ValueError('须给出课程内位置和实际整理依据')
            target=(self.root/course['code']/relative).resolve()
            if not target.is_relative_to(self.root/course['code']) or target==self.root/course['code']/'COURSE.md':raise ValueError('课程文件位置无效')
            path=target.relative_to(self.root).as_posix()
            saved=self.conn.execute('SELECT * FROM desktop_publications WHERE path=?',(path,)).fetchone()
            if saved and saved['file']!=file['id']:raise ValueError('此位置已有其他文件')
            if not saved and target.exists():raise ValueError('此位置已有课程文件，请先读取并保留原件')
            category=relative.parts[0] if relative.parts and relative.parts[0] in CATEGORIES else 'materials'
            folder=self.category_folder(course['code'],category)
            parts=relative.parts[1:-1] if relative.parts[0] in CATEGORIES else relative.parts[:-1]
            for part in parts:
                row=self.conn.execute('SELECT id FROM desktop_folders WHERE parent=? AND name=?',(folder,part)).fetchone()
                if row:folder=row['id']
                else:
                    child={'id':uid(),'name':part,'parent':folder};self.save_folder(child);folder=child['id']
            file.update(name=relative.name,course=course['code'],folder=folder,category=category,organization_reason=args['reason'])
            path=course['code']+'/'+self.folder_storage_path(folder)+'/'+self.storage_name(file['name'])
            self.save_file(file)
            self.register_publication(file,path)
            self.save_file(file)
            return self.public(file)
        if action == 'prepare':
            identifier=uid()
            work=self.root/'.study-system/runtime'/('pdf-work-'+identifier)
            brief=(self.root/args['brief']).resolve()
            preparation=delivery.prepare(self.root,work,args['course'],args['kind'],brief)
            record=dict(id=identifier,work=str(work),course=args['course'],kind=args['kind'],brief=str(brief.relative_to(self.root)))
            self.conn.execute('INSERT INTO desktop_preparations VALUES (?,?)',(identifier,encoded(record)))
            return dict(record,preparation=preparation)
        if action == 'create':
            filename = name(args['name'])
            content = args['text'].encode() if 'text' in args else base64.b64decode(args['data'], validate=True)
            mime = args.get('mime') or mimetypes.guess_type(filename)[0] or 'application/octet-stream'
            if Path(filename).suffix.lower() == '.tex': mime = 'text/x-tex'
            kind = args.get('kind', 'upload')
            if kind not in ('text','upload','note','practice','pdf'): raise ValueError('未知阅览方式')
            file = dict(id=uid(), name=filename, mime=mime, kind=kind, course=args.get('course') or None, folder=args.get('folder') or None, sha256=self.blob(content,MAX_AUDIO if mime.startswith('audio/') else MAX_FILE))
            self.check_folder(file['folder'])
            for source in args.get('sources',[]): self.file(source)
            file['sources'] = args.get('sources',[])
            file['session'] = args.get('session')
            if args.get('origin'):file['origin']=args['origin']
            if args.get('transcription'):file['transcription']=args['transcription']
            if args.get('preparation'):
                if not self.conn.execute('SELECT 1 FROM desktop_preparations WHERE id=?',(args['preparation'],)).fetchone():raise ValueError('制作依据不存在')
                file['preparation']=args['preparation']
            self.place_file(file,args)
            self.save_file(file)
            return self.public(file)
        if action in ('write', 'restore'):
            file = self.file(args['file'])
            if args.get('expected') != file['sha256']: raise ValueError('文件已变化，请重新读取后修改')
            if action == 'restore':
                versions = self.perform('history', {'file':file['id']})['versions']
                old = next((v for v in versions if v['sha256']==args['version']), None)
                if old is None: raise ValueError('找不到历史版本')
                file['sha256'] = old['sha256']
            else:
                file['sha256'] = self.blob(args['text'].encode())
            if file.get('pdf'): file['pdf']['stale'] = file['sha256']!=file['pdf']['source_sha256']
            self.save_file(file)
            self.conn.execute('UPDATE desktop_publications SET desired=? WHERE file=?',(file['sha256'],file['id']))
            return self.public(file)
        if action == 'folder':
            identifier = args.get('id') or uid()
            if not isinstance(identifier,str) or identifier=='unfiled' or len(identifier)>100:raise ValueError('文件夹身份无效')
            folder={'id':identifier,'name':name(args['name']),'parent':args.get('parent'),'category':args.get('category')}
            self.save_folder(folder)
            return dict(self.conn.execute('SELECT * FROM desktop_folders WHERE id=?',(identifier,)).fetchone())
        if action == 'metadata':
            if args.get('expected') != self.version(): raise ValueError('桌面已更新，请刷新后重试')
            for folder in args.get('folders',[]):self.save_folder(folder)
            for patch in args.get('files',[]):
                file = self.file(patch['id'])
                file.update({key:patch[key] for key in ('name','folder','order') if key in patch})
                file['name'] = name(file['name'])
                self.check_folder(file.get('folder'))
                self.place_file(file,{'folder':file.get('folder')},moving=True)
                self.save_file(file)
            if args.get('folders'):
                for (value,) in self.conn.execute('SELECT value FROM desktop_files').fetchall():
                    file=json.loads(value)
                    if file.get('course_path'):
                        self.place_file(file,{'folder':file.get('folder')},moving=True)
                        self.save_file(file)
            return {'saved':True}
        if action == 'task':
            file = self.file(args['file']) if args.get('file') else None
            task = self.tasks.create_task(name(args['title']), course=args.get('course') or (file.get('course') if file else None), actor='student', status='open', due_at=args.get('due_at'),source='desktop:'+file['id'] if file else None)
            day=args.get('date',self.tasks.clock.today().isoformat())
            if day:self.tasks.set_placement(task.id, kind='scheduled', scheduled_date=day, priority='if_time')
            if file: self.conn.execute('INSERT INTO desktop_task_files VALUES (?,?)',(task.id,file['id']))
            return dataclass_dict(task)
        if action == 'check_task':
            task = self.tasks.get_task(args['task'])
            if task.actor != 'student': raise ValueError('助理任务需要实际交付核验，不能由勾选代替')
            if args['done'] and task.status!='completed':
                self.tasks.add_evidence(task.id,'student_confirmation','用户在桌面确认已完成此任务',operator='student')
                task = self.tasks.complete(task.id,operator='student')
            elif not args['done'] and task.status=='completed':
                task = self.tasks.reopen(task.id,'用户取消完成勾选',placement={'kind':'scheduled','scheduled_date':self.tasks.clock.today().isoformat(),'priority':'if_time'},operator='student')
            return dataclass_dict(task)
        if action == 'answer':
            self.file(args['file'])
            answer = dict(id=uid(), text=args['text'], sources=args.get('sources',[]))
            self.conn.execute('INSERT INTO desktop_answers VALUES (?,?,?)',(answer['id'],args['file'],encoded(answer)))
            return answer
        if action == 'draft':
            self.conn.execute('INSERT OR REPLACE INTO desktop_drafts VALUES (?,?)',(args['channel'],encoded(args['value'])))
            return {'saved':True}
        if action == 'review':
            return self.review(args)
        raise ValueError('未知桌面操作：'+action)

    def migrate_folders(self):
        columns={row['name'] for row in self.conn.execute('PRAGMA table_info(desktop_folders)')}
        if 'parent' in columns:return
        with db.atomic(self.conn,bump_revision=False):
            for column in ('parent','course','category'):self.conn.execute('ALTER TABLE desktop_folders ADD COLUMN '+column+' TEXT')
            for row in self.conn.execute('SELECT code FROM courses').fetchall():self.course_folder(row['code'])
            # Use recorded purpose/course only; filenames do not establish a course.
            for row in self.conn.execute('SELECT value FROM desktop_files').fetchall():
                file=json.loads(row[0])
                if file.get('kind')=='practice' or file.get('preparation'):
                    try:self.place_file(file,{},moving=True);self.save_file(file)
                    except ValueError:pass
            self.bump()

    def course_folder(self,course):
        row=self.tasks._resolve_course_row(course)
        identifier='course-'+row['code']
        self.conn.execute('INSERT INTO desktop_folders (id,name,parent,course,category) VALUES (?,?,NULL,?,NULL) ON CONFLICT(id) DO UPDATE SET course=excluded.course,parent=NULL',(identifier,row['name'],row['code']))
        return identifier

    def category_folder(self,course,category):
        if category not in CATEGORIES:raise ValueError('请选择明确的材料分类')
        parent=self.course_folder(course)
        existing=self.conn.execute('SELECT id FROM desktop_folders WHERE course=? AND category=? AND parent IS NOT NULL ORDER BY rowid LIMIT 1',(course,category)).fetchone()
        if existing:return existing['id']
        identifier=parent+'-'+category
        self.conn.execute('INSERT INTO desktop_folders (id,name,parent,course,category) VALUES (?,?,?,?,?)',(identifier,CATEGORIES[category],parent,course,category))
        return identifier

    def folder_course(self,identifier):
        seen=set()
        while identifier:
            if identifier in seen:raise ValueError('文件夹不能循环嵌套')
            seen.add(identifier)
            row=self.conn.execute('SELECT * FROM desktop_folders WHERE id=?',(identifier,)).fetchone()
            if not row:raise ValueError('文件夹不存在')
            if row['course']:return row['course']
            identifier=row['parent']
        return None

    def save_folder(self,folder):
        identifier=folder['id']
        if not isinstance(identifier,str) or identifier=='unfiled' or len(identifier)>100:raise ValueError('文件夹身份无效')
        existing=self.conn.execute('SELECT * FROM desktop_folders WHERE id=?',(identifier,)).fetchone()
        parent=folder.get('parent',existing['parent'] if existing else None)
        current=parent;seen={identifier}
        while current:
            if current in seen:raise ValueError('文件夹不能包含自身或其上级')
            seen.add(current);row=self.conn.execute('SELECT * FROM desktop_folders WHERE id=?',(current,)).fetchone()
            if not row:raise ValueError('上级文件夹不存在')
            current=row['parent']
        category=folder.get('category',existing['category'] if existing else None)
        if category and category not in CATEGORIES:raise ValueError('文件夹用途无效')
        if existing and existing['course'] and not existing['parent'] and parent:raise ValueError('课程文件夹应位于桌面')
        course=self.folder_course(parent) if parent else existing['course'] if existing else None
        if existing and course!=existing['course']:raise ValueError('跨课程整理请移动文件，保留原课程文件夹')
        component=category or self.storage_name(name(folder['name']))
        for sibling in self.conn.execute('SELECT * FROM desktop_folders WHERE parent=? AND id!=?',(parent,identifier)):
            other=sibling['category'] or self.storage_name(sibling['name'])
            if self.path_key(component)==self.path_key(other):raise ValueError('此位置已有同名文件夹（不区分大小写），请使用不同名称')
        self.conn.execute('INSERT INTO desktop_folders (id,name,parent,course,category) VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,parent=excluded.parent,course=excluded.course,category=excluded.category',(identifier,name(folder['name']),parent,course,category))

    def place_file(self,file,args,moving=False):
        preparation=None
        if file.get('preparation'):
            row=self.conn.execute('SELECT value FROM desktop_preparations WHERE id=?',(file['preparation'],)).fetchone()
            if not row:raise ValueError('制作依据不存在')
            preparation=json.loads(row[0])
        category=args.get('category') or file.get('category') or ('practice' if file['kind']=='practice' else 'notes' if file['kind']=='note' or preparation and preparation['kind']=='note' else None)
        course=args.get('course') or file.get('course') or (preparation or {}).get('course') or self.folder_course(file.get('folder'))
        old_course=file.get('course')
        if moving and args.get('folder'):
            selected_course=self.folder_course(args['folder'])
            if selected_course:course=selected_course
        selected_row=self.conn.execute('SELECT * FROM desktop_folders WHERE id=?',(file.get('folder'),)).fetchone()
        if moving and selected_row:
            ancestor=selected_row
            while ancestor and not ancestor['category'] and ancestor['parent']:
                ancestor=self.conn.execute('SELECT * FROM desktop_folders WHERE id=?',(ancestor['parent'],)).fetchone()
            if ancestor and ancestor['category']:category=ancestor['category']
        if not category and selected_row and selected_row['category']:category=selected_row['category']
        if category and not course:raise ValueError('请先明确这份成果属于哪门课，再保存到课程内的分类文件夹')
        if moving and not category and args.get('folder'):
            course=self.folder_course(args['folder']) or course
        if course:
            course=self.tasks._resolve_course_row(course)['code']
            selected=file.get('folder');folder=self.conn.execute('SELECT * FROM desktop_folders WHERE id=?',(selected,)).fetchone() if selected else None
            if category:
                if folder and folder['course']==course and folder['parent']:
                    if not moving and folder['category'] and folder['category']!=category:raise ValueError('成果用途与所选分类不符')
                else:
                    if moving and selected and self.folder_course(selected)!=course:raise ValueError('请将成果保存在所属课程的分类文件夹；跨课使用可以复制')
                    file['folder']=self.category_folder(course,category)
            elif not folder or not folder['parent']:
                file['folder']=self.category_folder(course,'materials')
            file['course']=course
            file['category']=category or 'materials'
            self.register_publication(file,course+'/'+self.folder_storage_path(file['folder'])+'/'+self.storage_name(file['name']))
            if moving and old_course!=course:
                course_id=self.tasks._resolve_course_row(course)['id']
                for (task_id,) in self.conn.execute('SELECT task FROM desktop_task_files WHERE file=?',(file['id'],)).fetchall():
                    task=self.tasks.get_task(task_id)
                    if task.status in ('open','proposed') and task.course_id!=course_id:self.tasks.update_task(task_id,course=course,operator='student')

    @staticmethod
    def storage_name(value):
        import re
        value=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',value).rstrip(' .') or '文件'
        if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?',value):value='_'+value
        return value

    def folder_storage_path(self,identifier):
        parts=[]
        while identifier:
            folder=self.conn.execute('SELECT * FROM desktop_folders WHERE id=?',(identifier,)).fetchone()
            if not folder or not folder['parent']:break
            parts.insert(0,folder['category'] if folder['category'] else self.storage_name(folder['name']))
            identifier=folder['parent']
        return '/'.join(parts) or 'materials'

    @staticmethod
    def path_key(value):
        return unicodedata.normalize('NFC',value).casefold()

    def register_publication(self,file,path):
        relative=Path(path)
        if relative.is_absolute() or '..' in relative.parts:raise ValueError('课程文件位置无效')
        path=relative.as_posix()
        matches=[row for row in self.conn.execute('SELECT * FROM desktop_publications') if self.path_key(row['path'])==self.path_key(path)]
        if any(row['file']!=file['id'] for row in matches):raise ValueError('分类中已有同名成果（不区分大小写），请先读取再修订，或使用不同名称')
        target=self.root/relative
        tracked_alias=any((self.root/row['path']).exists() and target.exists() and (self.root/row['path']).samefile(target) for row in matches)
        if target.exists() and not tracked_alias:raise ValueError('此位置已有原文件，请先核对，不能覆盖')
        for previous in self.conn.execute('SELECT * FROM desktop_publications WHERE file=? AND path!=?',(file['id'],path)).fetchall():
            self.conn.execute('INSERT OR REPLACE INTO desktop_retirements VALUES (?,?,?)',(previous['path'],path,previous['published']))
            self.conn.execute('DELETE FROM desktop_publications WHERE path=?',(previous['path'],))
        file['course_path']=path
        self.conn.execute('INSERT INTO desktop_publications VALUES (?,?,?,NULL) ON CONFLICT(path) DO UPDATE SET desired=excluded.desired',(path,file['id'],file['sha256']))

    def check_folder(self, identifier):
        if identifier and not self.conn.execute('SELECT 1 FROM desktop_folders WHERE id=?',(identifier,)).fetchone():
            raise ValueError('文件夹不存在')

    def message(self, identifier, channel, value):
        with db.atomic(self.conn, bump_revision=False):
            self.conn.execute('INSERT OR IGNORE INTO desktop_messages VALUES (?,?,?)',(identifier,channel,encoded(value)))

    def compile(self, args, operation):
        file = self.file(args['file'])
        if args.get('expected') != file['sha256']: raise ValueError('源码已变化，请重新读取')
        if Path(file['name']).suffix.lower() != '.tex': raise ValueError('请选择 LaTeX 源文件')
        fingerprint = hashlib.sha256(encoded(['compile',args]).encode()).hexdigest()
        if operation:
            saved = self.conn.execute('SELECT fingerprint,result FROM desktop_operations WHERE id=?',(operation,)).fetchone()
            if saved:
                if saved[0]!=fingerprint: raise ValueError('重复编译内容不同')
                return json.loads(saved[1])
        identifier = uid()
        prepared={}
        if file.get('preparation'):
            row=self.conn.execute('SELECT value FROM desktop_preparations WHERE id=?',(file['preparation'],)).fetchone()
            prepared=json.loads(row[0])
        if args.get('kind') in ('note','worksheet') and not prepared:raise ValueError('正式交付须先 prepare 保存制作依据，再写作')
        work = Path(prepared['work']) if prepared else self.root / '.study-system/runtime' / ('compile-'+identifier)
        work.mkdir(parents=True,exist_ok=True)
        (work/'main.tex').write_text(self.text_content(file),encoding='utf-8')
        # Explicit companion files only. Never infer dependencies or read personal data.
        for item in args.get('assets',[]):
            asset = self.file(item['file'])
            destination = Path(item['path'])
            if destination.is_absolute() or '..' in destination.parts or str(destination) in ('main.tex','main.pdf'):
                raise ValueError('附件路径无效')
            target = work/destination
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(self.content(asset))
        env = dict(__import__('os').environ, openin_any='p', openout_any='p')
        try:
            if os.environ.get('STUDY_NATIVE')=='1':
                for unused in range(3):
                    result=subprocess.run(['xelatex','-no-pdf','-interaction=nonstopmode','-halt-on-error','-no-shell-escape','main.tex'],cwd=work,env=env,capture_output=True,timeout=30)
                    if result.returncode:break
                if not result.returncode:
                    # Invoke the driver ourselves: XeTeX's shell command breaks inside
                    # an app path such as "Study Agent.app" or Windows Program Files.
                    driver=subprocess.run(['xdvipdfmx','-o','main.pdf','main.xdv'],cwd=work,env=env,capture_output=True,timeout=30)
                    driver.stdout=result.stdout+driver.stdout;driver.stderr=result.stderr+driver.stderr;result=driver
            else:result = subprocess.run(['latexmk','-norc','-xelatex','-interaction=nonstopmode','-halt-on-error','-no-shell-escape','main.tex'],cwd=work,env=env,capture_output=True,timeout=90)
            log = (result.stdout+result.stderr).decode(errors='replace')[-20000:]
            ok = result.returncode==0 and (work/'main.pdf').is_file()
        except (subprocess.TimeoutExpired,FileNotFoundError) as error:
            ok,log = False,str(error)
        record = dict(kind=prepared.get('kind'),course=prepared.get('course'),brief=prepared.get('brief'),id=identifier,file=file['id'],source_sha256=file['sha256'],ok=ok,log=log,work=str(work))
        if ok:
            try:
                if os.environ.get('STUDY_NATIVE')=='1' or os.environ.get('STUDY_PDFIUM')=='1':count=pdf_tools.pages(work/'main.pdf')
                else:
                    info = subprocess.run(['pdfinfo',str(work/'main.pdf')],env=dict(env,LC_ALL='C'),capture_output=True,text=True,check=True,timeout=15).stdout
                    count = next(int(line.split(':')[1]) for line in info.splitlines() if line.startswith('Pages:'))
                record.update(sha256=self.blob((work/'main.pdf').read_bytes()),pages=count,accepted=False)
            except (OSError,subprocess.SubprocessError,StopIteration,ValueError):
                record.update(ok=False,log='PDF 核验未完成，上一版仍保留；请检查 PDF 读取工具后重试。')
        with db.atomic(self.conn, bump_revision=False):
            current = self.file(file['id'])
            if current['sha256']!=file['sha256']:
                record.update(ok=False,log='编译期间源码变化，保留原 PDF；请重新编译')
            if record['ok']:
                pdf = dict(id=current.get('pdf',{}).get('file') or uid(),name=Path(current['name']).stem+'.pdf',mime='application/pdf',kind='pdf',course=current['course'],folder=current['folder'],sha256=record['sha256'],pages=record['pages'],sources=[file['id']],compile=identifier,accepted=False,session=file.get('session'))
                if current.get('category'):
                    pdf['category']=current['category']
                    self.register_publication(pdf,current['course']+'/'+self.folder_storage_path(current['folder'])+'/'+self.storage_name(pdf['name']))
                self.save_file(pdf)
                current['pdf'] = dict(file=pdf['id'],compile=identifier,source_sha256=file['sha256'],stale=False)
            current['compile_result'] = {k:v for k,v in record.items() if k!='work'}
            self.save_file(current)
            self.conn.execute('INSERT INTO desktop_compiles VALUES (?,?,?)',(identifier,file['id'],encoded(record)))
            self.bump()
            if operation: self.conn.execute('INSERT INTO desktop_operations VALUES (?,?,?)',(operation,fingerprint,encoded(record)))
        self.sync_publications()
        return record

    def page(self,args):
        file = self.file(args['file'])
        if file['mime']!='application/pdf': raise ValueError('请选择 PDF')
        if args.get('expected',file['sha256'])!=file['sha256']:raise ValueError('PDF 版本已变化，请重新读取文件')
        page = args['page']
        if type(page)!=int or page<1: raise ValueError('页码无效')
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'page'
            if os.environ.get('STUDY_NATIVE')=='1' or os.environ.get('STUDY_PDFIUM')=='1':image=pdf_tools.image(self.blobs/file['sha256'],page)
            else:
                subprocess.run(['pdftoppm','-f',str(page),'-l',str(page),'-scale-to','1400','-singlefile','-png',str(self.blobs/file['sha256']),str(target)],check=True,capture_output=True,timeout=30)
                image = target.with_suffix('.png').read_bytes()
        if file.get('compile') and not read_only(self.root):
            with db.atomic(self.conn, bump_revision=False): self.conn.execute('INSERT OR IGNORE INTO desktop_views VALUES (?,?)',(file['compile'],page))
        return {'file':file['id'],'page':page,'sha256':file['sha256'],'images':[{'data':base64.b64encode(image).decode(),'mimeType':'image/png'}]}

    def review(self,args):
        file = self.file(args['file'])
        row = self.conn.execute('SELECT value FROM desktop_compiles WHERE id=?',(file.get('compile'),)).fetchone()
        if not row: raise ValueError('没有实际编译记录')
        record = json.loads(row[0])
        source = self.file(record['file'])
        if source['sha256']!=record['source_sha256'] or args.get('sha256')!=file['sha256']:
            raise ValueError('源码或 PDF 已变化，请重新核验')
        viewed = {r[0] for r in self.conn.execute('SELECT page FROM desktop_views WHERE compile=?',(record['id'],))}
        pages = args.get('pages',[])
        if [p.get('page') for p in pages]!=list(range(1,record['pages']+1)) or not set(range(1,record['pages']+1))<=viewed:
            raise ValueError('须实际渲染并核验每一页')
        if any(p.get('status')!='passed' or not p.get('summary','').strip() for p in pages): raise ValueError('有未通过的页面')
        for key in ('source_scope','layout'):
            if not args.get('checks',{}).get(key,'').strip(): raise ValueError('缺少内容与版式核验依据')
        receipt = self.root/'.study-system/evidence'/('pdf-'+record['id']+'.json')
        review = dict(args,artifact_sha256=file['sha256'],page_count=record['pages'])
        if args.get('kind') in ('note','worksheet'):
            # Formal course deliveries retain the original preparation and acceptance checks.
            if record.get('kind')!=args['kind'] or record.get('course')!=args['course'] or record.get('brief')!=args['brief']:
                raise ValueError('正式交付须在编译前保存课程和出卷/写作依据，请重新编译')
            delivery.accept(self.root,record['work'],self.blobs/file['sha256'],receipt,review)
        else:
            db.write_text_atomic(receipt,encoded(dict(source_sha256=source['sha256'],review=review)))
        file.update(accepted=True,receipt=str(receipt.relative_to(self.root)))
        self.save_file(file)
        return self.public(file)

if __name__ == '__main__':
    store = None
    try:
        store = Workspace(sys.argv[1])
        request = json.load(sys.stdin)
        print(encoded({'ok':True,'result':store.perform(request['action'],request.get('args',{}),request.get('id',''))}))
    except Exception as error:
        print(encoded({'ok':False,'error':str(error)}))
        sys.exit(1)
    finally:
        if store: store.close()
