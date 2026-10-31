"""FH-01: FreeformHtmlMotionLayer — the versioned HTML/CSS content layer.

The plan (FH-01) lets a ``MotionGraphicScene`` carry one *controlled*
freeform HTML/CSS layer so the model's web-design expressiveness is available
without making the scene a JavaScript project.  The layer is **versioned,
sanitized data** — not a runtime — and it is owned by the scene via
``scene_id`` (never an independent temp file), so save/reopen/migration,
command history and project compatibility all keep working.

Provenance is first-class here: the layer records the sanitizer version, the
pinned renderer build, the source hash (authored, pre-sanitization) and the
sanitizer hash (post-sanitization).  These feed the cache key and let
preview/export consistency be checked and let a reviewer see exactly what the
sanitizer changed.

The editable contract (FH-06/FH-13) is declared up front: which DOM nodes are
selectable (``data-topos-node-id``), their edit mode
(``content`` / ``absolute_transform`` / ``token_only``) and which CSS custom
properties / asset slots are exposed for Inspector editing.  Everything else
in the DOM is not directly editable — direct DOM writes go through
``HtmlPatch`` (FH-10), not free string mutation.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping


_logger = logging.getLogger(__name__)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Allowed edit modes for an HTML node (FH-13 authoring contract).
_EDIT_MODES = ("content", "absolute_transform", "token_only")
# Allowed editable slot kinds (matches the HtmlPatch operations in FH-10).
_SLOT_KINDS = ("text", "asset", "token", "slot_attr", "animation_timing")


@dataclass(frozen=True)
class HtmlEditSlot:
    """One declared editable point in the HTML layer (FH-06/FH-13).

    A slot binds a stable ``data-topos-node-id`` to what the user/AI may do with
    it.  Only declared slots are editable; unmarked DOM is selectable only as
    the whole layer.
    """

    node_id: str
    edit_mode: str = "content"        # one of _EDIT_MODES
    kind: str = "text"                # one of _SLOT_KINDS
    label: str = ""

    def __post_init__(self) -> None:
        if not self.node_id:
            raise ValueError("HtmlEditSlot node_id cannot be empty")
        if self.edit_mode not in _EDIT_MODES:
            raise ValueError(f"edit_mode must be one of {_EDIT_MODES}, got {self.edit_mode!r}")
        if self.kind not in _SLOT_KINDS:
            raise ValueError(f"kind must be one of {_SLOT_KINDS}, got {self.kind!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "edit_mode": self.edit_mode,
                "kind": self.kind, "label": self.label}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "HtmlEditSlot":
        return cls(
            node_id=str(data.get("node_id", "")),
            edit_mode=str(data.get("edit_mode", "content")),
            kind=str(data.get("kind", "text")),
            label=str(data.get("label", "")),
        )


@dataclass(frozen=True)
class FreeformHtmlMotionLayer:
    """A sanitized HTML/CSS content layer owned by a Teaching scene (FH-01).

    Attributes:
        layer_id: stable id within the scene.
        sanitized_html: the allowlist-sanitized HTML/CSS source.  This is the
            versioned source data — there is never any ``<script>`` or
            executable URL in it.
        viewport_width/height: design-pixel canvas the HTML was authored for.
        fps_num/fps_den/duration_frames: the layer's own time semantics so the
            seek bridge can render any frame deterministically.
        bound_asset_ids: ``topos-asset://`` ids this layer references; each MUST
            resolve to a project asset (validated against scene.asset_refs).
        editable_slots: declared editable nodes (FH-06/FH-13).
        editable_tokens: CSS custom properties exposed to the Inspector.
        sanitizer_version / renderer_build: pinned tool versions for cache keys
            and cross-build comparisons.
        source_hash: sha256 of the authored (pre-sanitization) source.
        sanitizer_hash: sha256 of ``sanitized_html`` (post-sanitization).
        csp: the Content-Security-Policy applied when rendering this layer.
        revision: patch revision for undo/redo of HTML edits.
    """

    layer_id: str
    sanitized_html: str
    viewport_width: int
    viewport_height: int
    fps_num: int
    fps_den: int
    duration_frames: int
    bound_asset_ids: tuple[str, ...] = ()
    editable_slots: tuple[HtmlEditSlot, ...] = ()
    editable_tokens: tuple[str, ...] = ()
    sanitizer_version: str = ""
    renderer_build: str = ""
    source_hash: str = ""
    sanitizer_hash: str = ""
    csp: str = ""
    #: sha256 over the packaged font set actually used to render this layer
    #: (W0-A font lock).  Empty for layers authored before font locking.  The
    #: value is the same default_font_set_hash() the renderer injects, recorded
    #: so a font-file bump invalidates cached frames and reviewers can audit it.
    font_set_hash: str = ""
    revision: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.layer_id:
            raise ValueError("layer_id cannot be empty")
        if not self.sanitized_html.strip():
            raise ValueError("sanitized_html cannot be empty")
        if self.viewport_width <= 0 or self.viewport_height <= 0:
            raise ValueError("viewport dimensions must be positive")
        if self.fps_num <= 0 or self.fps_den <= 0:
            raise ValueError("fps must be positive")
        if self.duration_frames <= 0:
            raise ValueError("duration_frames must be positive")
        if self.revision < 1:
            raise ValueError("revision must be >= 1")
        # Keep sanitizer_hash consistent with the payload when not overridden.
        if not self.sanitizer_hash:
            object.__setattr__(self, "sanitizer_hash", _sha256(self.sanitized_html))

    @property
    def fps(self) -> float:
        return self.fps_num / self.fps_den

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer_id": self.layer_id,
            "sanitized_html": self.sanitized_html,
            "viewport_width": self.viewport_width,
            "viewport_height": self.viewport_height,
            "fps_num": self.fps_num,
            "fps_den": self.fps_den,
            "duration_frames": self.duration_frames,
            "bound_asset_ids": list(self.bound_asset_ids),
            "editable_slots": [slot.to_dict() for slot in self.editable_slots],
            "editable_tokens": list(self.editable_tokens),
            "sanitizer_version": self.sanitizer_version,
            "renderer_build": self.renderer_build,
            "source_hash": self.source_hash,
            "sanitizer_hash": self.sanitizer_hash,
            "csp": self.csp,
            "font_set_hash": self.font_set_hash,
            "revision": self.revision,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FreeformHtmlMotionLayer":
        slots_data = data.get("editable_slots", ())
        layer_id = str(data.get("layer_id", ""))
        sanitized_html = str(data.get("sanitized_html", ""))
        sanitizer_hash = str(data.get("sanitizer_hash", ""))
        # AUD-27513: sanitizer_hash is redundant, derivable data carried by an
        # external payload (hand-edited / corrupted / older writer).  It anchors
        # the render-evidence cache key and audit trail, so a mismatching value
        # is overridden by the authoritative recompute and logged rather than
        # silently flowing into cache keys.
        actual_hash = _sha256(sanitized_html)
        if sanitizer_hash and sanitizer_hash != actual_hash:
            _logger.warning(
                "html layer %r: sanitizer_hash mismatch (payload=%s, recomputed=%s); "
                "using the recomputed hash", layer_id, sanitizer_hash, actual_hash,
            )
            sanitizer_hash = actual_hash
        return cls(
            layer_id=layer_id,
            sanitized_html=sanitized_html,
            viewport_width=int(data.get("viewport_width", 1920)),
            viewport_height=int(data.get("viewport_height", 1080)),
            fps_num=int(data.get("fps_num", 24)),
            fps_den=int(data.get("fps_den", 1)),
            duration_frames=int(data.get("duration_frames", 240)),
            bound_asset_ids=tuple(str(a) for a in data.get("bound_asset_ids", ())),
            editable_slots=tuple(
                HtmlEditSlot.from_dict(slot)
                for slot in slots_data
                if isinstance(slot, Mapping)
            ),
            editable_tokens=tuple(str(t) for t in data.get("editable_tokens", ())),
            sanitizer_version=str(data.get("sanitizer_version", "")),
            renderer_build=str(data.get("renderer_build", "")),
            source_hash=str(data.get("source_hash", "")),
            sanitizer_hash=sanitizer_hash,
            csp=str(data.get("csp", "")),
            font_set_hash=str(data.get("font_set_hash", "")),
            revision=int(data.get("revision", 1)),
            metadata=dict(data.get("metadata", {})),
        )


def build_html_layer(
    *,
    layer_id: str,
    sanitized_html: str,
    authored_source: str,
    viewport_width: int,
    viewport_height: int,
    fps_num: int = 24,
    fps_den: int = 1,
    duration_frames: int = 240,
    bound_asset_ids: tuple[str, ...] = (),
    editable_slots: tuple[HtmlEditSlot, ...] = (),
    editable_tokens: tuple[str, ...] = (),
    sanitizer_version: str = "",
    renderer_build: str = "",
    csp: str = "",
    font_set_hash: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> FreeformHtmlMotionLayer:
    """Construct a layer, deriving source_hash + sanitizer_hash from the sources.

    ``authored_source`` is the pre-sanitization text (what the model emitted);
    ``sanitized_html`` is the allowlisted result.  Keeping both hashes lets a
    reviewer audit what the sanitizer changed and lets the cache key react to
    either side changing.
    """
    return FreeformHtmlMotionLayer(
        layer_id=layer_id,
        sanitized_html=sanitized_html,
        viewport_width=viewport_width,
        viewport_height=viewport_height,
        fps_num=fps_num,
        fps_den=fps_den,
        duration_frames=duration_frames,
        bound_asset_ids=bound_asset_ids,
        editable_slots=editable_slots,
        editable_tokens=editable_tokens,
        sanitizer_version=sanitizer_version,
        renderer_build=renderer_build,
        source_hash=_sha256(authored_source),
        sanitizer_hash=_sha256(sanitized_html),
        csp=csp,
        font_set_hash=font_set_hash,
        metadata=dict(metadata or {}),
    )


__all__ = [
    "FreeformHtmlMotionLayer",
    "HtmlEditSlot",
    "build_html_layer",
]
