"""打包版入口（packaging/forza_exe.py）：双击 / 带参数时转给 python -m forza 的参数。"""
import importlib.util
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("forza_exe", Path(__file__).parent.parent / "packaging" / "forza_exe.py")
forza_exe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(forza_exe)


@pytest.mark.parametrize("args, expected", [
    ([], ["serve"]),                                   # 双击
    (["--record"], ["serve", "--record"]),            # Forza.exe --record
    (["-v", "--game", "fh6"], ["-v", "serve", "--game", "fh6"]),
    (["import", "a.frec"], ["import", "a.frec"]),
    (["--help"], ["--help"]),
    (["--no-pause", "--record"], ["serve", "--record"]),
])
def test_argv(monkeypatch, args, expected):
    got = []
    monkeypatch.setattr(forza_exe, "main", lambda argv: got.append(argv) or 0)
    monkeypatch.setattr(sys, "argv", ["Forza.exe", *args])
    monkeypatch.setattr(sys, "stdin", None)            # 不停下来等回车
    assert forza_exe.run() == 0
    assert got == [expected]


def test_unexpected_error_is_printed_not_raised(monkeypatch, capsys):
    """双击运行时 config.toml 写坏之类的异常：打印出来、返回 1（然后停住等回车），不是直接闪退。"""
    def boom(argv):
        raise ValueError("config.toml 写坏了")
    monkeypatch.setattr(forza_exe, "main", boom)
    monkeypatch.setattr(sys, "argv", ["Forza.exe"])
    monkeypatch.setattr(sys, "stdin", None)
    assert forza_exe.run() == 1
    assert "config.toml 写坏了" in capsys.readouterr().err
