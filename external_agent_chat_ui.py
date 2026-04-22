from __future__ import annotations

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

        self._init_styles()
        self._build_ui()
        self._schedule_health_refresh()

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

        bottom_row = ttk.Frame(composer_card, style="Card.TFrame")
        bottom_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(bottom_row, text="Enter to send, Shift+Enter for newline", style="InputHint.TLabel").pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="Ready")
        self.status_label = ttk.Label(bottom_row, textvariable=self.status_var, style="Status.TLabel")
        self.status_label.pack(side=tk.RIGHT)

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

        if role_key == "pilot":
            anchor = "e"
            role_fg = "#1E40AF"
            bubble_bg = "#1D4ED8"
            bubble_fg = "#FFFFFF"
            justify = tk.RIGHT
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
            target = job.result.get("target_pitch_deg")
            final = job.result.get("final_pitch_deg")
            if ok:
                return f"后台执行完成：俯仰目标 {target}° 已达到，当前约 {final}°。"
            return f"后台执行结束：俯仰目标 {target}° 未在时限内收敛，当前约 {final}°。"
        if job.tool_name == "turn_to_heading":
            target = job.result.get("target_heading_deg")
            final = job.result.get("final_heading_deg")
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
            "请默认使用中文回复。\n"
            "只返回 JSON，且必须包含 reply 与 overlay 两个字段。\n"
            "reply：简洁告警 + 当前建议动作。\n"
            "overlay：一句简短中文告警（最多90字符）。"
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
        self.input_text.delete("1.0", tk.END)
        self._append_chat("Pilot", prompt)
        self.state.append("user", prompt)
        self._set_status("Processing...")
        self.send_button.configure(state=tk.DISABLED)
        threading.Thread(target=self._run_fast_then_slow_turn, args=(prompt,), daemon=True).start()

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

    def _build_messages(self, user_prompt: str, *, fast_decision: FastPathDecision | None = None) -> list[dict[str, Any]]:
        # 构建首轮消息：系统约束 + 状态上下文 + 历史对话 + 当前输入
        state_context = self._build_state_context()
        system = (
            "You are an aviation co-pilot assistant for X-Plane 11.\n"
            "You can use tools to read or control aircraft state.\n"
            "Always call get_flight_state first before control tools when safety context is unclear.\n"
            "Default reply language is Chinese unless the user explicitly asks another language.\n"
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

        if name == "set_pitch_cmd" and target_pitch is not None:
            return "set_target_pitch_deg", {"value": float(target_pitch)}, "raw pitch input rewritten to target pitch tool"
        if name in {"set_roll_cmd", "set_rudder_cmd"} and target_heading is not None:
            return "turn_to_heading", {"heading_deg": float(target_heading)}, "raw heading input rewritten to target heading tool"
        return name, args, None

    def _run_fast_then_slow_turn(self, user_prompt: str) -> None:
        t0 = time.perf_counter()
        fast_decision: FastPathDecision | None = None
        try:
            self.fast_inflight = True
            fast_decision = self.fast_router.route(user_prompt)
            if fast_decision is not None:
                self.root.after(0, lambda: self._append_chat("Agent", fast_decision.immediate_reply))
                self.root.after(0, lambda: self._set_status(fast_decision.overlay))
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
                self._run_slow_turn(user_prompt, fast_decision)
            else:
                self.last_llm_ok_at = time.time()
                self.last_llm_latency_ms = int((time.perf_counter() - t0) * 1000)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            tb = traceback.format_exc()
            self._log_terminal(f"[fast/slow error] {err}\n{tb}")
            self.last_llm_error = err
            self.root.after(0, lambda: self._append_chat("Agent", "处理失败，请稍后重试。"))
            self.root.after(0, lambda: self._set_status("Failed"))
        finally:
            self.fast_inflight = False
            self.root.after(0, lambda: self.send_button.configure(state=tk.NORMAL))

    def _run_slow_turn(self, user_prompt: str, fast_decision: FastPathDecision | None = None) -> None:
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
            self.root.after(0, lambda: self._append_chat("Agent", final_reply))
            self.root.after(0, lambda: self._set_status(f"Plugin summary sent: {overlay}"))
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            tb = traceback.format_exc()
            self._log_terminal(f"[LLM error] {err}\n{tb}")
            self.last_llm_error = err
            self.root.after(0, lambda: self._append_chat("Agent", "模型响应失败，请重试。"))
            self.root.after(0, lambda: self._set_status("Failed"))
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

        if self.fast_inflight:
            self.status_var.set("Fast path running...")

    def run(self) -> None:
        self._append_chat("Agent", "Co-Pilot 已就绪，请输入指令。")
        self.root.mainloop()

    def _on_close(self) -> None:
        try:
            self._stop_background.set()
            self.proactive_watchdog.stop()
            self.monitor.stop()
        finally:
            self.root.destroy()


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
