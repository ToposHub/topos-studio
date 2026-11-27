"""GPU Scope data models.

This module defines the typed, testable data structures used by the
GPU scope pipeline.  It deliberately has no dependencies on PyQt,
ModernGL, NumPy or OpenCV so it can be imported and unit-tested anywhere.

Design notes (see docs/gpu_scope_implementation_plan_2026-08-05.md):

* ``ScopeDemand`` is produced by the UI/controller side.  It describes
  *what* the user wants to see right now (mode, size, zoom, play state)
  together with a monotonically increasing ``revision`` and the
  ``source_revision`` of the front texture the demand is based on.

* ``ScopeGpuFrame`` is produced by ``AsyncRenderer`` after a GPU scope
  pass.  It only carries *descriptors* of GPU resident textures
  (texture ids + sizes) plus the matching revisions and a small amount
  of metadata.  It NEVER carries pixel arrays.

The UI is only allowed to display a ``ScopeGpuFrame`` whose ``revision``
matches its own current demand AND whose ``source_revision`` is not
older than the demand's ``source_revision``.  Any other frame is
silently discarded.  This is the generation guard that prevents
stale "scope flashbacks" after seek / clip switch.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Optional

if TYPE_CHECKING:  # pragma: no cover — 仅类型，无运行期依赖
    from src.shared.gpu.scope_publication import ScopePublication
    from src.features.color.core.scope_graph_sampling import ScopeSampleIdentity


ScopeMode = Literal[
    'waveform', 'vectorscope', 'split', 'parade', 'luma',
    'histogram', 'cie', 'falsecolor', 'quad', 'all',
]

SCOPE_RANGE_MIN = -0.1
SCOPE_RANGE_MAX = 1.2


def waveform_plot_range(show_excursions: bool) -> tuple[float, float]:
    return (SCOPE_RANGE_MIN, SCOPE_RANGE_MAX) if show_excursions else (0.0, 1.0)


# Default analysis sizes (pre-sampled front texture dimensions).
# 320x180 = 57,600 samples: dynamic multi-view budget and the initial idle
# tier. Single-view playback/adjustment uses the denser tier below.
DEFAULT_ANALYSIS_SIZE: tuple[int, int] = (320, 180)
# Single-view playback/adjustment keeps a denser trace.
PLAYBACK_ANALYSIS_SIZE: tuple[int, int] = (512, 288)
# Paused/idle scopes can spend more GPU time for a crisp still frame.
HIGH_QUALITY_ANALYSIS_SIZE: tuple[int, int] = (768, 432)

# Default density texture sizes.
DEFAULT_WAVEFORM_DENSITY_SIZE: tuple[int, int] = (1024, 300)
DEFAULT_VECTORSCOPE_DENSITY_SIZE: tuple[int, int] = (800, 800)
# Histogram: point-splat density + per-column reduction.
DEFAULT_HISTOGRAM_DENSITY_SIZE: tuple[int, int] = (512, 256)
DEFAULT_HISTOGRAM_DISPLAY_SIZE: tuple[int, int] = (512, 300)
DEFAULT_CURVES_HISTOGRAM_SIZE: tuple[int, int] = (1024, 512)
# CIE 1931 xy chromaticity plot (matches the CPU widget bin grid).
DEFAULT_CIE_SIZE: tuple[int, int] = (384, 432)
# False Color renders the frame (aspect-fit) into a fixed 16:9 canvas.
DEFAULT_FALSECOLOR_SIZE: tuple[int, int] = (512, 288)


def scope_density_sizes(display_size: tuple[int, int], mode: str) -> tuple[tuple[int, int], tuple[int, int]]:
    """Resolve bounded, quantized density sizes from physical plot pixels.

    Keep the original minimums for compact docks. Large/Retina windows need
    more vertical bins to avoid magnifying a 300-row waveform into soft bands.
    Quantization avoids reallocating the resource pool at every resize pixel.
    """
    width, height = (max(1, int(value)) for value in display_size)

    def extent(value: int, minimum: int, maximum: int) -> int:
        if value <= minimum:
            return minimum
        return max(minimum, min(maximum, ((value + 63) // 64) * 64))

    waveform = DEFAULT_WAVEFORM_DENSITY_SIZE
    vectorscope = DEFAULT_VECTORSCOPE_DENSITY_SIZE
    if mode in ('waveform', 'parade', 'luma', 'split', 'quad', 'all'):
        waveform = (extent(width, 1024, 2048), extent(height, 300, 1200))
    if mode in ('vectorscope', 'split', 'quad', 'all'):
        side = extent(min(width, height), 800, 1600)
        vectorscope = (side, side)
    return waveform, vectorscope

# Scope input transfer characteristics (drives CIE linearisation and the
# False Color HDR stops).  Mirrors ScopePanel._scope_transfer values.
SCOPE_TRANSFERS = ('srgb', 'bt709', 'pq', 'hlg', 'scene')
# Scope gamuts (CIE XYZ matrix selection).
SCOPE_PRIMARIES = ('bt709', 'p3', 'bt2020')
# False Color display modes (mirrors FalseColorWidget modes).
SCOPE_FALSECOLOR_MODES = ('sdr', 'hdr', 'zebra')

# Maximum scope refresh rate (Hz) per state.
MAX_HZ_PLAYBACK: float = 10.0
MAX_HZ_INTERACTING: float = 10.0
MAX_HZ_IDLE: float = 20.0


@dataclass(frozen=True)
class ScopeDemand:
    """An immutable request for a GPU scope computation.

    Attributes:
        revision: Monotonically increasing id set by the UI whenever the
            desired output changes (mode, size, zoom, visibility).  The
            renderer and the UI use it to discard outdated work.
        source_revision: The front-texture revision this demand was created
            against. It is a lower-bound freshness marker: the low-priority
            renderer may analyse a newer front texture before the demand
            drains, but must never present an older result after a seek or
            clip switch.
        mode: Which scope outputs are required. ``'split'`` requests waveform
            and vectorscope, ``'quad'`` adds parade and histogram, and ``'all'``
            requests every scope. All density passes share one source sample.
        display_size: The exposed plot size (w, h) in physical pixels.
            Sets bounded density resolution; source sample count is independent.
        analysis_size: The pre-sampled front texture size used for
            density accumulation (e.g. 320x180 or 512x288).
        vectorscope_zoom: 1.0 or 2.0; only affects the display pass.
        is_playing: True while video playback is active.  Used by the
            scheduler to pick the refresh budget; never used to read
            pixels back to the CPU.
        mask_layer_id: Optional renderer layer id whose GPU mask texture
            should weight the analysis.  The texture is resolved inside the
            renderer context; no mask pixels cross into the UI.
        mask_enabled: Whether the mask texture should be applied.
        histogram_overlay: Also emit the GPU histogram texture for the curves
            canvas, regardless of the active scope mode.
    """
    revision: int
    source_revision: int
    mode: ScopeMode
    display_size: tuple[int, int]
    analysis_size: tuple[int, int] = DEFAULT_ANALYSIS_SIZE
    vectorscope_zoom: float = 1.0
    is_playing: bool = False
    mask_layer_id: Optional[str] = None
    mask_enabled: bool = False
    histogram_overlay: bool = False
    scope_transfer: str = 'srgb'
    scope_primaries: str = 'bt709'
    falsecolor_mode: str = 'sdr'
    zebra_threshold: float = 1.0
    curves_display_size: tuple[int, int] = DEFAULT_CURVES_HISTOGRAM_SIZE
    curves_line_width: float = 1.0
    curves_mode: str = 'Custom'
    # S05：完整取样身份（session/owner/graph revision/frame/颜色上下文/
    # 目标）。缺省 None 保持旧 revision 契约（兼容既有测试与 CPU 路径）。
    sample_identity: Optional['ScopeSampleIdentity'] = None
    # Curve-only demands share histogram counts without drawing a hidden
    # standalone histogram. Default preserves existing independent callers.
    standalone_enabled: bool = True
    show_waveform_excursions: bool = True


@dataclass(frozen=True)
class ScopeGpuFrame:
    """A descriptor for a completed GPU scope pass.

    The texture ids refer to textures owned by ``AsyncRenderer`` and
    shared with the presenter's context. ``publication`` revokes them before
    reallocation. Under the GL mutex the presenter waits on its GPU fence and
    copies to a local GPU snapshot, which survives resize and frozen curves.

    Attributes:
        revision: Matches the ``ScopeDemand.revision`` that produced it.
        source_revision: The front-texture revision actually analysed.
        waveform_texture_id / waveform_size: GPU density/colour-mapped
            waveform texture (RGBA).  ``None`` when the demand did not
            request waveform output.
        vectorscope_texture_id / vectorscope_size: GPU vectorscope
            texture (RGBA).  ``None`` when not requested.
        analysis_ms: CPU wall time to submit the analysis/display commands;
            GPU completion is asynchronous (not a GPU execution-time metric).
    """
    revision: int
    source_revision: int
    waveform_texture_id: Optional[int] = None
    waveform_size: tuple[int, int] = (0, 0)
    vectorscope_texture_id: Optional[int] = None
    vectorscope_size: tuple[int, int] = (0, 0)
    parade_texture_id: Optional[int] = None
    parade_size: tuple[int, int] = (0, 0)
    luma_texture_id: Optional[int] = None
    luma_size: tuple[int, int] = (0, 0)
    histogram_texture_id: Optional[int] = None
    histogram_size: tuple[int, int] = (0, 0)
    cie_texture_id: Optional[int] = None
    cie_size: tuple[int, int] = (0, 0)
    falsecolor_texture_id: Optional[int] = None
    falsecolor_size: tuple[int, int] = (0, 0)
    analysis_ms: float = 0.0
    curves_histogram_texture_id: Optional[int] = None
    curves_histogram_size: tuple[int, int] = (0, 0)
    curves_mode: str = 'Custom'
    # S05：执行侧回填的取样身份（source_content_revision 为实际分析的
    # front revision）。面板经 ``ScopeSampleIdentity.allows`` 拒绝迟到/
    # 跨 clip/跨图版本结果。
    sample_identity: Optional['ScopeSampleIdentity'] = None
    publication: Optional['ScopePublication'] = None


def is_frame_valid_for_demand(
    frame: Optional[ScopeGpuFrame], demand: Optional[ScopeDemand]
) -> bool:
    """Return whether ``frame`` may be displayed for ``demand``.

    A frame is valid only if:
    * both objects exist;
    * their UI-side ``revision`` matches exactly;
    * the frame was analysed against a source revision at least as new
      as the demand's source revision (older sources must be discarded
      to avoid stale "scope flashbacks" after seek / clip switch).
    """
    if frame is None or demand is None:
        return False
    if frame.revision != demand.revision:
        return False
    if frame.source_revision < demand.source_revision:
        return False
    # S05：双端都有身份时叠加完整身份校验（session/owner/graph revision/
    # 颜色上下文/目标精确相等；content revision/frame 为下界）。单端缺失
    # 时保持旧 revision 契约（渐进采用）。
    demand_identity = getattr(demand, 'sample_identity', None)
    frame_identity = getattr(frame, 'sample_identity', None)
    if demand_identity is not None and frame_identity is not None:
        return demand_identity.allows(frame_identity)
    return True


def mode_needs_waveform(mode: ScopeMode) -> bool:
    """Whether ``mode`` requires a waveform pass."""
    return mode in ('waveform', 'split', 'quad', 'all')


def mode_needs_vectorscope(mode: ScopeMode) -> bool:
    """Whether ``mode`` requires a vectorscope pass."""
    return mode in ('vectorscope', 'split', 'quad', 'all')


def mode_needs_parade(mode: ScopeMode) -> bool:
    """Whether ``mode`` requires the RGB parade passes."""
    return mode in ('parade', 'quad', 'all')


def mode_needs_luma(mode: ScopeMode) -> bool:
    """Whether ``mode`` requires the luma waveform passes."""
    return mode in ('luma', 'all')


def mode_needs_histogram(mode: ScopeMode) -> bool:
    """Whether ``mode`` requires the histogram passes."""
    return mode in ('histogram', 'quad', 'all')


def mode_needs_cie(mode: ScopeMode) -> bool:
    """Whether ``mode`` requires the CIE chromaticity passes."""
    return mode in ('cie', 'all')


def mode_needs_falsecolor(mode: ScopeMode) -> bool:
    """Whether ``mode`` requires the false color display pass."""
    return mode in ('falsecolor', 'all')
