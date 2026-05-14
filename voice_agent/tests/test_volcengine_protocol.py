import unittest

from voice_agent.session import VolcengineBinaryProtocol


class TestVolcengineProtocol(unittest.TestCase):
    def test_encode_decode_start_session(self):
        p = VolcengineBinaryProtocol()
        frame = p.build_start_session({"dialog": {"model": "o2.0"}})
        decoded = p.decode(frame)
        self.assertEqual(decoded["message_type"], VolcengineBinaryProtocol.MSG_CLIENT_FULL)
        self.assertEqual(decoded["event"], VolcengineBinaryProtocol.EVENT_START_SESSION)
        self.assertEqual(decoded["sequence"], 0)
        self.assertEqual(decoded["payload_msg"]["dialog"]["model"], "o2.0")

    def test_encode_decode_audio_only(self):
        p = VolcengineBinaryProtocol()
        raw = b"\x01\x02\x03\x04"
        frame = p.build_streaming_audio(raw, session_id="sid-1")
        decoded = p.decode(frame)
        self.assertEqual(decoded["message_type"], VolcengineBinaryProtocol.MSG_CLIENT_AUDIO_ONLY)
        self.assertEqual(decoded["event"], VolcengineBinaryProtocol.EVENT_STREAMING_AUDIO_ONLY)
        self.assertEqual(decoded["session_id"], "sid-1")
        self.assertEqual(decoded["payload_audio"], raw)

    def test_error_payload_decode(self):
        p = VolcengineBinaryProtocol()
        frame = p._encode(
            VolcengineBinaryProtocol.MSG_ERROR,
            VolcengineBinaryProtocol.EVENT_DIALOG_COMMON_ERROR,
            "sid-err",
            {"status_code": 42000020, "message": "StartSession event payload asr extra is null"},
            True,
        )
        decoded = p.decode(frame)
        self.assertEqual(decoded["message_type"], VolcengineBinaryProtocol.MSG_ERROR)
        self.assertEqual(decoded["event"], 0)
        self.assertEqual(decoded["payload_msg"]["status_code"], 42000020)

    def test_decode_truncated_frame_raises_value_error(self):
        p = VolcengineBinaryProtocol()
        decoded = p.decode(b"\x11\xc4\x10\x00\x00\x00")
        self.assertEqual(decoded["event"], 0)
        self.assertEqual(decoded["payload_size"], 0)

    def test_build_chat_tts_text(self):
        p = VolcengineBinaryProtocol()
        frame = p.build_chat_tts_text("后台结果", session_id="sid-tts", start=True, end=False)
        decoded = p.decode(frame)
        self.assertEqual(decoded["message_type"], VolcengineBinaryProtocol.MSG_CLIENT_FULL)
        self.assertEqual(decoded["event"], VolcengineBinaryProtocol.EVENT_CHAT_TTS_TEXT)
        self.assertEqual(decoded["session_id"], "sid-tts")
        self.assertEqual(decoded["payload_msg"]["content"], "后台结果")
        self.assertTrue(decoded["payload_msg"]["start"])
        self.assertFalse(decoded["payload_msg"]["end"])

    def test_build_end_asr(self):
        p = VolcengineBinaryProtocol()
        frame = p.build_end_asr(session_id="sid-asr")
        decoded = p.decode(frame)
        self.assertEqual(decoded["event"], VolcengineBinaryProtocol.EVENT_END_ASR)
        self.assertEqual(decoded["session_id"], "sid-asr")


if __name__ == "__main__":
    unittest.main()
