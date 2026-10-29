"""测试公用夹具。"""
import pytest

from forza.config import Config


@pytest.fixture
def cfg(tmp_path) -> Config:
    """指向临时目录的配置（数据库、录像都写到 tmp_path，不污染项目目录）。"""
    return Config(db_path=str(tmp_path / "test.db"), rec_dir=str(tmp_path / "rec"), live_hz=50)


@pytest.fixture(autouse=True)
def _testclient_is_local(monkeypatch):
    """TestClient 发请求的客户端地址叫 testclient：当成本机，原有接口测试照常能改数据。

    局域网设备的权限检查在 test_access.py 里单独用 client=(局域网 IP, 端口) 测。
    """
    from forza import access
    monkeypatch.setattr(access, "LOCAL_CLIENTS", {"testclient"})
