"""Retire explicitly finished build folders; never infer completion from age."""
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path
from uuid import uuid4


def _unhide_final_pdf(artifact):
    """Keep a delivered PDF visible in Finder without changing its contents."""
    hidden_flag = getattr(stat, 'UF_HIDDEN', 0)
    flags = getattr(artifact.stat(), 'st_flags', 0)
    if artifact.suffix.lower() != '.pdf' or not hidden_flag or not flags & hidden_flag:
        return
    before = hashlib.sha256(artifact.read_bytes()).digest()
    os.chflags(artifact, flags & ~hidden_flag)
    if getattr(artifact.stat(), 'st_flags', 0) & hidden_flag:
        raise OSError(f'Final PDF is still hidden: {artifact}')
    if hashlib.sha256(artifact.read_bytes()).digest() != before:
        raise OSError(f'Final PDF changed while clearing its hidden flag: {artifact}')


def finish_work(root, work, artifact, receipt, trash_root=None):
    root = Path(root).resolve()
    work = Path(work).resolve()
    runtime = root / '.study-system/runtime'
    if not work.is_relative_to(runtime) or work == runtime or work.is_relative_to(runtime / 'views'):
        raise ValueError('Only a dedicated runtime work folder may be retired')
    artifact, receipt = Path(artifact).resolve(), Path(receipt).resolve()
    if not artifact.is_file() or not artifact.stat().st_size:
        raise ValueError('A nonempty final artifact is required')
    if not receipt.is_file() or not receipt.stat().st_size:
        raise ValueError('A persistent receipt is required')
    if artifact.is_relative_to(runtime) or receipt.is_relative_to(runtime):
        raise ValueError('Artifact and receipt must be persistent, outside the build folder/runtime')
    from .delivery import verify_prepared
    verify_prepared(work, artifact, receipt)
    _unhide_final_pdf(artifact)
    if not work.is_dir():
        return {'already_finished': True}
    marker = work / '.finished.json'
    manifest = {'work': str(work), 'artifact': str(artifact), 'receipt': str(receipt),
                'artifact_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()}
    marker.write_text(json.dumps(manifest))
    return _retire(root, marker, trash_root)


def _retire(root, marker, trash_root=None):
    record = json.loads(marker.read_text(encoding='utf-8'))
    work = marker.parent
    runtime = Path(root).resolve() / '.study-system/runtime'
    if not work.resolve().is_relative_to(runtime) or work.resolve() == runtime or work.resolve().is_relative_to(runtime / 'views'):
        raise ValueError('Unsafe completion marker location')
    if work.resolve() != Path(record['work']).resolve():
        raise ValueError('Completion marker does not match its work folder')
    artifact, receipt = Path(record['artifact']), Path(record['receipt'])
    if not receipt.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest() != record['artifact_sha256']:
        raise ValueError('Final artifact or receipt changed; keep recovery files')
    if any(p.is_symlink() for p in work.rglob('*')):
        raise ValueError('Keep work folder containing symlinks for manual review')
    from .delivery import verify_prepared
    verify_prepared(work, artifact, receipt)
    files = [p for p in work.rglob('*') if p.is_file()]
    destination = Path(trash_root or Path.home() / '.Trash') / ('study-work-' + uuid4().hex)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(work), str(destination))
    return {'files': len(files), 'trash': str(destination)}


def resume_finished(root):
    root = Path(root).resolve()
    results = []
    for marker in (root / '.study-system/runtime').glob('*/.finished.json'):
        try:
            results.append(_retire(root, marker))
        except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
            results.append({'kept': str(marker.parent), 'reason': str(exc)})
    return results
