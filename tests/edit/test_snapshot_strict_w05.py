"""W05（计划 W-I05）：快照读回严格缓存正确性门禁 —— L3 真机。

B2.1 曾登记"快照回读特定交错下底部带陈旧纹素"缺陷。W05 定性结论：
**伪缺陷**——旧测试助手把 Format_RGB888（3 字节/像素）QImage 按
w*h*4 读取，越界的最后 25% 恰为图像底部 1/4 行（568×320 下即 240..319
行），读到 QImage 缓冲区之外的堆内存（每次分配不同 →"陈旧带"随交错
变动、24B–181KB 幅度、planar 同族 1 字节差）。修正读取尺寸后，快照链
逐字节确定。证据链：源级 slot 纹理 md5 一致 + present 槽 md5 一致 +
composite 快照瞬间 glGetTexImage 逐字节一致（诊断期间实测）+ 本文件
严格门禁。

W05 产品侧配套：
- `_capture_frame_snapshot` 状态卫生（scissor 归位/显式 viewport/清底：
  异常覆盖显性化为黑带，陈旧纹素不可能冒充有效内容进 seek 缓存）；
- present 链 fence 修复（PyOpenGL GLsync bytes 句柄转换——旧实现每帧
  吞异常 fence_ns=0，Viewer 无 fence 不等写入即 blit）。

严格门禁（计划验收：去掉同帧近 50% 字节差异的宽松断言）：
- 同帧快照**逐字节一致**（0 容差），cuda 驻留与下载 planar 两路由各跑；
- 反向 seek 交错（120→40→120）与背景预缓冲交错；
- 分辨率切换后同帧快照仍逐字节一致（快照纹理按尺寸重建路径）；
- 底部带覆盖检查（非黑——清底后未覆盖即黑，可检）；
- present descriptor 必须携带非零 fence（Windows GL）。

前置：win32 + Qt GL + 素材（否则诚实 skip）。
"""
from __future__ import annotations

import hashlib
import sys
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

pytest.importorskip("PyQt6")

TEST_VIDEOS = PROJECT_ROOT / "tests" / "test_videos"
ASSET = TEST_VIDEOS / "4K_H264.mov"


def _l3_ready() -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "not_windows"
    if not ASSET.exists():
        return False, "test_video_missing"
    return True, ""


_L3_OK, _L3_REASON = _l3_ready()

pytestmark = pytest.mark.skipif(
    not _L3_OK, reason=f"snapshot strict L3 unavailable: {_L3_REASON}")


@pytest.fixture(scope="module")
def renderer(qapp):
    from PyQt6.QtGui import QOffscreenSurface, QOpenGLContext

    surface = QOffscreenSurface()
    surface.create()
    main_ctx = QOpenGLContext()
    if not main_ctx.create():
        surface.destroy()
        pytest.skip("QOpenGLContext.create() failed (no Qt GL in this env)")
    main_ctx.makeCurrent(surface)

    from src.features.edit.core.edit_gpu_preview_renderer import (
        EditGpuPreviewRenderer,
    )

    r = EditGpuPreviewRenderer(main_context=main_ctx)
    r.start()
    for _ in range(200):
        if r.is_initialized:
            break
        qapp.processEvents()
        time.sleep(0.05)
    if not r.is_initialized:
        reason = r._startup_failure_reason
        r.stop()
        r.wait(5000)
        main_ctx.doneCurrent()
        surface.destroy()
        pytest.skip(f"renderer GL init failed: {reason!r}")

    yield r

    r.stop()
    r.wait(10000)
    main_ctx.doneCurrent()
    surface.destroy()


class _Gen:
    """renderer 代际单调递增（submit 拒绝旧代际任务）。"""

    def __init__(self, start: int) -> None:
        self._v = start

    def next(self) -> int:
        self._v += 1
        return self._v


_GEN = _Gen(5000)


def _snapshot_arr(qtbot, qapp, renderer, frame, layers, gen, *, size=(1920, 1080),
                  background=False):
    """提交任务取快照数组（RGB888 正确读取）；background 任务无 frame_ready。"""
    import numpy as np
    from PyQt6.QtGui import QImage

    from src.features.edit.core.edit_gpu_preview_renderer import EditRenderTask

    task = EditRenderTask(
        frame=frame, generation_id=gen, layers=layers,
        viewport_size=size, output_fbo_size=size, coordinate_size=size,
        background=background)
    got = {}
    ev = threading.Event()

    def _on(f, g, image):
        got["img"] = image
        ev.set()

    renderer.snapshot_ready.connect(_on)
    try:
        renderer.arm_frame_snapshot()
        assert renderer.submit(task) is True
        if background:
            # 背景任务不发 frame_ready：等快照即可（跨线程信号经主线程
            # 事件循环投递，轮询必须 processEvents）
            deadline = time.perf_counter() + 30
            while not ev.is_set() and time.perf_counter() < deadline:
                qapp.processEvents()
                time.sleep(0.02)
        else:
            with qtbot.waitSignal(renderer.frame_ready, timeout=30000):
                pass  # submit 已发出；等待渲染完成信号
            deadline = time.perf_counter() + 10
            while not ev.is_set() and time.perf_counter() < deadline:
                qapp.processEvents()
                time.sleep(0.02)
        assert ev.is_set(), "snapshot not delivered"
        img = got["img"]
        w, h = img.width(), img.height()
        assert img.format() == QImage.Format.Format_RGB888
        buf = img.constBits()
        buf.setsize(w * h * 3)
        return np.frombuffer(
            buf, dtype=np.uint8, count=w * h * 3).copy().reshape(h, w, 3)
    finally:
        try:
            renderer.snapshot_ready.disconnect(_on)
        except Exception:
            pass


def _layer(manager, frame, ti=0):
    f, ft = manager.get_frame(str(ASSET), frame)
    assert f is not None and ft in ("cuda_resident", "vt_planar", "planar"), ft
    key = "cuda_resident" if ft == "cuda_resident" else "decoded_frame"
    return {key: f, "frame_type": ft, "asset_path": str(ASSET),
            "track_info": {"opacity": 1.0, "clip_id": f"w05_{ti}",
                           "track_index": ti}}


def _assert_non_black(arr, context: str):
    import numpy as np

    assert int(np.count_nonzero(arr)) > arr.size * 0.30, (
        f"{context}: 内容过黑 (nonzero={np.count_nonzero(arr)}/{arr.size})")


def _md5(arr) -> str:
    return hashlib.md5(arr.tobytes()).hexdigest()


@pytest.mark.parametrize("resident", [True, False],
                         ids=["cuda_resident", "download_planar"])
def test_same_frame_snapshot_strict_equality(qtbot, qapp, renderer,
                                              monkeypatch, resident):
    """同帧快照逐字节一致：反向 seek 交错 + 背景预缓冲交错 + 分辨率切换。"""
    import numpy as np

    from src.features.edit.core.edit_gpu_decode_manager import (
        EditGpuDecodeManager,
    )

    if resident:
        monkeypatch.setenv("TOPOS_WIN_HW_RESIDENT_DECODE", "1")
    else:
        monkeypatch.delenv("TOPOS_WIN_HW_RESIDENT_DECODE", raising=False)
    monkeypatch.delenv("TOPOS_DISABLE_WIN_HW_DECODE", raising=False)

    manager = EditGpuDecodeManager()
    try:
        # --- 反向 seek 交错：120 → 40 → 120 → 120 ---
        a = _snapshot_arr(qtbot, qapp, renderer, 120,
                          [_layer(manager, 120)], _GEN.next())
        _assert_non_black(a, "120a")
        _ = _snapshot_arr(qtbot, qapp, renderer, 40,
                          [_layer(manager, 40)], _GEN.next())
        b = _snapshot_arr(qtbot, qapp, renderer, 120,
                          [_layer(manager, 120)], _GEN.next())
        assert _md5(a) == _md5(b), (
            f"同帧两次渲染快照不一致（seek 交错）: {_md5(a)} vs {_md5(b)}")

        # --- 背景预缓冲交错：背景任务（不发 frame_ready）穿插后同帧 ---
        _ = _snapshot_arr(qtbot, qapp, renderer, 121,
                          [_layer(manager, 121)], _GEN.next(),
                          background=True)
        c = _snapshot_arr(qtbot, qapp, renderer, 120,
                          [_layer(manager, 120)], _GEN.next())
        assert _md5(a) == _md5(c), (
            f"同帧快照不一致（背景预缓冲交错）: {_md5(a)} vs {_md5(c)}")

        # --- 分辨率切换：1080p → 2160p → 1080p（快照纹理按尺寸重建）---
        # 720p 会被 small_h=max(320, h//4) 下限钳到同尺寸；2160p 的
        # h//4=540 超过下限，真正走到纹理重建路径。
        s4k_a = _snapshot_arr(qtbot, qapp, renderer, 120,
                              [_layer(manager, 120)], _GEN.next(),
                              size=(3840, 2160))
        _assert_non_black(s4k_a, "2160p")
        assert s4k_a.shape != a.shape  # 快照尺寸随输出分辨率重建
        _ = _snapshot_arr(qtbot, qapp, renderer, 40,
                          [_layer(manager, 40)], _GEN.next(),
                          size=(3840, 2160))
        s4k_b = _snapshot_arr(qtbot, qapp, renderer, 120,
                              [_layer(manager, 120)], _GEN.next(),
                              size=(3840, 2160))
        assert _md5(s4k_a) == _md5(s4k_b), "2160p 同帧快照不一致"
        d = _snapshot_arr(qtbot, qapp, renderer, 120,
                          [_layer(manager, 120)], _GEN.next())
        assert _md5(a) == _md5(d), "切回 1080p 后同帧快照不一致"

        # --- 底部带覆盖：清底后未覆盖区必为黑 → 底部 1/4 必须非黑 ---
        bottom_quarter = d[d.shape[0] * 3 // 4:]
        nonzero_ratio = np.count_nonzero(bottom_quarter) / bottom_quarter.size
        assert nonzero_ratio > 0.30, (
            f"底部 1/4 覆盖不足（清底后黑带）: nonzero={nonzero_ratio:.2%}")
    finally:
        manager.close_all()


def test_present_descriptor_carries_fence(qtbot, qapp, renderer, monkeypatch):
    """W05 present 链 fence 修复：present descriptor 必须携带非零 fence。

    旧实现 PyOpenGL GLsync bytes 句柄 int() 转换抛异常 → 每帧 fence_ns=0
    → Viewer 不等 renderer 写入完成直接 blit（显示链潜在撕裂）。
    """
    monkeypatch.setenv("TOPOS_WIN_HW_RESIDENT_DECODE", "1")
    monkeypatch.delenv("TOPOS_DISABLE_WIN_HW_DECODE", raising=False)

    from src.features.edit.core.edit_gpu_decode_manager import (
        EditGpuDecodeManager,
    )
    from src.features.edit.core.edit_gpu_preview_renderer import EditRenderTask

    manager = EditGpuDecodeManager()
    got = {}
    ev = threading.Event()

    def _on_present(present):
        got["present"] = present
        ev.set()

    renderer.present_frame_ready.connect(_on_present)
    try:
        layer = _layer(manager, 0)
        task = EditRenderTask(
            frame=0, generation_id=_GEN.next(), layers=[layer],
            viewport_size=(1920, 1080), output_fbo_size=(1920, 1080),
            coordinate_size=(1920, 1080))
        with qtbot.waitSignal(renderer.frame_ready, timeout=30000):
            assert renderer.submit(task) is True
        deadline = time.perf_counter() + 10
        while not ev.is_set() and time.perf_counter() < deadline:
            qapp.processEvents()
            time.sleep(0.02)
        assert ev.is_set(), "present_frame_ready not emitted"
        present = got["present"]
        fence_ns = int(getattr(present, "fence_ns", 0) or 0)
        has_fence = bool(getattr(present, "has_fence", False))
        assert fence_ns != 0 and has_fence, (
            f"present descriptor 无 fence（W05 fence 修复失效）: "
            f"fence_ns={fence_ns}, has_fence={has_fence}")
    finally:
        try:
            renderer.present_frame_ready.disconnect(_on_present)
        except Exception:
            pass
        manager.close_all()
