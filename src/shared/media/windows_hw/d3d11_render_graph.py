"""Backend-neutral D3D11 render-plan and native execution contract.

The plan is compiled once per timeline/parameter revision.  Frame submission
only passes existing D3D11 handles to the native renderer; this module never
creates a texture, maps a staging resource, or returns a NumPy image.
"""

from __future__ import annotations

import threading

from dataclasses import dataclass
from typing import Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

from .d3d11_frame import D3D11GpuFrame
from .d3d11_effects import D3D11_EFFECTS
from .d3d11_shaders import D3D11_SHADER_SOURCES
from .d3d11_texture_pool import TextureLease
from .reasons import FallbackReason, param

__all__ = [
    "D3D11RenderPass",
    "D3D11RenderPlan",
    "D3D11RenderTarget",
    "D3D11RenderResult",
    "D3D11RenderBackend",
    "compile_d3d11_render_plan",
    "D3D11TimelineRenderer",
]

_PASS_NAMES = {
    "decode",
    "resize",
    "primary_grade",
    "color_convert",
    "blend",
    "transition",
    "effect",
    "yuv_conversion",
}
_OUTPUT_FORMATS = {"rgba16f", "nv12", "p010"}


@dataclass(frozen=True)
class D3D11RenderPass:
    name: str
    shader_id: str
    input_formats: Tuple[str, ...]
    output_format: str
    parameters: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.name not in _PASS_NAMES:
            raise ValueError(f"未知 D3D11 render pass: {self.name}")
        if not self.shader_id:
            raise ValueError("D3D11 render pass 缺少 shader_id")
        if self.shader_id not in D3D11_SHADER_SOURCES:
            raise ValueError(f"D3D11 render pass 缺少 shader source: {self.shader_id}")
        if any(fmt not in _OUTPUT_FORMATS for fmt in self.input_formats):
            raise ValueError(f"D3D11 render pass 输入格式非法: {self.input_formats}")
        if self.output_format not in _OUTPUT_FORMATS:
            raise ValueError(f"未知 D3D11 render format: {self.output_format}")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "shader_id": self.shader_id,
            "input_formats": list(self.input_formats),
            "output_format": self.output_format,
            "parameters": dict(self.parameters),
        }


@dataclass(frozen=True)
class D3D11RenderPlan:
    timeline_revision: str
    renderer_version: str
    source_format: str
    output_format: str
    passes: Tuple[D3D11RenderPass, ...]
    intermediate_slots: int
    layers: int
    transitions: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.timeline_revision or not self.renderer_version:
            raise ValueError("RenderPlan 必须绑定 timeline revision 和 renderer version")
        if self.output_format not in _OUTPUT_FORMATS:
            raise ValueError(f"不支持的 RenderPlan 输出格式: {self.output_format}")
        if self.intermediate_slots < 0 or self.layers < 1:
            raise ValueError("RenderPlan 的资源槽位和图层数非法")

    @property
    def cache_key(self) -> Tuple[object, ...]:
        return (
            self.timeline_revision,
            self.renderer_version,
            self.source_format,
            self.output_format,
            tuple((p.name, p.shader_id, p.output_format, p.parameters) for p in self.passes),
            self.intermediate_slots,
            self.layers,
            self.transitions,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "timeline_revision": self.timeline_revision,
            "renderer_version": self.renderer_version,
            "source_format": self.source_format,
            "output_format": self.output_format,
            "passes": [item.to_dict() for item in self.passes],
            "intermediate_slots": self.intermediate_slots,
            "layers": self.layers,
            "transitions": list(self.transitions),
        }


@dataclass(frozen=True)
class D3D11RenderTarget:
    texture_ptr: int
    dxgi_format: int
    width: int
    height: int
    adapter_luid: str
    lease: TextureLease
    completion_fence: object

    def __post_init__(self) -> None:
        if not self.texture_ptr or self.width <= 0 or self.height <= 0:
            raise ValueError("D3D11 render target 无效")
        if not self.adapter_luid:
            raise ValueError("D3D11 render target 缺少 adapter LUID")

    def release_after(self, fence: object | None = None) -> None:
        self.lease.release_after(fence or self.completion_fence)


@dataclass(frozen=True)
class D3D11RenderResult:
    target: Optional[D3D11RenderTarget]
    reason: str = ""
    gpu_render_ms: Optional[float] = None
    gpu_yuv_ms: Optional[float] = None

    @property
    def ok(self) -> bool:
        return self.target is not None and not self.reason


@runtime_checkable
class D3D11RenderBackend(Protocol):
    """Native HLSL renderer API; it must not expose CPU pixels."""

    def prepare_plan(self, plan: Mapping[str, object], adapter_luid: str) -> None:
        ...

    def render_frame(
        self, plan: Mapping[str, object], source_texture: int, frame_index: int,
        width: int, height: int,
    ) -> D3D11RenderResult | Mapping[str, object]:
        ...

    def release(self) -> None:
        ...


def compile_d3d11_render_plan(
    *,
    timeline_revision: str,
    renderer_version: str,
    source_format: str,
    output_format: str,
    layers: int = 1,
    effects: Sequence[str] = (),
    transitions: Sequence[str] = (),
    resize: bool = False,
    primary_grade: bool = False,
) -> D3D11RenderPlan:
    """Compile a deterministic pass list; no native resources are allocated."""
    if layers < 1:
        raise ValueError("D3D11 RenderPlan 至少需要一层")
    passes: list[D3D11RenderPass] = [
        D3D11RenderPass("decode", "d3d11.decode.nv12_p010", (source_format,), "rgba16f"),
    ]
    if resize:
        passes.append(D3D11RenderPass("resize", "d3d11.resize.linear", ("rgba16f",), "rgba16f"))
    if primary_grade:
        passes.append(D3D11RenderPass("primary_grade", "d3d11.grade.primary", ("rgba16f",), "rgba16f"))
    for effect in effects:
        name = str(effect).strip().lower()
        if not name:
            raise ValueError("RenderPlan effect 名不能为空")
        spec = D3D11_EFFECTS.get(name)
        if spec is None:
            raise ValueError(f"D3D11 effect 未声明: {name}")
        passes.append(D3D11RenderPass("effect", spec.shader_id, ("rgba16f",), "rgba16f"))
    if layers > 1:
        passes.append(D3D11RenderPass("blend", "d3d11.composite.blend", ("rgba16f",), "rgba16f"))
    for transition in transitions:
        name = str(transition).strip().lower()
        if not name:
            raise ValueError("RenderPlan transition 名不能为空")
        if name not in {"wipe", "iris", "slide", "user"}:
            raise ValueError(f"D3D11 transition 未声明: {name}")
        passes.append(D3D11RenderPass("transition", f"d3d11.transition.{name}", ("rgba16f",), "rgba16f"))
    final_pass = "yuv_conversion" if output_format in {"nv12", "p010"} else "color_convert"
    passes.append(D3D11RenderPass(final_pass, f"d3d11.output.{output_format}", ("rgba16f",), output_format))
    return D3D11RenderPlan(
        timeline_revision=timeline_revision,
        renderer_version=renderer_version,
        source_format=source_format,
        output_format=output_format,
        passes=tuple(passes),
        intermediate_slots=max(0, len(passes) - 1),
        layers=layers,
        transitions=tuple(str(item).lower() for item in transitions),
    )


class D3D11TimelineRenderer:
    """Fail-closed native renderer facade for Windows export."""

    VERSION = "d3d11-renderer-v1"

    def __init__(
        self,
        adapter_luid: str,
        backend: D3D11RenderBackend | None = None,
    ) -> None:
        self._adapter_luid = adapter_luid
        self._backend = backend
        self._plan: D3D11RenderPlan | None = None
        self._released = False
        self._last_reason = FallbackReason.NOT_PROBED.value
        self._plans_prepared = 0
        self._frames_rendered = 0
        # AUD-36107：生命周期状态（_plan/_backend/_released）与帧提交必须
        # 互斥——release()（导出线程 teardown）并发置空 _plan/_backend 会让
        # render_frame() 的属性访问抛 AttributeError，逃出"只返回
        # D3D11RenderResult"的异常契约。锁只串行化 Python 侧状态切换与
        # 原生调用的进入/退出；逐帧渲染本身仍由调用方顺序驱动。
        self._state_lock = threading.Lock()

    @property
    def last_reason(self) -> str:
        return self._last_reason

    @property
    def plan(self) -> D3D11RenderPlan | None:
        return self._plan

    @property
    def plans_prepared(self) -> int:
        return self._plans_prepared

    @property
    def frames_rendered(self) -> int:
        return self._frames_rendered

    def prepare(self, plan: D3D11RenderPlan) -> Tuple[bool, str]:
        with self._state_lock:
            if self._released:
                self._last_reason = param(FallbackReason.GPU_PROBE_FAILED, "renderer_released")
                return False, self._last_reason
            if not self._adapter_luid:
                self._last_reason = FallbackReason.ADAPTER_MISMATCH.value + ":renderer_luid_unavailable"
                return False, self._last_reason
            if self._backend is None:
                self._last_reason = FallbackReason.NOT_YET_IMPLEMENTED.value
                return False, self._last_reason
            try:
                self._backend.prepare_plan(plan.to_dict(), self._adapter_luid)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                self._last_reason = param(FallbackReason.GPU_PROBE_FAILED, f"prepare_plan:{exc}")
                return False, self._last_reason
            self._plan = plan
            self._plans_prepared += 1
            self._last_reason = ""
            return True, ""

    def render_frame(self, frame: D3D11GpuFrame) -> D3D11RenderResult:
        # 锁跨越原生调用：release() 的状态清空必须等待在途帧提交完成后
        # 才能进行（反向亦然），杜绝"检查非 None → 被置 None → 使用"窗口。
        with self._state_lock:
            backend = self._backend
            plan = self._plan
            if plan is None or backend is None:
                reason = self._last_reason or FallbackReason.NOT_PROBED.value
                return D3D11RenderResult(None, reason)
            if frame.adapter_luid != self._adapter_luid:
                reason = (
                    f"{FallbackReason.ADAPTER_MISMATCH.value}:"
                    f"frame={frame.adapter_luid}|renderer={self._adapter_luid}"
                )
                self._last_reason = reason
                return D3D11RenderResult(None, reason)
            try:
                raw = backend.render_frame(
                    plan.to_dict(), frame.texture_ptr, frame.frame_index,
                    frame.width, frame.height,
                )
                result = raw if isinstance(raw, D3D11RenderResult) else _render_result_from_mapping(raw)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                reason = param(FallbackReason.GPU_PROBE_FAILED, f"render_frame:{exc}")
                self._last_reason = reason
                return D3D11RenderResult(None, reason)
            if not result.ok:
                self._last_reason = result.reason or FallbackReason.GPU_PROBE_FAILED.value
                return result
            self._frames_rendered += 1
            self._last_reason = ""
            return result

    def release(self) -> None:
        with self._state_lock:
            if self._released:
                return
            self._released = True
            if self._backend is not None:
                try:
                    self._backend.release()
                except (OSError, RuntimeError, TypeError, ValueError):
                    # Release is best-effort during exporter teardown; the
                    # renderer remains fail-closed and must not mask the export
                    # outcome with a native cleanup exception.
                    pass
            self._backend = None
            self._plan = None


def _render_result_from_mapping(raw: Mapping[str, object]) -> D3D11RenderResult:
    if not isinstance(raw, Mapping):
        raise TypeError("native renderer 返回值必须是 D3D11RenderResult 或 mapping")
    target = raw.get("target")
    if not isinstance(target, D3D11RenderTarget):
        raise ValueError("native renderer 必须返回 D3D11RenderTarget")
    return D3D11RenderResult(
        target=target,
        reason=str(raw.get("reason", "")),
        gpu_render_ms=(float(raw["gpu_render_ms"]) if raw.get("gpu_render_ms") is not None else None),
        gpu_yuv_ms=(float(raw["gpu_yuv_ms"]) if raw.get("gpu_yuv_ms") is not None else None),
    )
