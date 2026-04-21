from __future__ import annotations

from dataclasses import dataclass

from .copilot_core import FlightSnapshot, SituationReport, TrendMetrics


def compute_trend_metrics(samples: list[FlightSnapshot]) -> TrendMetrics | None:
    # 趋势需要至少两个样本；不足时返回 None，交给上层降级处理
    if len(samples) < 2:
        return None
    start = samples[0]
    end = samples[-1]
    duration = max(end.timestamp_s - start.timestamp_s, 1e-3)
    return TrendMetrics(
        duration_s=duration,
        delta_altitude_ft=end.altitude_ft - start.altitude_ft,
        delta_airspeed_kts=end.airspeed_kts - start.airspeed_kts,
        delta_ground_speed_kts=end.ground_speed_kts - start.ground_speed_kts,
        delta_vsi_fpm=end.vertical_speed_fpm - start.vertical_speed_fpm,
        avg_airspeed_kts=sum(s.airspeed_kts for s in samples) / len(samples),
        avg_vertical_speed_fpm=sum(s.vertical_speed_fpm for s in samples) / len(samples),
        avg_roll_abs_deg=sum(abs(s.roll_deg) for s in samples) / len(samples),
        avg_pitch_deg=sum(s.pitch_deg for s in samples) / len(samples),
        avg_throttle_used_ratio=sum(s.throttle_used_ratio for s in samples) / len(samples),
    )


@dataclass
class PhaseScore:
    phase: str
    score: float
    evidence: list[str]


def _is_on_ground(s: FlightSnapshot) -> bool:
    if s.on_ground_any is not None:
        return s.on_ground_any > 0.5
    if s.radio_altitude_ft is not None:
        return s.radio_altitude_ft < 30.0
    return s.ground_speed_kts < 5.0 and abs(s.vertical_speed_fpm) < 120.0


def _infer_phase_scores(latest: FlightSnapshot, trend: TrendMetrics | None) -> list[PhaseScore]:
    # 轻量打分模型：根据速度/高度/垂速/刹车等特征计算各阶段分数
    scores: list[PhaseScore] = []
    on_ground = _is_on_ground(latest)
    near_ground = latest.radio_altitude_ft is not None and latest.radio_altitude_ft < 300.0
    t = trend
    dgs = 0.0 if t is None else t.delta_ground_speed_kts
    dalt = 0.0 if t is None else t.delta_altitude_ft

    evidence: list[str] = []
    ground_hold_score = 0.0
    if on_ground:
        ground_hold_score += 0.5
        evidence.append("on_ground")
    if latest.park_brake_ratio > 0.1:
        ground_hold_score += 0.3
        evidence.append("parking_brake")
    if latest.ground_speed_kts < 3:
        ground_hold_score += 0.2
        evidence.append("groundspeed_low")
    scores.append(PhaseScore("ground_hold", min(ground_hold_score, 1.0), evidence))

    evidence = []
    takeoff_roll_score = 0.0
    if on_ground:
        takeoff_roll_score += 0.3
        evidence.append("on_ground")
    if latest.ground_speed_kts > 20:
        takeoff_roll_score += 0.25
        evidence.append("groundspeed_gt20")
    if latest.throttle_used_ratio > 0.6:
        takeoff_roll_score += 0.2
        evidence.append("throttle_high")
    if dgs > 5:
        takeoff_roll_score += 0.25
        evidence.append("accelerating")
    scores.append(PhaseScore("takeoff_roll", min(takeoff_roll_score, 1.0), evidence))

    evidence = []
    initial_climb_score = 0.0
    if not on_ground:
        initial_climb_score += 0.2
        evidence.append("airborne")
    if latest.vertical_speed_fpm > 250:
        initial_climb_score += 0.35
        evidence.append("vsi_positive")
    if dalt > 120:
        initial_climb_score += 0.25
        evidence.append("altitude_increasing")
    if near_ground:
        initial_climb_score += 0.2
        evidence.append("near_ground")
    scores.append(PhaseScore("initial_climb", min(initial_climb_score, 1.0), evidence))

    evidence = []
    cruise_score = 0.0
    if not on_ground:
        cruise_score += 0.3
        evidence.append("airborne")
    if abs(latest.vertical_speed_fpm) < 400:
        cruise_score += 0.3
        evidence.append("vsi_stable")
    if t is not None and abs(t.delta_altitude_ft) < 150:
        cruise_score += 0.2
        evidence.append("altitude_stable")
    if latest.airspeed_kts > 80:
        cruise_score += 0.2
        evidence.append("ias_nominal")
    scores.append(PhaseScore("cruise", min(cruise_score, 1.0), evidence))

    evidence = []
    approach_score = 0.0
    if not on_ground and near_ground:
        approach_score += 0.35
        evidence.append("airborne_near_ground")
    if -1000 < latest.vertical_speed_fpm < -100:
        approach_score += 0.25
        evidence.append("descending")
    if latest.airspeed_kts < 140:
        approach_score += 0.2
        evidence.append("ias_approach_range")
    if latest.gear_ratio > 0.8:
        approach_score += 0.2
        evidence.append("gear_down")
    scores.append(PhaseScore("approach", min(approach_score, 1.0), evidence))

    evidence = []
    landing_roll_score = 0.0
    if on_ground:
        landing_roll_score += 0.4
        evidence.append("on_ground")
    if latest.ground_speed_kts > 20:
        landing_roll_score += 0.3
        evidence.append("groundspeed_gt20")
    if latest.throttle_used_ratio < 0.3:
        landing_roll_score += 0.1
        evidence.append("throttle_low")
    if latest.left_brake_ratio > 0.1 or latest.right_brake_ratio > 0.1:
        landing_roll_score += 0.2
        evidence.append("braking")
    scores.append(PhaseScore("landing_roll", min(landing_roll_score, 1.0), evidence))
    return scores


def _detect_risks(latest: FlightSnapshot, trend10: TrendMetrics | None) -> list[str]:
    # 风险规则保持可解释：每条风险都可映射回具体阈值条件
    risks: list[str] = []
    if latest.airspeed_kts < 55 and latest.vertical_speed_fpm < -500 and not _is_on_ground(latest):
        risks.append("stall_risk")
    if latest.airspeed_kts > 220:
        risks.append("overspeed_risk")
    if latest.throttle_cmd > 0.7 and latest.throttle_used_ratio < 0.2:
        risks.append("throttle_ineffective")
    if latest.radio_altitude_ft is not None and latest.radio_altitude_ft < 500 and abs(latest.roll_deg) > 25:
        risks.append("unstable_approach")
    if trend10 is not None and trend10.delta_ground_speed_kts > 15 and latest.left_brake_ratio > 0.2:
        risks.append("runway_excursion_risk")
    return risks


class SituationInferenceEngine:
    def infer(
        self,
        latest: FlightSnapshot,
        window_10s: list[FlightSnapshot],
        window_30s: list[FlightSnapshot],
    ) -> SituationReport:
        # 以 10s/30s 双窗口推断阶段和风险，输出统一报告供 UI/LLM 使用
        trend10 = compute_trend_metrics(window_10s)
        trend30 = compute_trend_metrics(window_30s)
        scores = _infer_phase_scores(latest, trend10)
        best = max(scores, key=lambda x: x.score)
        confidence = max(min(best.score, 0.99), 0.05)
        evidence = [f"{best.phase}:{best.score:.2f}"] + best.evidence
        risks = _detect_risks(latest, trend10)
        return SituationReport(
            phase=best.phase,
            confidence=confidence,
            evidence=evidence,
            risks=risks,
            latest=latest,
            trend_10s=trend10,
            trend_30s=trend30,
        )
