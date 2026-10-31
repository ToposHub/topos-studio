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
from typing import Literal, Optional


ScopeMode = Literal[
    'waveform', 'vectorscope', 'split', 'parade', 'luma',
    'histogram', 'cie', 'falsecolor',
]

# Default analysis sizes (pre-sampled front texture dimensions).
# 320x180 = 57,600 samples: used while the user is actively dragging a
# control, where latency matters more than final trace density.
DEFAULT_ANALYSIS_SIZE: tuple[int, int] = (320, 180)
# Playback gets a denser trace while remaining comfortably below the CPU
# path's cost.  The benchmark on the project Mac measured ~6.5 ms/pass.
PLAYBACK_ANALYSIS_SIZE: tuple[int, int] = (512, 288)
# Paused/idle scopes can spend more GPU time for a crisp still frame.
HIGH_QUALITY_ANALYSIS_SIZE: tuple[int, int] = (768, 432)

# Default density texture sizes.
DEFAULT_WAVEFORM_DENSITY_SIZE: tuple[int, int] = (1024, 300)
DEFAULT_VECTORSCOPE_DENSITY_SIZE: tuple[int, int] = (800, 800)
# Histogram: point-splat density + per-column reduction.
DEFAULT_HISTOGRAM_DENSITY_SIZE: tuple[int, int] = (512, 256)
DEFAULT_HISTOGRAM_DISPLAY_SIZE: tuple[int, int] = (512, 300)
# CIE 1931 xy chromaticity plot (matches the CPU widget bin grid).
DEFAULT_CIE_SIZE: tuple[int, int] = (384, 432)
# False Color renders the frame (aspect-fit) into a fixed 16:9 canvas.
DEFAULT_FALSECOLOR_SIZE: tuple[int, int] = (512, 288)

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
        mode: Which scope output(s) are required.  ``'split'`` requests
            both waveform and vectorscope in a single pass.
        display_size: The widget size (w, h) in pixels.  Used by the
            presenter; does not influence analysis cost.
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
    scope_transfer: str = 'srgb'
    scope_primaries: str = 'bt709'
    falsecolor_mode: str = 'sdr'
    zebra_threshold: float = 1.0


@dataclass(frozen=True)
class ScopeGpuFrame:
    """A descriptor for a completed GPU scope pass.

    The texture ids refer to textures owned by ``AsyncRenderer`` and
    shared with the presenter's context.  They are only valid as long as
    the renderer has not reallocated them; the presenter must consume
    them inside the GL mutex window (tryLock).

    Attributes:
        revision: Matches the ``ScopeDemand.revision`` that produced it.
        source_revision: The front-texture revision actually analysed.
        waveform_texture_id / waveform_size: GPU density/colour-mapped
            waveform texture (RGBA).  ``None`` when the demand did not
            request waveform output.
        vectorscope_texture_id / vectorscope_size: GPU vectorscope
            texture (RGBA).  ``None`` when not requested.
        analysis_ms: Wall time of the analysis+display passes, for
            performance logging only.
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
    return True


def mode_needs_waveform(mode: ScopeMode) -> bool:
    """Whether ``mode`` requires a waveform pass."""
    return mode in ('waveform', 'split')


def mode_needs_vectorscope(mode: ScopeMode) -> bool:
    """Whether ``mode`` requires a vectorscope pass."""
    return mode in ('vectorscope', 'split')


def mode_needs_parade(mode: ScopeMode) -> bool:
    """Whether ``mode`` requires the RGB parade passes."""
    return mode == 'parade'


def mode_needs_luma(mode: ScopeMode) -> bool:
    """Whether ``mode`` requires the luma waveform passes."""
    return mode == 'luma'


def mode_needs_histogram(mode: ScopeMode) -> bool:
    """Whether ``mode`` requires the histogram passes."""
    return mode == 'histogram'


def mode_needs_cie(mode: ScopeMode) -> bool:
    """Whether ``mode`` requires the CIE chromaticity passes."""
    return mode == 'cie'


def mode_needs_falsecolor(mode: ScopeMode) -> bool:
    """Whether ``mode`` requires the false color display pass."""
    return mode == 'falsecolor'
