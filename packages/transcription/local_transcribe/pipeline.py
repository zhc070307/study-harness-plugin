from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .audio import decode_audio, prepare_segments
from .models import ASR_REPO, QwenAsr, ensure_model, release_memory
from .paths import logs_root
from .task_store import (
    artifact_path,
    atomic_write_json,
    cancel_requested,
    cleanup_task_work,
    clear_cancel,
    read_task,
    task_work_dir,
    transcript_output_path,
    update_task,
)


class TaskCancelled(Exception):
    pass


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Pipeline:
    def __init__(self, task_id: str, emit: Callable[[dict[str, Any]], None]) -> None:
        self.task_id = task_id
        self.emit = emit
        self.root = task_work_dir(task_id)
        self.root.mkdir(parents=True, exist_ok=True)
        self.log_path = logs_root() / f"{task_id}.log"

    def event(
        self,
        stage: str,
        message: str,
        progress: float | None = None,
        **extra: Any,
    ) -> None:
        payload: dict[str, Any] = {"type": "progress", "stage": stage, "message": message}
        if progress is not None:
            payload["progress"] = max(0.0, min(1.0, progress))
        payload.update(extra)
        changes: dict[str, Any] = {"stage": message}
        if progress is not None:
            changes["progress"] = payload["progress"]
        update_task(self.task_id, **changes)
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        # Persist first so the GUI can safely reread the task as soon as it
        # receives this event without racing the worker's state update.
        self.emit(payload)

    def check_cancelled(self) -> None:
        if cancel_requested(self.task_id):
            raise TaskCancelled

    def run(self, *, skip_refiner: bool = False, max_segments: int | None = None) -> None:
        clear_cancel(self.task_id)
        started = time.monotonic()
        try:
            task = read_task(self.task_id)
            previous_status = str(task.get("status", "created"))
            source = Path(task["source_path"])
            raw_path = (
                Path(str(task.get("raw_transcript", "")))
                if task.get("raw_transcript")
                else transcript_output_path(self.task_id, "raw")
            )
            decoded = artifact_path(self.task_id, "preprocessed/decoded.wav")
            manifest_path = artifact_path(self.task_id, "segments/manifest.json")
            segments_dir = artifact_path(self.task_id, "segments/audio")
            results_dir = artifact_path(self.task_id, "segment-results")

            refiner_mode = "raw"
            if raw_path.is_file():
                raw_text = raw_path.read_text(encoding="utf-8").strip()
                if refiner_mode == "raw":
                    resume_message = "完整原稿已保存；无需重新转写"
                elif previous_status in {"refining", "failed", "cancelled"}:
                    resume_message = (
                        "完整原稿已保存；本次将重新通过 API 整理全文（不重新转写）"
                    )
                else:
                    resume_message = "完整原稿已保存；本次仅通过 API 整理全文"
                self.event(
                    "raw_ready",
                    resume_message,
                    0.73,
                )
            else:
                if not source.is_file():
                    raise FileNotFoundError(f"原录音已被移动或删除：{source}")
                results_dir.mkdir(parents=True, exist_ok=True)
                update_task(self.task_id, status="preprocessing", error=None)
                if not decoded.is_file():
                    self.event("preprocessing", "正在解码音频", 0.02)
                    decode_audio(source, decoded)
                elif previous_status in {"preprocessing", "cancelled", "failed"}:
                    self.event("preprocessing", "已恢复解码结果，继续检测语音", 0.04)
                self.check_cancelled()

                if manifest_path.is_file():
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    self.event(
                        "segmenting",
                        f"已恢复 {len(manifest)} 个音频片段的切分结果",
                        0.20,
                    )
                else:
                    self.event("segmenting", "正在检测语音和自然停顿", 0.05)

                    def segment_progress(done: int, total: int) -> None:
                        self.event(
                            "segmenting",
                            f"正在增强并准备音频片段 {done}/{total}",
                            0.05 + 0.15 * done / max(total, 1),
                        )

                    manifest = prepare_segments(
                        decoded, segments_dir, manifest_path, progress=segment_progress
                    )
                if max_segments is not None:
                    manifest = manifest[:max_segments]
                if not manifest:
                    raise RuntimeError("音频中没有找到可转写内容。")
                self.check_cancelled()

                asr_path = ensure_model(
                    ASR_REPO,
                    "Qwen3-ASR-1.7B-hf",
                    lambda message: self.event("model", message, 0.20),
                )
                update_task(self.task_id, status="transcribing")
                missing_results = [
                    segment
                    for segment in manifest
                    if not (results_dir / f"segment-{int(segment['index']):04d}.json").is_file()
                ]
                completed_count = len(manifest) - len(missing_results)
                if completed_count:
                    self.event(
                        "transcribing",
                        f"已恢复转写断点 {completed_count}/{len(manifest)}，从下一个未完成片段继续",
                        0.22 + 0.50 * completed_count / len(manifest),
                    )
                asr = None
                if missing_results:
                    self.event("transcribing", "正在加载 Qwen3-ASR-1.7B", 0.21)
                    asr_progress = [0.21]

                    def report_asr_status(message: str) -> None:
                        self.event("transcribing", message, asr_progress[0])

                    asr = QwenAsr(
                        asr_path,
                        report_asr_status,
                    )
                    self.event(
                        "transcribing",
                        f"模型已加载，正在使用 {asr.device} 推理",
                        0.22,
                        device=asr.device,
                    )
                for number, segment in enumerate(manifest, start=1):
                    self.check_cancelled()
                    result_path = results_dir / f"segment-{int(segment['index']):04d}.json"
                    if result_path.is_file():
                        result = json.loads(result_path.read_text(encoding="utf-8"))
                    else:
                        if asr is None:
                            raise RuntimeError("ASR 模型未加载。")
                        asr_progress[0] = 0.22 + 0.50 * (number - 1) / len(manifest)
                        self.event(
                            "transcribing",
                            f"正在转写片段 {number}/{len(manifest)}",
                            asr_progress[0],
                        )
                        result = asr.transcribe(Path(str(segment["path"])))
                        result.update(
                            index=int(segment["index"]),
                            start_seconds=float(segment["start_seconds"]),
                            end_seconds=float(segment["end_seconds"]),
                        )
                        atomic_write_json(result_path, result)
                if asr is not None:
                    del asr
                release_memory()

                results = [
                    json.loads(
                        (results_dir / f"segment-{int(segment['index']):04d}.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    for segment in manifest
                ]
                raw_text = "\n\n".join(
                    str(item.get("text", "")).strip() for item in results if item.get("text")
                ).strip()
                if not raw_text:
                    raise RuntimeError("ASR 没有生成可保存的文字内容。")
                atomic_write_text(raw_path, raw_text + "\n")
                update_task(self.task_id, status="raw_ready", raw_transcript=str(raw_path))
                self.event("raw_ready", "原始文字稿已保存", 0.73, artifact=str(raw_path))
                # ASR can now be resumed from the durable raw transcript. Its large
                # decoded audio, enhanced chunks and per-segment checkpoints are disposable.
                for path in (decoded.parent, manifest_path.parent, results_dir):
                    shutil.rmtree(path, ignore_errors=True)
            self.check_cancelled()

            if refiner_mode == "raw":
                update_task(
                    self.task_id,
                    status="completed",
                    stage="原稿已完成",
                    progress=1.0,
                    refiner_mode="raw",
                    duration_seconds=time.monotonic() - started,
                )
                self.event("completed", "本地转写已完成，原稿已保存", 1.0, artifact=str(raw_path))
                cleanup_task_work(self.task_id)
                return

        except TaskCancelled:
            update_task(self.task_id, status="cancelled", stage="已停止")
            self.emit({"type": "cancelled", "message": "任务已停止；已完成结果已经保留。"})
            task = read_task(self.task_id)
            if task.get("raw_transcript") and Path(task["raw_transcript"]).is_file():
                cleanup_task_work(self.task_id)
        except Exception as exc:
            task = read_task(self.task_id)
            raw_is_saved = bool(
                task.get("raw_transcript") and Path(task["raw_transcript"]).is_file()
            )
            message = str(exc)
            if raw_is_saved and "原稿已保留" not in message:
                message = f"{message}；原稿已保留，可继续转写。"
            update_task(self.task_id, status="failed", stage="失败", error=message)
            self.emit({"type": "error", "message": message})
            if raw_is_saved:
                cleanup_task_work(self.task_id)
            raise
