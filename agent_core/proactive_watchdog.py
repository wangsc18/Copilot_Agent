from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .copilot_situation import SituationInferenceEngine
from .copilot_state_monitor import FlightStateMonitor


@dataclass
class ProactiveEvent:
    event_type: str
    severity: str
    phase: str
    confidence: float
    risks: list[str]
    key_metrics: dict[str, float | None]
    triggered_at: float = field(default_factory=time.time)


@dataclass
class ProactiveWatchdogConfig:
    poll_hz: float = 2.0
    consecutive_hits: int = 3
    cooldown_seconds: float = 20.0
    enabled_risks: tuple[str, ...] = (
        "stall_risk",
        "overspeed_risk",
        "throttle_ineffective",
        "unstable_approach",
        "runway_excursion_risk",
    )


class ProactiveWatchdog:
    """Continuously watches inferred situation and emits debounced anomaly events."""

    def __init__(
        self,
        *,
        monitor: FlightStateMonitor,
        situation_engine: SituationInferenceEngine,
        on_event: Callable[[ProactiveEvent], None],
        config: ProactiveWatchdogConfig | None = None,
    ) -> None:
        self.monitor = monitor
        self.situation_engine = situation_engine
        self.on_event = on_event
        self.config = config or ProactiveWatchdogConfig()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._risk_hits: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="proactive_watchdog")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run_loop(self) -> None:
        period = 1.0 / max(self.config.poll_hz, 0.5)
        while not self._stop_event.is_set():
            tick = time.time()
            try:
                self._tick(tick)
            except Exception:
                # Watchdog must not break the main chat loop.
                pass
            time.sleep(max(period - (time.time() - tick), 0.0))

    def _tick(self, now_s: float) -> None:
        latest = self.monitor.get_latest()
        if latest is None:
            return
        win10 = self.monitor.get_window(10.0)
        win30 = self.monitor.get_window(30.0)
        situation = self.situation_engine.infer(latest, win10, win30)

        active = set(situation.risks) & set(self.config.enabled_risks)
        for risk in list(self._risk_hits.keys()):
            if risk not in active:
                self._risk_hits[risk] = 0

        for risk in active:
            self._risk_hits[risk] = self._risk_hits.get(risk, 0) + 1
            if self._risk_hits[risk] < self.config.consecutive_hits:
                continue
            cooldown_until = self._cooldown_until.get(risk, 0.0)
            if now_s < cooldown_until:
                continue
            event = ProactiveEvent(
                event_type=risk,
                severity=self._severity_of(risk),
                phase=situation.phase,
                confidence=situation.confidence,
                risks=list(situation.risks),
                key_metrics={
                    "airspeed_kts": latest.airspeed_kts,
                    "altitude_ft": latest.altitude_ft,
                    "vertical_speed_fpm": latest.vertical_speed_fpm,
                    "roll_deg": latest.roll_deg,
                    "pitch_deg": latest.pitch_deg,
                    "ground_speed_kts": latest.ground_speed_kts,
                },
            )
            self.on_event(event)
            self._cooldown_until[risk] = now_s + self.config.cooldown_seconds
            self._risk_hits[risk] = 0

    @staticmethod
    def _severity_of(risk: str) -> str:
        if risk in {"stall_risk", "runway_excursion_risk"}:
            return "high"
        if risk in {"overspeed_risk", "unstable_approach"}:
            return "medium"
        return "low"

