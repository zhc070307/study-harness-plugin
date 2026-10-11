"""External calendar adapters. CalDAV writes use ETags and stable operation UIDs."""
from __future__ import annotations
import base64
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import file_lock
from study_agent import db
from agreements import read_only

DAV = 'DAV:'
CAL = 'urn:ietf:params:xml:ns:caldav'


def moment(value, all_day=False):
    if not isinstance(value, str): raise ValueError('请提供明确的日历时间')
    if all_day: return date.fromisoformat(value)
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None: raise ValueError('非全天事件必须提供带时区的ISO8601时间')
    return result


def event_times(args):
    all_day = args.get('all_day', False)
    start, end = moment(args.get('start'), all_day), moment(args.get('end'), all_day)
    if end <= start: raise ValueError('事件结束时间必须晚于开始时间；全天结束日期不含边界')
    if not isinstance(args.get('title'), str) or not args['title'].strip(): raise ValueError('事件标题不能为空')
    return start, end


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): raise ValueError('日历服务发生重定向，请配置最终地址后重试')


class CalDAV:
    def __init__(self, config):
        self.url = config.get('url', '')
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('请配置无内嵌凭证的CalDAV日历集合地址')
        if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('远程日历必须使用HTTPS')
        if not self.url.endswith('/'): self.url += '/'
        self.headers = {}
        username = os.environ.get(config.get('usernameEnv', 'STUDY_CALDAV_USERNAME'), '')
        password = os.environ.get(config.get('passwordEnv', 'STUDY_CALDAV_PASSWORD'), '')
        if username or password:
            self.headers['Authorization'] = 'Basic '+base64.b64encode((username+':'+password).encode()).decode()
        self.opener = urllib.request.build_opener(NoRedirect())

    def event_url(self, identifier):
        if not isinstance(identifier, str) or not identifier.endswith('.ics') or '/' in identifier or '\\' in identifier or identifier.startswith('.'):
            raise ValueError('事件身份必须来自本日历的读取结果')
        return self.url+urllib.parse.quote(identifier, safe='')

    def request(self, method, url=None, body=None, headers=None):
        request = urllib.request.Request(url or self.url, data=body, method=method, headers={**self.headers, **(headers or {})})
        try:
            with self.opener.open(request, timeout=20) as response:
                data = response.read(8*1024*1024+1)
                if len(data) > 8*1024*1024: raise ValueError('日历响应过大，请缩小查询范围')
                return data, response.headers.get('ETag')
        except urllib.error.HTTPError as error:
            if error.code == 412: raise ValueError('日历事件已变化，请重新读取后再修改') from None
            if error.code in (401,403): raise ValueError('日历身份验证或访问权限失败') from None
            if error.code == 404: raise ValueError('日历或事件不存在') from None
            raise ValueError(f'日历请求失败（HTTP {error.code}）') from None
        except urllib.error.URLError: raise ValueError('无法连接日历服务，请检查地址和网络') from None

    @staticmethod
    def parse(data, identifier, etag):
        from icalendar import Calendar
        result = []
        for component in Calendar.from_ical(data).walk('VEVENT'):
            if str(component.get('STATUS','')).upper() == 'CANCELLED': continue
            start = component.decoded('DTSTART')
            end = component.decoded('DTEND') if component.get('DTEND') else start+component.decoded('DURATION') if component.get('DURATION') else start
            result.append({'id': identifier, 'expected': etag, 'uid': str(component.get('UID','')), 'title': str(component.get('SUMMARY','')), 'start': start.isoformat(), 'end': end.isoformat(), 'all_day': not isinstance(start, datetime), 'notes': str(component.get('DESCRIPTION','')), 'location': str(component.get('LOCATION','')), 'recurring': bool(component.get('RRULE') or component.get('RECURRENCE-ID'))})
        return result

    def read(self, identifier):
        data, etag = self.request('GET', self.event_url(identifier))
        return self.parse(data, identifier, etag)

    def calendars(self):
        body = b'<d:propfind xmlns:d="DAV:"><d:prop><d:displayname/><d:resourcetype/></d:prop></d:propfind>'
        data, _ = self.request('PROPFIND', body=body, headers={'Depth':'0','Content-Type':'application/xml'})
        document = ET.fromstring(data)
        if document.find('.//{'+CAL+'}calendar') is None: raise ValueError('所配置地址不是CalDAV日历集合')
        return [{'id': self.url, 'name': document.findtext('.//{DAV:}displayname') or self.url}]

    def list(self, args):
        start, end = moment(args['start']), moment(args['end'])
        if end <= start or (end-start).days > 366: raise ValueError('单次日历查询范围须大于零且不超过一年')
        fmt = lambda value: value.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        query = ET.Element('{'+CAL+'}calendar-query')
        prop = ET.SubElement(query, '{DAV:}prop')
        ET.SubElement(prop, '{DAV:}getetag')
        calendar_data = ET.SubElement(prop, '{'+CAL+'}calendar-data')
        ET.SubElement(calendar_data, '{'+CAL+'}expand', {'start':fmt(start),'end':fmt(end)})
        filters = ET.SubElement(query, '{'+CAL+'}filter')
        parent = ET.SubElement(filters, '{'+CAL+'}comp-filter', {'name':'VCALENDAR'})
        child = ET.SubElement(parent, '{'+CAL+'}comp-filter', {'name':'VEVENT'})
        ET.SubElement(child, '{'+CAL+'}time-range', {'start':fmt(start),'end':fmt(end)})
        data, _ = self.request('REPORT', body=ET.tostring(query), headers={'Depth':'1','Content-Type':'application/xml'})
        result = []
        for response in ET.fromstring(data).findall('{DAV:}response'):
            href = response.findtext('{DAV:}href','')
            url = urllib.parse.urljoin(self.url, href)
            if not url.startswith(self.url): raise ValueError('日历返回了集合之外的事件')
            identifier = urllib.parse.unquote(url[len(self.url):])
            self.event_url(identifier)
            raw = response.findtext('.//{'+CAL+'}calendar-data')
            if raw: result.extend(self.parse(raw.encode(), identifier, response.findtext('.//{DAV:}getetag')))
        return sorted(result, key=lambda value: (value['start'], value['id']))

    def write(self, action, args, operation):
        from icalendar import Calendar, Event
        if action == 'create':
            identifier = 'study-'+hashlib.sha256(operation.encode()).hexdigest()+'.ics'
            calendar = Calendar()
            calendar.add('version','2.0')
            calendar.add('prodid','-//Study Agent//Harness Plugin//EN')
            event = Event()
            event.add('uid',identifier[:-4]+'@study-agent')
            calendar.add_component(event)
            headers = {'If-None-Match':'*'}
        else:
            identifier = args['id']
            if not args.get('expected'): raise ValueError('修改或删除日历事件必须提供读取结果的expected版本')
            data, etag = self.request('GET', self.event_url(identifier))
            if etag != args['expected']: raise ValueError('日历事件已变化，请重新读取')
            calendar = Calendar.from_ical(data)
            events = calendar.walk('VEVENT')
            if len(events) != 1 or events[0].get('RRULE') or events[0].get('RECURRENCE-ID'):
                raise ValueError('重复事件须在日历应用中修改，当前工具不会改写整个系列')
            event = events[0]
            headers = {'If-Match':args['expected']}
        if action == 'delete':
            self.request('DELETE', self.event_url(identifier), headers=headers)
            return {'deleted': True, 'id':identifier}
        start, end = event_times(args)
        for key in ('summary','dtstart','dtend','duration','description','location','dtstamp'):
            if key in event: del event[key]
        for key, value in [('summary',args['title']),('dtstart',start),('dtend',end),('description',args.get('notes','')),('location',args.get('location','')),('dtstamp',datetime.now(timezone.utc))]: event.add(key,value)
        self.request('PUT', self.event_url(identifier), calendar.to_ical(), {**headers,'Content-Type':'text/calendar; charset=utf-8'})
        return {'saved': True, 'events': self.read(identifier)}


def execute(root, args, operation, config):
    action = args['action']
    if action == 'status': return {'configured': bool(config), 'provider': (config or {}).get('provider'), 'capabilities':['calendars','list','read','create','update','delete'] if config else []}
    if not config: raise ValueError('此项目尚未配置外部日历；没有创建或同步任何事件')
    mutates = action in ('create','update','delete')
    if mutates and read_only(root): raise ValueError('已归档学期不能写入日历')
    if config.get('provider') == 'apple':
        if sys.platform != 'darwin': raise ValueError('Apple Calendar工具仅在macOS可用；其他平台可配置CalDAV')
        command = config.get('executable')
        if not command or not Path(command).is_absolute(): raise ValueError('请配置已构建的study-calendar绝对路径')
        callback = lambda: json.loads(subprocess.run([command], input=json.dumps(dict(args,operation=operation)), encoding='utf-8', capture_output=True, timeout=120, check=True).stdout)
    elif config.get('provider') == 'caldav':
        provider = CalDAV(config)
        callback = lambda: provider.write(action,args,operation) if mutates else provider.read(args['id']) if action == 'read' else provider.list(args) if action == 'list' else provider.calendars()
    else: raise ValueError('不支持的日历提供方')
    if not mutates: return {'events' if action in ('list','read') else 'calendars':callback()}
    if not operation: raise ValueError('日历写操作必须有可恢复的调用身份')
    folder = root/'.study-system'
    folder.mkdir(parents=True, exist_ok=True)
    journal = folder/'calendar-operations.json'
    with (folder/'calendar.lock').open('a') as lock:
        file_lock.flock(lock,file_lock.LOCK_EX)
        state = json.loads(journal.read_text(encoding='utf-8')) if journal.exists() else {'operations':{},'links':{}}
        fingerprint = hashlib.sha256(json.dumps([args,config],sort_keys=True).encode()).hexdigest()
        prior = state['operations'].get(operation)
        if prior:
            if prior['fingerprint'] != fingerprint: raise ValueError('同一日历操作的内容不能变化')
            if 'result' in prior: return prior['result']
            raise ValueError('上次日历操作可能已生效，请先读取真实事件核对；不要重复创建')
        if args.get('task'):
            import sqlite3
            with sqlite3.connect((root/'.study-system/data/task_memory.sqlite3').as_uri()+'?mode=ro',uri=True) as conn:
                if not conn.execute('SELECT 1 FROM tasks WHERE task_key=? OR id=?',(args['task'],args['task'])).fetchone(): raise ValueError('日历关联的学习任务不存在')
        state['operations'][operation] = {'fingerprint':fingerprint,'status':'started'}
        db.write_text_atomic(journal,json.dumps(state))
        result = callback()
        state['operations'][operation]['result'] = result
        if args.get('task'): state['links'][args['task']] = result
        db.write_text_atomic(journal,json.dumps(state))
        return result


import sys
