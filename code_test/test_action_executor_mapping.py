import pathlib
import sys
import tempfile
import unittest
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from agent_core.copilot_core import Action, ActionPlan, ActionType, ControlMode, GuardResult
from agent_core.copilot_guard_executor import ActionExecutor


@dataclass
class _FakeClient:
    ctrl_calls: list
    dref_calls: list

    def sendCTRL(self, values):
        self.ctrl_calls.append(values)

    def sendDREFs(self, drefs, values):
        self.dref_calls.append((drefs, values))


class TestActionExecutorMapping(unittest.TestCase):
    def test_axis_signs_apply_to_ctrl_mapping(self):
        ex = ActionExecutor(roll_cmd_sign=-1.0, pitch_cmd_sign=-1.0, rudder_cmd_sign=-1.0)
        client = _FakeClient(ctrl_calls=[], dref_calls=[])

        ex._apply_action(client, Action(type=ActionType.SET_ROLL_CMD, value=0.2, reason="t"))
        ex._apply_action(client, Action(type=ActionType.SET_PITCH_CMD, value=0.1, reason="t"))
        ex._apply_action(client, Action(type=ActionType.SET_RUDDER_CMD, value=0.3, reason="t"))

        # XPC CTRL order: [pitch, roll, yaw, throttle, gear, flaps, speedbrake]
        self.assertEqual(client.ctrl_calls[0][1], -0.2)
        self.assertEqual(client.ctrl_calls[1][0], -0.1)
        self.assertEqual(client.ctrl_calls[2][2], -0.3)

    def test_load_axis_config(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "axis.json"
            path.write_text(
                '{"roll_cmd_sign": -1.0, "pitch_cmd_sign": -1.0, "rudder_cmd_sign": 1.0}',
                encoding="utf-8",
            )
            ex = ActionExecutor.from_axis_config(config_path=path)
            self.assertEqual(ex.roll_cmd_sign, -1.0)
            self.assertEqual(ex.pitch_cmd_sign, -1.0)
            self.assertEqual(ex.rudder_cmd_sign, 1.0)

    def test_execute_blocked_by_guard(self):
        ex = ActionExecutor()
        plan = ActionPlan(
            requested_by="test",
            mode=ControlMode.ASSISTED,
            actions=[Action(type=ActionType.SET_THROTTLE, value=0.2, reason="t")],
        )
        out = ex.execute(plan, GuardResult(allowed=False, violations=["blocked"]))
        self.assertFalse(out.success)
        self.assertIn("guard_blocked", out.error or "")


if __name__ == "__main__":
    unittest.main()
