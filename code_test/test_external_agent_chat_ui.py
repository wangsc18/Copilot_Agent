import os
import pathlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from external_agent_chat_ui import _load_dotenv, parse_agent_payload


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


if __name__ == "__main__":
    unittest.main()
