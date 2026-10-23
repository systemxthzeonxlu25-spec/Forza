"""诊断主流程：跑所有检查项 → 交叉印证 → 拼结果；对外的 diagnose / diagnose_many / track_outline。"""

from __future__ import annotations

import copy
import math

import numpy as np

from ..lapdata import LapData
from .rules import DEFAULT_RULES, _merge, rules_info
from .common import (
    AXLE_CN,
    CHECK_LIST,
    CONF_LEVELS,
    DRIVETRAIN_CN,
    NOT_AVAILABLE,
    SEVERITY_RANK,
    _Ctx,
    _corner_key,
    _corner_label,
    _json_safe,
    _lap_info,
    _lap_label,
    _lower,
    _raise,
)
from .quality import _check_quality, _find_corners, _turn_share
from .check_balance import _check_balance
from .check_suspension import _check_suspension
from .check_tires import _check_tires
from .check_traction import _check_traction
from .check_brakes import _check_brake_balance, _check_brakes
from .check_brake_pressure import _check_brake_pressure
from .check_differential import _check_differential
from .check_gearing import _check_gearing
from .blocks import _balance_block, _body_block, _body_samples, _brake_block

# ================================================================ 交叉印证

def _cross_reference(ctx: _Ctx) -> None:
    """同一位置同时出现的相关问题，互相补一句说明（不改变判定）。"""
    by_id = {f["id"]: f for f in ctx.findings}
    spin, os_exit = by_id.get("wheelspin"), by_id.get("oversteer_exit")
    if spin and os_exit:
        common = {l["corner"] for l in spin["locations"]} & {l["corner"] for l in os_exit["locations"]} - {None}
        if common:
            txt = (f"{'、'.join(ctx.cn(c) for c in sorted(common, key=_corner_key))} 出弯同时检测到转向过度和驱动轮空转："
                   "多半是同一件事——驱动轮空转让后轴丢了侧向抓地。")
            os_exit["evidence"].insert(len(os_exit["evidence"]) - 1, txt)
            spin["evidence"].insert(len(spin["evidence"]) - 1, txt)
    us_mid = by_id.get("understeer_mid")
    tire = by_id.get("tire_lr_front")
    if us_mid and tire and tire["severity"] != "info":
        txt = f"同一圈还有弯中转向不足（{'、'.join(ctx.cn(c) for c in us_mid['data']['corners'])}）：前轮长时间过极限滑动，也会让外侧前胎更热。"
        tire["evidence"].insert(len(tire["evidence"]) - 1, txt)
    # 抱死 × 直线重刹的前后对比：只有个别段偏向抱死的那根轴 → 更像是那一脚的问题，不是刹车平衡整体偏
    bb = ctx.extra.get("brake_balance_counts")
    for axle in ("front", "rear"):
        lock = by_id.get(f"brake_lock_{axle}")
        if not lock or not bb or f"brake_bias_{axle}" in by_id:
            continue
        k = bb[axle]
        if bb["balanced"] >= max(1, bb["zones"] - k) and bb["balanced"] > k:
            txt = (f"直线重刹段里 {bb['balanced']}/{bb['zones']} 段前后轴滑移差不多，只有 {k} 段{AXLE_CN[axle]}先饱和："
                   f"刹车平衡整体并不偏{'前' if axle == 'front' else '后'}，这次抱死更像是那一脚踩得太猛 / 刹车压力偏大，"
                   "先试驾驶和刹车压力，再考虑挪刹车平衡。")
            lock["evidence"].insert(len(lock["evidence"]) - 1, txt)
            lock["suggestions"] = [x for x in lock["suggestions"] if not x.startswith("先排除驾驶")]
            lock["suggestions"].insert(0, "先排除驾驶：别一脚踩死，踩到快抱死的位置就停（其他重刹段前后平衡是正常的）")


# ================================================================ 对外接口


def _sort_findings(findings: list[dict]) -> list[dict]:
    return sorted(findings, key=lambda f: (-SEVERITY_RANK[f["severity"]], -CONF_LEVELS.index(f["confidence"]),
                                           min((l["dist_m"] for l in f["locations"]), default=0.0)))


def _rules_info(rules: dict | None) -> dict:
    return rules_info() if rules is None else {"rules": _merge(DEFAULT_RULES, rules), "source": "调用方传入", "error": None}


CHECKS = (_check_balance, _check_suspension, _check_tires, _check_traction, _check_brakes, _check_brake_balance,
          _check_brake_pressure, _check_differential, _check_gearing)


def _run(lap: LapData, info: dict, corners: list[dict] | None, abs_on: bool | None, aero: bool | None) -> tuple[dict, _Ctx]:
    """单圈诊断，返回 (可 JSON 化的结果, 上下文)。上下文里的 samples 给多圈合并拼分析块用。"""
    ctx = _Ctx(lap, info["rules"], abs_on=abs_on, aero=aero)
    if info["error"]:
        ctx.note("warn", info["error"])
    usable = _check_quality(ctx)
    if usable:
        _find_corners(ctx, corners)
        for fn in CHECKS:
            fn(ctx)
        _cross_reference(ctx)
        ctx.samples["body"] = _body_samples(ctx)
    else:
        for cid, nm in CHECK_LIST:
            ctx.check(cid, nm, "skipped", "数据不足，未检查。")
    findings = _sort_findings(ctx.findings)
    m = ctx.meta
    spd = ctx.ch("speed") if ctx.dash else None
    share = _turn_share(ctx.corners)
    summary = {
        "lap_time_s": m.get("lap_time"),
        "distance_m": round(float(ctx.dist[-1] - ctx.dist[0]), 1) if ctx.n else 0.0,
        "duration_s": round(float(ctx.t[-1] - ctx.t[0]), 2) if ctx.n else 0.0,
        "samples": ctx.n,
        "sample_hz": round(1.0 / ctx.dt, 1) if ctx.dt > 0 else None,
        "drivetrain": m.get("drivetrain"),
        "drivetrain_label": DRIVETRAIN_CN.get(m.get("drivetrain"), "未知"),
        "engine_max_rpm": m.get("engine_max_rpm"),
        "max_speed_kmh": round(float(spd.max()) * 3.6, 1) if spd is not None and spd.size else None,
        "top_gear_used": ctx.extra.get("top_gear_used"),
        "corner_count": len(ctx.corners),
        "corner_source": ctx.corner_source,
        "turn_share": {"right": round(share["right"], 3), "left": round(share["left"], 3)} if share else None,
        "tire_temps_c": ctx.extra.get("tire_temps_c"),
        "has_dash": ctx.dash,
        "assists": {"abs_on": abs_on, "aero": aero},
        "counts": {s: sum(1 for f in findings if f["severity"] == s) for s in ("alert", "warn", "info")},
    }
    rr = info["rules"]
    blocks = _blocks([ctx], rr, abs_on, aero) if usable else _blocks([], rr, abs_on, aero)
    res = {
        "mode": "single", "usable": usable, "lap": _lap_info(m), "lap_label": _lap_label(m), "summary": summary,
        "corners": _corners_out(ctx.corners), "findings": findings, "checks": ctx.checks,
        "not_available": copy.deepcopy(NOT_AVAILABLE), "data_quality": ctx.quality,
        "rules": {"source": info["source"], "error": info["error"]}, **blocks,
    }
    return _json_safe(res), ctx


def _corners_out(corners: list[dict]) -> list[dict]:
    out = []
    for c in corners:
        ms = c.get("min_speed")
        out.append({"name": c["name"], "label": _corner_label(c), "direction": c["direction"],
                    "start_m": round(c["start_m"], 1), "apex_m": round(c["apex_m"], 1), "end_m": round(c["end_m"], 1),
                    "heading_change_deg": c.get("heading_change_deg"),
                    "min_speed_kmh": round(ms * 3.6, 1) if ms is not None and math.isfinite(ms) else None})
    return out


def _blocks(ctxs: list[_Ctx], rules: dict, abs_on: bool | None, aero: bool | None) -> dict:
    """三个分析块（单圈：一个 ctx；多圈：所有可用圈的 ctx 拼起来）。"""
    return {
        "balance_map": _balance_block([c.samples.get("balance") for c in ctxs], rules, aero),
        "brake_analysis": _brake_block([c.samples.get("brake_zones") for c in ctxs],
                                       [c.samples.get("brake_pedal") for c in ctxs], rules, abs_on),
        "body": _body_block([c.samples.get("body") for c in ctxs], rules),
    }


def diagnose(lap: LapData, rules: dict | None = None, corners: list[dict] | None = None, *,
             abs_on: bool | None = None, aero: bool | None = None) -> dict:
    """单圈诊断。返回（可直接 JSON 化，没有 NaN）：

        mode: "single"
        usable: 数据够不够做诊断
        lap: 这圈的元数据（id、圈号、圈时、赛道、车、调校版本……）
        summary: 圈时、距离、采样率、驱动方式、最高转速、最高车速、用到的最高挡、弯道数、弯道来源（layout / detected）、
                 左右弯占比、热胎平均胎温、用户勾选的辅助 assists{abs_on, aero}、各严重程度的问题数 counts
        corners: [{name（编号 T1…）, label（显示名）, direction, start_m, apex_m, end_m, heading_change_deg, min_speed_kmh}]
        findings: [{id, category, category_label, severity(info|warn|alert), title, summary, evidence[],
                    locations[{dist_m, end_m, corner, phase, phase_label, wheel, wheel_label, t_s, duration_s, label}],
                    basis{channels[], rule, principle}, suggestions[], caveat, confidence(low|medium|high), data{}}]
                  按严重程度、置信度排序
        checks: [{id, name, status(found|ok|skipped), note}]  每项检查做了什么、结论如何（“没发现”和“没法判断”分开）
        balance_map / brake_analysis / body: 分析块（画图用的量化数据 + 解读句，结构见 _balance_block 等）
        not_available: [{item, why, instead}]  Data Out 给不了、所以判断不了的东西
        data_quality: [{level(info|warn|error), text}]
        rules: {source, error}  阈值来自哪个文件

    corners：外部给的弯道表（如 sectors.layout_corners 的结果，name 必须是 T1… 编号，可带 label），不给就本圈自己识别。
    abs_on / aero：用户勾选的“我开了 ABS”/“有可调空力”（None = 没说），只影响刹车 / 平衡结论的措辞和置信度。
    """
    return _run(lap, _rules_info(rules), corners, abs_on, aero)[0]


def diagnose_many(laps: list[LapData], rules: dict | None = None, corners: list | None = None, *,
                  abs_on: bool | None = None, aero: bool | None = None) -> dict:
    """多圈合并（同一辆车、同一条赛道的几圈）。corners = 和 laps 一一对应的弯道表列表（元素可为 None）。

    每圈先单独诊断，再按 finding.id 合并：
    - 每圈都出现（≥ 2 圈）→ 置信度升一级；
    - 只在少于 sporadic_frac 比例的圈里出现 → 视为偶发，严重程度、置信度各降一级；
    - evidence 开头写“在几圈里出现”，接着是代表圈（最严重那圈）的完整证据，再每圈一行摘要；
    - locations 合并所有圈，每个位置带 lap_id / lap_label。
    分析块（平衡分布 / 刹车 / 车身）把所有可用圈的采样拼起来算，热力表按弯道编号对齐。
    返回结构和 diagnose() 一样（mode = "multi"），另有 laps（每圈摘要）和每条 finding 的 repeat 字段。
    """
    info = _rules_info(rules)
    rr = info["rules"]
    corners = list(corners) if corners is not None else []
    corners += [None] * (len(laps) - len(corners))
    runs = [_run(l, info, c, abs_on, aero) for l, c in zip(laps, corners)]
    results = [x[0] for x in runs]
    usable_ctx = [ctx for res, ctx in runs if res["usable"]]
    usable = [x for x in results if x["usable"]]
    quality: list[dict] = []
    if info["error"]:
        quality.append({"level": "warn", "text": info["error"]})
    if not laps:
        quality.append({"level": "error", "text": "数据不足：没有给任何一圈。"})
    cars = {(l.meta.get("game"), l.meta.get("car_ordinal")) for l in laps}
    tracks = {l.meta.get("track_id") for l in laps}
    setups = {l.meta.get("setup_id") for l in laps}
    if len(cars) > 1:
        quality.append({"level": "warn", "text": "这几圈不是同一辆车，合并结果仅供参考（不同车的问题不能互相印证）。"})
    if len(tracks) > 1:
        quality.append({"level": "warn", "text": "这几圈不是同一条赛道，合并结果仅供参考（弯道编号对不上）。"})
    if len(setups) > 1 and setups != {None}:
        quality.append({"level": "info", "text": "这几圈不是同一个调校版本：合并后看到的是几套调校的混合结果。"})
    # 每圈的数据质量提示：相同文字合并，注明是哪几圈
    seen: dict[str, dict] = {}
    for res in results:
        for q in res["data_quality"]:
            if q["text"] == info["error"]:
                continue
            item = seen.setdefault(q["text"], {"level": q["level"], "text": q["text"], "laps": []})
            item["laps"].append(res["lap_label"])
    n_all = len(results)
    for item in seen.values():
        who = "所有圈" if len(item["laps"]) == n_all and n_all > 1 else "、".join(item["laps"])
        quality.append({"level": item["level"], "text": f"{who}：{item['text']}"})
    if laps and not usable:
        quality.append({"level": "error", "text": "数据不足：没有一圈的数据够做诊断。"})

    n = len(usable)
    sporadic = rr["multi"]["sporadic_frac"]
    groups: dict[str, list[tuple[dict, dict]]] = {}
    for res in usable:
        for f in res["findings"]:
            groups.setdefault(f["id"], []).append((res, f))
    merged = []

    def _flying(res: dict) -> bool:
        return "first_lap" not in (res["lap"].get("flags") or [])

    for items in groups.values():
        # 代表圈：最严重 → 置信度最高 → 优先非第一圈（冷胎、站立起步）→ 靠后的圈
        rep_res, rep = max(enumerate(items), key=lambda x: (
            SEVERITY_RANK[x[1][1]["severity"]], CONF_LEVELS.index(x[1][1]["confidence"]), _flying(x[1][0]), x[0]))[1]
        f = copy.deepcopy(rep)
        k = len({id(res) for res, _ in items})
        labels = [res["lap_label"] for res, _ in items]
        conf, sev = f["confidence"], f["severity"]
        if n >= 2 and k == n:
            conf = _raise(conf)
            rep_txt = f"在 {n} 圈里每圈都出现（{'、'.join(labels)}）→ 不是偶然，置信度升一级"
        elif n >= 2 and k / n < sporadic:
            conf, sev = _lower(conf), ("warn" if sev == "alert" else "info")
            rep_txt = f"在 {n} 圈里只有 {k} 圈出现（{'、'.join(labels)}）→ 可能是偶发失误，严重程度和置信度各降一级"
        else:
            rep_txt = f"在 {n} 圈里有 {k} 圈出现（{'、'.join(labels)}）"
        f["confidence"], f["severity"] = conf, sev
        f["evidence"] = [rep_txt, f"以下是 {rep_res['lap_label']} 的详细证据："] + rep["evidence"]
        for res, other in items:
            if other is not rep:
                f["evidence"].append(f"{res['lap_label']}：{other['evidence'][0]}")
        f["locations"] = []
        for res, other in items:
            for loc in other["locations"]:
                f["locations"].append({**loc, "lap_id": res["lap"].get("id"), "lap_label": res["lap_label"]})
        f["repeat"] = {"laps": k, "of": n, "lap_labels": labels}
        merged.append(f)
    findings = _sort_findings(merged)
    # 摘要 / 弯道表用的参考圈：优先第一个非“第一圈”的可用圈
    first = next((x for x in usable if _flying(x)), usable[0] if usable else (results[0] if results else None))
    checks = []
    for cid, _ in CHECK_LIST:
        rows = [(res["lap_label"], c) for res in results for c in res["checks"] if c["id"] == cid]
        if not rows:
            continue
        statuses = {c["status"] for _, c in rows}
        status = "found" if "found" in statuses else ("ok" if "ok" in statuses else "skipped")
        by_note: dict[str, list[str]] = {}
        for lbl, c in rows:
            by_note.setdefault(c["note"], []).append(lbl)
        parts = [(("所有圈" if len(lbls) == len(rows) and len(rows) > 1 else "、".join(lbls)) + "：" + txt)
                 for txt, lbls in by_note.items()]
        checks.append({"id": cid, "name": rows[0][1]["name"], "status": status, "note": "\n".join(parts)})
    lap_rows = [{"id": res["lap"].get("id"), "label": res["lap_label"], "lap_number": res["lap"].get("lap_number"),
                 "lap_time_s": res["summary"]["lap_time_s"], "usable": res["usable"], "counts": res["summary"]["counts"],
                 "setup_id": res["lap"].get("setup_id"), "setup_name": res["lap"].get("setup_name"),
                 "corner_source": res["summary"].get("corner_source")}
                for res in results]
    summary = dict(first["summary"]) if first else {}
    summary["counts"] = {s: sum(1 for f in findings if f["severity"] == s) for s in ("alert", "warn", "info")}
    summary["laps_used"] = n
    summary["laps_total"] = n_all
    summary["reference_lap"] = first["lap_label"] if first else None
    summary["assists"] = {"abs_on": abs_on, "aero": aero}
    return _json_safe({
        "mode": "multi", "usable": n > 0, "lap": first["lap"] if first else {}, "lap_label": f"{n_all} 圈合并",
        "laps": lap_rows, "summary": summary, "corners": first["corners"] if first else [],
        "findings": findings, "checks": checks, "not_available": copy.deepcopy(NOT_AVAILABLE),
        "data_quality": quality, "rules": {"source": info["source"], "error": info["error"]},
        **_blocks(usable_ctx, rr, abs_on, aero),
    })


def track_outline(lap: LapData, max_points: int = 600) -> dict:
    """赛道小地图用：降采样后的 {pos_x, pos_z, dist} 列表（和 LapData.to_json 的通道名一致）。"""
    n = len(lap)
    if n == 0 or not (lap.has("pos_x") and lap.has("pos_z") and lap.has("dist")):
        return {"pos_x": [], "pos_z": [], "dist": []}
    step = max(1, math.ceil(n / max_points))
    idx = np.unique(np.concatenate([np.arange(0, n, step), [n - 1]]))
    return _json_safe({k: np.round(np.asarray(lap[k], float)[idx], 2) for k in ("pos_x", "pos_z", "dist")})
