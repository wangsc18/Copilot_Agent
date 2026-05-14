from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class VoiceEventType(str, Enum):
    USER_SPEAKING = "user_speaking"
    TURN_END = "turn_end"
    BARGE_IN = "barge_in"
    SPEAKER_SWITCH = "speaker_switch"
    ASR_PARTIAL = "asr_partial"
    ASR_FINAL = "asr_final"
    AGENT_THINKING = "agent_thinking"
    AGENT_SPEAKING = "agent_speaking"
    SILENCE_TIMEOUT = "silence_timeout"


class VoiceTurnState(str, Enum):
    IDLE = "idle"
    USER_TURN = "user_turn"
    AGENT_THINKING = "agent_thinking"
    AGENT_SPEAKING = "agent_speaking"
    INTERRUPTED = "interrupted"


@dataclass
class VoiceEvent:
    type: VoiceEventType
    timestamp_s: float
    payload: dict[str, Any] = field(default_factory=dict)

