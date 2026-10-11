import json
import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from study_agent.runtime_cleanup import finish_work, resume_finished

class RuntimeCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / '.study-system/runtime/build'
        self.work.mkdir(parents=True)
        (self.work / 'page.png').write_bytes(b'preview')
        self.artifact = self.root / 'course/notes.pdf'
        self.artifact.parent.mkdir()
        self.artifact.write_bytes(b'final pdf')
        self.receipt = self.root / '.study-system/evidence/receipt.json'
        self.receipt.parent.mkdir(parents=True)
        self.receipt.write_text('{"verified":true}')
        self.trash = self.root / 'trash'

    def test_final_artifact_survives_and_build_is_recoverable(self):
        result = finish_work(self.root,self.work,self.artifact,self.receipt,self.trash)
        self.assertFalse(self.work.exists())
        self.assertEqual(self.artifact.read_bytes(), b'final pdf')
        self.assertEqual((Path(result['trash'])/'page.png').read_bytes(), b'preview')

    @unittest.skipUnless(hasattr(os, 'chflags') and hasattr(stat, 'UF_HIDDEN'), 'macOS file flags required')
    def test_final_pdf_hidden_flag_is_cleared_without_changing_bytes(self):
        before = hashlib.sha256(self.artifact.read_bytes()).digest()
        os.chflags(self.artifact, self.artifact.stat().st_flags | stat.UF_HIDDEN)
        self.assertTrue(self.artifact.stat().st_flags & stat.UF_HIDDEN)
        finish_work(self.root,self.work,self.artifact,self.receipt,self.trash)
        self.assertFalse(self.artifact.stat().st_flags & stat.UF_HIDDEN)
        self.assertEqual(hashlib.sha256(self.artifact.read_bytes()).digest(), before)

    def test_rejects_receipt_in_runtime(self):
        receipt = self.work/'receipt.json';receipt.write_text('{}')
        with self.assertRaises(ValueError):finish_work(self.root,self.work,self.artifact,receipt,self.trash)
        self.assertTrue(self.work.exists())

    def test_rejects_root_and_missing_artifact(self):
        with self.assertRaises(ValueError):finish_work(self.root,self.work.parent,self.artifact,self.receipt,self.trash)
        self.artifact.unlink()
        with self.assertRaises(ValueError):finish_work(self.root,self.work,self.artifact,self.receipt,self.trash)

    def test_unfinished_work_is_not_cleaned(self):
        self.assertEqual(resume_finished(self.root),[])
        self.assertTrue(self.work.exists())

    def test_changed_artifact_keeps_interrupted_work(self):
        marker={'work':str(self.work),'artifact':str(self.artifact),'receipt':str(self.receipt),'artifact_sha256':'stale'}
        (self.work/'.finished.json').write_text(json.dumps(marker))
        self.assertIn('kept',resume_finished(self.root)[0])
        self.assertTrue(self.work.exists())
