from __future__ import annotations

"""Environment-backed configuration for the realtime voice subsystem."""

import os
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse


@dataclass(frozen=True)
class VoiceConfig:
    enabled: bool = False
    mode: str = "hybrid"
    fallback_text: bool = True
    sample_rate_hz: int = 24000
    frame_ms: int = 20
    vad_silence_ms: int = 450
    input_device_index: int = -1
    providers: tuple[str, ...] = ("volcengine", "mock")
    openai_realtime_model: str = "gpt-realtime"
    openai_realtime_url: str = "wss://api.openai.com/v1/realtime"
    volcengine_realtime_url: str = "wss://openspeech.bytedance.com/api/v3/realtime/dialogue"
    enable_provider_probe: bool = False
    local_stt_enabled: bool = False
    local_stt_model: str = "base"
    local_stt_device: str = "auto"
    local_stt_compute_type: str = "auto"

    @staticmethod
    def from_env() -> "VoiceConfig":
        enabled = _to_bool(os.getenv("VOICE_ENABLED"), default=False)
        mode = (os.getenv("VOICE_MODE") or "hybrid").strip().lower()
        if mode not in {"hybrid", "e2e", "cascade"}:
            mode = "hybrid"
        fallback_text = _to_bool(os.getenv("VOICE_FALLBACK_TEXT"), default=True)
        sample_rate_hz = _to_int(os.getenv("VOICE_SAMPLE_RATE_HZ"), default=24000)
        frame_ms = _to_int(os.getenv("VOICE_FRAME_MS"), default=20)
        vad_silence_ms = _to_int(os.getenv("VOICE_VAD_SILENCE_MS"), default=450)
        input_device_index = _to_int(os.getenv("VOICE_INPUT_DEVICE_INDEX"), default=-1)
        providers = _parse_providers(os.getenv("VOICE_PROVIDERS"))
        openai_realtime_model = (os.getenv("OPENAI_REALTIME_MODEL") or "gpt-realtime").strip()
        openai_realtime_url = (os.getenv("OPENAI_REALTIME_URL") or "").strip()
        if not openai_realtime_url:
            base_url = (os.getenv("OPENAI_BASE_URL") or "").strip()
            openai_realtime_url = _derive_realtime_url_from_base(base_url) or "wss://api.openai.com/v1/realtime"
        volcengine_realtime_url = (
            os.getenv("VOLCENGINE_REALTIME_URL") or "wss://openspeech.bytedance.com/api/v3/realtime/dialogue"
        ).strip()
        enable_provider_probe = _to_bool(os.getenv("VOICE_ENABLE_PROVIDER_PROBE"), default=False)
        local_stt_enabled = _to_bool(os.getenv("LOCAL_STT_ENABLED"), default=False)
        local_stt_model = (os.getenv("LOCAL_STT_MODEL") or "base").strip() or "base"
        local_stt_device = (os.getenv("LOCAL_STT_DEVICE") or "auto").strip().lower() or "auto"
        local_stt_compute_type = (os.getenv("LOCAL_STT_COMPUTE_TYPE") or "auto").strip().lower() or "auto"
        return VoiceConfig(
            enabled=enabled,
            mode=mode,
            fallback_text=fallback_text,
            sample_rate_hz=sample_rate_hz,
            frame_ms=frame_ms,
            vad_silence_ms=vad_silence_ms,
            input_device_index=input_device_index,
            providers=providers,
            openai_realtime_model=openai_realtime_model,
            openai_realtime_url=openai_realtime_url,
            volcengine_realtime_url=volcengine_realtime_url,
            enable_provider_probe=enable_provider_probe,
            local_stt_enabled=local_stt_enabled,
            local_stt_model=local_stt_model,
            local_stt_device=local_stt_device,
            local_stt_compute_type=local_stt_compute_type,
        )


def _to_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _to_int(raw: str | None, *, default: int) -> int:
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except Exception:
        return default


def _parse_providers(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ("volcengine", "mock")
    items = [x.strip().lower() for x in raw.split(",") if x.strip()]
    allowed = {"openai", "volcengine", "mock"}
    deduped: list[str] = []
    for item in items:
        if item in allowed and item not in deduped:
            deduped.append(item)
    if not deduped:
        return ("volcengine", "mock")
    return tuple(deduped)


def _derive_realtime_url_from_base(base_url: str) -> str:
    if not base_url:
        return ""
    try:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"}:
            return ""
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        path = (parsed.path or "").rstrip("/")
        if path.endswith("/v1"):
            realtime_path = path + "/realtime"
        else:
            realtime_path = path + "/v1/realtime" if path else "/v1/realtime"
        return urlunparse((ws_scheme, parsed.netloc, realtime_path, "", "", ""))
    except Exception:
        return ""
