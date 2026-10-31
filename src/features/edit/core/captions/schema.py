"""SUB-P2-10: Caption schema versioning, migration registry, unknown-field
preservation.

Phase 2A introduces a *separate* caption schema version (``caption_schema_version``)
rather than bumping the project-level ``SERIALIZATION_VERSION``. Per the plan's
§8 ("不要把 P0 修复与 P2 功能大重构绑在同一 PR"), the caption layer evolves
independently: a v1 caption block (flat fields, no metadata) and a v2 block
(layered CaptionStyle/CaptionTrackMetadata/CaptionCueMetadata) coexist, and
``to_dict`` **dual-writes** so old readers keep working.

This module owns:

* :data:`CAPTION_SCHEMA_VERSION` — the current caption schema version (``2``).
* :data:`CAPTION_SCHEMA_VERSION_KEY` — the dict key under which the version
  is stored (``"caption_schema_version"``).
* :func:`migrate_subtitle_clip_dict` — upgrade a serialized subtitle-clip dict
  to the current version, in-place + return. v0/v1 (no version key) → v2.
* :func:`migrate_track_caption_dict` — same for an ``EditTrack`` dict, moving
  flat style fields into a ``caption_metadata.default_style`` block.
* :func:`upgrade_captions_in_project` — walk a whole project dict and migrate
  every subtitle track + clip. Called from the main serializer's load path so
  caption migration piggybacks on project load without touching project version.
* Unknown-field preservation is handled by the model types'
  ``from_dict``/``to_dict`` (see ``CaptionCueMetadata.extra``); this module
  ensures the migration path never *drops* keys it doesn't recognize.

Migration rules (v1 → v2):
  * The 34 flat style fields stay where they are (legacy readers need them).
  * If the clip has *no* ``caption_metadata``, none is synthesized (a v1 clip
    loaded into v2 still works, it just has empty metadata — migration is
    lossless and lazy).
  * If ``caption_metadata`` is already present (forward-compat: a newer binary
    saved a v2+ block into a v1 file), it is left untouched and the version
    bumped.
"""

from __future__ import annotations

from typing import Any, Mapping


# ──────────────────────────────────────────────────────────────────────────
# Version constants
# ──────────────────────────────────────────────────────────────────────────

#: Current caption schema version. Bump when the caption block's *shape*
#: changes in a way old code can't ignore. Style/metadata additions that old
#: code simply doesn't read do NOT require a bump (they're forward-compatible).
CAPTION_SCHEMA_VERSION: int = 2

#: Dict key for the caption schema version on serialized subtitle blocks.
CAPTION_SCHEMA_VERSION_KEY: str = "caption_schema_version"

#: Lowest version we can migrate from. v0 = no version key at all (legacy).
MIN_MIGRATABLE_CAPTION_VERSION: int = 0


# ──────────────────────────────────────────────────────────────────────────
# Migration registry
# ──────────────────────────────────────────────────────────────────────────

def _clip_v0_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """v0/v1 (no version) → v2.

    A legacy clip has only flat fields. We do NOT synthesize a
    ``caption_metadata`` block here: the clip is perfectly usable in v2 with
    empty metadata (``CaptionCueMetadata()``), and synthesizing one would make
    the migration non-idempotent (re-migrating would see the synthesized block
    and treat it as user data). Instead we just stamp the version.

    The 34 flat style fields are left in place for legacy readers and as the
    source of truth when ``caption_metadata``/``style_override`` is absent.
    """
    data[CAPTION_SCHEMA_VERSION_KEY] = 2
    return data


def _clip_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """v1 → v2. v1 is identical in shape to v0 for our purposes (flat fields,
    optional explicit version=1). Just stamp v2."""
    data[CAPTION_SCHEMA_VERSION_KEY] = 2
    return data


# Ordered migration table: index = from_version, value = (to_version, function).
# Migrating from version N runs _CLIP_MIGRATIONS[N], then _CLIP_MIGRATIONS[N+1],
# etc. until reaching CAPTION_SCHEMA_VERSION.
_CLIP_MIGRATIONS: list[tuple[int, Any]] = [
    (2, _clip_v0_to_v2),   # index 0: v0 → v2
    None,                  # index 1: v1 → v2 placeholder (filled below)
    (2, _clip_v1_to_v2),   # index 2: v2 is current (no-op marker)
]
# v1 → v2 uses the same logic as v0 → v2 (stamp version).
_CLIP_MIGRATIONS[1] = (2, _clip_v1_to_v2)


def migrate_subtitle_clip_dict(
    data: dict[str, Any],
    from_version: int | None = None,
) -> dict[str, Any]:
    """Upgrade a serialized subtitle-clip dict to :data:`CAPTION_SCHEMA_VERSION`.

    Args:
        data: the clip dict (mutated in place and returned).
        from_version: explicit source version; if ``None``, read from
            ``data[CAPTION_SCHEMA_VERSION_KEY]`` (defaulting to ``0`` when the
            key is absent — i.e. a legacy v0/v1 clip).

    Returns:
        The same dict, migrated to the current caption schema version with
        ``caption_schema_version`` stamped.

    Raises:
        ValueError: if ``from_version`` is ahead of the current version (a
            newer binary wrote a block we don't understand). The caller should
            surface this to the user rather than silently downgrading.
    """
    if from_version is None:
        from_version = int(data.get(CAPTION_SCHEMA_VERSION_KEY, 0) or 0)

    if from_version > CAPTION_SCHEMA_VERSION:
        raise ValueError(
            f"Caption schema version {from_version} is ahead of supported "
            f"{CAPTION_SCHEMA_VERSION}; project saved by a newer build."
        )
    if from_version == CAPTION_SCHEMA_VERSION:
        # Already current; ensure the stamp is present for round-trip stability.
        data.setdefault(CAPTION_SCHEMA_VERSION_KEY, CAPTION_SCHEMA_VERSION)
        return data

    # Walk the migration chain from from_version up to current.
    version = from_version
    while version < CAPTION_SCHEMA_VERSION:
        entry = (_CLIP_MIGRATIONS[version]
                 if version < len(_CLIP_MIGRATIONS) else None)
        if entry is None:
            # No migration registered for this step; stamp and stop to avoid
            # an infinite loop. This shouldn't happen if the table is complete.
            data[CAPTION_SCHEMA_VERSION_KEY] = CAPTION_SCHEMA_VERSION
            break
        target_version, fn = entry
        data = fn(data)
        version = target_version
    data[CAPTION_SCHEMA_VERSION_KEY] = CAPTION_SCHEMA_VERSION
    return data


# ──────────────────────────────────────────────────────────────────────────
# Track-level caption migration
# ──────────────────────────────────────────────────────────────────────────

# Flat style field names that belong on a clip (the 34 from model.STYLE_FIELDS).
# Imported lazily to avoid a hard dep cycle (model imports nothing from here).
def _style_field_names() -> tuple[str, ...]:
    from src.features.edit.core.captions.model import STYLE_FIELDS
    return STYLE_FIELDS


def migrate_track_caption_dict(track_data: dict[str, Any]) -> dict[str, Any]:
    """Upgrade an ``EditTrack`` dict's caption layer to current schema.

    Migrates every clip in ``track_data["subtitle_clips"]`` and stamps a
    track-level ``caption_schema_version`` if the track carries a
    ``caption_metadata`` block. Does not synthesize metadata for legacy tracks
    (lossless + lazy, same rationale as the clip migration).

    Args:
        track_data: the serialized EditTrack dict (mutated in place + returned).

    Returns:
        The same dict with all subtitle clips migrated.
    """
    subs = track_data.get("subtitle_clips")
    if isinstance(subs, list):
        track_data["subtitle_clips"] = [
            migrate_subtitle_clip_dict(dict(c)) if isinstance(c, dict) else c
            for c in subs
        ]
    # Stamp track-level version if it already has caption metadata (forward-compat).
    if "caption_metadata" in track_data:
        track_data.setdefault(CAPTION_SCHEMA_VERSION_KEY, CAPTION_SCHEMA_VERSION)
    return track_data


# ──────────────────────────────────────────────────────────────────────────
# Project-level walker
# ──────────────────────────────────────────────────────────────────────────

def upgrade_captions_in_project(project_data: dict[str, Any]) -> dict[str, Any]:
    """Walk a whole serialized project and migrate every subtitle track/clip.

    Called from the main serializer's load path *after* the project-level
    migration chain runs, so caption migration is decoupled from the project
    schema version (SUB-P2-10 + plan §8). Idempotent: re-running on an already-
    current project is a no-op.

    Handles the real on-disk layout: the top-level wrapper holds
    ``timeline_sequences: {seq_id: seq_dict}`` (and a legacy ``timelines`` list
    fallback), and each ``seq_dict`` splits tracks into ``video_tracks`` /
    ``audio_tracks`` / ``subtitle_tracks`` (with ``tracks`` as an older unified
    fallback). We walk every track list and migrate any track that carries
    subtitle clips or is typed ``subtitle``.

    Args:
        project_data: the full project wrapper dict (mutated in place + returned).

    Returns:
        The same dict with all caption blocks at current schema version.
    """
    # Modern layout: timeline_sequences is {seq_id: seq_dict}.
    seqs = project_data.get("timeline_sequences")
    if isinstance(seqs, dict):
        for seq_id, seq_data in seqs.items():
            if isinstance(seq_data, dict):
                _migrate_sequence_tracks(seq_data)
    # Legacy/fallback: timelines as a list of seq dicts.
    timelines = project_data.get("timelines")
    if isinstance(timelines, list):
        for tl in timelines:
            if isinstance(tl, dict):
                _migrate_sequence_tracks(tl)
    return project_data


def _migrate_sequence_tracks(seq_data: dict[str, Any]) -> None:
    """Migrate every track list inside one TimelineSequence dict.

    ``TimelineSequence.to_dict`` emits ``video_tracks`` / ``audio_tracks`` /
    ``subtitle_tracks`` as separate lists; older layouts used a unified
    ``tracks`` list. We scan all four to be safe, but only touch tracks that
    actually carry subtitles (typed ``subtitle`` or with non-empty
    ``subtitle_clips``).
    """
    for key in ("subtitle_tracks", "video_tracks", "audio_tracks", "tracks"):
        tracks = seq_data.get(key)
        if not isinstance(tracks, list):
            continue
        for tr in tracks:
            if not isinstance(tr, dict):
                continue
            if tr.get("track_type") == "subtitle" or tr.get("subtitle_clips"):
                migrate_track_caption_dict(tr)


# ──────────────────────────────────────────────────────────────────────────
# Unknown-field preservation helper
# ──────────────────────────────────────────────────────────────────────────

def preserve_unknown_keys(
    original: Mapping[str, Any],
    known_keys: frozenset,
    target: dict[str, Any],
) -> dict[str, Any]:
    """Copy any keys from ``original`` not in ``known_keys`` into ``target``.

    Used by model ``to_dict`` implementations to round-trip fields a future
    schema version adds that this build doesn't know about (SUB-P2-10).
    Mutates and returns ``target``.
    """
    for k, v in original.items():
        if k not in known_keys and k not in target:
            target[k] = v
    return target
