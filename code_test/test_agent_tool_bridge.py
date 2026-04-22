import pathlib
import sys
import unittest
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from agent_core.copilot_core import Action, ActionPlan, ActionType, ControlMode, ExecResult, FlightSnapshot, GuardResult
from external_agent_chat_ui import AgentToolBridge


@dataclass
class FakeSituation:
    phase: str = "ground_hold"
    confidence: float = 0.9
    evidence: list[str] = None
    risks: list[str] = None

    def __post_init__(self):
        if self.evidence is None:
            self.evidence = ["test"]
        if self.risks is None:
            self.risks = []


class StubSituationEngine:
    def infer(self, latest, win10, win30):
        return FakeSituation()


class StubMonitor:
    def __init__(self, latest=None, error=None):
        self._latest = latest
        self._error = error

    def get_latest(self):
        return self._latest

    def get_window(self, seconds):
        return [self._latest] if self._latest else []

    def get_last_error(self):
        return self._error


class StubGuard:
    def __init__(self, allow=True):
        self.allow = allow

    def check(self, plan, latest):
        if self.allow:
            return GuardResult(allowed=True, violations=[])
        return GuardResult(allowed=False, violations=["blocked_by_test_guard"])


class StubExecutor:
    def execute(self, plan, allowed):
        if not allowed.allowed:
            return ExecResult(success=False, executed=[], error="guard_blocked")
        return ExecResult(success=True, executed=[a.type.value for a in plan.actions], error=None)


def make_snapshot() -> FlightSnapshot:
    return FlightSnapshot(
        timestamp_s=0.0,
        altitude_ft=1000.0,
        altitude_msl_m=300.0,
        airspeed_kts=90.0,
        ground_speed_kts=50.0,
        vertical_speed_fpm=0.0,
        pitch_deg=0.0,
        roll_deg=0.0,
        heading_true_deg=0.0,
        gear_ratio=1.0,
        throttle_cmd=0.2,
        throttle_used_ratio=0.2,
        flaps_ratio=0.0,
        speedbrake_ratio=0.0,
        park_brake_ratio=1.0,
        left_brake_ratio=1.0,
        right_brake_ratio=1.0,
    )


class TestAgentToolBridge(unittest.TestCase):
    def test_get_flight_state_success(self):
        bridge = AgentToolBridge(
            monitor=StubMonitor(latest=make_snapshot()),
            situation_engine=StubSituationEngine(),
            guard=StubGuard(allow=True),
            executor=StubExecutor(),
        )
        out = bridge.execute("get_flight_state", {})
        self.assertTrue(out["ok"])
        self.assertIn("state", out)
        self.assertEqual(out["state"]["phase"], "ground_hold")

    def test_write_action_blocked_by_guard(self):
        bridge = AgentToolBridge(
            monitor=StubMonitor(latest=make_snapshot()),
            situation_engine=StubSituationEngine(),
            guard=StubGuard(allow=False),
            executor=StubExecutor(),
        )
        out = bridge.execute("set_throttle", {"value": 0.7})
        self.assertFalse(out["ok"])
        self.assertFalse(out["guard_allowed"])
        self.assertIn("blocked_by_test_guard", out["guard_violations"])

    def test_write_action_success(self):
        bridge = AgentToolBridge(
            monitor=StubMonitor(latest=make_snapshot()),
            situation_engine=StubSituationEngine(),
            guard=StubGuard(allow=True),
            executor=StubExecutor(),
        )
        out = bridge.execute("set_gear", {"down": True})
        self.assertTrue(out["ok"])
        self.assertEqual(out["executed"], ["set_gear"])

    def test_new_control_tools_are_supported(self):
        bridge = AgentToolBridge(
            monitor=StubMonitor(latest=make_snapshot()),
            situation_engine=StubSituationEngine(),
            guard=StubGuard(allow=True),
            executor=StubExecutor(),
        )
        self.assertTrue(bridge.execute("set_roll_cmd", {"value": 0.2})["ok"])
        self.assertTrue(bridge.execute("set_pitch_cmd", {"value": 0.1})["ok"])
        self.assertTrue(bridge.execute("set_rudder_cmd", {"value": -0.3})["ok"])
        self.assertTrue(bridge.execute("set_speedbrake", {"value": 0.5})["ok"])

    def test_closed_loop_target_pitch_returns_structured_result(self):
        snap = make_snapshot()
        snap.pitch_deg = 8.0
        bridge = AgentToolBridge(
            monitor=StubMonitor(latest=snap),
            situation_engine=StubSituationEngine(),
            guard=StubGuard(allow=True),
            executor=StubExecutor(),
        )
        out = bridge.execute("set_target_pitch_deg", {"value": 8.0})
        self.assertTrue(out["ok"])
        self.assertIn("target_pitch_deg", out)
        self.assertIn("final_pitch_deg", out)
        self.assertIn("steps", out)
        self.assertIn("executed", out)

    def test_closed_loop_turn_heading_returns_structured_result(self):
        snap = make_snapshot()
        snap.heading_true_deg = 270.0
        bridge = AgentToolBridge(
            monitor=StubMonitor(latest=snap),
            situation_engine=StubSituationEngine(),
            guard=StubGuard(allow=True),
            executor=StubExecutor(),
        )
        out = bridge.execute("turn_to_heading", {"heading_deg": 270.0})
        self.assertTrue(out["ok"])
        self.assertIn("target_heading_deg", out)
        self.assertIn("final_heading_deg", out)
        self.assertIn("final_heading_error_deg", out)
        self.assertIn("steps", out)
        self.assertIn("executed", out)

    def test_closed_loop_tools_can_run_async(self):
        snap = make_snapshot()
        snap.pitch_deg = 8.0
        bridge = AgentToolBridge(
            monitor=StubMonitor(latest=snap),
            situation_engine=StubSituationEngine(),
            guard=StubGuard(allow=True),
            executor=StubExecutor(),
        )
        out = bridge.execute_async("set_target_pitch_deg", {"value": 8.0})
        self.assertTrue(out["ok"])
        self.assertTrue(out["accepted"])
        self.assertEqual(out["mode"], "async")
        self.assertIn("job_id", out)


if __name__ == "__main__":
    unittest.main()
