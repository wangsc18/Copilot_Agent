import unittest
import struct
import json

from voice_agent import VoiceConfig, VoiceEventType, VoiceOrchestrator
from voice_agent.session import VolcengineBinaryProtocol, VolcengineRealtimeSession


class TestVolcengineSessionSmoke(unittest.TestCase):
    def test_decode_supports_extended_header_words(self):
        p = VolcengineBinaryProtocol()
        option = {"event": VolcengineBinaryProtocol.EVENT_SESSION_STARTED, "session_id": "sid-1", "sequence": 1}
        option_raw = json.dumps(option, separators=(",", ":")).encode("utf-8")
        payload_raw = b""
        header = bytes(
            [
                (VolcengineBinaryProtocol.VERSION << 4) | 2,
                (VolcengineBinaryProtocol.MSG_SERVER_FULL << 4) | 0,
                (VolcengineBinaryProtocol.SERIALIZATION_JSON << 4) | VolcengineBinaryProtocol.COMPRESSION_NONE,
                0,
                0,
                0,
                0,
                0,
            ]
        )
        frame = b"".join([header, struct.pack(">I", len(option_raw)), option_raw, struct.pack(">I", len(payload_raw)), payload_raw])
        packet = p.decode(frame)
        self.assertEqual(packet["event"], VolcengineBinaryProtocol.EVENT_SESSION_STARTED)
        self.assertEqual(packet["session_id"], "sid-1")

    def test_start_session_uses_configured_sample_rate(self):
        cfg = VoiceConfig(enabled=True, providers=("volcengine", "mock"), sample_rate_hz=24000)
        orch = VoiceOrchestrator(cfg)
        session = VolcengineRealtimeSession(cfg, orch)
        sent: list[bytes] = []
        session._send_binary = sent.append  # type: ignore[method-assign]
        session._send_start_session()
        packet = session._protocol.decode(sent[0])
        asr_info = packet["payload_msg"]["asr"]["audio_info"]
        self.assertEqual(asr_info["sample_rate"], 24000)

    def test_handle_session_and_transcript_events(self):
        cfg = VoiceConfig(enabled=True, providers=("volcengine", "mock"))
        orch = VoiceOrchestrator(cfg)
        logs: list[str] = []
        session = VolcengineRealtimeSession(cfg, orch, log_fn=logs.append)
        p = VolcengineBinaryProtocol()

        session._handle_packet(
            {
                "message_type": VolcengineBinaryProtocol.MSG_SERVER_FULL,
                "event": VolcengineBinaryProtocol.EVENT_SESSION_STARTED,
                "payload_msg": {},
            }
        )
        self.assertTrue(session._session_started.is_set())
        self.assertTrue(any("Session Started" in x for x in logs))

        session._handle_packet(
            {
                "message_type": VolcengineBinaryProtocol.MSG_SERVER_FULL,
                "event": VolcengineBinaryProtocol.EVENT_ASR_RESPONSE,
                "payload_msg": {"results": [{"text": "hello", "is_interim": True}]},
            }
        )
        session._handle_packet(
            {
                "message_type": VolcengineBinaryProtocol.MSG_SERVER_FULL,
                "event": VolcengineBinaryProtocol.EVENT_CHAT_ENDED,
                "payload_msg": {"content": "done"},
            }
        )

        events = orch.drain_events()
        self.assertTrue(any(e.type == VoiceEventType.ASR_PARTIAL and e.payload.get("text") == "hello" for e in events))
        self.assertTrue(any(e.type == VoiceEventType.ASR_FINAL and e.payload.get("text") == "done" for e in events))


if __name__ == "__main__":
    unittest.main()
