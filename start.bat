@echo off
chcp 65001 >nul
rem 注意：cmd 里丢弃输出要写 nul，不是 /dev/null（写错的话重定向失败，py 那条命令根本不会执行）
rem 双击启动：第一次会自动创建虚拟环境并安装依赖；额外参数会传给 serve，例如 start.bat --game fh6 --record
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 第一次运行：正在创建虚拟环境并安装依赖……
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo 没找到 Python，请先安装 Python 3.11 或更新版本，并勾选 Add python.exe to PATH
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
".venv\Scripts\python.exe" -m forza serve %*
pause
