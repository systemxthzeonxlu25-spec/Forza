"""全局配置。

优先级：命令行参数 > 项目根目录（打包版是 exe 所在目录）的 config.toml > 这里的默认值。
config.toml 示例见 config.example.toml。
"""
from __future__ import annotations

import ipaddress
import logging
import sys
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError

from .profiles import PROFILES

log = logging.getLogger(__name__)

if getattr(sys, "frozen", False):
    # 打包成 exe（PyInstaller）：config.toml / data / recordings 放在 exe 旁边，网页文件在解包目录里
    ROOT = Path(sys.executable).resolve().parent
    WEB_DIR = Path(getattr(sys, "_MEIPASS", ROOT)) / "web"
else:
    ROOT = Path(__file__).resolve().parent.parent
    WEB_DIR = ROOT / "web"

# 指向“本机”的地址：转发目标是这些地址 + 自己的 UDP 端口时，包会转回给自己，无限循环
_SELF_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1", ""}


def _is_self_host(host: str, self_hosts: set[str]) -> bool:
    """转发目标 host 是不是本机（包发过去会被自己的 UDP 监听收到）。

    - 主机名不区分大小写（LOCALHOST 也是本机），IPv6 地址可能带方括号：比较前统一一下；
    - IP 地址按网段判断：整个 127.0.0.0/8（127.0.0.2 也是回环）、::1、0.0.0.0 都算本机。
    不做 DNS 解析（启动时查 DNS 可能卡很久）：别名指向本机的主机名查不出来，只能防住常见写法。
    """
    h = host.strip().strip("[]").lower()
    if h in self_hosts:
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


def _is_ipv6(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip().strip("[]")).version == 6
    except ValueError:
        return False


class ConfigError(ValueError):
    """配置写得不对。message 是给用户看的中文提示（指出是哪一项、该怎么改）。"""


# ---- 各项的类型和取值范围：写在字段的类型注解里，由 pydantic 统一检查（见 Config.validate）
Port = Annotated[int, Field(ge=0, le=65535)]                      # 0 = 让系统随便挑一个空闲端口（测试用）
PositiveNumber = Annotated[float, Field(gt=0, allow_inf_nan=False)]
NonNegativeNumber = Annotated[float, Field(ge=0, allow_inf_nan=False)]   # TOML 允许写 inf / nan，这里拒绝
GameKey = Literal[tuple(PROFILES)]                                # fm7 / fm2023 / fh4 / fh5 / fh6 / fm / fh
TireUnit = Literal["F", "C", "f", "c"]

# 校验不通过时给用户看的中文要求（pydantic 自己的报错是英文）
_FIELD_HINTS = {
    "udp_host": "要是字符串（IP 地址）", "http_host": "要是字符串（IP 地址）",
    "udp_port": "要是 0~65535 的整数", "http_port": "要是 0~65535 的整数",
    "db_path": "要是字符串（文件路径）", "rec_dir": "要是字符串（文件夹路径）",
    "game": f"只能是 {' / '.join(PROFILES)} 之一（或者不写）",
    "tire_temp_unit": '只能是 "F" 或 "C"',
    "live_hz": "要是 > 0 的数字", "record_buffer_minutes": "要是 ≥ 0 的数字（0 = 关闭）",
    "forward": '要是字符串列表，例如 ["127.0.0.1:20777"]',
}


@dataclass
class Config:
    # strict：不做类型转换（"5555" 不会被悄悄转成 5555、true 不会被当成 1），写错类型直接报错；
    # revalidate_instances：validate() 传进去的是 Config 实例本身，要求每次都把各字段重新检查一遍
    __pydantic_config__ = ConfigDict(strict=True, revalidate_instances="always")

    udp_host: str = "0.0.0.0"
    # 默认 5555：FH6 官方文档要求避开 5200~5300（游戏自己占用），5555 对所有游戏都安全
    udp_port: Port = 5555
    http_host: str = "0.0.0.0"   # 0.0.0.0 才能让局域网里的手机访问
    http_port: Port = 8000
    db_path: str = "data/forza.db"
    rec_dir: str = "recordings"
    game: GameKey | None = None  # 强制指定游戏：fm7 / fm2023 / fh4 / fh5 / fh6
    tire_temp_unit: TireUnit = "F"   # 游戏发来的胎温单位，社区共识是 °F
    live_hz: PositiveNumber = 20.0   # 实时面板推送频率（手机上 20Hz 足够流畅）
    forward: list[str] | None = None  # 原样转发原始包到其他程序，如 ["127.0.0.1:20777"]
    # 内存里常驻保留最近几分钟的原始包，实时面板菜单里可以“保存刚才 N 分钟”（0 = 关闭）。
    # 每分钟大约占 1.2 MB（FM 60 包/秒）~ 2.8 MB（FH 高帧率约 140 包/秒）
    record_buffer_minutes: NonNegativeNumber = 10.0

    def validate(self) -> None:
        """检查各项的类型和取值范围，不对就抛 ConfigError。

        config.toml 里写错类型（例如 udp_port = "5555" 写成了字符串）时，
        与其等到监听端口时才报一句看不懂的英文错误，不如启动时就指出是哪一项。
        类型 / 范围按上面字段注解由 pydantic 检查（只校验，不改值）；转发目标另外检查。
        """
        try:
            _ADAPTER.validate_python(self)
        except ValidationError as e:
            loc = e.errors()[0]["loc"]
            name = str(loc[0]) if loc else "config"
            raise ConfigError(f"{name} {_FIELD_HINTS.get(name, '写得不对')}，现在是 {getattr(self, name, None)!r}") from None
        self._validate_forward()

    def _validate_forward(self) -> None:
        """forward 每一项都要是 "主机:端口"，而且不能转回本程序自己监听的 UDP 端口。"""
        if self.forward is None:      # 是不是字符串列表已经由字段注解检查过了
            return
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
            if int(port_s) == self.udp_port and _is_self_host(host, self_hosts):
                raise ConfigError(f"forward 里的 {item!r} 指向本程序自己的 UDP 端口 {self.udp_port}："
                                  "转发出去的包会被自己再收到，无限循环。换成别的程序的端口")
            if _is_ipv6(host):
                # Hub 的转发 socket 是 IPv4 的，发 IPv6 地址每次都会失败（还被静默吞掉），看起来配好了其实什么都没发
                raise ConfigError(f"forward 里的 {item!r} 是 IPv6 地址，转发暂时只支持 IPv4（例如 127.0.0.1:20777）")

    def resolve(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else ROOT / path


_ADAPTER = TypeAdapter(Config)


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
