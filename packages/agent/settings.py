"""Private app configuration; model credentials never appear in workspace snapshots."""
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo
from study_agent import db

class Settings:
    def __init__(self, library):
        self.path=Path(library)/'.settings/config.json'
        self.apply()

    def load(self):
        value=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
        if value.get('model')=='deepseek-pro':value['model']='deepseek-v4-pro'
        return value

    def status(self):
        value=self.load()
        return {'configured':bool(value.get('api_key') or os.environ.get('DEEPSEEK_API_KEY','').strip()),
                'model':value.get('model','deepseek-flash'),'timezone':value.get('timezone','Asia/Hong_Kong')}

    def apply(self):
        value=self.load()
        if value.get('api_key'):os.environ['DEEPSEEK_API_KEY']=value['api_key']
        os.environ['STUDY_TIMEZONE']=value.get('timezone','Asia/Hong_Kong')

    def save(self, args):
        value=self.load()
        key=args.get('api_key','').strip()
        if len(key)>4096 or '\n' in key:raise ValueError('请填写有效的模型服务密钥')
        if key:value['api_key']=key
        if not (value.get('api_key') or os.environ.get('DEEPSEEK_API_KEY')):raise ValueError('请先填写模型服务密钥')
        model=args.get('model','deepseek-flash')
        if model not in ('deepseek-flash','deepseek-v4-pro'):raise ValueError('请选择支持的模型')
        timezone=args.get('timezone','Asia/Hong_Kong')
        try:ZoneInfo(timezone)
        except (KeyError,ValueError):raise ValueError('时区无法识别') from None
        value.update(model=model,timezone=timezone)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        db.write_text_atomic(self.path,json.dumps(value,ensure_ascii=False))
        if os.name!='nt':self.path.chmod(0o600)
        self.apply()
        return self.status()
