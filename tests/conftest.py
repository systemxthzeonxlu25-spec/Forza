"""测试公用夹具。"""
import pytest

from forza.config import Config


@pytest.fixture
def cfg(tmp_path) -> Config:
    """指向临时目录的配置（数据库、录像都写到 tmp_path，不污染项目目录）。"""
    return Config(db_path=str(tmp_path / "test.db"), rec_dir=str(tmp_path / "rec"), live_hz=50)
