"""Capability gate for Topos mapped decode surfaces.

This module deliberately separates *resource availability* from the codec's
write contract.  A D3D11 staging texture or a dmabuf that cannot be mapped by
the current process is a valid fallback, but it is not zero-copy.

A ``zero_copy=True`` verdict is only ever backed by an in-process probe that
owns a mapped resource (``evidence="probe"``).  Environment flags cannot
assert zero-copy on their own; when set they are recorded as
``evidence="env_override"`` diagnostics and the staging fallback is kept
until the platform probe confirms the mapping (P0-06).
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional


class ToposSurfaceBackend(str, Enum):
    CPU = "cpu_planar_upload"
    MACOS_IOSURFACE = "macos_iosurface_mapped"
    OPENGL_PBO = "opengl_pbo_staging"
    WINDOWS_D3D11_SHARED = "windows_d3d11_shared"
    WINDOWS_D3D11_STAGING = "windows_d3d11_staging"
    LINUX_DMABUF = "linux_dmabuf"
    LINUX_STAGING = "linux_staging"


@dataclass(frozen=True)
class ToposSurfaceCapability:
    """One backend decision exposed to diagnostics and playback policy."""

    backend: ToposSurfaceBackend
    available: bool
    zero_copy: bool
    reason: str
    requires_render_thread: bool
    supports_planar_u16: bool
    copy_bytes: bool
    evidence: str = "static"

    @property
    def zero_copy_proven(self) -> bool:
        """True only when zero-copy is claimed from a real in-process probe."""
        return self.zero_copy and self.evidence == "probe"

    def as_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend.value,
            "available": self.available,
            "zero_copy": self.zero_copy,
            "reason": self.reason,
            "requires_render_thread": self.requires_render_thread,
            "supports_planar_u16": self.supports_planar_u16,
            "copy_bytes": self.copy_bytes,
            "evidence": self.evidence,
        }


def _env_truthy(env: Mapping[str, str], key: str) -> bool:
    return env.get(key, "").strip().lower() in {"1", "true", "yes", "on"}


def _cpu_capability(reason: str) -> ToposSurfaceCapability:
    return ToposSurfaceCapability(
        backend=ToposSurfaceBackend.CPU,
        available=True,
        zero_copy=False,
        reason=reason,
        requires_render_thread=False,
        supports_planar_u16=True,
        copy_bytes=True,
    )


def _probe_windows_d3d11_shared() -> tuple[bool, str]:
    """Ask the Windows interop layer whether a CPU-writable shared mapping exists.

    The probe is trusted only when it names a concrete interop method; a bare
    ``available`` flag alone is not proof.  Until the D3D11 interop probe is
    implemented (Phase 8), the honest answer is False.
    """
    try:
        from src.shared.media.windows_hw.capability import probe_zero_copy_capability
    except Exception:
        return False, "windows_capability_probe_unavailable"
    try:
        cap = probe_zero_copy_capability()
    except Exception as exc:
        return False, f"windows_capability_probe_error: {exc}"
    if cap.available and cap.interop_method:
        return True, ""
    return False, cap.reason or "windows_capability_probe_negative"


def _probe_linux_dmabuf() -> tuple[bool, str]:
    """Probe the Linux dmabuf CPU-mapping contract (Phase 9 seam).

    No in-repo dmabuf interop exists yet, so the only truthful answer is
    False until the EGL/GBM import check lands.
    """
    return False, "dmabuf_interop_probe_unimplemented"


def _probe_macos_iosurface() -> tuple[bool, str]:
    """Back the macOS zero-copy claim with a real in-process mapping.

    AUD-35910：平台判断 + PyObjC 导入成功不构成零拷贝证据——模块自身
    契约要求 ``zero_copy=True`` 必须由持有已映射资源的进程内探测背书。
    此处分配一个最小 IOSurface ring，走 acquire→map→unmap→fence→release
    全链路；任一步失败即回退 CPU 上传（fail-closed）。探测只触碰
    IOSurface 的 CPU 映射，不涉及 OpenGL/Metal，可在能力检测线程执行。
    """
    try:
        from src.shared.media.macos_vt.topos_surface import (
            ToposIOSurfacePool,
            ToposSurfaceUnavailable,
        )
    except (ImportError, OSError) as exc:
        return False, f"iosurface_module_unavailable:{exc}"
    try:
        pool = ToposIOSurfacePool(64, 64, pixel_format=0, plane_count=3, ring_size=1)
    except ToposSurfaceUnavailable as exc:
        return False, f"iosurface_alloc_failed:{exc}"
    except (OSError, TypeError, ValueError) as exc:
        return False, f"iosurface_alloc_error:{exc}"
    try:
        lease = pool.acquire()
        if lease is None:
            return False, "iosurface_acquire_failed"
        try:
            lease.acquire()
            lease.map()
            lease.unmap()
            lease.fence()
        finally:
            lease.release()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        # DecodeSurfaceProtocolError / ToposSurfaceUnavailable 均为
        # RuntimeError 子类——映射链路任何失败都降级为回退，不让探测崩溃
        return False, f"iosurface_map_probe_error:{exc}"
    finally:
        try:
            pool.close()
        except RuntimeError:
            pass  # 探测槽位未按协议归还——已按失败/成功路径上报
    return True, ""


def detect_topos_surface_capability(
    requested: Optional[str] = None,
    *,
    system: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> ToposSurfaceCapability:
    """Choose a truthful surface backend for the current platform.

    ``TOPOS_SURFACE_BACKEND=cpu`` is an explicit diagnostic override.
    ``TOPOS_D3D11_SHARED_MAPPED`` / ``TOPOS_DMABUF_MAPPED`` no longer assert
    zero-copy by themselves; the mapped backend is selected only when the
    platform probe confirms the mapping, and the env flag is otherwise kept
    in the diagnostics reason.
    """
    values = os.environ if env is None else env
    name = (requested or values.get("TOPOS_SURFACE_BACKEND", "auto")).strip().lower()
    os_name = (system or platform.system()).strip().lower()
    if name in {"cpu", "cpu_upload", ToposSurfaceBackend.CPU.value}:
        return _cpu_capability("forced_cpu_upload")
    if name in {"pbo", "opengl_pbo", ToposSurfaceBackend.OPENGL_PBO.value}:
        return ToposSurfaceCapability(
            backend=ToposSurfaceBackend.OPENGL_PBO,
            available=True,
            zero_copy=False,
            reason="pbo_staging_copy",
            requires_render_thread=True,
            supports_planar_u16=True,
            copy_bytes=True,
        )

    if os_name == "darwin":
        # AUD-35910：导入成功 != 零拷贝可用——探测必须真实分配并 CPU 映射
        # 一个 IOSurface（见 _probe_macos_iosurface），失败回退 CPU 上传。
        mapped, probe_reason = _probe_macos_iosurface()
        if mapped:
            return ToposSurfaceCapability(
                backend=ToposSurfaceBackend.MACOS_IOSURFACE,
                available=True,
                zero_copy=True,
                reason="",
                requires_render_thread=True,
                supports_planar_u16=True,
                copy_bytes=False,
                evidence="probe",
            )
        return _cpu_capability(f"iosurface_mapping_unavailable:{probe_reason}")

    if os_name == "windows":
        env_flag = _env_truthy(values, "TOPOS_D3D11_SHARED_MAPPED")
        mapped, probe_reason = _probe_windows_d3d11_shared()
        if mapped:
            return ToposSurfaceCapability(
                backend=ToposSurfaceBackend.WINDOWS_D3D11_SHARED,
                available=True,
                zero_copy=True,
                reason="",
                requires_render_thread=True,
                supports_planar_u16=True,
                copy_bytes=False,
                evidence="probe",
            )
        if env_flag:
            return ToposSurfaceCapability(
                backend=ToposSurfaceBackend.WINDOWS_D3D11_STAGING,
                available=True,
                zero_copy=False,
                reason=f"d3d11_shared_env_flag_unproven:{probe_reason}",
                requires_render_thread=True,
                supports_planar_u16=True,
                copy_bytes=True,
            )
        return ToposSurfaceCapability(
            backend=ToposSurfaceBackend.WINDOWS_D3D11_STAGING,
            available=True,
            zero_copy=False,
            reason=f"d3d11_shared_mapping_unavailable:{probe_reason}",
            requires_render_thread=True,
            supports_planar_u16=True,
            copy_bytes=True,
        )

    if os_name == "linux":
        env_flag = _env_truthy(values, "TOPOS_DMABUF_MAPPED")
        mapped, probe_reason = _probe_linux_dmabuf()
        if mapped:
            return ToposSurfaceCapability(
                backend=ToposSurfaceBackend.LINUX_DMABUF,
                available=True,
                zero_copy=True,
                reason="",
                requires_render_thread=True,
                supports_planar_u16=True,
                copy_bytes=False,
                evidence="probe",
            )
        if env_flag:
            return ToposSurfaceCapability(
                backend=ToposSurfaceBackend.LINUX_STAGING,
                available=True,
                zero_copy=False,
                reason=f"dmabuf_env_flag_unproven:{probe_reason}",
                requires_render_thread=True,
                supports_planar_u16=True,
                copy_bytes=True,
            )
        return ToposSurfaceCapability(
            backend=ToposSurfaceBackend.LINUX_STAGING,
            available=True,
            zero_copy=False,
            reason=f"dmabuf_mapping_unavailable:{probe_reason}",
            requires_render_thread=True,
            supports_planar_u16=True,
            copy_bytes=True,
        )

    return _cpu_capability("platform_surface_adapter_unavailable")
