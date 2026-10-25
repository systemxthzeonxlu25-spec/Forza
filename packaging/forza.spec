# PyInstaller 打包配置：pyinstaller packaging/forza.spec（在仓库根目录跑）
# 出来的是 dist/Forza/ 文件夹（onedir：启动快，也不容易被杀毒软件误报），里面双击 Forza.exe。
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent

datas = [
    (str(ROOT / "web"), "web"),
    (str(ROOT / "forza" / "data"), "forza/data"),
    (str(ROOT / "forza" / "tuning_rules.toml"), "forza"),
]
# uvicorn 按名字动态加载协议 / 事件循环实现，静态分析找不到
hidden = collect_submodules("uvicorn") + collect_submodules("forza")

a = Analysis(
    [str(ROOT / "packaging" / "forza_exe.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "pytest", "matplotlib", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Forza",
    console=True,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Forza", upx=False)
