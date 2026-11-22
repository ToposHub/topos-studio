"""Resolve and mix composition audio on playback and export workers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from fractions import Fraction
from functools import lru_cache
from math import floor
from pathlib import Path
from typing import Any

import av
import numpy as np

from .comp_audio import (
    AudioStreamInfo, LayerAudio, LayerAudioTiming, audible_layers,
    build_audio_plan, sample_boundary,
)
from .comp_audio_mix import (
    AUDIO_STATE_ERROR, AUDIO_STATE_MISSING_MEDIA, AUDIO_STATE_NO_STREAM,
    AUDIO_STATE_SILENT, AudioExportRange, mixdown,
)
from .layer_time import fps_fraction
from .media_drop_payload import MEDIA_TYPE_AUDIO, MediaDropPayload

SAMPLE_RATE = 48_000


@dataclass(frozen=True)
class CompositionPCM:
    samples: np.ndarray | None
    state: str = AUDIO_STATE_SILENT
    detail: str = ""


@dataclass(frozen=True)
class _DecodeWindow:
    path: str
    offset: int
    limit: int
    length: int
    loop_start: int
    loop_length: int
    decode_rate: int


@lru_cache(maxsize=128)
def _probe(path: str, revision: tuple[int, int], stream_index: int) -> AudioStreamInfo | None:
    with av.open(path) as container:
        stream = next((audio for audio in container.streams.audio
                       if stream_index < 0 or audio.index == stream_index), None)
        if stream is None:
            return None
        time_base = stream.time_base or Fraction(0)
        video = next(iter(container.streams.video), None)
        first_video = ((video.start_time or 0) * (video.time_base or Fraction(0))
                       if video else (stream.start_time or 0) * time_base)
        relative_start = (stream.start_time or 0) * time_base - first_video
        sample_rate = stream.codec_context.sample_rate or 0
        return AudioStreamInfo(
            "", stream.index, sample_rate, stream.codec_context.channels,
            time_base, floor(relative_start / time_base) if time_base else 0,
            floor(stream.duration * time_base * sample_rate)
            if stream.duration is not None and time_base else None,
        )


def prepare_audio_drop(payload: MediaDropPayload) -> MediaDropPayload:
    """Probe a dropped sound file on the media preparation worker."""
    if payload.media_type != MEDIA_TYPE_AUDIO:
        return payload
    stat = Path(payload.path).stat()
    info = _probe(payload.path, (stat.st_mtime_ns, stat.st_size), -1)
    if info is None or not info.valid:
        raise ValueError(f"{payload.display_name}: no decodable audio stream")
    extras = {**payload.extras, "audio_stream_index": info.stream_index}
    extras["audio_info"] = {
        "sample_rate": info.sample_rate, "channel_count": info.channels,
        "stream_index": info.stream_index, "start_pts": info.start_pts,
        "time_base": str(info.time_base),
    }
    if info.duration_samples is not None:
        extras["duration_seconds"] = info.duration_samples / info.sample_rate
    return replace(payload, extras=extras)


def composition_audio_layers(graph: Mapping[str, Any]) -> list[tuple[str, Mapping, LayerAudio]]:
    """Read enabled audio routes from the main composition stack."""
    layers = graph.get("layers") or {}
    stacks = graph.get("layer_stacks") or {}
    main = stacks.get("main") or next(iter(stacks.values()), None)
    ids = main.get("layer_ids", ()) if main else layers
    entries = []
    for layer_id in ids:
        layer = layers.get(layer_id) or {}
        audio = LayerAudio.from_dict(layer.get("audio"))
        if audio is not None:
            entries.append((layer_id, layer, audio))
    return entries


def render_composition_pcm(
    graph: Mapping[str, Any],
    start_frame: int,
    end_frame: int,
    *,
    fps: Any = None,
    sample_rate: int = SAMPLE_RATE,
    project: Any = None,
    cancel_check: Callable[[], bool] | None = None,
) -> CompositionPCM:
    """Resolve and render one inclusive range of the immutable graph."""
    entries = composition_audio_layers(graph)
    audible = set(audible_layers([(layer_id, audio) for layer_id, _, audio in entries]))
    if not audible or end_frame < start_frame:
        return CompositionPCM(None)
    fps = fps_fraction(fps if fps is not None else graph.get("fps", 24))
    if fps <= 0 or sample_rate <= 0:
        return CompositionPCM(None, AUDIO_STATE_ERROR, "Invalid composition audio timebase")

    from src.features.edit.core.clip_pcm import decode_audio_range

    nodes = {node["id"]: node for node in graph.get("nodes", ())
             if isinstance(node, Mapping) and "id" in node}
    get_asset = getattr(project, "get_asset_by_id", None)
    streams = {}
    timings = []
    windows = {}
    for layer_id, layer, audio in entries:
        if (layer_id, audio) not in audible:
            continue
        if cancel_check is not None and cancel_check():
            return CompositionPCM(None)
        first = layer.get("in_frame")
        first = 0 if first is None else int(first)
        last = layer.get("out_frame")
        last = int(graph.get("duration_frames") or end_frame + 1) - 1 if last is None else int(last)
        left, right = max(start_frame, first), min(end_frame, last)
        if left > right:
            continue
        source = nodes.get(layer.get("source_node_id") or layer.get("root_node_id"), {})
        params = source.get("parameters") or {}
        source_range = source.get("time_range") or {}
        if not source_range.get("enabled", True):
            source_range = {}
        asset = get_asset(audio.asset_id) if get_asset is not None else None
        path = str(getattr(asset, "path", "") or params.get("file_path") or "")
        if not path:
            return CompositionPCM(None, AUDIO_STATE_MISSING_MEDIA, audio.asset_id)
        try:
            stat = Path(path).stat()
            info = _probe(path, (stat.st_mtime_ns, stat.st_size), audio.stream_index)
        except FileNotFoundError:
            return CompositionPCM(None, AUDIO_STATE_MISSING_MEDIA, audio.asset_id)
        except (OSError, av.error.FFmpegError, ValueError) as exc:
            return CompositionPCM(None, AUDIO_STATE_ERROR, f"{audio.asset_id}: {exc}")
        if info is None or not info.valid:
            if audio.stream_index < 0 and source.get("type") == "video_frame":
                continue
            return CompositionPCM(None, AUDIO_STATE_NO_STREAM, audio.asset_id)

        source_fps = fps_fraction(params.get("fps")) or fps
        source_step = fps_fraction(source_range.get("source_step")) or source_fps / fps
        rate = fps_fraction(source_range.get("source_rate")) or source_step * fps / source_fps
        speed = fps_fraction(params.get("speed", 1))
        rate *= speed
        if rate <= 0 or source_range.get("reverse") or source_range.get("time_remap"):
            return CompositionPCM(None, AUDIO_STATE_ERROR,
                                  f"{audio.asset_id}: audio requires forward constant playback")
        decode_rate = round(sample_rate / rate)
        if not 1000 <= decode_rate <= 384_000:
            return CompositionPCM(None, AUDIO_STATE_ERROR,
                                  f"{audio.asset_id}: audio playback rate is out of range")
        source_in = int(params.get("source_in", 0))
        anchor = (source_in + fps_fraction(source_range.get("source_phase"))) / source_fps
        index = None
        if "source_time_origin" in source_range:
            from src.shared.media.presentation_time import ready_presentation_index

            anchor = fps_fraction(source_range["source_time_origin"])
            index = ready_presentation_index(path)
        anchor += fps_fraction(params.get("time_offset")) * speed
        delay = info.start_pts * info.time_base
        offset = floor((anchor - delay) * decode_rate)
        offset += sample_boundary(left, fps, sample_rate) - sample_boundary(first, fps, sample_rate)
        length = (sample_boundary(right + 1, fps, sample_rate)
                  - sample_boundary(left, fps, sample_rate))
        source_limit = (floor(Fraction(info.duration_samples * decode_rate, info.sample_rate))
                        if info.duration_samples is not None else offset + length)
        source_out = params.get("source_out", -1)
        media_end = None
        if isinstance(source_out, int) and source_out >= 0:
            media_end = Fraction(source_out + 1) / source_fps
        elif isinstance((total_frames := params.get("total_frames")), int) and total_frames > 0:
            media_end = Fraction(total_frames) / source_fps
        media_start = Fraction(source_in) / source_fps
        if index is not None:
            media_start = index.seconds_at(source_in)
            media_end = index.seconds_at(source_out + 1 if source_out >= 0 else len(index.pts))
        if media_end is not None:
            source_limit = min(source_limit, floor((media_end - delay) * decode_rate))
        loop_start = floor((media_start - delay) * decode_rate)
        loop_end = floor((media_end - delay) * decode_rate) if media_end is not None else source_limit
        loop_length = max(0, loop_end - loop_start) if params.get("loop") else 0
        selected = replace(audio, stream_index=info.stream_index)
        streams[(audio.asset_id, info.stream_index)] = info
        timings.append(LayerAudioTiming(
            layer_id, selected, left, right,
            floor(Fraction(offset * info.sample_rate, decode_rate)),
        ))
        windows[layer_id] = _DecodeWindow(path, offset, source_limit, length, loop_start,
                                          loop_length, decode_rate)

    if not timings:
        return CompositionPCM(None)
    plan = build_audio_plan(timings, fps=fps, sample_rate=sample_rate, streams=streams)
    decoded = {}
    for segment in plan.segments:
        window = windows[segment.layer_id]
        pcm = np.zeros((window.length, 2), dtype=np.float32)
        offset = window.offset
        cursor = 0
        cache = {}
        while cursor < window.length:
            if cancel_check is not None and cancel_check():
                return CompositionPCM(None)
            count = window.length - cursor
            if window.loop_length:
                offset = window.loop_start + (offset - window.loop_start) % window.loop_length
                count = min(count, window.loop_start + window.loop_length - offset)
            begin = max(0, offset)
            stop = min(offset + count, window.limit)
            if stop > begin:
                key = (begin, stop)
                samples = cache.get(key)
                if samples is None:
                    samples = decode_audio_range(window.path, begin, stop, window.decode_rate,
                                                 stream_index=segment.stream_index, cancel_check=cancel_check)
                    if samples is None:
                        if cancel_check is not None and cancel_check():
                            return CompositionPCM(None)
                        return CompositionPCM(None, AUDIO_STATE_ERROR, segment.asset_id)
                    cache[key] = samples
                leading = cursor + begin - offset
                pcm[leading:leading + samples.shape[1]] = samples.T
            offset += count
            cursor += count
        decoded[f"{segment.layer_id}:{segment.asset_id}#{segment.stream_index}"] = pcm
    samples, state = mixdown(plan, decoded, AudioExportRange(start_frame, end_frame), fps=fps)
    return CompositionPCM(samples.T, state)
