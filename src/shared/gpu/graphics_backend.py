"""GraphicsBackend 契约（计划 P3-02 / §3.2）。

四维 backend 模型（``backend_types.py``）中 graphics 维度的操作接口：
initialize、create_texture、create_target、render_pass、present、release。

落地说明（对计划 v2 原文的一处勘误）：计划 §3.2 写「扩展
``src/shared/gpu/gl_backend.py``」，实际契约落为独立模块——契约必须能被
Phase 4 的 Metal backend 在无 moderngl 依赖的情况下 import，而
``gl_backend.py`` 是被十余个模块引用的运行时助手
（GLContextManager / FramebufferManager），保持原样不动。
具体 GL 实现按 §3.4 的边界裁定放在 ``src/features/color/core/
opengl_render_backend.py``（需要包装 feature 侧 renderer，放 shared 会
违反 ``module_boundary_rules.md``）。

线程契约（``.clinerules`` / 计划 §7.1）：

- ``initialize`` / ``create_texture`` / ``create_target`` / ``release``
  是真实 GL 操作，只允许 backend 渲染线程调用。
- ``render_pass`` 经 orchestrator 命令队列投递，任意线程可调用。
- ``present`` 只读取已发布的 front buffer 描述符，不做 GL 调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Protocol, runtime_checkable


class GraphicsBackendState(str, Enum):
    """backend 生命周期状态（计划 §4.2：切换时必须先 drain 再 release）。"""

    UNINITIALIZED = "uninitialized"
    READY = "ready"
    RELEASED = "released"
    FAILED = "failed"


class GraphicsBackendError(RuntimeError):
    """backend 契约违例或能力不足时的显式诊断（不静默降级）。"""


class BackendNotReadyError(GraphicsBackendError):
    """backend 尚未完成初始化（或已释放）就收到了 GL 操作请求。"""


class BackendThreadViolationError(GraphicsBackendError):
    """在非渲染线程上发起了只允许渲染线程执行的 GL 操作。"""


@dataclass(frozen=True)
class TextureDescriptor:
    """创建纹理所需的最小描述（后端无关，不含任何 GL/Metal 常量）。"""

    width: int
    height: int
    components: int = 4
    dtype: str = "f2"
    data: Any = None


@dataclass(frozen=True)
class PresentDescriptor:
    """已发布 front buffer 的只读描述，供 UI 侧 blit 与诊断使用。"""

    texture_id: Optional[int]
    width: int
    height: int
    # fence 由拥有资源的 backend 解释（GLsync / Metal event / None）。
    fence: Any = None


@runtime_checkable
class GraphicsBackend(Protocol):
    """graphics 维度的后端契约（OpenGL 实现见 features/color/core）。

    Phase 4 的 ``MetalComputeBackend`` / 未来 D3D 后端实现同一契约，
    ``backend_selector`` 据此做多后端调度而不感知具体 API。
    """

    @property
    def backend_id(self) -> str:
        """稳定标识（'opengl' / 'metal' / ...），匹配 GraphicsBackendKind。"""
        ...

    @property
    def graphics_api(self) -> str:
        """四维模型中的 graphics_api 值（如 'opengl_4.1'）。"""
        ...

    @property
    def state(self) -> GraphicsBackendState:
        """当前生命周期状态（UNINITIALIZED / READY / RELEASED / FAILED）。"""
        ...

    def initialize(self) -> None:
        """确认 backend 就绪；能力不足时抛 GraphicsBackendError。"""
        ...

    def create_texture(self, descriptor: TextureDescriptor) -> Any:
        """按后端无关描述创建纹理，返回后端句柄（渲染线程调用）。"""
        ...

    def create_target(self, texture: Any) -> Any:
        """为纹理创建渲染目标（GL: FBO；Metal: render pass descriptor）。"""
        ...

    def render_pass(self, command: int, data: Optional[Dict[str, Any]] = None) -> None:
        """提交一次渲染（经 orchestrator 命令队列，线程安全）。"""
        ...

    def present(self) -> Optional[PresentDescriptor]:
        """读取当前可呈现的 front buffer 描述符（不做 GL 调用）。"""
        ...

    def release(self) -> None:
        """释放 backend 名下资源并复位状态（渲染线程调用）。"""
        ...

    def resource_report(self) -> Dict[str, Any]:
        """资源台账诊断（组 → 资源名列表）。"""
        ...
