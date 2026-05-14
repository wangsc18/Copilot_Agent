import unittest

from voice_agent import VoiceConfig, VoiceEventType, VoiceOrchestrator


class TestVoiceOrchestrator(unittest.TestCase):
    def test_basic_turn_flow(self):
        orch = VoiceOrchestrator(VoiceConfig(enabled=True))
        orch.transition(VoiceEventType.USER_SPEAKING)
        self.assertEqual(orch.snapshot()["state"], "user_turn")
        orch.transition(VoiceEventType.TURN_END)
        self.assertEqual(orch.snapshot()["state"], "agent_thinking")
        orch.transition(VoiceEventType.AGENT_SPEAKING)
        self.assertEqual(orch.snapshot()["state"], "agent_speaking")
        orch.transition(VoiceEventType.SILENCE_TIMEOUT)
        self.assertEqual(orch.snapshot()["state"], "idle")

    def test_barge_in_interrupts_agent(self):
        orch = VoiceOrchestrator(VoiceConfig(enabled=True))
        orch.transition(VoiceEventType.AGENT_SPEAKING)
        orch.transition(VoiceEventType.USER_SPEAKING)
        self.assertEqual(orch.snapshot()["state"], "interrupted")

    def test_asr_and_speaker_tracking(self):
        orch = VoiceOrchestrator(VoiceConfig(enabled=True))
        orch.transition(VoiceEventType.ASR_PARTIAL, {"text": "hello"})
        orch.transition(VoiceEventType.ASR_FINAL, {"text": "hello world"})
        orch.transition(VoiceEventType.SPEAKER_SWITCH, {"speaker_id": "spk-1"})
        snap = orch.snapshot()
        self.assertEqual(snap["last_partial_text"], "hello")
        self.assertEqual(snap["last_final_text"], "hello world")
        self.assertEqual(snap["active_speaker_id"], "spk-1")


if __name__ == "__main__":
    unittest.main()

