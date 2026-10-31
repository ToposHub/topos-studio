"""W4: preview/export frame consistency + visual regression evidence (AQ-04, AQ-05).

Preview and export MUST share the same ``SceneEvaluator`` semantics; the only
allowed differences are resolution/quality, never time, layering, alpha or
colour meaning.  This module turns that contract into runnable assertions by
comparing two RGBA8 captures and reporting where they diverge.

It supports two modes:
* ``strict`` — same renderer build + profile requires identical bytes (golden
  regression for renderer/effect changes, AQ-04).
* ``perceptual`` — across builds/profiles where exact bytes are not expected,
  allow a configurable mean-per-channel tolerance (AQ-05 consistency).

No PIL/numpy dependency: differences are computed directly over the byte
buffers so the check runs in every environment, including headless workers.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum


class ComparisonMode(str, Enum):
    STRICT = "strict"
    PERCEPTUAL = "perceptual"


@dataclass(frozen=True)
class FrameSnapshot:
    """A minimal RGBA8 capture description used for comparison."""

    width: int
    height: int
    data: bytes
    render_profile: str = ""
    renderer_build: str = ""

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("frame dimensions must be positive")
        if len(self.data) != self.width * self.height * 4:
            raise ValueError("RGBA8 data length does not match dimensions")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class ConsistencyIssue:
    code: str          # dimension_mismatch | hash_mismatch | perceptual_exceeded | alpha_mismatch
    message: str
    metric: float = 0.0


@dataclass(frozen=True)
class ConsistencyResult:
    mode: ComparisonMode
    passed: bool
    issues: tuple[ConsistencyIssue, ...] = ()
    mean_abs_diff: float = 0.0
    max_abs_diff: int = 0

    @property
    def diagnostics(self) -> tuple[str, ...]:
        return tuple(issue.message for issue in self.issues)


def _perceptual_stats(a: FrameSnapshot, b: FrameSnapshot) -> tuple[float, int]:
    """Return (mean abs per-channel diff, max abs diff) over all pixels.

    Assumes equal dimensions; caller guarantees that.
    """
    total = 0
    count = 0
    max_diff = 0
    data_a = a.data
    data_b = b.data
    # Sample every pixel but only RGB (skip alpha for the perceptual stat; alpha
    # is checked separately to report alpha_mismatch distinctly).
    for pixel in range(a.width * a.height):
        base = pixel * 4
        for channel in range(3):
            diff = abs(data_a[base + channel] - data_b[base + channel])
            total += diff
            if diff > max_diff:
                max_diff = diff
        count += 3
    mean = (total / count) if count else 0.0
    return mean, max_diff


def _max_alpha_diff(a: FrameSnapshot, b: FrameSnapshot) -> int:
    data_a = a.data
    data_b = b.data
    max_diff = 0
    for pixel in range(a.width * a.height):
        diff = abs(data_a[pixel * 4 + 3] - data_b[pixel * 4 + 3])
        if diff > max_diff:
            max_diff = diff
    return max_diff


def compare_frames(
    preview: FrameSnapshot,
    export: FrameSnapshot,
    *,
    mode: ComparisonMode = ComparisonMode.STRICT,
    perceptual_tolerance: float = 2.0,
    alpha_tolerance: int = 0,
) -> ConsistencyResult:
    """Compare a preview capture against an export capture (AQ-05)."""
    issues: list[ConsistencyIssue] = []
    if preview.width != export.width or preview.height != export.height:
        issues.append(ConsistencyIssue(
            "dimension_mismatch",
            f"dimensions differ: preview {preview.width}x{preview.height} vs export {export.width}x{export.height}",
        ))
        return ConsistencyResult(mode=mode, passed=False, issues=tuple(issues))

    if mode == ComparisonMode.STRICT:
        if preview.data != export.data:
            issues.append(ConsistencyIssue(
                "hash_mismatch",
                f"strict byte mismatch: preview={preview.sha256[:12]} export={export.sha256[:12]}",
            ))
        return ConsistencyResult(mode=mode, passed=not issues, issues=tuple(issues))

    # Perceptual mode.
    mean_diff, max_diff = _perceptual_stats(preview, export)
    alpha_diff = _max_alpha_diff(preview, export)
    if mean_diff > perceptual_tolerance:
        issues.append(ConsistencyIssue(
            "perceptual_exceeded",
            f"mean per-channel diff {mean_diff:.3f} exceeds tolerance {perceptual_tolerance}",
            metric=mean_diff,
        ))
    if alpha_diff > alpha_tolerance:
        issues.append(ConsistencyIssue(
            "alpha_mismatch",
            f"max alpha diff {alpha_diff} exceeds tolerance {alpha_tolerance}",
            metric=float(alpha_diff),
        ))
    return ConsistencyResult(
        mode=mode, passed=not issues, issues=tuple(issues),
        mean_abs_diff=mean_diff, max_abs_diff=max_diff,
    )


def golden_evidence(snapshot: FrameSnapshot, *, frame: int, label: str) -> dict:
    """Build a serializable evidence record for a golden/regression frame (AQ-04)."""
    return {
        "label": label, "frame": frame,
        "width": snapshot.width, "height": snapshot.height,
        "renderer_build": snapshot.renderer_build, "render_profile": snapshot.render_profile,
        "sha256": snapshot.sha256,
    }


__all__ = [
    "ComparisonMode",
    "ConsistencyIssue",
    "ConsistencyResult",
    "FrameSnapshot",
    "golden_evidence",
    "compare_frames",
]
