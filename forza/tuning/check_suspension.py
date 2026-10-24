"""检查项②：悬挂触底 / 伸到底（含压路肩造成的触底）。"""

from __future__ import annotations

import numpy as np

from .common import ONE_AT_A_TIME, WHEELS, WHEEL_CN, _Ctx, _finding, _pct, _runs, _wheel_cn, _wheel_code

# ================================================================ ② 悬挂触底 / 伸到底

def _check_suspension(ctx: _Ctx) -> None:
    name = "悬挂触底 / 伸到底"
    need = [f"susp_{w}" for w in WHEELS]
    if ctx.missing(need):
        ctx.check("suspension", name, "skipped", "数据不足：缺少悬挂行程通道。")
        return
    r = ctx.r["suspension"]
    k_kerb = max(1, round(r["kerb_window_s"] / ctx.dt))
    events = []
    for w in WHEELS:
        s = ctx.ch(f"susp_{w}")
        rum = ctx.ch(f"rumble_{w}")
        for kind, mask in (("bottom", s >= r["bottom_level"]), ("lift", s <= r["lift_level"])):
            for i0, i1 in _runs(mask, ctx.t, 0.0, ctx.dt):
                if i1 - i0 + 1 < r["min_samples"]:
                    continue
                kerb = rum is not None and bool(np.any(rum[max(0, i0 - k_kerb): i1 + k_kerb + 1] > 0.5))
                seg = s[i0:i1 + 1]
                events.append({"kind": kind, "wheel": w, "axle": "front" if w in ("fl", "fr") else "rear",
                               "i0": i0, "i1": i1, "kerb": kerb,
                               "peak": float(seg.max() if kind == "bottom" else seg.min())})

    # 按 (现象, 是否路肩, 轴) 分组，再按距离聚成“位置”
    groups: dict[tuple, list[list[dict]]] = {}
    for e in sorted(events, key=lambda e: ctx.dist[e["i0"]]):
        key = (e["kind"], e["kerb"], "any" if e["kerb"] else e["axle"])
        clusters = groups.setdefault(key, [])
        if clusters and ctx.dist[e["i0"]] - max(ctx.dist[x["i1"]] for x in clusters[-1]) <= r["group_m"]:
            clusters[-1].append(e)
        else:
            clusters.append([e])

    found = 0
    for (kind, kerb, axle), clusters in groups.items():
        if kerb:
            continue
        found += 1
        ctx.findings.append(_susp_finding(ctx, kind, axle, clusters))
    kerb_clusters = [(k[0], c) for k, cl in groups.items() if k[1] for c in cl]
    if kerb_clusters:
        found += 1
        ctx.findings.append(_kerb_finding(ctx, kerb_clusters))
    lvl = f"压到底 ≥ {r['bottom_level']:.2f}、伸到底 ≤ {r['lift_level']:.2f}，连续 ≥ {r['min_samples']} 个采样"
    lo = min(float(ctx.ch(f"susp_{w}").min()) for w in WHEELS)
    hi = max(float(ctx.ch(f"susp_{w}").max()) for w in WHEELS)
    if found:
        n_place = {k: len(v) for k, v in groups.items()}
        parts = []
        for kind, word in (("bottom", "触底"), ("lift", "伸到底")):
            for axle in ("front", "rear"):
                k = n_place.get((kind, False, axle), 0)
                if k:
                    parts.append(f"{WHEEL_CN[axle]}{word} {k} 处")
        kerb = len(kerb_clusters)
        if kerb:
            parts.append(f"压路肩时 {kerb} 处")
        ctx.check("suspension", name, "found", f"四轮悬挂行程范围 {lo:.2f}~{hi:.2f}；{'，'.join(parts)}（{lvl}）。")
    else:
        ctx.check("suspension", name, "ok", f"四轮悬挂行程范围 {lo:.2f}~{hi:.2f}，没有触底或伸到底（{lvl}）。")


def _cluster_context(ctx: _Ctx, cl: list[dict]) -> dict:
    i0, i1 = min(e["i0"] for e in cl), max(e["i1"] for e in cl)
    sl = slice(i0, i1 + 1)
    out = {"i0": i0, "i1": i1, "wheels": sorted({e["wheel"] for e in cl}, key=WHEELS.index),
           "dur": max(ctx.dur(e["i0"], e["i1"]) for e in cl), "peak": cl[0]["peak"]}
    out["peak"] = max(e["peak"] for e in cl) if cl[0]["kind"] == "bottom" else min(e["peak"] for e in cl)
    for ch, key in (("brake", "brk"), ("throttle", "thr"), ("speed", "spd")):
        a = ctx.ch(ch) if ctx.dash else None
        out[key] = float(a[sl].mean()) if a is not None else None
    gl = ctx.ch("g_lat")
    out["glat"] = float(np.abs(gl[sl]).mean()) if gl is not None else None
    gv = ctx.ch("g_vert")
    out["gvert"] = float(np.abs(gv[sl] - np.median(gv)).max()) if gv is not None else None
    if out["brk"] is not None and out["brk"] >= 0.3:
        out["cause"] = "brake"
    elif out["glat"] is not None and out["glat"] >= 0.8:
        out["cause"] = "corner"
    else:
        out["cause"] = "bump"
    return out


def _susp_finding(ctx: _Ctx, kind: str, axle: str, clusters: list[list[dict]]) -> dict:
    r = ctx.r["suspension"]
    axle_cn = WHEEL_CN[axle]
    infos = [_cluster_context(ctx, cl) for cl in clusters]
    total = sum(x["dur"] for x in infos)
    min_s = r["min_samples"] * ctx.dt
    evidence, locs = [], []
    cause_cn = {"brake": "刹车点头时", "corner": "过弯侧倾时", "bump": "直线上 / 压到起伏时"}
    for x in infos:
        wheel = _wheel_code(x["wheels"])
        loc = ctx.loc(x["i0"], x["i1"], wheel=wheel)
        locs.append(loc)
        ctx_txt = []
        if x["spd"] is not None:
            ctx_txt.append(f"车速 {x['spd'] * 3.6:.0f} km/h")
        if x["brk"] is not None:
            ctx_txt.append(f"刹车 {_pct(x['brk'])}")
        if x["glat"] is not None:
            ctx_txt.append(f"横向 G {x['glat']:.2f}")
        if x["gvert"] is not None:
            ctx_txt.append(f"垂直 G 相对本圈中位数最大变化 {x['gvert']:.2f}")
        word = "压到底" if kind == "bottom" else "伸到底"
        evidence.append(f"{loc['label']}：{_wheel_cn(wheel)}悬挂行程 {x['peak']:.2f}（{word}），持续约 {x['dur']:.2f} s；"
                        f"前后 {r['kerb_window_s']:.2f} s 内没压路肩（rumble = 0）；{cause_cn[x['cause']]}（{'，'.join(ctx_txt)}）")
    causes = {x["cause"] for x in infos}
    if kind == "bottom":
        sev = "alert" if len(infos) >= r["alert_events"] else "warn"
        evidence.append(f"判定阈值：归一化悬挂行程 ≥ {r['bottom_level']:.2f} 连续 ≥ {r['min_samples']} 个采样，"
                        f"且不在压路肩时。本圈{axle_cn}共 {len(infos)} 处（≥ {r['alert_events']} 处记为严重）")
        title = f"{axle_cn}悬挂触底"
        summary = (f"{axle_cn}悬挂在 {len(infos)} 处被压到底（没压路肩），"
                   f"{'、'.join(cause_cn[c] for c in sorted(causes))}发生：触底瞬间轮胎抓地会突然变化、底盘可能擦地。")
        tips = [f"{axle_cn}弹簧调硬一点", f"{axle_cn}压缩（bump）阻尼调硬一点",
                f"{axle_cn}车身高度调高一点（会抬高重心，小步改）"]
        if "brake" in causes and axle == "front":
            tips.append("刹车点头触底：也可以试试刹车别一脚踩死，或前防倾杆略硬")
        if "corner" in causes:
            tips.append(f"过弯侧倾触底：{axle_cn}防倾杆调硬一点")
        caveat = "偶尔一次影响不大；每圈同一位置都触底才值得改。" + ONE_AT_A_TIME
        rule = (f"每个轮子：归一化悬挂行程 ≥ {r['bottom_level']:.2f} 连续 ≥ {r['min_samples']} 个采样记一次；"
                f"前后 {r['kerb_window_s']:.2f} s 内这个轮子 rumble = 1 的算“压路肩”，单独列为提示；"
                f"相距 < {r['group_m']:.0f} m 的归成同一处。")
        principle = ("悬挂行程 1 = 压到底：弹簧已经没有行程，冲击直接传给车身和轮胎，轮胎载荷瞬间突变、抓地不稳。"
                     "没压路肩时触底，说明这根轴的弹簧 / 压缩阻尼 / 车身高度不够应付这里的起伏或载荷转移。")
        conf = "high" if len(infos) >= 2 and total >= min_s * ctx.r["confidence"]["high_factor"] else \
            ("medium" if total >= min_s * ctx.r["confidence"]["medium_factor"] else "low")
    else:
        sev = "warn" if total >= r["lift_warn_s"] else "info"
        evidence.append(f"判定阈值：归一化悬挂行程 ≤ {r['lift_level']:.2f} 连续 ≥ {r['min_samples']} 个采样，且不在压路肩时。"
                        f"本圈累计 {total:.2f} s（≥ {r['lift_warn_s']:.2f} s 记为注意，否则只提示）")
        title = f"{axle_cn}悬挂伸到底（可能离地）"
        summary = (f"{axle_cn}悬挂在 {len(infos)} 处伸到最长（没压路肩），"
                   f"{'过弯时多半是内侧轮被抬起' if 'corner' in causes else '多半是过坡 / 起伏时车轮下垂'}：这一瞬间这个轮子几乎不出力。")
        tips = []
        if "corner" in causes:
            tips += [f"过弯内侧轮离地：{axle_cn}防倾杆调软一点", f"{axle_cn}回弹（rebound）阻尼调软一点，让车轮更快伸下去贴地"]
        tips += [f"过坡离地一般不用管；频繁发生可以试 {axle_cn}弹簧调软一点 / 车身高度调高一点"]
        caveat = "悬挂伸到底不一定等于离地（Data Out 没有“轮子离地”这个字段），这里是推断。" + ONE_AT_A_TIME
        rule = (f"每个轮子：归一化悬挂行程 ≤ {r['lift_level']:.2f} 连续 ≥ {r['min_samples']} 个采样记一次；"
                f"压路肩时的单独列为提示；相距 < {r['group_m']:.0f} m 的归成同一处。")
        principle = "悬挂行程 0 = 伸到最长：车轮已经挂到行程尽头，再往上抬车身轮子就离地了，这个轮子此时基本没有载荷和抓地。"
        conf = "medium" if total >= min_s * ctx.r["confidence"]["medium_factor"] else "low"
    basis = {"channels": ["susp_fl/fr/rl/rr（归一化悬挂行程，0 = 伸到最长，1 = 压到底）", "rumble_*（压路肩）",
                          "brake / g_lat / g_vert / speed（判断当时在刹车、过弯还是压起伏）"],
             "rule": rule, "principle": principle}
    fid = f"{'bottoming' if kind == 'bottom' else 'wheel_lift'}_{axle}"
    return _finding(fid, "suspension", sev, title, summary, evidence, locs, basis, tips, conf, caveat,
                    data={"kind": kind, "axle": axle, "places": len(infos), "total_s": round(total, 3)})


def _kerb_finding(ctx: _Ctx, kerb_clusters: list[tuple[str, list[dict]]]) -> dict:
    r = ctx.r["suspension"]
    evidence, locs = [], []
    for kind, cl in kerb_clusters:
        x = _cluster_context(ctx, cl)
        wheel = _wheel_code(x["wheels"])
        loc = ctx.loc(x["i0"], x["i1"], wheel=wheel)
        locs.append(loc)
        word = "压到底" if kind == "bottom" else "伸到底"
        evidence.append(f"{loc['label']}：{_wheel_cn(wheel)}悬挂{word}（{x['peak']:.2f}），同时压着路肩（rumble = 1），持续约 {x['dur']:.2f} s")
    evidence.append(f"判定：悬挂行程 ≥ {r['bottom_level']:.2f} 或 ≤ {r['lift_level']:.2f} 的前后 {r['kerb_window_s']:.2f} s 内该轮压了路肩")
    basis = {"channels": ["susp_fl/fr/rl/rr", "rumble_fl/fr/rl/rr"],
             "rule": "悬挂触底 / 伸到底发生时，同一个轮子正在压路肩（rumble = 1），归为“路肩造成”。",
             "principle": "路肩是突然的高低差，压上去时悬挂被瞬间压到底或掉下去都很常见，不一定是调校问题。"}
    tips = ["如果压路肩时车不稳：压缩（bump）阻尼调软一点，更“吃”路肩", "或者少压 / 换条走线避开高路肩"]
    return _finding("kerb_strike", "suspension", "info", "压路肩时悬挂触底 / 伸到底",
                    f"有 {len(locs)} 处悬挂到极限时正压着路肩，多半是路肩造成的，属于常见现象。",
                    evidence, locs, basis, tips, "medium", "只是提示：如果压这些路肩时车没有明显不稳，可以不管。",
                    data={"places": len(locs)})
