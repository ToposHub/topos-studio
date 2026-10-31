"""Audio transition catalogue — the single source of truth for audio crossfades.

Mirrors :mod:`transition_catalog` but lives apart so the video render plan
(:mod:`timeline_render_plan`, which only walks ``video_tracks``) and the video
Inspector never see ``audio:`` types.  Audio transitions are consumed by the
audio mixer/exporter only.

Naming convention: every ``transition_type`` is prefixed with ``audio:`` so a
single ``startswith`` check cleanly separates the two namespaces.  The part
after the colon is the ``curve`` used by :func:`audio_dsp.crossfade_envelopes`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AudioTransitionDefinition:
    """One audio crossfade flavour.

    Attributes:
        transition_type: Unique id, always ``audio:<curve>``. Stored on
            :class:`timeline_v2.TransitionInstance.transition_type`.
        label: Human label shown in the Effects panel / Inspector.
        curve: DSP curve key consumed by ``crossfade_envelopes``.
            One of ``constant_power | linear | equal_power | fade``.
    """

    transition_type: str
    label: str
    curve: str


# Cut-point model only — clips stay non-overlapping (the existing
# EditTrack overlap invariant is intentionally NOT relaxed).  Each flavour just
# picks a different pair of complementary gain curves over the transition span.
AUDIO_TRANSITION_DEFINITIONS: tuple[AudioTransitionDefinition, ...] = (
    AudioTransitionDefinition(
        "audio:constant_power", "Constant Power", "constant_power"
    ),
    AudioTransitionDefinition(
        "audio:linear", "Linear (Equal Gain)", "linear"
    ),
    AudioTransitionDefinition(
        "audio:equal_power", "Equal Power (3dB)", "equal_power"
    ),
    AudioTransitionDefinition(
        "audio:fade", "Fade (to Silence)", "fade"
    ),
)

AUDIO_TRANSITION_TYPE_OPTIONS: tuple[str, ...] = tuple(
    definition.transition_type for definition in AUDIO_TRANSITION_DEFINITIONS
)
AUDIO_TRANSITION_LABELS: tuple[str, ...] = tuple(
    definition.label for definition in AUDIO_TRANSITION_DEFINITIONS
)
AUDIO_TRANSITION_LABEL_MAP: dict[str, str] = {
    definition.transition_type: definition.label
    for definition in AUDIO_TRANSITION_DEFINITIONS
}
AUDIO_CURVE_MAP: dict[str, str] = {
    definition.transition_type: definition.curve
    for definition in AUDIO_TRANSITION_DEFINITIONS
}
# Reverse lookup: curve → canonical transition_type (first match wins).
AUDIO_CURVE_TO_TYPE: dict[str, str] = {
    definition.curve: definition.transition_type
    for definition in AUDIO_TRANSITION_DEFINITIONS
}


def is_audio_transition(transition_type: str) -> bool:
    """Return whether ``transition_type`` belongs to the audio namespace."""
    return transition_type in AUDIO_TRANSITION_TYPE_OPTIONS


def audio_curve_for(transition_type: str) -> str | None:
    """Return the DSP curve key for an audio transition type, else ``None``."""
    return AUDIO_CURVE_MAP.get(transition_type)
