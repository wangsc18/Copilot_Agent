import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from bridge_agent_chat import parse_wire_message, sanitize_wire_text, SessionState


class TestChatBridgeHelpers(unittest.TestCase):
    def test_sanitize_wire_text_strips_controls(self):
        raw = " hello\tpilot\r\nmsg\x01 "
        self.assertEqual(sanitize_wire_text(raw), "hello pilot  msg")

    def test_parse_wire_message(self):
        kind, content = parse_wire_message(b"PILOT|Rotate now?\n")
        self.assertEqual(kind, "PILOT")
        self.assertEqual(content, "Rotate now?")

    def test_session_state_keeps_tail(self):
        state = SessionState(max_history_pairs=2)
        for i in range(6):
            state.append_turn("user", f"u{i}")
        self.assertEqual(len(state.history), 4)
        self.assertEqual(state.history[0]["content"], "u2")


if __name__ == "__main__":
    unittest.main()
