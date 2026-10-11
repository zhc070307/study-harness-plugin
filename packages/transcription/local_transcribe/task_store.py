from __future__ import annotations

import json
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import documents_root, logs_root, task_cache_root, tasks_root

WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def task_dir(task_id: str) -> Path:
    return tasks_root() / task_id


def task_work_dir(task_id: str) -> Path:
    return task_cache_root() / task_id


def task_file(task_id: str) -> Path:
    return task_dir(task_id) / "task.json"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with open(handle, "w", encoding="utf-8", closefd=True) as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
        Path(temporary_name).replace(path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def read_task(task_id: str) -> dict[str, Any]:
    return json.loads(task_file(task_id).read_text(encoding="utf-8"))


def write_task(task: dict[str, Any]) -> None:
    task["updated_at"] = utc_now()
    atomic_write_json(task_file(task["id"]), task)


def update_task(task_id: str, **changes: Any) -> dict[str, Any]:
    task = read_task(task_id)
    task.update(changes)
    write_task(task)
    return task


def safe_output_basename(name: str) -> str:
    """Return a readable filename component that is valid on macOS and Windows."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not cleaned or cleaned.upper().split(".", 1)[0] in WINDOWS_RESERVED_NAMES:
        cleaned = "录音"
    return cleaned[:64].rstrip(" .") or "录音"


def transcript_filename(task: dict[str, Any], kind: str) -> str:
    basename = str(task.get("output_basename") or safe_output_basename(str(task["name"])))
    if kind == "raw":
        return f"{basename}-原稿.txt"
    if kind == "refined":
        return f"{basename}-整理稿.txt"
    raise ValueError(f"未知文稿类型：{kind}")


def create_task(
    source: Path,
    output_root: Path | None = None,
    *,
    batch_id: str | None = None,
    batch_position: int | None = None,
) -> dict[str, Any]:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    identifier = str(uuid.uuid4())
    basename = safe_output_basename(source.stem)
    selected_root = (output_root or documents_root()).expanduser().resolve()
    selected_root.mkdir(parents=True, exist_ok=True)
    output_dir = selected_root / f"{basename}-{identifier[:8]}"
    output_dir.mkdir(parents=True, exist_ok=False)
    root = task_dir(identifier)
    try:
        root.mkdir(parents=True)
    except Exception:
        output_dir.rmdir()
        raise
    now = utc_now()
    task = {
        "id": identifier,
        "name": source.stem,
        "source_name": source.name,
        "source_path": str(source),
        "original_source_path": str(source),
        "output_dir": str(output_dir),
        "output_basename": basename,
        "status": "created",
        "stage": "等待开始",
        "progress": 0.0,
        "created_at": now,
        "updated_at": now,
        "error": None,
    }
    if batch_id:
        task["batch_id"] = batch_id
        task["batch_position"] = int(batch_position or 0)
    write_task(task)
    return task


def list_tasks() -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for path in tasks_root().glob("*/task.json"):
        try:
            tasks.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(tasks, key=lambda item: item.get("created_at", ""), reverse=True)


def artifact_path(task_id: str, name: str) -> Path:
    return task_work_dir(task_id) / name


def output_path(task_id: str, name: str) -> Path:
    task = read_task(task_id)
    output_dir = Path(task.get("output_dir") or documents_root() / f"{task['name']}-{task_id[:8]}")
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / name


def transcript_output_path(task_id: str, kind: str) -> Path:
    task = read_task(task_id)
    return output_path(task_id, transcript_filename(task, kind))


def copy_preserving_existing(source: Path, destination: Path, task_id: str) -> Path:
    """Copy a legacy transcript without overwriting a user-edited destination."""
    if not destination.exists():
        shutil.copy2(source, destination)
        return destination
    if source.read_bytes() == destination.read_bytes():
        return destination
    counter = 1
    while True:
        suffix = f"-迁移-{task_id[:8]}" + (f"-{counter}" if counter > 1 else "")
        alternate = destination.with_name(f"{destination.stem}{suffix}{destination.suffix}")
        if not alternate.exists():
            shutil.copy2(source, alternate)
            return alternate
        if source.read_bytes() == alternate.read_bytes():
            return alternate
        counter += 1


def request_cancel(task_id: str) -> None:
    artifact_path(task_id, "CANCEL").touch()


def clear_cancel(task_id: str) -> None:
    artifact_path(task_id, "CANCEL").unlink(missing_ok=True)


def cancel_requested(task_id: str) -> bool:
    return artifact_path(task_id, "CANCEL").exists()


def cleanup_task_work(task_id: str) -> None:
    shutil.rmtree(task_work_dir(task_id), ignore_errors=True)
    legacy_root = task_dir(task_id)
    for name in (
        "source",
        "preprocessed",
        "segments",
        "segment-results",
        "refined-parts",
        "raw-transcript.json",
        "worker.log",
        "CANCEL",
    ):
        path = legacy_root / name
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def migrate_completed_task(task_id: str) -> dict[str, Any]:
    """Move legacy final files to the task output folder and normalize both to TXT."""
    task = read_task(task_id)
    legacy_candidates = (
        task_dir(task_id) / "raw-transcript.txt",
        task_dir(task_id) / "refined-transcript.md",
        task_dir(task_id) / "refined-transcript.txt",
    )
    recorded_candidates = [
        Path(str(task[key])) for key in ("raw_transcript", "refined_transcript") if task.get(key)
    ]
    if task.get("status") != "completed" and not any(
        path.is_file() for path in (*legacy_candidates, *recorded_candidates)
    ):
        return task
    output_dir = Path(task.get("output_dir") or documents_root() / f"{task['name']}-{task_id[:8]}")
    output_dir.mkdir(parents=True, exist_ok=True)
    changes: dict[str, Any] = {"output_dir": str(output_dir)}
    for key, kind, legacy_names in (
        ("raw_transcript", "raw", ("raw-transcript.txt",)),
        (
            "refined_transcript",
            "refined",
            ("refined-transcript.md", "refined-transcript.txt"),
        ),
    ):
        destination = output_dir / transcript_filename(task, kind)
        candidates = []
        if task.get(key):
            candidates.append(Path(str(task[key])))
        candidates.extend(task_dir(task_id) / name for name in legacy_names)
        candidates.extend(output_dir / name for name in legacy_names)
        candidates.append(destination)
        current = next((candidate for candidate in candidates if candidate.is_file()), None)
        if current is not None and current.resolve() != destination.resolve():
            destination = copy_preserving_existing(current, destination, task_id)
            inside_task = current.resolve().is_relative_to(task_dir(task_id).resolve())
            inside_output = current.resolve().is_relative_to(output_dir.resolve())
            if inside_task or inside_output:
                current.unlink(missing_ok=True)
        if destination.is_file():
            changes[key] = str(destination)
    original = Path(str(task.get("original_source_path", "")))
    if original.is_file():
        changes["source_path"] = str(original)
    task.update(changes)
    write_task(task)
    cleanup_task_work(task_id)
    return task


def delete_task(task_id: str) -> None:
    # Preserve every durable transcript, including files from legacy task layouts.
    task = migrate_completed_task(task_id)
    cleanup_task_work(task_id)
    task_log = logs_root() / f"{task_id}.log"
    task_log.unlink(missing_ok=True)
    shutil.rmtree(task_dir(task_id), ignore_errors=True)
    if task.get("output_dir"):
        try:
            # Avoid leaving an empty task folder when no transcript was ever produced.
            Path(str(task["output_dir"])).rmdir()
        except OSError:
            # A non-empty output folder is user-owned and must never be deleted here.
            pass


def cleanup_startup_caches() -> int:
    """Remove completed and orphaned work while preserving resumable tasks."""
    cleaned = 0
    tasks = list_tasks()
    task_ids = {str(task["id"]) for task in tasks}
    for task in tasks:
        if task.get("status") != "completed":
            continue
        migrate_completed_task(task["id"])
        work = task_work_dir(task["id"])
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
            cleaned += 1
    for path in task_cache_root().iterdir():
        if path.is_dir() and path.name not in task_ids:
            shutil.rmtree(path, ignore_errors=True)
            cleaned += 1
    return cleaned
