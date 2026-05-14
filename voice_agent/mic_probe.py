from __future__ import annotations

"""(已弃用) Manual microphone probe.

Kept for diagnosing local audio devices. It is not part of the supported
runtime or automated test flow.
"""

import time
import struct


def main() -> int:
    try:
        import sounddevice as sd  # type: ignore
    except Exception as exc:
        print(f"[mic_probe] sounddevice import failed: {type(exc).__name__}: {exc}", flush=True)
        return 2

    sample_rate = 16000
    blocksize = 320
    input_device = -1
    try:
        devices = sd.query_devices()
        inputs = []
        for idx, info in enumerate(devices):
            max_in = int(info.get("max_input_channels") or 0)
            if max_in > 0:
                inputs.append((idx, str(info.get("name") or ""), max_in))
        print("[mic_probe] input devices:", flush=True)
        for idx, name, max_in in inputs[:12]:
            print(f"[mic_probe]   {idx}: {name} (in={max_in})", flush=True)
        default_pair = sd.default.device
        if isinstance(default_pair, (list, tuple)) and len(default_pair) >= 1:
            input_device = int(default_pair[0])
            print(f"[mic_probe] default input device={input_device}", flush=True)
    except Exception as exc:
        print(f"[mic_probe] query_devices failed: {type(exc).__name__}: {exc}", flush=True)
        input_device = -1
    print("[mic_probe] opening microphone stream...", flush=True)
    try:
        with sd.RawInputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
            blocksize=blocksize,
            device=(input_device if input_device >= 0 else None),
        ) as stream:
            print(f"[mic_probe] microphone stream started device={input_device}", flush=True)
            started = time.time()
            frames = 0
            peak = 0
            while time.time() - started < 3.0:
                data, _ = stream.read(blocksize)
                if data:
                    frames += 1
                    raw = bytes(data)
                    for (sample,) in struct.iter_unpack("<h", raw):
                        a = abs(sample)
                        if a > peak:
                            peak = a
            print(f"[mic_probe] peak={peak}", flush=True)
            print(f"[mic_probe] captured_frames={frames}", flush=True)
            if frames <= 0:
                print("[mic_probe] no audio frames captured", flush=True)
                return 1
    except Exception as exc:
        print(f"[mic_probe] microphone open failed: {type(exc).__name__}: {exc}", flush=True)
        return 1
    print("[mic_probe] ok", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
