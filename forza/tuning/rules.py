"""阈值：内置默认值 DEFAULT_RULES + 读取 forza/tuning_rules.toml（改了文件会自动重新读取）。"""

from __future__ import annotations

import copy
import threading
import tomllib
from pathlib import Path

# 阈值文件放在 forza/ 下（不在这个包里），和以前的位置一样，用户改过的文件不用挪
RULES_PATH = Path(__file__).resolve().parent.parent / "tuning_rules.toml"

# 内置默认阈值（和 tuning_rules.toml 一致；每项的含义见 toml 里的注释）
DEFAULT_RULES: dict = {
    "general": {"min_lap_m": 400.0, "min_duration_s": 20.0, "min_samples": 200, "min_speed_ms": 8.0,
                "merge_gap_s": 0.10, "low_sample_hz": 20.0, "handbrake_max": 0.10, "clutch_max": 0.50},
    "balance": {"grip_limit": 1.0, "axle_diff": 0.30, "min_duration_s": 0.25, "min_lat_g": 0.30,
                "mid_min_lat_g": 0.50, "entry_min_brake": 0.10, "entry_min_steer": 0.05,
                "exit_min_throttle": 0.50, "exit_extend_m": 30.0, "min_phase_s": 0.30,
                "alert_total_s": 1.50, "alert_corners": 3},
    "suspension": {"bottom_level": 0.98, "lift_level": 0.02, "min_samples": 3, "group_m": 15.0,
                   "kerb_window_s": 0.10, "alert_events": 3, "lift_warn_s": 0.30},
    "tires": {"warm_ref_frac": 0.20, "warm_margin_c": 5.0, "min_warm_s": 30.0, "lr_diff_c": 6.0,
              "lr_alert_c": 12.0, "dominant_share": 0.60, "axle_excess_c": 4.0, "axle_diff_c": 8.0,
              "driven_axle_bonus_c": 4.0, "rising_c_per_min": 3.0, "sane_min_c": 40.0, "sane_max_c": 150.0},
    "traction": {"slip_limit": 1.0, "min_throttle": 0.50, "max_brake": 0.10, "min_duration_s": 0.10,
                 "alert_total_s": 1.0, "alert_events": 3},
    "brakes": {"slip_limit": 1.0, "min_brake": 0.30, "min_duration_s": 0.10, "alert_total_s": 0.50,
               "alert_events": 3},
    "gearing": {"limiter_frac": 0.98, "min_duration_s": 1.0, "merge_gap_s": 0.25, "alert_total_s": 3.0,
                "min_gear_time_s": 1.0, "headroom_frac": 0.85},
    "brake_balance": {"min_brake": 0.50, "max_steer": 0.05, "min_speed_kmh": 60.0, "kerb_window_s": 0.10,
                      "min_zone_s": 0.30, "slip_mark": 0.80, "peak_pct": 95.0, "bias_diff": 0.15,
                      "min_eval_peak": 0.30, "mark_lead_s": 0.05, "min_zones": 2, "dominant_share": 0.50},
    "brake_pressure": {"min_brake": 0.05, "max_steer": 0.05, "max_throttle": 0.05, "min_speed_kmh": 60.0,
                       "kerb_window_s": 0.10, "pedal_bin": 0.10, "min_bin_s": 0.15, "min_bins": 4, "min_span": 0.40,
                       "sat_frac": 0.95, "sat_warn_pedal": 0.80, "min_bins_above": 2, "full_pedal": 0.80,
                       "speed_band_quantiles": [0.5], "speed_band_edges_kmh": [], "coast_max_pedal": 0.02,
                       "min_coast_s": 1.0},
    "differential": {"min_lat_g": 0.30, "lift_max_throttle": 0.15, "lift_max_brake": 0.10, "slip_limit": 1.0,
                     "min_duration_s": 0.10, "alert_corners": 3, "sa_rise": 0.20, "sa_window_s": 0.30,
                     "min_lift_s": 0.50, "awd_ratio": 1.6, "awd_min_slip": 0.50, "awd_min_s": 0.30},
    "balance_map": {"speed_band_quantiles": [0.33, 0.67], "speed_band_edges_kmh": [], "min_band_s": 1.0,
                    "tendency_b": 0.15, "tendency_share": 0.15, "color_full_b": 0.50, "top_cells": 3},
    "body": {"kerb_window_s": 0.10, "damper_split_mm_s": 25.0, "hist_bin_mm_s": 10.0, "hist_max_mm_s": 200.0,
             "max_gap_factor": 3.0, "roll_min_lat_g": 0.50, "steady_max_dg_s": 1.0, "pitch_min_long_g": 0.30,
             "pitch_max_lat_g": 0.20, "cruise_max_long_g": 0.10, "near_bottom": 0.95, "near_top": 0.05,
             "min_samples": 120, "min_fit_s": 1.0, "grade_smooth_m": 10.0},
    "confidence": {"high_factor": 4.0, "medium_factor": 1.5, "margin": 0.10},
    "multi": {"sporadic_frac": 0.5},
}


# ================================================================ 阈值读取

_rules_lock = threading.Lock()
_rules_cache: dict = {"key": "unset", "rules": None, "source": None, "error": None}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_rules(path: str | Path | None = None) -> tuple[dict, str | None, str | None]:
    """读阈值文件并和默认值合并。返回 (rules, 实际读取的文件路径或 None, 错误信息或 None)。"""
    p = Path(path) if path else RULES_PATH
    if not p.exists():
        return copy.deepcopy(DEFAULT_RULES), None, None
    try:
        with open(p, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        return copy.deepcopy(DEFAULT_RULES), None, f"{p.name} 读取失败，已改用内置默认阈值：{e}"
    return _merge(DEFAULT_RULES, data), str(p), None


def rules_info() -> dict:
    """当前生效的阈值：{"rules", "source", "error"}。toml 文件改了（修改时间/大小变化）会自动重新读取。"""
    try:
        st = RULES_PATH.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    with _rules_lock:
        if _rules_cache["rules"] is None or _rules_cache["key"] != key:
            rules, src, err = load_rules()
            _rules_cache.update(key=key, rules=rules, source=src, error=err)
        return {"rules": copy.deepcopy(_rules_cache["rules"]), "source": _rules_cache["source"],
                "error": _rules_cache["error"]}


def get_rules() -> dict:
    return rules_info()["rules"]
