"""Small versioned agreements: learning-wide, semester and course, never inferred by filename."""
from pathlib import Path
import file_lock as fcntl
import hashlib
import json
import tempfile
import uuid


def library_root(root):
    root=Path(root).resolve()
    if root.parent.name=='.semesters' and (root.parent/'index.json').exists(): return root.parent.parent
    return root


def read_all(root, course=None):
    root=Path(root)
    def load(path):
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'version':0,'items':{},'history':[],'operations':{}}
    general=load(library_root(root)/'.semesters/agreements.json')
    local=load(root/'.study-system/agreements.json')
    relevant=[*general['items'].values(),*[v for v in local['items'].values() if v['scope']=='semester' or v.get('course')==course]]
    return {'learning':general,'semester':local,'references':relevant}



def save(root,args,operation):
    scope=args.get('scope')
    if scope not in ('learning','semester','course'): raise ValueError('请明确约定适用于通用学习、当前学期还是一门课程')
    if scope=='course' and not args.get('course'): raise ValueError('课程约定需要明确课程')
    if not all(isinstance(args.get(k),str) and args[k].strip() for k in ('key','text','reason')): raise ValueError('请保存约定内容、主题与用户提出改变的依据')
    if len(args['key'])>180 or len(args['text'])>20000 or len(args['reason'])>20000:raise ValueError('约定内容过长，请保留与用户相关的要点')
    path=(library_root(root)/'.semesters' if scope=='learning' else Path(root)/'.study-system')/'agreements.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    with (path.parent/'agreements.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        data=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'version':0,'items':{},'history':[],'operations':{}}
        fingerprint=hashlib.sha256(json.dumps(args,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        if operation and operation in data['operations']:
            previous=data['operations'][operation]
            if previous['fingerprint']!=fingerprint: raise ValueError('重复约定操作的内容不同')
            return previous['result']
        if args.get('expected')!=data['version']: raise ValueError('约定已有变化，请重新读取')
        key=scope+':'+str(args.get('course') or '')+':'+args['key']
        item={k:args[k] for k in ('scope','key','text','reason')}
        if scope=='course':item['course']=args['course']
        item['id']=uuid.uuid4().hex
        data['history'].append(item)
        if args.get('remove'):data['items'].pop(key,None)
        else:data['items'][key]=item
        data['version']+=1
        result={'saved':True,'version':data['version'],'agreement':item,'removed':bool(args.get('remove'))}
        if operation:data['operations'][operation]={'fingerprint':fingerprint,'result':result}
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,delete=False) as tmp:
            json.dump(data,tmp,ensure_ascii=False);temp=Path(tmp.name)
        temp.replace(path)
        return result


def read_only(root):
    root=Path(root).resolve()
    path=library_root(root)/'.semesters/index.json'
    if not path.exists():return False
    identifier=root.name if root.parent.name=='.semesters' else 'initial'
    item=next((i for i in json.loads(path.read_text(encoding='utf-8'))['items'] if i['id']==identifier),None)
    return bool(item and item['status']!='open')
