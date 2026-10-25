"""检查项⑦：降挡超转（降挡太早，转速一下冲到断油附近）。

思路参考 Pacefinder 的 bad_shift 检测：降挡后新挡位的转速冲过红线 = 这个车速降这个挡太早了。
后果：发动机制动突然变大（后驱车入弯容易甩尾、刹车时后轮容易抱死），也可能损失入弯的稳定性。
这一项看的是驾驶（什么时候降挡），不是调校；但如果每个弯都超转，也可能是相邻两挡齿比差太大。
"""

from __future__ import annotations

import numpy as np

from .common import ONE_AT_A_TIME, _Ctx, _confidence, _corner_key, _finding, _pct

# ================================================================ ⑦ 降挡超转

def _check_downshift(ctx: _Ctx) -> None:
    name = "降挡超转（降挡太早）"
    if not ctx.dash or ctx.missing(("gear", "rpm")):
        ctx.check("downshift", name, "skipped", "数据不足：需要挡位、转速。")
        return
    max_rpm = float(ctx.meta.get("engine_max_rpm") or 0.0)
    if max_rpm <= 0:
        ctx.check("downshift", name, "skipped", "数据不足：不知道发动机最高转速（engine_max_rpm）。")
        return
    r = ctx.r["downshift"]
    gear = np.round(ctx.ch("gear")).astype(int)
    rpm = ctx.ch("rpm")
    limit = r["overrev_frac"] * max_rpm
    win = max(1, int(round(r["window_s"] / ctx.dt)))

    # 降挡的位置：挡位变小（都是前进挡，一次最多降 max_drop 个挡，过滤倒挡 / 数据跳变）
    prev, cur = gear[:-1], gear[1:]
    drops = np.flatnonzero((prev >= 1) & (cur >= 1) & (cur < prev) & (prev - cur <= r["max_drop"])) + 1
    # 每次换挡（升或降）的位置：每次降挡的观察窗口截到下一次换挡为止，
    # 否则连降几个挡时，最后那一下的超转会被算到前面几次正常降挡头上
    changes = np.flatnonzero(np.diff(gear) != 0) + 1
    events = []
    for i in drops:
        if not ctx.speed_ok[i] or rpm[i - 1] >= limit:      # 降挡前就已经在断油（那是没升挡，不是降早了）
            continue
        nxt = changes[changes > i]
        j = min(ctx.n, i + win, int(nxt[0]) if nxt.size else ctx.n)
        k = i + int(np.argmax(rpm[i:j]))
        if rpm[k] >= limit:
            events.append((int(i), int(k)))
    if not drops.size:
        ctx.check("downshift", name, "ok", "这圈没有降挡（或者挡位数据没变化）。")
        return
    if not events:
        ctx.check("downshift", name, "ok", f"降挡 {drops.size} 次，降挡后转速都没冲到最高转速的 {_pct(r['overrev_frac'])}。")
        return

    locs, evidence = [], []
    for i, k in events:
        loc = ctx.loc(i, k)
        locs.append(loc)
        evidence.append(f"{loc['label']}：{gear[i - 1]} → {gear[i]} 挡，降挡前 {rpm[i - 1]:.0f} rpm，"
                        f"降挡后 {ctx.dur(i, k):.2f} s 内冲到 {rpm[k]:.0f} rpm（最高转速 {max_rpm:.0f} 的 {_pct(rpm[k] / max_rpm)}）")
    evidence.append(f"判定阈值：降挡后 {r['window_s']:.1f} s 内转速 ≥ 最高转速 × {r['overrev_frac']:.2f}；"
                    f"降挡前已经在这个转速以上的不算；{r['alert_events']} 次以上记为严重")
    corners = sorted({l["corner"] for l in locs if l["corner"]}, key=_corner_key)
    sev = "alert" if len(events) >= r["alert_events"] else "warn"
    where = f"（{'、'.join(ctx.cn(c) for c in corners)}）" if corners else ""
    ctx.findings.append(_finding(
        "downshift_overrev", "shifting", sev, "降挡太早，转速冲到断油附近",
        f"这圈有 {len(events)} 次降挡后转速一下冲到最高转速附近{where}：车速还太快就降了挡。",
        evidence, locs,
        {"channels": ["gear", "rpm", "meta.engine_max_rpm"],
         "rule": (f"找出每次降挡（挡位变小、一次最多降 {r['max_drop']} 挡），看降挡后 {r['window_s']:.1f} s 内的最高转速；"
                  f"≥ {_pct(r['overrev_frac'])} 最高转速、且降挡前还没到这个转速的，记一次。"),
         "principle": ("降挡后转速 ≈ 车速 × 新挡位的齿比：车速还高的时候降挡，转速会冲到断油附近，"
                       "发动机制动突然变大——后驱车容易在入弯时甩尾，刹车时后轮更容易抱死。")},
        ["降挡晚一点：先把车速刹下来，降挡后转速落在红线以下再降",
         "连降几个挡时一个一个来，每降一个等转速回落一点",
         "如果几乎每个弯都这样，再看相邻两挡齿比是不是差太大（降一挡转速跳得太多）"],
        _confidence(ctx.r, float(len(events)), 1.0, 0.0),
        "这一项主要是驾驶习惯；用自动挡时是游戏在降挡，这条参考意义不大。" + ONE_AT_A_TIME,
        data={"events": len(events), "downshifts": int(drops.size), "engine_max_rpm": max_rpm}))
    ctx.check("downshift", name, "found", f"降挡 {drops.size} 次，其中 {len(events)} 次降挡后转速冲到断油附近。")
