"""假遥测数据生成器（不开游戏也能开发、测试所有下游模块）。

    from forza.fake import generate, write_fake_recording
    res = generate("circuit", laps=5)            # [(t, Frame), ...] + truth（标准答案）
    write_fake_recording("samples/demo.frec")    # 写 .frec 录像 + demo.truth.json

赛道几何见 track.py，车 / 司机 / 场景 / 埋的问题见 generator.py。
"""
from .generator import FakeResult, generate, truth_path_for, write_fake_recording
from .track import Track, build_circuit, build_sprint_route

__all__ = ["FakeResult", "generate", "write_fake_recording", "truth_path_for", "Track", "build_circuit",
           "build_sprint_route"]
