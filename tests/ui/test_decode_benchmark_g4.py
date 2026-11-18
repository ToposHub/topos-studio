"""G4：decode_benchmark 接管——G 节行验收。

**来源/时间线上下文、预算、取消、零素材写入；不借旧 bridge**：
直接加载既有 host 实现（benchmark.py/benchmark_gui.py——PyAV 抽帧 +
帧边界协作取消 + BenchmarkConfig 预算档位），不重写第二套跑分。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from src.features.files.decode_benchmark_service import (
    DecodeBenchmarkHostService,
    load_benchmark_module,
)


# ---------------------------------------------------------------------------
# 直接接既有 host service（不重写、不走旧 bridge）
# ---------------------------------------------------------------------------

def test_loads_existing_host_implementation_not_a_copy():
    module = load_benchmark_module()
    # 既有实现的全部能力都在（同一套，不是第二套）
    for name in ("DecodeBenchmarkWindow", "BenchmarkWorker"):
        assert hasattr(module, name)
    core = __import__("importlib", fromlist=["util"]).import_module(
        "src.features.files.benchmark.benchmark")
    for name in ("DecodeBenchmarkRunner", "BenchmarkConfig", "BenchmarkResult",
                 "SUPPORTED_CODECS", "export_results_json",
                 "export_results_csv"):
        assert hasattr(core, name)


def test_load_is_cached_and_namespaced():
    first = load_benchmark_module()
    second = load_benchmark_module()
    assert first is second                      # 单次加载（包注册复用）
    assert first.__name__.startswith("src.features.files.benchmark.")


def test_host_service_creates_window_with_paths(qtbot, tmp_path):
    service = DecodeBenchmarkHostService()
    window = service.create_window(None, ())
    qtbot.addWidget(window)
    assert window.windowTitle()                 # 既有窗口就绪
    # 来源上下文：路径进入窗口的待测清单
    media = tmp_path / "clip.mov"
    media.write_bytes(b"x")
    window2 = service.create_window(None, (str(media),))
    qtbot.addWidget(window2)
    assert str(media) in window2._paths or str(media) in [
        str(path) for path in window2._paths]


def test_host_service_request_cancel_routes_to_worker(qtbot):
    service = DecodeBenchmarkHostService()
    window = service.create_window(None, ())
    qtbot.addWidget(window)

    class _FakeWorker:
        cancelled = False

        def cancel(self):
            self.cancelled = True

        def isRunning(self):   # 既有 closeEvent 会查
            return False

    fake = _FakeWorker()
    window._worker = fake
    service.request_cancel(window)
    assert fake.cancelled                        # 取消路由到 worker


def test_zero_media_writes_contract(qtbot, tmp_path):
    """零素材写入：打开/取消全程素材文件逐字节不变。"""
    media = tmp_path / "source.mov"
    media.write_bytes(os.urandom(2048))
    before = media.read_bytes()
    service = DecodeBenchmarkHostService()
    window = service.create_window(None, (str(media),))
    qtbot.addWidget(window)
    service.request_cancel(window)               # 关闭路径取消
    window.close()
    assert media.read_bytes() == before          # 只读契约


# ---------------------------------------------------------------------------
# 控制器端到端：原生命令 + F5 singleton + 零 HTTP
# ---------------------------------------------------------------------------
