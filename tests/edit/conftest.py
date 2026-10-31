"""Shared pytest fixtures for ``tests/edit/`` (FX-M08 infrastructure).

Provides the standalone ModernGL context fixture and exposes the small-size
frame generators / golden-compare helpers so every parity test under
``tests/edit/`` can import them without re-rolling its own GL bootstrap.

The GL fixture mirrors the proven pattern in ``tests/video/test_yuv_gpu_upload.py``:
if no GL context is available (headless CI without a software rasteriser),
the test is **skipped with a clear reason** — it never silently passes.
"""
from __future__ import annotations

import os
import sys

import pytest

# Ensure the project root is importable for the helpers package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

# Headless Qt — many edit modules pull in PyQt6 transitively.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import moderngl  # noqa: F401
    HAS_MODERNGL = True
except Exception:  # noqa: BLE001 — optional dependency
    HAS_MODERNGL = False

# Re-export helpers so tests can do ``from tests.edit.conftest import ...``
# or simply rely on the fixtures. Importing here also surfaces a clear
# ImportError early if numpy etc. are missing.
from tests.edit.helpers import golden_compare, frame_fixtures  # noqa: E402,F401


@pytest.fixture
def gl_context():
    """A standalone (offscreen) ModernGL context for GPU parity tests.

    Yields the context. On any failure to obtain GL the test is skipped —
    this is deliberate: a golden-frame parity test that cannot run GPU
    readback must not pass on the CPU path alone.
    """
    if not HAS_MODERNGL:
        pytest.skip("moderngl not available — GPU parity tests need GL")
    try:
        ctx = moderngl.create_standalone_context()
    except Exception as exc:  # noqa: BLE001 — environment, not a code bug
        pytest.skip(f"Failed to create standalone OpenGL context: {exc}")
    try:
        yield ctx
    finally:
        try:
            ctx.release()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture
def require_gl(gl_context):
    """Explicit alias for tests that want to document GL is mandatory.

    Provided so a test author can signal intent (``def test_x(require_gl)``)
    without the fixture name being mistaken for something it consumes.
    """
    return gl_context


# Make the helper modules importable as ``from tests.edit.helpers...`` even
# when tests are run with the file root (not package root) on sys.path.
sys.path.insert(0, os.path.dirname(__file__))



@pytest.fixture(scope="session")
def widget_shelter(qapp):
    """会话级隐藏宿主：给无父窗口的测试控件一个 C++ 所有者。

    无父（parent=None）的顶层 QWidget 只被 Python 引用持有，测试结束后
    由 GC 在任意时刻析构；析构若与后续测试的窗口 close/hide 事件交错，
    activeWindow/focus 链会向已释放对象派发事件（lldb 实测
    EXC_BAD_ACCESS 中断整套 tests/edit）。把这类控件挂到会话级宿主下，
    C++ 所有权归 Qt，Python GC 不再触发析构，销毁统一发生在
    QApplication 退出。新测试建议 ``ProgramViewerBar(widget_shelter)``。
    """
    from PyQt6.QtWidgets import QWidget

    shelter = QWidget()
    shelter.hide()
    yield shelter
    shelter.deleteLater()


@pytest.fixture(scope="session", autouse=True)
def _freeze_cyclic_gc_for_qt_widgets():
    """整个 tests/edit 会话期间禁用循环 GC（Qt 控件生命周期稳定性）。

    无父窗口的测试控件（parent=None 的顶层 QWidget）由 Python 引用持有，
    引用成环时交给循环 GC 在任意时刻析构；析构若与后续测试的窗口
    close/hide 事件处理交错，Qt 的 activeWindow/focus 链会向已释放对象
    派发事件（lldb 实测 EXC_BAD_ACCESS，曾确定性中断整套 tests/edit）。
    应用侧的真实生命周期问题已单独修复（dialog adapter 的
    WA_DeleteOnClose）；此处针对的是测试夹具大量无父控件的场景：
    会话期间冻结循环 GC，让控件析构统一发生在 QApplication 退出，
    代价仅是测试期内存峰值略高（套件时长分钟级，可接受）。
    """
    import gc

    gc.collect()
    gc.freeze()
    gc.disable()
    yield
    gc.enable()
    gc.unfreeze()
    gc.collect()


@pytest.fixture(autouse=True)
def _drain_leaked_cpu_fallback_decode_workers():
    """F1.2：测试间泄清泄漏的 source_viewer CPU 解码 worker。

    单进程全量套件的 92% 楔点定性为跨测试泄漏存活的
    ``_CpuFallbackDecodeWorker``（持有未释放解码器）× PyAV C 内部状态
    （见 docs/validation/feature_F/F1.2/4811a68fe 台账 §4）。每个用例收尾
    枚举 gc 中的存活 worker：先 close_decoder 释放解码器、再 stop 结束
    线程，消除交叉污染源。
    """
    import gc

    from src.features.edit.ui.source_viewer import _CpuFallbackDecodeWorker

    yield

    for worker in [w for w in gc.get_objects()
                   if isinstance(w, _CpuFallbackDecodeWorker)]:
        try:
            worker.close_decoder()
        except Exception:
            pass
        try:
            worker.stop()
        except Exception:
            pass
