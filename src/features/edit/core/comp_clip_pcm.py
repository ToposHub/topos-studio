"""Composition clips as virtual PCM inputs to the existing Edit audio mixer."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
import logging
from math import floor
from typing import TYPE_CHECKING, Any

import numpy as np

from src.features.composite.core.comp_audio import sample_boundary
from src.features.composite.core.composition_pcm import composition_audio_layers, render_composition_pcm
from src.features.composite.core.layer_time import fps_fraction

if TYPE_CHECKING:
    from .timeline_v2 import EditTrack, TimelineSequence

logger = logging.getLogger(__name__)


class CompClipAudioError(ValueError):
    """A composition's advertised audio source could not be rendered."""


def comp_clip_audio_graph(project: Any, asset_id: str) -> dict | None:
    get_comp_clip = getattr(project, "get_comp_clip_by_asset_id", None)
    clip = get_comp_clip(asset_id) if get_comp_clip is not None else None
    graph = project.compositions.get(clip.graph_id) if clip is not None else None
    return graph if graph and composition_audio_layers(graph) else None


def with_comp_clip_audio(
    sequence: TimelineSequence, tracks: tuple[EditTrack, ...], project: Any,
) -> tuple[EditTrack, ...]:
    """Mount contributions from picture-only Comp Clip instances once."""
    if project is None:
        return tracks
    from .timeline_v2 import EditTrack

    audio_clips = tuple(clip for track in tracks for clip in track.clips)
    owned_spans = {(clip.asset_id, clip.timeline_start, clip.timeline_duration)
                   for clip in audio_clips}
    extensions = []
    for track in getattr(sequence, "video_tracks", ()):
        if not track.is_enabled:
            continue
        for clip in track.clips:
            if not clip.is_enabled or clip.composite_bypassed or clip.freeze_source_frame is not None:
                continue
            span = (clip.asset_id, clip.timeline_start, clip.timeline_duration)
            graph = comp_clip_audio_graph(project, clip.asset_id)
            if span in owned_spans or graph is None:
                continue
            linked_sources = {
                audio.asset_id for audio in audio_clips
                if audio.clip_id == clip.linked_clip_id or audio.linked_clip_id == clip.clip_id
            }
            if linked_sources and all(
                    route.asset_id in linked_sources for _, _, route in composition_audio_layers(graph)):
                continue
            identifier = f"comp-audio:{clip.clip_id}"
            extensions.append(EditTrack(
                track_id=identifier, name=track.name, track_type="audio",
                clips=(replace(clip, clip_id=identifier, track_id=identifier),),
            ))
    return (*tracks, *extensions)


class CompClipPCMSource:
    """Serve bounded planar sample windows at the decoder's source rate."""

    def __init__(self, graph: dict, project: Any, rate: int, *, strict: bool = False) -> None:
        self._graph = graph
        self._project = project
        self._rate = rate
        self._fps = fps_fraction(graph.get("fps")) or Fraction(24)
        self._frames = max(0, int(graph.get("duration_frames") or 0))
        self.duration_samples = sample_boundary(self._frames, self._fps, rate)
        self._start = 0
        self._cached: np.ndarray | None = None
        self._strict = strict
        self._reported = False

    @property
    def sample_rate(self) -> int:
        return self._rate

    def read(self, position: int, count: int) -> np.ndarray | None:
        end = min(position + count, self.duration_samples)
        if end <= position:
            return None
        if (self._cached is None or position < self._start
                or end > self._start + self._cached.shape[1]):
            first = max(0, floor(Fraction(position, self._rate) * self._fps))
            # PCM boundaries round down; the exact sample may already be in
            # the next composition frame at a fractional boundary.
            if sample_boundary(first + 1, self._fps, self._rate) <= position:
                first += 1
            last = min(self._frames - 1, max(
                floor(Fraction(end, self._rate) * self._fps),
                first + round(5 * self._fps),
            ))
            result = render_composition_pcm(
                self._graph, first, last, sample_rate=self._rate, project=self._project,
            )
            if result.detail:
                message = f"Composition audio: {result.state} ({result.detail})"
                if self._strict:
                    raise CompClipAudioError(message)
                if not self._reported:
                    logger.warning("%s", message)
                    self._reported = True
            self._start = sample_boundary(first, self._fps, self._rate)
            stop = sample_boundary(last + 1, self._fps, self._rate)
            self._cached = result.samples if result.samples is not None else np.zeros(
                (2, stop - self._start), dtype=np.float32,
            )
        return self._cached[:, position - self._start:end - self._start].copy()
