from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .config import VoiceConfig
from .events import VoiceEvent, VoiceEventType, VoiceTurnState


class VoiceOrchestrator:
    """Unifies voice events into a deterministic turn-state machine."""

    def __init__(self, config: VoiceConfig, *, now_fn: Callable[[], float] | None = None) -> None:
        self.config = config
        self._now = now_fn or time.time
        self._lock = threading.Lock()
        self.state = VoiceTurnState.IDLE
        self.active_speaker_id: str | None = None
        self.last_partial_text: str = ""
        self.last_final_text: str = ""
        self.last_partial_role: str = ""
        self.last_final_role: str = ""
        self._events: list[VoiceEvent] = []

    def transition(self, event_type: VoiceEventType, payload: dict | None = None) -> VoiceEvent:
        payload = payload or {}
        event = VoiceEvent(type=event_type, timestamp_s=self._now(), payload=payload)
        with self._lock:
            self._apply(event)
            self._events.append(event)
        return event

    def snapshot(self) -> dict[str, str | bool | None]:
        with self._lock:
            return {
                "voice_enabled": self.config.enabled,
                "mode": self.config.mode,
                "state": self.state.value,
                "active_speaker_id": self.active_speaker_id,
                "last_partial_text": self.last_partial_text,
                "last_final_text": self.last_final_text,
                "last_partial_role": self.last_partial_role,
                "last_final_role": self.last_final_role,
            }

    def drain_events(self) -> list[VoiceEvent]:
        with self._lock:
            out = self._events[:]
            self._events.clear()
            return out

    def _apply(self, event: VoiceEvent) -> None:
        et = event.type
        p = event.payload
        if et == VoiceEventType.USER_SPEAKING:
            if self.state == VoiceTurnState.AGENT_SPEAKING:
                self.state = VoiceTurnState.INTERRUPTED
            else:
                self.state = VoiceTurnState.USER_TURN
            return

        if et == VoiceEventType.BARGE_IN:
            self.state = VoiceTurnState.INTERRUPTED
            return

        if et == VoiceEventType.TURN_END:
            if self.state in {VoiceTurnState.USER_TURN, VoiceTurnState.INTERRUPTED}:
                self.state = VoiceTurnState.AGENT_THINKING
            return

        if et == VoiceEventType.AGENT_THINKING:
            self.state = VoiceTurnState.AGENT_THINKING
            return

        if et == VoiceEventType.AGENT_SPEAKING:
            self.state = VoiceTurnState.AGENT_SPEAKING
            return

        if et == VoiceEventType.ASR_PARTIAL:
            self.last_partial_text = str(p.get("text") or "")
            self.last_partial_role = str(p.get("role") or "")
            return

        if et == VoiceEventType.ASR_FINAL:
            self.last_final_text = str(p.get("text") or "")
            self.last_final_role = str(p.get("role") or "")
            return

        if et == VoiceEventType.SPEAKER_SWITCH:
            self.active_speaker_id = str(p.get("speaker_id") or "") or None
            return

        if et == VoiceEventType.SILENCE_TIMEOUT:
            self.state = VoiceTurnState.IDLE
