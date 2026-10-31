"""Adaptive scrub display stride — 1x/2x/4x/8x/16x frame sampling.

A scrubbing playhead does not need to preview every intermediate frame.
The decode pipeline chases a stride-quantized target so nearly every
completed decode is displayable, instead of decoding frames the playhead
has already left (those complete as stale and are dropped — wasted cycles
that cap the visible update rate).

- Slow drags keep stride=1: frame-by-frame, identical to before.
- The stride only grows with drag velocity, in power-of-two steps
  (1/2/4/8/16) like fast-forward speeds.
- Quantized targets sit on an absolute floor grid of the stride, so a
  displayed frame is never *ahead* of the playhead, and grid positions
  stay stable while the stride changes mid-drag.
- Full decode resolution is preserved at every stride; the release path
  always lands on the exact frame under the pointer.
"""

from __future__ import annotations

MAX_STRIDE = 16
# Fallback display rate the scrub pipeline can sustain (frames/sec) when no
# completed-cycle measurement is available yet.
DEFAULT_DISPLAY_RATE_CAP = 48.0


def update_velocity(
    velocity: float,
    frame_delta: int,
    dt_s: float,
    alpha: float = 0.35,
) -> float:
    """Smoothed |frames/sec| of scrub input movement (EMA)."""
    if dt_s <= 0.0:
        return max(0.0, velocity)
    instantaneous = abs(int(frame_delta)) / dt_s
    if velocity <= 0.0:
        return instantaneous
    return velocity * (1.0 - alpha) + instantaneous * alpha


def display_stride(velocity: float, sustainable_rate: float) -> int:
    """Smallest power-of-two stride keeping the display rate sustainable.

    1.25x hysteresis keeps the stride from flapping around the boundary:
    once the drag is fast enough to need a stride it stays until the
    velocity falls clearly below what the pipeline can show.
    """
    if velocity <= 0.0 or sustainable_rate <= 0.0:
        return 1
    if velocity <= sustainable_rate * 1.25:
        return 1
    stride = 1
    while stride < MAX_STRIDE and velocity / stride > sustainable_rate:
        stride *= 2
    return stride


def quantize_frame(frame: int, stride: int) -> int:
    """Floor-quantize a playhead position onto the stride grid."""
    stride = max(1, int(stride))
    if stride <= 1:
        return int(frame)
    return (int(frame) // stride) * stride
