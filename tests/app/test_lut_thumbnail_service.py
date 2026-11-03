"""R07: LUT 缩略图后台任务（DTO/缓存键/世代守卫/失败可见/预算与 LRU）。

验收（计划 R07）：缓存键不按文件名（同内容不同路径命中）；过期世代不
投递；失败码可见（占位保持）；在途预算 + 有界 LRU；取消丢弃在途。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication

from src.app.controllers.lut_thumbnail_service import LutThumbnailService


def _identity_cube(size: int = 2, swap: bool = False) -> str:
    lines = [f"LUT_3D_SIZE {size}"]
    for b in range(size):
        for g in range(size):
            for r in range(size):
                v = [r / (size - 1), g / (size - 1), b / (size - 1)]
                if swap:
                    v = v[::-1]
                lines.append(" ".join(str(x) for x in v))
    return "\n".join(lines) + "\n"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _Recorder:
    def __init__(self) -> None:
        self.ready: list[tuple[str, QImage]] = []
        self.failed: list[tuple[str, str]] = []
        self.provider_calls = 0
        self.source = np.linspace(0.05, 0.95, 16 * 9, dtype=np.float32) \
            .reshape(9, 16, 1).repeat(3, axis=-1)

    def provider(self) -> np.ndarray:
        self.provider_calls += 1
        return self.source

    def on_ready(self, entry_id: str, image: QImage) -> None:
        self.ready.append((entry_id, image))

    def on_failed(self, entry_id: str, code: str) -> None:
        self.failed.append((entry_id, code))


def _make_service(recorder: _Recorder, **kw) -> LutThumbnailService:
    service = LutThumbnailService(source_provider=recorder.provider, **kw)
    service.thumbnail_ready.connect(recorder.on_ready)
    service.thumbnail_failed.connect(recorder.on_failed)
    return service


def _wait(recorder: _Recorder, *, count: int, timeout: float = 5.0) -> bool:
    import time
    deadline = time.monotonic() + timeout
    app = QApplication.instance()
    while time.monotonic() < deadline:
        app.processEvents()
        if len(recorder.ready) + len(recorder.failed) >= count:
            app.processEvents()
            return True
        time.sleep(0.01)
    return False


def test_same_content_different_names_share_cache(qtbot: Any, tmp_path: Path) -> None:
    first = tmp_path / "a" / "one.cube"
    first.parent.mkdir()
    first.write_text(_identity_cube(swap=True))
    second = tmp_path / "b" / "two.cube"
    second.parent.mkdir()
    second.write_text(_identity_cube(swap=True))  # 同内容不同路径
    recorder = _Recorder()
    service = _make_service(recorder)
    try:
        service.set_source_identity("s", "stack", 0, 1)
        service.request("e1", str(first))
        assert _wait(recorder, count=1)
        assert len(recorder.ready) == 1
        provider_calls_after_first = recorder.provider_calls
        # 同内容不同文件名：命中缓存（不再评估、ready 即刻来自缓存）
        service.request("e2", str(second))
        QApplication.instance().processEvents()
        assert len(recorder.ready) == 2
        assert recorder.provider_calls == provider_calls_after_first
    finally:
        service.shutdown()


def test_stale_generation_drops_late_result(qtbot: Any, tmp_path: Path) -> None:
    lut = tmp_path / "swap.cube"
    lut.write_text(_identity_cube(swap=True))
    recorder = _Recorder()
    service = _make_service(recorder)
    try:
        service.set_source_identity("s", "stack", 0, 1)
        service.request("e1", str(lut))
        # 评估前切 clip（世代 +1）：晚到结果不得投递
        service.set_source_identity("s2", "stack2", 0, 2)
        assert not _wait(recorder, count=1, timeout=1.0)
        assert recorder.ready == [] and recorder.failed == []
    finally:
        service.shutdown()


def test_failure_is_visible_and_placeholder_kept(qtbot: Any,
                                                 tmp_path: Path) -> None:
    broken = tmp_path / "broken.cube"
    broken.write_text("LUT_3D_SIZE 4\n0 0 0\n")
    recorder = _Recorder()
    service = _make_service(recorder)
    try:
        service.set_source_identity("s", "stack", 0, 1)
        service.request("bad", str(broken))
        assert _wait(recorder, count=1)
        assert recorder.ready == []
        assert recorder.failed and recorder.failed[0][0] == "bad"
    finally:
        service.shutdown()


def test_pending_budget_and_cancel_all(qtbot: Any, tmp_path: Path) -> None:
    import time as _time
    luts = []
    for i in range(6):
        path = tmp_path / f"lut{i}.cube"
        path.write_text(_identity_cube(swap=i % 2 == 0))
        luts.append(path)
    recorder = _Recorder()

    def slow_provider():
        recorder.provider_calls += 1
        _time.sleep(0.05)  # 保证取消落在评估在途期间（非竞态）
        return recorder.source

    service = LutThumbnailService(source_provider=slow_provider, max_pending=2)
    service.thumbnail_ready.connect(recorder.on_ready)
    service.thumbnail_failed.connect(recorder.on_failed)
    try:
        service.set_source_identity("s", "stack", 0, 1)
        accepted = [service.request(f"e{i}", str(p)) for i, p in enumerate(luts)]
        # 预算 2：超出的请求被拒（None）
        assert accepted[0] and accepted[1]
        assert accepted[2] is None and accepted[5] is None
        service.cancel_all()  # 在途（评估中）被取消——零投递
        assert not _wait(recorder, count=1, timeout=1.0)
        assert recorder.ready == []
    finally:
        service.shutdown()


def test_lru_cache_bound(qtbot: Any, tmp_path: Path) -> None:
    recorder = _Recorder()
    service = _make_service(recorder, cache_size=3, max_pending=16)
    try:
        for generation in range(5):
            service.set_source_identity("s", "stack", 0, generation)
            lut = tmp_path / f"g{generation}.cube"
            lut.write_text(_identity_cube(swap=generation % 2 == 0))
            service.request(f"e{generation}", str(lut))
            assert _wait(recorder, count=generation + 1)
        assert service.stats()["cached"] <= 3
    finally:
        service.shutdown()


def test_delivered_image_matches_swap_lut(qtbot: Any, tmp_path: Path) -> None:
    lut = tmp_path / "swap.cube"
    lut.write_text(_identity_cube(swap=True))
    recorder = _Recorder()
    # 源通道不对称（R≠B），swap LUT 后图像必须变化
    recorder.source = np.stack(
        [np.full((9, 16), 0.8), np.full((9, 16), 0.5),
         np.full((9, 16), 0.2)], axis=-1).astype(np.float32)
    service = _make_service(recorder)
    try:
        service.set_source_identity("s", "stack", 0, 1)
        service.request("e1", str(lut))
        assert _wait(recorder, count=1)
        entry_id, image = recorder.ready[0]
        assert entry_id == "e1" and image.width() == 16 and image.height() == 9
        # 采样中心像素：swap 后 R/B 互换（0.8→0.2 量级）
        color = image.pixelColor(8, 4)
        assert color.red() < color.blue()
    finally:
        service.shutdown()
