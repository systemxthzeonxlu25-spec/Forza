"""局域网访问控制：Host 白名单（防 DNS 重绑定）、局域网设备改数据要钥匙、跨站写请求。"""
import pytest
from fastapi.testclient import TestClient

from forza.access import COOKIE, host_allowed, is_local_client, load_access_key
from forza.app import create_app
from forza.runtime import Runtime

LAN = ("192.168.1.50", 50000)


@pytest.fixture
def rt(cfg):
    r = Runtime(cfg)
    yield r
    r.shutdown()


@pytest.mark.parametrize("host,ok", [
    ("localhost:8000", True), ("127.0.0.1:8000", True), ("192.168.1.23:8000", True), ("[::1]:8000", True),
    ("DESKTOP-ABC:8000", True), ("mypc.local:8000", True), ("testserver", True),
    ("evil.example.com:8000", False), ("attacker.com", False), ("", False),
])
def test_host_allowed(host, ok):
    assert host_allowed(host) is ok


def test_host_allowed_extra():
    assert host_allowed("mypc.lan:8000", {"mypc.lan"})


def test_is_local_client():
    assert is_local_client("127.0.0.1") and is_local_client("::1") and is_local_client("::ffff:127.0.0.1")
    assert not is_local_client("192.168.1.50") and not is_local_client(None)


def test_key_persisted(tmp_path):
    k = load_access_key(tmp_path)
    assert len(k) >= 16 and load_access_key(tmp_path) == k


def test_dns_rebinding_host_rejected(rt):
    c = TestClient(create_app(rt), base_url="http://evil.example.com:8000")
    assert c.get("/api/status").status_code == 403
    assert c.get("/").status_code == 403
    with pytest.raises(Exception):
        with c.websocket_connect("/ws/live", headers={"host": "evil.example.com:8000"}):
            pass


def test_allowed_hosts_config(cfg):
    cfg.allowed_hosts = ["MyPC.lan"]
    r = Runtime(cfg)
    try:
        c = TestClient(create_app(r), base_url="http://mypc.lan:8000")
        assert c.get("/api/status").status_code == 200
    finally:
        r.shutdown()


def test_lan_device_read_only_until_key(rt):
    c = TestClient(create_app(rt), client=LAN)
    st = c.get("/api/status").json()
    assert st["can_write"] is False and "access_key" not in st      # 局域网设备拿不到钥匙
    assert c.get("/api/laps").status_code == 200                     # 看是可以看的
    r = c.post("/api/record/stop")
    assert r.status_code == 403 and "扫" in r.json()["detail"]

    # 错的钥匙：跳转但不给 cookie
    r = c.get("/?key=wrong", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/" and "set-cookie" not in r.headers
    assert c.post("/api/record/stop").status_code == 403

    # 扫码打开带钥匙的网址：拿到 cookie，跳回去掉钥匙的网址（其他参数保留）
    r = c.get(f"/laps.html?a=1&key={rt.access_key}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/laps.html?a=1"
    assert COOKIE in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
    assert c.get("/api/status").json()["can_write"] is True
    assert c.post("/api/record/stop").status_code != 403


def test_local_gets_key(rt):
    c = TestClient(create_app(rt), client=("127.0.0.1", 50000))
    st = c.get("/api/status").json()
    assert st["can_write"] is True and st["access_key"] == rt.access_key


def test_lan_access_open(cfg):
    cfg.lan_access = "open"
    r = Runtime(cfg)
    try:
        c = TestClient(create_app(r), client=LAN)
        assert c.get("/api/status").json()["can_write"] is True
        assert c.post("/api/record/stop").status_code != 403
    finally:
        r.shutdown()


def test_cross_site_write_rejected(rt):
    c = TestClient(create_app(rt))
    assert c.post("/api/record/stop", headers={"Origin": "http://evil.example.com"}).status_code == 403
    assert c.post("/api/record/stop", headers={"Origin": "http://testserver"}).status_code != 403
