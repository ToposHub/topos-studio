"""
Timeline GPU Compositor
专业 NLE 实时播放 GPU 合成器（规格书 §13 / Stage F）。

当前阶段：包装现有 EditGpuPreviewRenderer，并成为 Edit 预览唯一的
GPU backend 提交/信号边界。后续阶段：把多轨合成 shader 和 task 数据结构
进一步收敛到本模块。

职责：
- 接收多个 layer 的 GpuFrameHandle / GpuLayerInput。
- 执行 transform/crop/opacity/letterbox/YUV->RGB。
- 输出一个可由 ProgramViewer 直接显示的 GPU texture。

关键不变量（规格书 §13.3）：
- 不在 GPU compositor 内部请求解码（只消费 ready frames）。
- GPU miss 后不自行 CPU fallback（由 engine hold/rebuffer）。
- 不每个 layer 分别 present。
- 不发生 GL readback。
- realtime 模式输出 GPU texture，不输出 QImage。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Tuple

from PyQt6.QtCore import QObject, pyqtSignal

from .types import FrameHandle

logger = logging.getLogger(__name__)


# =============================================================================
# Render Task 数据结构（规格书 §13.1）
# =============================================================================


@dataclass(frozen=True)
class GpuLayerInput:
    """
    单 layer 的 GPU 合成输入。

    Attributes:
        clip_id: timeline clip id
        track_index: 轨道序号（从小到大绘制）
        opacity: 不透明度 [0, 1]
        transform: 变换参数（与现有 TransformParams 兼容）
        crop: 裁剪参数
        frame: 已 ready 的 FrameHandle（payload 为 GPU texture / planar；
            AUD-17501：backend 仅接受 gpu_texture / planar_yuv 两种
            payload，cpu_ndarray/placeholder 会被 submit 显式拒绝）
        blend_mode: 混合模式（默认 normal）
    """
    clip_id: str
    track_index: int
    opacity: float
    transform: Any
    crop: Any
    frame: FrameHandle
    blend_mode: str = "normal"


@dataclass(frozen=True)
class TimelineGpuRenderTask:
    """
    一个 timeline frame 的 GPU 合成任务（规格书 §13.1）。

    Attributes:
        timeline_frame: 时间线帧号
        generation_id: 调度 generation
        output_width / output_height: 输出 FBO 尺寸
        layers: 各 layer 输入（按 track_index 排序）
        color_management: 色彩管理参数
        deadline_ns: 截止时间戳
    """
    timeline_frame: int
    generation_id: int
    output_width: int
    output_height: int
    layers: Tuple[GpuLayerInput, ...]
    color_management: dict = field(default_factory=dict)
    deadline_ns: int = 0
    # Native sequence canvas for pixel-space TransformParams. When omitted,
    # the backend falls back to the output FBO size for legacy callers.
    coordinate_size: Optional[Tuple[int, int]] = None


# =============================================================================
# GpuFrameHandle（规格书 §12.2）
# =============================================================================


@dataclass
class GpuFrameHandle:
    """
    GPU-resident frame handle。

    texture_ids: 单纹理（rgb）或多纹理（yuv420p = Y/U/V）。
    release: 释放回调（归还 GpuFramePool）。
    """
    identity: Any  # FrameIdentity
    texture_ids: Tuple[int, ...]
    texture_type: str  # "rgb" | "yuv420p" | "yuv422p" | "nv12" | "external"
    width: int
    height: int
    bit_depth: int
    color_space: Optional[str]
    generation_id: int
    release: Optional[Callable[[], None]] = None


# =============================================================================
# TimelineGpuCompositor
# =============================================================================


class TimelineGpuCompositor(QObject):
    """
    GPU 合成器（包装 EditGpuPreviewRenderer）。

    信号：
        frame_ready(timeline_frame, generation_id, texture_id, width, height)
        render_error(timeline_frame, message)

    用法：
        compositor = TimelineGpuCompositor()
        compositor.attach_backend(gpu_renderer)  # EditGpuPreviewRenderer
        task = TimelineGpuRenderTask(...)
        compositor.submit(task)
    """

    frame_ready = pyqtSignal(int, int, int, int, int)  # frame, gen, tex_id, w, h
    present_frame_ready = pyqtSignal(object)  # GpuPresentFrame descriptor
    render_error = pyqtSignal(int, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._backend: Any = None  # EditGpuPreviewRenderer
        self._connected = False
        # AUD-17500：已建立的 (signal, slot) 连接清单——部分连接失败时
        # 逐条回滚，detach 按清单断连，不再依赖单一 _connected 旗标。
        self._connections: list[tuple[Any, Any]] = []
        self._generation = 0
        # 统计
        # AUD-17505：统计计数器由渲染线程自增（submit_backend_task）、
        # GUI/telemetry 线程在 stats()/reset_stats() 读写——统一走这把锁。
        self._stats_lock = threading.Lock()
        self._submitted = 0
        self._gpu_composite_count = 0
        self._cpu_qimage_count = 0  # 必须 == 0（规格书 §13.3）
        self._readback_count = 0    # 必须 == 0

    # ------------------------------------------------------------------
    # attach backend
    # ------------------------------------------------------------------

    def attach_backend(self, gpu_renderer: Any) -> None:
        """
        注入底层 EditGpuPreviewRenderer。

        包装模式（规格书 §13）：本模块负责 API 收敛和约束检查，
        实际 GL 合成委托 backend。

        AUD-17500：先探测全部所需信号再逐条连接；任一连接失败时主动
        回滚已建立的连接（否则 backend 持有 compositor 槽引用、渲染线程
        每帧仍向僵尸 compositor 转发，且 detach_backend 因 _connected=False
        跳过断连）。失败以 warning 上报（原 debug 级不可见）。
        """
        self._backend = gpu_renderer
        if self._connected:
            return
        frame_ready = getattr(gpu_renderer, "frame_ready", None)
        render_error = getattr(gpu_renderer, "render_error", None)
        present_frame_ready = getattr(gpu_renderer, "present_frame_ready", None)
        if not callable(getattr(frame_ready, "connect", None)) or not callable(
            getattr(render_error, "connect", None)
        ):
            logger.warning(
                "attach_backend: backend missing frame_ready/render_error signals"
            )
            return
        planned: list[tuple[Any, Any]] = [
            (frame_ready, self._on_backend_frame_ready),
            (render_error, self._on_backend_render_error),
        ]
        if callable(getattr(present_frame_ready, "connect", None)):
            planned.insert(1, (present_frame_ready, self._on_backend_present_frame_ready))
        connected: list[tuple[Any, Any]] = []
        try:
            for signal, slot in planned:
                signal.connect(slot)
                connected.append((signal, slot))
        except Exception as e:
            logger.warning(
                f"attach_backend signal connect failed, rolling back "
                f"{len(connected)} connection(s): {e}"
            )
            self._disconnect_all(connected)
            return
        self._connections = connected
        self._connected = True

    @staticmethod
    def _disconnect_all(connections: list[tuple[Any, Any]]) -> None:
        """逐条断开 (signal, slot) 连接；单条失败只记 warning 不中断。"""
        for signal, slot in connections:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError) as e:
                logger.warning(f"disconnect failed during rollback: {e}")

    def detach_backend(self) -> None:
        # AUD-17500：按连接清单断连（部分连接失败后 _connected=False 的
        # 旧实现会跳过全部断连，造成连接与对象泄漏）。
        if self._backend is not None and self._connections:
            self._disconnect_all(self._connections)
        self._connections = []
        self._backend = None
        self._connected = False

    # ------------------------------------------------------------------
    # submit（规格书 §13.2）
    # ------------------------------------------------------------------

    def submit(self, task: TimelineGpuRenderTask) -> bool:
        """
        提交一个 GPU 合成任务。

        Returns:
            True if accepted by backend, False if backend unavailable.
        """
        if self._backend is None:
            self.render_error.emit(task.timeline_frame, "gpu backend not attached")
            return False

        # 约束检查（规格书 §13.3）：
        # - 不接受空 layer
        # - layer 必须按 track_index 排序
        if not task.layers:
            self.render_error.emit(task.timeline_frame, "no layers")
            return False

        # 转换为 backend 的 EditRenderTask
        try:
            backend_task = self._build_backend_task(task)
        except ValueError as e:
            # AUD-17501：backend 必然拒绝的 payload 类型在此显式拒绝。
            # 旧行为是照常构造任务——submit 返回 True 但 backend 永远
            # track_skip_unsupported，帧永不产出且 miss 归因误导。
            payload_type = getattr(e, "payload_type", "?")
            self.render_error.emit(
                task.timeline_frame,
                f"unsupported_payload_type:{payload_type} (realtime GPU path "
                "accepts gpu_texture/planar_yuv only)",
            )
            return False
        return self.submit_backend_task(backend_task)

    def submit_backend_task(self, backend_task: Any) -> bool:
        """Submit an already-expanded Edit GPU task through this boundary.

        ``TimelineGpuRenderTask`` is the compact scheduler-facing contract,
        but the Edit preview worker also carries effect chains, adjustment
        layers and procedural transition jobs that are richer than that
        contract.  This method lets the compositor own the real backend
        boundary without flattening or dropping those fields during the
        migration.
        """
        if self._backend is None:
            try:
                frame = int(getattr(backend_task, "frame", -1))
            except Exception:
                frame = -1
            self.render_error.emit(frame, "gpu backend not attached")
            return False
        if backend_task is None or not getattr(backend_task, "layers", None):
            try:
                frame = int(getattr(backend_task, "frame", -1))
            except Exception:
                frame = -1
            self.render_error.emit(frame, "no layers")
            return False

        # AUD-17505：计数器自增持锁。
        with self._stats_lock:
            self._submitted += 1
        try:
            ok = self._backend.submit(backend_task)
            if ok:
                with self._stats_lock:
                    self._gpu_composite_count += 1
            return bool(ok)
        except Exception as e:
            frame = int(getattr(backend_task, "frame", -1))
            self.render_error.emit(frame, f"submit failed: {e}")
            return False

    def _build_backend_task(self, task: TimelineGpuRenderTask) -> Any:
        """
        把 TimelineGpuRenderTask 转换为 backend (EditGpuPreviewRenderer) 的
        EditRenderTask。

        复用 backend 的真实 layer 数据结构：
        {
            "gpu_frame": GpuDecodedFrame, "frame_type": "gpu", "track_info": {...}
        }
        或
        {
            "decoded_frame": DecodedFrame, "frame_type": "planar", "track_info": {...}
        }

        注意：EditGpuPreviewRenderer._process_task() 不是消费裸 payload，
        它依赖 frame_type 与 track_info 判断渲染路径和轨道属性。这里必须
        保留 track_index / opacity 等信息，否则多轨合成会退化为不确定行为。
        """
        from src.features.edit.core.edit_gpu_preview_renderer import EditRenderTask

        layers = []
        for layer in task.layers:
            layers.append(self._build_backend_layer(layer))

        return EditRenderTask(
            frame=task.timeline_frame,
            generation_id=task.generation_id,
            layers=layers,
            viewport_size=(task.output_width, task.output_height),
            output_fbo_size=(task.output_width, task.output_height),
            coordinate_size=task.coordinate_size,
            deadline_ns=task.deadline_ns,
        )

    def _build_backend_layer(self, layer: GpuLayerInput) -> dict:
        """Convert a single realtime layer into EditGpuPreviewRenderer input."""
        frame = layer.frame
        payload = frame.payload
        track_info = {
            "opacity": float(layer.opacity),
            "track_index": int(layer.track_index),
            "clip_id": layer.clip_id,
            "transform": layer.transform,
            "crop": layer.crop,
            "blend_mode": layer.blend_mode,
            "asset_id": getattr(frame.identity, "asset_id", ""),
            "source_frame": frame.frame_index,
            "source_kind": (
                frame.source_kind.value
                if hasattr(frame.source_kind, "value")
                else str(frame.source_kind)
            ),
        }

        if isinstance(payload, dict):
            backend_layer = dict(payload)
            existing_track_info = dict(backend_layer.get("track_info") or {})
            existing_track_info.update(track_info)
            backend_layer["track_info"] = existing_track_info
            if not backend_layer.get("frame_type"):
                if backend_layer.get("gpu_frame") is not None:
                    backend_layer["frame_type"] = "gpu"
                elif backend_layer.get("decoded_frame") is not None:
                    backend_layer["frame_type"] = "planar"
            return backend_layer

        if frame.is_gpu_payload():
            return {
                "gpu_frame": payload,
                "frame_type": "gpu",
                "track_info": track_info,
            }

        if frame.is_planar() or getattr(payload, "is_planar", False):
            return {
                "decoded_frame": payload,
                "frame_type": "planar",
                "track_info": track_info,
            }

        # AUD-17501：cpu_ndarray/placeholder 等 payload 类型没有实时 GPU
        # 路径（backend 只接受 'gpu' / 'planar' / 'vt_planar'），为它们构造
        # 任务只会得到"submit 成功 + 永不产出 + unsupported miss 归因"。
        # 显式抛 ValueError 由 submit 转成语义化错误并返回 False。
        error = ValueError(
            f"payload type {frame.payload_type!r} has no realtime GPU path"
        )
        error.payload_type = frame.payload_type  # type: ignore[attr-defined]
        raise error

    # ------------------------------------------------------------------
    # generation control
    # ------------------------------------------------------------------

    def reset_generation(self, generation_id: int) -> None:
        """seek 后重置 generation，丢弃旧任务结果。"""
        self._generation = int(generation_id)
        if self._backend is not None:
            reset_fn = getattr(self._backend, "set_generation_id", None)
            if callable(reset_fn):
                try:
                    reset_fn(generation_id)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # backend signal handlers
    # ------------------------------------------------------------------

    def _on_backend_frame_ready(
        self, frame: int, generation: int, tex_id: int, width: int, height: int
    ) -> None:
        """转发 backend 的 frame_ready。"""
        # 转发即输出 GPU texture（规格书 §13.3：realtime 不输出 QImage）
        self.frame_ready.emit(frame, generation, tex_id, width, height)

    def _on_backend_present_frame_ready(self, present_frame: object) -> None:
        """转发 backend 的 GpuPresentFrame，保留 fence + slot ack 元数据。"""
        self.present_frame_ready.emit(present_frame)

    def _on_backend_render_error(self, frame: int, message: str) -> None:
        """转发 backend 的 render_error。GPU miss 由 engine hold/rebuffer，
        不自行 CPU fallback。"""
        self.render_error.emit(frame, message)

    # ------------------------------------------------------------------
    # shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        self.detach_backend()

    # ------------------------------------------------------------------
    # stats（规格书 §13.3 验收）
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        backend_deadline_drops = 0
        if self._backend is not None:
            try:
                raw_deadline_drops = getattr(
                    self._backend, "deadline_drop_count", 0
                )
                if isinstance(raw_deadline_drops, (int, float)):
                    backend_deadline_drops = int(raw_deadline_drops)
            except Exception:
                backend_deadline_drops = 0
        # AUD-17505：计数器快照在锁内完成。
        with self._stats_lock:
            return {
                "submitted": self._submitted,
                "gpu_composite_frames": self._gpu_composite_count,
                "cpu_qimage_frames": self._cpu_qimage_count,  # 必须 0
                "readback_count": self._readback_count,       # 必须 0
                "deadline_drops": backend_deadline_drops,
            }

    def reset_stats(self) -> None:
        # AUD-17505：清零与自增同锁。
        with self._stats_lock:
            self._submitted = 0
            self._gpu_composite_count = 0
            self._cpu_qimage_count = 0
            self._readback_count = 0
