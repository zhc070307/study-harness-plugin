"""Persist preparation and verify declared delivery evidence; no learning judgments."""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from .db import write_text_atomic


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def routes():
    base = Path(__file__).resolve().parents[2] / 'skills/study-system'
    return {'course_method': str(base / 'SKILL.md'),
            'note': str(base.parent / 'study-notes/SKILL.md'),
            'worksheet': str(base.parent / 'study-worksheets/SKILL.md'),
            'files': str(base.parent / 'study-files/SKILL.md'),
            'feedback': str(base.parent / 'study-feedback/SKILL.md'),
            'transcription': str(base.parent / 'study-transcription/SKILL.md')}


def _work(root, work):
    root, work = Path(root).resolve(), Path(work).resolve()
    runtime = root / '.study-system/runtime'
    if not work.is_relative_to(runtime) or work == runtime or work.is_relative_to(runtime / 'views'):
        raise ValueError('Use a dedicated semester runtime work directory')
    return root, work


def _course_plan(text, course):
    # Capture the selected course section, not other courses or a whole daily report.
    headings = list(re.finditer(r'^## (.+)$', text, re.M))
    for i, match in enumerate(headings):
        if re.search(r'(?<![A-Za-z0-9])' + re.escape(course) + r'(?![A-Za-z0-9])', match[1]):
            return text[match.start():headings[i+1].start() if i+1 < len(headings) else len(text)].strip()
    raise ValueError('Selection file must contain a ## heading for this course')


def prepare(root, work, course, kind, input_path):
    root, work = _work(root, work)
    if kind not in ('note', 'worksheet'):
        raise ValueError('kind must be note or worksheet')
    course_path = (root / course / 'COURSE.md').resolve()
    if not course_path.is_relative_to(root) or not course_path.is_file():
        raise ValueError('An existing course COURSE.md is required')
    input_path = Path(input_path).resolve()
    if not input_path.is_relative_to(root):
        raise ValueError('Preparation input must belong to the selected semester')
    text = input_path.read_text(encoding='utf-8').strip()
    if kind == 'worksheet':
        if input_path != root / '.study-system/PRACTICE_SELECTION.md':
            raise ValueError('Use the existing semester PRACTICE_SELECTION.md')
        text = _course_plan(text, course)
    if not text:
        raise ValueError('Nonempty writing brief or prior selection plan is required')
    work.mkdir(parents=True, exist_ok=True)
    marker = work / '.delivery.json'
    if marker.exists():
        raise ValueError('Preparation already exists; use a new work directory for a revised plan')
    record = {'version': 1, 'kind': kind, 'course': course,
              'prepared_at': datetime.now(timezone.utc).isoformat(),
              'course_sha256': digest(course_path), 'input_sha256': digest(input_path),
              'preparation': text, 'protocol_sha256': digest(routes()[kind]),
              'input_path': str(input_path), 'accepted': False}
    write_text_atomic(marker, json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    return record


def accept(root, work, artifact, receipt, review):
    root, work = _work(root, work)
    marker = work / '.delivery.json'
    record = json.loads(marker.read_text(encoding='utf-8'))
    artifact, receipt = Path(artifact).resolve(), Path(receipt).resolve()
    runtime = root / '.study-system/runtime'
    if not artifact.is_relative_to(root) or artifact.is_relative_to(runtime) or not artifact.is_file() or not artifact.stat().st_size:
        raise ValueError('A nonempty semester delivery artifact outside runtime is required')
    if not receipt.is_relative_to(root / '.study-system/evidence'):
        raise ValueError('Receipt must be in the selected semester evidence directory')
    # Reject stale review against changed source/method requirements.
    if digest(root / record['course'] / 'COURSE.md') != record['course_sha256']:
        raise ValueError('Course context changed after preparation; review and prepare again')
    if digest(routes()[record['kind']]) != record['protocol_sha256']:
        raise ValueError('Shared delivery standard changed after preparation')
    current_input = Path(record['input_path']).read_text(encoding='utf-8').strip()
    if record['kind'] == 'worksheet':
        current_input = _course_plan(current_input, record['course'])
    if current_input != record['preparation']:
        raise ValueError('Preparation input changed; prepare the revised plan before drafting')
    required = ['source_scope', 'layout', 'task_handoff']
    if record['kind'] == 'worksheet':
        required += ['plan_alignment', 'deduplication', 'source_questions', 'question_load', 'issue_weighting']
    else:
        required += ['author_model', 'brief_standards']
    checks = review.get('checks', {})
    for key in required:
        if not isinstance(checks.get(key), str) or not checks[key].strip():
            raise ValueError('Missing factual review: ' + key)
    count, pages = review.get('page_count'), review.get('pages')
    if type(count) is not int or count < 1 or not isinstance(pages, list):
        raise ValueError('Actual page_count and per-page review are required')
    if [item.get('page') for item in pages] != list(range(1, count + 1)):
        raise ValueError('Every page must be reviewed in order')
    if any(item.get('status') != 'passed' or not isinstance(item.get('summary'), str) or not item['summary'].strip() for item in pages):
        raise ValueError('Unresolved page review; revise and inspect again')
    if review.get('artifact_sha256') != digest(artifact):
        raise ValueError('Review must name the actual final artifact hash')
    result = json.loads(receipt.read_text(encoding='utf-8')) if receipt.exists() else {}
    persistent = {key: value for key, value in record.items() if key != 'input_path'}
    result['delivery_review'] = dict(persistent, accepted=True, review=review,
                                   artifact=str(artifact.relative_to(root)), artifact_sha256=digest(artifact))
    write_text_atomic(receipt, json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    record.update(accepted=True, artifact=str(artifact), artifact_sha256=digest(artifact),
                  receipt=str(receipt), receipt_sha256=digest(receipt))
    write_text_atomic(marker, json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    return {'accepted': True, 'receipt': str(receipt)}


def verify_prepared(work, artifact, receipt):
    marker = Path(work) / '.delivery.json'
    if not marker.exists():
        return  # Legacy work and non-learning system artifacts retain their contract.
    record = json.loads(marker.read_text(encoding='utf-8'))
    if not record.get('accepted') or record.get('artifact') != str(Path(artifact).resolve()) or record.get('receipt') != str(Path(receipt).resolve()):
        raise ValueError('Prepared learning delivery must be accepted before cleanup')
    if record['artifact_sha256'] != digest(artifact) or record['receipt_sha256'] != digest(receipt):
        raise ValueError('Accepted artifact or review receipt changed; retain recovery files')
