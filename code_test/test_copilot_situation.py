import pathlib
import sys
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from agent_core.copilot_core import FlightSnapshot
from agent_core.copilot_situation import SituationInferenceEngine, compute_trend_metrics


def snap(
    *,
    t: float,
    alt_ft: float,
    ias: float,
    gs: float,
    vsi: float,
    thr_used: float,
    park_brake: float = 0.0,
    radio_alt: float | None = None,
    on_ground: float | None = None,
    gear: float = 1.0,
    roll: float = 0.0,
) -> FlightSnapshot:
    return FlightSnapshot(
        timestamp_s=t,
        altitude_ft=alt_ft,
        altitude_msl_m=alt_ft * 0.3048,
        airspeed_kts=ias,
        ground_speed_kts=gs,
        vertical_speed_fpm=vsi,
        pitch_deg=5.0,
        roll_deg=roll,
        heading_true_deg=90.0,
        gear_ratio=gear,
        throttle_cmd=thr_used,
        throttle_used_ratio=thr_used,
        flaps_ratio=0.5,
        speedbrake_ratio=0.0,
        park_brake_ratio=park_brake,
        left_brake_ratio=0.0,
        right_brake_ratio=0.0,
        wind_speed_kt=None,
        wind_direction_deg=None,
        radio_altitude_ft=radio_alt,
        on_ground_any=on_ground,
    )


class TestCopilotSituation(unittest.TestCase):
    def test_trend_computation(self):
        now = time.time()
        samples = [
            snap(t=now, alt_ft=1000, ias=40, gs=35, vsi=100, thr_used=0.6),
            snap(t=now + 10, alt_ft=1150, ias=60, gs=55, vsi=600, thr_used=0.8),
        ]
        trend = compute_trend_metrics(samples)
        self.assertIsNotNone(trend)
        assert trend is not None
        self.assertGreater(trend.delta_altitude_ft, 100)
        self.assertGreater(trend.delta_airspeed_kts, 10)

    def test_infer_ground_hold(self):
        now = time.time()
        win10 = [
            snap(t=now, alt_ft=500, ias=0, gs=0, vsi=0, thr_used=0.1, park_brake=1.0, on_ground=1.0, radio_alt=0.0),
            snap(t=now + 10, alt_ft=500, ias=1, gs=1, vsi=0, thr_used=0.1, park_brake=1.0, on_ground=1.0, radio_alt=0.0),
        ]
        engine = SituationInferenceEngine()
        s = engine.infer(win10[-1], win10, win10)
        self.assertEqual(s.phase, "ground_hold")
        self.assertGreater(s.confidence, 0.5)

    def test_infer_cruise_not_takeoff(self):
        now = time.time()
        win10 = [
            snap(t=now, alt_ft=800, ias=110, gs=108, vsi=50, thr_used=0.55, on_ground=0.0, radio_alt=800.0, gear=0.0),
            snap(t=now + 10, alt_ft=820, ias=112, gs=110, vsi=20, thr_used=0.55, on_ground=0.0, radio_alt=820.0, gear=0.0),
        ]
        win30 = win10
        engine = SituationInferenceEngine()
        s = engine.infer(win10[-1], win10, win30)
        self.assertNotIn(s.phase, ("takeoff_roll", "ground_hold"))
        self.assertIn(s.phase, ("cruise", "initial_climb", "approach"))

    def test_risk_detected(self):
        now = time.time()
        win10 = [
            snap(t=now, alt_ft=1200, ias=50, gs=48, vsi=-650, thr_used=0.4, on_ground=0.0, radio_alt=400.0, roll=30.0),
            snap(t=now + 10, alt_ft=1080, ias=45, gs=43, vsi=-900, thr_used=0.4, on_ground=0.0, radio_alt=250.0, roll=28.0),
        ]
        engine = SituationInferenceEngine()
        s = engine.infer(win10[-1], win10, win10)
        self.assertTrue(len(s.risks) > 0)


if __name__ == "__main__":
    unittest.main()
