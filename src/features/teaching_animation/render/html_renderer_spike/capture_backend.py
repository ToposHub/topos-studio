"""W0-A spike: pluggable offscreen RGBA capture backends.

This module is the heart of the W0-A browser feasibility gate.  It defines a
backend-agnostic contract for "render this HTML, at this frame, to RGBA" so
that the chosen renderer (Qt WebEngine today, a pinned headless browser
tomorrow) is an interchangeable, versioned, fail-closed component rather than
an assumption.

The contract deliberately separates *availability* (can this backend produce
pixels at all in this environment?) from *success* (did this specific capture
return real content?).  A blank/uniform frame is reported as a structured
``CaptureFailure`` — never silently returned as a valid frame — because a blank
frame is the exact failure mode that W0-A exists to detect.

Per the plan this module MUST NOT touch ``core/model.py``, the timeline or the
UI.  It is a standalone spike that feeds an ADR.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Mapping, Protocol

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from .security import UrlPolicy


def rgba_sha256(data: bytes) -> str:
    """Stable content hash used for same-build determinism checks."""
    return hashlib.sha256(data).hexdigest()


def is_uniform_frame(data: bytes, width: int, height: int, *, max_samples: int = 4096) -> tuple[bool, tuple[int, int, int, int] | None]:
    """Detect uniform (blank) RGBA8 frames by evenly-distributed sampling.

    A truly uniform frame is the offline signature of a capture backend that
    did not composite the page (e.g. ``QWebEngineView.grab()`` on the offscreen
    platform returns all-white).  Sampling every pixel of a 4K frame is
    needlessly expensive; instead we visit up to ``max_samples`` pixels spread
    evenly across the whole frame.  This is robust at every size: small frames
    are checked pixel-for-pixel, large frames are checked at a coprime-style
    cadence that still visits every scanline.  Returns
    ``(is_uniform, sampled_rgba_or_None)``.
    """
    total = width * height
    expected = total * 4
    if len(data) < expected:
        return False, None
    first = (data[0], data[1], data[2], data[3])
    samples = max(1, min(total, int(max_samples)))
    for i in range(samples):
        index = (i * total) // samples
        offset = index * 4
        pixel = (data[offset], data[offset + 1], data[offset + 2], data[offset + 3])
        if pixel != first:
            return False, None
    return True, first


def is_effectively_blank(
    data: bytes,
    width: int,
    height: int,
    *,
    max_samples: int = 4096,
    max_unique_colors: int = 4,
    min_dominant_ratio: float = 0.97,
) -> tuple[bool, dict[str, float | int]]:
    """Detect a near-uniform frame that is technically not solid-color.

    A few pixels from an off-canvas element or a one-pixel border can make a
    browser capture pass the exact uniform-frame check while the user still
    sees an empty page.  This deliberately conservative helper is used by the
    page-deck quality gate, not by the generic renderer: a nearly solid frame
    can be a legitimate authored look in a normal video, but it is not enough
    evidence that every generated presentation page contains a composition.
    """
    total = width * height
    expected = total * 4
    if width <= 0 or height <= 0 or len(data) < expected:
        return False, {"sample_count": 0, "unique_colors": 0, "dominant_ratio": 0.0}
    samples = max(1, min(total, int(max_samples)))
    colors: dict[tuple[int, int, int, int], int] = {}
    for i in range(samples):
        index = (i * total) // samples
        offset = index * 4
        pixel = (data[offset], data[offset + 1], data[offset + 2], data[offset + 3])
        colors[pixel] = colors.get(pixel, 0) + 1
    dominant = max(colors.values())
    unique = len(colors)
    ratio = dominant / float(samples)
    return (
        unique <= max(1, int(max_unique_colors)) and ratio >= float(min_dominant_ratio),
        {
            "sample_count": samples,
            "unique_colors": unique,
            "dominant_ratio": round(ratio, 6),
        },
    )


class CaptureFailureCode(str, Enum):
    """Stable diagnostic codes for capture failures."""

    UNSUPPORTED = "unsupported"          # backend cannot run here (no display/headless)
    BLANK = "blank"                      # capture returned a uniform frame
    TIMEOUT = "timeout"                  # page did not reach readiness in budget
    CANCELLED = "cancelled"              # caller cancelled via token
    CRASH = "crash"                      # renderer process terminated abnormally
    INVALID_INPUT = "invalid_input"      # fixture/surface malformed
    BLOCKED_RESOURCE = "blocked_resource"  # required asset/font blocked by policy


@dataclass(frozen=True)
class CaptureSurface:
    """Target output geometry and device pixel ratio for a capture."""

    width: int
    height: int
    dpr: float = 1.0

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("capture surface dimensions must be positive")
        if self.dpr <= 0.0:
            raise ValueError("capture surface DPR must be positive")


@dataclass(frozen=True)
class CaptureEvidence:
    """Provenance recorded alongside every capture for cache keys and ADRs."""

    backend_id: str
    backend_build: str
    capture_method: str
    viewport: tuple[int, int]
    dpr: float
    duration_ms: float = 0.0
    notes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CapturedFrame:
    """A successful offscreen RGBA8 capture plus its evidence."""

    data: bytes
    surface: CaptureSurface
    evidence: CaptureEvidence

    def __post_init__(self) -> None:
        expected = self.surface.width * self.surface.height * 4
        if len(self.data) != expected:
            raise ValueError(
                f"captured RGBA8 data length {len(self.data)} != {expected} "
                f"({self.surface.width}x{self.surface.height})"
            )

    @property
    def content_hash(self) -> str:
        return rgba_sha256(self.data)


@dataclass(frozen=True)
class CaptureFailure:
    """Structured failure returned instead of a silent black/blank frame."""

    code: CaptureFailureCode
    message: str
    backend_id: str
    evidence: CaptureEvidence | None = None


# A capture result is either a real frame or a structured failure.  The union
# is expressed as ``CapturedFrame | CaptureFailure`` so callers must handle both
# explicitly — there is no "best effort" pixel path.
CaptureResult = "CapturedFrame | CaptureFailure"


class FrameCaptureBackend(Protocol):
    """Versioned, fail-closed contract for offscreen HTML→RGBA capture.

    Implementations own their renderer process/widget lifecycle and MUST:

    * report ``backend_id``/``backend_build`` for cache keys and evidence;
    * report ``is_available()`` honestly (no false positives that yield blanks);
    * classify uniform/blank frames as BLANK by default.  ``allow_uniform`` is
      a narrow exception used only after a host-level renderability gate has
      proven the scene paints at another authored frame;
    * honour the cancel token and budget, returning CANCELLED/TIMEOUT;
    * apply the supplied CSP and URL policy (no network, no file scheme).
    """

    backend_id: str
    backend_build: str

    def is_available(self) -> bool:
        """True only if the backend can plausibly produce real pixels here."""
        ...

    def capture(
        self,
        html: str,
        surface: "CaptureSurface",
        *,
        frame: int,
        fps_num: int,
        fps_den: int,
        csp: str,
        url_policy: "UrlPolicy",
        font_set: Mapping[str, str] = (),
        timeout_ms: int = 10000,
        cancel_token: "CancelToken | None" = None,
        transparent: bool = False,
        allow_uniform: bool = False,
    ) -> "CapturedFrame | CaptureFailure":
        ...


class CancelToken:
    """Minimal cooperative cancellation shared with capture backends."""

    def __init__(self) -> None:
        self._cancelled = False

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True


__all__ = [
    "CancelToken",
    "CaptureEvidence",
    "CaptureFailure",
    "CaptureFailureCode",
    "CaptureResult",
    "CaptureSurface",
    "CapturedFrame",
    "FrameCaptureBackend",
    "is_effectively_blank",
    "is_uniform_frame",
    "rgba_sha256",
]
