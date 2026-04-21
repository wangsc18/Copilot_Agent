from __future__ import annotations

import threading
import time
from collections import deque

import xpc

from .copilot_core import FlightSnapshot


class XPlaneStateReader:
    # 统一管理 DREF 常量，避免散落硬编码
    DREF_AIRSPEED = "sim/cockpit2/gauges/indicators/airspeed_kts_pilot"
    DREF_ALTITUDE = "sim/cockpit2/gauges/indicators/altitude_ft_pilot"
    DREF_VVI = "sim/cockpit2/gauges/indicators/vvi_fpm_pilot"
    DREF_GROUNDSPEED_MPS = "sim/flightmodel/position/groundspeed"
    DREF_THROTTLE_USED = "sim/flightmodel2/engines/throttle_used_ratio"
    DREF_PARK_BRAKE = "sim/flightmodel/controls/parkbrakel"
    DREF_LEFT_BRAKE = "sim/cockpit2/controls/left_brake_ratio"
    DREF_RIGHT_BRAKE = "sim/cockpit2/controls/right_brake_ratio"
    DREF_WIND_SPEED_KT = "sim/weather/wind_speed_kt[0]"
    DREF_WIND_DIR_DEG = "sim/weather/wind_direction_degt[0]"
    DREF_RADIO_ALT_FT = "sim/cockpit2/gauges/indicators/radio_altimeter_height_ft_pilot"
    DREF_ON_GROUND_ANY = "sim/flightmodel/failures/onground_any"

    def __init__(self, client: xpc.XPlaneConnect):
        self.client = client

    @staticmethod
    def _first(row: tuple[float, ...], default: float = 0.0) -> float:
        if row is None or len(row) == 0:
            return default
        return float(row[0])

    def _safe_get_dref(self, dref: str) -> float | None:
        # 个别 DREF 在不同机模/场景下可能不可用，读失败时返回 None
        try:
            return self._first(self.client.getDREF(dref), default=0.0)
        except Exception:
            return None

    def read(self) -> FlightSnapshot:
        # 单次采样：POSI + CTRL + 核心 DREF，组装成统一快照
        now_s = time.time()
        posi = self.client.getPOSI()
        ctrl = self.client.getCTRL()
        drefs = self.client.getDREFs(
            [
                self.DREF_AIRSPEED,
                self.DREF_ALTITUDE,
                self.DREF_VVI,
                self.DREF_GROUNDSPEED_MPS,
                self.DREF_THROTTLE_USED,
                self.DREF_PARK_BRAKE,
                self.DREF_LEFT_BRAKE,
                self.DREF_RIGHT_BRAKE,
            ]
        )
        ground_speed_kts = self._first(drefs[3]) * 1.943844
        return FlightSnapshot(
            timestamp_s=now_s,
            altitude_ft=self._first(drefs[1]),
            altitude_msl_m=float(posi[2]),
            airspeed_kts=self._first(drefs[0]),
            ground_speed_kts=ground_speed_kts,
            vertical_speed_fpm=self._first(drefs[2]),
            pitch_deg=float(posi[3]),
            roll_deg=float(posi[4]),
            heading_true_deg=float(posi[5]),
            gear_ratio=float(posi[6]),
            throttle_cmd=float(ctrl[3]),
            throttle_used_ratio=self._first(drefs[4]),
            flaps_ratio=float(ctrl[5]),
            speedbrake_ratio=float(ctrl[6]),
            park_brake_ratio=self._first(drefs[5]),
            left_brake_ratio=self._first(drefs[6]),
            right_brake_ratio=self._first(drefs[7]),
            wind_speed_kt=self._safe_get_dref(self.DREF_WIND_SPEED_KT),
            wind_direction_deg=self._safe_get_dref(self.DREF_WIND_DIR_DEG),
            radio_altitude_ft=self._safe_get_dref(self.DREF_RADIO_ALT_FT),
            on_ground_any=self._safe_get_dref(self.DREF_ON_GROUND_ANY),
        )


class FlightStateMonitor:
    def __init__(
        self,
        *,
        xp_host: str = "127.0.0.1",
        xp_port: int = 49009,
        sample_hz: float = 2.0,
        timeout_ms: int = 1000,
    ):
        self.xp_host = xp_host
        self.xp_port = xp_port
        self.sample_hz = max(sample_hz, 0.5)
        self.timeout_ms = timeout_ms
        self._samples: deque[FlightSnapshot] = deque()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="flight_state_monitor")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _trim_locked(self, now_s: float) -> None:
        # 仅保留近 30 秒窗口，满足态势推断并控制内存
        while self._samples and (now_s - self._samples[0].timestamp_s) > 30.0:
            self._samples.popleft()

    def _run_loop(self) -> None:
        # 持续采样线程：连接失败会记录 last_error 并按周期重试
        period = 1.0 / self.sample_hz
        while not self._stop_event.is_set():
            loop_start = time.time()
            try:
                with xpc.XPlaneConnect(
                    xpHost=self.xp_host,
                    xpPort=self.xp_port,
                    timeout=self.timeout_ms,
                ) as client:
                    reader = XPlaneStateReader(client)
                    while not self._stop_event.is_set():
                        tick = time.time()
                        snap = reader.read()
                        with self._lock:
                            self._samples.append(snap)
                            self._trim_locked(tick)
                            self._last_error = None
                        time.sleep(max(period - (time.time() - tick), 0.0))
            except Exception as exc:
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(max(period - (time.time() - loop_start), 0.0))

    def get_window(self, seconds: float) -> list[FlightSnapshot]:
        cutoff = time.time() - max(seconds, 0.0)
        with self._lock:
            return [s for s in self._samples if s.timestamp_s >= cutoff]

    def get_latest(self) -> FlightSnapshot | None:
        with self._lock:
            if not self._samples:
                return None
            return self._samples[-1]

    def get_last_error(self) -> str | None:
        with self._lock:
            return self._last_error
