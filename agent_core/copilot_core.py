from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ControlMode(str, Enum):
    # advisory: 仅建议，不执行
    # assisted: 人机协同执行（当前工具调用默认）
    # supervised_auto: 监督式自动执行（后续可扩展）
    ADVISORY = "advisory"
    ASSISTED = "assisted"
    SUPERVISED_AUTO = "supervised_auto"


class ActionType(str, Enum):
    # 统一的动作类型枚举，供 LLM 工具、Guard 和 Executor 共享
    SET_THROTTLE = "set_throttle"
    SET_PITCH_CMD = "set_pitch_cmd"
    SET_GEAR = "set_gear"
    SET_FLAPS = "set_flaps"
    RELEASE_BRAKES = "release_brakes"


@dataclass
class FlightSnapshot:
    timestamp_s: float
    altitude_ft: float
    altitude_msl_m: float
    airspeed_kts: float
    ground_speed_kts: float
    vertical_speed_fpm: float
    pitch_deg: float
    roll_deg: float
    heading_true_deg: float
    gear_ratio: float
    throttle_cmd: float
    throttle_used_ratio: float
    flaps_ratio: float
    speedbrake_ratio: float
    park_brake_ratio: float
    left_brake_ratio: float
    right_brake_ratio: float
    wind_speed_kt: float | None = None
    wind_direction_deg: float | None = None
    radio_altitude_ft: float | None = None
    on_ground_any: float | None = None


@dataclass
class TrendMetrics:
    duration_s: float
    delta_altitude_ft: float
    delta_airspeed_kts: float
    delta_ground_speed_kts: float
    delta_vsi_fpm: float
    avg_airspeed_kts: float
    avg_vertical_speed_fpm: float
    avg_roll_abs_deg: float
    avg_pitch_deg: float
    avg_throttle_used_ratio: float


@dataclass
class SituationReport:
    phase: str
    confidence: float
    evidence: list[str]
    risks: list[str]
    latest: FlightSnapshot
    trend_10s: TrendMetrics | None
    trend_30s: TrendMetrics | None

    def to_prompt_dict(self) -> dict[str, Any]:
        # 转成可直接注入提示词的结构，避免上层重复拼装
        return {
            "phase": self.phase,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "risks": self.risks,
            "latest": asdict(self.latest),
            "trend_10s": None if self.trend_10s is None else asdict(self.trend_10s),
            "trend_30s": None if self.trend_30s is None else asdict(self.trend_30s),
        }


@dataclass
class Action:
    type: ActionType
    value: float | int | None
    reason: str


@dataclass
class ActionPlan:
    # 一次动作计划可包含多个动作，便于做原子化校验和执行
    requested_by: str
    mode: ControlMode
    actions: list[Action] = field(default_factory=list)


@dataclass
class GuardResult:
    allowed: bool
    violations: list[str]


@dataclass
class ExecResult:
    success: bool
    executed: list[str]
    error: str | None = None
    timestamp_s: float = field(default_factory=time.time)
