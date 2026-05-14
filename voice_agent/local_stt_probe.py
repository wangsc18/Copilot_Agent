from __future__ import annotations

"""(已弃用) Manual local STT probe.

The current product path uses the realtime voice gateway. This file is kept
only for historical local Whisper diagnostics.
"""

import os
import time
from pathlib import Path

from .config import VoiceConfig
from .local_stt import AudioCapture, AudioCaptureConfig, LocalWhisperSTT


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def main() -> int:
    _load_dotenv(Path(".env"))
    cfg = VoiceConfig.from_env()
    if not cfg.local_stt_enabled:
        print("[local_stt] LOCAL_STT_ENABLED is false; continuing anyway for manual probe", flush=True)

    sample_rate = 16000
    blocksize = max(160, int(sample_rate * 20 / 1000))
    capture = AudioCapture(
        AudioCaptureConfig(
            sample_rate_hz=sample_rate,
            channels=1,
            sample_width_bytes=2,
            blocksize=blocksize,
            input_device_index=cfg.input_device_index,
        )
    )
    out_dir = Path("voice_agent") / ".artifacts"
    out_path = out_dir / f"local_stt_probe_{int(time.time())}.wav"

    record_seconds = float(os.getenv("LOCAL_STT_RECORD_SECONDS") or "5")
    if record_seconds <= 0:
        record_seconds = 5.0

    print("[local_stt] Press Enter to start recording...", flush=True)
    try:
        input()
    except EOFError:
        pass

    try:
        print(f"[local_stt] Recording for {record_seconds:.1f}s...", flush=True)
        rec = capture.record_for_duration(out_path, seconds=record_seconds)
        print(
            "[local_stt] capture done "
            + f"path={rec.wav_path} duration_ms={rec.duration_ms} frames={rec.num_frames} peak={rec.peak_abs}",
            flush=True,
        )
        stt = LocalWhisperSTT(
            model_name=cfg.local_stt_model,
            device=cfg.local_stt_device,
            compute_type=cfg.local_stt_compute_type,
        )
        tx = stt.transcribe(rec.wav_path)
        print(
            "[local_stt] transcription "
            + f'language="{tx.language}" duration_ms={tx.duration_ms} text="{tx.text}"',
            flush=True,
        )
        if not tx.text.strip():
            print("[local_stt] warning: empty transcription text", flush=True)
            return 1
        return 0
    except Exception as exc:
        print(f"[local_stt] failed: {type(exc).__name__}: {exc}", flush=True)
        if "faster-whisper is required" in str(exc):
            print("[local_stt] tip: pip install faster-whisper", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
