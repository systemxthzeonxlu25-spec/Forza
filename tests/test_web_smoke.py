"""网页冒烟测试：真的起一个服务（回放假数据），用无头 Chromium 打开每个页面。

查的是 Python 测试覆盖不到的前端：页面脚本有没有报错、实时面板有没有跟着数据动、
圈速页能不能列出圈并画出对比图、“连接手机”页的二维码有没有带上修改钥匙。

需要 playwright（pip install playwright && playwright install chromium）。没装时跳过；
CI 的网页测试任务设置了 FORZA_WEB_TESTS=1，装不上 / 起不来就直接失败，不会悄悄跳过。
浏览器不在 playwright 默认位置时，用 FORZA_CHROMIUM 指定可执行文件路径。
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

REQUIRED = os.environ.get("FORZA_WEB_TESTS") == "1"
if not REQUIRED:
    pytest.importorskip("playwright.sync_api", reason="没装 playwright，跳过网页冒烟测试")
from playwright.sync_api import Error as PwError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PAGES = ["/", "/laps.html", "/tuning.html", "/car.html", "/connect.html"]


def _free_port(kind=socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("web")
    rec = tmp / "smoke.frec"
    subprocess.run([sys.executable, "-m", "forza", "fake", "-o", str(rec), "--laps", "3"],
                   cwd=ROOT, check=True, capture_output=True)
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "forza", "serve", "--http-host", "127.0.0.1", "--http-port", str(port),
         "--udp-port", str(_free_port(socket.SOCK_DGRAM)), "--db", str(tmp / "forza.db"),
         "--replay", str(rec), "--speed", "30", "--loop"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            if proc.poll() is not None:
                raise RuntimeError(f"服务没起来：{proc.stderr.read().decode(errors='replace')}")
            try:
                urllib.request.urlopen(base + "/api/status", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError("服务 20 秒内没响应")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        exe = os.environ.get("FORZA_CHROMIUM") or None
        try:
            b = p.chromium.launch(executable_path=exe)
        except PwError as e:
            if REQUIRED:
                raise
            pytest.skip(f"Chromium 起不来（{e.message.splitlines()[0]}），跳过网页冒烟测试")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 412, "height": 915})   # 手机尺寸：实时面板主要在手机上看
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    # 404 是接口约定的“还没有数据”（比如还没算出功率曲线），页面会自己处理，浏览器照样会打一条
    # “Failed to load resource”：不算错误；5xx 才算
    pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
          if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
    pg.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 500 else None)
    pg.errors = errors
    yield pg
    ctx.close()


def _wait_laps(base: str, n: int, timeout: float = 60) -> None:
    import json
    end = time.time() + timeout
    while time.time() < end:
        st = json.loads(urllib.request.urlopen(base + "/api/status", timeout=2).read())
        if st.get("saved_count", 0) >= n:
            return
        time.sleep(0.5)
    raise AssertionError(f"{timeout} 秒内没存够 {n} 圈")


@pytest.mark.parametrize("path", PAGES)
def test_page_loads_without_errors(server, page, path):
    page.goto(server + path)
    page.wait_for_selector("nav.topnav")
    page.wait_for_timeout(1500)                 # 让首轮请求 / WebSocket 消息跑完
    assert page.errors == []
    # 手机宽度下不能横向滚动
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


def test_live_panel_follows_data(server, page):
    page.goto(server + "/")
    speed = page.locator("#speed")
    page.wait_for_function("() => /\\d/.test(document.querySelector('#speed').textContent)", timeout=15000)
    seen = {speed.text_content()}
    for _ in range(20):
        page.wait_for_timeout(250)
        seen.add(speed.text_content())
    assert len(seen) > 2, f"车速没在变：{seen}"
    assert page.errors == []


def test_laps_page_lists_and_compares(server, page):
    _wait_laps(server, 2)
    page.goto(server + "/laps.html")
    page.wait_for_selector("#lap-table tr[data-id]", timeout=15000)
    assert page.locator("#lap-table tr[data-id]").count() >= 2
    # 默认会选两圈对比，画出叠加曲线（uPlot 的 canvas）
    page.wait_for_selector("#charts canvas", timeout=15000)
    assert page.errors == []


def test_connect_page_qr_carries_key(server, page):
    page.goto(server + "/connect.html")
    page.wait_for_selector("#urls svg, #urls .warn", timeout=10000)
    links = [a.get_attribute("href") for a in page.locator("#urls a").all()]
    if links:      # 跑测试的机器可能没有局域网 IP，那就只有提示
        assert all(re.search(r"\?key=[\w-]{16,}$", u) for u in links), links
        assert "key=" not in page.locator("#urls").inner_text()     # 显示出来的地址不带钥匙
    assert page.errors == []
