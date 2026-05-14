import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from voice_agent.local_stt import AudioCaptureConfig, LocalWhisperSTT


class TestLocalSTT(unittest.TestCase):
    def test_audio_capture_config_validate(self):
        with self.assertRaises(ValueError):
            AudioCaptureConfig(sample_rate_hz=0).validate()
        with self.assertRaises(ValueError):
            AudioCaptureConfig(channels=0).validate()
        with self.assertRaises(ValueError):
            AudioCaptureConfig(sample_width_bytes=3).validate()
        with self.assertRaises(ValueError):
            AudioCaptureConfig(blocksize=0).validate()

    def test_local_whisper_transcribe_missing_audio_file(self):
        stt = LocalWhisperSTT()
        with self.assertRaises(FileNotFoundError):
            stt.transcribe(Path("this_file_should_not_exist_abc123.wav"))

    def test_local_whisper_missing_dependency(self):
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "x.wav"
            wav.write_bytes(b"RIFF")
            stt = LocalWhisperSTT()
            with patch("voice_agent.local_stt.LocalWhisperSTT._ensure_model", side_effect=RuntimeError("faster-whisper is required for local STT")):
                with self.assertRaises(RuntimeError):
                    stt.transcribe(wav)


if __name__ == "__main__":
    unittest.main()
