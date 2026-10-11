"""Lightweight host checks. No torch imports, environment install or model downloads."""
from __future__ import annotations
import ctypes
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess

GIB=1024**3

def command(*args):
    return subprocess.check_output(args,text=True,timeout=15,stderr=subprocess.DEVNULL).strip()

def available_memory():
    system=platform.system()
    if system=='Windows':
        class Status(ctypes.Structure):
            _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong)]+[(name,ctypes.c_ulonglong) for name in ('total','available','page_total','page_available','virtual_total','virtual_available','extended')]
        status=Status();status.length=ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):raise OSError('无法读取内存')
        return status.available
    if system=='Darwin':
        text=command('vm_stat');page_size=int(text.split('page size of ')[1].split(' bytes')[0])
        values={line.split(':')[0]:int(line.split(':')[1].strip().rstrip('.')) for line in text.splitlines()[1:] if ':' in line}
        # Do not count compressed or active pages as immediately available.
        return page_size*sum(values.get(name,0) for name in ('Pages free','Pages inactive','Pages speculative'))
    values={line.split(':')[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text(encoding='utf-8').splitlines()}
    return values['MemAvailable']

def detect():
    system=platform.system();machine=platform.machine().lower()
    if system=='Darwin' and machine in ('arm64','aarch64'):
        version=tuple(int(part) for part in platform.mac_ver()[0].split('.')[:2])
        if version<(14,0):return {'backend':None,'message':'Apple GPU 转写需要 macOS 14 或更高版本'}
        displays=json.loads(command('system_profiler','SPDisplaysDataType','-json'))['SPDisplaysDataType']
        if any('metal' in str(key).lower() and 'unsupported' not in str(value).lower() for display in displays for key,value in display.items()):
            return {'backend':'mps','name':'Apple GPU（统一内存）'}
        return {'backend':None,'message':'未检测到可用的 Metal 加速'}
    if system not in ('Windows','Linux') or machine not in ('x86_64','amd64'):
        return {'backend':None,'message':'当前转写工具支持 Apple Silicon 或 Windows/Linux x64 的 NVIDIA GPU'}
    try:
        rows=command('nvidia-smi','--query-gpu=name,memory.free,memory.total,driver_version','--format=csv,noheader,nounits').splitlines()
        gpus=[]
        for index,row in enumerate(rows):
            name,free,total,driver=[part.strip() for part in row.split(',')]
            gpus.append({'backend':'cuda','index':index,'name':name,'gpu_free_gib':int(free)/1024,'gpu_total_gib':int(total)/1024,'driver':driver})
        gpu=max(gpus,key=lambda item:item['gpu_free_gib'])
        # CUDA 12.8 wheels need a compatible driver. Reject rather than fetching unusable wheels.
        minimum=(570,65) if system=='Windows' else (570,26)
        version=tuple(int(part) for part in gpu['driver'].split('.')[:2])
        if version<minimum:return {'backend':None,'message':'NVIDIA 驱动过旧，请先更新到支持 CUDA 12.8 的版本'}
        return gpu
    except (OSError,ValueError,subprocess.SubprocessError):
        return {'backend':None,'message':'未检测到可用的 NVIDIA GPU；不会自动使用 CPU'}

def check(directory,cpu=False,installed=False):
    result={'backend':'cpu','name':'CPU（用户主动选择）'} if cpu else detect()
    reasons=[]
    if not result.get('backend'):reasons.append(result.get('message','未检测到可用 GPU'))
    backend=result.get('backend')
    required_memory=12 if cpu else 8 if backend=='mps' else 6
    required_disk=6 if installed else 18 if backend=='cuda' else 10
    try:
        memory=available_memory()/GIB
        if memory<required_memory:reasons.append(f'实际可用内存不足 {required_memory} GiB')
    except (OSError,ValueError,KeyError,subprocess.SubprocessError):memory=None;reasons.append('无法确定实际可用内存')
    folder=Path(directory)
    while not folder.exists():folder=folder.parent
    disk=shutil.disk_usage(folder).free/GIB
    if disk<required_disk:reasons.append(f'空闲磁盘不足 {required_disk} GiB')
    import tempfile
    if shutil.disk_usage(tempfile.gettempdir()).free<2*GIB:reasons.append('临时空间不足 2 GiB')
    if backend=='cuda' and result['gpu_free_gib']<6:reasons.append('可用显存不足 6 GiB；请关闭占用显卡的程序')
    return {**result,'eligible':not reasons,'gpu_required':not cpu,'available_memory_gib':round(memory,1) if memory is not None else None,'free_disk_gib':round(disk,1),'required_memory_gib':required_memory,'required_disk_gib':required_disk,'message':'；'.join(reasons) or f'设备检查通过：{result["name"]}。模型约 3.8 GiB，实际速度取决于设备和录音。'}

if __name__=='__main__':
    import sys
    print(json.dumps(check(Path.home(),cpu='--cpu' in sys.argv),ensure_ascii=False))
