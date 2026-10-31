"""Golden-frame pixel comparison helpers (FX-M08).

The edit page renders the same logical frame three ways:

* GPU preview  — ``EditGpuPreviewRenderer`` (ModernGL FBO ping-pong)
* CPU fallback — ``TimelinePreviewRenderer`` (numpy / cv2, no GL)
* Timeline export — ``TimelineExporter`` (a second independent numpy path)

These three paths do NOT share a compositing/effect implementation — they
each re-implement the same logic — so any drift between them is invisible
without a ground-truth pixel comparison. This module provides that
comparison.

Design rules (mirroring the audit ``§5`` architecture constraints):

1. A comparison must report *where* and *how badly* it differs, not just a
   boolean pass/fail. On failure it dumps a diff image plus a textual error
   summary so a future contributor can locate the regression without running
   a GUI.
2. SDR (8-bit) uses an absolute tolerance of ``<= 2/255`` per channel. HDR /
   float surfaces use a combined absolute + relative error.
3. A ``degraded`` preview (e.g. preview-quality policy downscaling an effect)
   must NOT be accepted as export parity — the comparator refuses to compare
   frames flagged ``degraded=True``.

Nothing here imports PyQt6 or ModernGL, so it can be used from any headless
test.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import Optional

import numpy as np

# Per-channel SDR tolerance used by :func:`assert_frames_equal` when
# ``hdr=False``. 2/255 is the audit's stated maximum-absolute-error bound
# for preview↔export 8-bit parity (audit §3 FX-M08).
SDR_MAX_ABS_ERR = 2.0 / 255.0


@dataclass(frozen=True)
class FrameComparison:
    """Outcome of comparing two frames.

    Attributes:
        max_abs_err: largest per-channel absolute difference.
        mse: mean squared error over all channels.
        worst_coord: (y, x, channel) of ``max_abs_err``.
        shape: ``(h, w, c)`` of the compared arrays (after alignment).
    """

    max_abs_err: float
    mse: float
    worst_coord: tuple
    shape: tuple

    @property
    def rmse(self) -> float:
        return float(np.sqrt(self.mse))


def _to_float(arr: np.ndarray) -> np.ndarray:
    """Return ``arr`` as float32 in the same value range it already has.

Range normalisation is fixed and unambiguous so tolerances mean the same
thing regardless of how a frame was produced:

* ``uint8`` SDR frames are divided by 255 → normalised to ``[0, 1]``.
* ``float`` frames are taken as-is (SDR floats are already ``[0, 1]``; HDR
  floats may exceed 1.0, which is exactly the banding signal we test for).

So a tolerance of ``2/255`` always means "2 out of 256 SDR levels", whether
the caller passed a uint8 buffer or a pre-normalised float.
"""
    arr = np.asarray(arr)
    if arr.dtype == np.uint8:
        return arr.astype(np.float32) / 255.0
    return arr.astype(np.float32)


def _align_shape(got: np.ndarray, golden: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Broadcast-compatible alignment of two frames.

    Both must end up the same shape. Channels are padded with the last
    channel (so a 3-channel BGR can be compared against a 4-channel BGRA
    when alpha is irrelevant to the assertion). The smaller channel count
    is extended by repeating its last channel.
    """
    got = np.asarray(got)
    golden = np.asarray(golden)
    if got.shape[:2] != golden.shape[:2]:
        raise ValueError(
            f"spatial shape mismatch: got {got.shape[:2]} vs golden {golden.shape[:2]}"
        )
    cg = got.shape[2] if got.ndim == 3 else 1
    cf = golden.shape[2] if golden.ndim == 3 else 1
    if got.ndim != 3:
        got = got[..., None]
    if golden.ndim != 3:
        golden = golden[..., None]
    cmax = max(cg, cf)
    while got.shape[2] < cmax:
        got = np.concatenate(
            [got, got[..., -1:]], axis=2,
        )
    while golden.shape[2] < cmax:
        golden = np.concatenate(
            [golden, golden[..., -1:]], axis=2,
        )
    return got, golden


def compare_frames(
    got: np.ndarray,
    golden: np.ndarray,
) -> FrameComparison:
    """Compute pixel-error statistics between ``got`` and ``golden``.

    Returns a :class:`FrameComparison`; never raises on pixel mismatch (that
    is :func:`assert_frames_equal`'s job). Tolerates uint8/float and
    differing channel counts.
    """
    got = _to_float(got)
    golden = _to_float(golden)
    got, golden = _align_shape(got, golden)
    diff = got - golden
    abs_diff = np.abs(diff)
    mse = float(np.mean(diff * diff))
    flat_idx = int(np.argmax(abs_diff))
    worst_coord = np.unravel_index(flat_idx, abs_diff.shape)
    return FrameComparison(
        max_abs_err=float(abs_diff.max()),
        mse=mse,
        worst_coord=tuple(int(i) for i in worst_coord),
        shape=tuple(got.shape),
    )


def _dump_artifacts(
    got: np.ndarray,
    golden: np.ndarray,
    diff: np.ndarray,
    label: str,
) -> str:
    """Persist ``got``/``golden``/``diff`` as .npy files for offline inspection.

    Returns the directory path so the assertion message can point the user
    at it. Uses a process-private temp dir because pytest's tmp_path is not
    available here (this runs inside the assertion, not the test body).
    """
    try:
        import cv2  # noqa: F401  — availability check only
        has_cv2 = True
    except Exception:  # noqa: BLE001
        has_cv2 = False

    out_dir = tempfile.mkdtemp(prefix=f"golden_diff_{label}_")
    np.save(os.path.join(out_dir, "got.npy"), np.asarray(got))
    np.save(os.path.join(out_dir, "golden.npy"), np.asarray(golden))
    np.save(os.path.join(out_dir, "diff.npy"), np.asarray(diff))
    if has_cv2:
        import cv2

        def _save_png(name: str, arr: np.ndarray) -> None:
            a = np.asarray(arr)
            if a.dtype != np.uint8:
                # Normalise for visual inspection only — non-authoritative.
                finite = a[np.isfinite(a)]
                lo = float(finite.min()) if finite.size else 0.0
                hi = float(finite.max()) if finite.size else 1.0
                rng = hi - lo if hi > lo else 1.0
                a = np.clip((a - lo) / rng, 0.0, 1.0)
                a = (a * 255.0).astype(np.uint8)
            vis = a
            if vis.ndim == 3 and vis.shape[2] == 4:
                vis = vis[:, :, :3]
            if vis.ndim == 2 or (vis.ndim == 3 and vis.shape[2] == 1):
                vis = np.squeeze(vis)
            try:
                cv2.imwrite(os.path.join(out_dir, name), vis)
            except Exception:  # noqa: BLE001
                pass

        _save_png("got.png", got)
        _save_png("golden.png", golden)
        _save_png("diff.png", diff)
    return out_dir


def assert_frames_equal(
    got: np.ndarray,
    golden: np.ndarray,
    *,
    max_abs_err: Optional[float] = None,
    tol_pct: float = 0.0,
    hdr: bool = False,
    label: str = "frame",
    degraded: bool = False,
) -> FrameComparison:
    """Assert that ``got`` and ``golden`` match within tolerance.

    Args:
        got: the frame under test (uint8 or float, any channel count).
        golden: the reference frame (same space as ``got``).
        max_abs_err: per-channel absolute tolerance. ``None`` selects the
            audit default: ``SDR_MAX_ABS_ERR`` (2/255) for SDR,
            ``1.0/1023.0``-scaled for HDR via ``tol_pct`` instead.
        tol_pct: extra relative tolerance as a fraction of the golden
            value's local magnitude (0 = off). Mainly used for HDR.
        hdr: when True the absolute default is loosened and ``tol_pct``
            drives the comparison (HDR banding is a *relative* signal).
        label: human-readable tag used in the failure message / artifact dir.
        degraded: when True the frames are *not* comparable for parity
            purposes (one was a downgraded preview) — raise immediately so a
            test cannot silently pass on a degraded frame.

    Raises:
        AssertionError: with a full error summary and artifact path.
    """
    if degraded:
        raise AssertionError(
            f"[{label}] refusing to compare a degraded preview frame as "
            "export parity (audit FX-M08: a degraded preview must not be "
            "accepted as ground truth)."
        )

    cmp = compare_frames(got, golden)

    if max_abs_err is None:
        max_abs_err = 0.05 if hdr else SDR_MAX_ABS_ERR

    passed_abs = cmp.max_abs_err <= max_abs_err + 1e-9
    passed_rel = True
    if tol_pct > 0.0:
        golden_f = _to_float(golden)
        got_f = _to_float(got)
        golden_f, got_f = _align_shape(golden_f, golden_f)  # align once
        # Recompute aligned pair for relative check.
        g2, f2 = _align_shape(got_f, golden_f)
        denom = np.maximum(np.abs(f2), 1e-6)
        rel_err = np.abs(g2 - f2) / denom
        passed_rel = float(rel_err.max()) <= tol_pct

    if passed_abs and passed_rel:
        return cmp

    # Failure — build a diagnostic dump and a readable message.
    got_f = _to_float(got)
    golden_f = _to_float(golden)
    diff = got_f - golden_f
    out_dir = _dump_artifacts(got_f, golden_f, diff, label)
    y, x, c = cmp.worst_coord
    mode = "HDR/float" if hdr else "SDR(8-bit)"
    raise AssertionError(
        f"[{label}] {mode} pixel mismatch:\n"
        f"  max_abs_err = {cmp.max_abs_err:.6f} (limit {max_abs_err:.6f})\n"
        f"  mse         = {cmp.mse:.6f}   rmse = {cmp.rmse:.6f}\n"
        f"  worst pixel = (y={y}, x={x}, ch={c})\n"
        f"  shape       = {cmp.shape}\n"
        f"  diff artifacts written to: {out_dir}"
    )


def frames_equivalent(
    got: np.ndarray,
    golden: np.ndarray,
    *,
    max_abs_err: Optional[float] = None,
    hdr: bool = False,
) -> bool:
    """Non-throwing variant of :func:`assert_frames_equal`.

    Useful inside parametrised loops where you want to collect all failing
    cases rather than abort on the first.
    """
    try:
        assert_frames_equal(got, golden, max_abs_err=max_abs_err, hdr=hdr)
        return True
    except AssertionError:
        return False
