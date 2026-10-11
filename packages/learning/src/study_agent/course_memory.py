"""Recoverable versions of course context in a non-Git workspace."""
import hashlib
from pathlib import Path
from . import db
from .models import Clock, to_iso
from .uuid7 import uuid7
from .errors import NotFoundError
from .learning import LearningService, _course_row
from .backup_store import read_backup


def snapshot(conn, root, course, reason='Before course context edit'):
    entry = LearningService(conn).course_roots(root).get(_course_row(conn, course)['id'])
    if not entry:
        raise NotFoundError('COURSE.md not found')
    row, folder = entry
    source = folder / 'COURSE.md'
    content = source.read_text(encoding='utf-8')
    digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
    previous = conn.execute('SELECT * FROM course_context_snapshots WHERE course_id=? AND content_hash=?',
                            (row['id'], digest)).fetchone()
    if previous:
        read_backup(root, previous['snapshot_path'], digest)
        return dict(previous)
    clock = Clock()
    identity = str(uuid7(clock.now_ms()))
    target = root / '.study-system' / 'backups' / 'course-context' / row['id'] / (identity + '.md')
    db.write_text_atomic(target, content)
    with db.atomic(conn):
        conn.execute('INSERT INTO course_context_snapshots VALUES (?,?,?,?,?,?,?)',
                     (identity, row['id'], str(source.relative_to(root)), str(target.relative_to(root)), digest, to_iso(clock.now()), reason))
    return dict(conn.execute('SELECT * FROM course_context_snapshots WHERE id=?', (identity,)).fetchone())


def restore(conn, root, identity):
    row = conn.execute('SELECT * FROM course_context_snapshots WHERE id=?', (identity,)).fetchone()
    if not row:
        raise NotFoundError('course snapshot not found')
    content = read_backup(root, row['snapshot_path'], row['content_hash'])
    snapshot(conn, root, row['course_id'], 'Before restoring ' + identity)
    entry = LearningService(conn).course_roots(root)[row['course_id']]
    source = entry[1] / 'COURSE.md'
    db.write_text_atomic(source, content)
    return {'restored': identity, 'path': str(source)}
