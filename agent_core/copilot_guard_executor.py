from __future__ import annotations

import json
from pathlib import Path

import xpc

from .copilot_core import Action, ActionPlan, ActionType, ExecResult, FlightSnapshot, GuardResult


class ActionGuard:
    def check(self, plan: ActionPlan, latest: FlightSnapshot) -> GuardResult:
        # 守卫层：所有写操作先做参数范围和场景约束校验
        violations: list[str] = []
        for action in plan.actions:
            if action.type == ActionType.SET_THROTTLE:
                if not isinstance(action.value, (int, float)) or float(action.value) < -1.0 or float(action.value) > 1.0:
                    violations.append("SET_THROTTLE value out of range [-1,1]")
            elif action.type == ActionType.SET_ROLL_CMD:
                if not isinstance(action.value, (int, float)) or float(action.value) < -1.0 or float(action.value) > 1.0:
                    violations.append("SET_ROLL_CMD value out of range [-1,1]")
            elif action.type == ActionType.SET_PITCH_CMD:
                if not isinstance(action.value, (int, float)) or float(action.value) < -0.5 or float(action.value) > 0.5:
                    violations.append("SET_PITCH_CMD value out of range [-0.5,0.5]")
            elif action.type == ActionType.SET_RUDDER_CMD:
                if not isinstance(action.value, (int, float)) or float(action.value) < -1.0 or float(action.value) > 1.0:
                    violations.append("SET_RUDDER_CMD value out of range [-1,1]")
            elif action.type == ActionType.SET_SPEEDBRAKE:
                if not isinstance(action.value, (int, float)) or float(action.value) < -0.5 or float(action.value) > 1.5:
                    violations.append("SET_SPEEDBRAKE value out of range [-0.5,1.5]")
            elif action.type == ActionType.SET_GEAR:
                if action.value not in (0, 1):
                    violations.append("SET_GEAR value must be 0 or 1")
                if latest.ground_speed_kts > 160 and action.value == 0:
                    violations.append("cannot retract gear at high ground speed")
            elif action.type == ActionType.SET_FLAPS:
                if not isinstance(action.value, (int, float)) or float(action.value) < 0.0 or float(action.value) > 1.0:
                    violations.append("SET_FLAPS value out of range [0,1]")
            elif action.type == ActionType.RELEASE_BRAKES:
                if latest.ground_speed_kts > 80:
                    violations.append("brake release command blocked at high speed")
            else:
                violations.append(f"unsupported action type: {action.type}")
        return GuardResult(allowed=len(violations) == 0, violations=violations)


class ActionExecutor:
    def __init__(
        self,
        xp_host: str = "127.0.0.1",
        xp_port: int = 49009,
        timeout_ms: int = 1000,
        *,
        roll_cmd_sign: float = 1.0,
        pitch_cmd_sign: float = 1.0,
        rudder_cmd_sign: float = 1.0,
    ):
        self.xp_host = xp_host
        self.xp_port = xp_port
        self.timeout_ms = timeout_ms
        self.roll_cmd_sign = float(roll_cmd_sign)
        self.pitch_cmd_sign = float(pitch_cmd_sign)
        self.rudder_cmd_sign = float(rudder_cmd_sign)

    @classmethod
    def from_axis_config(
        cls,
        *,
        xp_host: str = "127.0.0.1",
        xp_port: int = 49009,
        timeout_ms: int = 1000,
        config_path: Path | None = None,
    ) -> "ActionExecutor":
        roll = 1.0
        pitch = -1.0
        rudder = 1.0
        if config_path is not None and config_path.exists():
            try:
                raw = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    roll = float(raw.get("roll_cmd_sign", roll))
                    pitch = float(raw.get("pitch_cmd_sign", pitch))
                    rudder = float(raw.get("rudder_cmd_sign", rudder))
            except Exception:
                pass
        return cls(
            xp_host=xp_host,
            xp_port=xp_port,
            timeout_ms=timeout_ms,
            roll_cmd_sign=roll,
            pitch_cmd_sign=pitch,
            rudder_cmd_sign=rudder,
        )

    def execute(self, plan: ActionPlan, allowed: GuardResult) -> ExecResult:
        # 未通过守卫时直接拒绝执行，避免不安全动作下发到 X-Plane
        if not allowed.allowed:
            return ExecResult(success=False, executed=[], error="guard_blocked: " + "; ".join(allowed.violations))

        executed: list[str] = []
        try:
            with xpc.XPlaneConnect(xpHost=self.xp_host, xpPort=self.xp_port, timeout=self.timeout_ms) as client:
                for action in plan.actions:
                    self._apply_action(client, action)
                    executed.append(action.type.value)
            return ExecResult(success=True, executed=executed)
        except Exception as exc:
            return ExecResult(success=False, executed=executed, error=f"{type(exc).__name__}: {exc}")

    def _apply_action(self, client: xpc.XPlaneConnect, action: Action) -> None:
        # 与 XPC 的唯一动作映射入口，便于后续统一审计和扩展
        if action.type == ActionType.SET_THROTTLE:
            client.sendCTRL([-998, -998, -998, float(action.value), -998, -998, -998])
            return
        if action.type == ActionType.SET_ROLL_CMD:
            # XPC CTRL packet order is [pitch, roll, yaw, throttle, gear, flaps, speedbrake]
            client.sendCTRL([-998, float(action.value) * self.roll_cmd_sign, -998, -998, -998, -998, -998])
            return
        if action.type == ActionType.SET_PITCH_CMD:
            client.sendCTRL([float(action.value) * self.pitch_cmd_sign, -998, -998, -998, -998, -998, -998])
            return
        if action.type == ActionType.SET_RUDDER_CMD:
            client.sendCTRL([-998, -998, float(action.value) * self.rudder_cmd_sign, -998, -998, -998, -998])
            return
        if action.type == ActionType.SET_SPEEDBRAKE:
            client.sendCTRL([-998, -998, -998, -998, -998, -998, float(action.value)])
            return
        if action.type == ActionType.SET_GEAR:
            client.sendCTRL([-998, -998, -998, -998, int(action.value), -998, -998])
            return
        if action.type == ActionType.SET_FLAPS:
            client.sendCTRL([-998, -998, -998, -998, -998, float(action.value), -998])
            return
        if action.type == ActionType.RELEASE_BRAKES:
            client.sendDREFs(
                [
                    "sim/flightmodel/controls/parkbrakel",
                    "sim/cockpit2/controls/left_brake_ratio",
                    "sim/cockpit2/controls/right_brake_ratio",
                ],
                [0.0, 0.0, 0.0],
            )
            return
        raise ValueError(f"Unsupported action type: {action.type}")
