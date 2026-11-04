"""R06-B: bake 执行后端注册表（fail-closed 能力声明 + worker 契约）。

设计（2026-11-05 v1）：

- **后端 = 纯函数式 worker 契约**：``run(request, snapshot, source,
  cancel_check, progress_cb) -> BakeBackendOutput``。宿主
  （``ColorBakeService``）负责线程、生命周期与结果注册；后端不碰工程
  状态、不做结果发布。
- **能力 fail-closed**：未注册 / ``available=False`` 的后端提交即拒绝
  （``backend_unregistered`` / ``backend_unavailable``），预检与 UI 都
  能看到 disabled_reason。v1 只实现 ``onnx_tile``（.tai AI 插件 CPU
  分块推理）；``gpu_prefix``（AsyncRenderer 栈前缀）与 ``stylize``
  显式登记为未实现。
- **取消协作**：worker 周期调用 ``cancel_check()``；``onnx_tile`` 额外
  把活动的 ``AIPluginWorker`` 登记进模块级表，宿主/UI 可经
  ``request_active_cancel(task_id)`` 跨线程触发 tile 边界取消（与
  AUD-31500 协作语义一致：绝不 terminate 原生推理）。
- **结果版本化**：``npz``（float16 RGBA + 对象元数据：spec/双 revision/
  栈/后端），由 ``ColorBakeResultSession`` 内容寻址落盘。
"""
from __future__ import annotations

import io
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from src.features.color.core.color_bake_protocol import (
    ColorBakeCapability, ColorBakeRequest,
)


class BakeBackendError(RuntimeError):
    """后端执行失败（code 进 UI 状态，不带像素数据）。"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class BakeCancelled(Exception):
    """协作取消（worker 主动让出；宿主据此释放暂存）。"""


@dataclass(frozen=True)
class BakeBackendOutput:
    """一次成功 bake 的产物：字节 + 格式 + 描述性元数据。"""

    payload: bytes                  # 版本化结果体（v1: npz）
    format_hint: str                # "npz" / "exr" / ...
    metadata: Mapping[str, Any] = field(default_factory=dict)


#: worker 签名：(request, snapshot, source, cancel_check, progress_cb) -> output
BakeWorker = Callable[
    [ColorBakeRequest, Any, Any, Callable[[], bool], Callable[[float], None]],
    BakeBackendOutput]


class ColorBakeBackendRegistry:
    """backend_id → (worker, capability)；未注册即拒绝（fail-closed）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._workers: dict[str, BakeWorker] = {}
        self._capabilities: dict[str, ColorBakeCapability] = {}

    def register(self, capability: ColorBakeCapability,
                 worker: BakeWorker) -> None:
        with self._lock:
            self._capabilities[capability.backend_id] = capability
            self._workers[capability.backend_id] = worker

    def capability_of(self, backend_id: str) -> ColorBakeCapability | None:
        with self._lock:
            return self._capabilities.get(backend_id)

    def capabilities(self) -> list[ColorBakeCapability]:
        with self._lock:
            return [self._capabilities[key]
                    for key in sorted(self._capabilities)]

    def resolve(self, backend_id: str) -> tuple[BakeWorker | None,
                                                ColorBakeCapability | None,
                                                str]:
        """返回 (worker, capability, 拒绝码)；拒绝码为空表示可用。"""
        with self._lock:
            capability = self._capabilities.get(backend_id)
            worker = self._workers.get(backend_id)
        if capability is None or worker is None:
            return None, None, "backend_unregistered"
        if not capability.available:
            return None, capability, "backend_unavailable"
        return worker, capability, ""


_registry = ColorBakeBackendRegistry()

# task_id → AIPluginWorker（跨线程协作取消桥；只登记活动推理）
_active_workers: dict[str, Any] = {}
_active_lock = threading.Lock()


def get_bake_backend_registry() -> ColorBakeBackendRegistry:
    """进程级注册表（宿主装配与测试注入共用）。"""
    return _registry


def request_active_cancel(task_id: str) -> bool:
    """跨线程触发活动推理的协作取消（tile 边界生效；无活动返回 False）。"""
    with _active_lock:
        worker = _active_workers.get(task_id)
    if worker is None:
        return False
    worker.request_cancel()
    return True


def _onnx_tile_worker(request: ColorBakeRequest, snapshot: Any,
                      source: Any, cancel_check: Callable[[], bool],
                      progress_cb: Callable[[float], None],
                      ) -> BakeBackendOutput:
    """v1 唯一实现后端：.tai AI 插件 CPU 分块推理。

    在调用线程同步执行 ``AIPluginWorker._process``（宿主已在专用后台
    线程；不嵌 QThread 事件循环）。输入为宿主钉住的源帧（float RGB
    0..1；HDR 由 bake 语义先显示钳制——与 AI 插件预览一致）；
    ``plugin_data`` 来自请求 params（.tai 元数据）。模型缺失在能力
    声明期 fail-closed，此处再兜底一次（防装配后模型被删）。
    """
    import numpy as np

    plugin_data = dict(request.params.get("plugin_data") or {})
    model_path = str(plugin_data.get("model_path") or "")
    if not model_path:
        raise BakeBackendError("ai_model_missing", "插件未声明模型路径")

    image = np.asarray(source)
    if image.ndim != 3 or image.shape[-1] < 3:
        raise BakeBackendError("bad_source_shape",
                               f"源帧形状非法: {image.shape}")
    # bake 输入语义：显示钳制后的 RGB（AI 插件预览同口径）。
    rgb = np.clip(image[..., :3], 0.0, 1.0).astype(np.float32)

    if cancel_check():
        raise BakeCancelled()

    from src.shared.ai_models.ai_plugin_engine import AIPluginWorker

    progress_holder = {"percent": 0.0}
    worker = AIPluginWorker(
        layer_id=str(request.target.get("node_id") or ""),
        instance_id=str(request.target.get("instance_id") or request.task_id),
        image=rgb, plugin_data=plugin_data,
        plugin_params=dict(request.params.get("plugin_params") or {}))
    worker.progress.connect(
        lambda _lid, _iid, pct: progress_holder.__setitem__("percent", pct))

    with _active_lock:
        _active_workers[request.task_id] = worker
    try:
        progress_cb(5.0)
        output = worker._process()  # noqa: SLF001 — 同步复用（见模块 docstring）
    except FileNotFoundError as exc:
        raise BakeBackendError("ai_model_missing", str(exc)[:200]) from exc
    except Exception as exc:  # noqa: BLE001 — 后端失败统一编码
        if cancel_check() or worker._is_cancelled():  # noqa: SLF001
            raise BakeCancelled() from exc
        raise BakeBackendError("backend_exec_failed",
                               str(exc)[:200]) from exc
    finally:
        with _active_lock:
            _active_workers.pop(request.task_id, None)

    if output is None or cancel_check() or worker._is_cancelled():  # noqa: SLF001
        raise BakeCancelled()

    import numpy as np2

    height, width = output.shape[:2]
    rgba = np2.concatenate([output, np2.ones(output.shape[:2] + (1,),
                                             dtype=np2.float32)], axis=-1)
    buffer = io.BytesIO()
    np2.savez_compressed(
        buffer,
        image=np2.ascontiguousarray(rgba, dtype=np2.float16),
        meta=np2.asarray({
            "resource_id": request.resource_id,
            "spec_version": request.spec_version,
            "graph_revision": request.graph_revision,
            "source_content_revision": request.source_content_revision,
            "stack_id": request.stack_id,
            "backend_id": request.backend_id,
        }, dtype=object),
        allow_pickle=True)
    progress_cb(100.0)
    return BakeBackendOutput(
        payload=buffer.getvalue(), format_hint="npz",
        metadata={"width": width, "height": height, "dtype": "f2",
                  "model_path": model_path,
                  "tile_progress": progress_holder["percent"]})


def _fail_worker(backend_id: str) -> BakeWorker:
    def worker(*_args: Any, **_kwargs: Any) -> BakeBackendOutput:
        raise BakeBackendError(
            "backend_unavailable", f"后端未实现: {backend_id}")
    return worker


def install_default_backends() -> None:
    """装配默认能力表：v1 仅 onnx_tile 可用；其余显式 not_implemented。"""
    registry = get_bake_backend_registry()
    if registry.capability_of("onnx_tile") is not None:
        return  # 幂等（重复装配不重复注册）

    def _onnx_available() -> tuple[bool, str]:
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            return False, "onnxruntime_missing"
        return True, ""

    available, reason = _onnx_available()
    registry.register(
        ColorBakeCapability(
            backend_id="onnx_tile",
            categories=("upscale", "denoise", "deblur", "enhance"),
            available=available, disabled_reason=reason),
        _onnx_tile_worker)
    registry.register(
        ColorBakeCapability(
            backend_id="gpu_prefix", categories=("stack_prefix",),
            available=False, disabled_reason="not_implemented"),
        _fail_worker("gpu_prefix"))
    registry.register(
        ColorBakeCapability(
            backend_id="stylize", categories=("style",),
            available=False, disabled_reason="not_implemented"),
        _fail_worker("stylize"))
