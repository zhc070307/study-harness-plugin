"""Headless adapter for the adopted, raw-only LocalTranscribe pipeline."""
import json
from pathlib import Path
import sys

from local_transcribe.models import validate_device
# Verify the selected accelerator before the pipeline can download model weights.
validate_device()
from local_transcribe.pipeline import Pipeline
from local_transcribe.task_store import create_task, read_task, update_task

directory=Path(sys.argv[1]);job=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
marker=directory/'task-id'
if marker.is_file(): identifier=marker.read_text(encoding='utf-8').strip()
else:
    task=create_task(Path(job['source']),directory/'output')
    identifier=task['id']
    update_task(identifier,name=Path(job['name']).stem,source_name=job['name'],output_basename=Path(job['name']).stem)
    marker.write_text(identifier)
Pipeline(identifier,lambda event:print(json.dumps(event,ensure_ascii=False),flush=True)).run(skip_refiner=True)
task=read_task(identifier)
if task.get('status')!='completed': raise RuntimeError('转写未完成')
(directory/'result.json').write_text(json.dumps({'raw_transcript':task['raw_transcript']}))
