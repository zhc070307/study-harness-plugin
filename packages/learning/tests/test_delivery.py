import json
import tempfile
import unittest
from pathlib import Path
from study_agent.delivery import prepare, accept, digest, routes
from study_agent.runtime_cleanup import finish_work

class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'C/notes').mkdir(parents=True)
        (self.root/'C/COURSE.md').write_text('Agreed method')
        self.work = self.root/'.study-system/runtime/build'; self.work.mkdir(parents=True)
        self.plan = self.root/'.study-system/PRACTICE_SELECTION.md'
        self.plan.write_text('## C\nPrior plan\n## OTHER\nOther plan')
        self.artifact = self.root/'C/notes/final.txt'; self.artifact.write_text('artifact')
        self.receipt = self.root/'.study-system/evidence/review.json'; self.receipt.parent.mkdir(parents=True)
        self.review = {'artifact_sha256': digest(self.artifact), 'page_count': 2,
                      'pages': [{'page': i,'status':'passed','summary':'Actual page inspection'} for i in [1,2]],
                      'checks': {k:'Actual verification' for k in ['source_scope','layout','task_handoff','plan_alignment','deduplication','source_questions','question_load','issue_weighting']}}
        prepare(self.root,self.work,'C','worksheet',self.plan)
    def test_prepare_is_selected_course_and_routes_exist(self):
        record=json.loads((self.work/'.delivery.json').read_text())
        self.assertNotIn('Other plan',record['preparation'])
        self.assertTrue(all(Path(p).is_file() for p in routes().values()))
    def test_unaccepted_delivery_not_cleaned(self):
        self.receipt.write_text('{}')
        with self.assertRaises(ValueError):finish_work(self.root,self.work,self.artifact,self.receipt,self.root/'trash')
        self.assertTrue(self.work.exists())
    def test_missing_page_rejected(self):
        self.review['pages'].pop()
        with self.assertRaises(ValueError):accept(self.root,self.work,self.artifact,self.receipt,self.review)
        self.assertFalse(self.receipt.exists())
    def test_failed_page_rejected(self):
        self.review['pages'][0]['status']='needs_revision'
        with self.assertRaises(ValueError):accept(self.root,self.work,self.artifact,self.receipt,self.review)
    def test_changed_plan_rejected(self):
        self.plan.write_text('## C\nRetrospective rationale')
        with self.assertRaises(ValueError):accept(self.root,self.work,self.artifact,self.receipt,self.review)
    def test_changed_course_rejected(self):
        (self.root/'C/COURSE.md').write_text('Changed requirements')
        with self.assertRaises(ValueError):accept(self.root,self.work,self.artifact,self.receipt,self.review)
    def test_changed_final_hash_rejected(self):
        self.artifact.write_text('changed')
        with self.assertRaises(ValueError):accept(self.root,self.work,self.artifact,self.receipt,self.review)
    def test_receipt_merge_and_changed_receipt_keeps_recovery(self):
        self.receipt.write_text('{"source":"preserved"}')
        accept(self.root,self.work,self.artifact,self.receipt,self.review)
        saved=json.loads(self.receipt.read_text())
        self.assertEqual(saved['source'],'preserved');self.assertNotIn('input_path',saved['delivery_review'])
        self.receipt.write_text('{}')
        with self.assertRaises(ValueError):finish_work(self.root,self.work,self.artifact,self.receipt,self.root/'trash')
    def test_accepted_delivery_finishes(self):
        accept(self.root,self.work,self.artifact,self.receipt,self.review)
        finish_work(self.root,self.work,self.artifact,self.receipt,self.root/'trash')
        self.assertFalse(self.work.exists());self.assertEqual(self.artifact.read_text(),'artifact')

    def test_missing_question_load_review_rejected(self):
        del self.review['checks']['question_load']
        with self.assertRaises(ValueError):accept(self.root,self.work,self.artifact,self.receipt,self.review)
    def test_missing_issue_weighting_review_rejected(self):
        del self.review['checks']['issue_weighting']
        with self.assertRaises(ValueError):accept(self.root,self.work,self.artifact,self.receipt,self.review)
