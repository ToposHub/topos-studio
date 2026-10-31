"""P2-02: BackendCapabilities — probe-backed four-dim capability matrix.

Extends (delegates to) the existing capability probes instead of building a
parallel truth:

- decode/VT   : ``src.shared.media.macos_vt.hardware_capability``
- windows     : ``src.shared.media.windows_hw.capability``
- interop     : ``src.shared.media.topos_surface_capability`` (probe-backed;
  zero-copy claims are trusted only with ``evidence == "probe"``, P0-06)
- graphics    : the live GL driver string (renderer/version), when a context
  exists; without a context only static platform facts are reported.

Diagnostics rule (.clinerules:11): an unavailable or unprobed dimension is
reported with an explicit reason — never silently treated as available.

Thread boundary: the ``detect_*`` functions perform no GL calls of their
own and are safe to call from any thread.  Live GL facts
(``gl_version``/``gl_renderer``) must be captured by the caller on the
render thread that owns the context; ``detect_decode_backend`` performs
real file I/O (macOS VT probe decodes a sample) so callers should expect
it to block.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass, field
from typing import Any, Optional

from src.shared.gpu.backend_types import (
    BackendSelection,
    ComputeBackendKind,
    DecodeBackendKind,
    GraphicsBackendKind,
    InteropBackendKind,
)


@dataclass(frozen=True)
class DimensionCapability:
    """Probe result for one dimension."""

    kind: Any                    # *BackendKind member (AUTO means "unresolved")
    available: bool
    reason: str = ""
    zero_copy: bool = False      # only meaningful for interop/decode dims
    evidence: str = "static"     # static | env_override | probe

    def as_dict(self) -> dict[str, object]:
        """JSON-friendly snapshot; ``kind`` becomes its enum value string."""
        return {
            "kind": self.kind.value,
            "available": self.available,
            "reason": self.reason,
            "zero_copy": self.zero_copy,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class BackendCapabilities:
    """Resolved four-dim capability snapshot for the current machine.

    ``platform`` 额外携带按 kind 值索引的平台专属条目（W0-03：
    ``d3d11``/``d3d11va``/``nvdec``/``qsv``/``amf``/``cuda``/``directml``），
    供 backend_selector 在显式请求或 AUTO trail 中给出
    ``not_windows`` 类显式 reason，而不是 ``*_backend_unprobed``。
    """

    graphics: DimensionCapability
    compute: DimensionCapability
    decode: DimensionCapability
    interop: DimensionCapability
    environment: dict[str, str] = field(default_factory=dict)
    platform: dict[str, "DimensionCapability"] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        """JSON-friendly snapshot (platform entries sorted by kind value)."""
        return {
            "graphics": self.graphics.as_dict(),
            "compute": self.compute.as_dict(),
            "decode": self.decode.as_dict(),
            "interop": self.interop.as_dict(),
            "environment": dict(self.environment),
            "platform": {k: v.as_dict() for k, v in sorted(self.platform.items())},
        }

    def unavailable_reasons(self) -> dict[str, str]:
        """Dimensions explicitly reported unavailable (for diagnostics UI)."""
        out: dict[str, str] = {}
        for name in ("graphics", "compute", "decode", "interop"):
            cap: DimensionCapability = getattr(self, name)
            if not cap.available and cap.reason:
                out[name] = cap.reason
        return out


def _os_name(system: Optional[str]) -> str:
    """Normalized OS name (injectable for tests; defaults to this machine)."""
    return (system or platform.system()).strip().lower()


def detect_graphics_backend(
    gl_version: Optional[str] = None,
    gl_renderer: Optional[str] = None,
    *,
    system: Optional[str] = None,
) -> DimensionCapability:
    """Graphics dim: report the live driver when a GL context exists.

    ``gl_version``/``gl_renderer`` come from the existing render thread
    context (moderngl ``ctx.info``).  When there is no live GL context on
    macOS, probe the Metal device used by the timeline compute renderer.
    """
    if gl_version:
        return DimensionCapability(
            kind=GraphicsBackendKind.OPENGL,
            available=True,
            reason="",
            evidence="probe",
        )
    if _os_name(system) == "darwin":
        metal_available = _metal_device_available()
        return DimensionCapability(
            kind=GraphicsBackendKind.METAL,
            available=metal_available,
            reason="" if metal_available else "metal_compute_unavailable",
            evidence="probe",
        )
    return DimensionCapability(
        kind=GraphicsBackendKind.OPENGL,
        available=False,
        reason="no_live_gl_context",
        evidence="static",
    )


def _metal_device_available() -> bool:
    """Lazy Metal support probe; any failure counts as unavailable."""
    try:
        from src.shared.gpu.metal import MetalComputeContext
        return MetalComputeContext.is_supported()
    except Exception:
        return False


def detect_compute_backend(
    gl_version: Optional[str] = None,
    *,
    system: Optional[str] = None,
) -> DimensionCapability:
    """Compute dim: GLSL compute needs GL >= 4.3; macOS caps at 4.1.

    The project already falls back compute->fragment on macOS
    (compute_shader_manager); this reports the same fact for the whole
    selection layer.  Metal compute (Phase 4) is deliberately **not**
    reported as a selectable kind here: its runtime gating is
    measurement-driven and per-effect (blur default OFF via
    TOPOS_METAL_COMPUTE, Normalize histogram default ON)——a single
    DimensionCapability cannot express that split; it becomes selectable
    when the Phase 5+ compute backend selector unifies the gating.
    """
    if gl_version:
        def _major_minor(v: str) -> tuple[int, int]:
            parts = ""
            for ch in v:
                if ch.isdigit() or ch == ".":
                    parts += ch
                else:
                    break
            nums = parts.split(".")
            return (int(nums[0]), int(nums[1]) if len(nums) > 1 else 0)
        if _major_minor(gl_version) >= (4, 3):
            return DimensionCapability(
                kind=ComputeBackendKind.GLSL_COMPUTE, available=True, evidence="probe")
        return DimensionCapability(
            kind=ComputeBackendKind.GLSL_FRAGMENT,
            available=True,
            reason=f"gl_compute_unavailable_version={gl_version}",
            evidence="probe",
        )
    return DimensionCapability(
        kind=ComputeBackendKind.GLSL_FRAGMENT,
        available=False,
        reason="no_live_gl_context",
        evidence="static",
    )


def detect_decode_backend(
    *,
    system: Optional[str] = None,
    path: str = "",
    codec: str = "",
    bit_depth: int = 0,
    width: int = 0,
    height: int = 0,
) -> DimensionCapability:
    """Decode dim: delegate to the platform's existing probes.

    The macOS probe decodes a real file, so without ``path`` the only honest
    answer is "unprobed" — device presence alone is never proof (plan §0.1).
    """
    os_name = _os_name(system)
    if os_name == "darwin":
        try:
            from src.shared.media.macos_vt.hardware_capability import (
                vt_hardware_decode_available,
            )
        except Exception:
            return DimensionCapability(
                kind=DecodeBackendKind.VIDEOTOOLBOX,
                available=False,
                reason="videotoolbox_probe_unavailable",
                evidence="static",
            )
        if not path or not codec:
            return DimensionCapability(
                kind=DecodeBackendKind.VIDEOTOOLBOX,
                available=False,
                reason="probe_requires_media_path_and_codec",
                evidence="static",
            )
        verdict = vt_hardware_decode_available(path, codec, bit_depth, width, height)
        if verdict is None:
            return DimensionCapability(
                kind=DecodeBackendKind.VIDEOTOOLBOX,
                available=False,
                reason="vt_probe_inconclusive",
                evidence="probe",
            )
        return DimensionCapability(
            kind=DecodeBackendKind.VIDEOTOOLBOX,
            available=verdict,
            reason="" if verdict else "vt_hardware_decode_probe_negative",
            zero_copy=verdict,
            evidence="probe",
        )
    if os_name == "windows":
        # W0-03：解码可用性与零拷贝解耦——D3D11VA 硬解（download 路径）
        # 只要求 D3D11 硬件适配器存在（AMD/Intel/NVIDIA 皆可，实测本机
        # AMD RX 6800 可用）；WGL interop 失败只影响 zero_copy，不否定
        # 解码本身。codec 级的最终裁决仍在 hw_planar_source.open()
        # 首帧验证（P0-06 语义）。
        try:
            from src.shared.media.windows_hw.gpu_capability import (
                probe_gpu_capability,
            )
            gpu = probe_gpu_capability()
        except Exception:
            return DimensionCapability(
                kind=DecodeBackendKind.D3D11VA,
                available=False,
                reason="windows_decode_probe_unavailable",
                evidence="static",
            )
        hardware = [a for a in gpu.adapters
                    if a.vendor_name in ("amd", "nvidia", "intel")]
        if gpu.probe_ok and hardware:
            return DimensionCapability(
                kind=DecodeBackendKind.D3D11VA,
                available=True,
                reason="",
                zero_copy=False,
                evidence="probe",
            )
        from src.shared.media.windows_hw.reasons import param, FallbackReason
        if not gpu.probe_ok:
            reason = param(FallbackReason.GPU_PROBE_FAILED, gpu.reason)
        elif hardware:
            reason = ""
        else:
            # probe 成功但没有任何硬件 vendor 适配器（VM/远程会话只有
            # Basic Render Driver）。probe_ok=True 时 gpu.reason 是 "ok"，
            # 直接透传会产出 available=False + reason="ok" 的自相矛盾诊断。
            reason = ("no_hardware_adapter" if gpu.adapters
                      else gpu.reason or "no_hardware_adapter")
        return DimensionCapability(
            kind=DecodeBackendKind.D3D11VA,
            available=False,
            reason=reason or "windows_decode_probe_negative",
            evidence="probe",
        )
    return DimensionCapability(
        kind=DecodeBackendKind.CPU,
        available=True,
        reason="cpu_decode_universal_fallback",
        evidence="static",
    )


def detect_platform_video_kinds(
    *,
    system: Optional[str] = None,
) -> dict[str, DimensionCapability]:
    """W0-03: per-kind platform entries for the backend selector.

    键为 ``*BackendKind`` 的 value（与 ``all_dimension_values()`` 词汇一致，
    不发明新词）。非 Windows 上一律 ``available=False, reason="not_windows"``
    —— selector 对显式请求给出显式拒绝理由，而不是 ``*_backend_unprobed``。
    Windows 上按 P8-01 DXGI probe 数据整形：

    - ``d3d11va``/``qsv``/``amf``: 对应 vendor 硬件适配器存在即可用
      （adapter 级证据；codec 级裁决在解码器首帧验证）。
    - ``nvdec``/``cuda``: 仅 NVIDIA + 驱动版本可读（K-W3 驱动 gate 语义）。
    - ``d3d11`` (graphics): 未接线（W5-01 决策：present 保持 OpenGL/WGL）。
    - ``directml``: 未探测（W4-03 前置依赖决策未做）。
    """
    os_name = _os_name(system)

    def _not_windows(kind) -> DimensionCapability:
        return DimensionCapability(
            kind=kind, available=False, reason="not_windows", evidence="static")

    if os_name != "windows":
        return {
            "d3d11": _not_windows(GraphicsBackendKind.D3D11),
            "d3d11va": _not_windows(DecodeBackendKind.D3D11VA),
            "nvdec": _not_windows(DecodeBackendKind.NVDEC),
            "qsv": _not_windows(DecodeBackendKind.QSV),
            "amf": _not_windows(DecodeBackendKind.AMF),
            "cuda": _not_windows(ComputeBackendKind.CUDA),
            "directml": _not_windows(ComputeBackendKind.DIRECTML),
        }

    from src.shared.media.windows_hw.reasons import (
        FallbackReason, param,
    )

    try:
        from src.shared.media.windows_hw.gpu_capability import probe_gpu_capability
        gpu = probe_gpu_capability()
    except Exception as exc:
        failed = DimensionCapability(
            kind=DecodeBackendKind.D3D11VA, available=False,
            reason=param(FallbackReason.GPU_PROBE_FAILED, str(exc)),
            evidence="static")
        return {
            "d3d11": DimensionCapability(
                kind=GraphicsBackendKind.D3D11, available=False,
                reason=FallbackReason.NOT_YET_IMPLEMENTED.value, evidence="static"),
            "d3d11va": failed,
            "nvdec": failed, "qsv": failed, "amf": failed,
            "cuda": failed,
            "directml": DimensionCapability(
                kind=ComputeBackendKind.DIRECTML, available=False,
                reason=FallbackReason.NOT_PROBED.value, evidence="static"),
        }

    vendors = {a.vendor_name for a in gpu.adapters}
    has_hw_adapter = bool(vendors & {"amd", "nvidia", "intel"})
    nvidia_ok = gpu.probe_ok and "nvidia" in vendors and bool(gpu.driver_version)
    nvidia_hw = "nvidia" in vendors
    if not gpu.probe_ok:
        probe_failed_reason = param(FallbackReason.GPU_PROBE_FAILED, gpu.reason)
    elif has_hw_adapter:
        probe_failed_reason = ""
    else:
        # probe 成功但无硬件 vendor 适配器：gpu.reason 此时是 "ok"/
        # "no_adapter"，"ok" 不能充当不可用 reason（available=False +
        # reason="ok" 自相矛盾）；留空让 _vendor_decode 落到显式的
        # vendor_adapter_absent。
        probe_failed_reason = (gpu.reason
                               if gpu.reason not in ("ok", "") else "")

    def _vendor_decode(kind, vendor_ok: bool) -> DimensionCapability:
        if gpu.probe_ok and vendor_ok:
            return DimensionCapability(
                kind=kind, available=True, reason="", zero_copy=False,
                evidence="probe")
        return DimensionCapability(
            kind=kind, available=False,
            reason=probe_failed_reason or "vendor_adapter_absent",
            evidence="probe")

    def _nvidia_kind(kind) -> DimensionCapability:
        if nvidia_ok:
            return DimensionCapability(
                kind=kind, available=True, reason="", evidence="probe")
        # probe 本身失败时连 vendor 都未知——优先报探测失败，不猜
        # cuda_unavailable（与 _vendor_decode 的 probe_failed_reason 语义
        # 一致：探测失败 ≠ 能力缺席）。
        reason = probe_failed_reason or (
            FallbackReason.NVIDIA_DRIVER_VERSION_UNAVAILABLE.value
            if nvidia_hw else FallbackReason.CUDA_UNAVAILABLE.value)
        return DimensionCapability(
            kind=kind, available=False, reason=reason, evidence="probe")

    return {
        "d3d11": DimensionCapability(
            kind=GraphicsBackendKind.D3D11, available=False,
            reason=FallbackReason.NOT_YET_IMPLEMENTED.value, evidence="static"),
        "d3d11va": _vendor_decode(DecodeBackendKind.D3D11VA, has_hw_adapter),
        "nvdec": _nvidia_kind(DecodeBackendKind.NVDEC),
        "qsv": _vendor_decode(DecodeBackendKind.QSV, "intel" in vendors),
        "amf": _vendor_decode(DecodeBackendKind.AMF, "amd" in vendors),
        "cuda": _nvidia_kind(ComputeBackendKind.CUDA),
        "directml": DimensionCapability(
            kind=ComputeBackendKind.DIRECTML, available=False,
            reason=FallbackReason.NOT_PROBED.value, evidence="static"),
    }


def detect_interop_backend(
    *,
    system: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
) -> DimensionCapability:
    """Interop dim: delegate to the P0-06 probe-backed surface gate.

    Maps ``ToposSurfaceBackend`` verdicts onto ``InteropBackendKind``;
    staging/CPU paths map to ``NONE`` + unavailable, and zero-copy claims
    count only when the probe proved them (env flags never do, P0-06).
    """
    from src.shared.media.topos_surface_capability import (
        ToposSurfaceBackend,
        detect_topos_surface_capability,
    )

    cap = detect_topos_surface_capability(system=system, env=env)
    mapped = {
        ToposSurfaceBackend.MACOS_IOSURFACE: InteropBackendKind.IOSURFACE,
        ToposSurfaceBackend.WINDOWS_D3D11_SHARED: InteropBackendKind.D3D_SHARED,
        ToposSurfaceBackend.LINUX_DMABUF: InteropBackendKind.NONE,  # until Phase 9 probe
    }.get(cap.backend, InteropBackendKind.NONE)
    zero_copy_ok = cap.zero_copy_proven  # env flags never count (P0-06)
    if cap.backend in (ToposSurfaceBackend.CPU, ToposSurfaceBackend.OPENGL_PBO,
                       ToposSurfaceBackend.WINDOWS_D3D11_STAGING,
                       ToposSurfaceBackend.LINUX_STAGING):
        return DimensionCapability(
            kind=InteropBackendKind.NONE,
            available=False,
            reason=cap.reason or "no_mapped_surface",
            evidence=cap.evidence,
        )
    return DimensionCapability(
        kind=mapped,
        available=zero_copy_ok,
        reason=cap.reason,
        zero_copy=zero_copy_ok,
        evidence=cap.evidence,
    )


def detect_backend_capabilities(
    selection: Optional[BackendSelection] = None,
    *,
    gl_version: Optional[str] = None,
    gl_renderer: Optional[str] = None,
    system: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    decode_probe_path: str = "",
    decode_codec: str = "",
    decode_bit_depth: int = 0,
    decode_width: int = 0,
    decode_height: int = 0,
) -> BackendCapabilities:
    """Snapshot all four dims (+ platform entries + environment facts).

    ``gl_version``/``gl_renderer`` come from the live render-thread GL
    context (moderngl ``ctx.info``); the decode probe arguments gate the
    macOS VT probe (which needs a real media path + codec, see
    :func:`detect_decode_backend`).  Note: ``selection`` is accepted for
    call-site symmetry but is not currently woven into the returned
    snapshot (pre-existing behavior, preserved).
    """
    graphics = detect_graphics_backend(gl_version, gl_renderer, system=system)
    compute = detect_compute_backend(gl_version, system=system)
    decode = detect_decode_backend(
        system=system, path=decode_probe_path, codec=decode_codec,
        bit_depth=decode_bit_depth, width=decode_width, height=decode_height,
    )
    interop = detect_interop_backend(system=system, env=env)
    return BackendCapabilities(
        graphics=graphics,
        compute=compute,
        decode=decode,
        interop=interop,
        environment={
            "platform": system or platform.system(),
            "gl_version": gl_version or "",
            "gl_renderer": gl_renderer or "",
            "metal_graphics_available": str(
                _metal_device_available() if _os_name(system) == "darwin" else False
            ).lower(),
        },
        platform=detect_platform_video_kinds(system=system),
    )
