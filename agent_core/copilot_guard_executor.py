from __future__ import annotations

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
            elif action.type == ActionType.SET_PITCH_CMD:
                if not isinstance(action.value, (int, float)) or float(action.value) < -0.5 or float(action.value) > 0.5:
                    violations.append("SET_PITCH_CMD value out of range [-0.5,0.5]")
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
    def __init__(self, xp_host: str = "127.0.0.1", xp_port: int = 49009, timeout_ms: int = 1000):
        self.xp_host = xp_host
        self.xp_port = xp_port
        self.timeout_ms = timeout_ms

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

    @staticmethod
    def _apply_action(client: xpc.XPlaneConnect, action: Action) -> None:
        # 与 XPC 的唯一动作映射入口，便于后续统一审计和扩展
        if action.type == ActionType.SET_THROTTLE:
            client.sendCTRL([-998, -998, -998, float(action.value), -998, -998, -998])
            return
        if action.type == ActionType.SET_PITCH_CMD:
            client.sendCTRL([float(action.value), -998, -998, -998, -998, -998, -998])
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
