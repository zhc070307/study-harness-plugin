"""Optional, asynchronous LocalTranscribe tool; heavy dependencies load only on request."""
from __future__ import annotations
import file_lock as fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import threading
import time
import tempfile
import uuid
import urllib.request
import urllib.error

from workspace import Workspace, PROJECT, encoded
from agreements import library_root, read_only
from study_agent import db

SOURCE = PROJECT/'packages/transcription'
GIB = 1024**3


def memory_available():
    # Docker allocation is the limit; reclaimable cache is available to inference.
    info={line.split(':')[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text(encoding='utf-8').splitlines() if line.split(':')[0] in ('MemAvailable','MemTotal')}
    available=info['MemAvailable']
    limit=Path('/sys/fs/cgroup/memory.max')
    if limit.exists() and limit.read_text(encoding='utf-8').strip()!='max':
        base=limit.parent
        values=dict(line.split() for line in (base/'memory.stat').read_text(encoding='utf-8').splitlines())
        reclaimable=int(values.get('inactive_file',0))+int(values.get('slab_reclaimable',0))
        available=min(available,max(0,int(limit.read_text(encoding='utf-8'))-int((base/'memory.current').read_text(encoding='utf-8'))+reclaimable))
    else:
        base=Path('/sys/fs/cgroup/memory')
        if (base/'memory.limit_in_bytes').exists():
            values=dict(line.split() for line in (base/'memory.stat').read_text(encoding='utf-8').splitlines())
            available=min(available,max(0,int((base/'memory.limit_in_bytes').read_text(encoding='utf-8'))-int((base/'memory.usage_in_bytes').read_text(encoding='utf-8'))+int(values.get('total_inactive_file',0))))
    return available


def atomic(path, value):
    db.write_text_atomic(path, encoded(value))


class Transcription:
    def __init__(self, root):
        self.root=Path(root).resolve()
        self.shared=library_root(self.root)/'.transcription'
        self.jobs=self.root/'.study-system/transcription'
        self.python=self.shared/'venv/bin/python'
        self._host_status=None
        self._host_checked=0

    def host_config(self):
        path=Path(os.environ.get('STUDY_TRANSCRIBE_CONFIG','/host-tools/transcription.json'))
        if not path.exists():path=PROJECT/'.local-tools/transcription.json'
        try:
            config=json.loads(path.read_text(encoding='utf-8'))
            if not config['url'].startswith(('http://host.docker.internal:','http://127.0.0.1:')) or not config['token']:return None
            return config
        except (OSError,ValueError,KeyError):return None

    def host_request(self,route,data=None,headers=None,timeout=5):
        config=self.host_config()
        if not config:raise RuntimeError('请先启动可选的本机转写后台；不会默认使用 CPU')
        request=urllib.request.Request(config['url']+route,data=data,headers={'Authorization':'Bearer '+config['token'],**(headers or {})})
        try:
            # Local audio must never pass through a configured external HTTP proxy.
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:message=json.load(error).get('error','本机转写请求未完成')
            except ValueError:message='本机转写请求未完成'
            raise RuntimeError(message) from None
        except (OSError,ValueError):raise RuntimeError('转写服务暂时不可用，请重新打开应用后再试；原录音保留，不会自动改用 CPU' if os.environ.get('STUDY_NATIVE')=='1' else '无法连接本机转写后台；请启动它或检查本机防火墙，不会自动改用 CPU') from None

    def host_status(self):
        if self._host_status is None or time.monotonic()-self._host_checked>2:
            self._host_checked=time.monotonic()
            try:self._host_status=self.host_request('/status')
            except RuntimeError as error:self._host_status={'installed':False,'resources':{'eligible':False,'backend':None,'gpu_required':True,'message':str(error)}}
        return self._host_status

    def ready(self):
        if self.host_config():return self.host_status()['installed']
        marker=self.shared/'installed.json'
        try: return self.python.is_file() and json.loads(marker.read_text(encoding='utf-8')).get('runtime')==self.version()
        except (OSError,ValueError): return False

    def version(self):
        return hashlib.sha256(b''.join((SOURCE/name).read_bytes() for name in ('SOURCE.json','requirements.txt','requirements-cpu.txt'))).hexdigest()

    def resources(self):
        if self.host_config():return self.host_status()['resources']
        if os.environ.get('STUDY_TRANSCRIBE_CPU')!='1':
            return {'eligible':False,'backend':None,'gpu_required':True,'message':'请先启动 scripts 中的本机转写后台，它会检测 GPU、内存和磁盘。默认不使用 CPU；已有文字稿可直接上传，详见 README 的录音转写说明。'}
        reasons=[]
        directory=self.shared
        while not directory.exists():directory=directory.parent
        free=shutil.disk_usage(directory).free
        temporary=shutil.disk_usage(tempfile.gettempdir()).free
        try: ram=memory_available()
        except (OSError,ValueError,KeyError):ram=None;reasons.append('无法确定实际可用内存，暂不下载转写环境')
        if ram is not None and ram<12*GIB:reasons.append('实际可用内存不足 12 GiB；请关闭其他程序或为 Docker 分配更多内存（建议至少 16 GiB）')
        required=6*GIB if self.ready() else 8*GIB
        if free<required:reasons.append('学习存储的空闲磁盘不足 '+str(required//GIB)+' GiB')
        if temporary<2*GIB:reasons.append('临时存储的空闲磁盘不足 2 GiB')
        if sys.version_info<(3,10):reasons.append('录音转写需要 Python 3.10+，请使用容器安装')
        return {'eligible':not reasons,'backend':'cpu','gpu_required':False,'available_memory_gib':round(ram/GIB,1) if ram is not None else None,'free_disk_gib':round(free/GIB,1),'required_memory_gib':12,'required_disk_gib':required//GIB,'message':'；'.join(reasons) or '资源检查通过：已主动选择较慢的 CPU 模式'}

    def records(self):
        result=[]
        for path in sorted(self.jobs.glob('*/job.json')):
            try:
                job=json.loads(path.read_text(encoding='utf-8'))
                if job['status']=='running':
                    try:
                        # The inherited lock, not a reusable PID, establishes worker ownership.
                        with open(path.parent/'worker.lock','a+') as lock:
                            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        job.update(status='interrupted',message='转写被中断；原录音与已完成片段保留，可以继续。')
                        atomic(path,job)
                    except BlockingIOError: pass
                result.append(job)
            except (OSError,ValueError,KeyError): continue
        return result

    def status(self):
        return {'installed':self.ready(),'resources':self.resources(),'jobs':self.records()}

    def start(self,args):
        if read_only(self.root): raise ValueError('这个学期已归档，请重新开始后再转写')
        store=Workspace(self.root)
        try:
            file=store.file(args['file'])
            if not file['mime'].startswith('audio/'): raise ValueError('请选择录音文件；已有文字稿可以直接交给助理')
            if args.get('expected')!=file['sha256']: raise ValueError('录音版本已变化，请重新读取')
            source=store.blobs/file['sha256']
        finally: store.close()
        prior=next((job for job in self.records() if job['file']==file['id'] and job['source_sha256']==file['sha256']),None)
        if prior and prior['status']=='completed': return prior
        resources=self.resources()
        if not resources['eligible']:return {'blocked':True,'resources':resources,'message':resources['message']+'。已有文字稿仍可直接使用。'}
        if not self.ready() and args.get('allow_install') is not True:

            return {'needs_install':True,'message':'已有讲稿或转写稿请直接上传，无需安装。设备检查已通过；需要识别录音时才下载环境和约 3.8 GiB 模型。请说明所选加速方式与检测到的资源需求，用户同意后再开始。','resources':resources}
        self.shared.mkdir(parents=True,exist_ok=True)
        global_lock=open(self.shared/'active.lock','a+')
        try: fcntl.flock(global_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            global_lock.close()
            raise ValueError('已有转写正在处理，请等待完成或停止后再开始')
        try:
            prior=next((job for job in self.records() if job['file']==file['id'] and job['source_sha256']==file['sha256']),None)
            if prior and prior['status']=='completed': return prior
            identifier=prior['id'] if prior else uuid.uuid4().hex
            directory=self.jobs/identifier
            directory.mkdir(parents=True,exist_ok=True)
            lock=open(directory/'worker.lock','a+')
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            job={**(prior or {}),'id':identifier,'file':file['id'],'source_sha256':file['sha256'],'source':str(source),'name':file['name'],'status':'running','message':'正在准备转写','pid':None,'host':bool(self.host_config()),'allow_install':args.get('allow_install') is True,'attempt':uuid.uuid4().hex}
            atomic(directory/'job.json',job)
            # Original pipeline gets an immutable source, scoped checkpoints, and shared models.
            with open(directory/'diagnostics.log','a') as log:
                process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--run',str(self.root),identifier,str(global_lock.fileno()),str(lock.fileno())],
                    stdout=log,stderr=log,start_new_session=True,pass_fds=(global_lock.fileno(),lock.fileno()))
            job['pid']=process.pid
            atomic(directory/'job.json',job)
            threading.Thread(target=process.wait,daemon=True).start()
            lock.close()
            return job
        finally: global_lock.close()

    def stop(self,args):
        job=next((job for job in self.records() if job['id']==args.get('job')),None)
        if not job: raise ValueError('转写任务不存在')
        if job['status']!='running': return job
        # Verify the process belongs to this exact job before signalling its group.
        try:
            command=Path('/proc')/str(job['pid'])/'cmdline'
            if sys.platform=='darwin':
                result=subprocess.run(['ps','-p',str(job['pid']),'-o','command='],capture_output=True,encoding='utf-8',timeout=5)
                if result.returncode!=0:return {'id':job['id'],'status':'stopping','message':'转写进程已退出，请刷新'}
                tokens=result.stdout.split()
                owned=job['id'] in tokens and '--run' in tokens
            else:
                owned=job['id'].encode() in command.read_bytes().split(b'\0')
            if not owned:raise ValueError('转写进程已变化，请刷新')
            os.killpg(job['pid'],signal.SIGTERM)
        except (ProcessLookupError,FileNotFoundError): pass
        return {'id':job['id'],'status':'stopping','message':'正在停止；已完成片段保留'}


class NativeTranscription(Transcription):
    """The app-owned GPU service owns work; any tool can poll durable results."""
    def records(self):
        result=[]
        for path in sorted(self.jobs.glob('*/job.json')):
            try:
                with (path.parent/'worker.lock').open('a+') as lock:
                    fcntl.flock(lock,fcntl.LOCK_EX)
                    job=json.loads(path.read_text(encoding='utf-8'))
                    if job['status']=='running':
                        try:
                            reply=self.host_request('/jobs/'+job['id'])
                            if reply['status']=='completed':
                                self.complete(job,reply['text'])
                            elif reply['status']!='running':job.update(status=reply['status'],message=reply.get('message','转写已停止'))
                            else:job.update(message=reply.get('message','正在转写'),progress=reply.get('progress'))
                        except RuntimeError:job.update(status='interrupted',message='转写连接中断；原录音与已完成片段保留，可以继续')
                        atomic(path,job)
                result.append(job)
            except (OSError,ValueError,KeyError):continue
        return result

    def complete(self,job,text):
        with self.managed_store() as store:
            original=store.file(job['file'])
            if original['sha256']!=job['source_sha256']:raise RuntimeError('录音已变化，原版本转写保留')
            transcript=store.perform('create',{'name':Path(job['name']).stem+'-原稿.txt','text':text,'mime':'text/plain','folder':original['folder'],'course':original['course'],'sources':[original['id']],'transcription':{'job':job['id'],'source_sha256':job['source_sha256'],'engine':'LocalTranscribe/Qwen3-ASR-1.7B'}},'transcribe-'+job['id'])
            job.update(status='completed',message='转写已保存，重要术语与数字请核对',transcript=transcript['id'])
            store.message('transcribe-'+job['id'],job['file'],{'role':'assistant','text':'录音原稿已保存为 '+transcript['name']+'。请核对重要术语和数字。'})

    def managed_store(self):
        class Managed(Workspace):
            def __enter__(self):return self
            def __exit__(self,*unused):self.close()
        return Managed(self.root)

    def start(self,args):
        if read_only(self.root):raise ValueError('这个学期已归档，请重新开始后再转写')
        with self.managed_store() as store:
            file=store.file(args['file'])
            if not file['mime'].startswith('audio/'):raise ValueError('请选择录音；已有文字稿可以直接交给助理')
            if args.get('expected')!=file['sha256']:raise ValueError('录音版本已变化，请重新读取')
            audio=store.content(file)
        resources=self.resources()
        if not resources['eligible']:return {'blocked':True,'resources':resources,'message':resources['message']+'。已有文字稿可以直接使用。'}
        if not self.ready() and args.get('allow_install') is not True:
            return {'needs_install':True,'resources':resources,'message':'设备检查通过。已有文字稿无需下载；转写录音首次需要下载环境及约 3.8 GiB 模型，是否继续？'}
        self.shared.mkdir(parents=True,exist_ok=True)
        with (self.shared/'active.lock').open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            prior=next((job for job in self.records() if job['file']==file['id'] and job['source_sha256']==file['sha256']),None)
            if prior and prior['status'] in ('running','completed'):return prior
            identifier=prior['id'] if prior else uuid.uuid4().hex
            attempt=uuid.uuid4().hex
            reply=self.host_request('/jobs/'+identifier,data=audio,headers={'X-Attempt':attempt,'X-Source-SHA256':file['sha256'],'X-File-Name':file['name'].encode().hex(),'X-Allow-Install':'true' if args.get('allow_install') is True else 'false'},timeout=30)
            job={'id':identifier,'file':file['id'],'source_sha256':file['sha256'],'name':file['name'],'status':'running','message':reply.get('message','正在转写'),'attempt':attempt}
            atomic(self.jobs/identifier/'job.json',job)
            return job

    def stop(self,args):
        job=next((job for job in self.records() if job['id']==args.get('job')),None)
        if not job:raise ValueError('转写任务不存在')
        if job['status']!='running':return job
        self.host_request('/jobs/'+job['id']+'/stop',data=b'',headers={'X-Attempt':job['attempt']})
        return {'id':job['id'],'status':'stopping','message':'正在停止，已完成片段保留'}


if os.environ.get('STUDY_NATIVE')=='1':Transcription=NativeTranscription


def perform(root,action,args):
    tool=Transcription(root)
    if action=='status': return tool.status()
    if action=='check': return tool.resources()
    if action=='start': return tool.start(args)
    if action=='stop': return tool.stop(args)
    raise ValueError('未知转写操作')


def run_cpu(tool,directory,path,env,update):
    child=None
    try:
        if not tool.ready():
            resources=tool.resources()
            if not resources['eligible']:raise RuntimeError(resources['message'])
            if sys.version_info<(3,10): raise RuntimeError('转写需要 Python 3.10 或更高版本，请使用容器安装')
            if __import__('shutil').disk_usage(tool.shared).free<8*1024**3: raise RuntimeError('转写空间不足，请至少预留 8 GB')
            update(message='正在下载转写环境；已有文字稿无需此步骤')
            subprocess.run([sys.executable,'-m','venv',str(tool.shared/'venv')],check=True)
            # Official CPU wheels avoid pulling an unused CUDA runtime into Docker Desktop.
            for options in (['--index-url','https://download.pytorch.org/whl/cpu','-r',str(SOURCE/'requirements-cpu.txt')],['-r',str(SOURCE/'requirements.txt'),'-c',str(SOURCE/'requirements-cpu.txt')]):
                child=subprocess.Popen([str(tool.python),'-m','pip','install','--disable-pip-version-check',*options],env=env)
                if child.wait()!=0: raise RuntimeError('转写环境下载未完成；检查网络或磁盘后可以继续')
                child=None
            subprocess.run([str(tool.python),'-c','import torch,av,transformers,silero_vad; from transformers import AutoModelForMultimodalLM'],env=env,check=True)
            atomic(tool.shared/'installed.json',{'runtime':tool.version()})
        update(message='正在读取录音，首次使用还需下载模型')
        child=subprocess.Popen([str(tool.python),str(SOURCE/'run.py'),str(directory),str(path)],env=env,stdout=subprocess.PIPE,text=True)
        for line in child.stdout:
            try: event=json.loads(line)
            except ValueError: continue
            if event.get('type')=='progress': update(message=event.get('message','正在转写'),progress=event.get('progress'))
        if child.wait()!=0: raise RuntimeError('录音转写未完成；检查模型下载网络、可用内存或录音格式后可以继续')
        child.stdout.close();child=None
        result=json.loads((directory/'result.json').read_text(encoding='utf-8'))
        text=Path(result['raw_transcript']).read_text(encoding='utf-8')
        if not text.strip(): raise RuntimeError('没有识别到可保存的文字')
        return text
    finally:
        if child and child.poll() is None:
            child.terminate()
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:child.kill();child.wait()
        if child and child.stdout:child.stdout.close()


def run_job(root,identifier,global_fd,worker_fd):
    tool=Transcription(root);directory=tool.jobs/identifier;path=directory/'job.json'
    for unused in range(100):
        job=json.loads(path.read_text(encoding='utf-8'))
        if job.get('pid'): break
        time.sleep(0.05)
    job=json.loads(path.read_text(encoding='utf-8'))
    def update(**changes):
        job.update(changes);atomic(path,job)
    def stop(*unused): raise InterruptedError('转写已停止')
    signal.signal(signal.SIGTERM,stop)
    env=dict(os.environ, PYTHONPATH=str(SOURCE),
        LOCALTRANSCRIBE_DATA_DIR=str(tool.shared),
        LOCALTRANSCRIBE_CACHE_DIR=str(directory/'cache'),
        LOCALTRANSCRIBE_LOG_DIR=str(directory/'logs'),LOCALTRANSCRIBE_DEVICE='cpu')
    try:
        if job.get('host'):
            update(message='正在交给本机 GPU 转写后台')
            reply=tool.host_request('/jobs/'+identifier,data=Path(job['source']).read_bytes(),headers={'X-Attempt':job['attempt'],'X-Source-SHA256':job['source_sha256'],'X-File-Name':job['name'].encode().hex(),'X-Allow-Install':'true' if job['allow_install'] else 'false'},timeout=120)
            while reply['status']=='running':
                update(message=reply.get('message','正在转写'),progress=reply.get('progress'))
                time.sleep(1)
                reply=tool.host_request('/jobs/'+identifier)
            if reply['status']!='completed':raise RuntimeError(reply.get('message','本机转写未完成'))
            text=reply['text']
        else:
            text=run_cpu(tool,directory,path,env,update)
        if not text.strip(): raise RuntimeError('没有识别到可保存的文字')

        store=Workspace(root)
        try:
            original=store.file(job['file'])
            if original['sha256']!=job['source_sha256']: raise RuntimeError('录音已变化；原版本转写保留，未关联到新录音')
            transcript=store.perform('create',{'name':Path(job['name']).stem+'-原稿.txt','text':text,'mime':'text/plain','folder':original['folder'],'course':original['course'],'sources':[original['id']],'transcription':{'job':identifier,'source_sha256':job['source_sha256'],'engine':'LocalTranscribe/Qwen3-ASR-1.7B'}},'transcribe-'+identifier)
            update(status='completed',message='转写已保存，重要术语与数字请核对',transcript=transcript['id'])
            store.message('transcribe-'+identifier,job['file'],{'role':'assistant','text':'录音原稿已保存为 '+transcript['name']+'。自动识别可能有误，请核对重要术语和数字。'})
        finally: store.close()
    except InterruptedError:
        if job.get('host'):
            try:tool.host_request('/jobs/'+identifier+'/stop',data=b'',headers={'X-Attempt':job['attempt']})
            except RuntimeError:pass
        update(status='stopped',message='已停止；原录音与已完成片段保留，可以继续')
    except Exception as error:
        if job.get('host'):
            try:tool.host_request('/jobs/'+identifier+'/stop',data=b'',headers={'X-Attempt':job['attempt']})
            except RuntimeError:pass
        update(status='failed',message=str(error) if isinstance(error,RuntimeError) else '转写未完成；原录音和断点保留，请检查安装与磁盘后继续')
    finally:
        os.close(worker_fd);os.close(global_fd)


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--run': run_job(sys.argv[2],sys.argv[3],int(sys.argv[4]),int(sys.argv[5]))
    else:
        try:
            request=json.load(sys.stdin)
            print(encoded({'ok':True,'result':perform(sys.argv[1],request['action'],request.get('args',{}))}))
        except Exception as error:
            print(encoded({'ok':False,'error':str(error)}));sys.exit(1)
