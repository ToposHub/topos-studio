"""W0-A spike: determinism helpers for frame-seek reproducibility.

The plan's determinism contract for HTML: the same ``renderer_build +
viewport + DPR + font set + asset hash`` MUST produce identical RGBA for the
same frame, regardless of the order frames are requested in or how many times
they repeat.  These helpers turn that contract into runnable assertions.

They are pure functions over bytes so the security/determinism corpus can run
without a real browser; the real-browser test feeds them genuine captures.
"""

from __future__ import annotations

from dataclasses import dataclass

from .capture_backend import CapturedFrame, CaptureFailure


@dataclass(frozen=True)
class DeterminismReport:
    """Result of a repetition + out-of-order determinism probe."""

    same_frame_repeats_identical: bool
    out_of_order_identical: bool
    observed_hashes: tuple[str, ...]
    failure: "CaptureFailure | None" = None


def frame_hashes_equal(a: CapturedFrame, b: CapturedFrame) -> bool:
    """Strict byte-level determinism within one renderer build."""
    return a.content_hash == b.content_hash and a.surface == b.surface


def build_cache_key(
    *,
    backend_id: str,
    backend_build: str,
    source_hash: str,
    frame: int,
    fps_num: int,
    fps_den: int,
    width: int,
    height: int,
    dpr: float,
    font_set_hash: str,
    asset_set_hash: str,
) -> str:
    """Compose the cache key inputs mandated by the plan's RenderEvidence.

    Every field that can change the pixels participates, so a renderer/font/
    asset bump always invalidates stale frames instead of returning a stale
    frame that "looks similar".
    """
    parts = [
        backend_id, backend_build, source_hash, str(int(frame)),
        f"{int(fps_num)}/{int(fps_den)}", f"{int(width)}x{int(height)}",
        f"{dpr:.4f}", font_set_hash[:16], asset_set_hash[:16],
    ]
    return "|".join(parts)


def summarize_repetition(captures: tuple[CapturedFrame, ...]) -> DeterminismReport:
    """Verify N captures of the same target frame are byte-identical."""
    if not captures:
        return DeterminismReport(False, False, (), None)
    if any(isinstance(c, CaptureFailure) for c in captures):  # defensive; type is CapturedFrame
        return DeterminismReport(False, False, (), None)
    hashes = tuple(c.content_hash for c in captures)
    identical = len(set(hashes)) == 1
    return DeterminismReport(identical, identical, hashes, None)


def summarize_out_of_order(
    ordered: dict[int, CapturedFrame],
    shuffled: dict[int, CapturedFrame],
) -> DeterminismReport:
    """Verify that shuffling the request order does not change any frame."""
    if ordered.keys() != shuffled.keys():
        return DeterminismReport(False, False, (), None)
    ordered_hashes = tuple(ordered[f].content_hash for f in sorted(ordered))
    shuffled_hashes = tuple(shuffled[f].content_hash for f in sorted(shuffled))
    identical = ordered_hashes == shuffled_hashes
    return DeterminismReport(identical, identical, ordered_hashes, None)


__all__ = [
    "DeterminismReport",
    "build_cache_key",
    "frame_hashes_equal",
    "summarize_out_of_order",
    "summarize_repetition",
]
