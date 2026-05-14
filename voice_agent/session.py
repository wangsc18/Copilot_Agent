from __future__ import annotations

"""Realtime voice provider sessions.

The Volcengine/Doubao session is intentionally treated as a voice gateway:
- ASR final text is forwarded to the UI/backend dispatcher.
- Backend-authoritative replies are spoken through ChatTTSText.
- Gateway-generated speech is allowed only when the UI marks the turn as
  smalltalk or clarification.
"""

import base64
import json
import queue
import struct
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from urllib.parse import urlparse

from .config import VoiceConfig
from .events import VoiceEventType
from .orchestrator import VoiceOrchestrator


VoiceLogFn = Callable[[str], None]

DEFAULT_DOUBAO_BOT_NAME = "副驾语音网关"
DEFAULT_DOUBAO_SYSTEM_ROLE = (
    "你是 X-Plane Co-Pilot 的前台实时语音网关，只负责听写、简短确认、澄清和等待后台结果。"
    "涉及飞行状态读取、风险分析、工具调用、控制指令或执行结果时，后台 X-Plane agent 是唯一权威来源。"
    "在后台结果返回前，只能说“收到，正在交给后台处理”或提出必要澄清；不得编造高度、速度、航向、风险或执行结果。"
    "普通寒暄可以简短回答，但必须说明自己是语音网关，不是飞行状态事实源。"
)
DEFAULT_DOUBAO_SPEAKING_STYLE = "语气简洁、专业、克制。优先使用短句确认，不扩展推断。"


# Override legacy mojibake defaults above with readable runtime prompts.
DEFAULT_DOUBAO_BOT_NAME = "副驾语音网关"
DEFAULT_DOUBAO_SYSTEM_ROLE = (
    "你是 X-Plane Co-Pilot 的前台实时语音网关，只负责听写、简短确认、澄清和等待后台结果。"
    "涉及飞行状态读取、风险分析、工具调用、控制指令或执行结果时，后台 X-Plane agent 是唯一权威来源。"
    "在后台结果返回前，只能说“收到，正在交给后台处理”或提出必要澄清；不得编造高度、速度、航向、风险或执行结果。"
    "普通寒暄可以简短回答，但必须说明自己是语音网关，不是飞行状态事实源。"
)
DEFAULT_DOUBAO_SPEAKING_STYLE = "语气简洁、专业、克制。优先使用短句确认，不扩展推断。"


class VoiceSession(ABC):
    def __init__(self, config: VoiceConfig, orchestrator: VoiceOrchestrator, *, log_fn: VoiceLogFn | None = None) -> None:
        self.config = config
        self.orchestrator = orchestrator
        self._log_fn = log_fn or (lambda _: None)

    @abstractmethod
    def start(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def stop(self) -> None:
        raise NotImplementedError

    def speak_text(self, text: str) -> bool:
        return False

    def allow_gateway_tts(self, allowed: bool, *, ttl_s: float = 8.0) -> None:
        return None

    def _log(self, message: str) -> None:
        self._log_fn(message)


class MockRealtimeSession(VoiceSession):
    def __init__(self, config: VoiceConfig, orchestrator: VoiceOrchestrator, *, log_fn: VoiceLogFn | None = None) -> None:
        super().__init__(config, orchestrator, log_fn=log_fn)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="voice_mock_session")
        self._thread.start()
        self._log("[voice_session] mock realtime session started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._thread = None
        self._log("[voice_session] mock realtime session stopped")

    def _run(self) -> None:
        self.orchestrator.transition(VoiceEventType.SILENCE_TIMEOUT, {"source": "session_start"})
        while not self._stop_event.is_set():
            time.sleep(0.2)


class OpenAIRealtimeSession(MockRealtimeSession):
    def __init__(self, config: VoiceConfig, orchestrator: VoiceOrchestrator, *, log_fn: VoiceLogFn | None = None) -> None:
        super().__init__(config, orchestrator, log_fn=log_fn)
        self._audio_in_stop = threading.Event()
        self._ws_thread: threading.Thread | None = None
        self._audio_thread: threading.Thread | None = None
        self._play_thread: threading.Thread | None = None
        self._ws = None
        self._play_queue: queue.Queue[bytes] = queue.Queue(maxsize=256)
        self._last_audio_append_at = 0.0
        self._commit_thread: threading.Thread | None = None
        self._append_bytes_total = 0
        self._append_frames_total = 0
        self._last_audio_stats_log_at = 0.0

    def start(self) -> None:
        api_key = _read_env("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is missing for OpenAI realtime provider")
        _validate_ws_url(self.config.openai_realtime_url, "OPENAI_REALTIME_URL")
        ws_url = self._build_openai_ws_url(self.config.openai_realtime_url, self.config.openai_realtime_model)
        try:
            import websocket  # type: ignore
            import sounddevice as sd  # type: ignore
        except Exception as exc:
            raise RuntimeError("openai realtime dependencies missing") from exc
        self._audio_in_stop.clear()
        headers = ["Authorization: Bearer " + api_key, "OpenAI-Beta: realtime=v1"]

        def on_open(ws):
            self._ws = ws
            self._log("[voice_session] openai websocket connected")
            self.orchestrator.transition(VoiceEventType.SILENCE_TIMEOUT, {"source": "session_start"})
            self._start_audio_workers(sd)
            self._start_commit_worker()

        def on_message(ws, message: str):
            try:
                evt = json.loads(message)
            except Exception:
                return
            self._handle_openai_event(evt)

        self._ws = websocket.WebSocketApp(
            ws_url,
            header=headers,
            on_open=on_open,
            on_message=on_message,
            on_error=lambda _ws, e: self._log(f"[voice_session] openai ws error: {e}"),
            on_close=lambda _ws, c, m: self._log(f"[voice_session] openai ws closed code={c} msg={m}"),
        )
        self._ws_thread = threading.Thread(target=self._ws.run_forever, kwargs={"ping_interval": 20, "ping_timeout": 10}, daemon=True)
        self._ws_thread.start()

    def stop(self) -> None:
        self._audio_in_stop.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        for t in (self._audio_thread, self._play_thread, self._ws_thread, self._commit_thread):
            if t is not None:
                t.join(timeout=1.0)

    @staticmethod
    def _build_openai_ws_url(base_url: str, model: str) -> str:
        if "?" in base_url:
            return base_url
        return f"{base_url}?model={model}"

    def _start_audio_workers(self, sd) -> None:
        if self._audio_thread is None:
            self._audio_thread = threading.Thread(target=self._capture_audio_loop, args=(sd,), daemon=True)
            self._audio_thread.start()
        if self._play_thread is None:
            self._play_thread = threading.Thread(target=self._play_audio_loop, args=(sd,), daemon=True)
            self._play_thread.start()

    def _capture_audio_loop(self, sd) -> None:
        blocksize = max(160, int(self.config.sample_rate_hz * self.config.frame_ms / 1000))
        try:
            with sd.RawInputStream(samplerate=self.config.sample_rate_hz, channels=1, dtype="int16", blocksize=blocksize):
                while not self._audio_in_stop.is_set():
                    time.sleep(0.2)
        except Exception:
            return

    def _play_audio_loop(self, sd) -> None:
        try:
            with sd.RawOutputStream(samplerate=self.config.sample_rate_hz, channels=1, dtype="int16", blocksize=0):
                while not self._audio_in_stop.is_set():
                    try:
                        chunk = self._play_queue.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if chunk:
                        pass
        except Exception:
            return

    def _handle_openai_event(self, evt: dict) -> None:
        et = str(evt.get("type") or "")
        if et in {"response.audio_transcript.delta", "response.output_audio_transcript.delta"}:
            text = str(evt.get("delta") or "")
            if text:
                self.orchestrator.transition(VoiceEventType.ASR_PARTIAL, {"text": text, "source": "openai_output", "role": "agent"})

    def _send_json(self, payload: dict) -> None:
        ws = self._ws
        if ws is None:
            return
        ws.send(json.dumps(payload, ensure_ascii=False))

    def _start_commit_worker(self) -> None:
        if self._commit_thread is not None and self._commit_thread.is_alive():
            return
        self._commit_thread = threading.Thread(target=self._commit_loop, daemon=True)
        self._commit_thread.start()

    def _commit_loop(self) -> None:
        while not self._audio_in_stop.is_set():
            time.sleep(0.8)


class VolcengineBinaryProtocol:
    VERSION = 1
    HEADER_WORDS = 1
    SERIALIZATION_NONE = 0
    SERIALIZATION_JSON = 1
    COMPRESSION_NONE = 0

    MSG_CLIENT_FULL = 1
    MSG_CLIENT_AUDIO_ONLY = 2
    MSG_CLIENT_FINISH = 3
    MSG_SERVER_FULL = 9
    MSG_SERVER_AUDIO_ONLY = 11
    MSG_ERROR = 15

    EVENT_START_CONNECTION = 1
    EVENT_FINISH_CONNECTION = 2
    EVENT_START_SESSION = 100
    EVENT_FINISH_SESSION = 102
    EVENT_END_ASR = 400
    EVENT_CHAT_TTS_TEXT = 500
    EVENT_CHAT_TEXT_QUERY = 501
    EVENT_STREAMING_AUDIO_ONLY = 200
    EVENT_SESSION_STARTED = 150
    EVENT_ASR_RESPONSE = 451
    EVENT_ASR_ENDED = 459
    EVENT_CHAT_RESPONSE = 550
    EVENT_CHAT_ENDED = 559
    EVENT_TTS_RESPONSE = 352
    EVENT_TTS_SENTENCE_START = 350
    EVENT_TTS_SENTENCE_END = 351
    EVENT_TTS_ENDED = 359
    EVENT_ASR_INFO = 450
    EVENT_USAGE_RESPONSE = 154
    EVENT_DIALOG_COMMON_ERROR = 599
    EVENT_CONNECTION_STARTED = 50
    EVENT_CONNECTION_FAILED = 51
    EVENT_CONNECTION_FINISHED = 52

    def __init__(self) -> None:
        self.sequence = 0
        self.session_id = ""

    def build_start_session(self, payload: dict) -> bytes:
        return self._encode(self.MSG_CLIENT_FULL, self.EVENT_START_SESSION, self.session_id, payload, True)

    def build_start_connection(self) -> bytes:
        return self._encode(self.MSG_CLIENT_FULL, self.EVENT_START_CONNECTION, "", {}, True)

    def build_streaming_audio(self, audio_bytes: bytes, *, session_id: str) -> bytes:
        return self._encode(self.MSG_CLIENT_AUDIO_ONLY, self.EVENT_STREAMING_AUDIO_ONLY, session_id, audio_bytes, False)

    def build_finish_session(self, *, session_id: str) -> bytes:
        return self._encode(self.MSG_CLIENT_FINISH, self.EVENT_FINISH_SESSION, session_id, {}, True)

    def build_finish_connection(self) -> bytes:
        return self._encode(self.MSG_CLIENT_FULL, self.EVENT_FINISH_CONNECTION, "", {}, True)

    def build_end_asr(self, *, session_id: str) -> bytes:
        return self._encode(self.MSG_CLIENT_FULL, self.EVENT_END_ASR, session_id, {}, True)

    def build_chat_tts_text(self, text: str, *, session_id: str, start: bool, end: bool) -> bytes:
        payload = {
            "start": bool(start),
            "content": text,
            "end": bool(end),
        }
        return self._encode(self.MSG_CLIENT_FULL, self.EVENT_CHAT_TTS_TEXT, session_id, payload, True)

    def build_chat_text_query(self, text: str, *, session_id: str) -> bytes:
        payload = {"content": text}
        return self._encode(self.MSG_CLIENT_FULL, self.EVENT_CHAT_TEXT_QUERY, session_id, payload, True)

    def _encode(self, msg_type: int, event: int, session_id: str, payload: dict | bytes, payload_is_json: bool) -> bytes:
        self.sequence += 1
        if payload_is_json:
            payload_raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            serialization = self.SERIALIZATION_JSON
        else:
            payload_raw = payload
            serialization = self.SERIALIZATION_NONE
        header = bytes(
            [
                (self.VERSION << 4) | self.HEADER_WORDS,
                (msg_type << 4) | 4,
                (serialization << 4) | self.COMPRESSION_NONE,
                0,
            ]
        )
        parts = [header, struct.pack(">I", int(event))]
        sid = session_id
        if event in {
            self.EVENT_START_SESSION,
            self.EVENT_FINISH_SESSION,
            self.EVENT_STREAMING_AUDIO_ONLY,
            self.EVENT_END_ASR,
            self.EVENT_CHAT_TTS_TEXT,
            self.EVENT_CHAT_TEXT_QUERY,
        }:
            if not sid:
                sid = self.session_id or str(uuid.uuid4())
            if sid:
                self.session_id = sid
            sid_raw = sid.encode("utf-8")
            parts.extend([struct.pack(">I", len(sid_raw)), sid_raw])
        parts.extend([struct.pack(">I", len(payload_raw)), payload_raw])
        return b"".join(parts)

    def decode(self, frame: bytes) -> dict:
        if len(frame) < 4:
            raise ValueError("frame too short")
        b0, b1, b2 = frame[0], frame[1], frame[2]
        header_words = b0 & 0xF
        if (b0 >> 4) != self.VERSION:
            raise ValueError("unsupported frame header")
        if header_words <= 0:
            raise ValueError("invalid header words")
        msg_type = (b1 >> 4) & 0xF
        msg_flags = b1 & 0xF
        serialization = (b2 >> 4) & 0xF
        off = header_words * 4
        if len(frame) < off:
            raise ValueError("frame truncated before header ends")

        if msg_type == self.MSG_ERROR:
            if len(frame) < off + 4:
                raise ValueError("error frame missing code")
            code = struct.unpack(">I", frame[off : off + 4])[0]
            off += 4
            if len(frame) < off + 4:
                return {
                    "message_type": msg_type,
                    "event": 0,
                    "session_id": "",
                    "sequence": 0,
                    "payload_msg": {"status_code": code},
                    "payload_audio": None,
                    "payload_size": 0,
                }
            payload_len = struct.unpack(">I", frame[off : off + 4])[0]
            off += 4
            if len(frame) < off + payload_len:
                raise ValueError("error frame payload truncated")
            payload_raw = frame[off : off + payload_len]
            payload_msg: dict = {}
            if serialization == self.SERIALIZATION_JSON and payload_raw:
                try:
                    payload_msg = json.loads(payload_raw.decode("utf-8"))
                except Exception:
                    payload_msg = {"raw": payload_raw.decode("utf-8", errors="replace")}
            if "status_code" not in payload_msg:
                payload_msg["status_code"] = code
            return {
                "message_type": msg_type,
                "event": 0,
                "session_id": "",
                "sequence": 0,
                "payload_msg": payload_msg,
                "payload_audio": (payload_raw if serialization == self.SERIALIZATION_NONE else None),
                "payload_size": payload_len,
                "error_code": code,
                "message_flags": msg_flags,
                "serialization": serialization,
            }

        if len(frame) < off + 4:
            return {
                "message_type": msg_type,
                "event": 0,
                "session_id": "",
                "sequence": 0,
                "payload_msg": {},
                "payload_audio": None,
                "payload_size": 0,
            }

        # Compatible decoding for both:
        # 1) option-json envelope: [option_len][option_json][payload_len][payload]
        # 2) compact envelope used by realtime dialogue server:
        #    [event][session_id_len][session_id][payload_len][payload]
        first_u32 = struct.unpack(">I", frame[off : off + 4])[0]
        (
            event,
            session_id,
            sequence,
            payload_len,
            payload_raw,
        ) = self._decode_with_best_effort(frame, off, first_u32)

        payload_msg = json.loads(payload_raw.decode("utf-8")) if serialization == self.SERIALIZATION_JSON and payload_raw else {}
        payload_audio = payload_raw if serialization == self.SERIALIZATION_NONE else None
        return {
            "message_type": msg_type,
            "event": event,
            "session_id": session_id,
            "sequence": sequence,
            "payload_msg": payload_msg,
            "payload_audio": payload_audio,
            "payload_size": payload_len,
            "message_flags": msg_flags,
            "serialization": serialization,
        }

    def _decode_with_best_effort(self, frame: bytes, off: int, first_u32: int) -> tuple[int, str, int, int, bytes]:
        remaining = len(frame) - (off + 4)

        # Try option-json envelope first.
        if first_u32 <= max(0, remaining):
            try:
                option_len = first_u32
                off2 = off + 4
                option_raw = frame[off2 : off2 + option_len]
                off2 += option_len
                option = json.loads(option_raw.decode("utf-8")) if option_raw else {}
                event = int(option.get("event") or 0)
                session_id = str(option.get("session_id") or "")
                sequence = int(option.get("sequence") or 0)
                if session_id:
                    self.session_id = session_id
                if len(frame) < off2 + 4:
                    return event, session_id, sequence, 0, b""
                payload_len = struct.unpack(">I", frame[off2 : off2 + 4])[0]
                off2 += 4
                if len(frame) < off2 + payload_len:
                    raise ValueError("frame payload truncated")
                payload_raw = frame[off2 : off2 + payload_len]
                return event, session_id, sequence, payload_len, payload_raw
            except Exception:
                pass

        # Fallback to compact envelope.
        # Layout: [event][session_id_len][session_id][payload_len][payload]
        event = first_u32
        off2 = off + 4
        if len(frame) < off2 + 4:
            return event, "", 0, 0, b""
        sid_len = struct.unpack(">I", frame[off2 : off2 + 4])[0]
        off2 += 4
        session_id = ""
        if sid_len > 0 and len(frame) >= off2 + sid_len:
            sid_raw = frame[off2 : off2 + sid_len]
            off2 += sid_len
            try:
                session_id = sid_raw.decode("utf-8")
            except Exception:
                session_id = ""
        if session_id:
            self.session_id = session_id
        if len(frame) < off2 + 4:
            return event, session_id, 0, 0, b""
        payload_len = struct.unpack(">I", frame[off2 : off2 + 4])[0]
        off2 += 4
        if len(frame) < off2 + payload_len:
            raise ValueError("frame payload truncated")
        payload_raw = frame[off2 : off2 + payload_len]
        return event, session_id, 0, payload_len, payload_raw


class VolcengineRealtimeSession(VoiceSession):
    def __init__(self, config: VoiceConfig, orchestrator: VoiceOrchestrator, *, log_fn: VoiceLogFn | None = None) -> None:
        super().__init__(config, orchestrator, log_fn=log_fn)
        self._protocol = VolcengineBinaryProtocol()
        self._stop_event = threading.Event()
        self._connection_started = threading.Event()
        self._session_started = threading.Event()
        self._ws_thread: threading.Thread | None = None
        self._audio_thread: threading.Thread | None = None
        self._play_thread: threading.Thread | None = None
        self._ws = None
        self._play_queue: queue.Queue[bytes] = queue.Queue(maxsize=256)
        self._last_audio_stats_log_at = 0.0
        self._append_bytes_total = 0
        self._append_frames_total = 0
        self._headers: dict[str, str] = {}
        self._model = _read_env("VOLCENGINE_MODEL") or "o2.0"
        self._log_mic_stats = (_read_env("VOICE_LOG_MIC_STATS") or "").lower() in {"1", "true", "yes", "on"}
        self._tts_playback_allowed_until = 0.0
        self._gateway_tts_allowed_until = 0.0

    def start(self) -> None:
        app_id = _read_env("VOLCENGINE_APP_ID")
        access_key = _read_env("VOLCENGINE_ACCESS_KEY")
        if not app_id:
            raise RuntimeError("VOLCENGINE_APP_ID is missing for Volcengine realtime provider")
        if not access_key:
            raise RuntimeError("VOLCENGINE_ACCESS_KEY is missing for Volcengine realtime provider")
        _validate_ws_url(self.config.volcengine_realtime_url, "VOLCENGINE_REALTIME_URL")
        self._headers = {
            "X-Api-App-ID": app_id,
            "X-Api-Access-Key": access_key,
            "X-Api-Resource-Id": _read_env("VOLCENGINE_RESOURCE_ID") or "volc.speech.dialog",
            "X-Api-App-Key": _read_env("VOLCENGINE_APP_KEY") or "PlgvMymc7f3tQnJ6",
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }
        self._stop_event.clear()
        self._connection_started.clear()
        self._session_started.clear()
        self._ws_thread = threading.Thread(target=self._run_ws_loop, daemon=True, name="voice_volc_ws")
        self._ws_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        try:
            self._send_binary(self._protocol.build_finish_session(session_id=self._protocol.session_id))
            self._send_binary(self._protocol.build_finish_connection())
        except Exception:
            pass
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass

    def speak_text(self, text: str) -> bool:
        clean = (text or "").strip()
        if not clean or self._ws is None or not self._protocol.session_id:
            return False
        self._send_binary(
            self._protocol.build_chat_tts_text(
                clean,
                session_id=self._protocol.session_id,
                start=True,
                end=False,
            )
        )
        self._send_binary(
            self._protocol.build_chat_tts_text(
                "",
                session_id=self._protocol.session_id,
                start=False,
                end=True,
            )
        )
        self._tts_playback_allowed_until = time.time() + max(8.0, min(45.0, len(clean) / 6.0))
        self.orchestrator.transition(VoiceEventType.AGENT_SPEAKING, {"source": "volc_chat_tts_text"})
        self._log("[voice_session] ChatTTSText queued")
        return True

    def allow_gateway_tts(self, allowed: bool, *, ttl_s: float = 8.0) -> None:
        self._gateway_tts_allowed_until = time.time() + max(0.5, ttl_s) if allowed else 0.0
        self._log(f"[voice_session] gateway_tts_allowed={allowed}")

    def _run_ws_loop(self) -> None:
        import websocket  # type: ignore
        import sounddevice as sd  # type: ignore

        backoff = 1.0
        while not self._stop_event.is_set():
            self._connection_started.clear()
            self._session_started.clear()

            def on_open(ws):
                self._ws = ws
                self._log("[voice_session] Volcengine Connected")
                self._send_binary(self._protocol.build_start_connection())
                self._start_audio_workers(sd)
                self.orchestrator.transition(VoiceEventType.SILENCE_TIMEOUT, {"source": "session_start"})

            def on_message(ws, message):
                if not isinstance(message, (bytes, bytearray)):
                    return
                dump_all = (_read_env("VOLCENGINE_DEBUG_RAW_FRAME") or "").lower() in {"1", "true", "yes", "on"}
                if dump_all:
                    self._log(
                        "[voice_session] raw frame "
                        + f"len={len(message)} hex={bytes(message).hex()}"
                    )
                try:
                    packet = self._protocol.decode(bytes(message))
                except Exception as exc:
                    self._log(f"[voice_session] volc decode skip: {type(exc).__name__}: {exc}")
                    return
                event = int(packet.get("event") or 0)
                if event == VolcengineBinaryProtocol.EVENT_CONNECTION_STARTED and not self._connection_started.is_set():
                    self._connection_started.set()
                    self._log("[voice_session] Connection Started")
                    self._send_start_session()
                self._handle_packet(packet)

            app = websocket.WebSocketApp(
                self.config.volcengine_realtime_url,
                header=[f"{k}: {v}" for k, v in self._headers.items()],
                on_open=on_open,
                on_message=on_message,
                on_error=lambda _ws, e: self._log(f"[voice_session] volc ws error: {e}"),
                on_close=lambda _ws, c, m: self._log(f"[voice_session] volc ws closed code={c} msg={m}"),
            )
            self._ws = app
            app.run_forever(ping_interval=20, ping_timeout=10)
            self._ws = None
            if self._stop_event.is_set():
                break
            self._log("[voice_session] Reconnecting")
            time.sleep(backoff)
            backoff = min(backoff * 1.8, 5.0)

    def _start_audio_workers(self, sd) -> None:
        if self._audio_thread is None:
            self._audio_thread = threading.Thread(target=self._capture_audio_loop, args=(sd,), daemon=True, name="voice_volc_capture")
            self._audio_thread.start()
        if self._play_thread is None:
            self._play_thread = threading.Thread(target=self._play_audio_loop, args=(sd,), daemon=True, name="voice_volc_play")
            self._play_thread.start()

    def _capture_audio_loop(self, sd) -> None:
        blocksize = max(160, int(self.config.sample_rate_hz * self.config.frame_ms / 1000))
        self._log("[voice_session] opening microphone input stream...")
        input_device = self._resolve_input_device(sd)
        try:
            with sd.RawInputStream(
                samplerate=self.config.sample_rate_hz,
                channels=1,
                dtype="int16",
                blocksize=blocksize,
                device=input_device,
            ) as stream:
                self._log(
                    "[voice_session] microphone stream started "
                    + f"sample_rate={self.config.sample_rate_hz} blocksize={blocksize} requested_device={self.config.input_device_index} actual_device={input_device}"
                )
                while not self._stop_event.is_set():
                    data, _overflow = stream.read(blocksize)
                    if not data:
                        continue
                    raw = bytes(data)
                    self._append_frames_total += 1
                    self._append_bytes_total += len(raw)
                    now = time.time()
                    if self._log_mic_stats and now - self._last_audio_stats_log_at >= 1.5:
                        self._log(
                            "[voice_session] mic stats "
                            + f"frames={self._append_frames_total} bytes={self._append_bytes_total} peak={_pcm16_peak_abs(raw)}"
                        )
                        self._last_audio_stats_log_at = now
                    self.orchestrator.transition(VoiceEventType.USER_SPEAKING, {"source": "mic"})
                    if not self._session_started.wait(timeout=0.05):
                        continue
                    self._send_binary(self._protocol.build_streaming_audio(raw, session_id=self._protocol.session_id))
        except Exception as exc:
            self._log(f"[voice_session] audio capture stopped: {type(exc).__name__}: {exc}")

    def _resolve_input_device(self, sd):
        requested = self.config.input_device_index
        try:
            devices = sd.query_devices()
        except Exception as exc:
            self._log(f"[voice_session] query_devices failed: {type(exc).__name__}: {exc}")
            return requested if requested >= 0 else None
        default_input = None
        try:
            default_pair = sd.default.device
            if isinstance(default_pair, (list, tuple)) and len(default_pair) >= 1:
                default_input = int(default_pair[0])
        except Exception:
            default_input = None
        input_candidates: list[tuple[int, str, int]] = []
        for idx, info in enumerate(devices):
            max_in = int(info.get("max_input_channels") or 0)
            if max_in > 0:
                input_candidates.append((idx, str(info.get("name") or ""), max_in))
        preview = ", ".join([f"{idx}:{name}(in={max_in})" for idx, name, max_in in input_candidates[:8]])
        self._log(f"[voice_session] input devices: {preview if preview else 'none'}")
        self._log(f"[voice_session] default input device index={default_input}")
        if requested >= 0:
            self._log(f"[voice_session] using configured input device index={requested}")
            return requested
        if default_input is not None:
            for idx, _name, _max_in in input_candidates:
                if idx == default_input:
                    self._log(f"[voice_session] auto-selected default input device index={idx}")
                    return idx
        if input_candidates:
            preferred = input_candidates[0][0]
            for idx, name, _max_in in input_candidates:
                lower = name.lower()
                if "microphone" in lower or "mic" in lower:
                    preferred = idx
                    break
            self._log(f"[voice_session] auto-selected fallback input device index={preferred}")
            return preferred
        self._log("[voice_session] no input device with max_input_channels>0 found")
        return None

    def _play_audio_loop(self, sd) -> None:
        with sd.RawOutputStream(samplerate=24000, channels=1, dtype="int16", blocksize=0) as stream:
            while not self._stop_event.is_set():
                try:
                    chunk = self._play_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                if chunk:
                    stream.write(chunk)

    def _send_start_session(self) -> None:
        input_mod = (_read_env("DOUBAO_INPUT_MOD") or "keep_alive").strip() or "keep_alive"
        end_smooth_ms = _to_int_or_default(_read_env("DOUBAO_END_SMOOTH_MS"), 1500)
        enable_custom_vad = (_read_env("DOUBAO_ENABLE_CUSTOM_VAD") or "").lower() in {"1", "true", "yes", "on"}
        bot_name = (_read_env("DOUBAO_BOT_NAME") or "副驾语音网关").strip()
        system_role = (
            _read_env("DOUBAO_SYSTEM_ROLE")
            or "你是前台实时语音交互代理。你的职责仅限语音接入、简短确认和澄清。"
            "凡涉及新事实、飞行状态、控制指令、执行结果，必须等待后台分析Agent确认后再回复。"
            "在未确认前只允许回复笼统过渡语，不得编造参数、状态或执行结果。"
        ).strip()
        speaking_style = (
            _read_env("DOUBAO_SPEAKING_STYLE")
            or "语气简洁、专业、克制。优先使用短句确认，不扩展推断。"
        ).strip()
        payload = {
            "dialog": {
                "model": self._model,
                "bot_name": (_read_env("DOUBAO_BOT_NAME") or DEFAULT_DOUBAO_BOT_NAME).strip(),
                "system_role": (_read_env("DOUBAO_SYSTEM_ROLE") or DEFAULT_DOUBAO_SYSTEM_ROLE).strip(),
                "speaking_style": (_read_env("DOUBAO_SPEAKING_STYLE") or DEFAULT_DOUBAO_SPEAKING_STYLE).strip(),
                "extra": {
                    "input_mod": input_mod,
                    "end_smooth_window_ms": end_smooth_ms,
                    "enable_custom_vad": enable_custom_vad,
                },
            },
            "asr": {"audio_info": {"format": "pcm", "sample_rate": self.config.sample_rate_hz, "channel": 1}, "extra": {}},
            "tts": {
                "speaker": _read_env("VOLCENGINE_TTS_SPEAKER") or "zh_female_vv_jupiter_bigtts",
                "audio_config": {"channel": 1, "format": "pcm_s16le", "sample_rate": 24000},
                "extra": {},
            },
        }
        self._send_binary(self._protocol.build_start_session(payload))

    def _send_binary(self, data: bytes) -> None:
        ws = self._ws
        if ws is None:
            return
        import websocket  # type: ignore

        ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)

    def _handle_packet(self, packet: dict) -> None:
        msg_type = int(packet.get("message_type") or 0)
        event = int(packet.get("event") or 0)
        if msg_type == VolcengineBinaryProtocol.MSG_SERVER_AUDIO_ONLY:
            if not self._should_play_tts_audio():
                return
            audio = packet.get("payload_audio")
            if isinstance(audio, (bytes, bytearray)) and audio:
                try:
                    self._play_queue.put_nowait(bytes(audio))
                except queue.Full:
                    pass
                self.orchestrator.transition(VoiceEventType.AGENT_SPEAKING, {"source": "volc_audio"})
                self._log("[voice_session] Streaming")
            return
        payload = packet.get("payload_msg") if isinstance(packet.get("payload_msg"), dict) else {}
        payload_audio = packet.get("payload_audio")
        if event == VolcengineBinaryProtocol.EVENT_SESSION_STARTED:
            self._session_started.set()
            self._log("[voice_session] Session Started")
            return
        if event == VolcengineBinaryProtocol.EVENT_CONNECTION_STARTED:
            self._log("[voice_session] Connection Started ack")
            return
        if event == VolcengineBinaryProtocol.EVENT_CONNECTION_FAILED:
            self._log("[voice_session] Connection Failed")
            return
        if event == VolcengineBinaryProtocol.EVENT_CONNECTION_FINISHED:
            self._log("[voice_session] Connection Finished")
            return
        if event == VolcengineBinaryProtocol.EVENT_ASR_RESPONSE:
            results = payload.get("results")
            if isinstance(results, list) and results:
                first = results[0] if isinstance(results[0], dict) else {}
                text = str(first.get("text") or "")
                interim = bool(first.get("is_interim"))
                if text:
                    self.orchestrator.transition(
                        VoiceEventType.ASR_PARTIAL if interim else VoiceEventType.ASR_FINAL,
                        {"text": text, "source": "volc_asr", "role": "pilot"},
                    )
            return
        if event == VolcengineBinaryProtocol.EVENT_ASR_INFO:
            self._log("[voice_session] ASR first token detected")
            return
        if event == VolcengineBinaryProtocol.EVENT_ASR_ENDED:
            self.orchestrator.transition(VoiceEventType.TURN_END, {"source": "volc_asr_end"})
            return
        if event == VolcengineBinaryProtocol.EVENT_CHAT_RESPONSE:
            content = str(payload.get("content") or "").strip()
            if content:
                if self._should_play_tts_audio():
                    self.orchestrator.transition(VoiceEventType.ASR_PARTIAL, {"text": content, "source": "volc_chat", "role": "agent"})
                else:
                    self._log("[voice_session] suppressed gateway ChatResponse text")
            return
        if event == VolcengineBinaryProtocol.EVENT_CHAT_ENDED:
            content = str(payload.get("content") or "").strip()
            if content:
                if self._should_play_tts_audio():
                    self.orchestrator.transition(VoiceEventType.ASR_FINAL, {"text": content, "source": "volc_chat", "role": "agent"})
                else:
                    self._log("[voice_session] suppressed gateway ChatEnded text")
            return
        if event == VolcengineBinaryProtocol.EVENT_TTS_SENTENCE_START:
            txt = str(payload.get("text") or "").strip()
            if txt and self._should_play_tts_audio():
                self.orchestrator.transition(VoiceEventType.ASR_PARTIAL, {"text": txt, "source": "volc_tts_sentence", "role": "agent"})
                self.orchestrator.transition(VoiceEventType.AGENT_SPEAKING, {"source": "volc_tts_start"})
            return
        if event == VolcengineBinaryProtocol.EVENT_TTS_RESPONSE:
            if not self._should_play_tts_audio():
                return
            audio = packet.get("payload_audio")
            if isinstance(audio, (bytes, bytearray)) and audio:
                try:
                    self._play_queue.put_nowait(bytes(audio))
                except queue.Full:
                    pass
            self.orchestrator.transition(VoiceEventType.AGENT_SPEAKING, {"source": "volc_tts"})
            return
        if event == VolcengineBinaryProtocol.EVENT_TTS_SENTENCE_END:
            txt = str(payload.get("text") or "").strip()
            if txt and self._should_play_tts_audio():
                self.orchestrator.transition(VoiceEventType.ASR_FINAL, {"text": txt, "source": "volc_tts_sentence_end", "role": "agent"})
            self._log("[voice_session] TTS sentence end")
            return
        if event == VolcengineBinaryProtocol.EVENT_TTS_ENDED:
            txt = str(payload.get("text") or "").strip()
            if txt and self._should_play_tts_audio():
                self.orchestrator.transition(VoiceEventType.ASR_FINAL, {"text": txt, "source": "volc_tts_end", "role": "agent"})
            self._tts_playback_allowed_until = 0.0
            self.orchestrator.transition(VoiceEventType.SILENCE_TIMEOUT, {"source": "volc_tts_end"})
            return
        if event == VolcengineBinaryProtocol.EVENT_USAGE_RESPONSE:
            self._log("[voice_session] usage response received")
            return
        if msg_type == VolcengineBinaryProtocol.MSG_ERROR or event == VolcengineBinaryProtocol.EVENT_DIALOG_COMMON_ERROR:
            code = payload.get("status_code")
            message = str(payload.get("message") or "")
            if (code is None or not message) and isinstance(payload_audio, (bytes, bytearray)) and payload_audio:
                try:
                    raw_text = bytes(payload_audio).decode("utf-8", errors="replace")
                    message = message or raw_text
                except Exception:
                    pass
            if code is None and payload:
                # Fallback for non-standard key names.
                code = payload.get("code") or payload.get("status")
            self._log(f"[voice_session] volc error code={code} message={message}")
            self._log(
                "[voice_session] volc error packet "
                + (
                    f"msg_type={msg_type} flags={packet.get('message_flags')} event={event} "
                    f"payload_size={packet.get('payload_size')} ser={packet.get('serialization')} "
                    f"error_code={packet.get('error_code')} keys={list(payload.keys()) if payload else []}"
                )
            )
            return

        if event not in {
            VolcengineBinaryProtocol.EVENT_SESSION_STARTED,
            VolcengineBinaryProtocol.EVENT_ASR_RESPONSE,
            VolcengineBinaryProtocol.EVENT_ASR_ENDED,
            VolcengineBinaryProtocol.EVENT_CHAT_RESPONSE,
            VolcengineBinaryProtocol.EVENT_CHAT_ENDED,
            VolcengineBinaryProtocol.EVENT_TTS_RESPONSE,
            VolcengineBinaryProtocol.EVENT_TTS_SENTENCE_START,
            VolcengineBinaryProtocol.EVENT_TTS_SENTENCE_END,
            VolcengineBinaryProtocol.EVENT_TTS_ENDED,
            VolcengineBinaryProtocol.EVENT_ASR_INFO,
            VolcengineBinaryProtocol.EVENT_USAGE_RESPONSE,
        }:
            self._log(
                "[voice_session] unhandled packet "
                + f"msg_type={msg_type} event={event} payload_size={packet.get('payload_size')}"
            )

    def _should_play_tts_audio(self) -> bool:
        now = time.time()
        return now <= self._tts_playback_allowed_until or now <= self._gateway_tts_allowed_until


def build_voice_session(config: VoiceConfig, orchestrator: VoiceOrchestrator, *, log_fn: VoiceLogFn | None = None) -> VoiceSession:
    errors: list[str] = []
    for provider in config.providers:
        try:
            if provider == "openai":
                return OpenAIRealtimeSession(config, orchestrator, log_fn=log_fn)
            if provider == "volcengine":
                return VolcengineRealtimeSession(config, orchestrator, log_fn=log_fn)
            if provider == "mock":
                return MockRealtimeSession(config, orchestrator, log_fn=log_fn)
        except Exception as exc:
            errors.append(f"{provider} init failed: {type(exc).__name__}: {exc}")
    if not errors:
        return MockRealtimeSession(config, orchestrator, log_fn=log_fn)
    raise RuntimeError("No voice provider available. " + " | ".join(errors))


def select_and_start_voice_session(
    config: VoiceConfig,
    orchestrator: VoiceOrchestrator,
    *,
    log_fn: VoiceLogFn | None = None,
) -> VoiceSession:
    errors: list[str] = []
    for provider in config.providers:
        try:
            session = _build_by_provider(provider, config, orchestrator, log_fn=log_fn)
            session.start()
            _log(log_fn, f"[voice_session] active_provider={provider}")
            return session
        except Exception as exc:
            err = f"{provider} failed: {type(exc).__name__}: {exc}"
            errors.append(err)
            _log(log_fn, "[voice_session] " + err)
    raise RuntimeError("All voice providers failed. " + " | ".join(errors))


def _build_by_provider(
    provider: str,
    config: VoiceConfig,
    orchestrator: VoiceOrchestrator,
    *,
    log_fn: VoiceLogFn | None = None,
) -> VoiceSession:
    if provider == "openai":
        return OpenAIRealtimeSession(config, orchestrator, log_fn=log_fn)
    if provider == "volcengine":
        return VolcengineRealtimeSession(config, orchestrator, log_fn=log_fn)
    if provider == "mock":
        return MockRealtimeSession(config, orchestrator, log_fn=log_fn)
    raise RuntimeError(f"unknown provider: {provider}")


def _read_env(key: str) -> str:
    import os

    return (os.getenv(key) or "").strip()


def _validate_ws_url(value: str, env_key: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
        raise RuntimeError(f"{env_key} must be a valid ws/wss URL")


def _log(log_fn: VoiceLogFn | None, message: str) -> None:
    if log_fn is None:
        return
    log_fn(message)


def _pcm16_peak_abs(raw: bytes) -> int:
    if not raw:
        return 0
    peak = 0
    for (sample,) in struct.iter_unpack("<h", raw):
        sample_abs = abs(sample)
        if sample_abs > peak:
            peak = sample_abs
    return peak


def _to_int_or_default(raw: str, default: int) -> int:
    try:
        return int(raw.strip())
    except Exception:
        return default
