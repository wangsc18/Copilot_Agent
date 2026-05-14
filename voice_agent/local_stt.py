from __future__ import annotations

import struct
import time
import wave
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AudioCaptureConfig:
    sample_rate_hz: int = 16000
    channels: int = 1
    sample_width_bytes: int = 2
    blocksize: int = 320
    input_device_index: int = -1

    def validate(self) -> None:
        if self.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be > 0")
        if self.channels <= 0:
            raise ValueError("channels must be > 0")
        if self.sample_width_bytes not in {1, 2, 4}:
            raise ValueError("sample_width_bytes must be one of 1/2/4")
        if self.blocksize <= 0:
            raise ValueError("blocksize must be > 0")


@dataclass(frozen=True)
class AudioRecordResult:
    wav_path: Path
    duration_ms: int
    num_frames: int
    peak_abs: int


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    language: str
    duration_ms: int


class AudioCapture:
    def __init__(self, cfg: AudioCaptureConfig) -> None:
        cfg.validate()
        self.cfg = cfg

    def record_once(self, output_wav: Path, *, max_seconds: float = 30.0) -> AudioRecordResult:
        if max_seconds <= 0:
            raise ValueError("max_seconds must be > 0")
        try:
            import sounddevice as sd  # type: ignore
        except Exception as exc:
            raise RuntimeError("sounddevice is required for local STT recording") from exc

        output_wav.parent.mkdir(parents=True, exist_ok=True)
        input_device = self.cfg.input_device_index if self.cfg.input_device_index >= 0 else None
        chunks: list[bytes] = []
        peak = 0
        started = time.time()
        frames = 0

        with sd.RawInputStream(
            samplerate=self.cfg.sample_rate_hz,
            channels=self.cfg.channels,
            dtype="int16",
            blocksize=self.cfg.blocksize,
            device=input_device,
        ) as stream:
            print("[local_stt] Recording... press Enter to stop.", flush=True)
            # Wait for a line from stdin without busy polling.
            import threading

            stop_flag = {"stop": False}

            def _wait_enter() -> None:
                try:
                    input()
                except EOFError:
                    pass
                stop_flag["stop"] = True

            waiter = threading.Thread(target=_wait_enter, daemon=True)
            waiter.start()

            while not stop_flag["stop"]:
                if time.time() - started >= max_seconds:
                    break
                data, _overflow = stream.read(self.cfg.blocksize)
                raw = bytes(data) if data else b""
                if not raw:
                    continue
                chunks.append(raw)
                frames += 1
                for (sample,) in struct.iter_unpack("<h", raw):
                    sample_abs = abs(sample)
                    if sample_abs > peak:
                        peak = sample_abs

        audio = b"".join(chunks)
        _write_pcm16_wav(output_wav, audio, sample_rate_hz=self.cfg.sample_rate_hz, channels=self.cfg.channels)
        duration_ms = int((len(audio) / (self.cfg.channels * self.cfg.sample_width_bytes) / self.cfg.sample_rate_hz) * 1000)
        return AudioRecordResult(
            wav_path=output_wav,
            duration_ms=duration_ms,
            num_frames=frames,
            peak_abs=peak,
        )

    def record_for_duration(self, output_wav: Path, *, seconds: float) -> AudioRecordResult:
        if seconds <= 0:
            raise ValueError("seconds must be > 0")
        try:
            import sounddevice as sd  # type: ignore
        except Exception as exc:
            raise RuntimeError("sounddevice is required for local STT recording") from exc

        output_wav.parent.mkdir(parents=True, exist_ok=True)
        input_device = self.cfg.input_device_index if self.cfg.input_device_index >= 0 else None
        chunks: list[bytes] = []
        peak = 0
        frames = 0
        started = time.time()
        deadline = started + seconds

        with sd.RawInputStream(
            samplerate=self.cfg.sample_rate_hz,
            channels=self.cfg.channels,
            dtype="int16",
            blocksize=self.cfg.blocksize,
            device=input_device,
        ) as stream:
            while time.time() < deadline:
                data, _overflow = stream.read(self.cfg.blocksize)
                raw = bytes(data) if data else b""
                if not raw:
                    continue
                chunks.append(raw)
                frames += 1
                for (sample,) in struct.iter_unpack("<h", raw):
                    sample_abs = abs(sample)
                    if sample_abs > peak:
                        peak = sample_abs

        audio = b"".join(chunks)
        _write_pcm16_wav(output_wav, audio, sample_rate_hz=self.cfg.sample_rate_hz, channels=self.cfg.channels)
        duration_ms = int((len(audio) / (self.cfg.channels * self.cfg.sample_width_bytes) / self.cfg.sample_rate_hz) * 1000)
        return AudioRecordResult(
            wav_path=output_wav,
            duration_ms=duration_ms,
            num_frames=frames,
            peak_abs=peak,
        )


class LocalWhisperSTT:
    def __init__(self, *, model_name: str = "base", device: str = "auto", compute_type: str = "auto") -> None:
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self._model = None

    def transcribe(self, wav_path: Path) -> TranscriptionResult:
        if not wav_path.exists():
            raise FileNotFoundError(f"audio file not found: {wav_path}")
        started = time.time()
        model = self._ensure_model()
        try:
            segments, info = model.transcribe(str(wav_path))
        except Exception as exc:
            raise RuntimeError(f"local whisper transcribe failed: {type(exc).__name__}: {exc}") from exc
        text = " ".join((s.text or "").strip() for s in segments).strip()
        duration_ms = int((time.time() - started) * 1000)
        language = str(getattr(info, "language", "") or "")
        return TranscriptionResult(text=text, language=language, duration_ms=duration_ms)

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except Exception as exc:
            raise RuntimeError("faster-whisper is required for local STT") from exc
        model_kwargs = {}
        if self.device != "auto":
            model_kwargs["device"] = self.device
        if self.compute_type != "auto":
            model_kwargs["compute_type"] = self.compute_type
        self._model = WhisperModel(self.model_name, **model_kwargs)
        return self._model


def _write_pcm16_wav(path: Path, raw_pcm16: bytes, *, sample_rate_hz: int, channels: int) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate_hz)
        wf.writeframes(raw_pcm16)
