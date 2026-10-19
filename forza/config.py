"""全局配置。

优先级：命令行参数 > 项目根目录的 config.toml > 这里的默认值。
config.toml 示例见 config.example.toml。
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"


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
    return cfg
