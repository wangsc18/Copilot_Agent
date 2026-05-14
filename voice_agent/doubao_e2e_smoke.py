from __future__ import annotations

"""(已弃用) Manual Doubao end-to-end smoke script.

Kept for historical debugging only. The supported path is launching
external_agent_chat_ui.py with VOICE_ENABLED=true and using the normal UI.
"""

import os
import queue
import threading
import time
import uuid
import wave
from pathlib import Path

from voice_agent import VoiceConfig, VoiceEventType, VoiceOrchestrator
from voice_agent.local_stt import AudioCapture, AudioCaptureConfig
from voice_agent.session import VolcengineRealtimeSession


def _print_input_devices_preview() -> None:
    try:
        import sounddevice as sd  # type: ignore
    except Exception as exc:
        print(f"[doubao_e2e] sounddevice unavailable: {type(exc).__name__}: {exc}", flush=True)
        return
    try:
        devices = sd.query_devices()
        print("[doubao_e2e] input devices:", flush=True)
        for idx, info in enumerate(devices):
            max_in = int(info.get("max_input_channels") or 0)
            if max_in > 0:
                name = str(info.get("name") or "")
                print(f"[doubao_e2e]   {idx}: {name} (in={max_in})", flush=True)
    except Exception as exc:
        print(f"[doubao_e2e] query_devices failed: {type(exc).__name__}: {exc}", flush=True)


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


def _make_cfg() -> VoiceConfig:
    base = VoiceConfig.from_env()
    sample_rate = int(os.getenv("DOUBAO_SAMPLE_RATE_HZ") or "16000")
    frame_ms = int(os.getenv("DOUBAO_FRAME_MS") or "20")
    input_device_raw = (os.getenv("DOUBAO_INPUT_DEVICE_INDEX") or "").strip()
    if input_device_raw:
        try:
            input_device_index = int(input_device_raw)
        except Exception:
            input_device_index = -1
    else:
        input_device_index = -1

    return VoiceConfig(
        enabled=True,
        mode="e2e",
        fallback_text=True,
        sample_rate_hz=sample_rate,
        frame_ms=frame_ms,
        vad_silence_ms=base.vad_silence_ms,
        input_device_index=input_device_index,
        providers=("volcengine",),
        openai_realtime_model=base.openai_realtime_model,
        openai_realtime_url=base.openai_realtime_url,
        volcengine_realtime_url=base.volcengine_realtime_url,
        enable_provider_probe=base.enable_provider_probe,
        local_stt_enabled=base.local_stt_enabled,
        local_stt_model=base.local_stt_model,
        local_stt_device=base.local_stt_device,
        local_stt_compute_type=base.local_stt_compute_type,
    )


class PushAudioSession(VolcengineRealtimeSession):
    """Disable internal mic capture; push local wav chunks manually."""

    def _start_audio_workers(self, sd) -> None:  # type: ignore[override]
        if self._play_thread is None:
            import threading

            self._play_thread = threading.Thread(target=self._play_audio_loop, args=(sd,), daemon=True, name="voice_volc_play")
            self._play_thread.start()


def _capture_wav(cfg: VoiceConfig) -> Path:
    seconds = float(os.getenv("DOUBAO_RECORD_SECONDS") or "5")
    if seconds <= 0:
        seconds = 5.0
    out_dir = Path("voice_agent") / ".artifacts"
    out_dir.mkdir(parents=True, exist_ok=True)
    wav_path = out_dir / f"doubao_e2e_input_{int(time.time())}.wav"

    capture = AudioCapture(
        AudioCaptureConfig(
            sample_rate_hz=cfg.sample_rate_hz,
            channels=1,
            sample_width_bytes=2,
            blocksize=max(160, int(cfg.sample_rate_hz * cfg.frame_ms / 1000)),
            input_device_index=cfg.input_device_index,
        )
    )

    print("[doubao_e2e] Press Enter to start local capture...", flush=True)
    try:
        input()
    except EOFError:
        pass
    print(f"[doubao_e2e] recording for {seconds:.1f}s", flush=True)
    rec = capture.record_for_duration(wav_path, seconds=seconds)
    print(
        "[doubao_e2e] local capture done "
        + f"path={rec.wav_path} duration_ms={rec.duration_ms} peak={rec.peak_abs}",
        flush=True,
    )
    return rec.wav_path


def _stream_wav_to_session(session: PushAudioSession, wav_path: Path, frame_ms: int) -> None:
    if not session._session_started.wait(timeout=8.0):  # type: ignore[attr-defined]
        raise RuntimeError("session did not reach started state (handshake not acknowledged)")

    with wave.open(str(wav_path), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        if channels != 1 or width != 2:
            raise RuntimeError(f"wav must be mono/pcm16, got channels={channels}, width={width}")
        frame_samples = max(1, int(rate * frame_ms / 1000))
        frame_bytes = frame_samples * 2
        while True:
            raw = wf.readframes(frame_samples)
            if not raw:
                break
            if len(raw) < frame_bytes:
                raw += b"\x00" * (frame_bytes - len(raw))
            session._send_binary(session._protocol.build_streaming_audio(raw, session_id=session._protocol.session_id))  # type: ignore[attr-defined]
            time.sleep(frame_ms / 1000.0)


def _stream_mic_realtime_to_session(session: PushAudioSession, cfg: VoiceConfig, *, stop_event: threading.Event) -> dict[str, int]:
    if not session._session_started.wait(timeout=8.0):  # type: ignore[attr-defined]
        raise RuntimeError("session did not reach started state (handshake not acknowledged)")
    try:
        import sounddevice as sd  # type: ignore
    except Exception as exc:
        raise RuntimeError("sounddevice is required for realtime mic streaming") from exc

    blocksize = max(160, int(cfg.sample_rate_hz * cfg.frame_ms / 1000))
    input_device = cfg.input_device_index if cfg.input_device_index >= 0 else None
    frames = 0
    peak = 0
    started = time.time()
    with sd.RawInputStream(
        samplerate=cfg.sample_rate_hz,
        channels=1,
        dtype="int16",
        blocksize=blocksize,
        device=input_device,
    ) as stream:
        print(
            "[doubao_e2e] realtime mic streaming started "
            + f"sample_rate={cfg.sample_rate_hz} blocksize={blocksize} device={input_device}",
            flush=True,
        )
        while not stop_event.is_set():
            data, _overflow = stream.read(blocksize)
            raw = bytes(data) if data else b""
            if not raw:
                continue
            frames += 1
            local_peak = 0
            for i in range(0, len(raw), 2):
                sample = int.from_bytes(raw[i : i + 2], byteorder="little", signed=True)
                sample_abs = abs(sample)
                if sample_abs > local_peak:
                    local_peak = sample_abs
            if local_peak > peak:
                peak = local_peak
            session._send_binary(session._protocol.build_streaming_audio(raw, session_id=session._protocol.session_id))  # type: ignore[attr-defined]
            time.sleep(cfg.frame_ms / 1000.0)
    # Add short silence tail to help server-side VAD detect turn end.
    silence_tail_ms = int(os.getenv("DOUBAO_SILENCE_TAIL_MS") or "500")
    if silence_tail_ms > 0:
        tail_frames = max(1, silence_tail_ms // max(1, cfg.frame_ms))
        silence = b"\x00" * (blocksize * 2)
        for _ in range(tail_frames):
            session._send_binary(session._protocol.build_streaming_audio(silence, session_id=session._protocol.session_id))  # type: ignore[attr-defined]
            time.sleep(cfg.frame_ms / 1000.0)
    duration_ms = int((time.time() - started) * 1000)
    return {"frames": frames, "peak": peak, "duration_ms": duration_ms}


def _wait_enter(prompt: str) -> None:
    print(prompt, flush=True)
    try:
        input()
    except EOFError:
        pass


def _run_push_to_talk_turn(
    session: PushAudioSession,
    cfg: VoiceConfig,
    orch: VoiceOrchestrator,
    *,
    turn_index: int,
    post_wait_seconds: float = 18.0,
) -> dict[str, int | bool]:
    _wait_enter(f"[doubao_e2e] Turn {turn_index}: press Enter to START speaking")
    stop_event = threading.Event()
    stream_stats: dict[str, int] = {"frames": 0, "peak": 0, "duration_ms": 0}
    stream_error: list[str] = []

    def _capture_worker() -> None:
        try:
            stats = _stream_mic_realtime_to_session(session, cfg, stop_event=stop_event)
            stream_stats.update(stats)
        except Exception as exc:
            stream_error.append(f"{type(exc).__name__}: {exc}")

    worker = threading.Thread(target=_capture_worker, daemon=True, name="doubao_ptt_capture")
    worker.start()
    _wait_enter(f"[doubao_e2e] Turn {turn_index}: speaking... press Enter to STOP and wait model response")
    stop_event.set()
    worker.join(timeout=3.0)
    if stream_error:
        raise RuntimeError(stream_error[0])

    print(
        "[doubao_e2e] turn capture done "
        + f"duration_ms={stream_stats.get('duration_ms', 0)} frames={stream_stats.get('frames', 0)} peak={stream_stats.get('peak', 0)}",
        flush=True,
    )

    # Wait for model side response (text + audio playback already handled in session play thread).
    t0 = time.time()
    seen_agent_partial = False
    seen_agent_final = False
    seen_pilot_final = False
    pilot_finals: list[str] = []
    agent_finals: list[str] = []
    last_partial = ""
    last_final = ""
    while time.time() - t0 < post_wait_seconds:
        snap = orch.snapshot()
        partial = str(snap.get("last_partial_text") or "").strip()
        final = str(snap.get("last_final_text") or "").strip()
        partial_role = str(snap.get("last_partial_role") or "").strip().lower()
        final_role = str(snap.get("last_final_role") or "").strip().lower()
        if partial and partial != last_partial:
            last_partial = partial
            print(f"[doubao_e2e] partial({partial_role or 'unknown'}): {partial}", flush=True)
            if partial_role == "agent":
                seen_agent_partial = True
        if final and final != last_final:
            last_final = final
            print(f"[doubao_e2e] final({final_role or 'unknown'}): {final}", flush=True)
            if final_role == "pilot":
                seen_pilot_final = True
                pilot_finals.append(final)
            if final_role == "agent":
                seen_agent_final = True
                agent_finals.append(final)
        if snap.get("state") == "agent_speaking":
            seen_agent_partial = True
        if seen_agent_final:
            break
        time.sleep(0.2)

    ok_turn = bool(seen_pilot_final and (seen_agent_partial or seen_agent_final))
    return {
        "ok_turn": ok_turn,
        "seen_pilot_final": seen_pilot_final,
        "seen_agent_partial": seen_agent_partial,
        "seen_agent_final": seen_agent_final,
        "pilot_final_count": len(pilot_finals),
        "agent_final_count": len(agent_finals),
        "frames": int(stream_stats.get("frames", 0)),
        "peak": int(stream_stats.get("peak", 0)),
    }


def main() -> int:
    _load_dotenv(Path(".env"))
    _print_input_devices_preview()
    cfg = _make_cfg()
    print(
        "[doubao_e2e] cfg "
        + f"sample_rate={cfg.sample_rate_hz} frame_ms={cfg.frame_ms} input_device_index={cfg.input_device_index}",
        flush=True,
    )

    os.environ.setdefault("VOLCENGINE_RESOURCE_ID", "volc.speech.dialog")
    os.environ.setdefault("VOLCENGINE_APP_KEY", "PlgvMymc7f3tQnJ6")
    os.environ.setdefault("VOLCENGINE_MODEL", "o2.0")
    os.environ.setdefault("VOLCENGINE_CONNECT_ID", str(uuid.uuid4()))

    stream_mode = (os.getenv("DOUBAO_STREAM_MODE") or "realtime").strip().lower()
    wav_path: Path | None = None
    if stream_mode == "file":
        wav_path = _capture_wav(cfg)

    orch = VoiceOrchestrator(cfg)
    logs: queue.Queue[str] = queue.Queue()

    def log_fn(msg: str) -> None:
        print(msg, flush=True)
        logs.put(msg)

    session = PushAudioSession(cfg, orch, log_fn=log_fn)
    session.start()
    if stream_mode == "file":
        print("[doubao_e2e] session started, pushing captured wav...", flush=True)
    else:
        print("[doubao_e2e] session started, realtime mic mode (20ms packet recommended)", flush=True)

    run_seconds = float(os.getenv("DOUBAO_TEST_SECONDS") or "15")
    t0 = time.time()
    last_partial = ""
    last_final = ""
    ok_asr = False
    ok_any_transcript = False

    stream_error = ""
    stream_stats: dict[str, int] = {}
    turn_ok = False
    try:
        if stream_mode == "file":
            if wav_path is None:
                raise RuntimeError("wav path is missing in file mode")
            _stream_wav_to_session(session, wav_path, cfg.frame_ms)
        else:
            mode = (os.getenv("DOUBAO_REALTIME_MODE") or "push_to_talk").strip().lower()
            if mode == "push_to_talk":
                turns = int(os.getenv("DOUBAO_TURNS") or "1")
                if turns <= 0:
                    turns = 1
                for i in range(1, turns + 1):
                    turn_result = _run_push_to_talk_turn(session, cfg, orch, turn_index=i, post_wait_seconds=10.0)
                    stream_stats = {"frames": int(turn_result["frames"]), "peak": int(turn_result["peak"])}
                    turn_ok = bool(turn_result["ok_turn"])
                    print(
                        "[doubao_e2e] turn result "
                        + (
                            f"pilot_final={turn_result['seen_pilot_final']} "
                            f"agent_partial={turn_result['seen_agent_partial']} "
                            f"agent_final={turn_result['seen_agent_final']} "
                            f"pilot_final_count={turn_result['pilot_final_count']} "
                            f"agent_final_count={turn_result['agent_final_count']}"
                        ),
                        flush=True,
                    )
            else:
                realtime_seconds = float(os.getenv("DOUBAO_REALTIME_SECONDS") or "12")
                if realtime_seconds <= 0:
                    realtime_seconds = 12.0
                stop_event = threading.Event()
                timer = threading.Timer(realtime_seconds, stop_event.set)
                timer.start()
                try:
                    stream_stats = _stream_mic_realtime_to_session(session, cfg, stop_event=stop_event)
                finally:
                    timer.cancel()
                    print(
                        "[doubao_e2e] realtime mic streaming finished "
                        + f"duration_ms={stream_stats.get('duration_ms', 0)} frames={stream_stats.get('frames', 0)} peak={stream_stats.get('peak', 0)}",
                        flush=True,
                    )
        while time.time() - t0 < run_seconds:
            snap = orch.snapshot()
            partial = str(snap.get("last_partial_text") or "").strip()
            final = str(snap.get("last_final_text") or "").strip()
            if partial and partial != last_partial:
                last_partial = partial
                print(f"[doubao_e2e] ASR_PARTIAL: {partial}", flush=True)
                ok_asr = True
                ok_any_transcript = True
            if final and final != last_final:
                last_final = final
                print(f"[doubao_e2e] ASR_FINAL: {final}", flush=True)
                ok_asr = True
                ok_any_transcript = True
            time.sleep(0.2)
    except Exception as exc:
        stream_error = f"{type(exc).__name__}: {exc}"
        print(f"[doubao_e2e] stream failed: {stream_error}", flush=True)
    finally:
        session.stop()
        time.sleep(0.8)
        print("[doubao_e2e] session stopped", flush=True)

    events = orch.drain_events()
    asr_events = [e for e in events if e.type in {VoiceEventType.ASR_PARTIAL, VoiceEventType.ASR_FINAL}]
    print(f"[doubao_e2e] events={len(events)} asr_events={len(asr_events)}", flush=True)

    drained_logs: list[str] = []
    while not logs.empty():
        drained_logs.append(logs.get_nowait())
    key_logs = [
        x
        for x in drained_logs
        if any(
            k in x.lower()
            for k in [
                "connected",
                "session started",
                "ws error",
                "ws closed",
                "decode skip",
                "volc error",
                "mic stats",
                "audio capture stopped",
                "streaming",
                "raw frame",
            ]
        )
    ]
    debug_logs = (os.getenv("DOUBAO_DEBUG_LOGS") or "").strip().lower() in {"1", "true", "yes", "on"}
    if key_logs and (debug_logs or stream_error or not ok_any_transcript):
        print("[doubao_e2e] key logs:", flush=True)
        for line in key_logs[-30:]:
            print("  " + line, flush=True)

    if stream_error:
        print("[doubao_e2e] handshake/upload failed before ASR; check key logs above", flush=True)
        return 2

    if stream_mode == "realtime" and (os.getenv("DOUBAO_REALTIME_MODE") or "push_to_talk").strip().lower() == "push_to_talk":
        if not turn_ok:
            print("[doubao_e2e] realtime turn not fully verified (missing pilot/agent transcript)", flush=True)
            return 3

    if not ok_any_transcript:
        print("[doubao_e2e] no ASR transcript received", flush=True)
        return 1
    print("[doubao_e2e] success", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
