"""Speaker domains and gain rules shared by live and offline mixing."""
from __future__ import annotations

from typing import Any

import numpy as np

from src.features.edit.core.audio_dsp import pan_gains_for_channels, surround_panner_gains
from src.features.edit.core.channel_layout import ChannelLayout, route_pcm

MIX_LAYOUTS = ("mono", "stereo", "5.1", "7.1")

# Effects with channel-width-independent state and a common detector where
# applicable. Effects built around a stereo pair need an explicit layout adapter.
NATIVE_EFFECT_TYPES = frozenset({
    "strip_eq", "compressor", "limiter", "expander", "noise_gate",
    "saturation", "true_peak_limiter",
})


def mix_layout(name: str) -> ChannelLayout:
    if name not in MIX_LAYOUTS:
        raise ValueError(f"Unsupported mix layout: {name!r}")
    return ChannelLayout.from_av_layout_name(name)


def incompatible_effect(entries: list | tuple) -> str | None:
    return next((entry[0] for entry in entries
                 if entry[2] and (entry[1] or {}).get("enabled", True)
                 and entry[0] not in NATIVE_EFFECT_TYPES), None)


def pan_clip_pcm(samples: np.ndarray, source_channels: int, pan: float,
                 ramp: np.ndarray | None = None, *, native: bool = False) -> np.ndarray:
    """Apply mono placement or bed balance before clips are summed."""
    channels = samples.shape[0]
    if native and channels == 1:
        return samples
    if ramp is None and source_channels > 1 and pan == 0.0:
        return samples
    if native and channels in (6, 8) and source_channels == 1:
        # route_pcm put the source in C. Keep LFE silent. The ordinary pan
        # knob maps to the front pair; surround rear/side beds pass through.
        if ramp is None:
            gains = surround_panner_gains(channels, float(np.clip(pan, -1, 1)) * 30.0)
        else:
            # Only the two adjacent front speakers participate at a time.
            theta = np.deg2rad(np.abs(np.clip(ramp[:samples.shape[1]], -1, 1)) * 30.0)
            outer = np.sin(theta)
            center = np.sin(np.pi / 6.0 - theta)
            norm = np.hypot(outer, center)
            gains = np.zeros_like(samples)
            gains[0] = outer / norm * (ramp[:samples.shape[1]] < 0)
            gains[1] = outer / norm * (ramp[:samples.shape[1]] > 0)
            gains[2] = center / norm
            return gains * samples[2]
        return gains.astype(samples.dtype)[:, None] * samples[2]

    if ramp is None:
        left, right = pan_gains_for_channels(pan, source_channels)
    else:
        values = np.clip(ramp[:samples.shape[1]], -1.0, 1.0)
        if source_channels == 1:
            theta = (values + 1.0) * (np.pi / 4.0)
            left, right = np.cos(theta), np.sin(theta)
        else:
            left, right = 1.0 - np.maximum(values, 0), 1.0 + np.minimum(values, 0)
    output = samples.copy()
    output[0] *= left
    if channels > 1:
        output[1] *= right
    return output


def adapt_clip_pcm(samples: np.ndarray, target: ChannelLayout,
                   source: ChannelLayout | None, *, native: bool,
                   context: str, codec_name: str = "") -> np.ndarray:
    if native or target.channel_count in (6, 8):
        return route_pcm(samples, target, source, codec_name=codec_name)
    from src.features.edit.core.channel_layout import adapt_samples_for_delivery
    return adapt_samples_for_delivery(samples, target.channel_count, source, context=context)


def validate_native_layouts(sequence: Any, audio_tracks: tuple | None = None, *,
                            edited_bus: str | None = None) -> None:
    """Reject unsupported effects before an explicit native graph is published."""
    from src.features.edit.core.audio_edit import requires_pre_render
    from src.features.edit.core.audio_retime import clip_fx_entries

    master = str(getattr(sequence, "master_audio_layout", ""))
    if master:
        layout = mix_layout(master)
        chain = getattr(sequence, "master_effect_chain", None)
        _validate_effect_layout(layout, chain.to_dsp_entries() if chain is not None else (), "Master")
    tracks = getattr(sequence, "audio_tracks", ()) if audio_tracks is None else audio_tracks
    for track in tracks:
        if getattr(track, "is_bus", False):
            name = getattr(track, "bus_layout", "")
            if not master and name != "mono" and track.track_id != edited_bus:
                continue
        else:
            name = getattr(track, "audio_layout", "")
        if not name:
            continue
        layout = mix_layout(name)
        if layout is ChannelLayout.STEREO:
            continue
        owner = track.name or track.track_id
        chain = getattr(track, "effect_chain", None)
        _validate_effect_layout(layout, chain.to_dsp_entries() if chain is not None else (), owner)
        if getattr(track, "panner", {}).get("enabled", False):
            raise ValueError(f"{owner}: Stereo Spatial Panner cannot process {name}")
        for clip in track.clips:
            if requires_pre_render(clip.audio_edit):
                raise ValueError(f"{owner}/{clip.clip_id}: the clip pre-render cache is stereo-only")
            _validate_effect_layout(layout, clip_fx_entries(clip.effects, None) or (),
                                    f"{owner}/{clip.clip_id}")


def validate_rack_edit(sequence: Any, track_id: str | None, chain: Any) -> None:
    """Guard preview and history edits before they publish an incompatible chain."""
    if track_id is None:
        name = getattr(sequence, "master_audio_layout", "")
        owner = "Master"
    else:
        track = sequence.get_track_by_id(track_id)
        if track is None:
            return
        name = track.bus_layout if track.is_bus else track.audio_layout
        owner = track.name or track_id
    if name and chain is not None:
        _validate_effect_layout(mix_layout(name), chain.to_dsp_entries(), owner)


def _validate_effect_layout(layout: ChannelLayout, entries: list | tuple, owner: str) -> None:
    if layout is ChannelLayout.STEREO:
        return
    effect = incompatible_effect(entries)
    if effect is not None:
        raise ValueError(f"{owner}: {effect} cannot process {layout.av_layout_name}")
