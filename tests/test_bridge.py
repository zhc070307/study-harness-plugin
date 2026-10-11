import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'packages/agent'))
from plugin_bridge import execute, semester_root
from semesters import Semesters


class ProjectBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def call(self, action, args=None, session='first', desktop=False, operation=''):
        return execute(self.root, {'action':action,'args':dict(args or {},session=session),'desktop':desktop,'id':operation,'project':str(self.root)})

    def test_context_is_readonly_and_shared_between_chats(self):
        self.assertEqual(self.call('context')['courses'],[])
        self.assertEqual(list(self.root.iterdir()),[])
        self.call('course',{'code':'MATH','name':'数学'},desktop=True,operation='course')
        self.assertEqual(self.call('context',session='second')['courses'][0]['code'],'MATH')

    def test_import_preserves_original_deduplicates_and_rejects_escape(self):
        original = self.root/'lecture.txt'
        original.write_text('实际课堂材料',encoding='utf-8')
        first = self.call('import',{'path':'lecture.txt'})
        second = self.call('import',{'path':'lecture.txt'},session='second')
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(original.read_text(encoding='utf-8'),'实际课堂材料')
        with self.assertRaises(ValueError): self.call('import',{'path':'../outside.txt'})
        with self.assertRaises(ValueError): self.call('import',{'path':'lecture.txt','expected':'wrong'})

    def test_semester_selection_is_persisted_per_chat(self):
        self.call('course',{'code':'OLD','name':'旧课程'},desktop=True)
        library=Semesters(self.root)
        new=library.change('create',{'name':'新学期','expected':library.load()['version'],'activate':False})
        identifier = library.load()['items'][-1]['id']
        self.call('semester',{'action':'select','semester':identifier,'reason':'换学期'},desktop=True)
        self.call('course',{'code':'NEW','name':'新课程'},desktop=True)
        self.assertEqual(self.call('context')['courses'][0]['code'],'NEW')
        self.assertEqual(self.call('context',session='second')['courses'][0]['code'],'OLD')
        self.assertEqual(semester_root(self.root,'first').name,identifier)

    def test_source_changes_mark_shared_summary_stale(self):
        source=self.root/'lecture.txt';source.write_text('lecture v1')
        sha=hashlib.sha256(source.read_bytes()).hexdigest()
        args={'key':'lecture','summary':'已读材料摘要','sources':[{'path':'lecture.txt','sha256':sha}],'expected':0}
        self.call('remember',args,operation='remember')
        value=self.call('context',session='second')
        self.assertFalse(value['shared_context'][0]['stale'])
        source.write_text('lecture v2')
        self.assertTrue(self.call('context')['shared_context'][0]['stale'])
        with self.assertRaises(ValueError):self.call('remember',args,operation='new-remember')

    def test_archived_semester_rejects_new_context_and_calendar_writes(self):
        library=Semesters(self.root)
        library.change('archive',{'semester':'initial','expected':0})
        with self.assertRaises(ValueError):self.call('remember',{'key':'x','summary':'x','expected':0})
        self.assertEqual(self.call('context')['semester'],'initial')

    def test_gpu_check_never_downloads_or_selects_cpu(self):
        with patch('plugin_transcription.check',return_value={'eligible':False,'backend':None,'gpu_required':True,'message':'No GPU'}):
            result=self.call('transcribe',{'action':'check'})
        self.assertFalse(result['eligible'])
        self.assertFalse((self.root/'.transcription').exists())

    def test_imported_pdf_uses_pdfium_for_reading_and_rendering(self):
        import pypdfium2 as pdfium
        import pdf_tools
        source=self.root/'lecture.pdf'
        document=pdfium.PdfDocument.new()
        page=document.new_page(300,400)
        document.save(source);page.close();document.close()
        with patch.dict(os.environ,{'STUDY_PDFIUM':'1'}):
            record=self.call('import',{'path':'lecture.pdf'})
            result=self.call('read',{'file':record['id']},desktop=True)
            self.assertEqual(result['name'],'lecture.pdf')
            self.assertEqual(result['text'],'')
            self.assertEqual(list(pdf_tools.page_texts(source)),[(1,'')])
            self.assertEqual(pdf_tools.pages(source),1)
            self.assertTrue(pdf_tools.image(source,1).startswith(b'\x89PNG'))


if __name__=='__main__':unittest.main()
