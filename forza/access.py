"""局域网访问控制：挡住 DNS 重绑定，以及同一个 Wi-Fi 里别人的手机乱改数据。

两道检查，都在 AccessGuard 这个 ASGI 中间件里做（HTTP 和 WebSocket 都管）：

1. Host 白名单（防 DNS 重绑定）
   恶意网页可以把自己的域名临时解析到 127.0.0.1 / 本机局域网 IP，这样浏览器认为是“同源”，
   Origin 和 Host 都是它的域名，同源检查拦不住，它就能读圈数据、发修改请求。
   但这种攻击的 Host 头一定是攻击者能控制 DNS 的公网域名，所以只放行：
   IP 地址、localhost、不带点的机器名（DESKTOP-ABC）、.local / .localhost 结尾的名字，
   以及 config.toml 里 allowed_hosts 额外列出的名字。

2. 局域网设备改数据要钥匙（lan_access = "key"，默认）
   本机（127.0.0.1 / ::1）发来的请求随便改；局域网里的设备只能看，
   POST / PUT / PATCH / DELETE 要带上 cookie 里的钥匙。钥匙随机生成、存在数据库旁边的 access_key.txt，
   重启后不变。手机用“连接手机”页的二维码打开一次（网址带 ?key=...），服务端把钥匙写进 cookie，
   以后这台手机就一直有修改权限。lan_access = "open" 时不检查（旧行为）。
   同时保留原来的跨站请求伪造检查：带 Origin 的写请求，Origin 要和 Host 一致。
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import secrets
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

log = logging.getLogger(__name__)

KEY_FILE = "access_key.txt"
KEY_PARAM = "key"              # 网址里带钥匙的参数名：http://192.168.1.23:8000/?key=...
COOKIE = "forza_key"
COOKIE_MAX_AGE = 400 * 24 * 3600   # 浏览器允许的最长有效期（约 400 天）
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# 当成“本机”的客户端地址（除了回环 IP 以外）。测试里 TestClient 的客户端叫 testclient，conftest 里会加进来
LOCAL_CLIENTS: set[str] = set()


def load_access_key(folder: Path) -> str:
    """读出（没有就生成）局域网修改钥匙。读写失败时退回本次运行临时的钥匙，不影响启动。"""
    path = folder / KEY_FILE
    try:
        key = path.read_text(encoding="utf-8").strip()
        if len(key) >= 16:
            return key
    except OSError:
        pass
    key = secrets.token_urlsafe(18)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(key + "\n", encoding="utf-8")
    except OSError as e:
        log.warning("局域网修改钥匙存不下来（%s），这次运行先用临时钥匙，重启后手机要重新扫码", e)
    return key


def _strip_port(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):                       # [::1]:8000
        return host[1:host.find("]")] if "]" in host else host[1:]
    if host.count(":") == 1:                       # 192.168.1.23:8000 / localhost:8000
        return host.rsplit(":", 1)[0]
    return host                                    # 不带方括号的 IPv6，或者没有端口


def host_allowed(host_header: str, extra: set[str] | frozenset[str] = frozenset()) -> bool:
    """Host 头是不是“本机 / 局域网”的写法（见模块说明第 1 条）。"""
    h = _strip_port(host_header).rstrip(".")
    if not h:
        return False
    if h in extra:
        return True
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    return h == "localhost" or "." not in h or h.endswith(".local") or h.endswith(".localhost")


def is_local_client(client_host: str | None) -> bool:
    if not client_host:
        return False
    if client_host in LOCAL_CLIENTS:
        return True
    try:
        ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:         # ::ffff:127.0.0.1
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _headers(scope) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in scope.get("headers") or ():
        out.setdefault(k.decode("latin-1").lower(), v.decode("latin-1"))
    return out


def _cookie(headers: dict[str, str], name: str) -> str | None:
    raw = headers.get("cookie")
    if not raw:
        return None
    try:
        c = SimpleCookie()
        c.load(raw)
    except Exception:
        return None
    m = c.get(name)
    return m.value if m else None


def _same(a: str | None, b: str) -> bool:
    return bool(a) and hmac.compare_digest(a.encode(), b.encode())


class AccessGuard:
    """ASGI 中间件，规则见模块说明。get_settings() 返回 (钥匙, lan_access, 额外放行的主机名)。"""

    def __init__(self, app, get_settings):
        self.app = app
        self.get_settings = get_settings

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        key, mode, extra = self.get_settings()
        headers = _headers(scope)
        host = headers.get("host", "")

        if not host_allowed(host, extra):
            return await self._deny(scope, send, 403,
                                    f"不认识的主机名 {_strip_port(host)!r}：请用 IP 地址或 localhost 打开；"
                                    "确实要用这个名字的话，把它加进 config.toml 的 allowed_hosts")

        if scope["type"] == "websocket":      # 实时推送只读，过了 Host 检查就行
            return await self.app(scope, receive, send)

        method = scope.get("method", "GET")
        client = (scope.get("client") or (None, None))[0]
        local = is_local_client(client)

        if method in WRITE_METHODS:
            origin = headers.get("origin")
            if origin and urlsplit(origin).netloc.lower() != host.lower():
                return await self._deny(scope, send, 403, "不接受来自其他网站的修改请求")
            if mode == "key" and not local and not _same(_cookie(headers, COOKIE), key):
                return await self._deny(scope, send, 403,
                                        "这台设备还没有修改权限：在电脑上打开“连接手机”页，用手机扫一次二维码就行")
            return await self.app(scope, receive, send)

        # 打开带 ?key=... 的网址：钥匙对就写进 cookie，再跳回去掉钥匙的网址（免得钥匙留在地址栏里被截图）
        if method == "GET" and scope.get("query_string"):
            params = parse_qsl(scope["query_string"].decode("latin-1"), keep_blank_values=True)
            given = [v for k, v in params if k == KEY_PARAM]
            if given:
                rest = urlencode([(k, v) for k, v in params if k != KEY_PARAM])
                location = scope.get("path", "/") + (f"?{rest}" if rest else "")
                extra_headers = []
                if _same(given[-1], key):
                    extra_headers.append((b"set-cookie", (
                        f"{COOKIE}={key}; Path=/; Max-Age={COOKIE_MAX_AGE}; HttpOnly; SameSite=Strict"
                    ).encode()))
                await send({"type": "http.response.start", "status": 303,
                            "headers": [(b"location", location.encode()), (b"cache-control", b"no-store"),
                                        (b"content-length", b"0"), *extra_headers]})
                await send({"type": "http.response.body", "body": b""})
                return
        return await self.app(scope, receive, send)

    @staticmethod
    async def _deny(scope, send, status: int, detail: str):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        import json
        body = json.dumps({"detail": detail}, ensure_ascii=False).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def can_write(request) -> bool:
    """这个请求的发起设备能不能改数据（网页上据此决定提示要不要扫码）。"""
    rt = request.app.state.rt
    if rt.cfg.lan_access != "key":
        return True
    client = request.client.host if request.client else None
    return is_local_client(client) or _same(request.cookies.get(COOKIE), rt.access_key)
