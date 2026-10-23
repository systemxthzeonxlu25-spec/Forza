"""全局配置。

优先级：命令行参数 > 项目根目录的 config.toml > 这里的默认值。
config.toml 示例见 config.example.toml。
"""
from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

# 指向“本机”的地址：转发目标是这些地址 + 自己的 UDP 端口时，包会转回给自己，无限循环
_SELF_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1", ""}


class ConfigError(ValueError):
    """配置写得不对。message 是给用户看的中文提示（指出是哪一项、该怎么改）。"""


@dataclass
class Config:
    udp_host: str = "0.0.0.0"
    # 默认 5555：FH6 官方文档要求避开 5200~5300（游戏自己占用），5555 对所有游戏都安全
    udp_port: int = 5555
    http_host: str = "0.0.0.0"   # 0.0.0.0 才能让局域网里的手机访问
    http_port: int = 8000
    db_path: str = "data/forza.db"
    rec_dir: str = "recordings"
    game: str | None = None      # 强制指定游戏：fm7 / fm2023 / fh4 / fh5 / fh6
    tire_temp_unit: str = "F"    # 游戏发来的胎温单位，社区共识是 °F
    live_hz: float = 20.0        # 实时面板推送频率（手机上 20Hz 足够流畅）
    forward: list[str] | None = None  # 原样转发原始包到其他程序，如 ["127.0.0.1:20777"]
    # 内存里常驻保留最近几分钟的原始包，实时面板菜单里可以“保存刚才 N 分钟”（0 = 关闭）。
    # 每分钟大约占 1.2 MB（FM 60 包/秒）~ 2.8 MB（FH 高帧率约 140 包/秒）
    record_buffer_minutes: float = 10.0

    def validate(self) -> None:
        """检查各项的类型和取值范围，不对就抛 ConfigError。

        config.toml 里写错类型（例如 udp_port = "5555" 写成了字符串）时，
        与其等到监听端口时才报一句看不懂的英文错误，不如启动时就指出是哪一项。
        """
        from .profiles import PROFILES

        def port(name: str) -> None:
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 65535:
                raise ConfigError(f"{name} 要是 0~65535 的整数，现在是 {v!r}")

        def number(name: str, lo: float, allow_eq: bool = True) -> None:
            v = getattr(self, name)
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) and (v >= lo if allow_eq else v > lo)
            if not ok:
                cmp = "≥" if allow_eq else ">"
                raise ConfigError(f"{name} 要是 {cmp} {lo:g} 的数字，现在是 {v!r}")

        for name in ("udp_host", "http_host", "db_path", "rec_dir"):
            if not isinstance(getattr(self, name), str):
                raise ConfigError(f"{name} 要是字符串，现在是 {getattr(self, name)!r}")
        port("udp_port")
        port("http_port")
        number("live_hz", 0, allow_eq=False)
        number("record_buffer_minutes", 0)
        if str(self.tire_temp_unit).upper() not in ("F", "C"):
            raise ConfigError(f'tire_temp_unit 只能是 "F" 或 "C"，现在是 {self.tire_temp_unit!r}')
        if self.game is not None and self.game not in PROFILES:
            raise ConfigError(f"game 不认识：{self.game!r}，可选：{', '.join(PROFILES)}")
        self._validate_forward()

    def _validate_forward(self) -> None:
        """forward 每一项都要是 "主机:端口"，而且不能转回本程序自己监听的 UDP 端口。"""
        if self.forward is None:
            return
        if not isinstance(self.forward, list):
            raise ConfigError(f'forward 要是列表，例如 ["127.0.0.1:20777"]，现在是 {self.forward!r}')
        self_hosts = set(_SELF_HOSTS) | {str(self.udp_host).lower()}
        try:
            from .netinfo import lan_ips
            self_hosts |= {ip.lower() for ip in lan_ips()}
        except Exception:      # 查不到局域网 IP 不影响启动，只是少查几个地址
            pass
        for item in self.forward:
            host, sep, port_s = str(item).rpartition(":")
            if not sep or not port_s.isdigit() or not 0 < int(port_s) <= 65535:
                raise ConfigError(f'forward 里的 {item!r} 格式不对，要写成 "主机:端口"，例如 "127.0.0.1:20777"')
            # 主机名不区分大小写（LOCALHOST 也是本机），IPv6 地址可能带方括号：比较前统一一下
            if int(port_s) == self.udp_port and host.strip("[]").lower() in self_hosts:
                raise ConfigError(f"forward 里的 {item!r} 指向本程序自己的 UDP 端口 {self.udp_port}："
                                  "转发出去的包会被自己再收到，无限循环。换成别的程序的端口")

    def resolve(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else ROOT / path


def load_config(path: Path | None = None) -> Config:
    cfg = Config()
    path = path or ROOT / "config.toml"
    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)
        names = {f.name for f in fields(Config)}
        for k, v in data.items():
            if k in names:
                setattr(cfg, k, v)
            else:          # 多半是拼错了：提醒一下，免得改了半天不生效
                log.warning("config.toml 里的 %r 不是已知的配置项，已忽略（可用的有：%s）", k, ", ".join(sorted(names)))
    return cfg
