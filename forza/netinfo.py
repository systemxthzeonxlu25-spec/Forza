"""本机局域网 IP，用来告诉你手机该打开哪个地址、游戏里该填哪个 IP。"""
from __future__ import annotations

import socket


def lan_ips() -> list[str]:
    ips: list[str] = []
    try:
        # UDP connect 不会真的发包，只是让系统选出“默认出口网卡”的 IP
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]
