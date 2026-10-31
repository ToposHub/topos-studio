"""W03（计划 W-I02）：运行中驻留源失败的真实恢复 —— L3 端到端（机器 B）。

验证计划 W03 验收的"真实错误 handler→重建→重新呈现"（不只测门闩函数）：

    播放中注入 UV 拷贝失败（interop bind 首次调用失败一次）
    → 渲染器 cuda_resident_import_failed(asset, reason, generation) 上报
    →（生产接线 = TPR._on_cuda_resident_import_failed；本测试在真渲染线程
      上连接同一管理器入口）管理器按素材降级
    → 停新驻留请求 + 在飞租约归零后关旧驻留源（恰一次）
    → 下一次 get_frame 重建下载 planar（vt_planar）源 → 同一渲染线程
      重新呈现非黑画面（当前片段自动恢复）
    → 后续 seek 正确；**其他轨（2K H265）驻留路由不受影响**（故障隔离，
      全局门闩未触发）；降级原因/后端经 resident_demotion_stats 可见。

前置：win32 + nvcuda + cuda-bindings + Qt GL（否则诚实 skip）。
"""
from __future__ import annotations

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
    if not ASSET_H264_4K.exists() or not ASSET_H265_2K.exists():
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
    monkeypatch.setenv("TOPOS_WIN_HW_RESIDENT_DECODE", "1")
    monkeypatch.delenv("TOPOS_DISABLE_WIN_HW_DECODE", raising=False)


@pytest.fixture
def manager():
    """每次用例独立管理器（降级状态互不泄漏），收尾全关。"""
    from src.features.edit.core.edit_gpu_decode_manager import (
        EditGpuDecodeManager,
    )

    m = EditGpuDecodeManager()
    yield m
    m.close_all()


@pytest.fixture(scope="module")
def renderer(qapp):
    """真实 EditGpuPreviewRenderer 线程（共享主 GL 上下文）。"""
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


def _qimage_array(image):
    """快照 → uint8 RGB 数组（W05：RGB888=3字节/像素，4 字节读法越界）。"""
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


def _submit_and_wait(qtbot, qapp, renderer, frame_index: int, layers: list,
                     generation: int):
    """提交任务 → frame_ready + 快照数组（失败帧用 render_error 分支）。"""
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


def _resident_layer(asset: Path, manager, frame_index: int,
                    track_index: int = 0) -> tuple[dict, str]:
    """经真实管理器取帧，按 worker 形态打包（含 W03 asset_path）。"""
    frame, frame_type = manager.get_frame(str(asset), frame_index)
    assert frame is not None, f"decode failed for {asset.name}@{frame_index}"
    assert frame_type == "cuda_resident", (
        f"expected cuda_resident route, got {frame_type!r} for {asset.name}")
    layer = {
        "cuda_resident": frame,
        "frame_type": "cuda_resident",
        "asset_path": str(asset),
        "track_info": {
            "opacity": 1.0,
            "clip_id": f"w03_{track_index}",
            "track_index": track_index,
        },
    }
    return layer, frame_type


def _planar_layer(asset: Path, manager, frame_index: int,
                  track_index: int = 0) -> tuple[dict, str]:
    """取帧并按下载 planar（vt_planar）形态打包（恢复后的层形状）。"""
    frame, frame_type = manager.get_frame(str(asset), frame_index)
    assert frame is not None, f"decode failed for {asset.name}@{frame_index}"
    assert frame_type in ("vt_planar", "planar"), (
        f"expected rebuilt download/CPU route, got {frame_type!r} "
        f"for {asset.name}")
    layer = {
        "decoded_frame": frame,
        "frame_type": frame_type,
        "asset_path": str(asset),
        "track_info": {
            "opacity": 1.0,
            "clip_id": f"w03r_{track_index}",
            "track_index": track_index,
        },
    }
    return layer, frame_type


def _wait_demotion_applied(manager, asset: str, timeout: float = 10.0):
    """等待降级关闭作业收尾（pending 清空 + 源已从管理器移除）。"""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        stats = manager.resident_demotion_stats().get(asset)
        if stats is not None and not stats["close_pending"]:
            return
        time.sleep(0.05)
    raise AssertionError(
        f"demotion close job not finished for {asset}: "
        f"{manager.resident_demotion_stats()}")


class TestResidentFailureRecoveryL3:
    """W03：注入 UV 拷贝失败 → handler → 重建下载 planar → 重新呈现。"""

    def test_injected_copy_failure_recovers_same_segment(
            self, qtbot, qapp, renderer, manager, monkeypatch):
        from src.shared.media.windows_hw.cuda_resident import (
            import_failure_latch_tripped,
        )

        asset = str(ASSET_H264_4K)

        # --- ① 基线：驻留路由正常渲染 ---
        layer0, _ = _resident_layer(ASSET_H264_4K, manager, 0)
        arr0 = _assert_non_black(
            _submit_and_wait(qtbot, qapp, renderer, 0, [layer0], 1),
            "baseline_resident")
        session = renderer._cuda_interop_session
        assert session is not None
        session_stats_before = dict(session.stats)

        # 捕获驻留源对象并包装 close 计数（恰一次断言）
        resident_sources = [
            src for key, src in manager._sources.items()
            if key[1] == asset
            and manager._source_types.get(key) == "win_cuda_resident"
        ]
        assert len(resident_sources) == 1
        resident_src = resident_sources[0]
        orig_close = resident_src.close
        close_calls = {"n": 0}

        def _counting_close():
            close_calls["n"] += 1
            orig_close()

        resident_src.close = _counting_close

        # --- ② 注入 UV 拷贝失败（bind 首次失败一次，之后恢复正常）---
        orig_bind = session.bind_resident_frame
        injected = {"failed": False}

        def _failing_once_bind(cu_frame, decode_path=""):
            if not injected["failed"]:
                injected["failed"] = True
                session.last_fallback_reason = "copy_failed:uv_injected"
                return None
            return orig_bind(cu_frame, decode_path=decode_path)

        monkeypatch.setattr(session, "bind_resident_frame", _failing_once_bind)

        # 生产接线等价物：TPR._on_cuda_resident_import_failed 的转发
        report_errors: list[tuple] = []

        def _handler(asset_path: str, reason: str, generation: int):
            report_errors.append((asset_path, reason, generation))
            manager.note_resident_import_failure(asset_path, reason)

        renderer.cuda_resident_import_failed.connect(_handler)
        try:
            # --- ③ 失败帧：该轨被跳过（render_error），信号按素材上报 ---
            layer5, _ = _resident_layer(ASSET_H264_4K, manager, 5)
            from src.features.edit.core.edit_gpu_preview_renderer import (
                EditRenderTask,
            )

            task = EditRenderTask(
                frame=5, generation_id=2, layers=[layer5],
                viewport_size=(1920, 1080), output_fbo_size=(1920, 1080),
                coordinate_size=(1920, 1080),
            )
            with qtbot.waitSignal(renderer.render_error, timeout=30000):
                assert renderer.submit(task) is True

            deadline = time.perf_counter() + 10.0
            while not report_errors and time.perf_counter() < deadline:
                qapp.processEvents()
                time.sleep(0.02)
            assert report_errors, "cuda_resident_import_failed 未上报"
            assert report_errors[0][0] == asset
            assert report_errors[0][1] == "copy_failed:uv_injected"
            assert report_errors[0][2] == 2  # generation 随任务传递

            # --- ④ 降级生效：旧驻留源租约归零后恰关闭一次 ---
            _wait_demotion_applied(manager, asset)
            deadline = time.perf_counter() + 10.0
            while close_calls["n"] < 1 and time.perf_counter() < deadline:
                time.sleep(0.05)
            assert close_calls["n"] == 1, (
                f"旧驻留源应恰关闭一次，实际 {close_calls['n']} 次")
            stats = manager.resident_demotion_stats()[asset]
            assert stats["last_reason"] == "copy_failed:uv_injected"
            assert stats["cycles"] == 1
            assert stats["barred_permanently"] is False

            # --- ⑤ 当前片段自动恢复：下一帧重建下载 planar 并重新呈现 ---
            layer6, rebuilt_type = _planar_layer(ASSET_H264_4K, manager, 6)
            assert rebuilt_type == "vt_planar"  # 下载 planar（非驻留、非 CPU）
            arr6 = _assert_non_black(
                _submit_and_wait(qtbot, qapp, renderer, 6, [layer6], 3),
                "recovered_planar")

            # --- ⑥ 后续 seek 正确（重建源的真实 GOP seek 路径）---
            layer100, _ = _planar_layer(ASSET_H264_4K, manager, 100)
            arr100 = _assert_non_black(
                _submit_and_wait(qtbot, qapp, renderer, 100, [layer100], 4),
                "recovered_seek_100")

            import hashlib
            import numpy as np

            assert hashlib.md5(arr6.tobytes()).hexdigest() != \
                hashlib.md5(arr100.tobytes()).hexdigest(), \
                "不同帧快照应不同（seek 后内容未更新？）"

            # --- ⑦ 故障隔离：其他轨（2K H265）驻留路由不受影响 ---
            layer_2k, ft_2k = _resident_layer(ASSET_H265_2K, manager, 10, 1)
            assert ft_2k == "cuda_resident"
            arr_2k = _assert_non_black(
                _submit_and_wait(qtbot, qapp, renderer, 10, [layer_2k], 5),
                "other_track_resident_healthy")
            session_stats_after = dict(session.stats)
            assert session_stats_after["bind_count"] > \
                session_stats_before["bind_count"]
            # 全局门闩未触发（单素材故障不应拖垮所有驻留源）
            assert import_failure_latch_tripped() is False
        finally:
            try:
                renderer.cuda_resident_import_failed.disconnect(_handler)
            except Exception:
                pass
