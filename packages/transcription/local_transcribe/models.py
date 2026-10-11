from __future__ import annotations

import gc
import json
import os
from collections.abc import Callable
from pathlib import Path

import torch
from huggingface_hub import snapshot_download
from huggingface_hub.errors import LocalEntryNotFoundError

from .paths import models_root

ASR_REPO = "Qwen/Qwen3-ASR-1.7B-hf"


def model_is_complete(target: Path) -> bool:
    from safetensors import safe_open

    def valid_weights(path: Path) -> bool:
        if not path.is_file() or path.stat().st_size <= 1_000_000:
            return False
        try:
            with safe_open(path, framework="pt", device="cpu") as handle:
                return bool(handle.keys())
        except Exception:  # noqa: BLE001 - incomplete/corrupt weight files vary by backend
            return False

    if not (target / "config.json").is_file():
        return False
    indexes = sorted(target.glob("*.safetensors.index.json"))
    if indexes:
        try:
            payload = json.loads(indexes[0].read_text(encoding="utf-8"))
            weight_files = {str(name) for name in payload["weight_map"].values()}
        except (OSError, KeyError, TypeError, ValueError):
            return False
        return bool(weight_files) and all(valid_weights(target / name) for name in weight_files)
    weights = sorted(target.glob("*.safetensors"))
    return bool(weights) and all(valid_weights(path) for path in weights)


def ensure_model(
    repo_id: str,
    folder_name: str,
    on_status: Callable[[str], None] | None = None,
) -> Path:
    target = models_root() / folder_name
    if model_is_complete(target):
        return target
    target.mkdir(parents=True, exist_ok=True)
    if on_status:
        on_status(f"正在下载模型 {repo_id}…")
    options = {
        "repo_id": repo_id,
        "local_dir": target,
        "allow_patterns": [
            "*.json",
            "*.jinja",
            "*.safetensors",
            "*.model",
            "*.txt",
            "LICENSE*",
            "README*",
            "merges.txt",
            "vocab.json",
        ],
    }
    try:
        # Reuse a complete standard Hugging Face cache when one already exists.
        snapshot_download(**options, local_files_only=True)
    except LocalEntryNotFoundError:
        snapshot_download(**options)
    if not model_is_complete(target):
        raise RuntimeError(f"模型下载不完整：{repo_id}")
    return target


def detect_device() -> tuple[str, torch.dtype]:
    if os.environ.get("LOCALTRANSCRIBE_DEVICE")=="cpu":
        return "cpu", torch.float32
    if torch.backends.mps.is_available():
        return "mps", torch.bfloat16
    if torch.cuda.is_available():
        if torch.cuda.is_bf16_supported():
            return "cuda:0", torch.bfloat16
        return "cuda:0", torch.float16
    xpu = getattr(torch, "xpu", None)
    if xpu is not None and xpu.is_available():
        supports_bfloat16 = getattr(xpu, "is_bf16_supported", None)
        if callable(supports_bfloat16) and supports_bfloat16():
            return "xpu:0", torch.bfloat16
        return "xpu:0", torch.float16
    raise RuntimeError("没有可用 GPU；未下载模型，也不会自动改用 CPU。只有用户主动选择 CPU 模式才启用。")


def validate_device() -> tuple[str, torch.dtype]:
    device, dtype = detect_device()
    # A reported GPU is insufficient: unsupported kernels/drivers must fail before weights.
    probe = torch.ones((8, 8), device=device, dtype=dtype)
    (probe @ probe).sum().item()
    del probe
    release_memory()
    return device, dtype


def device_details() -> dict[str, str]:
    device, dtype = detect_device()
    if device == "mps":
        backend = "mps"
        name = "Apple GPU"
    elif device.startswith("cuda"):
        backend = "rocm" if getattr(torch.version, "hip", None) else "cuda"
        name = str(torch.cuda.get_device_name(0))
    elif device.startswith("xpu"):
        backend = "xpu"
        name = str(torch.xpu.get_device_name(0))
    else:
        backend = "cpu"
        name = "CPU"
    return {
        "device": device,
        "dtype": str(dtype),
        "backend": backend,
        "name": name,
    }


def release_memory() -> None:
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    if torch.backends.mps.is_available():
        torch.mps.empty_cache()
    xpu = getattr(torch, "xpu", None)
    if xpu is not None and xpu.is_available():
        xpu.empty_cache()


class QwenAsr:
    def __init__(
        self,
        model_path: Path,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        from transformers import AutoModelForMultimodalLM, AutoProcessor

        self.model_path = model_path
        self.on_status = on_status
        self.model_factory = AutoModelForMultimodalLM
        self.device, self.dtype = detect_device()
        self.processor = AutoProcessor.from_pretrained(str(model_path))
        self.model = self._load_model(self.device, self.dtype)

    def _notify(self, message: str) -> None:
        if self.on_status:
            self.on_status(message)

    def _load_model(self, device: str, dtype: torch.dtype):
        model = self.model_factory.from_pretrained(
            str(self.model_path),
            dtype=dtype,
            low_cpu_mem_usage=True,
        )
        model.to(device)
        model.eval()
        return model

    def _transcribe_once(self, audio_path: Path, context: str) -> dict[str, str]:
        inputs = self.processor.apply_transcription_request(
            audio=str(audio_path),
            prompt=context or None,
        ).to(self.device, self.dtype)
        input_length = int(inputs["input_ids"].shape[1])
        with torch.inference_mode():
            output_ids = self.model.generate(**inputs, max_new_tokens=1_024)
        generated_ids = output_ids[:, input_length:]
        parsed = self.processor.decode(generated_ids, return_format="parsed")[0]
        return {
            "text": str(parsed.get("transcription", "")).strip(),
            "language": str(parsed.get("language", "") or ""),
        }

    def transcribe(self, audio_path: Path, context: str = "") -> dict[str, str]:
        return self._transcribe_once(audio_path, context)
