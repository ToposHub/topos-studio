"""B2.1 L3 端到端：Windows cuda 驻留零拷贝 × 生产渲染线程（机器 B）。

驱动**真实生产链**（非 mock）：

    EditGpuDecodeManager（真实路由 3.4b）→ WindowsCudaResidentSource
    → CudaResidentFrame → worker 形态 layer_data → EditGpuPreviewRenderer
    （真实 QThread + 共享 GL 上下文 + _process_task +
    _import_cuda_resident_frame + YUV→RGB→composite）→ frame_ready /
    snapshot_ready

覆盖计划 B2.1 验收的播放侧子集：单轨/双轨（多轨合成）、正向顺序 +
**反向 seek**（经管理器真实 GOP seek 路径）、渲染内容确定性
（同帧两次渲染快照 md5 一致）、interop 会话绑定统计与降级门闩未触发。
窗口最小化/失焦/DPI/关闭需要真实窗口管理器交互，不在 headless 范围
（如实登记，属 C3.1/Phase 5 后续人工档）。

前置：win32 + nvcuda + cuda-bindings + Qt GL（否则诚实 skip——环境缺项
不算用例失败）。
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

ASSET_H264_4K = TEST_VIDEOS / "4K_H264.mov"
ASSET_H265_2K = TEST_VIDEOS / "2K_H265.mov"


def _l3_ready() -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "not_windows"
    try:
        import ctypes

        ctypes.WinDLL("nvcuda.dll")
    except OSError:
        return False, "nvcuda_missing"
    try:
        from cuda.bindings import driver  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"cuda_bindings_unavailable:{exc}"
    if not ASSET_H264_4K.exists():
        return False, "test_video_missing"
    return True, ""


_L3_OK, _L3_REASON = _l3_ready()

pytestmark = [
    pytest.mark.skipif(
        not _L3_OK, reason=f"cuda resident L3 unavailable: {_L3_REASON}"),
    pytest.mark.usefixtures("_resident_env"),
]


@pytest.fixture
def _resident_env(monkeypatch):
    """opt-in 开启（路由门在源创建时读取）。"""
    monkeypatch.setenv("TOPOS_WIN_HW_RESIDENT_DECODE", "1")
    monkeypatch.delenv("TOPOS_DISABLE_WIN_HW_DECODE", raising=False)


@pytest.fixture(scope="module")
def manager():
    from src.features.edit.core.edit_gpu_decode_manager import (
        EditGpuDecodeManager,
    )

    m = EditGpuDecodeManager()
    yield m
    m.close_all()


@pytest.fixture(scope="module")
def renderer(qapp):
    """真实 EditGpuPreviewRenderer 线程（共享主 GL 上下文）。

    Qt GL 缺席时诚实 skip（_startup_failure_reason 透出原因）。
    """
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
    for _ in range(200):  # ≤10s 等待 GL 初始化
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


def _qimage_array(image):
    """快照 → uint8 RGB 数组（1/4 分辨率 Format_RGB888）。

    W05 关键修正：RGB888 = 3 字节/像素，旧实现 setsize(w*h*4) 越界
    25%——最后 1/4 行读到 QImage 缓冲区外堆内存（每次分配不同），即
    B2.1 登记"底部带陈旧纹素"的伪缺陷根源（W05 定性为测试侧读取越界，
    产品快照链逐字节确定；见 docs/validation/upgrade_2026-10-24/W05）。
    """
    import numpy as np
    from PyQt6.QtGui import QImage

    w, h = image.width(), image.height()
    assert image.format() == QImage.Format.Format_RGB888
    buf = image.constBits()
    buf.setsize(w * h * 3)
    return np.frombuffer(
        buf, dtype=np.uint8, count=w * h * 3).copy().reshape(h, w, 3)


def _assert_non_black(arr, context: str):
    import numpy as np

    assert int(np.count_nonzero(arr)) > arr.size * 0.30, (
        f"{context}: 合成内容过黑 (nonzero={np.count_nonzero(arr)}/{arr.size})")
    return arr


def _arr_md5(arr) -> str:
    return hashlib.md5(arr.tobytes()).hexdigest()


def _slot_texture_md5_set(renderer) -> frozenset:
    """经主线程共享上下文读回会话全部 slot 纹理内容（源级判别用）。"""
    import ctypes

    from PyQt6.QtGui import QOffscreenSurface
    from OpenGL.GL import (
        glBindTexture, glGetTexImage, GL_TEXTURE_2D, GL_RED, GL_RG,
        GL_UNSIGNED_BYTE, GL_UNSIGNED_SHORT,
    )

    session = renderer._cuda_interop_session
    if session is None:
        return frozenset()
    surface = QOffscreenSurface()
    surface.create()
    renderer.main_context.makeCurrent(surface)
    out = set()
    try:
        for slot in session._slots:
            itemsize = 2 if slot.key[2] != "nv12" else 1
            pair = []
            for pi, (gl_fmt, ncomp) in enumerate(((GL_RED, 1), (GL_RG, 2))):
                w = slot.key[0] if pi == 0 else slot.key[0] // 2
                h = slot.key[1] if pi == 0 else slot.key[1] // 2
                nbytes = w * h * ncomp * itemsize
                buf = ctypes.create_string_buffer(nbytes)
                glBindTexture(GL_TEXTURE_2D, slot.textures[pi].glo)
                glGetTexImage(
                    GL_TEXTURE_2D, 0, gl_fmt,
                    GL_UNSIGNED_BYTE if itemsize == 1 else GL_UNSIGNED_SHORT,
                    buf)
                pair.append(hashlib.md5(buf.raw).hexdigest())
            out.add(tuple(pair))
    finally:
        renderer.main_context.doneCurrent()
        surface.destroy()
    return frozenset(out)


def _render_and_snapshot(qtbot, qapp, renderer, frame_index: int,
                         layers: list, generation: int):
    """提交真实任务 → frame_ready + 快照内容（md5, 非零像素数）。"""
    from src.features.edit.core.edit_gpu_preview_renderer import (
        EditRenderTask,
    )

    task = EditRenderTask(
        frame=frame_index,
        generation_id=generation,
        layers=layers,
        viewport_size=(1920, 1080),
        output_fbo_size=(1920, 1080),
        coordinate_size=(1920, 1080),
    )

    got = {"image": None}
    got_event = threading.Event()

    def _on_snapshot(frame, gen, image):
        got["image"] = image
        got_event.set()

    renderer.snapshot_ready.connect(_on_snapshot)
    try:
        renderer.arm_frame_snapshot()
        with qtbot.waitSignal(renderer.frame_ready, timeout=30000) as blocker:
            assert renderer.submit(task) is True
        assert blocker.args[0] == frame_index

        deadline = time.time() + 10.0
        while not got_event.is_set() and time.time() < deadline:
            qapp.processEvents()
            time.sleep(0.02)
        assert got_event.is_set(), "snapshot_ready not emitted"
        assert got["image"] is not None
        return _qimage_array(got["image"])
    finally:
        try:
            renderer.snapshot_ready.disconnect(_on_snapshot)
        except Exception:
            pass


def _layer(asset: Path, manager, frame_index: int, track_index: int) -> dict:
    """经真实管理器取帧并按 worker 形态打包 layer_data。"""
    frame, frame_type = manager.get_frame(str(asset), frame_index)
    assert frame is not None, f"decode failed for {asset.name}@{frame_index}"
    assert frame_type == "cuda_resident", (
        f"expected cuda_resident route, got {frame_type!r} for {asset.name}")
    return {
        "cuda_resident": frame,
        "frame_type": "cuda_resident",
        "track_info": {
            "opacity": 1.0,
            "clip_id": f"l3_{track_index}",
            "track_index": track_index,
        },
    }


class TestCudaResidentPlaybackL3:
    """真实渲染线程端到端（计划 B2.1 验收·播放侧子集）。"""

    def test_single_track_render_and_session_stats(
            self, qtbot, qapp, renderer, manager):
        from src.shared.media.windows_hw.cuda_resident import (
            import_failure_latch_tripped,
        )

        arr_a = _assert_non_black(_render_and_snapshot(
            qtbot, qapp, renderer, 0,
            [_layer(ASSET_H264_4K, manager, 0, 0)], 1), "single_track")
        md5_a = _arr_md5(arr_a)

        # 真实 interop 会话被使用且无失败；降级门闩未触发
        session = renderer._cuda_interop_session
        assert session is not None
        assert session.stats["bind_count"] >= 1
        assert session.stats["fail_count"] == 0
        assert import_failure_latch_tripped() is False

    def test_backward_seek_determinism(self, qtbot, qapp, renderer, manager):
        """同帧两次渲染（中间穿插反向 seek）快照 md5 必须一致。"""
        arr_120a = _assert_non_black(_render_and_snapshot(
            qtbot, qapp, renderer, 120,
            [_layer(ASSET_H264_4K, manager, 120, 0)], 2), "seek_120")
        slots_a = _slot_texture_md5_set(renderer)

        # 反向 seek：走管理器真实 GOP seek + decode-forward 路径
        arr_40 = _assert_non_black(_render_and_snapshot(
            qtbot, qapp, renderer, 40,
            [_layer(ASSET_H264_4K, manager, 40, 0)], 3), "seek_back_40")

        arr_120b = _assert_non_black(_render_and_snapshot(
            qtbot, qapp, renderer, 120,
            [_layer(ASSET_H264_4K, manager, 120, 0)], 4), "seek_120_replay")
        slots_b = _slot_texture_md5_set(renderer)

        import numpy as np

        d40 = np.abs(arr_120a.astype(int) - arr_40.astype(int))

        # 源级严格保证（本路径职责边界）：两次 120 渲染导入的 slot 纹理
        # 逐字节一致（主线程共享上下文 glGetTexImage 读回）。
        assert slots_a == slots_b, (
            "同帧两次导入的会话纹理内容不一致（interop 源级回归）")

        # 快照级确定性（W05 严格门禁）：B2.1 登记的"底部带陈旧纹素"
        # 已定性为测试侧伪缺陷——旧 _qimage_array 把 RGB888（3 字节/
        # 像素）按 w*h*4 读，越界的最后 25% 恰为图像底部 1/4 行，读到
        # 缓冲区外堆内存（每次分配不同→"陈旧带"）。修正读取尺寸后：
        # 同帧两次渲染（含反向 seek 交错）快照必须**逐字节一致**（0
        # 容差；快照链清底+状态归位后未覆盖区只会是可检黑带）。
        same_ndiff = int((arr_120a != arr_120b).sum())
        assert same_ndiff == 0, (
            f"同帧重复渲染快照必须逐字节一致: ndiff={same_ndiff}")
        assert int((arr_120a != arr_40).sum()) > arr_120a.size * 0.01, (
            "不同帧快照应显著不同")

    def test_multi_track_composite(self, qtbot, qapp, renderer, manager):
        """双轨（4K H264 + 2K H265，双驻留源并发解码）合成。"""
        single_arr = _assert_non_black(_render_and_snapshot(
            qtbot, qapp, renderer, 10,
            [_layer(ASSET_H264_4K, manager, 10, 0)], 5), "single_ref")

        dual_arr = _assert_non_black(_render_and_snapshot(
            qtbot, qapp, renderer, 10,
            [_layer(ASSET_H264_4K, manager, 10, 0),
             _layer(ASSET_H265_2K, manager, 10, 1)], 6), "dual_composite")

        assert _arr_md5(dual_arr) != _arr_md5(single_arr), (
            "双轨合成结果应区别于单轨")

        session = renderer._cuda_interop_session
        assert session.stats["bind_count"] >= 4  # 累积绑定（含前序用例）
        assert session.stats["fail_count"] == 0
