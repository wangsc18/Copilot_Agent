import json
import os
import pathlib
import sys
import tempfile
import unittest
import threading
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from external_agent_chat_ui import (
    _load_dotenv,
    _load_fast_path_policy,
    AgentToolBridge,
    FastCommandRouter,
    parse_agent_payload,
)
from agent_core.copilot_core import ExecResult, FlightSnapshot, GuardResult


class TestExternalAgentChatUI(unittest.TestCase):
    def test_parse_agent_payload_json(self):
        raw = 'prefix {"reply":"详细解释","overlay":"短句总结"} suffix'
        reply, overlay = parse_agent_payload(raw)
        self.assertEqual(reply, "详细解释")
        self.assertEqual(overlay, "短句总结")

    def test_parse_agent_payload_overlay_fallback(self):
        raw = '{"reply":"abc","overlay":""}'
        reply, overlay = parse_agent_payload(raw)
        self.assertEqual(reply, "abc")
        self.assertEqual(overlay, "abc")

    def test_parse_agent_payload_plain_text_fallback(self):
        raw = "这是纯文本输出，不是JSON。"
        reply, overlay = parse_agent_payload(raw)
        self.assertEqual(reply, raw)
        self.assertEqual(overlay, raw)

    def test_load_dotenv_sets_env_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / ".env"
            p.write_text(
                "OPENAI_API_KEY=test_key\nOPENAI_BASE_URL=https://example.com/v1\n",
                encoding="utf-8",
            )
            old_key = os.environ.get("OPENAI_API_KEY")
            old_base = os.environ.get("OPENAI_BASE_URL")
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("OPENAI_BASE_URL", None)
            try:
                _load_dotenv(p)
                self.assertEqual(os.environ.get("OPENAI_API_KEY"), "test_key")
                self.assertEqual(os.environ.get("OPENAI_BASE_URL"), "https://example.com/v1")
            finally:
                if old_key is None:
                    os.environ.pop("OPENAI_API_KEY", None)
                else:
                    os.environ["OPENAI_API_KEY"] = old_key
                if old_base is None:
                    os.environ.pop("OPENAI_BASE_URL", None)
                else:
                    os.environ["OPENAI_BASE_URL"] = old_base


class StubSituationEngine:
    def infer(self, latest, win10, win30):
        return type(
            "Situation",
            (),
            {
                "phase": "ground_hold",
                "confidence": 0.9,
                "evidence": ["test"],
                "risks": [],
                "to_prompt_dict": lambda self: {
                    "phase": "ground_hold",
                    "confidence": 0.9,
                    "evidence": ["test"],
                    "risks": [],
                    "latest": {},
                    "trend_10s": None,
                    "trend_30s": None,
                },
            },
        )()


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
        return GuardResult(allowed=False, violations=["blocked"])


class StubExecutor:
    def execute(self, plan, allowed):
        if not allowed.allowed:
            return ExecResult(success=False, executed=[], error="blocked")
        return ExecResult(success=True, executed=[a.type.value for a in plan.actions], error=None)


def make_snapshot():
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


class TestFastCommandRouter(unittest.TestCase):
    def test_state_query_fast_path(self):
        tools = AgentToolBridge(
            monitor=StubMonitor(latest=make_snapshot()),
            situation_engine=StubSituationEngine(),
            guard=StubGuard(),
            executor=StubExecutor(),
        )
        router = FastCommandRouter(tools)
        decision = router.route("请告诉我当前飞行状态")
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertTrue(decision.handled)
        self.assertEqual(decision.kind, "state_query")

    def test_throttle_fast_path(self):
        tools = AgentToolBridge(
            monitor=StubMonitor(latest=make_snapshot()),
            situation_engine=StubSituationEngine(),
            guard=StubGuard(),
            executor=StubExecutor(),
        )
        router = FastCommandRouter(tools)
        decision = router.route("油门 70%")
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.kind, "fast_control")
        self.assertTrue(decision.run_slow)

    def test_target_pitch_fast_path(self):
        snap = make_snapshot()
        snap.pitch_deg = 8.0
        tools = AgentToolBridge(
            monitor=StubMonitor(latest=snap),
            situation_engine=StubSituationEngine(),
            guard=StubGuard(),
            executor=StubExecutor(),
        )
        router = FastCommandRouter(tools)
        decision = router.route("把仰角调整到8度")
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertIn(decision.kind, ("fast_target_control", "fast_target_control_partial"))

    def test_target_heading_deferred_by_gate(self):
        snap = make_snapshot()
        snap.heading_true_deg = 0.0
        tools = AgentToolBridge(
            monitor=StubMonitor(latest=snap),
            situation_engine=StubSituationEngine(),
            guard=StubGuard(),
            executor=StubExecutor(),
        )
        router = FastCommandRouter(tools)
        decision = router.route("向西偏转")
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.kind, "fast_gate_defer")
        self.assertTrue(decision.run_slow)

    def test_relative_left_turn_uses_spoken_delta(self):
        snap = make_snapshot()
        snap.heading_true_deg = 45.0
        tools = AgentToolBridge(
            monitor=StubMonitor(latest=snap),
            situation_engine=StubSituationEngine(),
            guard=StubGuard(),
            executor=StubExecutor(),
        )
        router = FastCommandRouter(tools)
        delta = router._parse_relative_heading_delta_deg("将我的飞机向左偏转 15 度")
        self.assertEqual(delta, -15.0)
        target = router._target_heading_from_relative_delta(delta)
        self.assertEqual(target, 30.0)

    def test_policy_file_controls_fast_actions(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "fast_path_policy.json"
            path.write_text(
                json.dumps(
                    {
                        "action_policies": {
                            "set_throttle": {"mode": "direct"},
                            "set_target_pitch_deg": {"mode": "direct"},
                            "turn_to_heading": {"mode": "direct"},
                            "set_flaps": {"mode": "llm"},
                            "set_gear": {"mode": "llm"},
                            "release_brakes": {"mode": "direct"},
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            policy = _load_fast_path_policy(path)
            tools = AgentToolBridge(
                monitor=StubMonitor(latest=make_snapshot()),
                situation_engine=StubSituationEngine(),
                guard=StubGuard(),
                executor=StubExecutor(),
            )
            router = FastCommandRouter(tools, policy=policy)
            self.assertIsNone(router.route("收襟翼"))
            self.assertIsNone(router.route("起落架收起"))
            self.assertIsNotNone(router.route("油门 50%"))


class TestBackgroundSummary(unittest.TestCase):
    def test_format_bg_job_summary(self):
        app_cls = __import__("external_agent_chat_ui").ExternalAgentChatApp
        job = type("Job", (), {})()
        job.tool_name = "set_target_pitch_deg"
        job.result = {"ok": True, "target_pitch_deg": 6.0, "final_pitch_deg": 6.1}
        text = app_cls._format_bg_job_summary(job)
        self.assertIn("后台执行完成", text)

    def test_drain_bg_summaries(self):
        app_cls = __import__("external_agent_chat_ui").ExternalAgentChatApp
        fake = type("Fake", (), {})()
        fake._bg_summary_lock = threading.Lock()
        fake._pending_bg_summaries = ["a", "b"]
        out = app_cls._drain_bg_summaries(fake)
        self.assertEqual(out, ["a", "b"])
        self.assertEqual(fake._pending_bg_summaries, [])


class TestVoiceGatewayHelpers(unittest.TestCase):
    def test_voice_classification_routes_flight_status_to_backend(self):
        app_cls = __import__("external_agent_chat_ui").ExternalAgentChatApp
        fake = type("Fake", (), {})()
        fake._voice_agent_call_mode = "on_demand"
        fake.client = None
        fake._voice_intent_llm_fallback = False
        fake._voice_intent_llm_threshold = 0.55
        fake._should_dispatch_voice_to_backend = lambda text: app_cls._should_dispatch_voice_to_backend(fake, text)
        self.assertEqual(app_cls._classify_voice_final_by_rule(fake, "现在飞机状态如何").intent, "flight_task")
        self.assertEqual(app_cls._classify_voice_final_by_rule(fake, "你是谁").intent, "smalltalk")
        self.assertEqual(app_cls._classify_voice_final_by_rule(fake, "嗯").intent, "unclear")

    def test_voice_playback_text_is_short(self):
        app_cls = __import__("external_agent_chat_ui").ExternalAgentChatApp
        long_text = "状态正常。" + "高度速度航向稳定" * 40
        playback = app_cls._build_voice_playback_text(long_text)
        self.assertLessEqual(len(playback), 220)
        self.assertTrue(playback)

    def test_voice_playback_text_summarizes_flight_state(self):
        app_cls = __import__("external_agent_chat_ui").ExternalAgentChatApp
        raw = "阶段：cruise | 置信度：0.99 | 空速：107.89724731445312 kt | 高度：856.0022583007812 ft | 升降率：0.045777201652526855 fpm | 风险：无明显风险"
        playback = app_cls._build_voice_playback_text(raw)
        self.assertEqual(playback, "当前处于巡航阶段，空速约108节，高度约856英尺，未见明显风险。")

    def test_voice_playback_rounds_long_decimals(self):
        app_cls = __import__("external_agent_chat_ui").ExternalAgentChatApp
        raw = "后台结果简报：后台执行结束：俯仰目标 15.0° 未在时限内收敛，当前约 9.429946899414062°。"
        playback = app_cls._build_voice_playback_text(raw)
        self.assertIn("15°", playback)
        self.assertIn("9.43°", playback)
        self.assertNotIn("9.429946899414062", playback)


if __name__ == "__main__":
    unittest.main()
