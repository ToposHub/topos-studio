"""
AsyncRenderer 覆盖补齐测试（计划02：测试覆盖补齐，agent A1）

## 测试目标

针对 src/features/color/core/async_renderer.py 中尚未覆盖的 headless 可测
单元补齐用例（不创建真实 GL 上下文）：

1. 纯逻辑：matte 缓存键、YUV/Range 元数据映射、连续半平面帧规范化、
   UV 变换数学、ColorOutput finish 策略
2. 渲染请求排队：submit 哨兵去重、GPU scope demand revision、lane 替换时
   GpuDecodedFrame 释放、7-lane drain 顺序
3. 取消路径：full-resolution 注册/取消/遗忘、全局 stop、渲染前取消、
   渲染失败、missing_output、PBO 延迟与同步回读
4. 信号发射：progress 钳制、full_resolution_ready/failed、scope 守卫
5. 资源清理：REMOVE_LAYER / CLEAR_AUX_TEX / reference / garbage /
   denoise 窗口 / persistent 纹理 / ColorOutput FBO
6. 状态更新：UPLOAD_VIDEO_FRAME、UPDATE_UNIFORMS、denoise 参数、stats

惯例与 test_async_renderer_lane_merge.py 一致：AsyncRenderer.__new__ 跳过
__init__（无 GL），仅注入被测方法所需属性；需要发信号的用例先执行
QThread.__init__。
"""

from __future__ import annotations

import queue
import threading
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, Mock, patch

import numpy as np
import pytest

from PyQt6.QtCore import QThread

from src.features.color.core.async_renderer import (
    MASK_PREVIEW_MODES,
    AsyncRenderer,
    RenderCommand,
    RenderLane,
    matte_cache_hash,
)
from src.features.color.core.scope_types import ScopeDemand
from src.shared.media.frame import DecodedFrame
from src.shared.video.denoise.params import VideoDenoiseParams

AR = "src.features.color.core.async_renderer"


# =============================================================================
# Helpers
# =============================================================================

def make_stub(**attrs: Any) -> AsyncRenderer:
    """构造跳过 __init__（无 GL、无 QObject）的 AsyncRenderer 轻量桩。"""
    renderer = AsyncRenderer.__new__(AsyncRenderer)
    for name, value in attrs.items():
        setattr(renderer, name, value)
    return renderer


def make_qt_stub(**attrs: Any) -> AsyncRenderer:
    """构造跳过 AsyncRenderer.__init__ 但完成 QObject 初始化的桩（可发信号）。"""
    renderer = AsyncRenderer.__new__(AsyncRenderer)
    QThread.__init__(renderer)
    for name, value in attrs.items():
        setattr(renderer, name, value)
    return renderer


class SignalRecorder:
    """记录 Qt 信号发射参数序列。"""

    def __init__(self, signal: Any) -> None:
        self.calls: List[Tuple[Any, ...]] = []
        signal.connect(self._on_emit)

    def _on_emit(self, *args: Any) -> None:
        self.calls.append(tuple(args))

    def __len__(self) -> int:
        return len(self.calls)

    def phases(self) -> List[str]:
        """取进度信号的 phase 列表（full_resolution_progress 专用）。"""
        return [call[2] for call in self.calls]


def make_demand(revision: int = 5) -> ScopeDemand:
    """构造一个最小可用的 GPU scope 需求。"""
    return ScopeDemand(
        revision=revision,
        source_revision=0,
        mode="waveform",
        display_size=(320, 180),
    )


def make_scope_stub(**overrides: Any) -> AsyncRenderer:
    """构造 _do_render_gpu_scope 守卫测试所需的桩。"""
    attrs: Dict[str, Any] = dict(
        gpu_scope_enabled=True,
        ctx=Mock(),
        current_front_tex_id=11,
        _gpu_scope_shaders_loaded=True,
        _scope_demand_revision=0,
        _pending_lock=threading.Lock(),
        _pending_slots={lane: None for lane in RenderLane},
        _ensure_gpu_scope_pool=Mock(return_value={"wf_display": Mock(glo=1)}),
    )
    attrs.update(overrides)
    return make_qt_stub(**attrs)


def make_fullres_bookkeeping(**overrides: Any) -> AsyncRenderer:
    """构造 full-resolution 注册簿齐全的桩（可发信号）。"""
    attrs: Dict[str, Any] = dict(
        _full_res_cancel_lock=threading.Lock(),
        _full_res_active_requests=set(),
        _full_res_cancelled_requests=set(),
    )
    attrs.update(overrides)
    return make_qt_stub(**attrs)


# =============================================================================
# 纯逻辑：matte 缓存键（P1-COLOR-08）
# =============================================================================

class TestMatteCacheHash:
    """matte_cache_hash 必须覆盖所有会导致 matte 内容变化的输入。"""

    def test_same_inputs_produce_same_hash(self) -> None:
        h1 = matte_cache_hash({"hue": 0.5}, [], (64, 64), (0, 0, 64, 64), 3, 4)
        h2 = matte_cache_hash({"hue": 0.5}, [], (64, 64), (0, 0, 64, 64), 3, 4)
        assert h1 == h2

    def test_qualifier_change_changes_hash(self) -> None:
        base = matte_cache_hash({"hue": 0.5}, [], (64, 64), None, 0, 0)
        changed = matte_cache_hash({"hue": 0.9}, [], (64, 64), None, 0, 0)
        assert base != changed

    def test_mask_shapes_change_changes_hash(self) -> None:
        base = matte_cache_hash({}, [{"type": "circle"}], (64, 64), None, 0, 0)
        changed = matte_cache_hash({}, [{"type": "rect"}], (64, 64), None, 0, 0)
        assert base != changed

    def test_frame_epoch_change_changes_hash(self) -> None:
        """播放中每帧上传都必须生成新的键，否则会复用第 0 帧的 matte。"""
        base = matte_cache_hash({}, [], (64, 64), None, 1, 0)
        changed = matte_cache_hash({}, [], (64, 64), None, 2, 0)
        assert base != changed

    def test_mask_epoch_change_changes_hash(self) -> None:
        """AI matte/bitmap 重传不改 qp/ms 序列化，必须靠 mask_epoch 区分。"""
        base = matte_cache_hash({}, [], (64, 64), None, 0, 1)
        changed = matte_cache_hash({}, [], (64, 64), None, 0, 2)
        assert base != changed

    def test_size_and_rect_list_tuple_normalized(self) -> None:
        """list/tuple 输入应归一化到同一键。"""
        h1 = matte_cache_hash({}, [], [64, 64], [0, 0, 64, 64], 0, 0)
        h2 = matte_cache_hash({}, [], (64, 64), (0, 0, 64, 64), 0, 0)
        assert h1 == h2

    def test_none_size_differs_from_value(self) -> None:
        base = matte_cache_hash({}, [], None, None, 0, 0)
        valued = matte_cache_hash({}, [], (64, 64), None, 0, 0)
        assert base != valued


class TestMaskPreviewModes:
    """P1-COLOR-08：mask_preview shader u_mode 整数映射。"""

    def test_mode_mapping_is_stable_contract(self) -> None:
        assert MASK_PREVIEW_MODES == {"highlight": 0, "black": 1, "matte": 2}

    def test_unknown_style_falls_back_to_highlight(self) -> None:
        """调用点用 .get(style, 0)：未知样式必须回退 highlight(0)。"""
        assert MASK_PREVIEW_MODES.get("nonexistent", 0) == 0


# =============================================================================
# 纯逻辑：YUV / 色彩范围元数据映射
# =============================================================================

class TestCoerceVideoYuvMatrix:
    """_coerce_video_yuv_matrix：任意 metadata → YUVGPUUploader uniform 码。"""

    def test_valid_int_passthrough(self) -> None:
        for value in (0, 1, 2):
            assert AsyncRenderer._coerce_video_yuv_matrix(value) == value

    def test_invalid_int_falls_back_to_bt709(self) -> None:
        assert AsyncRenderer._coerce_video_yuv_matrix(9) == 0
        assert AsyncRenderer._coerce_video_yuv_matrix(-1) == 0

    def test_string_names_map(self) -> None:
        assert AsyncRenderer._coerce_video_yuv_matrix("bt709") == 0
        assert AsyncRenderer._coerce_video_yuv_matrix("BT.709") == 0
        assert AsyncRenderer._coerce_video_yuv_matrix("bt2020nc") == 1
        assert AsyncRenderer._coerce_video_yuv_matrix("bt2020") == 1
        assert AsyncRenderer._coerce_video_yuv_matrix("bt601") == 2
        assert AsyncRenderer._coerce_video_yuv_matrix("smpte170m") == 2
        assert AsyncRenderer._coerce_video_yuv_matrix("bt470bg") == 2

    def test_none_and_unknown_default_to_bt709(self) -> None:
        assert AsyncRenderer._coerce_video_yuv_matrix(None) == 0
        assert AsyncRenderer._coerce_video_yuv_matrix("smpte-2084") == 0

    def test_enum_like_value_object(self) -> None:
        assert AsyncRenderer._coerce_video_yuv_matrix(
            SimpleNamespace(value="bt2020")) == 1


class TestCoerceVideoColorRange:
    """_coerce_video_color_range：任意 metadata → limited/full uniform 码。"""

    def test_valid_int_passthrough(self) -> None:
        assert AsyncRenderer._coerce_video_color_range(0) == 0
        assert AsyncRenderer._coerce_video_color_range(1) == 1

    def test_invalid_int_falls_back_to_limited(self) -> None:
        assert AsyncRenderer._coerce_video_color_range(5) == 0
        assert AsyncRenderer._coerce_video_color_range(-2) == 0

    def test_string_names_map(self) -> None:
        assert AsyncRenderer._coerce_video_color_range("limited") == 0
        assert AsyncRenderer._coerce_video_color_range("tv") == 0
        assert AsyncRenderer._coerce_video_color_range("mpeg") == 0
        assert AsyncRenderer._coerce_video_color_range("full") == 1
        assert AsyncRenderer._coerce_video_color_range("pc") == 1
        assert AsyncRenderer._coerce_video_color_range("jpeg") == 1

    def test_none_and_enum_like(self) -> None:
        assert AsyncRenderer._coerce_video_color_range(None) == 0
        assert AsyncRenderer._coerce_video_color_range(
            SimpleNamespace(value="full")) == 1


# =============================================================================
# 纯逻辑：连续半平面帧规范化（NV12/P010 硬解直通）
# =============================================================================

class TestMaterializeContiguousSemiplanarFrame:
    """_materialize_contiguous_semiplanar_frame：静态、无 GL。"""

    def test_non_decoded_frame_returned_unchanged(self) -> None:
        payload = {"not": "a frame"}
        out = AsyncRenderer._materialize_contiguous_semiplanar_frame(payload)
        assert out is payload

    def test_already_planar_frame_returned_unchanged(self) -> None:
        y = np.zeros((4, 8), dtype=np.uint8)
        uv = np.zeros((2, 8), dtype=np.uint8)
        frame = DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            planes=(y, uv), pixel_format="nv12",
        )
        out = AsyncRenderer._materialize_contiguous_semiplanar_frame(frame)
        assert out is frame

    def test_packed_bgra_returns_none(self) -> None:
        frame = DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            data=np.zeros((4, 8, 4), dtype=np.uint8),
            pixel_format="bgra",
        )
        assert AsyncRenderer._materialize_contiguous_semiplanar_frame(frame) is None

    def test_2d_contiguous_nv12_split_into_planes(self) -> None:
        h, w = 4, 8
        packed = np.arange(h * 3 // 2 * w, dtype=np.uint8).reshape(h * 3 // 2, w)
        frame = DecodedFrame(
            frame_index=7, pts=3, time_seconds=0.25,
            data=packed, pixel_format="nv12", width=w, height=h,
        )
        out = AsyncRenderer._materialize_contiguous_semiplanar_frame(frame)
        assert isinstance(out, DecodedFrame)
        assert out is not frame
        assert out.data is None
        assert out.is_planar is True
        assert [plane.shape for plane in out.planes] == [(4, 8), (2, 8)]
        assert out.frame_index == 7
        assert out.width == 8 and out.height == 4

    def test_split_preserves_plane_content(self) -> None:
        h, w = 4, 8
        packed = np.zeros((h + h // 2, w), dtype=np.uint8)
        packed[:h] = 10          # Y 区
        packed[h:] = 200         # UV 区
        frame = DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            data=packed, pixel_format="nv12", width=w, height=h,
        )
        out = AsyncRenderer._materialize_contiguous_semiplanar_frame(frame)
        assert out is not None
        y_plane, uv_plane = out.planes
        assert float(y_plane.min()) == 10.0 and float(y_plane.max()) == 10.0
        assert float(uv_plane.min()) == 200.0 and float(uv_plane.max()) == 200.0

    def test_1d_buffer_split(self) -> None:
        h, w = 2, 4
        flat = np.arange((h + h // 2) * w, dtype=np.uint8)
        frame = DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            data=flat, pixel_format="p010", width=w, height=h,
        )
        out = AsyncRenderer._materialize_contiguous_semiplanar_frame(frame)
        assert out is not None
        assert [plane.shape for plane in out.planes] == [(2, 4), (1, 4)]
        np.testing.assert_array_equal(out.planes[0].ravel(), flat[:8])
        np.testing.assert_array_equal(out.planes[1].ravel(), flat[8:])

    def test_odd_dimensions_return_none(self) -> None:
        packed = np.zeros((5, 7), dtype=np.uint8)
        frame = DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            data=packed, pixel_format="nv12", width=7, height=4,
        )
        assert AsyncRenderer._materialize_contiguous_semiplanar_frame(frame) is None

    def test_buffer_too_small_return_none(self) -> None:
        packed = np.zeros((3, 8), dtype=np.uint8)  # 4x8 NV12 需要 6 行
        frame = DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            data=packed, pixel_format="nv12", width=8, height=4,
        )
        assert AsyncRenderer._materialize_contiguous_semiplanar_frame(frame) is None

    def test_3d_data_shape_return_none(self) -> None:
        packed = np.zeros((6, 8, 1), dtype=np.uint8)
        frame = DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            data=packed, pixel_format="nv12", width=8, height=4,
        )
        assert AsyncRenderer._materialize_contiguous_semiplanar_frame(frame) is None


# =============================================================================
# submit()：哨兵去重 / scope revision / lane 替换释放
# =============================================================================

class TestSubmitQueueing:
    """submit() 的队列行为（真实 queue.Queue）。"""

    def _make_submitter(self) -> AsyncRenderer:
        return make_stub(
            command_queue=queue.Queue(),
            _pending_slots={lane: None for lane in RenderLane},
            _pending_aux_keys=[],
            _pending_lock=threading.Lock(),
            _drain_pending_signaled=False,
            _drain_pending_signal_lock=threading.Lock(),
            _coalesced_lane_commands=0,
            _scope_demand_revision=0,
        )

    def test_lane_submits_enqueue_single_deduped_sentinel(self) -> None:
        renderer = self._make_submitter()
        data = {"render_reason": "viewport_refresh"}
        renderer.submit(RenderCommand.RENDER_FRAME, data)
        renderer.submit(RenderCommand.RENDER_FRAME, data)

        assert renderer.command_queue.qsize() == 1
        assert renderer.command_queue.queue[0] == ("_drain_pending", None)
        assert renderer._drain_pending_signaled is True
        # 同 lane 第二次提交被合并
        assert renderer._coalesced_lane_commands == 1

    def test_critical_submit_bypasses_lanes(self) -> None:
        renderer = self._make_submitter()
        renderer.submit(RenderCommand.UPLOAD_IMAGE, {"path": "x.png"})

        assert renderer.command_queue.qsize() == 1
        assert renderer.command_queue.queue[0] == (
            RenderCommand.UPLOAD_IMAGE, {"path": "x.png"},
        )
        assert all(slot is None for slot in renderer._pending_slots.values())

    def test_gpu_scope_submit_tracks_demand_revision(self) -> None:
        renderer = self._make_submitter()
        renderer.submit(
            RenderCommand.RENDER_GPU_SCOPE,
            SimpleNamespace(revision=7),
        )
        assert renderer._scope_demand_revision == 7
        assert renderer._pending_slots[RenderLane.GPU_SCOPE] == (
            RenderCommand.RENDER_GPU_SCOPE, SimpleNamespace(revision=7),
        ) or renderer._pending_slots[RenderLane.GPU_SCOPE][0] == (
            RenderCommand.RENDER_GPU_SCOPE
        )

    def test_gpu_scope_revision_never_decreases(self) -> None:
        renderer = self._make_submitter()
        renderer._scope_demand_revision = 9
        renderer.submit(
            RenderCommand.RENDER_GPU_SCOPE,
            SimpleNamespace(revision=3),
        )
        assert renderer._scope_demand_revision == 9

    def test_replaced_gpu_frame_is_released(self) -> None:
        """lane 合并丢弃旧 RENDER_VIDEO_GPU_FRAME 时必须释放 GPU 帧。"""
        renderer = self._make_submitter()
        old_frame = object()
        new_frame = object()
        playback = {"quality": "proxy", "reason": "playback",
                    "drop_if_stale": True}

        with patch(f"{AR}.safe_release_gpu_frame") as release:
            renderer.submit(
                RenderCommand.RENDER_VIDEO_GPU_FRAME,
                {**playback, "gpu_decoded_frame": old_frame},
            )
            renderer.submit(
                RenderCommand.RENDER_VIDEO_GPU_FRAME,
                {**playback, "gpu_decoded_frame": new_frame},
            )

        release.assert_called_once_with(old_frame)

    def test_drain_pending_slots_full_seven_lane_order(self) -> None:
        """7 个 lane 全满时 drain 顺序必须以 SCOPE_SNAPSHOT/GPU_SCOPE 收尾。"""
        renderer = self._make_submitter()
        slots = renderer._pending_slots
        aux_key = (RenderLane.AUX_TEX, "L1", "curves")
        slots[aux_key] = ("aux", {})
        renderer._pending_aux_keys.append(aux_key)
        slots[RenderLane.GRADING_REFRESH] = ("grading", {})
        slots[RenderLane.VIDEO_PLAYBACK] = ("video", {})
        slots[RenderLane.VIEWPORT_REFRESH] = ("viewport", {})
        slots[RenderLane.COLOR_OUTPUT] = ("color", {})
        slots[RenderLane.SCOPE_SNAPSHOT] = ("scope_snapshot", {})
        slots[RenderLane.GPU_SCOPE] = ("gpu_scope", {})

        commands = renderer._drain_pending_slots()

        assert [cmd for cmd, _ in commands] == [
            "aux", "grading", "video", "viewport",
            "color", "scope_snapshot", "gpu_scope",
        ]
        assert all(slot is None for slot in slots.values())
        assert renderer._pending_aux_keys == []

    def test_distinct_aux_resources_never_coalesced(self) -> None:
        """不同 (layer_id, type) 的 AUX 更新 submit 后必须全部保留。"""
        renderer = self._make_submitter()
        renderer.submit(RenderCommand.UPDATE_AUX_TEX,
                        {"layer_id": "L1", "type": "curves", "data": b"a"})
        renderer.submit(RenderCommand.UPDATE_AUX_TEX,
                        {"layer_id": "L1", "type": "lut3d", "path": "x.cube"})
        renderer.submit(RenderCommand.UPDATE_AUX_TEX,
                        {"layer_id": "L2", "type": "mask", "data": b"m"})

        commands = renderer._drain_pending_slots()

        assert [c[1]["type"] for c in commands] == ["curves", "lut3d", "mask"]
        # 同一资源重复提交才合并
        renderer.submit(RenderCommand.UPDATE_AUX_TEX,
                        {"layer_id": "L1", "type": "curves", "data": b"1"})
        renderer.submit(RenderCommand.UPDATE_AUX_TEX,
                        {"layer_id": "L1", "type": "curves", "data": b"2"})
        assert renderer._coalesced_lane_commands == 1
        merged = renderer._drain_pending_slots()
        assert len(merged) == 1 and merged[0][1]["data"] == b"2"


# =============================================================================
# full-resolution 请求登记 / 取消
# =============================================================================

class TestFullResolutionRegistry:
    """register/forget/cancel 的请求级取消簿记。"""

    def test_register_rejects_blank_ids(self) -> None:
        renderer = make_fullres_bookkeeping()
        assert renderer.register_full_resolution("") is False
        assert renderer.register_full_resolution("   ") is False
        assert renderer.register_full_resolution(None) is False
        assert renderer._full_res_active_requests == set()

    def test_register_rejects_duplicate_active_id(self) -> None:
        renderer = make_fullres_bookkeeping()
        assert renderer.register_full_resolution("r1") is True
        assert renderer.register_full_resolution("r1") is False
        assert renderer._full_res_active_requests == {"r1"}

    def test_reregister_after_forget_clears_stale_cancel(self) -> None:
        renderer = make_fullres_bookkeeping()
        renderer.register_full_resolution("r1")
        renderer.cancel_full_resolution("r1")
        assert renderer._full_resolution_cancelled("r1") is True

        renderer.forget_full_resolution("r1")
        assert renderer.register_full_resolution("r1") is True
        assert renderer._full_resolution_cancelled("r1") is False

    def test_cancel_unknown_id_returns_false(self) -> None:
        renderer = make_fullres_bookkeeping()
        assert renderer.cancel_full_resolution("ghost") is False
        assert renderer._full_res_cancelled_requests == set()

    def test_forget_unknown_id_is_noop(self) -> None:
        renderer = make_fullres_bookkeeping()
        renderer.register_full_resolution("r1")
        renderer.forget_full_resolution("ghost")  # 不得抛异常
        assert renderer._full_res_active_requests == {"r1"}

    def test_global_stop_cancels_every_request(self) -> None:
        renderer = make_stub(_stop_event=threading.Event())
        assert renderer._full_resolution_cancelled("anything") is False
        renderer._stop_event.set()
        assert renderer._full_resolution_cancelled("anything") is True
        assert renderer._full_resolution_cancelled(None) is True

    def test_render_cancelled_adapter_delegates_to_request_scope(self) -> None:
        renderer = make_fullres_bookkeeping(
            _full_res_cancelled_requests={"r1"},
        )
        assert renderer._render_cancelled("r1") is True
        assert renderer._render_cancelled("r2") is False


# =============================================================================
# full-resolution 进度 / PBO 轮询信号
# =============================================================================

class TestFullResolutionProgressEmission:
    """_emit_full_resolution_progress 的钳制与类型收敛。"""

    def _make(self) -> Tuple[AsyncRenderer, SignalRecorder]:
        renderer = make_fullres_bookkeeping()
        recorder = SignalRecorder(renderer.full_resolution_progress)
        return renderer, recorder

    def test_percent_clamped_to_bounds(self) -> None:
        renderer, recorder = self._make()
        renderer._emit_full_resolution_progress("r1", 150, "render")
        renderer._emit_full_resolution_progress("r2", -20, "render")
        assert recorder.calls == [("r1", 100, "render"), ("r2", 0, "render")]

    def test_request_id_and_phase_coerced_to_str(self) -> None:
        renderer, recorder = self._make()
        renderer._emit_full_resolution_progress(123, 50, "phase")
        assert recorder.calls == [("123", 50, "phase")]

    def test_emission_failure_is_swallowed(self) -> None:
        renderer, _ = self._make()
        with patch.object(
            renderer, "full_resolution_progress",
        ) as broken_signal:
            broken_signal.emit.side_effect = RuntimeError("telemetry down")
            # 不得抛出：遥测失败不能打断渲染
            renderer._emit_full_resolution_progress("r1", 10, "x")


class TestPollFullResolutionReadbacks:
    """_poll_full_resolution_readbacks：PBO 完成结果的发布与异常终态。"""

    def _make(
        self,
        pending: Dict[str, Tuple[int, int]],
        active: Tuple[str, ...] = (),
        cancelled: Tuple[str, ...] = (),
    ) -> AsyncRenderer:
        renderer = make_fullres_bookkeeping(
            _full_res_active_requests=set(active),
            _full_res_cancelled_requests=set(cancelled),
        )
        renderer._full_res_pending_readbacks = dict(pending)
        return renderer

    def test_ready_result_emits_ready_with_bgr_float32(self) -> None:
        renderer = self._make({"r1": (8, 4)}, active=("r1",))
        ready = SignalRecorder(renderer.full_resolution_ready)
        failed = SignalRecorder(renderer.full_resolution_failed)
        progress = SignalRecorder(renderer.full_resolution_progress)
        result = SimpleNamespace(
            request_id="r1",
            data=np.arange(4 * 8 * 4, dtype=np.float32).reshape(4, 8, 4),
        )
        renderer._full_res_pbo_readback = Mock(
            poll_ready=Mock(return_value=[result]),
        )

        renderer._poll_full_resolution_readbacks()

        assert len(ready) == 1
        request_id, payload = ready.calls[0]
        assert request_id == "r1"
        assert isinstance(payload, np.ndarray)
        assert payload.shape == (4, 8, 3)
        assert payload.dtype == np.float32
        assert payload.flags["C_CONTIGUOUS"]
        assert len(failed) == 0
        assert ("r1", 100, "done") in progress.calls
        assert renderer._full_res_pending_readbacks == {}
        assert "r1" not in renderer._full_res_active_requests

    def test_cancelled_result_emits_failed_cancelled(self) -> None:
        renderer = self._make(
            {"r1": (8, 4)}, active=("r1",), cancelled=("r1",),
        )
        ready = SignalRecorder(renderer.full_resolution_ready)
        failed = SignalRecorder(renderer.full_resolution_failed)
        progress = SignalRecorder(renderer.full_resolution_progress)
        result = SimpleNamespace(
            request_id="r1",
            data=np.zeros((4, 8, 4), dtype=np.float32),
        )
        renderer._full_res_pbo_readback = Mock(
            poll_ready=Mock(return_value=[result]),
        )

        renderer._poll_full_resolution_readbacks()

        assert len(ready) == 0
        assert failed.calls == [("r1", "cancelled")]
        assert ("r1", 100, "cancelled") in progress.calls
        assert renderer._full_res_pending_readbacks == {}
        assert "r1" not in renderer._full_res_active_requests

    def test_wrong_shape_emits_failed(self) -> None:
        renderer = self._make({"r1": (8, 4)}, active=("r1",))
        failed = SignalRecorder(renderer.full_resolution_failed)
        progress = SignalRecorder(renderer.full_resolution_progress)
        result = SimpleNamespace(
            request_id="r1",
            data=np.zeros((2, 2, 1), dtype=np.float32),  # 形状不符
        )
        renderer._full_res_pbo_readback = Mock(
            poll_ready=Mock(return_value=[result]),
        )

        renderer._poll_full_resolution_readbacks()

        assert len(failed) == 1
        request_id, reason = failed.calls[0]
        assert request_id == "r1"
        assert "unexpected PBO result shape" in reason
        assert ("r1", 100, "failed") in progress.calls
        assert renderer._full_res_pending_readbacks == {}

    def test_poll_exception_fails_all_pending_and_disables_pbo(self) -> None:
        renderer = self._make(
            {"r1": (8, 4), "r2": (8, 4)}, active=("r1", "r2"),
        )
        failed = SignalRecorder(renderer.full_resolution_failed)
        renderer._full_res_pbo_readback = Mock(
            poll_ready=Mock(side_effect=RuntimeError("fence broken")),
        )

        renderer._poll_full_resolution_readbacks()

        assert sorted(failed.calls) == [
            ("r1", "readback_failed"), ("r2", "readback_failed"),
        ]
        assert renderer._full_res_pbo_disabled is True
        assert renderer._full_res_pending_readbacks == {}
        assert renderer._full_res_active_requests == set()

    def test_unknown_request_result_ignored(self) -> None:
        renderer = self._make({"r1": (8, 4)}, active=("r1",))
        ready = SignalRecorder(renderer.full_resolution_ready)
        failed = SignalRecorder(renderer.full_resolution_failed)
        result = SimpleNamespace(
            request_id="ghost",
            data=np.zeros((4, 8, 4), dtype=np.float32),
        )
        renderer._full_res_pbo_readback = Mock(
            poll_ready=Mock(return_value=[result]),
        )

        renderer._poll_full_resolution_readbacks()

        assert len(ready) == 0
        assert len(failed) == 0
        # 未匹配的 pending 保持原状，等待它自己的结果
        assert renderer._full_res_pending_readbacks == {"r1": (8, 4)}

    def test_noop_without_pending(self) -> None:
        renderer = self._make({})
        renderer._full_res_pbo_readback = Mock()
        renderer._poll_full_resolution_readbacks()
        renderer._full_res_pbo_readback.poll_ready.assert_not_called()


# =============================================================================
# _do_render_full_resolution：取消 / 失败 / 缺输出 / PBO / 同步回读
# =============================================================================

class TestDoRenderFullResolutionPaths:
    """full-resolution 命令的终态信号路径（GL 全部 mock）。"""

    def test_missing_request_id_emits_nothing(self) -> None:
        renderer = make_fullres_bookkeeping()
        failed = SignalRecorder(renderer.full_resolution_failed)
        progress = SignalRecorder(renderer.full_resolution_progress)

        renderer._do_render_full_resolution({})

        assert len(failed) == 0
        assert len(progress) == 0

    def test_cancelled_before_render_emits_cancelled(self) -> None:
        renderer = make_fullres_bookkeeping()
        renderer.register_full_resolution("r1")
        renderer.cancel_full_resolution("r1")
        renderer._do_render = Mock()
        failed = SignalRecorder(renderer.full_resolution_failed)
        progress = SignalRecorder(renderer.full_resolution_progress)

        renderer._do_render_full_resolution({
            "render_request_id": "r1",
            "viewport_size": (8, 8),
        })

        assert failed.calls == [("r1", "cancelled")]
        assert ("r1", 100, "cancelled") in progress.calls
        renderer._do_render.assert_not_called()
        assert "r1" not in renderer._full_res_active_requests

    def test_render_failure_emits_render_failed(self) -> None:
        renderer = make_fullres_bookkeeping()
        renderer.register_full_resolution("r1")
        renderer._do_render = Mock(return_value=False)
        failed = SignalRecorder(renderer.full_resolution_failed)
        progress = SignalRecorder(renderer.full_resolution_progress)

        renderer._do_render_full_resolution({
            "render_request_id": "r1",
            "viewport_size": (8, 8),
        })

        assert failed.calls == [("r1", "render_failed")]
        assert ("r1", 100, "render_failed") in progress.calls
        assert "r1" not in renderer._full_res_active_requests

    def test_missing_output_fbo_emits_missing_output(self) -> None:
        renderer = make_fullres_bookkeeping()
        renderer.register_full_resolution("r1")
        renderer._do_render = Mock(return_value=True)
        failed = SignalRecorder(renderer.full_resolution_failed)

        renderer._do_render_full_resolution({
            "render_request_id": "r1",
            "viewport_size": (8, 8),
        })
        # 未设置 _full_res_color_output_fbo -> getattr 默认 None

        assert failed.calls == [("r1", "missing_output")]
        assert "r1" not in renderer._full_res_active_requests

    def test_pbo_enqueue_defers_terminal_signals(self) -> None:
        """PBO 入队成功时终端信号延迟到 poll，注册簿保留请求。"""
        renderer = make_fullres_bookkeeping()
        renderer.register_full_resolution("r1")
        renderer._do_render = Mock(return_value=True)
        renderer._full_res_color_output_fbo = Mock()
        renderer._enqueue_full_resolution_readback = Mock(return_value=True)
        ready = SignalRecorder(renderer.full_resolution_ready)
        failed = SignalRecorder(renderer.full_resolution_failed)
        progress = SignalRecorder(renderer.full_resolution_progress)

        renderer._do_render_full_resolution({
            "render_request_id": "r1",
            "viewport_size": (8, 8),
        })

        assert len(ready) == 0
        assert len(failed) == 0
        assert progress.calls[-1] == ("r1", 85, "readback")
        # 延迟路径不终结请求（由 poll 负责）
        assert "r1" in renderer._full_res_active_requests

    def test_synchronous_readback_emits_ready_bgr(self) -> None:
        """PBO 不可用时的同步回读必须发出 BGR ndarray。"""
        w, h = 4, 2
        rgba = np.arange(h * w * 4, dtype=np.float32).reshape(h, w, 4)
        renderer = make_fullres_bookkeeping()
        renderer.register_full_resolution("r1")
        renderer._do_render = Mock(return_value=True)
        output_fbo = Mock()
        output_fbo.read.return_value = rgba.tobytes()
        renderer._full_res_color_output_fbo = output_fbo
        renderer._enqueue_full_resolution_readback = Mock(return_value=False)
        ready = SignalRecorder(renderer.full_resolution_ready)
        failed = SignalRecorder(renderer.full_resolution_failed)

        renderer._do_render_full_resolution({
            "render_request_id": "r1",
            "viewport_size": (w, h),
        })

        assert len(failed) == 0
        assert len(ready) == 1
        request_id, payload = ready.calls[0]
        assert request_id == "r1"
        assert isinstance(payload, np.ndarray)
        assert payload.shape == (h, w, 3)
        assert payload.dtype == np.float32
        assert "r1" not in renderer._full_res_active_requests
        output_fbo.read.assert_called_once_with(
            components=4, alignment=1, dtype="f4",
        )


# =============================================================================
# GPU scope 守卫（_do_render_gpu_scope 的无 GL 早退分支）
# =============================================================================

class TestDoRenderGpuScopeGuards:
    """GPU scope pass 的守卫条件必须在触碰 GL 前生效。"""

    def test_disabled_lane_returns_without_work(self) -> None:
        renderer = make_scope_stub(gpu_scope_enabled=False)
        gpu_ready = SignalRecorder(renderer.gpu_scope_ready)

        renderer._do_render_gpu_scope(make_demand())

        assert renderer.gpu_scope_enabled is False
        renderer._ensure_gpu_scope_pool.assert_not_called()
        assert len(gpu_ready) == 0

    def test_non_scope_demand_payload_ignored(self) -> None:
        renderer = make_scope_stub()
        renderer._do_render_gpu_scope({"revision": 1})  # dict 不是 ScopeDemand
        renderer._ensure_gpu_scope_pool.assert_not_called()

    def test_missing_front_texture_ignored(self) -> None:
        renderer = make_scope_stub(current_front_tex_id=None)
        renderer._do_render_gpu_scope(make_demand())
        renderer._ensure_gpu_scope_pool.assert_not_called()

    def test_shaders_not_loaded_disables_lane(self) -> None:
        renderer = make_scope_stub(_gpu_scope_shaders_loaded=False)
        renderer._pending_slots[RenderLane.GPU_SCOPE] = ("stale", {})

        renderer._do_render_gpu_scope(make_demand())

        assert renderer.gpu_scope_enabled is False
        assert renderer._pending_slots[RenderLane.GPU_SCOPE] is None
        renderer._ensure_gpu_scope_pool.assert_not_called()

    def test_stale_demand_revision_rejected_before_pool(self) -> None:
        renderer = make_scope_stub(_scope_demand_revision=9)
        renderer._do_render_gpu_scope(make_demand(revision=3))
        renderer._ensure_gpu_scope_pool.assert_not_called()

    def test_missing_scope_render_params_returns_after_pool(self) -> None:
        renderer = make_scope_stub(_last_scope_render_params=None)
        gpu_ready = SignalRecorder(renderer.gpu_scope_ready)

        renderer._do_render_gpu_scope(make_demand(revision=5))

        renderer._ensure_gpu_scope_pool.assert_called_once()
        assert len(gpu_ready) == 0


# =============================================================================
# 视频降噪参数与邻帧窗口（M5 / M4.2）
# =============================================================================

class TestVideoDenoiseParamsHandling:
    """_do_set_video_denoise：渲染线程侧参数落地。"""

    def test_non_dict_payload_is_noop(self) -> None:
        renderer = make_stub(video_denoise_params="sentinel")
        AsyncRenderer._do_set_video_denoise(renderer, None)
        assert renderer.__dict__["video_denoise_params"] == "sentinel"

    def test_params_parsed_and_stored(self) -> None:
        renderer = make_stub(video_denoise_pipeline=None)
        AsyncRenderer._do_set_video_denoise(
            renderer, {"params": {}, "quality_mode": "paused"},
        )
        assert isinstance(
            renderer.__dict__["video_denoise_params"], VideoDenoiseParams,
        )
        assert renderer.__dict__["video_denoise_quality_mode"] == "paused"

    def test_invalid_quality_mode_keeps_previous(self) -> None:
        renderer = make_stub(video_denoise_pipeline=None)
        renderer.__dict__["video_denoise_quality_mode"] = "paused"
        AsyncRenderer._do_set_video_denoise(
            renderer, {"params": {}, "quality_mode": "bogus"},
        )
        assert renderer.__dict__["video_denoise_quality_mode"] == "paused"

    def test_pipeline_receives_params_and_overlay(self) -> None:
        pipeline = Mock()
        renderer = make_stub(video_denoise_pipeline=pipeline)
        AsyncRenderer._do_set_video_denoise(
            renderer,
            {"params": {}, "quality_mode": "export", "debug_overlay": True},
        )
        pipeline.set_params.assert_called_once()
        args = pipeline.set_params.call_args.args
        assert isinstance(args[0], VideoDenoiseParams)
        assert args[1] == "export"
        pipeline.set_debug_overlay.assert_called_once_with(True)


class TestVideoDenoiseUIEntries:
    """UI 线程入口：投递命令而非直接改状态。"""

    def test_none_params_not_submitted(self) -> None:
        renderer = make_stub(submit=Mock())
        renderer.set_video_denoise_params(None)
        renderer.submit.assert_not_called()

    def test_params_submitted_with_payload(self) -> None:
        renderer = make_stub(submit=Mock())
        params = VideoDenoiseParams.from_dict({})
        renderer.set_video_denoise_params(
            params, quality_mode="paused", debug_overlay=True,
        )
        cmd, payload = renderer.submit.call_args.args
        assert cmd == RenderCommand.SET_VIDEO_DENOISE
        assert payload["quality_mode"] == "paused"
        assert payload["debug_overlay"] is True
        assert isinstance(payload["params"], dict)

    def test_debug_overlay_omitted_when_none(self) -> None:
        renderer = make_stub(submit=Mock())
        renderer.set_video_denoise_params(VideoDenoiseParams.from_dict({}))
        payload = renderer.submit.call_args.args[1]
        assert "debug_overlay" not in payload

    def test_submit_failure_is_swallowed(self) -> None:
        renderer = make_stub(submit=Mock(side_effect=RuntimeError("queue")))
        renderer.set_video_denoise_params(
            VideoDenoiseParams.from_dict({}),
        )  # 不得抛出
        renderer.reset_video_denoise_history()  # 不得抛出

    def test_reset_history_submits_command(self) -> None:
        renderer = make_stub(submit=Mock())
        renderer.reset_video_denoise_history()
        assert renderer.submit.call_args.args == (
            RenderCommand.RESET_VIDEO_DENOISE_HISTORY, None,
        )


class TestVideoDenoiseWindow:
    """暂停态邻帧窗口：只存数据，纹理懒上传。"""

    def test_window_payload_stored_with_textures_none(self) -> None:
        renderer = make_stub()
        neighbors = [(4, np.zeros((2, 2, 4))), (6, np.zeros((2, 2, 4)))]
        payload = {
            "path": "clip.mov", "center_frame": 5, "neighbors": neighbors,
        }

        AsyncRenderer._do_set_video_denoise_window(renderer, payload)

        window = renderer.__dict__["_video_denoise_window"]
        assert window["path"] == "clip.mov"
        assert window["center_frame"] == 5
        assert window["neighbors"] == neighbors
        assert window["neighbors"] is not neighbors  # 拷贝而非引用
        assert window["textures"] is None

    def test_empty_neighbors_clear_window(self) -> None:
        renderer = make_stub()
        renderer.__dict__["_video_denoise_window"] = {"path": "old"}
        AsyncRenderer._do_set_video_denoise_window(renderer, {"neighbors": []})
        assert "_video_denoise_window" not in renderer.__dict__

    def test_release_window_textures_releases_each(self) -> None:
        tex_a, tex_b = MagicMock(), MagicMock()
        renderer = make_stub()
        renderer.__dict__["_video_denoise_window"] = {
            "textures": [tex_a, tex_b],
        }

        AsyncRenderer._release_video_denoise_window_textures(renderer)

        tex_a.release.assert_called_once()
        tex_b.release.assert_called_once()
        assert renderer.__dict__["_video_denoise_window"]["textures"] is None

    def test_release_tolerates_raising_texture(self) -> None:
        tex_a = MagicMock()
        tex_a.release.side_effect = RuntimeError("dead ctx")
        tex_b = MagicMock()
        renderer = make_stub()
        renderer.__dict__["_video_denoise_window"] = {
            "textures": [tex_a, tex_b],
        }

        AsyncRenderer._release_video_denoise_window_textures(renderer)

        tex_b.release.assert_called_once()
        assert renderer.__dict__["_video_denoise_window"]["textures"] is None

    def test_release_without_window_is_noop(self) -> None:
        renderer = make_stub()
        AsyncRenderer._release_video_denoise_window_textures(renderer)
        assert "_video_denoise_window" not in renderer.__dict__


# =============================================================================
# 状态更新与资源清理
# =============================================================================

class TestDoUpdateUniforms:
    """UPDATE_UNIFORMS：layers / raw_preview / view_params 落地。"""

    def test_layers_and_raw_preview_updated(self) -> None:
        layers = [{"id": "L1"}]
        raw = {"exposure": 1.5}
        renderer = make_stub(layers=[], raw_preview_params={})
        data = {"layers": layers, "raw_preview": raw}

        AsyncRenderer._do_update_uniforms(renderer, data)

        assert renderer.layers == layers
        assert renderer.raw_preview_params == raw
        assert renderer.view_params is data

    def test_empty_data_is_noop(self) -> None:
        renderer = make_stub(layers=["old"], raw_preview_params={"exposure": 0.0})
        AsyncRenderer._do_update_uniforms(renderer, None)
        assert renderer.layers == ["old"]


class TestDoRemoveLayer:
    """REMOVE_LAYER：仅释放目标层的全部 GPU 资源。"""

    def _make(self) -> Tuple[AsyncRenderer, Dict[str, MagicMock]]:
        tex_a, tex_b, tex_c = MagicMock(), MagicMock(), MagicMock()
        matte_l1, matte_l2 = MagicMock(), MagicMock()
        vp_l1, vp_l2 = MagicMock(), MagicMock()
        baked_l1, baked_l2 = MagicMock(), MagicMock()
        ai_l1a, ai_l1b, ai_l2 = MagicMock(), MagicMock(), MagicMock()
        renderer = make_stub(
            layer_textures={"L1": {"curves": tex_a, "mask": tex_b},
                            "L2": {"curves": tex_c}},
            matte_cache={"L1": {"matte": matte_l1},
                         "L2": {"matte": matte_l2}},
            _matte_viewport_textures={"L1": vp_l1, "L2": vp_l2},
            _mask_epoch={"L1": 3, "L2": 1},
            baked_textures={"L1": baked_l1, "L2": baked_l2},
            ai_plugin_textures={("L1", "i1"): ai_l1a,
                                ("L1", "i3"): ai_l1b,
                                ("L2", "i2"): ai_l2},
            fb_manager=Mock(),
        )
        mocks = dict(
            tex_a=tex_a, tex_b=tex_b, matte_l1=matte_l1,
            baked_l1=baked_l1, ai_l1a=ai_l1a, ai_l1b=ai_l1b, vp_l1=vp_l1,
        )
        return renderer, mocks

    def test_removes_only_target_layer_resources(self) -> None:
        renderer, mocks = self._make()

        AsyncRenderer._do_remove_layer(renderer, "L1")

        for name, tex in mocks.items():
            if name == "vp_l1":
                # viewport matte 纹理经 fb_manager 归还池，而非直接 release
                continue
            tex.release.assert_called_once()
        renderer.fb_manager.release_temp_texture.assert_called_once_with(
            mocks["vp_l1"],
        )
        mocks["vp_l1"].release.assert_not_called()
        assert "L1" not in renderer.layer_textures
        assert renderer.layer_textures == {"L2": renderer.layer_textures["L2"]}
        assert set(renderer.matte_cache) == {"L2"}
        assert set(renderer._matte_viewport_textures) == {"L2"}
        assert renderer._mask_epoch == {"L2": 1}
        assert set(renderer.baked_textures) == {"L2"}
        assert set(renderer.ai_plugin_textures) == {("L2", "i2")}

    def test_unknown_layer_is_noop(self) -> None:
        renderer, _ = self._make()
        AsyncRenderer._do_remove_layer(renderer, "ghost")
        assert set(renderer.layer_textures) == {"L1", "L2"}
        assert set(renderer.baked_textures) == {"L1", "L2"}


class TestDoClearAuxTex:
    """CLEAR_AUX_TEX：grade reset 后丢弃该层的 AUX 纹理。"""

    def test_dict_payload_releases_all_aux_entries(self) -> None:
        curves, matte, vp = MagicMock(), MagicMock(), MagicMock()
        fb_manager = Mock()
        renderer = make_stub(
            layer_textures={"L1": {"curves": curves}, "L2": {"curves": MagicMock()}},
            matte_cache={"L1": {"matte": matte}},
            _matte_viewport_textures={"L1": vp},
            fb_manager=fb_manager,
        )

        AsyncRenderer._do_clear_aux_tex(renderer, {"layer_id": "L1"})

        curves.release.assert_called_once()
        matte.release.assert_called_once()
        fb_manager.release_temp_texture.assert_called_once_with(vp)
        assert "L1" not in renderer.layer_textures
        assert "L1" not in renderer.matte_cache
        assert "L1" not in renderer._matte_viewport_textures
        assert set(renderer.layer_textures) == {"L2"}

    def test_raw_layer_id_payload_supported(self) -> None:
        curves = MagicMock()
        renderer = make_stub(
            layer_textures={"L1": {"curves": curves}},
            matte_cache={},
            _matte_viewport_textures={},
            fb_manager=Mock(),
        )

        AsyncRenderer._do_clear_aux_tex(renderer, "L1")

        curves.release.assert_called_once()
        assert renderer.layer_textures == {}

    def test_missing_layer_id_is_noop(self) -> None:
        renderer = make_stub(
            layer_textures={"L1": {"curves": MagicMock()}},
            matte_cache={},
            _matte_viewport_textures={},
            fb_manager=Mock(),
        )
        AsyncRenderer._do_clear_aux_tex(renderer, {})
        assert set(renderer.layer_textures) == {"L1"}


class TestReferenceHandling:
    """reference pyramid 的清理与展示查询。"""

    def test_clear_reference_without_ctx_noop(self) -> None:
        tex = MagicMock()
        renderer = make_stub(
            ctx=None, reference_pyramid_textures=[tex],
        )
        AsyncRenderer._do_clear_reference(renderer)
        tex.release.assert_not_called()
        assert renderer.reference_pyramid_textures == [tex]

    def test_clear_reference_releases_and_empties(self) -> None:
        tex_a, tex_b = MagicMock(), MagicMock()
        renderer = make_stub(
            ctx=Mock(), reference_pyramid_textures=[tex_a, tex_b],
        )
        AsyncRenderer._do_clear_reference(renderer)
        tex_a.release.assert_called_once()
        tex_b.release.assert_called_once()
        assert renderer.reference_pyramid_textures == []

    def test_get_original_texture_id_prefers_reference(self) -> None:
        ref = MagicMock(glo=101)
        main = MagicMock(glo=202)
        renderer = make_stub(
            reference_pyramid_textures=[ref], pyramid_textures=[main],
        )
        assert renderer.get_original_texture_id() == 101

    def test_get_original_texture_id_falls_back_and_clamps(self) -> None:
        lod0, lod1 = MagicMock(glo=1), MagicMock(glo=2)
        renderer = make_stub(
            reference_pyramid_textures=[], pyramid_textures=[lod0, lod1],
        )
        assert renderer.get_original_texture_id() == 1
        assert renderer.get_original_texture_id(lod=9) == 2
        assert renderer.get_original_texture_id(lod=-3) == 1

    def test_get_original_texture_id_empty_returns_none(self) -> None:
        renderer = make_stub(
            reference_pyramid_textures=[], pyramid_textures=[],
        )
        assert renderer.get_original_texture_id() is None


class TestProcessGarbage:
    """_process_garbage：2 秒延迟释放。"""

    def test_old_entries_released_fresh_retained(self) -> None:
        old_fbo, old_tex = MagicMock(), MagicMock()
        fresh_fbo, fresh_tex = MagicMock(), MagicMock()
        now = time.time()
        renderer = make_stub(garbage=[
            (now - 3.0, old_fbo, old_tex),
            (now, fresh_fbo, fresh_tex),
        ])

        renderer._process_garbage()

        old_fbo.release.assert_called_once()
        old_tex.release.assert_called_once()
        fresh_fbo.release.assert_not_called()
        assert len(renderer.garbage) == 1
        assert renderer.garbage[0][1] is fresh_fbo

    def test_force_releases_everything(self) -> None:
        fbo, tex = MagicMock(), MagicMock()
        renderer = make_stub(garbage=[(time.time(), fbo, tex)])
        renderer._process_garbage(force=True)
        fbo.release.assert_called_once()
        tex.release.assert_called_once()
        assert renderer.garbage == []


class TestLookupLayerAiTexture:
    """_lookup_layer_ai_texture：最后一个启用的 AI 插件结果。"""

    def test_dict_layer_enabled_ai_returns_texture(self) -> None:
        tex = MagicMock()
        renderer = make_stub(ai_plugin_textures={("L1", "inst1"): tex})
        layer = {"id": "L1", "plugins": [
            {"type": "ai", "instance_id": "inst1", "enabled": True},
        ]}
        assert AsyncRenderer._lookup_layer_ai_texture(renderer, layer) is tex

    def test_last_enabled_ai_plugin_wins(self) -> None:
        tex1, tex2 = MagicMock(), MagicMock()
        renderer = make_stub(ai_plugin_textures={
            ("L1", "i1"): tex1, ("L1", "i2"): tex2,
        })
        layer = {"id": "L1", "plugins": [
            {"type": "ai", "instance_id": "i1", "enabled": True},
            {"type": "ai", "instance_id": "i2", "enabled": True},
        ]}
        assert AsyncRenderer._lookup_layer_ai_texture(renderer, layer) is tex2

    def test_disabled_or_non_ai_returns_none(self) -> None:
        renderer = make_stub(ai_plugin_textures={("L1", "i1"): MagicMock()})
        disabled = {"id": "L1", "plugins": [
            {"type": "ai", "instance_id": "i1", "enabled": False},
        ]}
        non_ai = {"id": "L1", "plugins": [
            {"type": "film_grain", "instance_id": "i1"},
        ]}
        assert AsyncRenderer._lookup_layer_ai_texture(renderer, disabled) is None
        assert AsyncRenderer._lookup_layer_ai_texture(renderer, non_ai) is None

    def test_object_layer_supported(self) -> None:
        tex = MagicMock()
        renderer = make_stub(ai_plugin_textures={("L1", "i1"): tex})
        layer = SimpleNamespace(id="L1", plugins=[
            {"type": "ai", "instance_id": "i1", "enabled": True},
        ])
        assert AsyncRenderer._lookup_layer_ai_texture(renderer, layer) is tex


# =============================================================================
# matte 生成路由（P2-1）
# =============================================================================

class TestGenerateMatteRouting:
    """_generate_matte 的路由与白图快速路径。"""

    def test_none_layer_returns_white_tex(self) -> None:
        white = object()
        renderer = make_stub(white_tex=white)
        assert AsyncRenderer._generate_matte(renderer, None, None, 0, 0, None) is white

    def test_plain_grade_returns_white_without_gpu_work(self) -> None:
        """无 qualifier 无 mask 的普通调色层不得分配任何 GPU 纹理。"""
        white = object()
        renderer = make_stub(
            white_tex=white,
            pyramid_textures=[MagicMock(size=(64, 64))],
            video_source_size=(0, 0),
            fb_manager=Mock(),
        )
        layer = {"id": "L1"}
        result = AsyncRenderer._generate_matte(
            renderer, layer, MagicMock(), 32, 32, (0, 0, 32, 32),
        )
        assert result is white
        renderer.fb_manager.get_temp_texture.assert_not_called()

    def test_qualifier_enabled_routes_to_viewport_matte(self) -> None:
        """qualifier 必须走 viewport 路径并携带帧 epoch。"""
        sentinel = object()
        viewport_matte = Mock(return_value=sentinel)
        renderer = make_stub(
            pyramid_textures=[MagicMock(size=(64, 64))],
            video_source_size=(0, 0),
            _video_matte_epoch=7,
            _generate_viewport_matte=viewport_matte,
        )
        layer = {"id": "L1", "qualifier_params": {"enabled": True}}

        result = AsyncRenderer._generate_matte(
            renderer, layer, "input", 8, 6, (0, 0, 8, 6),
        )

        assert result is sentinel
        viewport_matte.assert_called_once_with(
            layer, "input", 8, 6, (0, 0, 8, 6), frame_epoch=7,
        )

    def test_no_source_resolution_routes_to_viewport_matte(self) -> None:
        """YUV planes 等无源分辨率输入回退 viewport 路径。"""
        sentinel = object()
        viewport_matte = Mock(return_value=sentinel)
        renderer = make_stub(
            pyramid_textures=[],
            video_source_size=(0, 0),
            current_video_texture=None,
            current_video_planes=None,
            _video_matte_epoch=0,
            _generate_viewport_matte=viewport_matte,
        )
        layer = {"id": "L1", "mask_shapes": [{"type": "circle"}]}

        result = AsyncRenderer._generate_matte(
            renderer, layer, "input", 8, 6, (0, 0, 8, 6),
        )

        assert result is sentinel
        assert viewport_matte.call_args.kwargs.get("frame_epoch") == 0


# =============================================================================
# UV 变换数学（纯逻辑）
# =============================================================================

class TestVideoSourceUvTransform:
    """_video_source_uv_transform：viewport UV → 可见矩形。"""

    def _renderer(self) -> AsyncRenderer:
        return make_stub()

    def test_no_visible_rect_is_identity(self) -> None:
        scale, offset = AsyncRenderer._video_source_uv_transform(
            self._renderer(), None, 64, 32,
        )
        assert scale == (1.0, 1.0)
        assert offset == (0.0, 0.0)

    def test_invalid_source_size_is_identity(self) -> None:
        scale, offset = AsyncRenderer._video_source_uv_transform(
            self._renderer(), (0, 0, 32, 16), 0, 0,
        )
        assert scale == (1.0, 1.0)
        assert offset == (0.0, 0.0)

    def test_zero_extent_is_identity(self) -> None:
        scale, offset = AsyncRenderer._video_source_uv_transform(
            self._renderer(), (10, 10, 0, 0), 64, 32,
        )
        assert scale == (1.0, 1.0)
        assert offset == (0.0, 0.0)

    def test_scale_and_offset_math(self) -> None:
        scale, offset = AsyncRenderer._video_source_uv_transform(
            self._renderer(), (16, 8, 32, 16), 64, 32,
        )
        assert scale == pytest.approx((0.5, 0.5))
        assert offset == pytest.approx((0.25, 0.25))

    def test_partial_extent_math(self) -> None:
        scale, offset = AsyncRenderer._video_source_uv_transform(
            self._renderer(), (10, 20, 30, 10), 100, 40,
        )
        assert scale == pytest.approx((0.3, 0.25))
        assert offset == pytest.approx((0.1, 0.5))


class TestScopeMaskUvRect:
    """_scope_mask_uv_rect：mask UV 矩形（top-down → bottom-up 翻转）。"""

    def test_none_mask_returns_identity(self) -> None:
        renderer = make_stub(rendered_visible_rect=(0, 0, 10, 10))
        assert AsyncRenderer._scope_mask_uv_rect(renderer, None) == \
            (0.0, 0.0, 1.0, 1.0)

    def test_no_visible_rect_returns_identity(self) -> None:
        renderer = make_stub(rendered_visible_rect=None)
        assert AsyncRenderer._scope_mask_uv_rect(renderer, MagicMock()) == \
            (0.0, 0.0, 1.0, 1.0)

    def test_uv_mapping_flips_v_axis(self) -> None:
        renderer = make_stub(
            rendered_visible_rect=(10, 20, 50, 25),
            source_image_size=(100, 100),
            video_source_size=(0, 0),
        )
        rect = AsyncRenderer._scope_mask_uv_rect(renderer, MagicMock())
        assert rect[0] == pytest.approx(0.1)
        assert rect[1] == pytest.approx(0.45)
        assert rect[2] == pytest.approx(0.5)
        assert rect[3] == pytest.approx(-0.25)

    def test_source_size_falls_back_to_video(self) -> None:
        renderer = make_stub(
            rendered_visible_rect=(50, 0, 100, 50),
            source_image_size=(0, 0),
            video_source_size=(200, 100),
        )
        rect = AsyncRenderer._scope_mask_uv_rect(renderer, MagicMock())
        assert rect == pytest.approx((0.25, 0.5, 0.5, -0.5))

    def test_invalid_source_size_returns_identity(self) -> None:
        renderer = make_stub(
            rendered_visible_rect=(10, 20, 50, 25),
            source_image_size=(0, 0),
            video_source_size=(0, 0),
        )
        assert AsyncRenderer._scope_mask_uv_rect(renderer, MagicMock()) == \
            (0.0, 0.0, 1.0, 1.0)

    def test_zero_visible_extent_returns_identity(self) -> None:
        renderer = make_stub(
            rendered_visible_rect=(10, 20, 0, 0),
            source_image_size=(100, 100),
            video_source_size=(0, 0),
        )
        assert AsyncRenderer._scope_mask_uv_rect(renderer, MagicMock()) == \
            (0.0, 0.0, 1.0, 1.0)


class TestColorOutputFinishPolicy:
    """_color_output_requires_immediate_finish：跨上下文消费者策略。"""

    def test_composite_and_monitor_require_finish(self) -> None:
        assert AsyncRenderer._color_output_requires_immediate_finish(None) is True
        assert AsyncRenderer._color_output_requires_immediate_finish("monitor") is True
        assert AsyncRenderer._color_output_requires_immediate_finish("composite") is True

    def test_scope_and_full_res_exempt(self) -> None:
        assert AsyncRenderer._color_output_requires_immediate_finish("scope") is False
        assert AsyncRenderer._color_output_requires_immediate_finish("full_res") is False


class TestBakedLayerTextureLookup:
    """_get_baked_layer_texture：仅当 DTO 明确 is_baked 才消费。"""

    def test_not_baked_returns_none(self) -> None:
        renderer = make_stub(baked_textures={"L1": MagicMock()})
        assert AsyncRenderer._get_baked_layer_texture(
            renderer, {"id": "L1", "is_baked": False},
        ) is None

    def test_baked_without_texture_returns_none(self) -> None:
        renderer = make_stub(baked_textures={})
        assert AsyncRenderer._get_baked_layer_texture(
            renderer, {"id": "L1", "is_baked": True},
        ) is None

    def test_baked_with_texture_returns_texture(self) -> None:
        tex = MagicMock()
        renderer = make_stub(baked_textures={"L1": tex})
        assert AsyncRenderer._get_baked_layer_texture(
            renderer, {"id": "L1", "is_baked": True},
        ) is tex

    def test_baked_coordinate_size_falls_back_when_lod_out_of_range(self) -> None:
        renderer = make_stub(
            pyramid_textures=[MagicMock(size=(1000, 500))],
        )
        assert AsyncRenderer._baked_layer_coordinate_size(
            renderer, (4000, 2000), 5,
        ) == (4000, 2000)


# =============================================================================
# 统计与报告
# =============================================================================

class TestRendererStats:
    """get/reset stats（真实方法，纯状态）。"""

    def test_get_render_stats_reports_counters(self) -> None:
        renderer = make_stub(
            _temp_texture_alloc_count=4,
            _finish_call_count=2,
            _persistent_tex_size=(64, 64),
            _yuv_rgb_output_cache={(64, 64): [Mock(), Mock()]},
            video_render_sync_mode="none",
        )
        stats = renderer.get_render_stats()
        assert stats["temp_texture_alloc_count"] == 4
        assert stats["finish_call_count"] == 2
        assert stats["persistent_tex_size"] == (64, 64)
        assert stats["yuv_rgb_cache_sizes"] == {"(64, 64)": 2}
        assert stats["video_render_sync_mode"] == "none"

    def test_reset_render_stats_zeroes_counters(self) -> None:
        renderer = make_stub(
            _temp_texture_alloc_count=9, _finish_call_count=8,
        )
        renderer.reset_render_stats()
        assert renderer._temp_texture_alloc_count == 0
        assert renderer._finish_call_count == 0

    def test_get_renderer_stats_merges_queue_and_lanes(self) -> None:
        command_queue: queue.Queue = queue.Queue()
        command_queue.put(("a", None))
        command_queue.put(("b", None))
        renderer = make_stub(
            _renderer_stats={
                "dropped_preview_frames": 1, "queue_depth": 0,
                "processed_frames": 5, "coalesced_video_commands": 2,
                "coalesced_lane_commands": 3,
            },
            command_queue=command_queue,
            _coalesced_video_commands=4,
            _coalesced_lane_commands=6,
        )
        stats = renderer.get_renderer_stats()
        assert stats["queue_depth"] == 2
        assert stats["coalesced_video_commands"] == 4
        assert stats["coalesced_lane_commands"] == 6
        assert stats["processed_frames"] == 5

    def test_reset_renderer_stats_zeroes_all(self) -> None:
        renderer = make_stub(
            _renderer_stats={
                "dropped_preview_frames": 1, "queue_depth": 9,
                "processed_frames": 5, "coalesced_video_commands": 2,
                "coalesced_lane_commands": 3,
            },
            command_queue=queue.Queue(),
            _dropped_preview_frames=1,
            _coalesced_video_commands=2,
            _coalesced_lane_commands=3,
        )
        renderer.reset_renderer_stats()
        stats = renderer._renderer_stats
        assert stats["dropped_preview_frames"] == 0
        assert stats["processed_frames"] == 0
        assert stats["coalesced_video_commands"] == 0
        assert stats["coalesced_lane_commands"] == 0
        assert renderer._dropped_preview_frames == 0
        assert renderer._coalesced_video_commands == 0
        assert renderer._coalesced_lane_commands == 0

    def test_get_gpu_backend_report_uninitialized_shape(self) -> None:
        renderer = make_stub()
        report = renderer.get_gpu_backend_report()
        assert report["backend_id"] == "opengl"
        assert report["state"] == "uninitialized"
        assert report["groups"] == {}


class TestYuvGpuStatus:
    """get_yuv_gpu_status：UI 诊断视图。"""

    def test_not_run_status(self) -> None:
        renderer = make_stub(
            _yuv_gpu_supported=False,
            _yuv_gpu_self_test_result=None,
        )
        status = renderer.get_yuv_gpu_status()
        assert status == {
            "supported": False,
            "self_test_result": "not_run",
            "reason": "not_tested",
        }

    def test_result_exposes_reason(self) -> None:
        class _FakeSelfTestResult:
            """模拟带 reason 的自测结果对象。"""

            reason = "mismatch"

            def __str__(self) -> str:
                return "self-test failed"

        renderer = make_stub(
            _yuv_gpu_supported=True,
            _yuv_gpu_self_test_result=_FakeSelfTestResult(),
        )
        status = renderer.get_yuv_gpu_status()
        assert status["supported"] is True
        assert status["reason"] == "mismatch"
        assert "self-test failed" in status["self_test_result"]


class TestVideoTexturePoolStats:
    """get_video_texture_pool_stats：无池时返回空统计。"""

    def test_pool_present_delegates(self) -> None:
        pool = Mock(get_stats=Mock(return_value={"pool_size": 5}))
        renderer = make_stub(_video_texture_pool=pool)
        assert renderer.get_video_texture_pool_stats() == {"pool_size": 5}

    def test_pool_absent_returns_empty_shape(self) -> None:
        renderer = make_stub(_video_texture_pool=None)
        stats = renderer.get_video_texture_pool_stats()
        assert stats == {
            "pool_size": 0, "max_size": 0, "active_frames": [], "sizes": [],
        }


# =============================================================================
# GPU 同步模式与纹理 ring buffer（真实方法）
# =============================================================================

class TestSyncGpuRealMethod:
    """_sync_gpu：真实方法 + Mock ctx。"""

    def _make(self, mode: str) -> Tuple[AsyncRenderer, Mock]:
        ctx = Mock()
        return make_stub(
            ctx=ctx, video_render_sync_mode=mode, _finish_call_count=0,
        ), ctx

    def test_finish_mode_blocks_and_counts(self) -> None:
        renderer, ctx = self._make("finish")
        assert renderer._sync_gpu() is True
        ctx.finish.assert_called_once_with()
        assert renderer._finish_call_count == 1

    def test_fence_mode_uses_finish_fallback(self) -> None:
        renderer, ctx = self._make("fence")
        assert renderer._sync_gpu() is True
        ctx.finish.assert_called_once_with()

    def test_none_mode_skips_sync(self) -> None:
        renderer, ctx = self._make("none")
        assert renderer._sync_gpu() is False
        ctx.finish.assert_not_called()
        assert renderer._finish_call_count == 0


class TestPersistentRenderTexturesRealMethod:
    """_ensure_persistent_render_textures：复用与重建。"""

    def _make(self) -> Tuple[AsyncRenderer, Mock, List[MagicMock]]:
        created: List[MagicMock] = []

        def fake_texture(*args: Any, **kwargs: Any) -> MagicMock:
            tex = MagicMock()
            created.append(tex)
            return tex

        ctx = Mock()
        ctx.texture.side_effect = fake_texture
        renderer = make_stub(
            ctx=ctx,
            _persistent_raw_view_tex=None,
            _persistent_raw_view_fbo=None,
            _persistent_ping_tex=None,
            _persistent_ping_fbo=None,
            _persistent_pong_tex=None,
            _persistent_pong_fbo=None,
            _persistent_tex_size=(0, 0),
            _temp_texture_alloc_count=0,
        )
        return renderer, ctx, created

    def test_first_call_creates_three_of_each(self) -> None:
        renderer, ctx, created = self._make()
        result = renderer._ensure_persistent_render_textures(64, 64)
        assert len(created) == 3  # raw_view + ping + pong
        assert ctx.framebuffer.call_count == 3
        assert result[0] is created[0]
        assert result[2] is created[1]
        assert result[4] is created[2]
        assert renderer._persistent_tex_size == (64, 64)
        assert renderer._temp_texture_alloc_count == 3

    def test_same_size_reuses_without_new_allocations(self) -> None:
        renderer, ctx, created = self._make()
        first = renderer._ensure_persistent_render_textures(64, 64)
        second = renderer._ensure_persistent_render_textures(64, 64)
        assert second == first
        assert len(created) == 3
        assert renderer._temp_texture_alloc_count == 3

    def test_size_change_releases_old_and_recreates(self) -> None:
        renderer, ctx, created = self._make()
        first_tex = first = renderer._ensure_persistent_render_textures(64, 64)
        renderer._ensure_persistent_render_textures(128, 64)

        for tex in (first_tex[0], first_tex[2], first_tex[4]):
            tex.release.assert_called_once()
        assert len(created) == 6  # 旧 3 + 新 3
        assert renderer._persistent_tex_size == (128, 64)
        assert renderer._temp_texture_alloc_count == 6


class TestYuvRgbRingBufferRealMethod:
    """_get_yuv_rgb_output_texture：ring buffer 轮换。"""

    def _make(self) -> Tuple[AsyncRenderer, Mock]:
        ctx = Mock()
        renderer = make_stub(
            ctx=ctx,
            _yuv_rgb_output_cache={},
            _yuv_rgb_output_ring_idx={},
            _yuv_rgb_output_ring_size=3,
            _temp_texture_alloc_count=0,
            # AUD-0202（wd3-g17）：尺寸 LRU 淘汰所需的新状态
            _yuv_rgb_output_size_order=[],
            _yuv_rgb_output_max_sizes=2,
            _yuv_conv_fbo=None,
            _yuv_conv_fbo_tex_glo=0,
        )
        return renderer, ctx

    def test_fills_ring_then_rotates_in_order(self) -> None:
        renderer, ctx = self._make()
        ring = [MagicMock() for _ in range(3)]
        ctx.texture.side_effect = list(ring)

        first_round = [
            renderer._get_yuv_rgb_output_texture(64, 64) for _ in range(3)
        ]
        second_round = [
            renderer._get_yuv_rgb_output_texture(64, 64) for _ in range(3)
        ]

        assert first_round == ring
        assert second_round == ring  # 0 -> 1 -> 2 -> 0
        assert ctx.texture.call_count == 3
        assert renderer._temp_texture_alloc_count == 3

    def test_sizes_cached_independently(self) -> None:
        renderer, ctx = self._make()
        renderer._get_yuv_rgb_output_texture(64, 64)
        renderer._get_yuv_rgb_output_texture(128, 64)
        assert set(renderer._yuv_rgb_output_cache) == {(64, 64), (128, 64)}
        assert ctx.texture.call_count == 2


class TestEnsureColorOutputFbo:
    """_ensure_color_output_fbo：目标缓存 / 重建 / dtype。"""

    def test_same_size_and_dtype_returns_cached(self) -> None:
        tex, fbo = MagicMock(size=(8, 8)), MagicMock()
        ctx = Mock()
        renderer = make_stub(
            ctx=ctx,
            _color_output_tex=tex,
            _color_output_fbo=fbo,
            _color_output_dtype="f2",
        )

        result = renderer._ensure_color_output_fbo(8, 8, None)

        assert result == (tex, fbo)
        ctx.texture.assert_not_called()
        tex.release.assert_not_called()

    def test_scope_resize_releases_old_and_resets_cache_key(self) -> None:
        old_tex, old_fbo = MagicMock(size=(4, 4)), MagicMock()
        new_tex = MagicMock()
        ctx = Mock()
        ctx.texture.return_value = new_tex
        renderer = make_stub(
            ctx=ctx,
            garbage=[],
            _scope_color_output_tex=old_tex,
            _scope_color_output_fbo=old_fbo,
            _scope_color_output_dtype="f2",
            _scope_color_output_revision=3,
            _scope_color_output_max_dim=256,
        )

        tex, fbo = renderer._ensure_color_output_fbo(8, 8, "scope")

        assert tex is new_tex
        assert renderer._scope_color_output_tex is new_tex
        # AUD-0201（wd3-g17 翻转协议）：被替换的 ColorOutput 对可能仍被
        # 跨上下文消费者采样，必须走 garbage 延迟释放队列，而不是立即
        # release（与 _ensure_fbos 的前缓冲路径一致）。
        old_tex.release.assert_not_called()
        old_fbo.release.assert_not_called()
        assert (old_fbo, old_tex) in [(e[1], e[2]) for e in renderer.garbage]
        # 尺寸变化必须作废 scope 的内容缓存键
        assert renderer._scope_color_output_revision == -1
        assert renderer._scope_color_output_max_dim == 0

    def test_full_res_target_uses_f4_dtype(self) -> None:
        new_tex = MagicMock()
        ctx = Mock()
        ctx.texture.return_value = new_tex
        renderer = make_stub(
            ctx=ctx,
            _full_res_color_output_tex=None,
            _full_res_color_output_fbo=None,
        )

        tex, _fbo = renderer._ensure_color_output_fbo(16, 16, "full_res")

        assert tex is new_tex
        ctx.texture.assert_called_once_with((16, 16), 4, dtype="f4")
        assert renderer._full_res_color_output_dtype == "f4"

    def test_partial_creation_failure_releases_and_reraises(self) -> None:
        tex = MagicMock()
        ctx = Mock()
        ctx.texture.return_value = tex
        ctx.framebuffer.side_effect = RuntimeError("out of memory")
        renderer = make_stub(
            ctx=ctx,
            _color_output_tex=None,
            _color_output_fbo=None,
        )

        with pytest.raises(RuntimeError):
            renderer._ensure_color_output_fbo(8, 8, None)

        # 半创建的纹理不能泄漏
        tex.release.assert_called_once()


# =============================================================================
# UPLOAD_VIDEO_FRAME 状态更新（P4.2）
# =============================================================================

class TestDoUploadVideoFrame:
    """_do_upload_video_frame：视频帧上传与状态落地。"""

    def _make(
        self, acquired: Optional[MagicMock] = None,
    ) -> Tuple[AsyncRenderer, Mock]:
        pool = Mock()
        pool.acquire.return_value = acquired
        renderer = make_stub(
            ctx=Mock(),
            _video_texture_pool=pool,
            _ensure_source_tiles=Mock(),
            current_video_texture=None,
            current_video_planes=("old", "planes"),
            current_video_pixel_format="yuv420p",
            current_video_chroma_siting=2,
            current_video_frame_index=-1,
            video_source_size=(0, 0),
            source_image_size=(0, 0),
        )
        renderer.__dict__["_video_needs_swizzle"] = True
        return renderer, pool

    def _payload(self, **overrides: Any) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "frame_data": np.zeros((6, 8, 4), dtype=np.float16),
            "frame_index": 3,
            "width": 8,
            "height": 6,
            "components": 4,
            "dtype": "f2",
        }
        data.update(overrides)
        return data

    def test_missing_ctx_returns_none(self) -> None:
        renderer = make_stub(ctx=None)
        assert AsyncRenderer._do_upload_video_frame(
            renderer, self._payload(),
        ) is None

    def test_invalid_payload_returns_none(self) -> None:
        renderer, _ = self._make()
        assert AsyncRenderer._do_upload_video_frame(renderer, None) is None
        assert AsyncRenderer._do_upload_video_frame(
            renderer, {"frame_index": 0, "width": 0, "height": 0},
        ) is None
        renderer._video_texture_pool.acquire.assert_not_called()

    def test_successful_upload_updates_video_state(self) -> None:
        tex = MagicMock()
        renderer, pool = self._make(acquired=tex)

        result = AsyncRenderer._do_upload_video_frame(
            renderer, self._payload(),
        )

        assert result is tex
        pool.acquire.assert_called_once_with(
            width=8, height=6, frame_index=3, components=4, dtype="f2",
        )
        assert renderer.current_video_texture is tex
        assert renderer.current_video_planes is None
        assert renderer.current_video_pixel_format is None
        assert renderer.current_video_chroma_siting == 0
        assert renderer.current_video_frame_index == 3
        assert renderer.video_source_size == (8, 6)
        assert renderer.source_image_size == (8, 6)
        assert renderer.__dict__["_video_needs_swizzle"] is False
        renderer._ensure_source_tiles.assert_called_once_with(8, 6)

    def test_pool_acquire_failure_returns_none_without_state_change(self) -> None:
        renderer, _ = self._make(acquired=None)
        assert AsyncRenderer._do_upload_video_frame(
            renderer, self._payload(),
        ) is None
        assert renderer.current_video_texture is None
        assert renderer.current_video_planes == ("old", "planes")
        renderer._ensure_source_tiles.assert_not_called()

    def test_texture_write_failure_returns_none_without_state_change(self) -> None:
        tex = MagicMock()
        tex.write.side_effect = RuntimeError("upload failed")
        renderer, _ = self._make(acquired=tex)
        assert AsyncRenderer._do_upload_video_frame(
            renderer, self._payload(),
        ) is None
        assert renderer.current_video_texture is None
        assert renderer.current_video_frame_index == -1


class TestEnsureSourceTiles:
    """_ensure_source_tiles：512 网格与按需重建。"""

    def test_force_creates_512_grid_with_clamped_edges(self) -> None:
        renderer = make_stub(tiles=[])
        renderer._ensure_source_tiles(704, 512, force=True)
        assert len(renderer.tiles) == 2  # 512 + 192
        first, second = renderer.tiles
        assert (first.x, first.y, first.w, first.h, first.level) == \
            (0, 0, 512, 512, 0)
        assert (second.x, second.y, second.w, second.h) == (512, 0, 192, 512)

    def test_matching_size_keeps_existing_tiles(self) -> None:
        from src.features.color.core.image_engine import Tile

        tiles = [Tile(0, 0, 704, 512, level=0)]
        renderer = make_stub(tiles=tiles)
        renderer._ensure_source_tiles(704, 512)
        assert renderer.tiles is tiles

    def test_size_change_triggers_rebuild(self) -> None:
        from src.features.color.core.image_engine import Tile

        renderer = make_stub(tiles=[Tile(0, 0, 704, 512, level=0)])
        renderer._ensure_source_tiles(256, 256)
        assert len(renderer.tiles) == 1
        assert renderer.tiles[0].w == 256
        assert renderer.tiles[0].h == 256

    def test_invalid_dimensions_noop(self) -> None:
        renderer = make_stub(tiles=[])
        renderer._ensure_source_tiles(0, 100)
        renderer._ensure_source_tiles(100, -1)
        assert renderer.tiles == []


class TestDoUploadWithoutContext:
    """_do_upload 在无 ctx 时必须安全早退。"""

    def test_missing_ctx_returns_without_touching_state(self) -> None:
        renderer = make_stub(
            ctx=None, pyramid_textures=[], reference_pyramid_textures=[],
        )
        renderer._do_upload(np.zeros((2, 2, 3), dtype=np.uint8))
        assert renderer.pyramid_textures == []


# =============================================================================
# fence：发布同步（_retire/_publish，patch OpenGL）
# =============================================================================

class TestFrontPresentFence:
    """front 纹理展示 fence 的退役与发布回退。"""

    def test_retire_without_fence_is_noop(self) -> None:
        renderer = make_qt_stub()
        renderer.current_front_fence = None
        AsyncRenderer._retire_front_present_fence(renderer)
        assert renderer.current_front_fence is None

    def test_retire_deletes_fence_and_clears_state(self) -> None:
        renderer = make_qt_stub()
        with patch("OpenGL.GL.glDeleteSync") as delete:
            renderer.current_front_fence = "fence-sentinel"
            AsyncRenderer._retire_front_present_fence(renderer)
        delete.assert_called_once_with("fence-sentinel")
        assert renderer.current_front_fence is None

    def test_render_complete_retires_fence(self) -> None:
        renderer = make_qt_stub()
        renderer.current_front_fence = "stale"
        with patch("OpenGL.GL.glDeleteSync"):
            AsyncRenderer._update_front_present_sync(
                renderer, render_complete=True,
            )
        assert renderer.current_front_fence is None

    def test_publish_unavailable_falls_back_to_finish(self) -> None:
        """fence 不可用时必须回退 ctx.finish() 保证正确性。"""
        renderer = make_qt_stub()
        renderer.ctx = Mock()
        renderer._finish_call_count = 0
        renderer.current_front_fence = None
        with patch("OpenGL.GL.glFenceSync",
                   side_effect=RuntimeError("no sync objects")):
            result = AsyncRenderer._update_front_present_sync(
                renderer, render_complete=False,
            )
        assert result is None
        assert renderer.current_front_fence is None
        renderer.ctx.finish.assert_called_once_with()
        assert renderer._finish_call_count == 1


# =============================================================================
# _handle_command 分发
# =============================================================================

class TestHandleCommandDispatch:
    """_handle_command 的分发契约（实例方法 mock）。"""

    def _stub(self, **attrs: Any) -> AsyncRenderer:
        return make_stub(**attrs)

    def test_update_uniforms_dispatch(self) -> None:
        handler = Mock()
        renderer = self._stub(_do_update_uniforms=handler)
        renderer._handle_command(RenderCommand.UPDATE_UNIFORMS, {"layers": []})
        handler.assert_called_once_with({"layers": []})

    def test_clear_baked_texture_dispatch(self) -> None:
        handler = Mock()
        renderer = self._stub(_do_clear_baked_texture=handler)
        renderer._handle_command(RenderCommand.CLEAR_BAKED_TEXTURE, "L1")
        handler.assert_called_once_with("L1")

    def test_gpu_scope_dispatch(self) -> None:
        handler = Mock()
        renderer = self._stub(_do_render_gpu_scope=handler)
        demand = make_demand()
        renderer._handle_command(RenderCommand.RENDER_GPU_SCOPE, demand)
        handler.assert_called_once_with(demand)

    def test_reset_denoise_history_calls_pipeline(self) -> None:
        pipeline = Mock()
        renderer = self._stub(video_denoise_pipeline=pipeline)
        renderer._handle_command(RenderCommand.RESET_VIDEO_DENOISE_HISTORY, None)
        pipeline.reset_temporal_history.assert_called_once()

    def test_reset_denoise_history_without_pipeline_is_safe(self) -> None:
        renderer = self._stub(video_denoise_pipeline=None)
        renderer._handle_command(
            RenderCommand.RESET_VIDEO_DENOISE_HISTORY, None,
        )  # 不得抛出

    def test_set_video_denoise_dispatch(self) -> None:
        handler = Mock()
        renderer = self._stub(_do_set_video_denoise=handler)
        renderer._handle_command(
            RenderCommand.SET_VIDEO_DENOISE, {"params": {}},
        )
        handler.assert_called_once_with({"params": {}})

    def test_upload_image_dict_carries_flags(self) -> None:
        handler = Mock()
        renderer = self._stub(_do_upload=handler)
        image = np.zeros((2, 2, 3), dtype=np.uint8)
        renderer._handle_command(RenderCommand.UPLOAD_IMAGE, {
            "image": image, "is_video": True, "original_size": (2, 2),
        })
        handler.assert_called_once_with(
            image, is_reference=False, original_size=(2, 2), is_video=True,
        )

    def test_upload_reference_dict_path(self) -> None:
        handler = Mock()
        renderer = self._stub(_do_upload=handler)
        image = np.zeros((2, 2, 3), dtype=np.uint8)
        renderer._handle_command(RenderCommand.UPLOAD_REFERENCE, {"image": image})
        handler.assert_called_once_with(
            image, is_reference=True, original_size=None,
        )


# =============================================================================
# stop() 语义
# =============================================================================

class TestStopSemantics:
    """stop()：协作式停止标记 + 可超时返回。"""

    def test_stop_sets_flags(self) -> None:
        renderer = make_qt_stub(is_running=True, _stop_event=threading.Event())
        assert renderer.stop(timeout_ms=500) is True
        assert renderer.is_running is False
        assert renderer._stop_event.is_set() is True
        assert renderer._cancel_requested() is True


# =============================================================================
# EntryPoint
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
