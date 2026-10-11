"""Inspect the selected interpreter; install only missing study dependencies."""
from __future__ import annotations
import argparse
import importlib
from importlib import metadata, util
import json
from pathlib import Path
import signal
import subprocess
import sys
import file_lock

# Pins apply only to newly installed packages; keep packages already in Harness.
DEPENDENCIES = (
    ('pypdfium2', 'pypdfium2', '4.30.0'),
    ('pillow', 'PIL', '11.3.0'),
    ('icalendar', 'icalendar', '6.3.2'),
    ('tzdata', 'tzdata', '2025.2'),
)


def inspect():
    if sys.version_info < (3, 10):
        raise ValueError('学习插件需要 Python 3.10 或以上，请检查 Harness 提供的解释器')
    packages = []
    for name, module, pinned in DEPENDENCIES:
        try:
            importlib.import_module(module)
        except ModuleNotFoundError as error:
            if error.name != module:
                raise ValueError(f'{name} 已存在但无法加载：{error}；请修复该环境') from error
            packages.append({'name': name, 'available': False, 'install': f'{name}=={pinned}'})
        except Exception as error:
            raise ValueError(f'{name} 已存在但无法加载：{error}；请修复该环境') from error
        else:
            try: version = metadata.version(name)
            except metadata.PackageNotFoundError: version = 'unknown'
            packages.append({'name': name, 'available': True, 'version': version})
    return {'python': sys.executable, 'environment': sys.prefix, 'packages': packages,
            'missing': [item['name'] for item in packages if not item['available']]}


def run_command(arguments):
    child = subprocess.Popen([sys.executable, *arguments], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    def stop(_number, _frame):
        child.terminate()
        try: child.wait(timeout=5)
        except subprocess.TimeoutExpired: child.kill(); child.wait()
        raise KeyboardInterrupt('依赖安装已取消')
    previous = signal.signal(signal.SIGTERM, stop)
    try:
        try: output, _ = child.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            child.kill(); child.communicate()
            raise ValueError('依赖安装超时，请检查网络后重试')
        if child.returncode:
            raise ValueError('无法在当前 Python 环境安装依赖，请检查网络、写入权限和包管理限制：\n'+output[-3000:])
    finally:
        signal.signal(signal.SIGTERM, previous)


def execute(action):
    if action not in ('check', 'install'): raise ValueError('请选择 check 或 install')
    result = dict(inspect(), installed=[])
    if action == 'check' or not result['missing']: return result
    try:
        lock = (Path(sys.prefix)/'.study-plugin-dependencies.lock').open('a+')
    except OSError as error:
        raise ValueError('当前 Python 环境不可写；请使用 Harness 提供的可写环境') from error
    with lock:
        file_lock.flock(lock, file_lock.LOCK_EX)
        before = inspect()
        missing = [item['install'] for item in before['packages'] if not item['available']]
        if not missing: return dict(before, installed=[])
        if util.find_spec('pip') is None:
            run_command(['-m', 'ensurepip', '--default-pip'])
        run_command(['-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
                     '--only-binary=:all:', '--upgrade-strategy', 'only-if-needed', *missing])
        importlib.invalidate_caches()
        result = inspect()
        if result['missing']: raise ValueError('安装后仍有缺失依赖：'+', '.join(result['missing']))
        return dict(result, installed=before['missing'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='检查当前 Python 的学习依赖；--install 只安装缺少的包')
    parser.add_argument('--install', action='store_true')
    parser.add_argument('root', nargs='?', help=argparse.SUPPRESS)
    options = parser.parse_args()
    try:
        action = json.load(sys.stdin)['action'] if options.root else 'install' if options.install else 'check'
        print(json.dumps({'ok': True, 'result': execute(action)}, ensure_ascii=False))
    except (Exception, KeyboardInterrupt) as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False))
        sys.exit(1)
