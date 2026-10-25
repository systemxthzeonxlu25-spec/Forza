"""打包版（Forza.exe）的入口：双击不带参数 = serve；带参数和 python -m forza 一样。

窗口是控制台程序，退出（或出错）时停一下，免得双击运行的人看不到提示就闪退。
"""
import sys

from forza.__main__ import main


def run() -> int:
    argv = [a for a in sys.argv[1:] if a != "--no-pause"]
    lead = [a for a in argv[:1] if a in ("-v", "--verbose")]
    rest = argv[len(lead):]
    if not rest or rest[0].startswith("-"):  # 双击 / Forza.exe --record 这种：参数都是给 serve 的
        if rest[:1] not in (["-h"], ["--help"]):
            rest = ["serve", *rest]
    argv = lead + rest
    code = 1
    try:
        code = main(argv) or 0
    except SystemExit as e:                  # argparse 的 --help / 参数错误
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except KeyboardInterrupt:
        code = 0
    if sys.stdin and sys.stdin.isatty() and "--no-pause" not in sys.argv:
        try:
            input("\n按回车关闭窗口……")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


if __name__ == "__main__":
    sys.exit(run())
