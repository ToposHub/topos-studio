"""Structured, versioned Motion Graphic brief (MG-01).

The brief is the editable, auditable source of user intent for a Motion Graphic
Clip.  Before this module existed the request lived as a free-form string plus
a few dict fields on ``module.py``; there was no place to record *what the user
actually asked for* (audience, platform, brand, forbidden content, budget) and
no way to detect that a proposal was generated against an older brief.

Goals (MG-01):

1. One request forms a visible :class:`MotionGraphicBrief`; missing critical
   selections (duration, canvas, insertion point) are reported through
   :meth:`missing_required_fields` so the Assistant can *ask* instead of guess.
2. Every user edit increments ``revision`` and produces a :class:`BriefPatch`
   with the changed fields plus a short reason.  Patches are append-only so the
   lineage of a proposal is reconstructable.
3. Briefs hash deterministically (:meth:`MotionGraphicBrief.brief_hash`) so an
   audit event can say "generated against brief hash H" without persisting the
   raw prompt.
4. The model is Qt-free and import-cheap; ``module.py`` / planners / validators
   all share the same typed object instead of ad-hoc dicts.

The brief intentionally does **not** carry a scene or LLM output — those are
produced from the brief and live in the proposal / plan objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import uuid
from typing import Any, Mapping


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #
#
# Canvas / platform / language are closed enums so the validator and the
# delivery profile can stay deterministic.  New values must be added here
# explicitly — the LLM is never allowed to invent a platform string.

#: Canonical aspect ratios.  MG-11 will let the same brief produce several of
#: these; for B0 we only need the enum so the brief cannot hold "kinda 16:9".
CANVAS_16_9 = "16:9"
CANVAS_9_16 = "9:16"
CANVAS_1_1 = "1:1"
CANVAS_4_3 = "4:3"
CANVAS_21_9 = "21:9"
SUPPORTED_CANVAS_ASPECTS: frozenset[str] = frozenset(
    {CANVAS_16_9, CANVAS_9_16, CANVAS_1_1, CANVAS_4_3, CANVAS_21_9}
)

#: Default pixel sizes per aspect; MG-11 will make this a delivery profile, but
#: the brief already needs a sane default so it does not hardcode 1920x1080.
DEFAULT_CANVAS_SIZES: dict[str, tuple[int, int]] = {
    CANVAS_16_9: (1920, 1080),
    CANVAS_9_16: (1080, 1920),
    CANVAS_1_1: (1080, 1080),
    CANVAS_4_3: (1440, 1080),
    CANVAS_21_9: (2560, 1080),
}

PLATFORM_GENERIC = "generic"
PLATFORM_YOUTUBE = "youtube"
PLATFORM_SHORTS = "shorts"  # vertical short-form (YouTube Shorts / TikTok style)
PLATFORM_TIKTOK = "tiktok"
PLATFORM_INSTAGRAM = "instagram"
PLATFORM_WEB = "web"
PLATFORM_PRESENTATION = "presentation"
SUPPORTED_PLATFORMS: frozenset[str] = frozenset(
    {
        PLATFORM_GENERIC, PLATFORM_YOUTUBE, PLATFORM_SHORTS, PLATFORM_TIKTOK,
        PLATFORM_INSTAGRAM, PLATFORM_WEB, PLATFORM_PRESENTATION,
    }
)

LANGUAGE_ZH = "zh"
LANGUAGE_EN = "en"
LANGUAGE_JA = "ja"
SUPPORTED_LANGUAGES: frozenset[str] = frozenset({LANGUAGE_ZH, LANGUAGE_EN, LANGUAGE_JA})

#: Fields the user must supply before a proposal can be generated.  The
#: Assistant is expected to ask for whichever of these are missing instead of
#: silently filling them from a template (MG-01 acceptance: "缺少关键选择时只
#: 提问，不擅自猜测").
REQUIRED_BRIEF_FIELDS: tuple[str, ...] = ("topic", "duration_seconds", "canvas_aspect")


# --------------------------------------------------------------------------- #
# Brief patch
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BriefPatch:
    """An immutable, append-only change applied to a brief (MG-01 / MG-08).

    ``changes`` stores only the fields that were actually modified in this
    edit, keyed by brief attribute name.  ``reason`` is the short user-supplied
    or system-inferred justification (e.g. "节奏更快").  Patches are linked
    through ``from_revision`` → ``to_revision`` so the revision graph is
    reconstructable even if intermediate briefs are evicted from memory.
    """

    patch_id: str
    from_revision: int
    to_revision: int
    changes: Mapping[str, Any]
    reason: str = ""
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch_id": self.patch_id,
            "from_revision": self.from_revision,
            "to_revision": self.to_revision,
            "changes": _copy_json(dict(self.changes)),
            "reason": self.reason,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BriefPatch":
        return cls(
            patch_id=str(data.get("patch_id", "")),
            from_revision=int(data.get("from_revision", 0)),
            to_revision=int(data.get("to_revision", 0)),
            changes=_copy_json(data.get("changes", {})),
            reason=str(data.get("reason", "")),
            created_at=float(data.get("created_at", 0.0)),
        )


# --------------------------------------------------------------------------- #
# Brief
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MotionGraphicBrief:
    """The editable, versioned intent for a Motion Graphic Clip.

    Attributes:
        brief_id: Stable identifier shared across revisions.
        revision: Monotonically increasing; starts at 1, bumps on every patch.
        topic: One-line user goal (e.g. "介绍 AI Agent").
        goal: Longer narrative goal; optional but recommended.
        audience: Target audience description; optional.
        duration_seconds: Requested clip length.  Required for proposal generation.
        canvas_aspect: One of :data:`SUPPORTED_CANVAS_ASPECTS`.
        width / height: Pixel dimensions; default from aspect but overridable.
        fps: Frames per second; defaults to 24 to match the scene model.
        platform: Delivery platform; influences safe area / codec later.
        language: Primary language for generated copy.
        brand: Optional brand tokens (colors, fonts, logo policy) mapping.
        style: Free-form style tokens (mood, references); kept as a mapping so
            the planner can interpret them without a rigid schema here.
        call_to_action: Optional CTA text or reference.
        asset_scope: Asset ids the user explicitly allowed into this clip.
        audio_scope: Optional audio intent (narration text, music mood, sfx).
            Kept loose for B0; MG-15 will tighten it.
        budget_units: Cost ceiling the user accepted for paid generation.
        forbidden: Explicitly disallowed content (words, colors, assets).
        constraints: Extra planner hints (max elements, reading speed, etc.).
        patches: Append-only patch history producing this revision.
        source_user_text_hash: Hash of the original user prompt — used to
            detect "user typed a new request" vs "refining the same request".
            The raw prompt itself is never stored.
    """

    brief_id: str
    revision: int = 1
    topic: str = ""
    goal: str = ""
    audience: str = ""
    duration_seconds: float | None = None
    canvas_aspect: str = CANVAS_16_9
    width: int | None = None
    height: int | None = None
    fps: float = 24.0
    platform: str = PLATFORM_GENERIC
    language: str = LANGUAGE_ZH
    brand: Mapping[str, Any] = field(default_factory=dict)
    style: Mapping[str, Any] = field(default_factory=dict)
    call_to_action: str | None = None
    asset_scope: tuple[str, ...] = ()
    audio_scope: Mapping[str, Any] = field(default_factory=dict)
    budget_units: float | None = None
    forbidden: Mapping[str, Any] = field(default_factory=dict)
    constraints: Mapping[str, Any] = field(default_factory=dict)
    patches: tuple[BriefPatch, ...] = ()
    source_user_text_hash: str | None = None

    def __post_init__(self) -> None:
        if not self.brief_id:
            raise ValueError("MotionGraphicBrief.brief_id is required")
        if self.revision < 1:
            raise ValueError("MotionGraphicBrief.revision must be >= 1")
        if self.canvas_aspect not in SUPPORTED_CANVAS_ASPECTS:
            raise ValueError(
                f"Unsupported canvas aspect: {self.canvas_aspect!r}; "
                f"expected one of {sorted(SUPPORTED_CANVAS_ASPECTS)}"
            )
        if self.platform not in SUPPORTED_PLATFORMS:
            raise ValueError(
                f"Unsupported platform: {self.platform!r}; "
                f"expected one of {sorted(SUPPORTED_PLATFORMS)}"
            )
        if self.language not in SUPPORTED_LANGUAGES:
            raise ValueError(
                f"Unsupported language: {self.language!r}; "
                f"expected one of {sorted(SUPPORTED_LANGUAGES)}"
            )
        if self.fps <= 0:
            raise ValueError("MotionGraphicBrief.fps must be positive")
        if self.duration_seconds is not None and self.duration_seconds <= 0:
            raise ValueError("MotionGraphicBrief.duration_seconds must be positive")
        if self.budget_units is not None and self.budget_units < 0:
            raise ValueError("MotionGraphicBrief.budget_units cannot be negative")
        if self.width is not None and self.width <= 0:
            raise ValueError("MotionGraphicBrief.width must be positive")
        if self.height is not None and self.height <= 0:
            raise ValueError("MotionGraphicBrief.height must be positive")

    # ---------------- derived geometry ---------------- #

    def canvas_size(self) -> tuple[int, int]:
        """Return ``(width, height)`` falling back to the aspect default."""
        default_w, default_h = DEFAULT_CANVAS_SIZES.get(self.canvas_aspect, (1920, 1080))
        return (int(self.width or default_w), int(self.height or default_h))

    def duration_frames(self) -> int:
        """Duration expressed in frames at the brief's fps (>= 1 frame)."""
        if self.duration_seconds is None:
            return 1
        return max(1, int(round(self.duration_seconds * self.fps)))

    # ---------------- validation helpers ---------------- #

    def missing_required_fields(self) -> tuple[str, ...]:
        """Return the subset of :data:`REQUIRED_BRIEF_FIELDS` still unset."""
        missing: list[str] = []
        if not self.topic.strip():
            missing.append("topic")
        if self.duration_seconds is None:
            missing.append("duration_seconds")
        if not self.canvas_aspect:
            missing.append("canvas_aspect")
        return tuple(missing)

    def is_complete(self) -> bool:
        return not self.missing_required_fields()

    # ---------------- hashing ---------------- #

    def to_dict(self) -> dict[str, Any]:
        return {
            "brief_id": self.brief_id,
            "revision": self.revision,
            "topic": self.topic,
            "goal": self.goal,
            "audience": self.audience,
            "duration_seconds": self.duration_seconds,
            "canvas_aspect": self.canvas_aspect,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "platform": self.platform,
            "language": self.language,
            "brand": _copy_json(dict(self.brand)),
            "style": _copy_json(dict(self.style)),
            "call_to_action": self.call_to_action,
            "asset_scope": list(self.asset_scope),
            "audio_scope": _copy_json(dict(self.audio_scope)),
            "budget_units": self.budget_units,
            "forbidden": _copy_json(dict(self.forbidden)),
            "constraints": _copy_json(dict(self.constraints)),
            "patches": [patch.to_dict() for patch in self.patches],
            "source_user_text_hash": self.source_user_text_hash,
        }

    def brief_hash(self) -> str:
        """Deterministic sha256 of the brief's semantic content.

        The hash deliberately excludes ``patches`` (full history) and the
        ``source_user_text_hash`` so that two briefs with identical fields but
        different edit lineage compare equal — the proposal "was generated
        against this brief shape" regardless of how the user got there.
        """

        payload = {
            "brief_id": self.brief_id,
            "revision": self.revision,
            "topic": self.topic,
            "goal": self.goal,
            "audience": self.audience,
            "duration_seconds": self.duration_seconds,
            "canvas_aspect": self.canvas_aspect,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "platform": self.platform,
            "language": self.language,
            "brand": _copy_json(dict(self.brand)),
            "style": _copy_json(dict(self.style)),
            "call_to_action": self.call_to_action,
            "asset_scope": list(self.asset_scope),
            "audio_scope": _copy_json(dict(self.audio_scope)),
            "budget_units": self.budget_units,
            "forbidden": _copy_json(dict(self.forbidden)),
            "constraints": _copy_json(dict(self.constraints)),
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MotionGraphicBrief":
        patches_data = data.get("patches", [])
        patches = tuple(
            BriefPatch.from_dict(item)
            for item in patches_data
            if isinstance(item, Mapping)
        )
        # Numeric fields are normalized through typed coercion (AUD-26700) so a
        # polluted/persisted value like "duration_seconds": "30" fails with a
        # readable ValueError instead of a TypeError deep inside __post_init__.
        fps = _to_float_or_none(data.get("fps"), "fps")
        return cls(
            brief_id=str(data.get("brief_id", "")),
            revision=int(data.get("revision", 1)),
            topic=str(data.get("topic", "")),
            goal=str(data.get("goal", "")),
            audience=str(data.get("audience", "")),
            duration_seconds=_to_float_or_none(
                data.get("duration_seconds"), "duration_seconds"),
            canvas_aspect=str(data.get("canvas_aspect", CANVAS_16_9)),
            width=_to_int_or_none(data.get("width"), "width"),
            height=_to_int_or_none(data.get("height"), "height"),
            fps=24.0 if fps is None else fps,
            platform=str(data.get("platform", PLATFORM_GENERIC)),
            language=str(data.get("language", LANGUAGE_ZH)),
            brand=_copy_json(data.get("brand", {})),
            style=_copy_json(data.get("style", {})),
            call_to_action=_to_str_or_none(data.get("call_to_action")),
            asset_scope=tuple(str(item) for item in data.get("asset_scope", [])),
            audio_scope=_copy_json(data.get("audio_scope", {})),
            budget_units=_to_float_or_none(data.get("budget_units"), "budget_units"),
            forbidden=_copy_json(data.get("forbidden", {})),
            constraints=_copy_json(data.get("constraints", {})),
            patches=patches,
            source_user_text_hash=_to_str_or_none(data.get("source_user_text_hash")),
        )

    # ---------------- revision / patching ---------------- #

    def apply_patch(
        self,
        changes: Mapping[str, Any],
        *,
        reason: str = "",
        created_at: float = 0.0,
    ) -> tuple["MotionGraphicBrief", BriefPatch]:
        """Return a new brief with ``changes`` applied plus the recorded patch.

        ``changes`` keys must be valid brief attribute names.  Unknown keys
        raise ``ValueError`` so a caller cannot smuggle in untyped state via a
        patch.  The returned brief has ``revision`` incremented by one and the
        new :class:`BriefPatch` appended to ``patches``.
        """

        if not changes:
            raise ValueError("BriefPatch must contain at least one change")
        valid_keys = {
            "topic", "goal", "audience", "duration_seconds", "canvas_aspect",
            "width", "height", "fps", "platform", "language", "brand", "style",
            "call_to_action", "asset_scope", "audio_scope", "budget_units",
            "forbidden", "constraints",
        }
        unknown = set(changes) - valid_keys
        if unknown:
            raise ValueError(f"Unknown brief fields in patch: {sorted(unknown)}")

        normalized = _normalize_patch_values(changes)
        merged = self._merge_changes(normalized)
        new_revision = self.revision + 1
        patch = BriefPatch(
            patch_id=f"brief-patch-{uuid.uuid4().hex}",
            from_revision=self.revision,
            to_revision=new_revision,
            changes=_copy_json(normalized),
            reason=reason,
            created_at=created_at,
        )
        new_brief = replace(merged, revision=new_revision, patches=self.patches + (patch,))
        return new_brief, patch

    def _merge_changes(self, changes: Mapping[str, Any]) -> "MotionGraphicBrief":
        """Apply validated changes by constructing a new frozen brief."""
        return replace(self, **changes)

    # ---------------- factory ---------------- #

    @classmethod
    def from_user_text(
        cls,
        text: str,
        *,
        brief_id: str | None = None,
        **defaults: Any,
    ) -> "MotionGraphicBrief":
        """Bootstrap a brief from a raw user prompt without storing the prompt.

        The original text is discarded after hashing, following the project
        ``.topos`` rule (no raw prompts persisted).  The Assistant is expected
        to refine the brief through :meth:`apply_patch` afterwards.
        """

        digest = hashlib.sha256((text or "").encode("utf-8")).hexdigest()
        return cls(
            brief_id=brief_id or f"mg-brief-{uuid.uuid4().hex}",
            topic=(text or "").strip()[:200],
            source_user_text_hash=digest,
            **defaults,
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


_ALLOWED_TYPES = (str, int, float, bool, list, tuple, dict, type(None))


def _to_float_or_none(value: Any, field_name: str) -> float | None:
    """Coerce an external value to ``float | None`` or raise a clean ValueError.

    Normalization for persisted/external data (AUD-26700): ``None`` stays
    ``None`` (optional field), numbers pass through, numeric strings are
    coerced; anything else raises ``ValueError`` naming the field instead of
    letting a ``TypeError`` escape from a comparison inside ``__post_init__``.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number, got bool")
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number, got {value!r}") from exc


def _to_int_or_none(value: Any, field_name: str) -> int | None:
    """Coerce an external value to ``int | None`` (see :func:`_to_float_or_none`)."""

    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer, got bool")
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer, got {value!r}") from exc


def _to_str_or_none(value: Any) -> str | None:
    """Coerce an external value to ``str | None`` (``None`` stays ``None``)."""

    return None if value is None else str(value)


def _copy_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _copy_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy_json(item) for item in value]
    return value


def _normalize_patch_values(changes: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize sequence/mapping/scalar values so the frozen brief accepts them.

    - ``asset_scope`` may be given as a list/tuple/set and is coerced to a tuple.
    - ``brand`` / ``style`` / ``audio_scope`` / ``forbidden`` / ``constraints``
      may be given as any mapping and are copied to plain dicts.
    - Numeric fields (``duration_seconds`` / ``width`` / ``height`` / ``fps`` /
      ``budget_units``) are coerced through the same typed helpers as
      :meth:`MotionGraphicBrief.from_dict`, so a patch like ``{"fps": "24"}``
      is normalized and an unparseable value raises a clean ``ValueError``
      naming the field (AUD-26700) instead of a ``TypeError`` at comparison.
    - Other scalars are passed through unchanged.
    """

    normalized: dict[str, Any] = {}
    for key, value in changes.items():
        if key == "asset_scope":
            if value is None:
                normalized[key] = ()
            elif isinstance(value, (list, tuple, set)):
                normalized[key] = tuple(str(item) for item in value if str(item).strip())
            else:
                raise ValueError("asset_scope must be a list/tuple/set of asset ids")
        elif key in {"brand", "style", "audio_scope", "forbidden", "constraints"}:
            if value is None:
                normalized[key] = {}
            elif isinstance(value, Mapping):
                normalized[key] = _copy_json(dict(value))
            else:
                raise ValueError(f"{key} must be a mapping")
        elif key == "duration_seconds" or key == "budget_units":
            normalized[key] = _to_float_or_none(value, key)
        elif key == "width" or key == "height":
            normalized[key] = _to_int_or_none(value, key)
        elif key == "fps":
            fps = _to_float_or_none(value, key)
            normalized[key] = 24.0 if fps is None else fps
        else:
            normalized[key] = value
    return normalized


__all__ = [
    "MotionGraphicBrief",
    "BriefPatch",
    "SUPPORTED_CANVAS_ASPECTS",
    "SUPPORTED_PLATFORMS",
    "SUPPORTED_LANGUAGES",
    "DEFAULT_CANVAS_SIZES",
    "REQUIRED_BRIEF_FIELDS",
    "CANVAS_16_9",
    "CANVAS_9_16",
    "CANVAS_1_1",
    "CANVAS_4_3",
    "CANVAS_21_9",
    "PLATFORM_GENERIC",
    "PLATFORM_YOUTUBE",
    "PLATFORM_SHORTS",
    "PLATFORM_TIKTOK",
    "PLATFORM_INSTAGRAM",
    "PLATFORM_WEB",
    "PLATFORM_PRESENTATION",
    "LANGUAGE_ZH",
    "LANGUAGE_EN",
    "LANGUAGE_JA",
]
