from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'packages/agent'))
from plugin_calendar import execute, CalDAV


class FakeCalendar(BaseHTTPRequestHandler):
    records = {}
    def log_message(self,*args): pass
    def reply(self,code,body=b'',etag=None):
        self.send_response(code)
        if etag:self.send_header('ETag',etag)
        self.end_headers();self.wfile.write(body)
    def do_PROPFIND(self):
        self.reply(207,b'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:propstat><d:prop><d:displayname>Study</d:displayname><d:resourcetype><c:calendar/></d:resourcetype></d:prop></d:propstat></d:response></d:multistatus>')
    def do_GET(self):
        if self.path not in self.records:self.reply(404);return
        body,etag=self.records[self.path];self.reply(200,body,etag)
    def do_REPORT(self):
        query=ET.fromstring(self.rfile.read(int(self.headers['Content-Length'])))
        interval=query.find('.//{urn:ietf:params:xml:ns:caldav}time-range')
        if interval is None or interval.attrib['start']!='20300101T000000Z':self.reply(400);return
        document=ET.Element('{DAV:}multistatus')
        for path,(body,etag) in self.records.items():
            response=ET.SubElement(document,'{DAV:}response')
            ET.SubElement(response,'{DAV:}href').text=path
            prop=ET.SubElement(ET.SubElement(response,'{DAV:}propstat'),'{DAV:}prop')
            ET.SubElement(prop,'{DAV:}getetag').text=etag
            ET.SubElement(prop,'{urn:ietf:params:xml:ns:caldav}calendar-data').text=body.decode()
        self.reply(207,ET.tostring(document))
    def do_PUT(self):
        prior=self.records.get(self.path)
        if self.headers.get('If-None-Match')=='*' and prior:self.reply(412);return
        if self.headers.get('If-Match') and (not prior or self.headers['If-Match']!=prior[1]):self.reply(412);return
        body=self.rfile.read(int(self.headers['Content-Length']))
        version=int(prior[1].strip('"'))+1 if prior else 1
        self.records[self.path]=(body,f'"{version}"');self.reply(201,etag=f'"{version}"')
    def do_DELETE(self):
        prior=self.records.get(self.path)
        if not prior:self.reply(404);return
        if self.headers.get('If-Match')!=prior[1]:self.reply(412);return
        del self.records[self.path];self.reply(204)


class CalendarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),FakeCalendar)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.config={'provider':'caldav','url':f'http://127.0.0.1:{cls.server.server_port}/calendar/'}
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);FakeCalendar.records={}
    def call(self,args,operation):return execute(self.root,args,operation,self.config)
    def test_real_http_create_read_update_delete_with_retry_and_etag(self):
        values={'action':'create','title':'复习','start':'2030-01-01T10:00:00+08:00','end':'2030-01-01T11:00:00+08:00'}
        first=self.call(values,'create-1');self.assertEqual(first,self.call(values,'create-1'))
        self.assertEqual(len(FakeCalendar.records),1)
        event=first['events'][0]
        self.assertEqual(event['title'],'复习')
        listed=self.call({'action':'list','start':'2030-01-01T08:00:00+08:00','end':'2030-01-02T08:00:00+08:00'},'')['events']
        self.assertEqual(listed,[event])
        updated=self.call(dict(values,action='update',id=event['id'],expected=event['expected'],title='练习'),'update-1')['events'][0]
        self.assertNotEqual(updated['expected'],event['expected'])
        with self.assertRaises(ValueError):self.call({'action':'delete','id':event['id'],'expected':event['expected']},'stale-delete')
        self.call({'action':'delete','id':updated['id'],'expected':updated['expected']},'delete-1')
        self.assertEqual(FakeCalendar.records,{})
    def test_calendar_discovery_and_missing_configuration(self):
        self.assertEqual(self.call({'action':'calendars'},'')['calendars'][0]['name'],'Study')
        self.assertFalse(execute(self.root,{'action':'status'},'',None)['configured'])
        with self.assertRaises(ValueError):execute(self.root,{'action':'list'},'',None)
    def test_timezones_and_event_url_are_validated(self):
        with self.assertRaises(ValueError):self.call({'action':'create','title':'x','start':'2030-01-01T10:00:00','end':'2030-01-01T11:00:00'},'bad')
        with self.assertRaises(ValueError):CalDAV(self.config).event_url('../outside.ics')
        with self.assertRaises(ValueError):CalDAV({'url':'http://calendar.example/'})


if __name__=='__main__':unittest.main()
