"""Authenticated headless host helper; the Study Agent owns the interface and files."""
from __future__ import annotations
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading
import time

from device import check
SOURCE=Path(__file__).resolve().parent
PROJECT=SOURCE.parent.parent
MAX_AUDIO=200*1024**2

def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8');temporary.replace(path)

class Service:
    def __init__(self,root,cpu=False):
        self.root=Path(root);self.cpu=cpu;self.lock=threading.Lock();self.child=None;self.active=None;self.active_attempt=None;self.cancelled=set()
        self.python=self.root/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        for path in (self.root/'jobs').glob('*/job.json'):
            try:
                job=json.loads(path.read_text(encoding='utf-8'))
                if job.get('status')=='running':
                    job.update(status='interrupted',message='上次转写被中断，已完成片段保留，可以继续')
                    write(path,job)
            except (OSError,ValueError):pass
    def version(self):
        return hashlib.sha256(b''.join((SOURCE/name).read_bytes() for name in ('SOURCE.json','requirements.txt'))+str(self.cpu).encode()).hexdigest()
    def ready(self):
        try:return self.python.exists() and json.loads((self.root/'installed.json').read_text(encoding='utf-8'))['runtime']==self.version()
        except (OSError,ValueError,KeyError):return False
    def resources(self):return check(self.root,self.cpu,self.ready())
    def status(self):return {'installed':self.ready(),'resources':self.resources()}
    def record(self,identifier):
        path=self.root/'jobs'/identifier/'job.json'
        if not path.exists():raise ValueError('任务不存在')
        return json.loads(path.read_text(encoding='utf-8'))
    def start(self,identifier,digest,name,audio,allow_install,attempt):
        if not self.lock.acquire(blocking=False):raise ValueError('已有录音正在转写')
        try:
            if (identifier,attempt) in self.cancelled:raise ValueError('转写已停止')
            resources=self.resources()
            if not resources['eligible']:raise ValueError(resources['message'])
            if not self.ready() and not allow_install:raise ValueError('请先确认下载转写环境与模型')
            directory=self.root/'jobs'/identifier;directory.mkdir(parents=True,exist_ok=True)
            path=directory/'job.json'
            if path.exists():
                prior=self.record(identifier)
                if prior['source_sha256']!=digest:raise ValueError('任务录音版本不一致')
                if prior['status']=='completed':self.lock.release();return prior
            source=directory/'audio'
            if not source.exists():source.write_bytes(audio)
            job={'id':identifier,'source':str(source),'name':Path(name).name,'source_sha256':digest,'status':'running','message':'正在准备转写','attempt':attempt}
            write(path,job);self.active=identifier;self.active_attempt=attempt
            threading.Thread(target=self.run,args=(directory,job),daemon=True).start()
            return job
        except BaseException:self.lock.release();raise
    def stop(self,identifier,attempt=None):
        if attempt:self.cancelled.add((identifier,attempt))
        if identifier==self.active and (attempt is None or attempt==self.active_attempt):
            self.active=None
            if self.child and self.child.poll() is None:self.child.terminate()
        return {'status':'stopping'}
    def run(self,directory,job):
        path=directory/'job.json';child=None
        def update(**values):job.update(values);write(path,job)
        def execute(command,env):
            if self.active!=job['id']:raise InterruptedError()
            process=subprocess.Popen(command,env=env,stdout=log,stderr=log);self.child=process
            if process.wait()!=0:
                if self.active!=job['id']:raise InterruptedError()
                raise RuntimeError('转写环境未能安装或 GPU 验证失败；请检查网络、驱动及诊断日志')
            if self.active!=job['id']:raise InterruptedError()
        env=dict(os.environ,PYTHONPATH=str(SOURCE),LOCALTRANSCRIBE_DATA_DIR=str(self.root),LOCALTRANSCRIBE_CACHE_DIR=str(directory/'cache'),LOCALTRANSCRIBE_LOG_DIR=str(directory/'logs'),LOCALTRANSCRIBE_DEVICE='cpu' if self.cpu else 'auto')
        resources=self.resources()
        if resources.get('backend')=='cuda':env['CUDA_VISIBLE_DEVICES']=str(resources['index'])
        try:
            with open(directory/'diagnostics.log','a',encoding='utf-8') as log:
                if not resources['eligible']:raise RuntimeError(resources['message'])
                if not self.ready():
                    update(message='正在下载转写环境；尚未下载模型')
                    execute([sys.executable,'-m','venv',str(self.root/'venv')],env)
                    backend=resources['backend'];index='cpu' if backend=='cpu' else 'cu128' if backend=='cuda' else None
                    options=['--index-url','https://download.pytorch.org/whl/'+index] if index else []
                    execute([str(self.python),'-m','pip','install','--disable-pip-version-check','--force-reinstall',*options,'torch==2.11.0','torchaudio==2.11.0'],env)
                    constraints=self.root/'torch-constraints.txt';constraints.write_text('torch==2.11.0\ntorchaudio==2.11.0\n')
                    execute([str(self.python),'-m','pip','install','--disable-pip-version-check','-r',str(SOURCE/'requirements.txt'),'-c',str(constraints)],env)
                    # Actual torch capability is checked before model weights are fetched.
                    execute([str(self.python),'-c','from local_transcribe.models import validate_device; print(validate_device()); import av,silero_vad; from transformers import AutoModelForMultimodalLM'],env)
                    write(self.root/'installed.json',{'runtime':self.version()})
                update(message='正在读取录音，首次使用需下载约 3.8 GiB 模型')
                if self.active!=job['id']:raise InterruptedError()
                child=subprocess.Popen([str(self.python),str(SOURCE/'run.py'),str(directory),str(path)],env=env,stdout=subprocess.PIPE,stderr=log,text=True,encoding='utf-8');self.child=child
                for line in child.stdout:
                    if self.active!=job['id']:raise InterruptedError()
                    try:event=json.loads(line)
                    except ValueError:continue
                    if event.get('type')=='progress':update(message=event.get('message','正在转写'),progress=event.get('progress'))
                if self.active!=job['id']:raise InterruptedError()
                if child.wait()!=0:raise RuntimeError('GPU 转写未完成；请检查驱动、可用内存、模型下载或录音格式；没有自动改用 CPU')
                result=json.loads((directory/'result.json').read_text(encoding='utf-8'));text=Path(result['raw_transcript']).read_text(encoding='utf-8')
                if not text.strip():raise RuntimeError('没有识别到文字')
                update(status='completed',message='转写完成',text=text)
        except InterruptedError:update(status='stopped',message='已停止，完成片段保留，可以继续')
        except Exception as error:update(status='failed',message=str(error) if isinstance(error,RuntimeError) else '转写未完成，请检查本机转写诊断日志')
        finally:
            if self.child and self.child.poll() is None:
                self.child.terminate()
                try:self.child.wait(timeout=5)
                except subprocess.TimeoutExpired:self.child.kill();self.child.wait()
            if child and child.stdout:child.stdout.close()
            self.child=None;self.active=None;self.active_attempt=None;self.lock.release()

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*unused):pass
    def reply(self,value,status=200):
        data=json.dumps(value,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def handle_request(self):
        if not secrets.compare_digest(self.headers.get('Authorization',''),'Bearer '+self.server.token):self.reply({'error':'未授权'},401);return
        try:
            service=self.server.service
            if self.command=='GET' and self.path=='/status':self.reply(service.status());return
            match=re.fullmatch('/jobs/([a-f0-9]{32})(/stop)?',self.path)
            if not match:raise ValueError('未知操作')
            identifier=match[1]
            if self.command=='GET':self.reply(service.record(identifier));return
            attempt=self.headers.get('X-Attempt','')
            if not re.fullmatch('[a-f0-9]{32}',attempt):raise ValueError('任务请求身份无效')
            if match[2]:self.reply(service.stop(identifier,attempt));return
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=MAX_AUDIO:raise ValueError('录音大小超过限制')
            digest=self.headers.get('X-Source-SHA256','')
            if not re.fullmatch('[a-f0-9]{64}',digest):raise ValueError('录音版本无效')
            audio=self.rfile.read(length)
            if len(audio)!=length or hashlib.sha256(audio).hexdigest()!=digest:raise ValueError('录音传输不完整')
            name=bytes.fromhex(self.headers.get('X-File-Name','')).decode('utf-8')
            self.reply(service.start(identifier,digest,name,audio,self.headers.get('X-Allow-Install')=='true',attempt))
        except Exception as error:self.reply({'error':str(error)},400)
    do_GET=handle_request
    do_POST=handle_request

def main():
    parser=argparse.ArgumentParser(description='Study Agent 可选转写后台（无独立界面）')
    parser.add_argument('--cpu',action='store_true',help='明确接受较慢的 CPU 转写；默认只使用可用 GPU')
    parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--bind',default='0.0.0.0')
    parser.add_argument('--config',type=Path)
    parser.add_argument('--data',type=Path,default=Path.home()/'.study-agent/transcription')
    args=parser.parse_args()
    if sys.version_info[:2]!=(3,12):raise SystemExit('转写后台需要 Python 3.12；请先安装该版本，普通文字资料无需此工具。')
    service=Service(args.data,args.cpu);resources=service.resources()
    print(resources['message'],flush=True)
    if not resources['eligible']:raise SystemExit('设备检查未通过，没有下载转写环境或模型。已有文字稿可以直接上传。')
    config=args.config or PROJECT/'.local-tools/transcription.json'
    try:token=json.loads(config.read_text(encoding='utf-8'))['token']
    except (OSError,ValueError,KeyError):token=secrets.token_urlsafe(32)
    server=ThreadingHTTPServer((args.bind,args.port),Handler);server.daemon_threads=True;server.service=service;server.token=token
    try:write(config,{'url':f'http://127.0.0.1:{server.server_port}' if args.bind == '127.0.0.1' else f'http://host.docker.internal:{server.server_port}','token':token})
    except OSError:server.server_close();raise SystemExit('无法保存本机连接配置，请确认项目目录可写，并由你创建 .local-tools 文件夹。')
    print('本机转写后台已就绪。在 Study Agent 打开录音并点击“转写”；首次下载前仍会征求同意。请保留此窗口。',flush=True)
    signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:server.serve_forever()
    except KeyboardInterrupt:service.stop(service.active)
    finally:server.server_close()

if __name__=='__main__':main()
