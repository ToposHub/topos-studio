"""SUB-P2-01 / SUB-P2-06: Caption data model — style + track + cue metadata.

Phase 2A introduces a layered caption model **without** rewriting the existing
``SubtitleClip`` (which the Phase 0+1 contract, renderer and 28 commits depend
on). Per the plan's §8 "不要把 P0 修复与 P2 功能大重构绑在同一 PR", this
module is an **additive adaptation layer**:

* ``CaptionStyle`` — the 27 visual style fields, each ``Optional`` so a cue can
  express "inherit from track". A style with all fields ``None`` is an *empty
  override* (fully inherits its parent).
* ``CaptionTrackMetadata`` — per-subtitle-track language / role / default style
  / safe-area anchor. Stored on ``EditTrack`` and serves as the inheritance
  root for cues that do not override a given field.
* ``CaptionCueMetadata`` — per-cue speaker / status / assignee / note /
  source_span / revision / locked / provenance. Pure metadata: it never affects
  visible pixels, only QC / review / collaboration (SUB-P2-12).

The renderer never reads these types directly. :func:`resolve_style` flattens
``track_default`` ⊕ ``cue_override`` into the *same flat dict* the existing
``subtitle_renderer`` already consumes (``font_family``/``fill_r``/…), so the
render path is untouched. ``from_legacy_clip`` / ``style_to_legacy_fields``
bridge the new model to/from the existing ``SubtitleClip`` flat fields for
migration and dual-write serialization.

All three types are ``frozen=True`` to match the project's immutable-data
convention (SubtitleClip / EditTrack / commands). They round-trip through
``to_dict`` / ``from_dict`` and preserve unknown keys (SUB-P2-10) so a future
schema bump does not lose user data.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace as _dc_replace
from typing import Any, ClassVar, Mapping, Optional


# ──────────────────────────────────────────────────────────────────────────
# Enums-as-constants (match project style: EditTool, CaptionEditMode)
# ──────────────────────────────────────────────────────────────────────────

class CaptionRole:
    """Role of a subtitle track (SUB-P2-01).

    Using a constant class rather than ``enum.Enum`` to match the project's
    existing convention (``EditTool``, ``CaptionEditMode``) and to keep the
    values JSON-serializable as plain strings.
    """
    PRIMARY = "primary"        # 主字幕（对话/叙述）
    SECONDARY = "secondary"    # 副字幕（翻译/注释，通常不同语言）
    SDH = "sdh"                # 听障/聋人字幕（含音效描述）
    FORCED = "forced"          # 强制字幕（仅翻译不可译文本）
    COMMENTARY = "commentary"  # 评论/导演解说
    KARAOKE = "karaoke"        # 逐词/逐音节（SUB-P3 模板）


class CaptionStatus:
    """Review/QC status of a cue (SUB-P2-12).

    Values are plain strings for JSON serialization. ``LOCKED`` is a *review*
    lock (disables edits in workspace/QC), orthogonal to ``EditTrack.is_locked``
    (which locks the whole track geometry).
    """
    DRAFT = "draft"
    TRANSCRIBED = "transcribed"
    TRANSLATED = "translated"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    LOCKED = "locked"
    REJECTED = "rejected"


class CaptionSafeAnchor:
    """Where a cue's bounding box is anchored within the safe area (SUB-P2-06/08)."""
    CENTER = "center"
    BOTTOM_CENTER = "bottom_center"   # 典型广播字幕位
    TOP_CENTER = "top_center"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"


class CaptionWordAnimation:
    """逐词动画策略。

    ``highlight`` 是编辑器和交付格式的通用默认语义；``karaoke`` 会在
    ASS 导出中生成逐词高亮控制码；``none`` 只保留词级时间码而不动画。
    """

    NONE = "none"
    HIGHLIGHT = "highlight"
    KARAOKE = "karaoke"


_VALID_ROLES = frozenset({
    CaptionRole.PRIMARY, CaptionRole.SECONDARY, CaptionRole.SDH,
    CaptionRole.FORCED, CaptionRole.COMMENTARY, CaptionRole.KARAOKE,
})
_VALID_STATUSES = frozenset({
    CaptionStatus.DRAFT, CaptionStatus.TRANSCRIBED, CaptionStatus.TRANSLATED,
    CaptionStatus.REVIEWED, CaptionStatus.APPROVED, CaptionStatus.LOCKED,
    CaptionStatus.REJECTED,
})
_VALID_ANCHORS = frozenset({
    CaptionSafeAnchor.CENTER, CaptionSafeAnchor.BOTTOM_CENTER,
    CaptionSafeAnchor.TOP_CENTER, CaptionSafeAnchor.BOTTOM_LEFT,
    CaptionSafeAnchor.BOTTOM_RIGHT,
})
_VALID_WORD_ANIMATIONS = frozenset({
    CaptionWordAnimation.NONE,
    CaptionWordAnimation.HIGHLIGHT,
    CaptionWordAnimation.KARAOKE,
})
_VALID_HALIGNS = frozenset({"left", "center", "right"})
_VALID_VALIGNS = frozenset({"top", "middle", "bottom"})
_VALID_WEIGHTS = frozenset({"light", "normal", "demibold", "bold", "black"})


def _opt(value: Any, valid: Optional[frozenset] = None) -> Any:
    """Coerce a legacy/serialized value to ``Optional[...]``.

    Empty strings / None normalize to ``None`` (meaning "inherit" for styles,
    "unset" for metadata). If ``valid`` is given, an invalid non-empty value
    raises ``ValueError`` so bad data fails fast at the model boundary rather
    than silently rendering wrong.
    """
    if value is None:
        return None
    if isinstance(value, str) and value == "":
        return None
    if valid is not None and value not in valid:
        raise ValueError(f"Invalid caption value: {value!r} (allowed: {sorted(valid)})")
    return value


# ──────────────────────────────────────────────────────────────────────────
# CaptionStyle — visual + transform fields, each Optional (None = inherit)
# ──────────────────────────────────────────────────────────────────────────

# The canonical list of style field names. This is the contract for
# ``resolve_style`` output keys and the dual-write bridge to SubtitleClip.
STYLE_FIELDS: tuple[str, ...] = (
    # Font
    "font_family", "font_size", "font_weight", "italic",
    # Layout
    "halign", "valign", "letter_spacing", "line_spacing",
    # Transform / placement
    "translate_x", "translate_y", "scale_x", "scale_y", "rotation",
    "pivot_mode", "pivot_x", "pivot_y",
    # Fill (HDR float)
    "fill_r", "fill_g", "fill_b", "fill_a",
    # Outline
    "outline_enable", "outline_width",
    "outline_r", "outline_g", "outline_b", "outline_a",
    # Shadow
    "shadow_enable", "shadow_offset_x", "shadow_offset_y", "shadow_blur",
    "shadow_r", "shadow_g", "shadow_b", "shadow_a",
)
assert len(STYLE_FIELDS) == 34, f"STYLE_FIELDS must list 34 fields, got {len(STYLE_FIELDS)}"


@dataclass(frozen=True)
class CaptionStyle:
    """A caption visual style. Every field is ``Optional``: ``None`` means
    "inherit from the parent (track default)".

    An all-``None`` style is an *empty override* — the cue fully inherits its
    track's default. This is the value ``CaptionCueMetadata`` defaults to when
    no per-cue style is set.

    The fields mirror ``SubtitleClip``'s 34 visual style fields one-for-one so
    that :func:`style_to_legacy_fields` / :func:`from_legacy_clip` are lossless.
    Geometry/placement fields are included so global track styling can cover
    alignment and transform operations; timing/identity fields such as
    ``timeline_start`` and ``clip_id`` remain on ``SubtitleClip``.
    """

    # Font
    font_family: Optional[str] = None
    font_size: Optional[int] = None
    font_weight: Optional[str] = None
    italic: Optional[bool] = None
    # Layout
    halign: Optional[str] = None
    valign: Optional[str] = None
    letter_spacing: Optional[float] = None
    line_spacing: Optional[float] = None
    # Transform / placement
    translate_x: Optional[float] = None
    translate_y: Optional[float] = None
    scale_x: Optional[float] = None
    scale_y: Optional[float] = None
    rotation: Optional[float] = None
    pivot_mode: Optional[str] = None
    pivot_x: Optional[float] = None
    pivot_y: Optional[float] = None
    # Fill (HDR float)
    fill_r: Optional[float] = None
    fill_g: Optional[float] = None
    fill_b: Optional[float] = None
    fill_a: Optional[float] = None
    # Outline
    outline_enable: Optional[bool] = None
    outline_width: Optional[float] = None
    outline_r: Optional[float] = None
    outline_g: Optional[float] = None
    outline_b: Optional[float] = None
    outline_a: Optional[float] = None
    # Shadow
    shadow_enable: Optional[bool] = None
    shadow_offset_x: Optional[float] = None
    shadow_offset_y: Optional[float] = None
    shadow_blur: Optional[float] = None
    shadow_r: Optional[float] = None
    shadow_g: Optional[float] = None
    shadow_b: Optional[float] = None
    shadow_a: Optional[float] = None

    def __post_init__(self) -> None:
        # Validate enum-like fields only when explicitly set.
        _opt(self.font_weight, _VALID_WEIGHTS)
        _opt(self.halign, _VALID_HALIGNS)
        _opt(self.valign, _VALID_VALIGNS)
        _opt(self.pivot_mode, frozenset(("center", "custom")))
        if self.font_size is not None and self.font_size < 0:
            raise ValueError(f"font_size must be >= 0: {self.font_size}")

    # ── Queries ──

    def is_empty(self) -> bool:
        """True if every field is ``None`` (cue fully inherits track default)."""
        return all(getattr(self, f) is None for f in STYLE_FIELDS)

    def overridden_fields(self) -> tuple[str, ...]:
        """Names of fields this style explicitly sets (non-``None``)."""
        return tuple(f for f in STYLE_FIELDS if getattr(self, f) is not None)

    def merge_over(self, base: "CaptionStyle") -> "CaptionStyle":
        """Return a new style = ``self`` overrides on top of ``base``.

        For each field: take ``self``'s value if not ``None``, else ``base``'s.
        This is the per-cue inheritance primitive; :func:`resolve_style` uses it
        to fold track-default ⊕ cue-override.
        """
        if base is None:
            raise TypeError("merge_over base cannot be None")
        kwargs: dict[str, Any] = {}
        for f in STYLE_FIELDS:
            v = getattr(self, f)
            kwargs[f] = v if v is not None else getattr(base, f)
        return CaptionStyle(**kwargs)

    # ── Serialization ──

    def to_dict(self) -> dict[str, Any]:
        """Serialize. Only writes non-``None`` fields to keep payloads small;
        unknown keys are the caller's responsibility (see ``CaptionCueMetadata``
        for full unknown-key preservation)."""
        out: dict[str, Any] = {}
        for f in STYLE_FIELDS:
            v = getattr(self, f)
            if v is not None:
                out[f] = v
        return out

    @staticmethod
    def from_dict(data: Optional[Mapping[str, Any]]) -> "CaptionStyle":
        """Deserialize. Unknown keys are silently ignored at this layer; callers
        that need unknown-key preservation must capture them separately."""
        if not data:
            return CaptionStyle()
        kwargs: dict[str, Any] = {}
        for f in STYLE_FIELDS:
            if f in data:
                kwargs[f] = data[f]
        return CaptionStyle(**kwargs)

    def replace(self, **kwargs: Any) -> "CaptionStyle":
        """Immutable replace (mirrors SubtitleClip.with_params)."""
        return _dc_replace(self, **kwargs)


# Sentinel empty style — the "no override" value cues default to.
EMPTY_STYLE: CaptionStyle = CaptionStyle()


@dataclass(frozen=True)
class CaptionWordTiming:
    """One word/phrase timing in timeline-frame coordinates."""

    text: str
    start_frame: int
    end_frame: int
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if not str(self.text).strip():
            raise ValueError("CaptionWordTiming.text cannot be empty")
        if self.start_frame < 0 or self.end_frame <= self.start_frame:
            raise ValueError(
                "CaptionWordTiming requires 0 <= start_frame < end_frame"
            )
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("CaptionWordTiming.confidence must be in [0, 1]")

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "confidence": self.confidence,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "CaptionWordTiming":
        return CaptionWordTiming(
            text=str(data.get("text", "")),
            start_frame=int(data.get("start_frame", 0)),
            end_frame=int(data.get("end_frame", 0)),
            confidence=float(data.get("confidence", 1.0)),
        )


def active_caption_word(
    word_timings: tuple[CaptionWordTiming, ...] | list[CaptionWordTiming],
    frame: int,
) -> int | None:
    """Return the active word index for a half-open timeline frame."""
    for index, word in enumerate(word_timings):
        if word.start_frame <= int(frame) < word.end_frame:
            return index
    return None


# ──────────────────────────────────────────────────────────────────────────
# CaptionTrackMetadata — language / role / default style / safe-area anchor
# ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CaptionTrackMetadata:
    """Per-subtitle-track metadata (SUB-P2-01).

    Stored on ``EditTrack`` (via ``caption_metadata`` field added in Phase 2A).
    Serves as the **inheritance root** for cues: a cue's resolved style is
    ``cue.style.merge_over(track.caption_metadata.default_style)``.

    Attributes:
        language: BCP-47 / ISO-639 tag, e.g. ``"en"``, ``"zh-Hans"``, ``"ar"``.
            Empty string = unspecified (project default applies).
        role: One of :class:`CaptionRole`. Drives export selection (e.g. only
            ``PRIMARY`` + ``FORCED`` burned, ``SECONDARY`` as sidecar).
        default_style: The track-level default style every cue inherits unless
            it overrides a field. Changes here propagate to all non-overriding
            cues (SUB-P2-06 propagation requirement).
        safe_anchor: Where cues on this track anchor within the safe area.
        label: Human-readable track name shown in workspace/export UI.
    """

    language: str = ""
    role: str = CaptionRole.PRIMARY
    default_style: CaptionStyle = EMPTY_STYLE
    safe_anchor: str = CaptionSafeAnchor.BOTTOM_CENTER
    label: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.language, str):
            raise TypeError("language must be str")
        if self.role not in _VALID_ROLES:
            raise ValueError(f"Invalid caption role: {self.role!r}")
        if self.safe_anchor not in _VALID_ANCHORS:
            raise ValueError(f"Invalid safe_anchor: {self.safe_anchor!r}")
        if not isinstance(self.default_style, CaptionStyle):
            raise TypeError("default_style must be a CaptionStyle")

    def with_default_style(self, style: CaptionStyle) -> "CaptionTrackMetadata":
        return _dc_replace(self, default_style=style)

    def with_language(self, language: str) -> "CaptionTrackMetadata":
        return _dc_replace(self, language=language)

    def with_role(self, role: str) -> "CaptionTrackMetadata":
        if role not in _VALID_ROLES:
            raise ValueError(f"Invalid caption role: {role!r}")
        return _dc_replace(self, role=role)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "language": self.language,
            "role": self.role,
            "safe_anchor": self.safe_anchor,
            "label": self.label,
        }
        # Only write default_style when non-empty to keep old projects compact.
        if not self.default_style.is_empty():
            d["default_style"] = self.default_style.to_dict()
        return d

    @staticmethod
    def from_dict(data: Optional[Mapping[str, Any]]) -> "CaptionTrackMetadata":
        if not data:
            return CaptionTrackMetadata()
        style_data = data.get("default_style")
        return CaptionTrackMetadata(
            language=str(data.get("language", "") or ""),
            role=str(data.get("role", CaptionRole.PRIMARY) or CaptionRole.PRIMARY),
            default_style=CaptionStyle.from_dict(style_data),
            safe_anchor=str(data.get("safe_anchor", CaptionSafeAnchor.BOTTOM_CENTER)
                            or CaptionSafeAnchor.BOTTOM_CENTER),
            label=str(data.get("label", "") or ""),
        )


# ──────────────────────────────────────────────────────────────────────────
# CaptionCueMetadata — speaker / status / note / lock / provenance
# ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CaptionCueMetadata:
    """Per-cue metadata that does NOT affect visible pixels (SUB-P2-12).

    Stored on ``SubtitleClip`` (via ``caption_metadata`` field added in Phase
    2A). Drives QC review status, collaboration (assignee/note/locked),
    transcription provenance (source_span/revision) and import traceability.

    All fields default to "unset"; a cue with empty metadata behaves exactly
    like a legacy cue (no review status, no lock, no provenance).
    """

    speaker: str = ""                  # 说话人（diarization 结果或手填）
    status: str = CaptionStatus.DRAFT  # 审校状态
    assignee: str = ""                 # 当前负责人（协作）
    note: str = ""                     # 审校评论（不影响可见文本）
    source_span: Optional[tuple[float, float]] = None  # 原音频 [start,end] 秒
    revision: int = 0                  # 编辑版本号（每次提交 +1）
    locked: bool = False               # 审校锁（LOCKED 状态的快捷位）
    import_source: str = ""            # 来源文件（SRT/VTT/TTML 路径或转写 job id）
    word_timings: tuple[CaptionWordTiming, ...] = ()  # 逐词时间码（时间线帧）
    word_animation: str = CaptionWordAnimation.HIGHLIGHT
    # SUB-P2-10: unknown keys preserved verbatim so future schema fields survive
    # a round-trip through an older binary. Populated only by from_dict.
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError(f"Invalid caption status: {self.status!r}")
        if self.revision < 0:
            raise ValueError(f"revision must be >= 0: {self.revision}")
        if self.source_span is not None:
            if (not isinstance(self.source_span, tuple)
                    or len(self.source_span) != 2):
                raise TypeError("source_span must be a 2-tuple (start_sec, end_sec)")
            s, e = self.source_span
            if not (isinstance(s, (int, float)) and isinstance(e, (int, float))):
                raise TypeError("source_span elements must be numeric")
            if e < s:
                raise ValueError(f"source_span end < start: {self.source_span}")
        if self.word_animation not in _VALID_WORD_ANIMATIONS:
            raise ValueError(f"Invalid word_animation: {self.word_animation!r}")
        if not isinstance(self.word_timings, tuple):
            raise TypeError("word_timings must be a tuple")
        for word in self.word_timings:
            if not isinstance(word, CaptionWordTiming):
                raise TypeError("word_timings must contain CaptionWordTiming values")

    # ── Lock helpers ──

    def is_edit_locked(self) -> bool:
        """True if this cue must not be edited (review lock)."""
        return self.locked or self.status == CaptionStatus.LOCKED

    def with_status(self, status: str) -> "CaptionCueMetadata":
        if status not in _VALID_STATUSES:
            raise ValueError(f"Invalid caption status: {status!r}")
        return _dc_replace(self, status=status)

    def with_speaker(self, speaker: str) -> "CaptionCueMetadata":
        return _dc_replace(self, speaker=speaker)

    def with_note(self, note: str) -> "CaptionCueMetadata":
        return _dc_replace(self, note=note)

    def with_locked(self, locked: bool) -> "CaptionCueMetadata":
        return _dc_replace(self, locked=locked)

    def with_revision(self, revision: int) -> "CaptionCueMetadata":
        if revision < 0:
            raise ValueError(f"revision must be >= 0: {revision}")
        return _dc_replace(self, revision=revision)

    def with_word_timings(
        self,
        word_timings: tuple[CaptionWordTiming, ...] | list[CaptionWordTiming],
        *,
        animation: str | None = None,
    ) -> "CaptionCueMetadata":
        """Attach aligned words while keeping review/provenance metadata."""
        return _dc_replace(
            self,
            word_timings=tuple(word_timings),
            word_animation=self.word_animation if animation is None else animation,
        )

    def bumped_revision(self) -> "CaptionCueMetadata":
        """Return a copy with revision + 1 (call on each user edit)."""
        return _dc_replace(self, revision=self.revision + 1)

    # ── Serialization (with unknown-key preservation) ──

    # Known keys — everything else goes into ``extra``.
    _KNOWN_KEYS: ClassVar[frozenset] = frozenset({
        "speaker", "status", "assignee", "note", "source_span",
        "revision", "locked", "import_source",
        "word_timings", "word_animation",
    })

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "speaker": self.speaker,
            "status": self.status,
            "assignee": self.assignee,
            "note": self.note,
            "revision": self.revision,
            "locked": self.locked,
            "import_source": self.import_source,
            "word_timings": [word.to_dict() for word in self.word_timings],
            "word_animation": self.word_animation,
        }
        if self.source_span is not None:
            d["source_span"] = list(self.source_span)  # tuple → list for JSON
        # Unknown keys: round-trip verbatim (SUB-P2-10).
        if self.extra:
            for k, v in self.extra.items():
                if k not in self._KNOWN_KEYS:
                    d[k] = v
        return d

    @staticmethod
    def from_dict(data: Optional[Mapping[str, Any]]) -> "CaptionCueMetadata":
        if not data:
            return CaptionCueMetadata()
        # source_span: JSON list → tuple
        ss = data.get("source_span")
        if isinstance(ss, (list, tuple)) and len(ss) == 2:
            source_span: Optional[tuple[float, float]] = (float(ss[0]), float(ss[1]))
        elif ss is None:
            source_span = None
        else:
            source_span = None  # malformed → drop, don't crash
        # Capture unknown keys for verbatim preservation.
        extra: dict[str, Any] = {}
        known = CaptionCueMetadata._KNOWN_KEYS
        for k, v in data.items():
            if k not in known:
                extra[k] = v
        return CaptionCueMetadata(
            speaker=str(data.get("speaker", "") or ""),
            status=str(data.get("status", CaptionStatus.DRAFT) or CaptionStatus.DRAFT),
            assignee=str(data.get("assignee", "") or ""),
            note=str(data.get("note", "") or ""),
            source_span=source_span,
            revision=int(data.get("revision", 0) or 0),
            locked=bool(data.get("locked", False)),
            import_source=str(data.get("import_source", "") or ""),
            word_timings=tuple(
                CaptionWordTiming.from_dict(item)
                for item in (data.get("word_timings") or ())
                if isinstance(item, Mapping)
            ),
            word_animation=str(
                data.get("word_animation", CaptionWordAnimation.HIGHLIGHT)
                or CaptionWordAnimation.HIGHLIGHT
            ),
            extra=extra,
        )

    def replace(self, **kwargs: Any) -> "CaptionCueMetadata":
        return _dc_replace(self, **kwargs)


# ──────────────────────────────────────────────────────────────────────────
# resolve_style — the flatten primitive the renderer consumes
# ──────────────────────────────────────────────────────────────────────────

# Hard defaults matching SubtitleClip's dataclass defaults. Used only when
# neither cue override nor track default supplies a field (i.e. a brand-new
# track with empty default_style). Kept in sync with SubtitleClip defaults.
_LEGACY_DEFAULTS: dict[str, Any] = {
    "font_family": "Arial",
    "font_size": 0,  # 0 = 自适应（按画布高度比例）
    "font_weight": "normal",
    "italic": False,
    "halign": "center",
    "valign": "bottom",
    "letter_spacing": 0.0,
    "line_spacing": 1.0,
    "translate_x": 0.0, "translate_y": 0.0,
    "scale_x": 1.0, "scale_y": 1.0,
    "rotation": 0.0,
    "pivot_mode": "center", "pivot_x": 0.0, "pivot_y": 0.0,
    "fill_r": 1.0, "fill_g": 1.0, "fill_b": 1.0, "fill_a": 1.0,
    "outline_enable": False,
    "outline_width": 4.0,
    "outline_r": 0.0, "outline_g": 0.0, "outline_b": 0.0, "outline_a": 1.0,
    "shadow_enable": False,
    "shadow_offset_x": 8.0, "shadow_offset_y": 8.0, "shadow_blur": 8.0,
    "shadow_r": 0.0, "shadow_g": 0.0, "shadow_b": 0.0, "shadow_a": 1.0,
}
assert set(_LEGACY_DEFAULTS) == set(STYLE_FIELDS)


def resolve_style(
    track_default: CaptionStyle,
    cue_override: Optional[CaptionStyle] = None,
    base: Optional[dict] = None,
) -> dict[str, Any]:
    """Flatten track-default ⊕ cue-override into a legacy-compatible dict.

    Resolution order (most-specific wins):
        1. ``cue_override`` field if not ``None``
        2. ``track_default`` field if not ``None``
        3. ``base[f]`` (the cue's legacy flat fields) when ``base`` is given
        4. :data:`_LEGACY_DEFAULTS` (matches ``SubtitleClip`` dataclass defaults)

    The returned dict uses the **same flat keys** (``font_family``, ``fill_r``,
    …) the existing ``subtitle_renderer`` already reads, so the renderer is
    untouched. This is the single function that materializes inheritance.

    A cue with ``cue_override=None`` or ``EMPTY_STYLE`` fully inherits the
    track default — so changing ``track_default`` propagates to every
    non-overriding cue (SUB-P2-06 requirement).

    ``base`` (the cue's existing flat-field dict, e.g. its ``font_family``) is
    used as the layer between track-default and the hard-coded legacy defaults.
    This matters because legacy-edited cues store their style in the flat fields
    with ``style_override=None``; without ``base`` they would render with the
    hard-coded defaults instead of their actual flat values. Callers that have a
    clip in hand should pass ``base`` = the clip's flat style fields.
    """
    if cue_override is None:
        cue_override = EMPTY_STYLE
    merged = cue_override.merge_over(track_default)
    out: dict[str, Any] = {}
    for f in STYLE_FIELDS:
        v = getattr(merged, f)
        if v is not None:
            out[f] = v
        elif base is not None and f in base and base[f] is not None:
            out[f] = base[f]
        else:
            out[f] = _LEGACY_DEFAULTS[f]
    return out


def clip_style_base(clip: Any) -> dict[str, Any]:
    """Extract the 34 style fields from a clip's flat (legacy) fields.

    Use as the ``base`` argument to :func:`resolve_style` so legacy-edited cues
    (whose style lives in the flat fields with ``style_override=None``) render
    with their actual values rather than the hard-coded legacy defaults.
    """
    return {f: getattr(clip, f, None) for f in STYLE_FIELDS}


# ──────────────────────────────────────────────────────────────────────────
# Legacy bridge — SubtitleClip flat fields ↔ CaptionStyle
# ──────────────────────────────────────────────────────────────────────────

def from_legacy_clip(clip: Any) -> CaptionStyle:
    """Build a :class:`CaptionStyle` from a legacy ``SubtitleClip``'s flat
    fields.

    Every style field on the clip is copied verbatim (none become ``None``),
    so the result fully describes the clip's current visual state. Used by the
    schema migration to populate a track's ``default_style`` from the first
    cue, and by tests asserting migration losslessness.
    """
    kwargs: dict[str, Any] = {}
    for f in STYLE_FIELDS:
        kwargs[f] = getattr(clip, f)
    return CaptionStyle(**kwargs)


def style_to_legacy_fields(style: CaptionStyle) -> dict[str, Any]:
    """Inverse of :func:`from_legacy_clip`: produce the *overrides-only* flat
    dict (only non-``None`` fields). Suitable for merging into an
    ``UpdateSubtitleClipCommand.updates`` dict.
    """
    return {f: getattr(style, f) for f in STYLE_FIELDS if getattr(style, f) is not None}


def style_to_full_legacy_fields(
    track_default: CaptionStyle,
    cue_override: Optional[CaptionStyle] = None,
) -> dict[str, Any]:
    """Resolve to a *complete* flat dict (every key present, no ``None``).

    This is what the dual-write serializer uses when emitting the legacy flat
    block: it guarantees old readers (pre-Phase-2A) see correct values even
    though the source of truth moved to the layered model.
    """
    return resolve_style(track_default, cue_override)


# Public compatibility exports: caption-graphic bindings are kept in their
# own module to avoid coupling the layered style model to timeline rendering,
# but callers may import the complete caption contract from ``model``.
