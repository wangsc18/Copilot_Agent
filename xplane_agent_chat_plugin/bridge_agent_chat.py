from __future__ import annotations

import argparse
import os
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List


def load_dotenv_file(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def sanitize_wire_text(text: str, *, max_chars: int = 500) -> str:
    sanitized = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    sanitized = "".join(ch for ch in sanitized if ord(ch) >= 32 or ch == " ")
    return sanitized.strip()[:max_chars]


@dataclass
class SessionState:
    history: List[dict] = field(default_factory=list)
    max_history_pairs: int = 10

    def append_turn(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})
        max_items = self.max_history_pairs * 2
        if len(self.history) > max_items:
            self.history = self.history[-max_items:]


class AgentResponder:
    def __init__(self, model: str, api_key: str | None, base_url: str | None):
        self.model = model
        self.client = None
        resolved_key = api_key or os.getenv("OPENAI_API_KEY")
        resolved_base = base_url or os.getenv("OPENAI_BASE_URL")
        if resolved_key:
            from openai import OpenAI

            kwargs = {"api_key": resolved_key}
            if resolved_base:
                kwargs["base_url"] = resolved_base
            self.client = OpenAI(**kwargs)

    def reply(self, text: str, state: SessionState) -> str:
        user_text = sanitize_wire_text(text)
        if not user_text:
            return "Empty message received."

        if self.client is None:
            return "OPENAI_API_KEY missing. Bridge is online but LLM is disabled."

        system_prompt = (
            "You are a concise co-pilot assistant inside X-Plane 11. "
            "Reply in the same language as the pilot's message. "
            "If the pilot writes Chinese, reply in Chinese. "
            "Give practical cockpit advice in one short sentence."
        )
        input_messages = [{"role": "system", "content": system_prompt}]
        input_messages.extend(state.history)
        input_messages.append({"role": "user", "content": user_text})

        response = self.client.responses.create(
            model=self.model,
            input=input_messages,
            temperature=0.2,
            max_output_tokens=120,
        )
        content = getattr(response, "output_text", "") or ""
        content = sanitize_wire_text(content, max_chars=500)
        if not content:
            content = "No response text from model."
        state.append_turn("user", user_text)
        state.append_turn("assistant", content)
        return content


def parse_wire_message(packet: bytes) -> tuple[str, str]:
    text = packet.decode("utf-8", errors="ignore").strip()
    if "|" not in text:
        return "", ""
    kind, payload = text.split("|", 1)
    return kind.strip().upper(), sanitize_wire_text(payload)


def run_bridge(
    *,
    bind_host: str,
    listen_port: int,
    plugin_host: str,
    plugin_port: int,
    model: str,
    api_key: str | None,
    base_url: str | None,
    timeout_s: float,
) -> None:
    responder = AgentResponder(model=model, api_key=api_key, base_url=base_url)
    sessions: Dict[str, SessionState] = {}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((bind_host, listen_port))
    sock.settimeout(timeout_s)

    print(
        f"Bridge listening on {bind_host}:{listen_port}, "
        f"sending replies to {plugin_host}:{plugin_port}, model={model}"
    )

    target = (plugin_host, plugin_port)
    while True:
        try:
            payload, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue

        kind, content = parse_wire_message(payload)
        if kind != "PILOT" or not content:
            sock.sendto(b"SYSTEM|Ignored unknown packet.", target)
            continue

        key = f"{addr[0]}:{addr[1]}"
        state = sessions.setdefault(key, SessionState())
        try:
            answer = responder.reply(content, state)
            wire = f"AGENT|{answer}"
        except Exception as exc:
            wire = f"SYSTEM|Agent error: {type(exc).__name__}: {exc}"
        sock.sendto(wire.encode("utf-8"), target)
        print(
            f"[{time.strftime('%H:%M:%S')}] pilot={content!r} "
            f"-> {wire[:120]!r}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="X-Plane pilot-agent chat UDP bridge.")
    parser.add_argument("--bind-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=49121)
    parser.add_argument("--plugin-host", default="127.0.0.1")
    parser.add_argument("--plugin-port", type=int, default=49120)
    parser.add_argument("--timeout-s", type=float, default=0.1)
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--base-url", default=None)
    return parser.parse_args()


def main() -> int:
    load_dotenv_file(Path(__file__).resolve().parent / ".env")
    args = parse_args()
    run_bridge(
        bind_host=args.bind_host,
        listen_port=args.listen_port,
        plugin_host=args.plugin_host,
        plugin_port=args.plugin_port,
        model=args.model,
        api_key=args.api_key,
        base_url=args.base_url,
        timeout_s=max(0.01, args.timeout_s),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
