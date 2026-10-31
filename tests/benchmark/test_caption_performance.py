"""SUB-P2-03: caption workspace scalability benchmark (1000 cues).

The plan's Phase 2A acceptance gate requires "1000 cue 流畅" (smooth with 1000
cues). This benchmark makes that measurable and repeatable for the data/UI
layers that can run headless, without depending on a real GL preview:

* :class:`CaptionCueModel.reload_from_sequence` — building the row list for 1000
  cues must be well under a perceptible hiccup (the model is virtualized: Qt
  only queries visible rows via ``data()``).
* on-demand ``data()`` access for first / middle / last row — proves the model
  serves any row cheaply (no full-table materialization at paint time).
* :func:`find_in_clips` over 1000 cues — the replace-all search path must stay
  linear and cheap.

These are NOT the Phase-1 render-budget thresholds (16 ms / 33 ms P95, which
need a live GL context). They are loose, CI-stable guards that the cue list /
search stay usable at scale and don't accidentally regress to quadratic or
full-materialization behaviour. Tightened only if a real regression appears.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.timeline_v2 import (  # noqa: E402
    EditTrack,
    SubtitleClip,
    TimelineSequence,
)
from src.features.edit.ui.caption_workspace import (  # noqa: E402
    CaptionCueModel,
    CueColumn,
    find_in_clips,
    frame_to_timecode,
)

pytestmark = pytest.mark.benchmark

N_CUES = 1000

# Loose, CI-stable thresholds (seconds). These guard against accidental
# quadratic / full-materialization regressions, not against perceptible lag
# (which needs a live GL context and is covered elsewhere).
RELOAD_BUDGET_S = 0.25      # building 1000 rows
PER_DATA_BUDGET_S = 0.005   # a single data() lookup
FIND_BUDGET_S = 0.05        # scanning 1000 cues for a needle


def _make_sequence(n: int) -> TimelineSequence:
    """Build a sequence with n non-overlapping cues on one subtitle track."""
    cues = []
    for i in range(n):
        cues.append(SubtitleClip(
            clip_id=f"c{i}", track_id="s1",
            timeline_start=i * 60, timeline_duration=48,
            text=f"cue number {i}",
        ))
    track = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        subtitle_clips=tuple(cues),
    )
    return TimelineSequence(
        timeline_id="seq-1", name="Seq", width=1920, height=1080, fps=24,
        subtitle_tracks=(track,),
    )


@pytest.fixture(scope="module")
def big_sequence() -> TimelineSequence:
    return _make_sequence(N_CUES)


# ──────────────────────────────────────────────────────────────────────────
# Sanity / correctness at scale (cheap to always run)
# ──────────────────────────────────────────────────────────────────────────

def test_frame_to_timecode_stable_at_scale() -> None:
    """Timecode formatting is pure and deterministic for a large cue count."""
    assert frame_to_timecode(0, 24.0) == "00:00:00:00"
    assert frame_to_timecode(N_CUES * 60, 24.0) == frame_to_timecode(N_CUES * 60, 24.0)


def test_model_rowcount_matches_cue_count(big_sequence, qapp) -> None:
    model = CaptionCueModel()
    model.reload_from_sequence(big_sequence, fps=24.0)
    assert model.rowCount() == N_CUES


def test_find_returns_expected_matches_at_scale(big_sequence) -> None:
    clips = list(big_sequence.subtitle_tracks[0].subtitle_clips)
    # Every cue contains "cue number"; expect N_CUES matches.
    matches = find_in_clips(clips, "cue number")
    assert len(matches) == N_CUES
    # A needle present in one cue only.
    solo = find_in_clips(clips, "number 500")
    assert len(solo) == 1
    assert solo[0].clip_id == "c500"


# ──────────────────────────────────────────────────────────────────────────
# Timing benchmarks (loose guards)
# ──────────────────────────────────────────────────────────────────────────

def test_bench_reload_1000_cues(big_sequence, qapp) -> None:
    """reload_from_sequence over 1000 cues stays within the reload budget."""
    model = CaptionCueModel()
    # Warm up once (first call may JIT-import).
    model.reload_from_sequence(big_sequence, fps=24.0)
    start = time.perf_counter()
    model.reload_from_sequence(big_sequence, fps=24.0)
    elapsed = time.perf_counter() - start
    assert model.rowCount() == N_CUES
    assert elapsed < RELOAD_BUDGET_S, (
        f"reload of {N_CUES} cues took {elapsed:.3f}s (budget {RELOAD_BUDGET_S}s)"
    )


def test_bench_data_lookup_cheap_per_row(big_sequence, qapp) -> None:
    """data() for first/middle/last row is cheap (virtualized, on-demand)."""
    from PyQt6.QtCore import Qt
    model = CaptionCueModel()
    model.reload_from_sequence(big_sequence, fps=24.0)
    rows_to_probe = (0, N_CUES // 2, N_CUES - 1)
    for r in rows_to_probe:
        idx = model.index(r, CueColumn.TEXT)
        start = time.perf_counter()
        value = model.data(idx, int(Qt.ItemDataRole.DisplayRole))
        elapsed = time.perf_counter() - start
        assert value is not None
        assert elapsed < PER_DATA_BUDGET_S, (
            f"data() row {r} took {elapsed:.5f}s (budget {PER_DATA_BUDGET_S}s)"
        )


def test_bench_find_in_clips_1000(big_sequence) -> None:
    """find_in_clips over 1000 cues stays within the find budget."""
    clips = list(big_sequence.subtitle_tracks[0].subtitle_clips)
    # Warm up.
    find_in_clips(clips, "warmup")
    start = time.perf_counter()
    matches = find_in_clips(clips, "cue number")
    elapsed = time.perf_counter() - start
    assert len(matches) == N_CUES
    assert elapsed < FIND_BUDGET_S, (
        f"find_in_clips over {N_CUES} cues took {elapsed:.4f}s "
        f"(budget {FIND_BUDGET_S}s)"
    )
