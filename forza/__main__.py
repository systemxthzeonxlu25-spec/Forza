"""命令行入口：python -m forza <命令>

    serve    收游戏的包 + 开网页（实时面板 / 圈速 / 诊断）；--replay 用录像代替游戏
    record   只录原始包，不开网页
    replay   把录像按原始时序用 UDP 发出去（假装自己是游戏）
    import   离线导入录像：切圈后存进数据库
    fake     生成假数据录像（不开游戏也能开发调试）
    info     查看录像内容（包长分布、识别出的格式、能切出几圈）
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections import Counter
from pathlib import Path

from .config import ConfigError, load_config
from .profiles import PROFILES

GAME_CHOICES = [k for k in PROFILES if k not in ("fm", "fh")]


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")


def _apply_common(cfg, args) -> None:
    for name in ("udp_host", "udp_port", "http_host", "http_port", "db_path", "game", "tire_temp_unit"):
        v = getattr(args, name, None)
        if v is not None:
            setattr(cfg, name, v)
    if getattr(args, "forward", None):
        cfg.forward = args.forward
    cfg.validate()        # 命令行覆盖完再统一检查一遍（不对就抛 ConfigError，main() 里转成中文提示）


def cmd_serve(args) -> None:
    from .runtime import Runtime

    cfg = load_config()
    _apply_common(cfg, args)
    rt = Runtime(cfg, replay=args.replay, speed=args.speed, loop=args.loop, record=args.record)
    try:
        asyncio.run(rt.run())
    except KeyboardInterrupt:
        pass


def cmd_record(args) -> None:
    from datetime import datetime

    from .hub import Hub
    from .recorder import Recorder
    from .udp import start_udp

    cfg = load_config()
    _apply_common(cfg, args)
    out = Path(args.output) if args.output else cfg.resolve(cfg.rec_dir) / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + ".frec")
    rec = Recorder(out, meta={"source": f"udp:{cfg.udp_host}:{cfg.udp_port}", "game": cfg.game})
    hub = Hub(cfg.game, rec, cfg.forward, "record")

    async def main():
        tr = await start_udp(hub, cfg.udp_host, cfg.udp_port)
        print(f"正在录制到 {out}，UDP 端口 {cfg.udp_port}，Ctrl+C 结束", flush=True)
        t0 = time.monotonic()
        try:
            while args.duration is None or time.monotonic() - t0 < args.duration:
                await asyncio.sleep(2)
                if hub.recorder is None:      # 写文件失败，Hub 已经自动停了录制：别假装还在录
                    print(f"  {hub.record_error or '录制已停止'}", flush=True)
                    break
                st = hub.status()
                print(f"  已收 {st['packets']} 包，{st['pps']:.0f} 包/秒，格式 {st['format_label'] or '-'}",
                      flush=True)
        finally:
            tr.close()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        rec.close()
        print(f"已保存 {rec.count} 个包 → {out}")


def cmd_replay(args) -> None:
    from .replay import replay_udp

    host, _, port = args.to.rpartition(":")
    print(f"回放 {args.file} → {host or '127.0.0.1'}:{port}（{args.speed}x{'，循环' if args.loop else ''}）")
    try:
        n = replay_udp(args.file, host or "127.0.0.1", int(port), args.speed, args.loop)
        print(f"发送完毕：{n} 个包")
    except KeyboardInterrupt:
        pass


def cmd_import(args) -> None:
    from . import frame as frame_mod
    from .hub import Hub
    from .laps import LapDetector, LapSaver
    from .replay import import_file
    from .storage import Store

    cfg = load_config()
    _apply_common(cfg, args)
    frame_mod.TIRE_TEMP_UNIT = cfg.tire_temp_unit
    store = Store(cfg.resolve(cfg.db_path))
    for f in args.files:
        hub = Hub(cfg.game, source=f"import:{Path(f).name}")
        det = LapDetector()
        saver = LapSaver(store, f"import:{Path(f).name}", background=False)
        det.add_listener(saver)
        det.add_invalidation_listener(saver.invalidate)
        hub.subscribe(det.on_frame)
        n = import_file(f, hub)
        det.flush()
        print(f"{f}：{n} 个包，存了 {len(saver.saved_ids)} 圈，丢弃未完成 {det.dropped_partial} 段")
        for lap in store.list_laps_by_ids(saver.saved_ids):
            print(f"  #{lap['id']:<5} {lap['kind']:<6} 第 {lap['lap_number']} 圈  {_fmt_time(lap['lap_time'])}"
                  f"  {lap['distance_m']:.0f} m  {'完整' if lap['is_complete'] else '不完整'}  {lap['track_name']}")
    print(f"数据库：{store.path}")


def cmd_fake(args) -> None:
    from .fake import write_fake_recording

    fmt = args.format
    if args.scenario in ("sprint", "freeroam") and not fmt.startswith("fh"):
        fmt = "fh"   # 冲刺赛 / 自由漫游只有 Horizon 有
        print("冲刺赛 / 自由漫游是 Horizon 才有的玩法，包格式自动改用 fh（324 字节）")
    out = Path(args.output or f"samples/demo_{fmt}_{args.scenario}.frec")
    truth = write_fake_recording(out, fmt, args.scenario, laps=args.laps, hz=args.hz, seed=args.seed,
                                 issues=not args.clean, rewind=args.rewind, restart=args.restart,
                                 total_size=args.size, grid_offset_m=args.grid_offset)
    print(f"已生成 {out}")
    for lap in truth.get("laps", []):
        print(f"  第 {lap['lap_number']} 圈 {_fmt_time(lap['lap_time'])}（{lap['kind']}）")
    for run in truth.get("runs", []):
        print(f"  冲刺：{run}")


def cmd_info(args) -> None:
    from . import frame as frame_mod
    from .hub import Hub
    from .laps import LapDetector
    from .recorder import iter_records, read_meta

    cfg = load_config()
    cfg.validate()        # info 不走 _apply_common，这里单独检查（胎温单位写错会让整份报告的胎温都不对）
    frame_mod.TIRE_TEMP_UNIT = cfg.tire_temp_unit
    meta = read_meta(args.file)
    hub = Hub(args.game)
    det = LapDetector()
    laps = []
    det.add_listener(laps.append)
    hub.subscribe(det.on_frame)
    fmts: Counter[str] = Counter()
    hub.subscribe(lambda fr: fmts.update([f"{fr.fmt}/{fr.game}"]))
    t_last = 0.0
    for t, data in iter_records(args.file):
        hub.feed(data, t)
        t_last = t
    det.flush()
    print(f"文件：{args.file}")
    print(f"元数据：{meta}")
    print(f"时长：{t_last:.1f} s，包数：{hub.packets}，包长分布：{dict(hub.sizes)}")
    print(f"识别结果（格式/游戏）：{dict(fmts)}")
    for w in hub.decoder.warnings.values():
        print(f"提示：{w}")
    print(f"切出 {len(laps)} 圈：")
    for lap in laps:
        m = lap.meta
        print(f"  {m['kind']:<6} 第 {m['lap_number']} 圈 {_fmt_time(m['lap_time'])}  {m['distance_m']:.0f} m"
              f"  来源={m['time_source']}  {' '.join(m['flags'])}")


def _fmt_time(s: float | None) -> str:
    if not s:
        return "-"
    m, sec = divmod(s, 60)
    return f"{int(m)}:{sec:06.3f}"


def main(argv=None) -> int | None:
    p = argparse.ArgumentParser(prog="python -m forza", description="Forza Data Out 遥测分析工具")
    p.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, http=True):
        sp.add_argument("--udp-host", help="UDP 监听地址（仅本机采样可设 127.0.0.1）")
        sp.add_argument("--udp-port", type=int, help="UDP 端口（默认 5555）")
        if http:
            sp.add_argument("--http-host", help="网页监听地址（仅本机访问可设 127.0.0.1）")
            sp.add_argument("--http-port", type=int, help="网页端口（默认 8000）")
        sp.add_argument("--game", choices=GAME_CHOICES, help="指定游戏（324 字节包分不出 FH4/5/6 时用）")
        sp.add_argument("--db", dest="db_path", help="数据库路径（默认 data/forza.db）")
        sp.add_argument("--tire-unit", dest="tire_temp_unit", choices=["F", "C"], help="游戏胎温单位（默认 F）")
        sp.add_argument("--forward", nargs="*", metavar="HOST:PORT", help="把原始包转发给其他程序（如 SimHub）")

    sp = sub.add_parser("serve", help="收包 + 网页")
    common(sp)
    sp.add_argument("--record", nargs="?", const="", default=None, metavar="PATH",
                    help="同时录制原始包（不给路径就存到 recordings/）")
    sp.add_argument("--replay", metavar="FILE", help="用录像代替游戏")
    sp.add_argument("--speed", type=float, default=1.0, help="回放倍速（0 = 尽快）")
    sp.add_argument("--loop", action="store_true", help="循环回放")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("record", help="只录制原始包")
    common(sp, http=False)
    sp.add_argument("-o", "--output", help="输出文件（.frec，以 .gz 结尾则压缩）")
    sp.add_argument("--duration", type=float, help="录多少秒后自动停")
    sp.set_defaults(func=cmd_record)

    sp = sub.add_parser("replay", help="用 UDP 回放录像")
    sp.add_argument("file")
    sp.add_argument("--to", default="127.0.0.1:5555", help="目标地址（默认 127.0.0.1:5555）")
    sp.add_argument("--speed", type=float, default=1.0, help="倍速（0 = 尽快）")
    sp.add_argument("--loop", action="store_true")
    sp.set_defaults(func=cmd_replay)

    sp = sub.add_parser("import", help="离线导入录像到数据库")
    sp.add_argument("files", nargs="+")
    sp.add_argument("--game", choices=GAME_CHOICES)
    sp.add_argument("--db", dest="db_path")
    sp.add_argument("--tire-unit", dest="tire_temp_unit", choices=["F", "C"])
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("fake", help="生成假数据录像")
    sp.add_argument("-o", "--output")
    sp.add_argument("--format", default="fm2023_dash",
                    choices=["sled", "fm7_dash", "fh_legacy", "fh", "fm2023_dash"])
    sp.add_argument("--scenario", default="circuit", choices=["circuit", "sprint", "freeroam"])
    sp.add_argument("--laps", type=int, default=5)
    sp.add_argument("--hz", type=float, default=60.0)
    sp.add_argument("--seed", type=int, default=1)
    sp.add_argument("--clean", action="store_true", help="不埋调车问题（测误报用）")
    sp.add_argument("--rewind", action="store_true", help="第 2 圈中途倒带一次")
    sp.add_argument("--restart", action="store_true", help="第 2 圈中途重开比赛")
    sp.add_argument("--size", type=int, help="把包补零到这个长度（模拟未知格式）")
    sp.add_argument("--grid-offset", type=float, default=0.0, metavar="M",
                    help="赛道赛从起终点线后 M 米的发车格起步（第一圈会比飞驰圈长，测赛道识别用；0~260）")
    sp.set_defaults(func=cmd_fake)

    sp = sub.add_parser("info", help="查看录像内容")
    sp.add_argument("file")
    sp.add_argument("--game", choices=GAME_CHOICES)
    sp.set_defaults(func=cmd_info)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        args.func(args)
    except ConfigError as e:
        print(f"配置有误：{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
