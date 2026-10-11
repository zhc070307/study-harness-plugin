from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

SAMPLE_RATE = 16_000


@dataclass(frozen=True)
class Segment:
    index: int
    start_sample: int
    end_sample: int
    sample_rate: int = SAMPLE_RATE

    @property
    def start_seconds(self) -> float:
        return self.start_sample / self.sample_rate

    @property
    def end_seconds(self) -> float:
        return self.end_sample / self.sample_rate

    @property
    def duration_seconds(self) -> float:
        return (self.end_sample - self.start_sample) / self.sample_rate

    def to_json(self) -> dict[str, float | int]:
        payload = asdict(self)
        payload.update(
            start_seconds=self.start_seconds,
            end_seconds=self.end_seconds,
            duration_seconds=self.duration_seconds,
        )
        return payload


def decode_audio(source: Path, destination: Path) -> None:
    import av
    import soundfile as sf

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        container = av.open(str(source))
        stream = next(item for item in container.streams if item.type == "audio")
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=SAMPLE_RATE)
        with sf.SoundFile(
            destination,
            mode="w",
            samplerate=SAMPLE_RATE,
            channels=1,
            subtype="FLOAT",
        ) as output:
            for frame in container.decode(stream):
                for converted in resampler.resample(frame):
                    output.write(converted.to_ndarray().reshape(-1))
            for converted in resampler.resample(None):
                output.write(converted.to_ndarray().reshape(-1))
        container.close()
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"音频解码失败：{exc}") from exc


def load_mono(path: Path) -> np.ndarray:
    import numpy as np
    import soundfile as sf

    waveform, rate = sf.read(path, dtype="float32", always_2d=False)
    if rate != SAMPLE_RATE:
        raise ValueError(f"预期 {SAMPLE_RATE} Hz，实际为 {rate} Hz")
    if waveform.ndim > 1:
        waveform = np.mean(waveform, axis=1)
    return np.asarray(waveform, dtype=np.float32)


def lightly_condition(waveform: np.ndarray) -> np.ndarray:
    """Remove sub-speech rumble and normalize without altering speech content."""
    import numpy as np
    from scipy import signal

    if waveform.size < 32:
        return waveform.astype(np.float32, copy=True)
    sos = signal.butter(4, 70.0, btype="highpass", fs=SAMPLE_RATE, output="sos")
    conditioned = signal.sosfiltfilt(sos, waveform).astype(np.float32)
    reference = float(np.percentile(np.abs(conditioned), 99.5))
    if reference > 1e-6:
        conditioned = conditioned * min(0.95 / reference, 8.0)
    return np.clip(conditioned, -1.0, 1.0).astype(np.float32)


def speech_timestamps(waveform: np.ndarray) -> list[dict[str, int]]:
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad

    # Reuse the application's PyTorch runtime instead of adding ONNX Runtime.
    model = load_silero_vad(onnx=False)
    tensor = torch.from_numpy(waveform)
    timestamps = get_speech_timestamps(
        tensor,
        model,
        sampling_rate=SAMPLE_RATE,
        threshold=0.45,
        min_speech_duration_ms=200,
        min_silence_duration_ms=450,
        speech_pad_ms=120,
        return_seconds=False,
    )
    return [{"start": int(item["start"]), "end": int(item["end"])} for item in timestamps]


def choose_segments(
    total_samples: int,
    speech: Iterable[dict[str, int]],
    *,
    minimum_seconds: float = 80.0,
    target_seconds: float = 120.0,
    maximum_seconds: float = 150.0,
) -> list[Segment]:
    """Create long contiguous segments, preferring natural silence near 120 s."""
    if total_samples <= 0:
        return []
    minimum = int(minimum_seconds * SAMPLE_RATE)
    target = int(target_seconds * SAMPLE_RATE)
    maximum = int(maximum_seconds * SAMPLE_RATE)
    speech_items = sorted(speech, key=lambda item: item["start"])
    gaps: list[tuple[int, int]] = []
    previous_end = 0
    for item in speech_items:
        start = max(0, min(total_samples, int(item["start"])))
        end = max(start, min(total_samples, int(item["end"])))
        if start > previous_end:
            gaps.append((previous_end, start))
        previous_end = max(previous_end, end)
    if previous_end < total_samples:
        gaps.append((previous_end, total_samples))

    boundaries = [0]
    cursor = 0
    while total_samples - cursor > maximum:
        remaining = total_samples - cursor
        # Avoid creating a tiny final tail. A final segment may exceed the
        # preferred maximum by at most 20 seconds; otherwise split the
        # remaining audio into two useful segments.
        if remaining <= maximum + int(20 * SAMPLE_RATE):
            break
        low = cursor + minimum
        if remaining <= maximum + minimum:
            ideal = cursor + remaining // 2
            high = total_samples - minimum
        else:
            ideal = cursor + target
            high = min(cursor + maximum, total_samples)
        candidates: list[tuple[float, int]] = []
        for gap_start, gap_end in gaps:
            midpoint = (gap_start + gap_end) // 2
            if low <= midpoint <= high:
                gap_seconds = (gap_end - gap_start) / SAMPLE_RATE
                distance_seconds = abs(midpoint - ideal) / SAMPLE_RATE
                score = distance_seconds - min(gap_seconds, 3.0) * 2.0
                candidates.append((score, midpoint))
        fallback = ideal if remaining <= maximum + minimum else high
        cut = min(candidates)[1] if candidates else fallback
        if cut <= cursor:
            cut = high
        boundaries.append(cut)
        cursor = cut
    boundaries.append(total_samples)
    return [
        Segment(index=index, start_sample=start, end_sample=end)
        for index, (start, end) in enumerate(pairwise(boundaries))
        if end > start
    ]


def enhance_segment(waveform: np.ndarray) -> np.ndarray:
    import noisereduce as nr
    import numpy as np

    conditioned = lightly_condition(waveform)
    if conditioned.size < SAMPLE_RATE:
        return conditioned
    enhanced = nr.reduce_noise(
        y=conditioned,
        sr=SAMPLE_RATE,
        stationary=False,
        prop_decrease=0.65,
        time_mask_smooth_ms=80,
        freq_mask_smooth_hz=200,
        n_fft=1024,
        hop_length=256,
    )
    enhanced = np.asarray(enhanced, dtype=np.float32)
    peak = float(np.percentile(np.abs(enhanced), 99.8))
    if peak > 1e-6:
        enhanced *= min(0.95 / peak, 4.0)
    return np.clip(enhanced, -1.0, 1.0).astype(np.float32)


def prepare_segments(
    decoded_path: Path,
    segments_dir: Path,
    manifest_path: Path,
    progress: Callable[[int, int], None] | None = None,
) -> list[dict[str, object]]:
    import soundfile as sf

    waveform = load_mono(decoded_path)
    conditioned = lightly_condition(waveform)
    speech = speech_timestamps(conditioned)
    segments = choose_segments(len(waveform), speech)
    segments_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, object]] = []
    for number, segment in enumerate(segments, start=1):
        path = segments_dir / f"segment-{segment.index:04d}.wav"
        if not path.is_file():
            chunk = waveform[segment.start_sample : segment.end_sample]
            sf.write(path, enhance_segment(chunk), SAMPLE_RATE, subtype="PCM_16")
        entry = segment.to_json()
        entry["path"] = str(path)
        manifest.append(entry)
        if progress:
            progress(number, len(segments))
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
