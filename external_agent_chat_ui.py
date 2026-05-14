from __future__ import annotations

"""Main desktop console for the X-Plane Co-Pilot.

This module owns the end-to-end product flow:
- Tkinter UI and operator-visible chat history.
- Fast rule-based routing for low-latency state/control requests.
- Slow LLM tool loop for complex tasks.
- Voice ASR final dispatch and backend-authoritative TTS playback.

Design boundary: Doubao/voice providers are treated as a voice gateway.
The backend agent remains the authority for flight facts, risk analysis,
tool calls, and control execution.
"""

import json
import os
import queue
import re
import socket
import threading
import time
import tkinter as tk
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tkinter import ttk
from typing import Any

from agent_core.copilot_core import Action, ActionPlan, ActionType, ControlMode
from agent_core.copilot_guard_executor import ActionExecutor, ActionGuard
from agent_core.proactive_watchdog import ProactiveEvent, ProactiveWatchdog, ProactiveWatchdogConfig
from agent_core.copilot_situation import SituationInferenceEngine
from agent_core.copilot_state_monitor import FlightStateMonitor


from agent_core.agent_tools import (
    _load_fast_path_policy,
    AgentToolBridge,
    ChatState,
    FastCommandRouter,
    FastPathDecision,
    load_fast_path_policy,
    parse_agent_payload,
)
from agent_core.background_tools import BackgroundToolJob
from voice_agent import VoiceConfig, VoiceEventType, VoiceOrchestrator, select_and_start_voice_session


@dataclass(frozen=True)
class VoiceIntentDecision:
    """Classification result for one stable ASR final transcript."""

    intent: str
    confidence: float
    reason: str
    source: str = "rule"


class ExternalAgentChatApp:
    def __init__(
        self,
        *,
        model: str = "gpt-4o",
        plugin_host: str = "127.0.0.1",
        plugin_port: int = 49120,
        xp_host: str = "127.0.0.1",
        xp_port: int = 49009,
    ) -> None:
        self._configure_dpi_awareness()

        self.model = model
        self.voice_config = VoiceConfig.from_env()
        self.voice = VoiceOrchestrator(self.voice_config)
        self.voice_session = None
        self.voice_ui_running = False
        self._voice_partial_rendered = ""
        self._voice_final_seen = ""
        self._voice_final_seen_role = ""
        self._voice_agent_partial_last = ""
        self._voice_agent_partial_last_emit = ""
        self._voice_agent_partial_buffer = ""
        self._voice_agent_partial_flushed = ""
        self._voice_pilot_partial_last = ""
        self._voice_pilot_partial_last_emit = ""
        self._voice_pilot_final_last_handled = ""
        self._voice_auto_submit_enabled = True
        self._show_backend_agent_reply = False
        self._show_backend_reply_for_voice_tasks = True
        self._voice_agent_call_mode = (os.getenv("VOICE_AGENT_CALL_MODE") or "always").strip().lower()
        self._voice_intent_llm_fallback = os.getenv("VOICE_INTENT_LLM_FALLBACK", "false").strip().lower() in {"1", "true", "yes", "on"}
        self._voice_intent_llm_threshold = float(os.getenv("VOICE_INTENT_LLM_THRESHOLD", "0.55") or "0.55")
        self._voice_transcript_from_model_capable = True
        self._voice_playback_text_last = ""
        self._pending_voice_prompts: queue.Queue[str] = queue.Queue(maxsize=16)
        self._strategy_lock = threading.Lock()
        self._strategy_version = 0
        self._strategy_updated_at = 0.0
        self._strategy_delta: dict[str, Any] = {}
        self._stop_strategy = threading.Event()
        self.state = ChatState()
        self.client = self._init_openai_client()
        self.plugin_host = plugin_host
        self.plugin_port = plugin_port
        self.monitor = FlightStateMonitor(
            xp_host=xp_host,
            xp_port=xp_port,
            sample_hz=2.0,
            timeout_ms=1000,
        )
        self.situation_engine = SituationInferenceEngine()
        self.guard = ActionGuard()
        axis_cfg = Path(__file__).resolve().parent / "control_axis_config.json"
        self.executor = ActionExecutor.from_axis_config(
            xp_host=xp_host,
            xp_port=xp_port,
            timeout_ms=1000,
            config_path=axis_cfg,
        )
        self.fast_policy = load_fast_path_policy(Path(__file__).resolve().parent)
        self.tools = AgentToolBridge(
            monitor=self.monitor,
            situation_engine=self.situation_engine,
            guard=self.guard,
            executor=self.executor,
        )
        self.fast_router = FastCommandRouter(self.tools, policy=self.fast_policy)
        self.monitor.start()
        self.proactive_events: queue.Queue[ProactiveEvent] = queue.Queue(maxsize=64)
        self.proactive_watchdog = ProactiveWatchdog(
            monitor=self.monitor,
            situation_engine=self.situation_engine,
            on_event=self._enqueue_proactive_event,
            config=ProactiveWatchdogConfig(),
        )
        self.proactive_watchdog.start()
        self._stop_background = threading.Event()
        self._proactive_thread = threading.Thread(target=self._run_proactive_worker, daemon=True, name="proactive_worker")
        self._proactive_thread.start()
        self._strategy_thread = threading.Thread(target=self._run_strategy_worker, daemon=True, name="voice_strategy_worker")
        self._strategy_thread.start()
        self._bg_summary_lock = threading.Lock()
        self._pending_bg_summaries: list[str] = []

        self.last_plugin_ok_at: float | None = None
        self.last_plugin_error: str | None = None
        self.last_llm_ok_at: float | None = None
        self.last_llm_error: str | None = None
        self.last_llm_latency_ms: int | None = None
        self.llm_inflight = False
        self.fast_inflight = False
        self.proactive_inflight = False
        self.health_widgets: dict[str, dict[str, tk.Label]] = {}
        self.bubble_labels: list[tk.Label] = []

        self.root = tk.Tk()
        self.root.title("X-Plane Co-Pilot Console")
        self.root.geometry("1220x820")
        self.root.minsize(980, 680)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.configure(bg="#EEF3FA")
        self._apply_tk_scaling()
        self.voice_speaker_var = tk.StringVar(master=self.root, value=os.getenv("VOLCENGINE_TTS_SPEAKER", "zh_female_vv_jupiter_bigtts"))
        self.voice_input_mod_var = tk.StringVar(master=self.root, value=os.getenv("DOUBAO_INPUT_MOD", "keep_alive"))
        self.voice_end_smooth_var = tk.StringVar(master=self.root, value=os.getenv("DOUBAO_END_SMOOTH_MS", "1500"))
        self.voice_custom_vad_var = tk.BooleanVar(master=self.root, value=(os.getenv("DOUBAO_ENABLE_CUSTOM_VAD", "false").lower() in {"1", "true", "yes", "on"}))
        self.voice_vad_silence_var = tk.StringVar(master=self.root, value=os.getenv("VOICE_VAD_SILENCE_MS", str(self.voice_config.vad_silence_ms)))

        self._init_styles()
        self._build_ui()
        self._schedule_health_refresh()
        if self.voice_config.enabled:
            try:
                self.voice_session = select_and_start_voice_session(self.voice_config, self.voice, log_fn=self._log_terminal)
                self._log_terminal(
                    "[voice] enabled mode="
                    + self.voice_config.mode
                    + f", sample_rate_hz={self.voice_config.sample_rate_hz}, frame_ms={self.voice_config.frame_ms}"
                    + f", providers={','.join(self.voice_config.providers)}"
                )
            except Exception as exc:
                self._log_terminal(f"[voice] startup failed: {type(exc).__name__}: {exc}")
                if self.voice_config.fallback_text:
                    self._log_terminal("[voice] fallback to text mode is enabled; continue without voice session")
                else:
                    raise
        else:
            self._log_terminal("[voice] disabled; using text-only mode")

        # 背景执行完成回调：仅记录终端日志，不直接插入 UI，避免打断对话连贯性
        self.tools.background._on_complete = self._on_background_tool_complete

    @staticmethod
    def _configure_dpi_awareness() -> None:
        if os.name != "nt":
            return
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            return
        except Exception:
            pass
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    def _apply_tk_scaling(self) -> None:
        try:
            ppi = float(self.root.winfo_fpixels("1i"))
            scaling = max(1.15, min(2.0, ppi / 72.0))
            self.root.tk.call("tk", "scaling", scaling)
        except Exception:
            pass

    def _init_styles(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Root.TFrame", background="#EEF3FA")
        style.configure("Card.TFrame", background="#FFFFFF", relief="flat")
        style.configure("HeaderTitle.TLabel", background="#EEF3FA", foreground="#0B1220", font=("Segoe UI Semibold", 22))
        style.configure("HeaderMeta.TLabel", background="#EEF3FA", foreground="#5E6C84", font=("Segoe UI", 10))
        style.configure("Status.TLabel", background="#DCE7F5", foreground="#1A2433", font=("Segoe UI Semibold", 10), padding=(10, 4))
        style.configure("Primary.TButton", font=("Segoe UI Semibold", 11), padding=(16, 10))
        style.map("Primary.TButton", background=[("active", "#1E40AF"), ("!disabled", "#1D4ED8")], foreground=[("!disabled", "#FFFFFF")])
        style.configure("InputHint.TLabel", background="#FFFFFF", foreground="#5E6C84", font=("Segoe UI", 9))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, style="Root.TFrame", padding=(20, 18, 20, 16))
        outer.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(outer, style="Root.TFrame")
        header.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(header, text="External Agent Chat", style="HeaderTitle.TLabel").pack(side=tk.LEFT)
        ttk.Label(header, text=f"Model: {self.model} | UDP: {self.plugin_host}:{self.plugin_port}", style="HeaderMeta.TLabel").pack(side=tk.RIGHT, pady=(10, 0))

        health_card = ttk.Frame(outer, style="Card.TFrame", padding=(14, 10, 14, 10))
        health_card.pack(fill=tk.X, pady=(0, 10))
        self._create_health_item(health_card, "xpc", "XPC")
        self._create_health_item(health_card, "plugin", "Plugin")
        self._create_health_item(health_card, "llm", "LLM")
        self._create_health_item(health_card, "voice_gateway", "Voice Gateway")
        self._create_health_item(health_card, "backend", "Backend Running")
        self._create_health_item(health_card, "tts", "TTS Playback")

        chat_card = ttk.Frame(outer, style="Card.TFrame", padding=(10, 10, 10, 10))
        chat_card.pack(fill=tk.BOTH, expand=True)

        self.chat_canvas = tk.Canvas(chat_card, bg="#F8FBFF", bd=0, highlightthickness=0)
        chat_scroll = ttk.Scrollbar(chat_card, orient=tk.VERTICAL, command=self.chat_canvas.yview)
        self.chat_canvas.configure(yscrollcommand=chat_scroll.set)
        self.chat_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        chat_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.messages_frame = tk.Frame(self.chat_canvas, bg="#F8FBFF")
        self.chat_window_id = self.chat_canvas.create_window((0, 0), window=self.messages_frame, anchor="nw")
        self.messages_frame.bind("<Configure>", self._on_messages_configure)
        self.chat_canvas.bind("<Configure>", self._on_canvas_resize)

        composer_card = ttk.Frame(outer, style="Card.TFrame", padding=(12, 10, 12, 8))
        composer_card.pack(fill=tk.X, pady=(10, 0))

        input_row = ttk.Frame(composer_card, style="Card.TFrame")
        input_row.pack(fill=tk.X)

        self.input_text = tk.Text(input_row, height=3, wrap=tk.WORD, font=("Segoe UI", 11), bg="#F4F8FF", fg="#0F172A", bd=1, relief="solid", padx=8, pady=6)
        self.input_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.input_text.bind("<Return>", self._on_enter_send)
        self.input_text.bind("<Shift-Return>", self._on_shift_enter)
        self.input_text.focus_set()

        self.send_button = ttk.Button(input_row, text="Send", style="Primary.TButton", command=self._on_send)
        self.send_button.pack(side=tk.LEFT, padx=(10, 0))
        self.voice_button = ttk.Button(input_row, text="Voice Start", style="Primary.TButton", command=self._toggle_voice_session)
        self.voice_button.pack(side=tk.LEFT, padx=(8, 0))
        self._build_voice_settings(composer_card)

        bottom_row = ttk.Frame(composer_card, style="Card.TFrame")
        bottom_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(bottom_row, text="Enter to send, Shift+Enter for newline", style="InputHint.TLabel").pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="Ready")
        self.status_label = ttk.Label(bottom_row, textvariable=self.status_var, style="Status.TLabel")
        self.status_label.pack(side=tk.RIGHT)

    def _build_voice_settings(self, parent: ttk.Frame) -> None:
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(row, text="Voice Speaker", style="InputHint.TLabel").pack(side=tk.LEFT)
        speaker = ttk.Combobox(
            row,
            width=28,
            state="readonly",
            textvariable=self.voice_speaker_var,
            values=(
                "zh_female_vv_jupiter_bigtts",
                "zh_female_xiaohe_jupiter_bigtts",
                "zh_male_yunzhou_jupiter_bigtts",
                "zh_male_xiaotian_jupiter_bigtts",
            ),
        )
        speaker.pack(side=tk.LEFT, padx=(6, 12))
        ttk.Label(row, text="Input Mode", style="InputHint.TLabel").pack(side=tk.LEFT)
        input_mode = ttk.Combobox(
            row,
            width=14,
            state="readonly",
            textvariable=self.voice_input_mod_var,
            values=("keep_alive", "push_to_talk"),
        )
        input_mode.pack(side=tk.LEFT, padx=(6, 12))
        ttk.Label(row, text="EndSmooth(ms)", style="InputHint.TLabel").pack(side=tk.LEFT)
        tk.Entry(row, width=7, textvariable=self.voice_end_smooth_var).pack(side=tk.LEFT, padx=(6, 12))
        ttk.Label(row, text="VAD Silence(ms)", style="InputHint.TLabel").pack(side=tk.LEFT)
        tk.Entry(row, width=7, textvariable=self.voice_vad_silence_var).pack(side=tk.LEFT, padx=(6, 12))
        ttk.Checkbutton(row, text="Custom VAD", variable=self.voice_custom_vad_var).pack(side=tk.LEFT, padx=(6, 12))
        ttk.Button(row, text="Apply Voice", command=self._apply_voice_runtime_settings).pack(side=tk.LEFT)

    def _create_health_item(self, parent: ttk.Frame, key: str, title: str) -> None:
        row = tk.Frame(parent, bg="#FFFFFF")
        row.pack(side=tk.LEFT, padx=(0, 24))
        dot = tk.Label(row, text="●", fg="#94A3B8", bg="#FFFFFF", font=("Segoe UI Symbol", 11))
        dot.pack(side=tk.LEFT)
        label = tk.Label(row, text=f"{title}: Unknown", fg="#334155", bg="#FFFFFF", font=("Segoe UI", 10))
        label.pack(side=tk.LEFT, padx=(6, 0))
        self.health_widgets[key] = {"dot": dot, "label": label}

    def _set_health_item(self, key: str, *, level: str, text: str) -> None:
        item = self.health_widgets.get(key)
        if item is None:
            return
        colors = {"ok": "#16A34A", "busy": "#2563EB", "warn": "#F59E0B", "err": "#DC2626"}
        item["dot"].configure(fg=colors.get(level, "#94A3B8"))
        item["label"].configure(text=text)

    def _on_messages_configure(self, event=None) -> None:
        self.chat_canvas.configure(scrollregion=self.chat_canvas.bbox("all"))

    def _on_canvas_resize(self, event) -> None:
        width = max(200, event.width)
        self.chat_canvas.itemconfigure(self.chat_window_id, width=width)
        wrap = max(340, int(width * 0.62))
        for bubble in self.bubble_labels:
            bubble.configure(wraplength=wrap)

    @staticmethod
    def _init_openai_client():
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is missing.")
        from openai import OpenAI

        base_url = os.getenv("OPENAI_BASE_URL")
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        return OpenAI(**kwargs)

    def _append_chat(self, role: str, text: str) -> None:
        role_key = role.strip().lower()
        outer = tk.Frame(self.messages_frame, bg="#F8FBFF")
        outer.pack(fill=tk.X, pady=6, padx=8)

        if role_key.startswith("pilot"):
            anchor = "e"
            role_fg = "#1E40AF"
            bubble_bg = "#1D4ED8"
            bubble_fg = "#FFFFFF"
            justify = tk.RIGHT
        elif role_key == "backend":
            anchor = "w"
            role_fg = "#0F766E"
            bubble_bg = "#ECFEFF"
            bubble_fg = "#0F172A"
            justify = tk.LEFT
        elif role_key == "voice":
            anchor = "w"
            role_fg = "#7C3AED"
            bubble_bg = "#F5F3FF"
            bubble_fg = "#1E1B4B"
            justify = tk.LEFT
        elif role_key == "agent":
            anchor = "w"
            role_fg = "#0F766E"
            bubble_bg = "#ECFEFF"
            bubble_fg = "#0F172A"
            justify = tk.LEFT
        else:
            anchor = "w"
            role_fg = "#64748B"
            bubble_bg = "#E2E8F0"
            bubble_fg = "#0F172A"
            justify = tk.LEFT

        ts = time.strftime("%H:%M:%S")
        role_label = tk.Label(outer, text=f"{role}  {ts}", fg=role_fg, bg="#F8FBFF", font=("Segoe UI Semibold", 9))
        role_label.pack(anchor=anchor, padx=4, pady=(0, 2))

        bubble = tk.Label(
            outer,
            text=text,
            fg=bubble_fg,
            bg=bubble_bg,
            justify=justify,
            anchor="w",
            padx=12,
            pady=10,
            font=("Segoe UI", 11),
            wraplength=max(340, int(self.chat_canvas.winfo_width() * 0.62)),
        )
        bubble.pack(anchor=anchor, padx=4)
        self.bubble_labels.append(bubble)
        self.chat_canvas.update_idletasks()
        self.chat_canvas.yview_moveto(1.0)

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _toggle_voice_session(self) -> None:
        if not self.voice_config.enabled:
            self._set_status("Voice disabled by VOICE_ENABLED=false")
            return
        try:
            if self.voice_ui_running and self.voice_session is not None:
                self.voice_session.stop()
                self.voice_ui_running = False
                self.voice_button.configure(text="Voice Start")
                self._set_status("Voice stopped")
                return
            if self.voice_session is None:
                self.voice_session = select_and_start_voice_session(self.voice_config, self.voice, log_fn=self._log_terminal)
            else:
                self.voice_session.start()
            self.voice_ui_running = True
            self.voice_button.configure(text="Voice Stop")
            self._set_status("Voice running")
        except Exception as exc:
            self._set_status(f"Voice start failed: {type(exc).__name__}")
            self._log_terminal(f"[voice] toggle failed: {type(exc).__name__}: {exc}")

    def _apply_voice_runtime_settings(self) -> None:
        os.environ["VOLCENGINE_TTS_SPEAKER"] = self.voice_speaker_var.get().strip()
        os.environ["DOUBAO_INPUT_MOD"] = self.voice_input_mod_var.get().strip()
        os.environ["DOUBAO_END_SMOOTH_MS"] = self.voice_end_smooth_var.get().strip()
        os.environ["DOUBAO_ENABLE_CUSTOM_VAD"] = "true" if self.voice_custom_vad_var.get() else "false"
        os.environ["VOICE_VAD_SILENCE_MS"] = self.voice_vad_silence_var.get().strip()
        os.environ["DOUBAO_BOT_NAME"] = "副驾语音网关"
        os.environ["DOUBAO_SYSTEM_ROLE"] = (
            "你是前台实时语音交互代理。你的职责仅限语音接入、简短确认和澄清。"
            "凡涉及新事实、飞行状态、控制指令、执行结果，必须等待后台分析Agent确认后再回复。"
            "在未确认前只允许回复笼统过渡语，不得编造参数、状态或执行结果。"
        )
        os.environ["DOUBAO_SPEAKING_STYLE"] = "语气简洁、专业、克制。优先使用短句确认，不扩展推断。"
        try:
            self.voice_config = VoiceConfig.from_env()
            if self.voice_session is not None and self.voice_ui_running:
                self.voice_session.stop()
                self.voice_session = select_and_start_voice_session(self.voice_config, self.voice, log_fn=self._log_terminal)
            self._set_status("Voice settings applied")
            self._log_terminal(
                "[voice_settings] "
                + json.dumps(
                    {
                        "speaker": os.environ.get("VOLCENGINE_TTS_SPEAKER"),
                        "input_mod": os.environ.get("DOUBAO_INPUT_MOD"),
                        "end_smooth_ms": os.environ.get("DOUBAO_END_SMOOTH_MS"),
                        "custom_vad": os.environ.get("DOUBAO_ENABLE_CUSTOM_VAD"),
                        "vad_silence_ms": os.environ.get("VOICE_VAD_SILENCE_MS"),
                    },
                    ensure_ascii=False,
                )
            )
        except Exception as exc:
            self._set_status(f"Apply voice failed: {type(exc).__name__}")
            self._log_terminal(f"[voice_settings] apply failed: {type(exc).__name__}: {exc}")

    @staticmethod
    def _log_terminal(message: str) -> None:
        print(message, flush=True)

    def _on_background_tool_complete(self, job: BackgroundToolJob) -> None:
        payload = {
            "job_id": job.job_id,
            "tool": job.tool_name,
            "status": job.status,
            "result": job.result,
            "error": job.error,
            "created_at": job.created_at,
            "finished_at": job.finished_at,
        }
        self._log_terminal("[bg_tool_done] " + json.dumps(payload, ensure_ascii=False))
        summary = self._format_bg_job_summary(job)
        with self._bg_summary_lock:
            self._pending_bg_summaries.append(summary)

    @staticmethod
    def _format_bg_job_summary(job: BackgroundToolJob) -> str:
        if job.result is None:
            return f"{job.tool_name} 后台任务结束，但未返回结果。"
        ok = bool(job.result.get("ok"))
        if job.tool_name == "set_target_pitch_deg":
            target = ExternalAgentChatApp._format_spoken_number(job.result.get("target_pitch_deg"), decimals=1)
            final = ExternalAgentChatApp._format_spoken_number(job.result.get("final_pitch_deg"), decimals=1)
            if ok:
                return f"后台执行完成：俯仰目标 {target}° 已达到，当前约 {final}°。"
            return f"后台执行结束：俯仰目标 {target}° 未在时限内收敛，当前约 {final}°。"
        if job.tool_name == "turn_to_heading":
            target = ExternalAgentChatApp._format_spoken_number(job.result.get("target_heading_deg"), decimals=0)
            final = ExternalAgentChatApp._format_spoken_number(job.result.get("final_heading_deg"), decimals=0)
            if ok:
                return f"后台执行完成：航向目标 {target}° 已达到，当前约 {final}°。"
            return f"后台执行结束：航向目标 {target}° 未在时限内收敛，当前约 {final}°。"
        if ok:
            return f"后台执行完成：{job.tool_name} 成功。"
        return f"后台执行结束：{job.tool_name} 失败（{job.result.get('error', 'unknown_error')}）。"

    def _drain_bg_summaries(self) -> list[str]:
        with self._bg_summary_lock:
            if not self._pending_bg_summaries:
                return []
            out = self._pending_bg_summaries[:]
            self._pending_bg_summaries.clear()
            return out

    def _enqueue_proactive_event(self, event: ProactiveEvent) -> None:
        try:
            self.proactive_events.put_nowait(event)
            self._log_terminal(
                "[proactive_event] "
                + json.dumps(
                    {
                        "event_type": event.event_type,
                        "severity": event.severity,
                        "phase": event.phase,
                        "confidence": event.confidence,
                        "key_metrics": event.key_metrics,
                    },
                    ensure_ascii=False,
                )
            )
        except queue.Full:
            self._log_terminal("[proactive_event] dropped because queue is full")

    def _build_proactive_messages(self, event: ProactiveEvent) -> list[dict[str, str]]:
        system = (
            "你是 X-Plane 11 的副驾驶助手。\n"
            "你收到了一条监测系统主动触发的异常事件。\n"
            "Reply in the same language as the pilot's latest message in this conversation.\n"
            "只返回 JSON，且必须包含 reply 与 overlay 两个字段。\n"
            "reply：简洁告警 + 当前建议动作。\n"
            "overlay：一句简短告警（最多90字符）。"
        )
        payload = {
            "event_type": event.event_type,
            "severity": event.severity,
            "phase": event.phase,
            "confidence": event.confidence,
            "risks": event.risks,
            "key_metrics": event.key_metrics,
            "triggered_at": event.triggered_at,
        }
        return [
            {"role": "system", "content": system},
            {"role": "system", "content": "proactive_event=" + json.dumps(payload, ensure_ascii=False)},
            {"role": "user", "content": "请生成主动座舱告警，并给出一条可执行建议。"},
        ]

    @staticmethod
    def _clamp(value: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, value))

    def _build_proactive_action_plan(self, event: ProactiveEvent) -> ActionPlan | None:
        metrics = event.key_metrics or {}
        vvi = float(metrics.get("vertical_speed_fpm") or 0.0)
        roll = float(metrics.get("roll_deg") or 0.0)
        actions: list[Action] = []

        if event.event_type == "stall_risk":
            actions.append(Action(type=ActionType.SET_THROTTLE, value=0.9, reason="proactive_stall_recovery"))
            actions.append(Action(type=ActionType.SET_PITCH_CMD, value=-0.12, reason="proactive_stall_recovery"))
        elif event.event_type == "overspeed_risk":
            actions.append(Action(type=ActionType.SET_THROTTLE, value=0.2, reason="proactive_overspeed_recovery"))
            actions.append(Action(type=ActionType.SET_SPEEDBRAKE, value=0.5, reason="proactive_overspeed_recovery"))
        elif event.event_type == "unstable_approach":
            if abs(roll) > 20.0:
                roll_cmd = self._clamp(-roll / 45.0, -0.35, 0.35)
                actions.append(Action(type=ActionType.SET_ROLL_CMD, value=roll_cmd, reason="proactive_stabilize_roll"))
            if abs(vvi) > 1400.0:
                pitch_cmd = -0.10 if vvi > 0 else 0.12
                actions.append(Action(type=ActionType.SET_PITCH_CMD, value=pitch_cmd, reason="proactive_stabilize_vvi"))
        elif event.event_type == "runway_excursion_risk":
            actions.append(Action(type=ActionType.SET_THROTTLE, value=0.0, reason="proactive_runway_excursion"))
            actions.append(Action(type=ActionType.SET_RUDDER_CMD, value=0.0, reason="proactive_runway_excursion"))
        else:
            return None

        if not actions:
            return None
        return ActionPlan(requested_by="proactive_watchdog", mode=ControlMode.ASSISTED, actions=actions)

    @staticmethod
    def _format_proactive_exec_summary(exec_result: dict[str, Any] | None) -> str:
        if not exec_result:
            return "未执行自动缓解动作。"
        if exec_result.get("ok"):
            executed = exec_result.get("executed") or []
            if executed:
                return "已执行自动缓解动作：" + "、".join(map(str, executed)) + "。"
            return "已触发自动缓解流程。"
        violations = exec_result.get("guard_violations") or []
        if violations:
            return "自动缓解动作被安全规则拦截：" + "；".join(map(str, violations)) + "。"
        return "自动缓解动作执行失败。"

    @staticmethod
    def _looks_english(text: str) -> bool:
        if not text:
            return False
        letters = sum(1 for ch in text if ("a" <= ch.lower() <= "z"))
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        return letters > 12 and cjk == 0

    def _fallback_cn_proactive_reply(self, event: ProactiveEvent) -> tuple[str, str]:
        metrics = event.key_metrics or {}
        phase = event.phase
        risk = event.event_type
        airspeed = metrics.get("airspeed_kts")
        vvi = metrics.get("vertical_speed_fpm")
        if risk == "stall_risk":
            reply = f"检测到失速风险（阶段：{phase}）。建议立即加油门并轻微下俯，优先恢复空速。"
            overlay = "主动告警：失速风险，立即恢复空速。"
        elif risk == "overspeed_risk":
            reply = f"检测到超速风险（阶段：{phase}）。建议立即减油门并适度放出速度刹车。"
            overlay = "主动告警：超速风险，立即减速。"
        elif risk == "unstable_approach":
            reply = f"检测到进近不稳定（阶段：{phase}，V/S={vvi} fpm）。建议先稳住姿态，再修正下滑率。"
            overlay = "主动告警：进近不稳定，先稳住姿态。"
        else:
            reply = (
                f"检测到异常风险（{risk}，阶段：{phase}，空速={airspeed} kt）。"
                "建议立即检查姿态与推力，执行稳定化操作。"
            )
            overlay = "主动告警：检测到异常风险。"
        return reply, overlay[:90]

    def _run_proactive_worker(self) -> None:
        while not self._stop_background.is_set():
            try:
                event = self.proactive_events.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.proactive_inflight = True
                proactive_plan = self._build_proactive_action_plan(event)
                proactive_exec: dict[str, Any] | None = None
                if proactive_plan is not None:
                    proactive_exec = self.tools.execute_plan(proactive_plan)
                    self._log_terminal("[proactive_exec] " + json.dumps(proactive_exec, ensure_ascii=False))

                messages = self._build_proactive_messages(event)
                if proactive_exec is not None:
                    messages.append(
                        {
                            "role": "system",
                            "content": "proactive_exec=" + json.dumps(proactive_exec, ensure_ascii=False),
                        }
                    )
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.2,
                    max_tokens=240,
                )
                content = response.choices[0].message.content or ""
                reply, overlay = parse_agent_payload(content)
                if self._looks_english(reply):
                    reply, overlay = self._fallback_cn_proactive_reply(event)
                exec_summary = self._format_proactive_exec_summary(proactive_exec)
                reply = exec_summary + "\n" + reply
                self._send_overlay_to_plugin(overlay)
                self.last_llm_ok_at = time.time()
                self.last_llm_error = None
                self.root.after(0, lambda: self._append_chat("Agent", reply))
                self.root.after(0, lambda: self._set_status(f"主动告警：{overlay}"))
                self._log_terminal(f"[proactive_reply] {reply}")
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                self.last_llm_error = err
                self._log_terminal(f"[proactive_error] {err}")
            finally:
                self.proactive_inflight = False
                self.proactive_events.task_done()

    def _on_send(self, event=None) -> None:
        prompt = self.input_text.get("1.0", tk.END).strip()
        if not prompt:
            return
        self._submit_user_prompt(prompt, source="text_input")

    def _on_enter_send(self, event=None):
        self._on_send()
        return "break"

    @staticmethod
    def _on_shift_enter(event=None):
        return None

    def _build_state_context(self) -> dict[str, Any]:
        # 兼容无样本场景，确保提示词始终有稳定结构
        latest = self.monitor.get_latest()
        if latest is None:
            return {
                "available": False,
                "error": self.monitor.get_last_error() or "no_samples",
                "phase": "unavailable",
                "confidence": 0.0,
                "evidence": ["no_samples"],
                "risks": [],
                "latest": None,
                "trend_10s": None,
                "trend_30s": None,
            }

        win10 = self.monitor.get_window(10.0)
        win30 = self.monitor.get_window(30.0)
        situation = self.situation_engine.infer(latest, win10, win30)
        return {
            "available": True,
            "error": self.monitor.get_last_error(),
            **situation.to_prompt_dict(),
        }

    def _build_strategy_context(self) -> dict[str, Any]:
        with self._strategy_lock:
            return {
                "version": self._strategy_version,
                "updated_at": self._strategy_updated_at,
                "delta": dict(self._strategy_delta),
            }

    def _build_messages(self, user_prompt: str, *, fast_decision: FastPathDecision | None = None) -> list[dict[str, Any]]:
        # 构建首轮消息：系统约束 + 状态上下文 + 历史对话 + 当前输入
        state_context = self._build_state_context()
        system = (
            "You are an aviation co-pilot assistant for X-Plane 11.\n"
            "You can use tools to read or control aircraft state.\n"
            "Always call get_flight_state first before control tools when safety context is unclear.\n"
            "Reply in the same language as the user's latest message.\n"
            "For target attitude request (e.g., pitch angle in degrees), use set_target_pitch_deg.\n"
            "For target heading request (e.g., turn west/east/north/south or heading number), use turn_to_heading.\n"
            "set_pitch_cmd/set_roll_cmd/set_rudder_cmd are raw control-input tools, not target-setting tools.\n"
            "After all needed tools are done, return JSON only with exactly two fields:\n"
            "reply: detailed response for external chat UI.\n"
            "overlay: one short sentence (max 90 chars) for in-sim overlay.\n"
            "When giving advice, use this format: action + one-sentence reason.\n"
            "Never suggest takeoff actions when phase is not ground_hold or takeoff_roll."
        )
        msgs: list[dict[str, Any]] = [{"role": "system", "content": system}]
        if fast_decision is not None:
            msgs.append(
                {
                    "role": "system",
                    "content": "fast_path="
                    + json.dumps(
                        {
                            "handled": fast_decision.handled,
                            "kind": fast_decision.kind,
                            "immediate_reply": fast_decision.immediate_reply,
                            "overlay": fast_decision.overlay,
                            "run_slow": fast_decision.run_slow,
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        msgs.append({"role": "system", "content": "state_context=" + json.dumps(state_context, ensure_ascii=False)})
        msgs.append({"role": "system", "content": "strategy_context=" + json.dumps(self._build_strategy_context(), ensure_ascii=False)})
        msgs.extend(self.state.history[-20:])
        msgs.append({"role": "user", "content": user_prompt})

        self._log_terminal(
            "[state_context] "
            + json.dumps(
                {
                    "phase": state_context.get("phase"),
                    "confidence": state_context.get("confidence"),
                    "evidence": state_context.get("evidence"),
                    "risks": state_context.get("risks"),
                    "error": state_context.get("error"),
                },
                ensure_ascii=False,
            )
        )
        return msgs

    def _rewrite_tool_call_by_intent(self, user_prompt: str, name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
        text = (user_prompt or "").strip().lower()
        target_pitch = self.fast_router._parse_target_pitch_deg(text)
        target_heading = self.fast_router._parse_target_heading_deg(text)
        relative_heading_delta = self.fast_router._parse_relative_heading_delta_deg(text)
        if target_heading is None and relative_heading_delta is not None:
            target_heading = self.fast_router._target_heading_from_relative_delta(relative_heading_delta)

        if name == "set_pitch_cmd" and target_pitch is not None:
            return "set_target_pitch_deg", {"value": float(target_pitch)}, "raw pitch input rewritten to target pitch tool"
        if name in {"set_roll_cmd", "set_rudder_cmd"} and target_heading is not None:
            return "turn_to_heading", {"heading_deg": float(target_heading)}, "raw heading input rewritten to target heading tool"
        return name, args, None

    def _run_fast_then_slow_turn(self, user_prompt: str, source: str = "text_input") -> None:
        t0 = time.perf_counter()
        fast_decision: FastPathDecision | None = None
        try:
            self.fast_inflight = True
            self.voice.transition(VoiceEventType.TURN_END, {"source": "text_input"})
            fast_decision = self.fast_router.route(user_prompt)
            if fast_decision is not None:
                show_now = self._show_backend_agent_reply or (source == "voice_input" and self._show_backend_reply_for_voice_tasks)
                if show_now:
                    role = "Backend" if source == "voice_input" else "Agent"
                    self.root.after(0, lambda role=role: self._append_chat(role, fast_decision.immediate_reply))
                status_text = fast_decision.overlay
                if source == "voice_input":
                    status_text = f"[voice->backend] {fast_decision.overlay}"
                self.root.after(0, lambda: self._set_status(status_text))
                try:
                    self._send_overlay_to_plugin(fast_decision.overlay)
                except Exception as exc:
                    self._log_terminal(f"[fast_overlay_error] {type(exc).__name__}: {exc}")
                if fast_decision.tool_result is not None:
                    self._log_terminal("[fast_path_result] " + json.dumps(fast_decision.tool_result, ensure_ascii=False))
            else:
                self.root.after(0, lambda: self._set_status("LLM reasoning..."))

            should_run_slow = fast_decision is None or fast_decision.run_slow
            if should_run_slow:
                self.voice.transition(VoiceEventType.AGENT_THINKING, {"source": "slow_path"})
                self._run_slow_turn(user_prompt, fast_decision, source=source)
            else:
                self.last_llm_ok_at = time.time()
                self.last_llm_latency_ms = int((time.perf_counter() - t0) * 1000)
                self.voice.transition(VoiceEventType.AGENT_SPEAKING, {"source": "fast_path"})
                if source == "voice_input" and fast_decision is not None:
                    self.root.after(0, lambda text=fast_decision.immediate_reply: self._speak_backend_result(text))
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            tb = traceback.format_exc()
            self._log_terminal(f"[fast/slow error] {err}\n{tb}")
            self.last_llm_error = err
            if self._show_backend_agent_reply:
                self.root.after(0, lambda: self._append_chat("Agent", "处理失败，请稍后重试。"))
            self.root.after(0, lambda: self._set_status("Failed"))
            self.voice.transition(VoiceEventType.SILENCE_TIMEOUT, {"reason": "error"})
        finally:
            self.fast_inflight = False
            self.root.after(0, lambda: self.send_button.configure(state=tk.NORMAL))
            self.root.after(0, self._drain_pending_voice_prompt)

    def _run_slow_turn(self, user_prompt: str, fast_decision: FastPathDecision | None = None, *, source: str = "text_input") -> None:
        t0 = time.perf_counter()
        try:
            self.llm_inflight = True
            # 一次完整回合：可能先发生多次工具调用，最后生成 reply/overlay
            reply, overlay, tool_events, async_jobs = self._run_llm_with_tools(user_prompt, fast_decision=fast_decision)
            bg_updates = self._drain_bg_summaries()
            final_reply = reply
            if async_jobs:
                running_text = "、".join(async_jobs[:3])
                final_reply += f"\n已在后台持续执行：{running_text}。"
            if bg_updates:
                final_reply += "\n后台结果简报：" + " ".join(bg_updates[:2])

            self.state.append("assistant", final_reply)
            self._send_overlay_to_plugin(overlay)
            self.last_llm_ok_at = time.time()
            self.last_llm_error = None
            self.last_llm_latency_ms = int((time.perf_counter() - t0) * 1000)

            if tool_events:
                self._log_terminal("[tool_events] " + " | ".join(tool_events))
            show_final = self._show_backend_agent_reply or (source == "voice_input" and self._show_backend_reply_for_voice_tasks)
            if show_final:
                role = "Backend" if source == "voice_input" else "Agent"
                self.root.after(0, lambda role=role: self._append_chat(role, final_reply))
            self.root.after(0, lambda: self._set_status(f"Plugin summary sent: {overlay}"))
            self.voice.transition(VoiceEventType.AGENT_SPEAKING, {"source": "slow_path"})
            if source == "voice_input":
                self.root.after(0, lambda text=final_reply: self._speak_backend_result(text))
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            tb = traceback.format_exc()
            self._log_terminal(f"[LLM error] {err}\n{tb}")
            self.last_llm_error = err
            if self._show_backend_agent_reply:
                self.root.after(0, lambda: self._append_chat("Agent", "模型响应失败，请重试。"))
            self.root.after(0, lambda: self._set_status("Failed"))
            self.voice.transition(VoiceEventType.SILENCE_TIMEOUT, {"reason": "slow_path_error"})
        finally:
            self.llm_inflight = False
            self.root.after(0, lambda: self.send_button.configure(state=tk.NORMAL))

    def _run_llm_with_tools(self, user_prompt: str, *, fast_decision: FastPathDecision | None = None) -> tuple[str, str, list[str], list[str]]:
        messages = self._build_messages(user_prompt, fast_decision=fast_decision)
        tool_events: list[str] = []
        async_jobs: list[str] = []
        dedup_cache: dict[str, dict[str, Any]] = {}

        # 限制工具循环轮数，避免模型异常时无限 tool-call
        for _ in range(4):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=AgentToolBridge.tool_schemas(),
                tool_choice="auto",
                temperature=0.2,
                max_tokens=700,
            )
            choice = response.choices[0]
            msg = choice.message
            content = msg.content or ""
            tool_calls = msg.tool_calls or []

            if not tool_calls:
                self._log_terminal(f"[LLM raw] {content}")
                reply, overlay = parse_agent_payload(content)
                return reply, overlay, tool_events, async_jobs

            assistant_tool_calls: list[dict[str, Any]] = []
            tool_results: list[tuple[str, dict[str, Any]]] = []
            for call in tool_calls:
                call_id = call.id
                name = call.function.name
                arg_text = call.function.arguments or "{}"
                try:
                    args = json.loads(arg_text)
                except Exception:
                    args = {}

                exec_name, exec_args, rewrite_note = self._rewrite_tool_call_by_intent(user_prompt, name, args)
                cache_key = ""
                if exec_name in {"set_target_pitch_deg", "turn_to_heading"}:
                    cache_key = f"{exec_name}:{json.dumps(exec_args, sort_keys=True, ensure_ascii=False)}"
                if cache_key and cache_key in dedup_cache:
                    result = dict(dedup_cache[cache_key])
                    result["deduped"] = True
                else:
                    if exec_name in {"set_target_pitch_deg", "turn_to_heading"}:
                        result = self.tools.execute_async(exec_name, exec_args)
                    else:
                        result = self.tools.execute(exec_name, exec_args)
                    if cache_key:
                        dedup_cache[cache_key] = dict(result)

                if rewrite_note is not None:
                    result = {
                        **result,
                        "rewritten_from_tool": name,
                        "rewritten_to_tool": exec_name,
                        "rewritten_to_args": exec_args,
                    }
                if result.get("mode") == "async" and result.get("accepted"):
                    async_jobs.append(f"{exec_name}(job={result.get('job_id')})")
                ok = bool(result.get("ok"))
                label = f"{name}->{exec_name}" if exec_name != name else name
                tool_events.append(f"Tool {label}: {'OK' if ok else 'FAILED'}")
                if rewrite_note is not None:
                    self._log_terminal(f"[tool_rewrite] {rewrite_note}; from={name} args={args} to={exec_name} args={exec_args}")
                self._log_terminal(f"[tool] {label} args={exec_args if exec_name != name else args} result={result}")

                assistant_tool_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": arg_text,
                        },
                    }
                )
                tool_results.append((call_id, result))

            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": assistant_tool_calls,
                }
            )
            for call_id, result in tool_results:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

        raise RuntimeError("Tool-call loop exceeded max rounds without final assistant JSON output.")

    def _send_overlay_to_plugin(self, overlay: str) -> None:
        msg = overlay.replace("\r", " ").replace("\n", " ").strip()
        wire = f"AGENT|{msg}"[:380]
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(wire.encode("utf-8"), (self.plugin_host, self.plugin_port))
            self.last_plugin_ok_at = time.time()
            self.last_plugin_error = None
        except Exception as exc:
            self.last_plugin_error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            sock.close()

    def _schedule_health_refresh(self) -> None:
        self._refresh_health()
        self.root.after(1200, self._schedule_health_refresh)

    def _refresh_health(self) -> None:
        now = time.time()

        latest = self.monitor.get_latest()
        xpc_err = self.monitor.get_last_error()
        if latest is not None:
            self._set_health_item("xpc", level="ok", text="XPC: Connected")
        elif xpc_err:
            self._set_health_item("xpc", level="err", text=f"XPC: {xpc_err}")
        else:
            self._set_health_item("xpc", level="warn", text="XPC: Waiting samples")

        if self.last_plugin_error:
            self._set_health_item("plugin", level="err", text=f"Plugin: {self.last_plugin_error}")
        elif self.last_plugin_ok_at is None:
            self._set_health_item("plugin", level="warn", text="Plugin: No message sent yet")
        else:
            age = int(now - self.last_plugin_ok_at)
            level = "ok" if age <= 20 else "warn"
            self._set_health_item("plugin", level=level, text=f"Plugin: Sent {age}s ago")

        if self.llm_inflight:
            self._set_health_item("llm", level="busy", text="LLM: Requesting")
        elif self.proactive_inflight:
            self._set_health_item("llm", level="busy", text="LLM: Proactive alerting")
        elif self.last_llm_error:
            self._set_health_item("llm", level="err", text=f"LLM: {self.last_llm_error}")
        elif self.last_llm_ok_at is not None:
            age = int(now - self.last_llm_ok_at)
            latency = f"{self.last_llm_latency_ms}ms" if self.last_llm_latency_ms is not None else "-"
            level = "ok" if age <= 60 else "warn"
            self._set_health_item("llm", level=level, text=f"LLM: OK {latency}, {age}s ago")
        else:
            self._set_health_item("llm", level="warn", text="LLM: Idle")

        voice_state = str(self.voice.snapshot().get("state") or "idle")
        if self.voice_config.enabled and self.voice_ui_running:
            self._set_health_item("voice_gateway", level="ok", text=f"Voice Gateway: {voice_state}")
        elif self.voice_config.enabled:
            self._set_health_item("voice_gateway", level="warn", text="Voice Gateway: Stopped")
        else:
            self._set_health_item("voice_gateway", level="warn", text="Voice Gateway: Disabled")

        backend_busy = self.fast_inflight or self.llm_inflight
        self._set_health_item(
            "backend",
            level="busy" if backend_busy else "ok",
            text="Backend Running: Yes" if backend_busy else "Backend Running: Idle",
        )
        tts_active = voice_state == "agent_speaking"
        self._set_health_item(
            "tts",
            level="busy" if tts_active else "ok",
            text="TTS Playback: Active" if tts_active else "TTS Playback: Idle",
        )

        base_status = self.status_var.get().split(" | Voice:")[0]
        if self.fast_inflight:
            base_status = "Fast path running..."
        if self.voice_config.enabled:
            strategy = self._build_strategy_context()
            self.status_var.set(
                f"{base_status} | Voice Gateway: {voice_state}"
                + f" | Strategy: v{strategy['version']}"
            )
            self._sync_voice_transcript_to_chat()
        else:
            self.status_var.set(base_status)

    def run(self) -> None:
        self._append_chat("Agent", "Co-Pilot 已就绪，请输入指令。")
        if self.voice_config.enabled:
            self.voice_ui_running = self.voice_session is not None
            if self.voice_ui_running:
                self.voice_button.configure(text="Voice Stop")
            else:
                self.voice_button.configure(text="Voice Start")
        self.root.mainloop()

    def _on_close(self) -> None:
        try:
            self._stop_background.set()
            self._stop_strategy.set()
            if self.voice_session is not None:
                self.voice_session.stop()
            self.proactive_watchdog.stop()
            self.monitor.stop()
        finally:
            self.root.destroy()

    def _run_strategy_worker(self) -> None:
        interval_ms = 1500
        while not self._stop_strategy.is_set():
            try:
                state_context = self._build_state_context()
                delta = {
                    "language": "zh",
                    "phase": state_context.get("phase"),
                    "risk_level": "high" if state_context.get("risks") else "normal",
                    "style": "concise_operational",
                    "policy": "voice_no_autonomous_action",
                }
                with self._strategy_lock:
                    if delta != self._strategy_delta:
                        self._strategy_delta = delta
                        self._strategy_version += 1
                        self._strategy_updated_at = time.time()
            except Exception as exc:
                self._log_terminal(f"[strategy_worker] {type(exc).__name__}: {exc}")
            self._stop_strategy.wait(interval_ms / 1000.0)

    def _drain_pending_voice_prompt(self) -> None:
        if self.fast_inflight or self.llm_inflight:
            return
        try:
            prompt = self._pending_voice_prompts.get_nowait()
        except queue.Empty:
            return
        self._log_terminal(f"[voice_auto_submit] drain queued prompt: {prompt}")
        self._submit_user_prompt(prompt, source="voice_input")

    def _sync_voice_transcript_to_chat(self) -> None:
        snap = self.voice.snapshot()
        partial = str(snap.get("last_partial_text") or "").strip()
        final = str(snap.get("last_final_text") or "").strip()
        partial_role = str(snap.get("last_partial_role") or "").strip().lower()
        final_role = str(snap.get("last_final_role") or "").strip().lower()
        if partial and partial != self._voice_partial_rendered:
            self._voice_partial_rendered = partial
            if partial_role == "agent":
                self._voice_agent_partial_last = partial
                self._voice_agent_partial_buffer = partial
                self._set_status(f"TTS Playback: {partial[:60]}")
            else:
                self._voice_pilot_partial_last = partial
                self._set_status(f"Listening: {partial[:60]}")
                if self._should_emit_partial(self._voice_pilot_partial_last_emit, partial):
                    self._voice_pilot_partial_last_emit = partial
                    self._append_chat("Pilot [voice]", f"[voice~] {partial}")
        if final and (final != self._voice_final_seen or final_role != self._voice_final_seen_role):
            self._voice_final_seen = final
            self._voice_final_seen_role = final_role
            if final_role == "agent":
                if self._is_meaningful_agent_voice_text(final):
                    self._log_terminal(f"[voice_tts_transcript] {final}")
                    self._voice_agent_partial_flushed = final
                    self._voice_agent_partial_buffer = ""
            else:
                submitted = self._maybe_submit_voice_final(final)
                if not submitted:
                    self._append_chat("Pilot [voice]", f"[voice] {final}")
        self._update_voice_text_capability_hint()
        self._flush_agent_voice_buffer_if_needed()

    def _submit_user_prompt(self, prompt: str, *, source: str) -> None:
        clean = (prompt or "").strip()
        if not clean:
            return
        self.voice.transition(VoiceEventType.USER_SPEAKING, {"source": source})
        if source == "text_input":
            self.input_text.delete("1.0", tk.END)
        self._append_chat("Pilot [voice]" if source == "voice_input" else "Pilot", clean)
        self.state.append("user", clean)
        if source == "voice_input":
            self._set_status("Processing voice command via backend agent...")
            self._log_terminal(f"[voice_backend_dispatch] {clean}")
        else:
            self._set_status("Processing...")
        self.send_button.configure(state=tk.DISABLED)
        threading.Thread(target=self._run_fast_then_slow_turn, args=(clean, source), daemon=True).start()

    def _maybe_submit_voice_final(self, final_text: str) -> bool:
        """Dispatch one pilot ASR final to backend or gateway speech."""
        if not self._voice_auto_submit_enabled:
            return False
        text = (final_text or "").strip()
        if not text:
            return False
        decision = self._classify_voice_final(text)
        self._log_terminal(
            "[voice_intent] "
            + json.dumps(asdict(decision), ensure_ascii=False)
        )
        if decision.intent == "smalltalk":
            self._allow_gateway_tts(True, ttl_s=8.0)
            self._log_terminal(f"[voice_auto_submit] bypass backend: {text}")
            return False
        if decision.intent == "unclear":
            self._allow_gateway_tts(True, ttl_s=10.0)
            self._log_terminal(f"[voice_auto_submit] unclear, keep in voice gateway: {text}")
            return False
        self._allow_gateway_tts(False, ttl_s=0.5)
        if text == self._voice_pilot_final_last_handled:
            return False
        if self.fast_inflight or self.llm_inflight:
            try:
                self._pending_voice_prompts.put_nowait(text)
                self._log_terminal(f"[voice_auto_submit] queued while busy: {text}")
                self._voice_pilot_final_last_handled = text
                return True
            except queue.Full:
                self._log_terminal(f"[voice_auto_submit] queue full, dropped: {text}")
                return False
        self._voice_pilot_final_last_handled = text
        self._log_terminal(f"[voice_auto_submit] submit: {text}")
        self._submit_user_prompt(text, source="voice_input")
        return True

    def _classify_voice_final(self, text: str) -> VoiceIntentDecision:
        """Classify ASR final with deterministic rules first and optional LLM fallback."""
        rule_decision = self._classify_voice_final_by_rule(text)
        if (
            self._voice_intent_llm_fallback
            and rule_decision.confidence < self._voice_intent_llm_threshold
            and self.client is not None
        ):
            llm_decision = self._classify_voice_final_by_llm(text)
            if llm_decision is not None:
                if llm_decision.intent == "flight_task" or llm_decision.confidence >= self._voice_intent_llm_threshold:
                    return llm_decision
        return rule_decision

    def _classify_voice_final_by_rule(self, text: str) -> VoiceIntentDecision:
        t = (text or "").strip().lower()
        if not t:
            return VoiceIntentDecision("unclear", 1.0, "empty_text")
        if self._voice_agent_call_mode == "always":
            return VoiceIntentDecision("flight_task", 1.0, "VOICE_AGENT_CALL_MODE=always")
        if self._voice_agent_call_mode == "never":
            return VoiceIntentDecision("smalltalk", 1.0, "VOICE_AGENT_CALL_MODE=never")
        if self._should_dispatch_voice_to_backend(t):
            return VoiceIntentDecision("flight_task", 0.95, "flight_keyword")
        smalltalk_exact = {
            "你是谁",
            "你是什么",
            "你叫什么",
            "hello",
            "hi",
            "你好",
            "谢谢",
            "辛苦了",
        }
        if t in smalltalk_exact:
            return VoiceIntentDecision("smalltalk", 0.95, "smalltalk_exact")
        if len(t) <= 2:
            return VoiceIntentDecision("unclear", 0.85, "too_short")
        if any(token in t for token in ("飞机", "飞行", "x-plane", "xplane", "副驾", "驾驶", "机场", "跑道")):
            return VoiceIntentDecision("flight_task", 0.75, "aviation_context")
        if any(token in t for token in ("什么", "怎么", "能不能", "可以", "帮我", "请你")):
            return VoiceIntentDecision("unclear", 0.45, "ambiguous_request")
        return VoiceIntentDecision("smalltalk", 0.7, "default_non_flight")

    def _classify_voice_final_by_llm(self, text: str) -> VoiceIntentDecision | None:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Classify one ASR final utterance for an X-Plane voice gateway. "
                            "Return only JSON with intent, confidence, reason. "
                            "intent must be one of flight_task, smalltalk, unclear. "
                            "Use flight_task for aircraft state, control, risk, tool, navigation, flight operation, or simulator status. "
                            "Use unclear for incomplete commands or missing target/action. Prefer flight_task when safety-relevant."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                temperature=0.0,
                max_tokens=80,
            )
            content = response.choices[0].message.content or ""
            data = json.loads(content[content.find("{") : content.rfind("}") + 1])
            intent = str(data.get("intent") or "").strip()
            if intent not in {"flight_task", "smalltalk", "unclear"}:
                return None
            confidence = float(data.get("confidence", 0.0))
            confidence = max(0.0, min(1.0, confidence))
            reason = str(data.get("reason") or "llm_classifier").strip()
            return VoiceIntentDecision(intent, confidence, reason, "llm")
        except Exception as exc:
            self._log_terminal(f"[voice_intent_llm] failed: {type(exc).__name__}: {exc}")
            return None

    def _should_dispatch_voice_to_backend(self, text: str) -> bool:
        t = (text or "").strip().lower()
        if not t:
            return False
        keywords = (
            "航向",
            "高度",
            "速度",
            "油门",
            "襟翼",
            "起落架",
            "俯仰",
            "滚转",
            "舵",
            "爬升",
            "下降",
            "转向",
            "设置",
            "调整",
            "执行",
            "检查",
            "读取",
            "状态",
            "风险",
            "告警",
            "接管",
            "自动驾驶",
            "heading",
            "altitude",
            "speed",
            "throttle",
            "flaps",
            "gear",
            "turn",
            "climb",
            "descend",
            "set",
            "check",
            "read",
            "status",
        )
        return any(k in t for k in keywords)

    def _allow_gateway_tts(self, allowed: bool, *, ttl_s: float) -> None:
        session = self.voice_session
        if session is None:
            return
        try:
            session.allow_gateway_tts(allowed, ttl_s=ttl_s)
        except Exception as exc:
            self._log_terminal(f"[voice_gateway_tts_policy] failed: {type(exc).__name__}: {exc}")

    def _speak_backend_result(self, text: str) -> None:
        """Convert backend result into pilot-friendly speech and send ChatTTSText."""
        playback_text = self._build_voice_playback_text(text)
        if not playback_text:
            return
        if playback_text != self._voice_playback_text_last:
            self._append_chat("Voice", f"[playback] {playback_text}")
            self._voice_playback_text_last = playback_text
        session = self.voice_session
        spoken = False
        if session is not None:
            try:
                spoken = bool(session.speak_text(playback_text))
            except Exception as exc:
                self._log_terminal(f"[voice_tts] ChatTTSText failed: {type(exc).__name__}: {exc}")
        if spoken:
            self._set_status("TTS Playback: backend result queued")
        else:
            self._set_status("TTS Playback unavailable; backend result shown in UI")

    @staticmethod
    def _build_voice_playback_text(text: str) -> str:
        """Sanitize backend text for speech without changing auditable Backend UI text."""
        clean = re.sub(r"\s+", " ", (text or "").strip())
        if not clean:
            return ""
        clean = re.sub(r"\boverlay\s*[:：]\s*[^。！？.!?]*(?:[。！？.!?]|$)", "", clean, flags=re.IGNORECASE).strip()
        clean = re.sub(r"已在后台持续执行\s*[:：]\s*[^。！？.!?]*(?:[。！？.!?]|$)", "后台控制已接收，正在持续执行。", clean).strip()
        clean = re.sub(r"\b[a-zA-Z_]+\s*\(job=[^)]+\)", "后台任务", clean)
        clean = re.sub(r"\bjob=[0-9a-fA-F-]+\b", "后台任务", clean)
        clean = ExternalAgentChatApp._round_long_numbers_for_voice(clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        state_summary = ExternalAgentChatApp._build_state_voice_summary(clean)
        if state_summary:
            return state_summary
        if len(clean) <= 220:
            return clean
        sentence = re.split(r"(?<=[。！？.!?])", clean, maxsplit=1)[0].strip()
        if 20 <= len(sentence) <= 220:
            return sentence
        return clean[:217].rstrip() + "..."

    @staticmethod
    def _round_long_numbers_for_voice(text: str) -> str:
        def repl(match: re.Match[str]) -> str:
            raw = match.group(0)
            try:
                value = float(raw)
            except Exception:
                return raw
            return ExternalAgentChatApp._format_spoken_number(value, decimals=2)

        return re.sub(r"-?\d+\.\d+", repl, text)

    @staticmethod
    def _format_spoken_number(value: Any, *, decimals: int = 2) -> str:
        if value is None:
            return "-"
        try:
            number = float(value)
        except Exception:
            return str(value)
        if decimals <= 0:
            return str(int(round(number)))
        text = f"{number:.{decimals}f}"
        return text.rstrip("0").rstrip(".")

    @staticmethod
    def _build_state_voice_summary(text: str) -> str:
        if "阶段" not in text or "空速" not in text or "高度" not in text:
            return ""
        phase = ExternalAgentChatApp._extract_labeled_value(text, "阶段")
        speed = ExternalAgentChatApp._extract_labeled_number(text, "空速")
        altitude = ExternalAgentChatApp._extract_labeled_number(text, "高度")
        risk = ExternalAgentChatApp._extract_labeled_value(text, "风险")
        parts: list[str] = []
        if phase:
            parts.append(f"当前{ExternalAgentChatApp._voice_phase_label(phase)}")
        if speed is not None:
            parts.append(f"空速约{speed:.0f}节")
        if altitude is not None:
            parts.append(f"高度约{altitude:.0f}英尺")
        if risk:
            if "无" in risk and "风险" in risk:
                parts.append("未见明显风险")
            else:
                parts.append(f"风险提示：{risk}")
        if not parts:
            return ""
        return "，".join(parts) + "。"

    @staticmethod
    def _extract_labeled_value(text: str, label: str) -> str:
        match = re.search(rf"{re.escape(label)}\s*[:：]\s*([^|，。,;；]+)", text)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _extract_labeled_number(text: str, label: str) -> float | None:
        match = re.search(rf"{re.escape(label)}\s*[:：]\s*(-?\d+(?:\.\d+)?)", text)
        if not match:
            return None
        try:
            return float(match.group(1))
        except Exception:
            return None

    @staticmethod
    def _voice_phase_label(phase: str) -> str:
        phase = phase.strip()
        labels = {
            "cruise": "处于巡航阶段",
            "takeoff_roll": "处于起飞滑跑阶段",
            "climb": "处于爬升阶段",
            "approach": "处于进近阶段",
            "landing_roll": "处于着陆滑跑阶段",
            "ground_hold": "在地面等待",
        }
        return labels.get(phase, f"阶段为{phase}")

    @staticmethod
    def _should_emit_partial(last: str, current: str) -> bool:
        if not current:
            return False
        if not last:
            return len(current) >= 6
        if current == last:
            return False
        if len(current) - len(last) >= 8:
            return True
        if current.endswith(("。", "！", "？", ".", "!", "?")):
            return True
        return False

    def _flush_agent_voice_buffer_if_needed(self) -> None:
        # If server does not provide explicit agent final text, keep one readable
        # transcript line in UI for post-hoc review when speech segment ends.
        if not self._voice_agent_partial_buffer:
            return
        state = str(self.voice.snapshot().get("state") or "")
        if state == "agent_speaking":
            return
        text = self._voice_agent_partial_buffer.strip()
        if not text:
            return
        if not self._is_meaningful_agent_voice_text(text):
            self._voice_agent_partial_buffer = ""
            return
        if text == self._voice_agent_partial_flushed:
            self._voice_agent_partial_buffer = ""
            return
        self._voice_agent_partial_flushed = text
        self._voice_agent_partial_buffer = ""
        self._log_terminal(f"[voice_tts_transcript_flush] {text}")

    def _update_voice_text_capability_hint(self) -> None:
        # If model keeps speaking but transcript remains tiny/noisy, show explicit hint.
        snap = self.voice.snapshot()
        state = str(snap.get("state") or "")
        if state != "agent_speaking":
            return
        text = str(snap.get("last_partial_text") or "").strip()
        if len(text) <= 1:
            if self._voice_transcript_from_model_capable:
                self._voice_transcript_from_model_capable = False
                self._set_status("Agent speaking (audio-only or weak text stream)")
        else:
            self._voice_transcript_from_model_capable = True

    @staticmethod
    def _is_meaningful_agent_voice_text(text: str) -> bool:
        t = (text or "").strip()
        if len(t) < 2:
            return False
        if t in {"。", "，", ".", ",", "嗯", "啊"}:
            return False
        zh_count = sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff")
        an_count = sum(1 for ch in t if ch.isalnum())
        return zh_count >= 1 or an_count >= 2


def main() -> int:
    load_env_files()
    app = ExternalAgentChatApp()
    app.run()
    return 0


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_env_files() -> None:
    here = Path(__file__).resolve().parent
    candidates = [
        here / ".env",
        here / "xplane_agent_chat_plugin" / ".env",
    ]
    for path in candidates:
        _load_dotenv(path)



if __name__ == "__main__":
    raise SystemExit(main())
