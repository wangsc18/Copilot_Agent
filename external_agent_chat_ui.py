from __future__ import annotations

import json
import os
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
from agent_core.copilot_situation import SituationInferenceEngine
from agent_core.copilot_state_monitor import FlightStateMonitor


def _extract_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("No JSON object found in model output.")
    return json.loads(text[start : end + 1])


def parse_agent_payload(text: str) -> tuple[str, str]:
    raw = (text or "").strip()
    if not raw:
        return "模型返回空内容。", "模型返回空内容。"

    try:
        data = _extract_json_object(raw)
        reply = str(data.get("reply", "")).strip()
        overlay = str(data.get("overlay", "")).strip()
        if not reply:
            raise ValueError("Missing non-empty 'reply' field.")
        if not overlay:
            overlay = reply[:90]
        return reply, overlay[:120]
    except Exception:
        # Fallback: accept plain text output to avoid breaking chat flow.
        reply = raw
        overlay = raw.replace("\n", " ").replace("\r", " ").strip()[:90]
        return reply, overlay


@dataclass
class ChatState:
    history: list[dict[str, str]] = field(default_factory=list)
    max_turns: int = 20

    def append(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})
        max_items = self.max_turns * 2
        if len(self.history) > max_items:
            self.history = self.history[-max_items:]


class AgentToolBridge:
    """将模型工具调用路由到状态读取与受控执行链路（Guard -> Executor）。"""

    def __init__(
        self,
        *,
        monitor: FlightStateMonitor,
        situation_engine: SituationInferenceEngine,
        guard: ActionGuard,
        executor: ActionExecutor,
    ) -> None:
        self.monitor = monitor
        self.situation_engine = situation_engine
        self.guard = guard
        self.executor = executor

    @staticmethod
    def tool_schemas() -> list[dict[str, Any]]:
        # 这里定义给 LLM 的“可调用工具契约”，名称与 execute() 分发保持一致
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_flight_state",
                    "description": "Read current aircraft state and inferred phase/risks from X-Plane monitor.",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "set_throttle",
                    "description": "Set throttle command in range [-1.0, 1.0]. Positive values increase thrust.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "value": {
                                "type": "number",
                                "minimum": -1.0,
                                "maximum": 1.0,
                                "description": "Throttle ratio in [-1,1].",
                            }
                        },
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "set_flaps",
                    "description": "Set flaps ratio in range [0.0, 1.0].",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "value": {
                                "type": "number",
                                "minimum": 0.0,
                                "maximum": 1.0,
                                "description": "Flaps ratio in [0,1].",
                            }
                        },
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "set_gear",
                    "description": "Set landing gear state.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "down": {
                                "type": "boolean",
                                "description": "true to extend gear, false to retract gear.",
                            }
                        },
                        "required": ["down"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "release_brakes",
                    "description": "Release parking and wheel brakes.",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                },
            },
        ]

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # 统一工具分发入口：读状态和写动作都返回结构化结果，供模型二次推理
        try:
            if name == "get_flight_state":
                return self._get_flight_state()
            if name == "set_throttle":
                value = float(arguments.get("value"))
                return self._exec_write_action(
                    ActionPlan(
                        requested_by="llm_tool",
                        mode=ControlMode.ASSISTED,
                        actions=[Action(type=ActionType.SET_THROTTLE, value=value, reason="llm_tool_call")],
                    )
                )
            if name == "set_flaps":
                value = float(arguments.get("value"))
                return self._exec_write_action(
                    ActionPlan(
                        requested_by="llm_tool",
                        mode=ControlMode.ASSISTED,
                        actions=[Action(type=ActionType.SET_FLAPS, value=value, reason="llm_tool_call")],
                    )
                )
            if name == "set_gear":
                down = bool(arguments.get("down"))
                return self._exec_write_action(
                    ActionPlan(
                        requested_by="llm_tool",
                        mode=ControlMode.ASSISTED,
                        actions=[Action(type=ActionType.SET_GEAR, value=1 if down else 0, reason="llm_tool_call")],
                    )
                )
            if name == "release_brakes":
                return self._exec_write_action(
                    ActionPlan(
                        requested_by="llm_tool",
                        mode=ControlMode.ASSISTED,
                        actions=[Action(type=ActionType.RELEASE_BRAKES, value=0, reason="llm_tool_call")],
                    )
                )
            return {"ok": False, "error": f"Unsupported tool: {name}"}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _get_flight_state(self) -> dict[str, Any]:
        # 将 monitor 原始快照 + 态势推断聚合成一个工具返回
        latest = self.monitor.get_latest()
        if latest is None:
            return {
                "ok": False,
                "error": self.monitor.get_last_error() or "no_samples",
                "state": None,
            }
        win10 = self.monitor.get_window(10.0)
        win30 = self.monitor.get_window(30.0)
        situation = self.situation_engine.infer(latest, win10, win30)
        return {
            "ok": True,
            "error": self.monitor.get_last_error(),
            "state": {
                "latest": asdict(latest),
                "phase": situation.phase,
                "confidence": situation.confidence,
                "evidence": situation.evidence,
                "risks": situation.risks,
            },
        }

    def _exec_write_action(self, plan: ActionPlan) -> dict[str, Any]:
        # 写操作必须依赖最新快照做 Guard 校验
        latest = self.monitor.get_latest()
        if latest is None:
            return {
                "ok": False,
                "error": "no_latest_snapshot_for_guard",
                "executed": [],
            }
        guard_result = self.guard.check(plan, latest)
        exec_result = self.executor.execute(plan, guard_result)
        return {
            "ok": exec_result.success,
            "guard_allowed": guard_result.allowed,
            "guard_violations": guard_result.violations,
            "executed": exec_result.executed,
            "error": exec_result.error,
        }


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
        self.executor = ActionExecutor(xp_host=xp_host, xp_port=xp_port, timeout_ms=1000)
        self.tools = AgentToolBridge(
            monitor=self.monitor,
            situation_engine=self.situation_engine,
            guard=self.guard,
            executor=self.executor,
        )
        self.monitor.start()

        self.last_plugin_ok_at: float | None = None
        self.last_plugin_error: str | None = None
        self.last_llm_ok_at: float | None = None
        self.last_llm_error: str | None = None
        self.last_llm_latency_ms: int | None = None
        self.llm_inflight = False
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

    def _on_send(self, event=None) -> None:
        prompt = self.input_text.get("1.0", tk.END).strip()
        if not prompt:
            return
        self.input_text.delete("1.0", tk.END)
        self._append_chat("Pilot", prompt)
        self.state.append("user", prompt)
        self._set_status("Thinking...")
        self.send_button.configure(state=tk.DISABLED)
        threading.Thread(target=self._run_agent_turn, args=(prompt,), daemon=True).start()

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

    def _build_messages(self, user_prompt: str) -> list[dict[str, Any]]:
        # 构建首轮消息：系统约束 + 状态上下文 + 历史对话 + 当前输入
        state_context = self._build_state_context()
        system = (
            "You are an aviation co-pilot assistant for X-Plane 11.\n"
            "You can use tools to read or control aircraft state.\n"
            "Always call get_flight_state first before control tools when safety context is unclear.\n"
            "After all needed tools are done, return JSON only with exactly two fields:\n"
            "reply: detailed response for external chat UI.\n"
            "overlay: one short sentence (max 90 chars) for in-sim overlay.\n"
            "Use the same language as user input.\n"
            "When giving advice, use this format: action + one-sentence reason.\n"
            "Never suggest takeoff actions when phase is not ground_hold or takeoff_roll."
        )
        msgs: list[dict[str, Any]] = [{"role": "system", "content": system}]
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

    def _run_agent_turn(self, user_prompt: str) -> None:
        t0 = time.perf_counter()
        try:
            self.llm_inflight = True
            # 一次完整回合：可能先发生多次工具调用，最后生成 reply/overlay
            reply, overlay, tool_events = self._run_llm_with_tools(user_prompt)
            self.state.append("assistant", reply)
            self._send_overlay_to_plugin(overlay)
            self.last_llm_ok_at = time.time()
            self.last_llm_error = None
            self.last_llm_latency_ms = int((time.perf_counter() - t0) * 1000)

            if tool_events:
                self.root.after(0, lambda: self._append_chat("System", "\n".join(tool_events)))
            self.root.after(0, lambda: self._append_chat("Agent", reply))
            self.root.after(0, lambda: self._set_status(f"Plugin summary sent: {overlay}"))
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            tb = traceback.format_exc()
            self._log_terminal(f"[LLM error] {err}\n{tb}")
            self.last_llm_error = err
            self.root.after(0, lambda: self._append_chat("System", f"Error: {type(exc).__name__}: {exc}"))
            self.root.after(0, lambda: self._set_status("Failed"))
        finally:
            self.llm_inflight = False
            self.root.after(0, lambda: self.send_button.configure(state=tk.NORMAL))

    def _run_llm_with_tools(self, user_prompt: str) -> tuple[str, str, list[str]]:
        messages = self._build_messages(user_prompt)
        tool_events: list[str] = []

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
                return reply, overlay, tool_events

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

                result = self.tools.execute(name, args)
                ok = bool(result.get("ok"))
                tool_events.append(f"Tool {name}: {'OK' if ok else 'FAILED'}")
                self._log_terminal(f"[tool] {name} args={args} result={result}")

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
        elif self.last_llm_error:
            self._set_health_item("llm", level="err", text=f"LLM: {self.last_llm_error}")
        elif self.last_llm_ok_at is not None:
            age = int(now - self.last_llm_ok_at)
            latency = f"{self.last_llm_latency_ms}ms" if self.last_llm_latency_ms is not None else "-"
            level = "ok" if age <= 60 else "warn"
            self._set_health_item("llm", level=level, text=f"LLM: OK {latency}, {age}s ago")
        else:
            self._set_health_item("llm", level="warn", text="LLM: Idle")

    def run(self) -> None:
        self._append_chat("System", "External chat is ready. Send a message to start.")
        self.root.mainloop()

    def _on_close(self) -> None:
        try:
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
