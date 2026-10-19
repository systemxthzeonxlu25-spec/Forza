"""服务骨架：Runtime 组装、/api/status、静态网页。"""
from fastapi.testclient import TestClient

from forza.app import create_app
from forza.packet import encode
from forza.runtime import Runtime
from tests.helpers import CircleStream


def test_status_and_pipeline(cfg):
    rt = Runtime(cfg)
    s = CircleStream()
    for f in s.run_dist(s.lap_len * 1.6):
        rt.hub.feed(encode(f, "fm2023_dash"))
    rt.shutdown()   # 收尾：落盘挂起的圈
    client = TestClient(create_app(rt))
    st = client.get("/api/status").json()
    assert st["format"] == "fm2023_dash" and st["game"] == "fm2023"
    assert st["packets"] > 0 and st["udp_port"] == cfg.udp_port
    assert rt.store.list_laps()[0]["lap_number"] == 0
    assert client.get("/common.js").status_code == 200
