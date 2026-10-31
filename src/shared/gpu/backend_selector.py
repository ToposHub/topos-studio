"""P2-06: backend selection + diagnostics service (plan §4.1–§4.3).

Resolution rules:
- ``AUTO`` walks the per-dimension preference chain and picks the first
  *probe-available* backend; every skipped step is recorded (fallback trail,
  plan §4.3 required fields).
- An explicit request (auto != kind) is honored only when the capability
  probe says available; otherwise the plan stays **unavailable with the
  probe reason** — it never silently downgrades (.clinerules:11; §4.3
  fallback logs).  The ``force_`` prefix marks the same behavior for
  regression runs.

Thread boundary: this module is a pure, stateless function of
``(selection, capabilities)`` — no GL calls, no shared mutable state —
so it may be called from any thread (UI or render).  The live GL facts
it consumes (``gl_version``/``gl_renderer`` inside ``capabilities``)
must have been captured on the GL/render thread by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.shared.gpu.backend_types import (
    BackendSelection,
    ComputeBackendKind,
    DecodeBackendKind,
    GraphicsBackendKind,
    InteropBackendKind,
)
from src.shared.gpu.capabilities import BackendCapabilities, DimensionCapability

# §4.3 fallback chains (first entry = highest preference).  Windows-native
# kinds join the chains in Phase 8/9; today they simply never probe available.
_GRAPHICS_PREFERENCE = [
    GraphicsBackendKind.OPENGL,
    GraphicsBackendKind.METAL,
]
_COMPUTE_PREFERENCE = [
    ComputeBackendKind.METAL_COMPUTE,
    ComputeBackendKind.GLSL_COMPUTE,
    ComputeBackendKind.GLSL_FRAGMENT,
    ComputeBackendKind.CPU,
]
_DECODE_PREFERENCE = [
    DecodeBackendKind.VIDEOTOOLBOX,
    DecodeBackendKind.NVDEC,
    DecodeBackendKind.D3D11VA,
    DecodeBackendKind.QSV,
    DecodeBackendKind.AMF,
    DecodeBackendKind.CPU,
]
_INTEROP_PREFERENCE = [
    InteropBackendKind.CVMETAL,
    InteropBackendKind.IOSURFACE,
    InteropBackendKind.D3D_SHARED,
    InteropBackendKind.CUDA_INTEROP,
    InteropBackendKind.NONE,
]

_PREFERENCES = {
    "graphics": _GRAPHICS_PREFERENCE,
    "compute": _COMPUTE_PREFERENCE,
    "decode": _DECODE_PREFERENCE,
    "interop": _INTEROP_PREFERENCE,
}


@dataclass(frozen=True)
class DimensionResolution:
    """One dimension: requested vs selected vs capability verdict.

    Attributes:
        dimension: Dimension name (``"graphics"``/``"compute"``/``"decode"``
            /``"interop"``).
        requested: What the user asked for (kind value, or ``"auto"``).
        selected: What the plan actually binds — equals ``requested`` for
            explicit requests (even when unavailable), the first available
            chain entry for ``auto``.
        available: Probe verdict for ``selected``.
        forced: True when the request carried the ``force_`` prefix.
        fallback_reason: Empty when the first choice won; otherwise the
            explicit probe reason (never a silent switch).
        fallback_trail: Ordered kind values walked by ``auto`` before the
            verdict, e.g. ``("metal", "opengl")``.
    """

    dimension: str
    requested: str
    selected: str
    available: bool
    forced: bool
    fallback_reason: str = ""
    fallback_trail: tuple[str, ...] = ()  # e.g. ("metal", "opengl")

    def as_dict(self) -> dict[str, object]:
        """JSON-friendly copy (trail becomes a list)."""
        return {
            "dimension": self.dimension,
            "requested": self.requested,
            "selected": self.selected,
            "available": self.available,
            "forced": self.forced,
            "fallback_reason": self.fallback_reason,
            "fallback_trail": list(self.fallback_trail),
        }


@dataclass(frozen=True)
class ResolvedBackendPlan:
    """All four dims + required fallback log fields (plan §4.3)."""

    graphics: DimensionResolution
    compute: DimensionResolution
    decode: DimensionResolution
    interop: DimensionResolution

    def as_dict(self) -> dict[str, object]:
        """Serialize to the §4.3 fallback-log field layout."""
        return {
            "requested_backend": {
                "graphics": self.graphics.requested,
                "compute": self.compute.requested,
                "decode": self.decode.requested,
                "interop": self.interop.requested,
            },
            "selected_backend": {
                "graphics": self.graphics.selected,
                "compute": self.compute.selected,
                "decode": self.decode.selected,
                "interop": self.interop.selected,
            },
            "fallback_reason": {
                k: v.fallback_reason
                for k, v in (("graphics", self.graphics), ("compute", self.compute),
                             ("decode", self.decode), ("interop", self.interop))
                if v.fallback_reason
            },
            "dimensions": [d.as_dict() for d in
                           (self.graphics, self.compute, self.decode, self.interop)],
        }

    @property
    def fully_available(self) -> bool:
        """True only when all four dimensions resolved to an available kind."""
        return all(d.available for d in
                   (self.graphics, self.compute, self.decode, self.interop))

    def diagnostics(self) -> list[str]:
        """Human-readable lines for unavailable/switched dimensions."""
        lines: list[str] = []
        for dim in (self.graphics, self.compute, self.decode, self.interop):
            if not dim.available:
                lines.append(
                    f"{dim.dimension}: requested={dim.requested} unavailable — "
                    f"{dim.fallback_reason or 'probe negative'}")
            elif dim.selected != dim.requested:
                lines.append(
                    f"{dim.dimension}: {dim.requested} -> {dim.selected} "
                    f"({dim.fallback_reason})")
        return lines


def _capability_for(
    capabilities: BackendCapabilities,
    kind: "GraphicsBackendKind | ComputeBackendKind | DecodeBackendKind | InteropBackendKind",
) -> Optional[DimensionCapability]:
    """Look up the capability verdict for one backend kind.

    Order: the four live dimensions first; then the Metal-graphics
    environment flag (see inline comment); finally the per-kind platform
    entries.  Returns ``None`` when nothing knows about ``kind`` — the
    caller then reports ``<dimension>_backend_unprobed``.
    """
    for dim_name in ("graphics", "compute", "decode", "interop"):
        cap: DimensionCapability = getattr(capabilities, dim_name)
        if cap.kind is kind:
            return cap
    if kind is GraphicsBackendKind.METAL:
        # The live graphics dimension reports the currently-bound GL driver
        # when one exists. Keep the separate Metal probe available so an
        # explicit Metal request can still resolve on a running GL session.
        probed = capabilities.environment.get("metal_graphics_available")
        if probed is not None:
            available = str(probed).strip().lower() == "true"
            return DimensionCapability(
                kind=kind,
                available=available,
                reason="" if available else "metal_compute_unavailable",
                evidence="probe",
            )
    # W0-03: 平台专属条目（d3d11/d3d11va/nvdec/cuda/directml...）——
    # 非 Windows 上的显式请求得到 not_windows 类显式 reason，
    # 而不是泛化的 "*_backend_unprobed"。
    return capabilities.platform.get(kind.value)


def _resolve_dimension(
    dimension: str,
    requested: "GraphicsBackendKind | ComputeBackendKind | DecodeBackendKind | InteropBackendKind",
    forced: bool,
    capabilities: BackendCapabilities,
) -> DimensionResolution:
    """Resolve one dimension: honor explicit requests, walk the chain for auto.

    Explicit request (``requested`` is not AUTO): available when the probe
    says so, otherwise the resolution stays on the requested kind with
    ``available=False`` and the probe reason — never a silent downgrade.

    AUTO: walk ``_PREFERENCES[dimension]``; the first probe-available kind
    wins and earlier failures are preserved as ``fallback_reason`` /
    ``fallback_trail``.  Chain exhausted => last candidate, unavailable.
    """
    cap = _capability_for(capabilities, requested)

    if not _is_auto(requested):
        # Explicit request: honor or diagnose — never silently switch.
        if cap is not None and cap.available:
            return DimensionResolution(
                dimension=dimension, requested=requested.value,
                selected=requested.value, available=True, forced=forced)
        reason = (cap.reason if cap is not None
                  else f"{dimension}_backend_unprobed")
        return DimensionResolution(
            dimension=dimension, requested=requested.value,
            selected=requested.value, available=False, forced=forced,
            fallback_reason=reason)

    # AUTO: walk the preference chain.
    trail: list[str] = []
    fallback_reason = ""
    for candidate in _PREFERENCES[dimension]:
        cap = _capability_for(capabilities, candidate)
        trail.append(candidate.value)
        if cap is not None and cap.available:
            if len(trail) == 1:
                return DimensionResolution(
                    dimension=dimension, requested="auto",
                    selected=candidate.value, available=True, forced=False)
            return DimensionResolution(
                dimension=dimension, requested="auto",
                selected=candidate.value, available=True, forced=False,
                fallback_reason=fallback_reason or f"{trail[0]}_unavailable",
                fallback_trail=tuple(trail))
        if cap is not None and cap.reason and not fallback_reason:
            fallback_reason = cap.reason
    # Chain exhausted: every candidate unavailable.
    last = _PREFERENCES[dimension][-1]
    return DimensionResolution(
        dimension=dimension, requested="auto",
        selected=last.value, available=False, forced=False,
        fallback_reason=fallback_reason or "all_candidates_unavailable",
        fallback_trail=tuple(trail))


def _is_auto(
    kind: "GraphicsBackendKind | ComputeBackendKind | DecodeBackendKind | InteropBackendKind",
) -> bool:
    """True when the kind is the dimension's neutral ``auto`` value."""
    return kind.value == "auto"


def resolve_backend_plan(
    selection: BackendSelection,
    capabilities: BackendCapabilities,
) -> ResolvedBackendPlan:
    """Resolve the four-dim plan; pure function of (selection, capabilities)."""
    return ResolvedBackendPlan(
        graphics=_resolve_dimension("graphics", selection.graphics,
                                    selection.graphics_forced, capabilities),
        compute=_resolve_dimension("compute", selection.compute,
                                   selection.compute_forced, capabilities),
        decode=_resolve_dimension("decode", selection.decode,
                                  selection.decode_forced, capabilities),
        interop=_resolve_dimension("interop", selection.interop,
                                   selection.interop_forced, capabilities),
    )
