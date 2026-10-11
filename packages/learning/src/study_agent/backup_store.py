"""Portable, hash-checked backup references and lossless archive rotation."""
import hashlib
import os
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path, PureWindowsPath

from . import db
from .errors import ConflictError, NotFoundError
from .models import Clock, from_iso, HKT


def backup_path(root, reference):
    reference = str(reference)
    normalized = reference.replace('\\', '/')
    path = Path(normalized)
    absolute = normalized.startswith('/') or path.is_absolute() or PureWindowsPath(reference).is_absolute()
    marker = '.study-system/backups/'
    if absolute and marker in normalized:
        return Path(root) / marker / normalized.split(marker, 1)[1]
    return path if absolute else Path(root) / path


def verified_bytes(content, expected_hash):
    """Accept old Windows text backups only if canonical newlines match their recorded hash."""
    if hashlib.sha256(content).hexdigest() == expected_hash:
        return content
    normalized = content.replace(b'\r\n', b'\n').replace(b'\r', b'\n')
    if hashlib.sha256(normalized).hexdigest() == expected_hash:
        return normalized
    raise ConflictError('Backup hash mismatch')


def read_backup(root, reference, expected_hash=None):
    archive, separator, member = reference.partition('::')
    path = backup_path(root, archive)
    if not path.is_file():
        if not separator and 'course-context' in path.parts and expected_hash:
            course_id = path.parent.name
            canonical = Path(root) / '.study-system/backups/course-context' / course_id / (expected_hash + '.md')
            if canonical.is_file():
                return read_backup(root, str(canonical), expected_hash)
            archived = Path(root) / '.study-system/backups/archives/course-context.zip'
            if archived.is_file():
                return read_backup(root, str(archived) + '::' + course_id + '/' + expected_hash + '.md', expected_hash)
        if not separator and 'agent-memory' in path.parts:
            archived = Path(root) / '.study-system/backups/archives/agent-memory.zip'
            if archived.is_file():
                return read_backup(root, str(archived) + '::' + path.name, expected_hash or path.stem)
        raise NotFoundError('Backup not found: ' + reference)
    if separator:
        with zipfile.ZipFile(path) as stream:
            content = stream.read(member)
    else:
        content = path.read_bytes()
    if expected_hash:
        try:
            content = verified_bytes(content, expected_hash)
        except ConflictError:
            raise ConflictError('Backup hash mismatch: ' + reference) from None
    return content.decode('utf-8')


def _archive(path, additions):
    """Publish and verify the archive before any original can be removed."""
    entries = {}
    if path.exists():
        with zipfile.ZipFile(path) as old:
            entries = {name: old.read(name) for name in old.namelist()}
    for name, content in additions.items():
        if name in entries and entries[name] != content:
            raise ConflictError('Archive member conflicts: ' + name)
        entries[name] = content
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix='.zip.tmp')
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as stream:
            for name, content in sorted(entries.items()):
                stream.writestr(name, content)
        with zipfile.ZipFile(temporary) as stream:
            for name, content in entries.items():
                if stream.read(name) != content:
                    raise ConflictError('Archive verification failed: ' + name)
        with open(temporary, 'r+b') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def maintain_backups(conn, root, clock=None, force=False, retention_days=0):
    with db.atomic(conn):
        result = _maintain_backups(conn, root, clock, force, retention_days)
    if not result.get("skipped") and not conn.in_transaction:
        db.backup(conn, Path(root) / ".study-system/backups/latest.sqlite3")
    return result


def _maintain_backups(conn, root, clock=None, force=False, retention_days=0):
    """Deduplicate and archive indexed contents; loose retention is optional."""
    root = Path(root).resolve()
    clock = clock or Clock()
    previous = conn.execute("SELECT value_json,updated_at FROM system_checkpoints WHERE key='backup.last_maintenance'").fetchone()
    if previous and not force:
        import json
        saved = json.loads(previous['value_json'])
        if saved.get('root') == str(root) and saved.get('retention_days') == retention_days and from_iso(previous['updated_at']).astimezone(HKT).date() == clock.today():
            return dict(saved, skipped=True)
    if retention_days < 0:
        raise ValueError("retention_days must be nonnegative")
    cutoff = clock.now() - timedelta(days=retention_days)
    from .learning import LearningService
    service = LearningService(conn, clock)
    folders = service.course_roots(root)
    rows = list(conn.execute('SELECT * FROM course_context_snapshots ORDER BY created_at DESC'))
    groups = {}
    for row in rows:
        if row['course_id'] not in folders:
            raise NotFoundError('Course folder missing: ' + row['course_id'])
        groups.setdefault((row['course_id'], row['content_hash']), []).append(row)
    updates, additions, removals = [], {}, set()
    repaired = archived = duplicates = 0
    # Validate every indexed original before publishing archives or changing rows.
    for (course_id, digest), versions in groups.items():
        content = read_backup(root, versions[0]['snapshot_path'], digest)
        for row in versions[1:]:
            read_backup(root, row['snapshot_path'], digest)
        files = {backup_path(root, row['snapshot_path']) for row in versions if '::' not in row['snapshot_path']}
        recent = retention_days > 0 and from_iso(versions[0]['created_at']) >= cutoff
        if recent:
            target = root / '.study-system/backups/course-context' / course_id / (digest + '.md')
            db.write_text_atomic(target, content)
            reference = str(target.relative_to(root))
            removals.update(files - {target})
        else:
            member = course_id + '/' + digest + '.md'
            additions[member] = content.encode('utf-8')
            reference = '.study-system/backups/archives/course-context.zip::' + member
            removals.update(files)
            archived += len(files)
        duplicates += max(0, len(files) - 1)
        source = str((folders[course_id][1] / 'COURSE.md').relative_to(root))
        for row in versions:
            repaired += int(row['snapshot_path'] != reference or row['source_path'] != source)
            updates.append((source, reference, row['id']))
    memory_additions, memory_removals = {}, set()
    for path in (root / '.study-system/backups/agent-memory').glob('*.md'):
        content = path.read_bytes()
        digest = path.stem
        try:
            content = verified_bytes(content, digest)
        except ConflictError:
            raise ConflictError('Action-memory backup filename/hash mismatch: ' + str(path)) from None
        if retention_days == 0 or path.stat().st_mtime < cutoff.timestamp():
            memory_additions[digest + '.md'] = content
            memory_removals.add(path)
    if additions:
        _archive(root / '.study-system/backups/archives/course-context.zip', additions)
    if memory_additions:
        _archive(root / '.study-system/backups/archives/agent-memory.zip', memory_additions)
    # Referenced course versions stay recoverable even if cleanup is interrupted.
    with db.atomic(conn):
        conn.executemany('UPDATE course_context_snapshots SET source_path=?,snapshot_path=? WHERE id=?', updates)
    for path in removals | memory_removals:
        path.unlink(missing_ok=True)
    result = {'root': str(root), 'course_records_repaired': repaired,
              'duplicate_loose_copies_removed': duplicates, 'course_files_archived': archived,
              'memory_files_archived': len(memory_removals), 'retention_days': retention_days}
    service.checkpoint('backup.last_maintenance', result)
    return result
