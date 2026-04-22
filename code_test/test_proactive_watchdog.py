import pathlib
import sys
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from agent_core.copilot_core import FlightSnapshot
from agent_core.proactive_watchdog import ProactiveWatchdog, ProactiveWatchdogConfig


def snap() -> FlightSnapshot:
    return FlightSnapshot(
        timestamp_s=time.time(),
        altitude_ft=1200.0,
        altitude_msl_m=365.0,
        airspeed_kts=45.0,
        ground_speed_kts=43.0,
        vertical_speed_fpm=-900.0,
        pitch_deg=3.0,
        roll_deg=30.0,
        heading_true_deg=90.0,
        gear_ratio=1.0,
        throttle_cmd=0.4,
        throttle_used_ratio=0.4,
        flaps_ratio=0.5,
        speedbrake_ratio=0.0,
        park_brake_ratio=0.0,
        left_brake_ratio=0.0,
        right_brake_ratio=0.0,
        radio_altitude_ft=250.0,
        on_ground_any=0.0,
    )


class StubMonitor:
    def __init__(self):
        self._latest = snap()

    def get_latest(self):
        return self._latest

    def get_window(self, seconds):
        return [self._latest, self._latest]


class StubSituation:
    phase = "approach"
    confidence = 0.85
    risks = ["stall_risk"]


class StubEngine:
    def infer(self, latest, win10, win30):
        return StubSituation()


class TestProactiveWatchdog(unittest.TestCase):
    def test_debounce_and_cooldown(self):
        events = []
        wd = ProactiveWatchdog(
            monitor=StubMonitor(),
            situation_engine=StubEngine(),
            on_event=lambda ev: events.append(ev),
            config=ProactiveWatchdogConfig(poll_hz=10.0, consecutive_hits=2, cooldown_seconds=0.5),
        )
        now = time.time()
        wd._tick(now)
        self.assertEqual(len(events), 0)
        wd._tick(now + 0.05)
        self.assertEqual(len(events), 1)
        wd._tick(now + 0.1)
        self.assertEqual(len(events), 1)
        wd._tick(now + 0.7)
        wd._tick(now + 0.75)
        self.assertEqual(len(events), 2)


if __name__ == "__main__":
    unittest.main()

