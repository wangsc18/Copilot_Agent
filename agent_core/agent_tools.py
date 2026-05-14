from __future__ import annotations

"""Backend tool bridge and fast command router.

Core responsibilities:
- Parse low-risk, high-confidence commands in the fast path.
- Expose guarded X-Plane tools to the slow LLM tool loop.
- Keep all aircraft state reads and control writes behind AgentToolBridge.

The UI and voice layers should not read or write X-Plane directly.
"""

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
import time

from .copilot_core import Action, ActionPlan, ActionType, ControlMode
from .copilot_guard_executor import ActionExecutor, ActionGuard
from .copilot_situation import SituationInferenceEngine
from .copilot_state_monitor import FlightStateMonitor
from .background_tools import BackgroundToolRunner


def _extract_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("No JSON object found in model output.")
    return json.loads(text[start : end + 1])


def parse_agent_payload(text: str) -> tuple[str, str]:
    raw = (text or "").strip()
    if not raw:
        return "模型返回空内容。", "模型返回空内容。"
    try:
        data = _extract_json_object(raw)
        reply = str(data.get("reply", "")).strip()
        overlay = str(data.get("overlay", "")).strip()
        if not reply:
            raise ValueError("Missing non-empty 'reply' field.")
        if not overlay:
            overlay = reply[:90]
        return reply, overlay[:120]
    except Exception:
        reply = raw
        overlay = raw.replace("\n", " ").replace("\r", " ").strip()[:90]
        return reply, overlay


@dataclass
class ChatState:
    history: list[dict[str, str]] = field(default_factory=list)
    max_turns: int = 20

    def append(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})
        max_items = self.max_turns * 2
        if len(self.history) > max_items:
            self.history = self.history[-max_items:]


@dataclass
class FastPathDecision:
    handled: bool
    kind: str
    immediate_reply: str
    overlay: str
    run_slow: bool = False
    tool_result: dict[str, Any] | None = None
    ui_summary: str | None = None


@dataclass
class FastActionPolicy:
    mode: str = "direct"


@dataclass
class FastPathPolicy:
    state_query_keywords: list[str] = field(default_factory=lambda: ["状态", "state", "phase", "当前情况", "现在情况", "飞行状态", "风险", "risks"])
    action_policies: dict[str, FastActionPolicy] = field(default_factory=dict)
    max_abs_target_pitch_deg_fast: float = 10.0
    max_heading_delta_deg_fast: float = 45.0
    blocked_phases_for_fast_control: list[str] = field(default_factory=lambda: ["takeoff_roll", "landing_roll"])
    blocked_risks_for_fast_control: list[str] = field(
        default_factory=lambda: ["stall_risk", "overspeed_risk", "unstable_approach", "runway_excursion_risk"]
    )

    @classmethod
    def default(cls) -> "FastPathPolicy":
        return cls(
            action_policies={
                "set_throttle": FastActionPolicy(mode="direct"),
                "set_roll_cmd": FastActionPolicy(mode="direct"),
                "set_rudder_cmd": FastActionPolicy(mode="direct"),
                "set_speedbrake": FastActionPolicy(mode="direct"),
                "set_pitch_cmd": FastActionPolicy(mode="llm"),
                "set_target_pitch_deg": FastActionPolicy(mode="direct"),
                "turn_to_heading": FastActionPolicy(mode="direct"),
                "release_brakes": FastActionPolicy(mode="direct"),
                "set_flaps": FastActionPolicy(mode="llm"),
                "set_gear": FastActionPolicy(mode="llm"),
            }
        )


def _load_fast_path_policy(path: Path) -> FastPathPolicy:
    policy = FastPathPolicy.default()
    if not path.exists():
        return policy
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return policy
    if isinstance(raw, dict):
        keywords = raw.get("state_query_keywords")
        if isinstance(keywords, list) and keywords:
            policy.state_query_keywords = [str(item) for item in keywords if str(item).strip()]
        actions = raw.get("action_policies")
        if isinstance(actions, dict):
            parsed: dict[str, FastActionPolicy] = {}
            for name, value in actions.items():
                if not isinstance(name, str) or not isinstance(value, dict):
                    continue
                mode = str(value.get("mode", "direct")).strip().lower()
                if mode not in {"direct", "llm"}:
                    mode = "direct"
                parsed[name] = FastActionPolicy(mode=mode)
            if parsed:
                policy.action_policies = parsed
        if "max_abs_target_pitch_deg_fast" in raw:
            try:
                policy.max_abs_target_pitch_deg_fast = float(raw["max_abs_target_pitch_deg_fast"])
            except Exception:
                pass
        if "max_heading_delta_deg_fast" in raw:
            try:
                policy.max_heading_delta_deg_fast = float(raw["max_heading_delta_deg_fast"])
            except Exception:
                pass
        blocked_phases = raw.get("blocked_phases_for_fast_control")
        if isinstance(blocked_phases, list):
            policy.blocked_phases_for_fast_control = [str(item) for item in blocked_phases if str(item).strip()]
        blocked_risks = raw.get("blocked_risks_for_fast_control")
        if isinstance(blocked_risks, list):
            policy.blocked_risks_for_fast_control = [str(item) for item in blocked_risks if str(item).strip()]
    return policy


def load_fast_path_policy(base_dir: Path) -> FastPathPolicy:
    for path in (base_dir / "fast_path_policy.json", base_dir / "config" / "fast_path_policy.json"):
        if path.exists():
            return _load_fast_path_policy(path)
    return FastPathPolicy.default()


class AgentToolBridge:
    """Single backend authority for flight state reads and guarded control writes."""

    def __init__(
        self,
        *,
        monitor: FlightStateMonitor,
        situation_engine: SituationInferenceEngine,
        guard: ActionGuard,
        executor: ActionExecutor,
    ) -> None:
        self.monitor = monitor
        self.situation_engine = situation_engine
        self.guard = guard
        self.executor = executor
        self.background = BackgroundToolRunner()

    @staticmethod
    def tool_schemas() -> list[dict[str, Any]]:
        return [
            {"type": "function", "function": {"name": "get_flight_state", "description": "Read current aircraft state and inferred phase/risks from X-Plane monitor.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
            {"type": "function", "function": {"name": "set_throttle", "description": "Set throttle command in range [-1.0, 1.0]. Positive values increase thrust.", "parameters": {"type": "object", "properties": {"value": {"type": "number", "minimum": -1.0, "maximum": 1.0, "description": "Throttle ratio in [-1,1]."}}, "required": ["value"], "additionalProperties": False}}},
            {"type": "function", "function": {"name": "set_roll_cmd", "description": "Set roll command in range [-1.0, 1.0]. Positive values bank right, negative bank left.", "parameters": {"type": "object", "properties": {"value": {"type": "number", "minimum": -1.0, "maximum": 1.0, "description": "Roll command in [-1,1]."}}, "required": ["value"], "additionalProperties": False}}},
            {"type": "function", "function": {"name": "set_pitch_cmd", "description": "Set pitch command in range [-0.5, 0.5]. Positive values pitch up.", "parameters": {"type": "object", "properties": {"value": {"type": "number", "minimum": -0.5, "maximum": 0.5, "description": "Pitch command in [-0.5,0.5]."}}, "required": ["value"], "additionalProperties": False}}},
            {"type": "function", "function": {"name": "set_rudder_cmd", "description": "Set rudder command in range [-1.0, 1.0]. Positive values yaw right, negative yaw left.", "parameters": {"type": "object", "properties": {"value": {"type": "number", "minimum": -1.0, "maximum": 1.0, "description": "Rudder command in [-1,1]."}}, "required": ["value"], "additionalProperties": False}}},
            {"type": "function", "function": {"name": "set_speedbrake", "description": "Set speedbrake ratio in range [-0.5, 1.5].", "parameters": {"type": "object", "properties": {"value": {"type": "number", "minimum": -0.5, "maximum": 1.5, "description": "Speedbrake ratio in [-0.5,1.5]."}}, "required": ["value"], "additionalProperties": False}}},
            {"type": "function", "function": {"name": "set_target_pitch_deg", "description": "Closed-loop adjust to target pitch angle (degrees). Use this when user asks for angle target, not stick input.", "parameters": {"type": "object", "properties": {"value": {"type": "number", "minimum": -15.0, "maximum": 20.0, "description": "Target pitch angle in degrees."}, "timeout_s": {"type": "number", "minimum": 1.0, "maximum": 12.0, "description": "Control timeout in seconds."}}, "required": ["value"], "additionalProperties": False}}},
            {"type": "function", "function": {"name": "turn_to_heading", "description": "Closed-loop turn to target true heading (degrees). Use this when user asks turn to west/east or heading number.", "parameters": {"type": "object", "properties": {"heading_deg": {"type": "number", "minimum": 0.0, "maximum": 360.0, "description": "Target true heading in degrees."}, "timeout_s": {"type": "number", "minimum": 3.0, "maximum": 30.0, "description": "Turn timeout in seconds."}}, "required": ["heading_deg"], "additionalProperties": False}}},
            {"type": "function", "function": {"name": "set_flaps", "description": "Set flaps ratio in range [0.0, 1.0].", "parameters": {"type": "object", "properties": {"value": {"type": "number", "minimum": 0.0, "maximum": 1.0, "description": "Flaps ratio in [0,1]."}}, "required": ["value"], "additionalProperties": False}}},
            {"type": "function", "function": {"name": "set_gear", "description": "Set landing gear state.", "parameters": {"type": "object", "properties": {"down": {"type": "boolean", "description": "true to extend gear, false to retract gear."}}, "required": ["down"], "additionalProperties": False}}},
            {"type": "function", "function": {"name": "release_brakes", "description": "Release parking and wheel brakes.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
        ]

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "get_flight_state":
                return self._get_flight_state()
            if name == "set_throttle":
                return self._exec_write_action(ActionPlan(requested_by="llm_tool", mode=ControlMode.ASSISTED, actions=[Action(type=ActionType.SET_THROTTLE, value=float(arguments.get("value")), reason="llm_tool_call")]))
            if name == "set_roll_cmd":
                return self._exec_write_action(ActionPlan(requested_by="llm_tool", mode=ControlMode.ASSISTED, actions=[Action(type=ActionType.SET_ROLL_CMD, value=float(arguments.get("value")), reason="llm_tool_call")]))
            if name == "set_pitch_cmd":
                return self._exec_write_action(ActionPlan(requested_by="llm_tool", mode=ControlMode.ASSISTED, actions=[Action(type=ActionType.SET_PITCH_CMD, value=float(arguments.get("value")), reason="llm_tool_call")]))
            if name == "set_rudder_cmd":
                return self._exec_write_action(ActionPlan(requested_by="llm_tool", mode=ControlMode.ASSISTED, actions=[Action(type=ActionType.SET_RUDDER_CMD, value=float(arguments.get("value")), reason="llm_tool_call")]))
            if name == "set_speedbrake":
                return self._exec_write_action(ActionPlan(requested_by="llm_tool", mode=ControlMode.ASSISTED, actions=[Action(type=ActionType.SET_SPEEDBRAKE, value=float(arguments.get("value")), reason="llm_tool_call")]))
            if name == "set_target_pitch_deg":
                timeout_s = float(arguments.get("timeout_s", 4.0))
                return self._set_target_pitch_deg(float(arguments.get("value")), timeout_s=timeout_s)
            if name == "turn_to_heading":
                timeout_s = float(arguments.get("timeout_s", 8.0))
                return self._turn_to_heading(float(arguments.get("heading_deg")), timeout_s=timeout_s)
            if name == "set_flaps":
                return self._exec_write_action(ActionPlan(requested_by="llm_tool", mode=ControlMode.ASSISTED, actions=[Action(type=ActionType.SET_FLAPS, value=float(arguments.get("value")), reason="llm_tool_call")]))
            if name == "set_gear":
                down = bool(arguments.get("down"))
                return self._exec_write_action(ActionPlan(requested_by="llm_tool", mode=ControlMode.ASSISTED, actions=[Action(type=ActionType.SET_GEAR, value=1 if down else 0, reason="llm_tool_call")]))
            if name == "release_brakes":
                return self._exec_write_action(ActionPlan(requested_by="llm_tool", mode=ControlMode.ASSISTED, actions=[Action(type=ActionType.RELEASE_BRAKES, value=0, reason="llm_tool_call")]))
            return {"ok": False, "error": f"Unsupported tool: {name}"}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def execute_plan(self, plan: ActionPlan) -> dict[str, Any]:
        return self._exec_write_action(plan)

    def execute_async(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in {"set_target_pitch_deg", "turn_to_heading"}:
            return self.execute(name, arguments)

        args = dict(arguments)
        job_id = self.background.submit(name, args, lambda: self.execute(name, args))
        return {
            "ok": True,
            "accepted": True,
            "mode": "async",
            "job_id": job_id,
            "tool": name,
            "args": args,
            "message": "accepted_for_background_execution",
        }

    def _get_flight_state(self) -> dict[str, Any]:
        latest = self.monitor.get_latest()
        if latest is None:
            return {"ok": False, "error": self.monitor.get_last_error() or "no_samples", "state": None}
        situation = self.situation_engine.infer(latest, self.monitor.get_window(10.0), self.monitor.get_window(30.0))
        return {"ok": True, "error": self.monitor.get_last_error(), "state": {"latest": asdict(latest), "phase": situation.phase, "confidence": situation.confidence, "evidence": situation.evidence, "risks": situation.risks}}

    def _exec_write_action(self, plan: ActionPlan) -> dict[str, Any]:
        latest = self.monitor.get_latest()
        if latest is None:
            return {"ok": False, "error": "no_latest_snapshot_for_guard", "executed": []}
        guard_result = self.guard.check(plan, latest)
        exec_result = self.executor.execute(plan, guard_result)
        return {"ok": exec_result.success, "guard_allowed": guard_result.allowed, "guard_violations": guard_result.violations, "executed": exec_result.executed, "error": exec_result.error}

    @staticmethod
    def _clamp(value: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, value))

    @staticmethod
    def _heading_error(target_deg: float, current_deg: float) -> float:
        return (target_deg - current_deg + 540.0) % 360.0 - 180.0

    def _wait_next_snapshot(self, prev_ts: float | None, *, max_wait_s: float = 0.8):
        deadline = time.time() + max_wait_s
        while time.time() < deadline:
            latest = self.monitor.get_latest()
            if latest is not None:
                cur_ts = float(latest.timestamp_s)
                if prev_ts is None or cur_ts > prev_ts + 1e-4:
                    return latest
            time.sleep(0.05)
        return self.monitor.get_latest()

    def _set_target_pitch_deg(self, target_deg: float, *, timeout_s: float = 6.0) -> dict[str, Any]:
        target_deg = self._clamp(float(target_deg), -15.0, 20.0)
        timeout_s = self._clamp(float(timeout_s), 1.0, 12.0)
        start = time.time()
        steps = 0
        last_exec: dict[str, Any] | None = None
        achieved = False
        last_err: float | None = None
        cmd_sign = 1.0
        sign_flipped = False

        while (time.time() - start) < timeout_s:
            latest = self.monitor.get_latest()
            if latest is None:
                return {"ok": False, "error": "no_latest_snapshot_for_control_loop", "executed": [], "target_pitch_deg": target_deg}
            err = target_deg - float(latest.pitch_deg)
            if abs(err) <= 1.5:
                achieved = True
                break
            if last_err is not None and (abs(err) > abs(last_err) + 0.8) and not sign_flipped:
                cmd_sign *= -1.0
                sign_flipped = True
            pitch_cmd = self._clamp(err * 0.08 * cmd_sign, -0.45, 0.45)
            last_exec = self._exec_write_action(
                ActionPlan(
                    requested_by="llm_tool_closed_loop",
                    mode=ControlMode.ASSISTED,
                    actions=[Action(type=ActionType.SET_PITCH_CMD, value=pitch_cmd, reason="set_target_pitch_deg")],
                )
            )
            if not last_exec.get("ok"):
                return {
                    "ok": False,
                    "error": last_exec.get("error", "closed_loop_exec_failed"),
                    "guard_violations": last_exec.get("guard_violations", []),
                    "executed": last_exec.get("executed", []),
                    "target_pitch_deg": target_deg,
                }
            steps += 1
            last_err = err
            latest_after = self._wait_next_snapshot(float(latest.timestamp_s), max_wait_s=0.8)
            if latest_after is None:
                time.sleep(0.15)

        self._exec_write_action(
            ActionPlan(
                requested_by="llm_tool_closed_loop",
                mode=ControlMode.ASSISTED,
                actions=[Action(type=ActionType.SET_PITCH_CMD, value=0.0, reason="set_target_pitch_deg_hold")],
            )
        )
        final = self.monitor.get_latest()
        final_pitch = None if final is None else float(final.pitch_deg)
        return {
            "ok": achieved,
            "error": None if achieved else "target_pitch_not_reached_before_timeout",
            "target_pitch_deg": target_deg,
            "final_pitch_deg": final_pitch,
            "steps": steps,
            "control_sign_flipped": sign_flipped,
            "executed": [] if last_exec is None else last_exec.get("executed", []),
        }

    def _turn_to_heading(self, target_heading_deg: float, *, timeout_s: float = 16.0) -> dict[str, Any]:
        target = float(target_heading_deg) % 360.0
        timeout_s = self._clamp(float(timeout_s), 3.0, 30.0)
        start = time.time()
        steps = 0
        last_exec: dict[str, Any] | None = None
        achieved = False
        last_err: float | None = None
        cmd_sign = 1.0
        sign_flipped = False

        while (time.time() - start) < timeout_s:
            latest = self.monitor.get_latest()
            if latest is None:
                return {"ok": False, "error": "no_latest_snapshot_for_control_loop", "executed": [], "target_heading_deg": target}
            err = self._heading_error(target, float(latest.heading_true_deg))
            if abs(err) <= 4.0:
                achieved = True
                break
            if last_err is not None and (abs(err) > abs(last_err) + 6.0) and not sign_flipped:
                cmd_sign *= -1.0
                sign_flipped = True
            roll_cmd = self._clamp(err * 0.018 * cmd_sign, -0.55, 0.55)
            rudder_cmd = self._clamp(err * 0.01 * cmd_sign, -0.25, 0.25)
            last_exec = self._exec_write_action(
                ActionPlan(
                    requested_by="llm_tool_closed_loop",
                    mode=ControlMode.ASSISTED,
                    actions=[
                        Action(type=ActionType.SET_ROLL_CMD, value=roll_cmd, reason="turn_to_heading"),
                        Action(type=ActionType.SET_RUDDER_CMD, value=rudder_cmd, reason="turn_to_heading"),
                        Action(type=ActionType.SET_PITCH_CMD, value=0.0, reason="turn_to_heading_keep_pitch"),
                    ],
                )
            )
            if not last_exec.get("ok"):
                return {
                    "ok": False,
                    "error": last_exec.get("error", "closed_loop_exec_failed"),
                    "guard_violations": last_exec.get("guard_violations", []),
                    "executed": last_exec.get("executed", []),
                    "target_heading_deg": target,
                }
            steps += 1
            last_err = err
            latest_after = self._wait_next_snapshot(float(latest.timestamp_s), max_wait_s=0.9)
            if latest_after is None:
                time.sleep(0.20)

        self._exec_write_action(
            ActionPlan(
                requested_by="llm_tool_closed_loop",
                mode=ControlMode.ASSISTED,
                actions=[
                    Action(type=ActionType.SET_ROLL_CMD, value=0.0, reason="turn_to_heading_hold"),
                    Action(type=ActionType.SET_RUDDER_CMD, value=0.0, reason="turn_to_heading_hold"),
                    Action(type=ActionType.SET_PITCH_CMD, value=0.0, reason="turn_to_heading_hold"),
                ],
            )
        )
        final = self.monitor.get_latest()
        final_heading = None if final is None else float(final.heading_true_deg)
        final_err = None if final_heading is None else self._heading_error(target, final_heading)
        return {
            "ok": achieved,
            "error": None if achieved else "target_heading_not_reached_before_timeout",
            "target_heading_deg": target,
            "final_heading_deg": final_heading,
            "final_heading_error_deg": final_err,
            "steps": steps,
            "control_sign_flipped": sign_flipped,
            "executed": [] if last_exec is None else last_exec.get("executed", []),
        }


class FastCommandRouter:
    """Rule-based low-latency router for common state/control requests."""

    THROTTLE_KEYWORDS = ("throttle", "油门", "推力", "动力")
    ROLL_KEYWORDS = ("roll", "bank", "横滚", "转弯", "转向", "转右", "转左", "左右滚转")
    PITCH_KEYWORDS = ("pitch", "俯仰", "抬头", "低头")
    RUDDER_KEYWORDS = ("rudder", "方向舵", "脚舵", "偏航", "转向", "yaw")
    SPEEDBRAKE_KEYWORDS = ("speedbrake", "spoiler", "减速板", "扰流板", "减速", "刹车板")
    FLAPS_KEYWORDS = ("flaps", "襟翼")
    GEAR_KEYWORDS = ("gear", "起落架")
    BRAKE_KEYWORDS = ("brake", "刹车", "制动", "parking brake", "park brake", "松刹")
    HEADING_KEYWORDS = ("heading", "航向", "航向角", "turn to", "朝向", "对准")
    CARDINAL_TO_HEADING = {
        "north": 0.0,
        "south": 180.0,
        "east": 90.0,
        "west": 270.0,
        "北": 0.0,
        "南": 180.0,
        "东": 90.0,
        "西": 270.0,
    }

    def __init__(self, tools: AgentToolBridge, policy: FastPathPolicy | None = None) -> None:
        self.tools = tools
        self.policy = policy or FastPathPolicy.default()

    @staticmethod
    def _normalize(text: str) -> str:
        return (text or "").strip().lower()

    @staticmethod
    def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
        return any(keyword in text for keyword in keywords)

    @staticmethod
    def _extract_number_near_keywords(text: str, keywords: tuple[str, ...]) -> float | None:
        for keyword in keywords:
            idx = text.find(keyword)
            if idx < 0:
                continue
            match = re.search(r"(-?\d+(?:\.\d+)?)\s*%?", text[idx : idx + 48])
            if not match:
                continue
            value = float(match.group(1))
            if value > 1.0 and value <= 100.0:
                value /= 100.0
            return max(min(value, 1.0), -1.0)
        return None

    @staticmethod
    def _format_ratio(value: float) -> str:
        return f"{int(round(value * 100))}%"

    def _parse_throttle(self, text: str) -> float | None:
        if not self._contains_any(text, self.THROTTLE_KEYWORDS):
            return None
        if any(k in text for k in ("idle", "怠速", "收油", "减油", "reduce throttle", "cut throttle")):
            return 0.0
        if any(k in text for k in ("full", "最大", "全油门", "加满", "maximum", "max")):
            return 1.0
        return self._extract_number_near_keywords(text, self.THROTTLE_KEYWORDS)

    def _parse_roll(self, text: str) -> float | None:
        if not self._contains_any(text, self.ROLL_KEYWORDS):
            return None
        if any(k in text for k in ("左转", "向左", "左滚", "bank left", "roll left")):
            return -0.4
        if any(k in text for k in ("右转", "向右", "右滚", "bank right", "roll right")):
            return 0.4
        return self._extract_number_near_keywords(text, self.ROLL_KEYWORDS)

    def _parse_pitch(self, text: str) -> float | None:
        if not self._contains_any(text, self.PITCH_KEYWORDS):
            return None
        if any(k in text for k in ("抬头", "上仰", "pitch up")):
            return 0.25
        if any(k in text for k in ("低头", "下俯", "pitch down")):
            return -0.25
        value = self._extract_number_near_keywords(text, self.PITCH_KEYWORDS)
        return None if value is None else max(min(value, 0.5), -0.5)

    def _parse_rudder(self, text: str) -> float | None:
        if not self._contains_any(text, self.RUDDER_KEYWORDS):
            return None
        if any(k in text for k in ("左舵", "向左", "yaw left")):
            return -0.35
        if any(k in text for k in ("右舵", "向右", "yaw right")):
            return 0.35
        value = self._extract_number_near_keywords(text, self.RUDDER_KEYWORDS)
        return None if value is None else max(min(value, 1.0), -1.0)

    def _parse_speedbrake(self, text: str) -> float | None:
        if not self._contains_any(text, self.SPEEDBRAKE_KEYWORDS):
            return None
        if any(k in text for k in ("收回", "收起", "retract", "stow")):
            return 0.0
        if any(k in text for k in ("放出", "展开", "deploy", "extend")):
            return 1.0
        value = self._extract_number_near_keywords(text, self.SPEEDBRAKE_KEYWORDS)
        return None if value is None else max(min(value, 1.5), -0.5)

    def _parse_flaps(self, text: str) -> float | None:
        if not self._contains_any(text, self.FLAPS_KEYWORDS):
            return None
        if any(k in text for k in ("收襟翼", "襟翼收起", "flaps up", "retract flaps", "flaps 0", "0襟翼")):
            return 0.0
        if any(k in text for k in ("全襟翼", "最大襟翼", "full flaps")):
            return 1.0
        return self._extract_number_near_keywords(text, self.FLAPS_KEYWORDS)

    def _parse_gear(self, text: str) -> bool | None:
        if not self._contains_any(text, self.GEAR_KEYWORDS):
            return None
        if any(k in text for k in ("放下", "下放", "伸出", "down", "extend", "deploy")):
            return True
        if any(k in text for k in ("收起", "收上", "retract", "up")):
            return False
        return None

    def _parse_release_brakes(self, text: str) -> bool:
        return self._contains_any(text, self.BRAKE_KEYWORDS) and any(k in text for k in ("release", "松刹", "松开", "释放", "放开", "解除", "brake off"))

    def _parse_target_pitch_deg(self, text: str) -> float | None:
        pitch_intent = ("仰角" in text) or ("俯仰角" in text) or ("俯角" in text) or ("pitch angle" in text)
        has_pitch = self._contains_any(text, self.PITCH_KEYWORDS) or pitch_intent
        if not has_pitch:
            return None

        # Prefer explicit degree expressions first.
        m = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:deg|degree|degrees|°|度)", text)
        if m:
            value = float(m.group(1))
            if "俯角" in text and value >= 0:
                value = -abs(value)
            if "仰角" in text and value <= 0:
                value = abs(value)
            return max(-15.0, min(20.0, value))

        if pitch_intent:
            m2 = (
                re.search(r"仰角\s*([+-]?\d+(?:\.\d+)?)", text)
                or re.search(r"俯仰角\s*([+-]?\d+(?:\.\d+)?)", text)
                or re.search(r"俯角\s*([+-]?\d+(?:\.\d+)?)", text)
            )
            if m2:
                value = float(m2.group(1))
                if "俯角" in text and value >= 0:
                    value = -abs(value)
                if "仰角" in text and value <= 0:
                    value = abs(value)
                return max(-15.0, min(20.0, value))
        return None

    def _parse_target_heading_deg(self, text: str) -> float | None:
        if not text:
            return None

        for token, heading in self.CARDINAL_TO_HEADING.items():
            if token in text:
                return heading

        if any(k in text for k in self.HEADING_KEYWORDS):
            m = re.search(r"(?:heading|航向|航向角)\s*[:：=]?\s*(-?\d+(?:\.\d+)?)", text)
            if m:
                return float(m.group(1)) % 360.0
        return None

    def _parse_relative_heading_delta_deg(self, text: str) -> float | None:
        if not text:
            return None
        left = any(token in text for token in ("左", "left", "port"))
        right = any(token in text for token in ("右", "right", "starboard"))
        turn_intent = any(token in text for token in ("偏转", "转", "转向", "航向", "heading", "turn", "bank"))
        if not turn_intent or left == right:
            return None
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?:°|度|deg|degree|degrees)?", text)
        if not match:
            return None
        value = min(45.0, max(1.0, float(match.group(1))))
        return -value if left else value

    def _target_heading_from_relative_delta(self, delta_deg: float) -> float | None:
        state_result = self.tools.execute("get_flight_state", {})
        if not state_result.get("ok"):
            return None
        latest = ((state_result.get("state") or {}).get("latest") or {})
        current = latest.get("heading_true_deg")
        if not isinstance(current, (int, float)):
            return None
        return (float(current) + float(delta_deg)) % 360.0

    @staticmethod
    def _summarize_target_result(name: str, result: dict[str, Any]) -> str:
        if name == "set_target_pitch_deg":
            target = result.get("target_pitch_deg")
            final = result.get("final_pitch_deg")
            target_text = "-" if target is None else f"{float(target):.1f}"
            final_text = "-" if final is None else f"{float(final):.1f}"
            if result.get("ok"):
                return f"仰角目标已完成（目标 {target_text}°，当前 {final_text}°）"
            return f"仰角目标未在时限内收敛（目标 {target_text}°，当前 {final_text}°）"
        if name == "turn_to_heading":
            target = result.get("target_heading_deg")
            final = result.get("final_heading_deg")
            target_text = "-" if target is None else f"{float(target):.0f}"
            final_text = "-" if final is None else f"{float(final):.0f}"
            if result.get("ok"):
                return f"航向目标已完成（目标 {target_text}°，当前 {final_text}°）"
            return f"航向目标未在时限内收敛（目标 {target_text}°，当前 {final_text}°）"
        return f"{name} executed"

    def _format_state_summary(self, result: dict[str, Any]) -> tuple[str, str]:
        state = result.get("state") or {}
        latest = state.get("latest") or {}
        risks = state.get("risks") or []
        risk_text = "无明显风险" if not risks else "，".join(risks)
        reply = (
            f"阶段：{state.get('phase', 'unknown')} | 置信度：{state.get('confidence', 0.0):.2f} | "
            f"空速：{latest.get('airspeed_kts', '-')} kt | 高度：{latest.get('altitude_ft', '-')} ft | "
            f"升降率：{latest.get('vertical_speed_fpm', '-')} fpm | 风险：{risk_text}"
        )
        return reply, f"{state.get('phase', 'unknown')} | {risk_text}"[:120]

    def _action_mode(self, action_name: str) -> str:
        policy = self.policy.action_policies.get(action_name)
        return policy.mode if policy is not None else "llm"

    def _build_gate_context(self) -> dict[str, Any] | None:
        state_result = self.tools.execute("get_flight_state", {})
        if not state_result.get("ok"):
            return None
        state = state_result.get("state") or {}
        return {
            "phase": str(state.get("phase") or ""),
            "risks": [str(r) for r in (state.get("risks") or [])],
            "latest": state.get("latest") or {},
        }

    def _check_fast_gate_for_targets(
        self,
        *,
        target_pitch: float | None,
        target_heading: float | None,
    ) -> tuple[bool, str | None, dict[str, Any] | None]:
        ctx = self._build_gate_context()
        if ctx is None:
            return False, "state_unavailable_for_fast_gate", None

        phase = ctx["phase"]
        risks = ctx["risks"]
        if phase in set(self.policy.blocked_phases_for_fast_control):
            return False, f"phase_blocked:{phase}", ctx
        blocked_risks = set(self.policy.blocked_risks_for_fast_control)
        hit_risk = next((r for r in risks if r in blocked_risks), None)
        if hit_risk is not None:
            return False, f"risk_blocked:{hit_risk}", ctx

        latest = ctx.get("latest") or {}
        if target_pitch is not None:
            if abs(float(target_pitch)) > float(self.policy.max_abs_target_pitch_deg_fast):
                return False, "target_pitch_too_large_for_fast_path", ctx

        if target_heading is not None:
            cur_heading = latest.get("heading_true_deg")
            if isinstance(cur_heading, (int, float)):
                heading_delta = abs(self.tools._heading_error(float(target_heading), float(cur_heading)))
                if heading_delta > float(self.policy.max_heading_delta_deg_fast):
                    return False, "heading_delta_too_large_for_fast_path", ctx

        return True, None, ctx

    def route(self, prompt: str) -> FastPathDecision | None:
        text = self._normalize(prompt)
        if not text:
            return None

        target_ops: list[tuple[str, dict[str, Any]]] = []
        target_pitch = self._parse_target_pitch_deg(text)
        if target_pitch is not None and self._action_mode("set_target_pitch_deg") == "direct":
            target_ops.append(("set_target_pitch_deg", {"value": target_pitch}))
        target_heading = self._parse_target_heading_deg(text)
        relative_heading_delta = self._parse_relative_heading_delta_deg(text)
        if target_heading is None and relative_heading_delta is not None:
            target_heading = self._target_heading_from_relative_delta(relative_heading_delta)
        if target_heading is not None and self._action_mode("turn_to_heading") == "direct":
            heading_args: dict[str, Any] = {"heading_deg": target_heading}
            if relative_heading_delta is not None:
                heading_args["relative_delta_deg"] = relative_heading_delta
            target_ops.append(("turn_to_heading", heading_args))
        if target_ops:
            gate_ok, gate_reason, gate_ctx = self._check_fast_gate_for_targets(target_pitch=target_pitch, target_heading=target_heading)
            if not gate_ok:
                summary = f"已转慢路径：{gate_reason}"
                return FastPathDecision(
                    handled=True,
                    kind="fast_gate_defer",
                    immediate_reply=summary,
                    overlay=summary[:120],
                    run_slow=True,
                    tool_result={"gate_reason": gate_reason, "gate_context": gate_ctx},
                    ui_summary=summary,
                )
            results: list[dict[str, Any]] = []
            summaries: list[str] = []
            for tool_name, args in target_ops:
                result = self.tools.execute(tool_name, args)
                results.append({"tool": tool_name, "result": result})
                summaries.append(self._summarize_target_result(tool_name, result))
            all_ok = all(bool(item["result"].get("ok")) for item in results)
            reply = "；".join(summaries) + "。"
            overlay = "；".join(summaries)[:120]
            kind = "fast_target_control" if all_ok else "fast_target_control_partial"
            return FastPathDecision(True, kind, reply, overlay, False, {"results": results}, reply)

        if self._contains_any(text, tuple(self.policy.state_query_keywords)) and not any(v is not None for v in (self._parse_throttle(text), self._parse_flaps(text), self._parse_gear(text))):
            result = self.tools.execute("get_flight_state", {})
            if not result.get("ok"):
                error = result.get("error", "unknown_error")
                return FastPathDecision(True, "state_query_failed", f"当前状态读取失败：{error}", "state read failed", False, result, "状态读取失败")
            reply, overlay = self._format_state_summary(result)
            return FastPathDecision(True, "state_query", reply, overlay, False, result, reply)

        actions: list[Action] = []
        parsers = [
            ("set_throttle", ActionType.SET_THROTTLE, self._parse_throttle(text)),
            ("set_roll_cmd", ActionType.SET_ROLL_CMD, self._parse_roll(text)),
            ("set_pitch_cmd", ActionType.SET_PITCH_CMD, self._parse_pitch(text)),
            ("set_rudder_cmd", ActionType.SET_RUDDER_CMD, self._parse_rudder(text)),
            ("set_speedbrake", ActionType.SET_SPEEDBRAKE, self._parse_speedbrake(text)),
            ("set_flaps", ActionType.SET_FLAPS, self._parse_flaps(text)),
        ]
        for key, a_type, value in parsers:
            if value is not None and self._action_mode(key) == "direct":
                actions.append(Action(type=a_type, value=value, reason="fast_rule"))
        gear = self._parse_gear(text)
        if gear is not None and self._action_mode("set_gear") == "direct":
            actions.append(Action(type=ActionType.SET_GEAR, value=1 if gear else 0, reason="fast_rule"))
        if self._parse_release_brakes(text) and self._action_mode("release_brakes") == "direct":
            actions.append(Action(type=ActionType.RELEASE_BRAKES, value=0, reason="fast_rule"))
        if not actions:
            return None

        result = self.tools.execute_plan(ActionPlan(requested_by="fast_rule", mode=ControlMode.ASSISTED, actions=actions))
        if not result.get("ok"):
            violations = result.get("guard_violations") or []
            msg = "；".join(map(str, violations)) if violations else str(result.get("error") or "execution_failed")
            return FastPathDecision(True, "fast_blocked", f"已阻止执行：{msg}", "command blocked", False, result, f"已阻止执行：{msg}")

        action_texts: list[str] = []
        for action in actions:
            if action.type == ActionType.SET_THROTTLE:
                action_texts.append(f"油门 {self._format_ratio(float(action.value))}")
            elif action.type == ActionType.SET_ROLL_CMD:
                action_texts.append(f"横滚 {float(action.value):+.2f}")
            elif action.type == ActionType.SET_PITCH_CMD:
                action_texts.append(f"俯仰 {float(action.value):+.2f}")
            elif action.type == ActionType.SET_RUDDER_CMD:
                action_texts.append(f"方向舵 {float(action.value):+.2f}")
            elif action.type == ActionType.SET_SPEEDBRAKE:
                action_texts.append(f"速度刹车 {float(action.value):+.2f}")
            elif action.type == ActionType.SET_FLAPS:
                action_texts.append(f"襟翼 {self._format_ratio(float(action.value))}")
            elif action.type == ActionType.SET_GEAR:
                action_texts.append("起落架放下" if int(action.value or 0) == 1 else "起落架收起")
            elif action.type == ActionType.RELEASE_BRAKES:
                action_texts.append("刹车已释放")
        reply = "已执行：" + "，".join(action_texts) + "。"
        return FastPathDecision(True, "fast_control", reply, "；".join(action_texts)[:120], True, result, reply)
