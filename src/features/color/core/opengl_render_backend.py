"""OpenGLRenderBackend — 既有 ModernGL 渲染管线的 GraphicsBackend facade。

计划 P3-02 / §3.4：本类是 graphics 维度契约
（``src/shared/gpu/graphics_backend.py``）的 OpenGL 具体实现。放在
feature 侧而非 ``src/shared/``，因为它包装的是 feature 侧 orchestrator
（``AsyncRenderer``）；放 shared 会违反 ``module_boundary_rules.md``。

关键约束（P3-01 行为不变）：

- **不创建平行渲染路径。** 全部 GL 资源仍由 AsyncRenderer 渲染线程按原有
  时机创建与释放；本类只做契约包装与资源台账（``GLResourceRegistry``）。
- ``create_texture`` / ``create_target`` 是真实 GL 调用，只允许渲染线程
  调用，违例抛 ``BackendThreadViolationError``（显式诊断，不静默降级）。
- ``render_pass`` 走 ``orchestrator.submit``——命令队列本身就是 orchestrator
  设计好的跨线程入口（队列合并 / lane / revision 语义原样保留）。
- ``present`` 只读 front buffer 描述符，不做 GL 调用；UI blit 仍由
  ImageEngine 按既有 front fence 协议执行。
- ``release`` 是台账模式：真实释放由 ``AsyncRenderer._cleanup`` 在渲染线程
  完成，此处只清台账、复位状态。Phase 4 backend 切换时，
  ``registry.release_group`` 才开始承担实际释放回调编排。
"""

from __future__ import annotations

import logging

import moderngl
from PyQt6.QtCore import QThread
from typing import Any, Dict, Optional

from src.shared.gpu.backend_types import GraphicsBackendKind
from src.shared.gpu.gl_resource_registry import GLResourceRegistry
from src.shared.gpu.graphics_backend import (
    BackendNotReadyError,
    BackendThreadViolationError,
    GraphicsBackendError,
    GraphicsBackendState,
    PresentDescriptor,
    TextureDescriptor,
)

logger = logging.getLogger(__name__)


class OpenGLRenderBackend:
    """AsyncRenderer 的 GraphicsBackend 视图（'opengl' 维度的正式门面）。"""

    backend_id: str = GraphicsBackendKind.OPENGL.value

    def __init__(
        self,
        orchestrator: Any,
        *,
        owner_thread: Optional[QThread] = None,
        registry: Optional[GLResourceRegistry] = None,
        owner_name: str = "async_renderer",
    ) -> None:
        """
        Args:
            orchestrator: 拥有 GL context 的 ``AsyncRenderer`` 实例。
            owner_thread: 唯一允许执行真实 GL 操作的线程（默认 orchestrator
                所在线程）；仅供无头测试注入。
            registry: 外部注入的资源台账（默认自建）。
            owner_name: 台账 owner 标识。
        """
        self._orchestrator = orchestrator
        self._owner_thread = owner_thread if owner_thread is not None else orchestrator
        self.registry = registry if registry is not None else GLResourceRegistry(owner=owner_name)
        self._state = GraphicsBackendState.UNINITIALIZED

    # ------------------------------------------------------------------
    # 契约属性
    # ------------------------------------------------------------------

    @property
    def graphics_api(self) -> str:
        return GraphicsBackendKind.OPENGL.value

    @property
    def state(self) -> GraphicsBackendState:
        return self._state

    @property
    def orchestrator(self) -> Any:
        return self._orchestrator

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """确认渲染线程已完成 ModernGL 初始化；不创建任何资源。

        资源创建仍按既有 ``run()`` 顺序执行（P3-01 行为不变）；此处只把
        已就绪事实登记为 READY，能力不足时显式诊断。
        """
        if self._state == GraphicsBackendState.READY:
            return
        if self._state == GraphicsBackendState.RELEASED:
            raise GraphicsBackendError(
                "OpenGLRenderBackend 已释放；backend 重新初始化必须经"
                "计划 §4.2 的安全切换流程（quiesce → release → rebuild）"
            )
        if getattr(self._orchestrator, "ctx", None) is None:
            self._state = GraphicsBackendState.FAILED
            raise BackendNotReadyError(
                "OpenGL backend 初始化失败：orchestrator 尚无 ModernGL context"
                "（渲染线程未完成 run() 初始化）"
            )
        self._state = GraphicsBackendState.READY

    def release(self) -> None:
        """清空资源台账并标记 RELEASED（渲染线程调用）。

        AUD-0731: 与 ``create_*`` 的 ``_assert_render_thread`` 硬守卫相对
        称，release 的渲染线程契约不再仅靠 docstring 约定：当台账中存在
        带 ``release_fn`` 的记录且当前线程不是渲染线程时（``release_all``
        会在调用线程执行 GL 释放回调，有崩溃/驱动错误风险），记录 error
        日志显式暴露违例。纯台账记录（``release_fn=None``，当前生产的
        唯一登记方式）在任何线程清台账均安全，行为不变。
        """
        if self._state == GraphicsBackendState.RELEASED:
            return
        self._warn_release_fn_off_thread()
        self.registry.release_all()
        self._state = GraphicsBackendState.RELEASED

    # ------------------------------------------------------------------
    # 真实 GL 操作（仅渲染线程）
    # ------------------------------------------------------------------

    def create_texture(self, descriptor: TextureDescriptor) -> Any:
        """在 orchestrator 的共享 context 中创建纹理（LINEAR + clamp）。"""
        self._assert_render_thread("create_texture")
        ctx = self._require_ctx()
        tex = ctx.texture(
            (descriptor.width, descriptor.height),
            descriptor.components,
            data=descriptor.data,
            dtype=descriptor.dtype,
        )
        # 与 GLContextManager.create_texture 一致：LINEAR 平滑缩放 +
        # CLAMP_TO_EDGE 防止 tile 拼接边缘伪影。
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        tex.repeat_x = False
        tex.repeat_y = False
        return tex

    def create_target(self, texture: Any) -> Any:
        """为纹理取/建 FBO（经 FramebufferManager 缓存，渲染线程调用）。"""
        self._assert_render_thread("create_target")
        self._require_ctx()
        fb_manager = getattr(self._orchestrator, "fb_manager", None)
        if fb_manager is None:
            raise BackendNotReadyError(
                "OpenGL backend 无可用 FramebufferManager（渲染线程未完成初始化）"
            )
        return fb_manager.get_fbo(texture)

    # ------------------------------------------------------------------
    # 跨线程安全入口
    # ------------------------------------------------------------------

    def render_pass(self, command: int, data: Optional[Dict[str, Any]] = None) -> None:
        """经 orchestrator 命令队列提交一次渲染（lane 合并语义不变）。"""
        self._orchestrator.submit(command, data if data is not None else {})

    def present(self) -> Optional[PresentDescriptor]:
        """读取当前 front buffer 描述符（只读，无 GL 调用）。"""
        orch = self._orchestrator
        width, height = getattr(orch, "current_front_size", (0, 0)) or (0, 0)
        return PresentDescriptor(
            texture_id=getattr(orch, "current_front_tex_id", None),
            width=int(width),
            height=int(height),
            fence=getattr(orch, "current_front_fence", None),
        )

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------

    def resource_report(self) -> Dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "graphics_api": self.graphics_api,
            "state": self._state.value,
            "owner": self.registry.owner,
            "groups": self.registry.snapshot(),
        }

    # ------------------------------------------------------------------
    # 内部守卫
    # ------------------------------------------------------------------

    def _assert_render_thread(self, operation: str) -> None:
        if QThread.currentThread() is not self._owner_thread:
            raise BackendThreadViolationError(
                f"OpenGLRenderBackend.{operation} 只允许渲染线程调用"
                f"（当前线程 {QThread.currentThread().objectName() or QThread.currentThread()}）"
            )

    def _warn_release_fn_off_thread(self) -> None:
        """AUD-0731: 非渲染线程释放带 ``release_fn`` 的台账时显式告警。

        台账中存在 GL 释放回调而在错误线程执行 ``release_all`` 是
        崩溃/驱动错误级别违例——必须留下可见诊断（不静默）。
        """
        if QThread.currentThread() is self._owner_thread:
            return
        offenders: list[str] = []
        for group, names in self.registry.snapshot().items():
            for name in names:
                record = self.registry.get(group, name)
                if record is not None and record.release_fn is not None:
                    offenders.append(f"{group}/{name}")
        if offenders:
            logger.error(
                "OpenGLRenderBackend.release called off the render thread "
                "while GL release callbacks are registered (%s); release_fn "
                "runs on the calling thread",
                ", ".join(offenders),
            )

    def _require_ctx(self) -> Any:
        ctx = getattr(self._orchestrator, "ctx", None)
        if ctx is None:
            raise BackendNotReadyError(
                "orchestrator 无 ModernGL context（渲染线程未初始化或已清理）"
            )
        return ctx
