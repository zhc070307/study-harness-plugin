"""Semester selection is a user action; each semester owns its entire learning state."""
from pathlib import Path
import json
import file_lock as fcntl
import hashlib
import os
import sqlite3
from contextlib import closing
import tempfile
import uuid
import zipfile
import shutil
import stat
import unicodedata
from pathlib import PurePosixPath

class Semesters:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / '.semesters/index.json'
        if not self.path.exists():
            self.save({'version':0,'active':'initial','items':[{'id':'initial','name':'当前学期','status':'open'}]})

    def load(self):
        return json.loads(self.path.read_text(encoding='utf-8'))

    def save(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=self.path.parent,delete=False) as out:
            json.dump(value,out,ensure_ascii=False)
            out.flush(); os.fsync(out.fileno()); temp=Path(out.name)
        temp.replace(self.path)

    def item(self, identifier=None):
        data=self.load()
        item=next((i for i in data['items'] if i['id']==(identifier or data['active'])),None)
        if not item: raise ValueError('学期不存在')
        return item

    def directory(self, identifier=None):
        item=self.item(identifier)
        return self.root if item['id']=='initial' else self.root/'.semesters'/item['id']

    def change(self, action, args, operation=""):
        with (self.path.parent/'index.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            return self._change(action,args,operation)

    def _change(self, action, args, operation):
        data=self.load()
        fingerprint=hashlib.sha256(json.dumps([action,args],sort_keys=True).encode()).hexdigest()
        if operation and operation in data.get('operations',{}):
            previous=data['operations'][operation]
            if previous['fingerprint']!=fingerprint:raise ValueError('重复学期操作的内容不同')
            return previous['result']
        if args.get('expected')!=data['version']: raise ValueError('学期列表已变化，请重新打开')
        if action=='cancel':
            data.pop('pending',None)
        elif action=='create':
            title=str(args.get('name','')).strip()
            if not title or len(title)>80: raise ValueError('请填写学期名称（不超过 80 字）')
            item={'id':uuid.uuid4().hex,'name':title,'status':'open'}
            self.directory_for_new(item).mkdir(parents=True)
            data['items'].append(item)
            if args.get('activate',True):
                data['active']=item['id']
                data.pop('pending',None)
            else:data['created']=item['id']
        else:
            item=next((i for i in data['items'] if i['id']==args.get('semester')),None)
            if not item: raise ValueError('学期不存在')
            if action=='select':
                if item['status']=='removed':raise ValueError('请先恢复已移除学期')
                data['pending']={'semester':item['id'],'requested':uuid.uuid4().hex}
            elif action=='switch':
                if item['status']=='removed': raise ValueError('请先从已移除学期中恢复')
                data['active']=item['id']
                data.pop('pending',None)
            elif action=='rename':
                title=str(args.get('name','')).strip()
                if not title or len(title)>80: raise ValueError('请填写学期名称（不超过 80 字）')
                item['name']=title
            elif action=='archive':
                if item['status']!='open':raise ValueError('只能归档正在使用的学期')
                item['status']='archived'
            elif action=='reopen':
                if item['status']!='archived': raise ValueError('只能重新开始已归档的学期')
                item['status']='open'
            elif action=='remove':
                if item['status']=='removed':raise ValueError('这个学期已经移除')
                if item['id']==data['active']: raise ValueError('请先切换到另一个学期，再移除这个学期')
                if args.get('confirm')!=item['name']: raise ValueError('请核对要移除的学期名称')
                item['previous_status']=item['status']; item['status']='removed'
                if data.get('pending',{}).get('semester')==item['id']:data.pop('pending',None)
            elif action=='restore':
                if item['status']!='removed': raise ValueError('这个学期没有被移除')
                item['status']=item.pop('previous_status','archived')
            else: raise ValueError('未知学期操作')
        data['version']+=1
        if operation:
            compact={key:data[key] for key in ('version','active','created','pending') if key in data}
            data.setdefault('operations',{})[operation]={'fingerprint':fingerprint,'result':compact}
        self.save(data)
        return data

    def directory_for_new(self,item):
        return self.root/'.semesters'/item['id']

    def export(self, identifier):
        """Quiescent server + SQLite backup; exclude credentials and transient compiler work."""
        root=self.directory(identifier)
        output=tempfile.TemporaryFile()
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
            from agreements import read_all
            memory=self.root/'.semesters/shared/.study-system/AGENT_MEMORY.md'
            from worker import Store
            shared=Store(self.root/'.semesters/shared',inherit=False)
            try:flows={row['flow']:shared.version(row['version']) for row in shared.conn.execute("SELECT flow,version FROM flow_active WHERE scope='*'")}
            finally:shared.conn.close()
            archive.writestr('semester.json',json.dumps(dict(self.item(identifier),format=1,source_root=str(root),learning_flows=flows,agent_memory=memory.read_text(encoding='utf-8') if memory.exists() else '',learning_references=read_all(root)['learning']['items']),ensure_ascii=False))
            for path in root.rglob('*'):
                rel=path.relative_to(root)
                if not path.is_file() or path.is_symlink(): continue
                if any(part in ('.semesters','.harness','.settings','.transcription','runtime','__pycache__') for part in rel.parts): continue
                if path.name.startswith(('.env','credentials')) or path.suffix in ('.pem','.key'): continue
                if path.name.startswith('task_memory.sqlite3'): continue
                archive.write(path,rel.as_posix())
            source=root/'.study-system/data/task_memory.sqlite3'
            if source.exists():
                with tempfile.TemporaryDirectory() as directory:
                    backup=Path(directory)/'ledger.sqlite3'
                    with closing(sqlite3.connect(source)) as conn, closing(sqlite3.connect(backup)) as dest: conn.backup(dest)
                    archive.write(backup,'.study-system/data/task_memory.sqlite3')
        output.seek(0)
        return output

    def import_backup(self, source, expected):
        """Restore into a new semester; never overwrite live data or current preferences."""
        with (self.path.parent/'index.lock').open('a+') as lock:
            import file_lock
            file_lock.flock(lock,file_lock.LOCK_EX)
            data=self.load()
            if data['version']!=expected:raise ValueError('学期列表已变化，请重新打开后恢复备份')
            identifier=uuid.uuid4().hex
            target=self.root/'.semesters'/identifier
            try:archive=zipfile.ZipFile(source)
            except zipfile.BadZipFile:raise ValueError('这不是有效的学期备份，请选择从本软件下载的 ZIP 备份') from None
            with archive:
                entries=archive.infolist()
                if len(entries)>50000 or sum(entry.file_size for entry in entries)>50*1024**3:raise ValueError('备份内容过大，暂不能恢复')
                if shutil.disk_usage(self.root).free<sum(entry.file_size for entry in entries)+1024**3:raise ValueError('学习存储空间不足，请先释放空间再恢复备份')
                try:manifest_entry=archive.getinfo('semester.json')
                except KeyError:raise ValueError('这不是 Study Agent 的学期备份') from None
                if manifest_entry.file_size>2*1024**2:raise ValueError('备份说明文件过大')
                manifest=json.loads(archive.read('semester.json'))
                if not isinstance(manifest,dict) or manifest.get('format')!=1:raise ValueError('备份格式暂不支持，请使用与备份匹配的应用版本')
                title=str(manifest.get('name','')).strip()
                if not title or len(title)>80:raise ValueError('备份中的学期名称无效')
                seen=set();components={}
                for entry in entries:
                    path=PurePosixPath(entry.filename)
                    if entry.filename in seen:raise ValueError('备份含有重复文件')
                    seen.add(entry.filename)
                    # Windows 和默认 macOS 文件系统会将大小写不同的名称视为同一路径。
                    for length in range(1,len(path.parts)+1):
                        prefix='/'.join(path.parts[:length])
                        key=unicodedata.normalize('NFC',prefix).casefold()
                        if key in components and components[key]!=prefix:raise ValueError('备份含有大小写或 Unicode 等价的同名路径，无法安全恢复')
                        components[key]=prefix
                    if path.is_absolute() or '..' in path.parts or '\\' in entry.filename or ':' in entry.filename or stat.S_ISLNK(entry.external_attr>>16):raise ValueError('备份中含有无效文件路径')
                    if any(part in ('.settings','.harness','.semesters','.transcription') or part.startswith(('.env','credentials')) for part in path.parts):raise ValueError('备份不应包含私有配置或运行环境')
                try:
                    target.mkdir()
                    for entry in entries:
                        if entry.filename=='semester.json' or entry.is_dir():continue
                        destination=target.joinpath(*PurePosixPath(entry.filename).parts)
                        destination.parent.mkdir(parents=True,exist_ok=True)
                        with archive.open(entry) as reader,destination.open('wb') as writer:shutil.copyfileobj(reader,writer)
                    ledger=target/'.study-system/data/task_memory.sqlite3'
                    if ledger.exists():
                        with closing(sqlite3.connect(ledger)) as conn,conn:
                            if conn.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('备份中的学习记录损坏')
                            old=manifest.get('source_root')
                            if old:
                                # Relocate recorded work/publication paths, keeping all evidence and identities.
                                def relocate(value):
                                    if isinstance(value,str) and (value==old or value.startswith(old+'/') or value.startswith(old+'\\')):return str(target)+value[len(old):].replace('\\','/')
                                    if isinstance(value,list):return [relocate(item) for item in value]
                                    if isinstance(value,dict):return {key:relocate(item) for key,item in value.items()}
                                    return value
                                for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                                    if not table.replace('_','').isalnum():continue
                                    columns=[row[1] for row in conn.execute('PRAGMA table_info("'+table+'")') if 'TEXT' in row[2].upper()]
                                    for column in columns:
                                        if not column.replace('_','').isalnum():continue
                                        for rowid,value in conn.execute('SELECT rowid,"'+column+'" FROM "'+table+'"').fetchall():
                                            if not isinstance(value,str):continue
                                            try:updated=json.dumps(relocate(json.loads(value)),ensure_ascii=False)
                                            except ValueError:updated=relocate(value)
                                            if updated!=value:conn.execute('UPDATE "'+table+'" SET "'+column+'"=? WHERE rowid=?',(updated,rowid))
                    references=manifest.get('learning_references',{})
                    if manifest.get('learning_flows'):references['__learning_flows__']={'scope':'learning','key':'旧流程候选','definitions':manifest['learning_flows'],'reason':'备份中的通用服务方式，需结合当前用户意愿验证后采用'}
                    if manifest.get('agent_memory'):references['__agent_memory__']={'scope':'learning','key':'旧记忆候选','text':manifest['agent_memory'],'reason':'备份中保存的参考，不覆盖当前意愿'}
                    if references:
                        folder=target/'.study-system';folder.mkdir(exist_ok=True)
                        (folder/'restored-references.json').write_text(json.dumps(references,ensure_ascii=False),encoding='utf-8')
                    item={'id':identifier,'name':title+'（恢复）','status':'open' if manifest.get('status')=='open' else 'archived'}
                    data['items'].append(item);data['version']+=1
                    self.save(data)
                    return dict(item,version=data['version'])
                except BaseException:
                    shutil.rmtree(target,ignore_errors=True)
                    raise
