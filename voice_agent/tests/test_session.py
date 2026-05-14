import unittest
from unittest.mock import patch

from voice_agent import VoiceConfig, VoiceEventType, VoiceOrchestrator
from voice_agent.session import (
    MockRealtimeSession,
    OpenAIRealtimeSession,
    VolcengineRealtimeSession,
    build_voice_session,
    select_and_start_voice_session,
)


class TestVoiceSession(unittest.TestCase):
    def test_build_voice_session_returns_volcengine_by_default(self):
        cfg = VoiceConfig(enabled=True)
        orch = VoiceOrchestrator(cfg)
        session = build_voice_session(cfg, orch)
        self.assertIsInstance(session, VolcengineRealtimeSession)

    def test_mock_session_lifecycle(self):
        cfg = VoiceConfig(enabled=True)
        orch = VoiceOrchestrator(cfg)
        logs: list[str] = []
        session = MockRealtimeSession(cfg, orch, log_fn=logs.append)
        session.start()
        session.stop()
        events = orch.drain_events()
        self.assertTrue(any(e.type == VoiceEventType.SILENCE_TIMEOUT for e in events))
        self.assertTrue(any("started" in msg for msg in logs))
        self.assertTrue(any("stopped" in msg for msg in logs))

    def test_select_and_start_falls_back_to_volcengine(self):
        cfg = VoiceConfig(enabled=True, providers=("openai", "volcengine"))
        orch = VoiceOrchestrator(cfg)
        logs: list[str] = []
        with patch("voice_agent.session._read_env") as read_env:
            def fake_read(key: str) -> str:
                if key == "OPENAI_API_KEY":
                    return ""
                if key == "VOLCENGINE_APP_ID":
                    return "app-id"
                if key == "VOLCENGINE_ACCESS_KEY":
                    return "access-key"
                return ""
            read_env.side_effect = fake_read
            session = select_and_start_voice_session(cfg, orch, log_fn=logs.append)
            self.assertIsInstance(session, VolcengineRealtimeSession)
            session._stop_event.set()
        self.assertTrue(any("openai failed" in x for x in logs))
        self.assertTrue(any("active_provider=volcengine" in x for x in logs))

    def test_build_voice_session_prefers_openai_class(self):
        cfg = VoiceConfig(enabled=True, providers=("openai", "mock"))
        orch = VoiceOrchestrator(cfg)
        session = build_voice_session(cfg, orch)
        self.assertIsInstance(session, OpenAIRealtimeSession)


if __name__ == "__main__":
    unittest.main()
