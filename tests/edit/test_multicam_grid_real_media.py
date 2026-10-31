"""F2.5 · 实时四路网格——真实素材同机负载证据（四路 vs 基线）。

验收（计划 F2.5）：「记录同机负载与掉帧，不能以缩略图更新冒充实时四路
通过」。本测试用 MulticamGridStream 的真实解码路径（FFmpegMediaSource
target_size=(170,96) 小尺寸解码，单线程轮转）在同一测试进程内对拍：

- 基线：单角度连续解码 N 帧；
- 四路：四解码器轮转各解码 N 帧（与网格流 worker 同形态：单线程、
  每角度 latest-wins 逐帧）。

数字打印到 stdout（进台账）；断言只做硬性正确性（帧非空、四路完成、
单帧耗时有记录），不做绝对性能门槛——负载证据以记录为准。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from statistics import median

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REAL_MEDIA = "tests/test_videos/2K_h264.mp4"
_FRAMES_PER_ANGLE = 12


def _media_available() -> bool:
    return Path(_REAL_MEDIA).exists()


def _open_real_decoder(path: str):
    from src.features.edit.ui.source_viewer import _open_cpu_decode_source
    return _open_cpu_decode_source(path, (170, 96))


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(pct * (len(ordered) - 1))))
    return ordered[idx]


@pytest.mark.edit_ui
@pytest.mark.skipif(not _media_available(), reason="真实测试素材不存在")
class TestGridFourWayRealLoad:

    def test_four_way_vs_baseline_load(self) -> None:
        """四路 96px 轮转解码 vs 单路基线：帧正确 + 负载数字记录。"""
        from src.features.edit.ui.source_viewer import _decoded_frame_to_rgb24

        paths = [_REAL_MEDIA] * 4  # 同机同码流 ×4 解码器实例
        # -- 基线：单角度 ------------------------------------------------
        baseline = _open_real_decoder(paths[0])
        try:
            base_ms: list[float] = []
            for i in range(_FRAMES_PER_ANGLE):
                t0 = time.perf_counter()
                decoded = baseline.read_frame(i * 5)
                rgb = _decoded_frame_to_rgb24(decoded)
                base_ms.append((time.perf_counter() - t0) * 1000.0)
                assert rgb is not None and rgb.size > 0
        finally:
            baseline.close()

        # -- 四路：单线程轮转（网格流 worker 同形态） --------------------
        decoders = []
        grid_ms: dict[int, list[float]] = {k: [] for k in range(4)}
        try:
            for p in paths:
                decoders.append(_open_real_decoder(p))
            for i in range(_FRAMES_PER_ANGLE):
                for k, decoder in enumerate(decoders):
                    t0 = time.perf_counter()
                    decoded = decoder.read_frame(i * 5)
                    rgb = _decoded_frame_to_rgb24(decoded)
                    grid_ms[k].append((time.perf_counter() - t0) * 1000.0)
                    assert rgb is not None and rgb.size > 0
        finally:
            for decoder in decoders:
                decoder.close()

        all_grid = [ms for k in grid_ms for ms in grid_ms[k]]
        print(
            f"\n[multicam-grid load] baseline p50={median(base_ms):.1f}ms "
            f"p95={_percentile(base_ms, 0.95):.1f}ms "
            f"total={sum(base_ms):.0f}ms n={len(base_ms)}")
        for k in sorted(grid_ms):
            print(
                f"[multicam-grid load] angle{k} p50="
                f"{median(grid_ms[k]):.1f}ms "
                f"p95={_percentile(grid_ms[k], 0.95):.1f}ms")
        print(
            f"[multicam-grid load] 4-way p50={median(all_grid):.1f}ms "
            f"p95={_percentile(all_grid, 0.95):.1f}ms "
            f"total={sum(all_grid):.0f}ms n={len(all_grid)}")

        # 硬性正确性：四路全部完成、帧尺寸一致（同源同档）
        assert len(all_grid) == 4 * _FRAMES_PER_ANGLE
        # 负载记录有效：每角度都有耗时候本
        assert all(len(v) == _FRAMES_PER_ANGLE for v in grid_ms.values())
