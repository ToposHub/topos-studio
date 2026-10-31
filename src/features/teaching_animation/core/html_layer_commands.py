"""FH-01/W2: undoable commands that land a Freeform HTML layer in a scene.

These close the loop the plan calls "approve → write to project / insert into
timeline": once ``run_html_generation_pipeline`` has produced a **writable,
approved** outcome (AQ-09), the host builds a :class:`FreeformHtmlMotionLayer`
from it and applies it to the scene through the immutable command stack, so the
write is reversible, recorded, and round-trips with the project (FH-01).

* :class:`SetHtmlLayerCommand` — set/replace the scene's HTML layer; undo
  restores the previous layer (which may legitimately be ``None``).
* :class:`RemoveHtmlLayerCommand` — drop the layer (e.g. the user rejects the
  generated page after preview).
* :func:`build_html_layer_from_pipeline` — construct a layer from a writable
  generation outcome, pulling ``renderer_build`` from the preview evidence and
  stamping both provenance hashes.

The commands never invent asset content: bound assets referenced by the layer
must already be declared on the scene (the FH-01 validator enforces
``bound_asset_ids ⊆ asset_refs``); the host registers them separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Mapping
import hashlib
import uuid

from .html_layer import FreeformHtmlMotionLayer, HtmlEditSlot, build_html_layer
from .model import ScenePage, TeachingSceneDocument
from .pages import sync_pages_with_html_layer

if TYPE_CHECKING:  # avoid an import cycle at runtime
    from ..ai.html_generation_protocol import HtmlGenerationOutcome
    from ..render.html_layer.html_patch import HtmlPatch


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class SetHtmlLayerCommand:
    """Set or replace the scene's Freeform HTML layer (FH-01).

    ``apply`` records the previous layer so ``undo`` can restore it exactly —
    including the case where the scene previously had no layer (``None``),
    which is tracked separately via ``_applied`` because ``None`` is a valid
    previous value.
    """

    layer: FreeformHtmlMotionLayer
    command_id: str = field(default_factory=lambda: _new_id("set_html_layer"))
    label: str = "Set Freeform HTML Layer"
    _previous_layer: FreeformHtmlMotionLayer | None = None
    _previous_pages: tuple[ScenePage, ...] | None = None
    _previous_elements: Any = None
    _applied: bool = False

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if not isinstance(self.layer, FreeformHtmlMotionLayer):
            raise ValueError("layer must be a FreeformHtmlMotionLayer")
        object.__setattr__(self, "_previous_layer", scene.html_layer)
        object.__setattr__(self, "_previous_pages", scene.pages)
        object.__setattr__(self, "_previous_elements", scene.elements)
        object.__setattr__(self, "_applied", True)
        return sync_pages_with_html_layer(replace(scene, html_layer=self.layer))

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if not self._applied:
            raise RuntimeError("set_html_layer has not been applied")
        return replace(scene, html_layer=self._previous_layer,
                       pages=self._previous_pages, elements=self._previous_elements)


@dataclass(frozen=True)
class SetHtmlPageTimingCommand:
    """Change page-deck timing without touching the authored HTML source."""

    timing: Any
    command_id: str = field(default_factory=lambda: _new_id("set_html_page_timing"))
    label: str = "Set HTML Page Timing"
    _previous_layer: FreeformHtmlMotionLayer | None = None
    _previous_pages: tuple[ScenePage, ...] | None = None
    _previous_elements: Any = None
    _applied: bool = False

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        layer = scene.html_layer
        if layer is None:
            raise ValueError("cannot set HTML page timing: scene has no html_layer")
        from .html_page_deck import timing_metadata
        object.__setattr__(self, "_previous_layer", layer)
        object.__setattr__(self, "_previous_pages", scene.pages)
        object.__setattr__(self, "_previous_elements", scene.elements)
        object.__setattr__(self, "_applied", True)
        metadata = dict(layer.metadata)
        metadata.update(timing_metadata(self.timing))
        duration_frames = max(1, round(self.timing.total_seconds * scene.fps))
        updated = replace(layer, metadata=metadata, duration_frames=duration_frames,
                          revision=layer.revision + 1)
        return sync_pages_with_html_layer(
            replace(scene, html_layer=updated), duration_frames=duration_frames)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if not self._applied:
            raise RuntimeError("set_html_page_timing has not been applied")
        return replace(scene, html_layer=self._previous_layer,
                       pages=self._previous_pages, elements=self._previous_elements)


@dataclass(frozen=True)
class ReplaceHtmlSourceCommand:
    """Sanitize and replace the complete HTML source as one undoable edit.

    This is the manual-editor escape hatch for layouts that do not declare
    individual ``data-topos-node-id`` slots.  It never writes raw source
    directly: the layer's existing browser profile is reused, conventional
    page containers receive stable page markers, and editable-node metadata is
    rebuilt from the sanitized result.
    """

    source: str
    command_id: str = field(default_factory=lambda: _new_id("replace_html_source"))
    label: str = "Replace HTML Source"
    _previous_layer: FreeformHtmlMotionLayer | None = None
    _previous_pages: tuple[ScenePage, ...] | None = None
    _previous_elements: Any = None
    _applied: bool = False

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        layer = scene.html_layer
        if layer is None:
            raise ValueError("cannot replace HTML source: scene has no html_layer")
        source = str(self.source or "")
        if not source.strip():
            raise ValueError("HTML source cannot be empty")
        from ..render.html_layer.node_identity import extract_declared_nodes, extract_tokens
        from ..render.html_layer.sanitizer import HtmlSanitizer
        from .html_page_deck import (
            PageDeckTiming,
            detect_page_count,
            ensure_page_markers,
            page_timing_for,
            timing_metadata,
        )

        metadata = dict(layer.metadata)
        sanitizer = HtmlSanitizer(
            allow_network_resources=metadata.get("network_access", False) is True,
            allow_inline_scripts=metadata.get("allow_inline_scripts", False) is True,
            allow_event_handlers=metadata.get("allow_event_handlers", False) is True,
            allow_freeform_css=metadata.get("allow_freeform_css", False) is True,
            allow_inline_visual_resources=(
                metadata.get("allow_inline_visual_resources", False) is True
            ),
        )
        result = sanitizer.sanitize(source)
        if not result.html.strip():
            detail = ", ".join(result.report.notes) or "清洗后没有可用 HTML"
            raise ValueError(f"HTML source rejected: {detail}")
        sanitized_html = ensure_page_markers(result.html)
        page_count = detect_page_count(sanitized_html)
        total_seconds = max(0.001, layer.duration_frames / max(0.001, layer.fps))
        old_timing = page_timing_for(
            metadata,
            page_count=page_count,
            fallback_default_seconds=total_seconds / page_count,
        )
        # A page-count change invalidates a custom duration vector.  Keep the
        # total clip length and transition choice, then fall back to an equal
        # split that the user can immediately refine in the timing panel.
        raw_timing = metadata.get("page_timing", {})
        raw_durations = raw_timing.get("durations_seconds", ()) if isinstance(raw_timing, dict) else ()
        raw_mode = raw_timing.get("mode") if isinstance(raw_timing, dict) else None
        if raw_mode == "custom" and len(raw_durations) == page_count:
            timing = old_timing
        else:
            timing = PageDeckTiming(
                page_count,
                "default",
                total_seconds / page_count,
                transition=old_timing.transition,
                transition_seconds=old_timing.transition_seconds,
            )
        metadata.update(timing_metadata(timing))

        declared = extract_declared_nodes(sanitized_html)
        old_labels = {slot.node_id: slot.label for slot in layer.editable_slots}
        slots = tuple(
            HtmlEditSlot(
                node_id=node_id,
                edit_mode=node.edit_mode,
                kind=("asset" if node.asset_slot else
                      "token" if node.edit_mode == "token_only" else "text"),
                label=old_labels.get(node_id, node_id),
            )
            for node_id, node in sorted(declared.items())
        )
        object.__setattr__(self, "_previous_layer", layer)
        object.__setattr__(self, "_previous_pages", scene.pages)
        object.__setattr__(self, "_previous_elements", scene.elements)
        object.__setattr__(self, "_applied", True)
        updated = replace(
            layer,
            sanitized_html=sanitized_html,
            editable_slots=slots,
            editable_tokens=tuple(sorted(extract_tokens(sanitized_html))),
            source_hash=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            sanitizer_hash=hashlib.sha256(sanitized_html.encode("utf-8")).hexdigest(),
            metadata=metadata,
            revision=layer.revision + 1,
        )
        return sync_pages_with_html_layer(replace(scene, html_layer=updated))

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if not self._applied:
            raise RuntimeError("replace_html_source has not been applied")
        return replace(scene, html_layer=self._previous_layer,
                       pages=self._previous_pages, elements=self._previous_elements)


@dataclass(frozen=True)
class RemoveHtmlLayerCommand:
    """Remove the scene's HTML layer (restores it on undo)."""

    command_id: str = field(default_factory=lambda: _new_id("remove_html_layer"))
    label: str = "Remove Freeform HTML Layer"
    _previous_layer: FreeformHtmlMotionLayer | None = None
    _applied: bool = False

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        object.__setattr__(self, "_previous_layer", scene.html_layer)
        object.__setattr__(self, "_applied", True)
        return replace(scene, html_layer=None)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if not self._applied:
            raise RuntimeError("remove_html_layer has not been applied")
        return replace(scene, html_layer=self._previous_layer)


@dataclass(frozen=True)
class ApplyHtmlPatchCommand:
    """Apply a validated ``HtmlPatch`` to the scene's HTML layer (FH-10).

    The apply half the plan calls for: the validator
    (:func:`validate_patch`) is the fail-before-write boundary, and this command
    is the reversible write.  It delegates to
    :func:`apply_html_patch`, which mutates only the patch's declared target and
    re-sanitizes the result, then returns a new scene carrying the bumped-
    revision layer.  Undo restores the previous layer exactly.

    The command is a no-op error if the scene has no HTML layer.
    """

    patch: "HtmlPatch"
    command_id: str = field(default_factory=lambda: _new_id("apply_html_patch"))
    label: str = "Apply HTML Patch"
    _previous_layer: FreeformHtmlMotionLayer | None = None
    _previous_pages: tuple[ScenePage, ...] | None = None
    _previous_elements: Any = None
    _applied: bool = False

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if scene.html_layer is None:
            raise ValueError("cannot apply an HTML patch: scene has no html_layer")
        from ..render.html_layer.html_patch_apply import apply_html_patch
        object.__setattr__(self, "_previous_layer", scene.html_layer)
        object.__setattr__(self, "_previous_pages", scene.pages)
        object.__setattr__(self, "_previous_elements", scene.elements)
        object.__setattr__(self, "_applied", True)
        new_layer = apply_html_patch(scene.html_layer, self.patch)
        return sync_pages_with_html_layer(replace(scene, html_layer=new_layer))

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if not self._applied:
            raise RuntimeError("apply_html_patch has not been applied")
        return replace(scene, html_layer=self._previous_layer,
                       pages=self._previous_pages, elements=self._previous_elements)


def build_html_layer_from_pipeline(
    outcome: "HtmlGenerationOutcome",
    *,
    layer_id: str,
    authored_source: str,
    bound_asset_ids: tuple[str, ...] = (),
    editable_slots: tuple[HtmlEditSlot, ...] = (),
    editable_tokens: tuple[str, ...] = (),
    sanitizer_version: str = "",
    viewport_width: int = 1920,
    viewport_height: int = 1080,
    duration_frames: int = 240,
    fps_num: int = 24,
    fps_den: int = 1,
    metadata: dict[str, Any] | None = None,
) -> FreeformHtmlMotionLayer:
    """Build a layer from an approved, renderable outcome (AQ-09 → FH-01).

    Refuses a non-writable outcome: only an approved, sanitized page may become
    a project-owned layer.  In browser/render-only mode static analysis is
    intentionally reported as skipped, because the model/user own design
    decisions.  The pinned ``renderer_build``
    is pulled from the preview evidence (empty when preview was not run), and
    both provenance hashes are stamped so a reviewer can audit what the
    sanitizer changed and the cache key can react to either side changing.
    """
    if not getattr(outcome, "writable", False):
        raise ValueError(
            "cannot build an HTML layer from a non-writable outcome "
            "(approve + pass sanitize/static-analysis first)"
        )
    # The renderer that previewed this page is the one export must use to
    # reproduce it byte-for-byte; carry its build onto the layer.
    preview = getattr(outcome, "preview", None) or {}
    renderer_build = str(preview.get("backend_build", "") or "")
    # The normal HTML path keeps the packaged font lock for cross-machine
    # determinism.  Motion Graphic Clip's freeform browser profile can opt out
    # so authored font-family choices are rendered faithfully; its preview and
    # export then use the same unlocked profile.
    layer_metadata = dict(metadata or {})
    font_lock_enabled = layer_metadata.get("font_lock", True) is not False
    network_access = layer_metadata.get("network_access", False) is True
    allow_inline_scripts = layer_metadata.get("allow_inline_scripts", False) is True
    allow_inline_visual_resources = layer_metadata.get(
        "allow_inline_visual_resources", False,
    ) is True
    raw_network_hosts = layer_metadata.get("network_hosts", ())
    network_hosts = frozenset(
        str(host).strip().lower().rstrip(".")
        for host in (raw_network_hosts if isinstance(raw_network_hosts, (list, tuple, set)) else ())
        if str(host).strip()
    )
    # CSP must allow the packaged font-lock assets (topos-asset://font/...) in
    # addition to the layer's own bound image assets when the lock is active.
    from .html_fonts import DEFAULT_FONT_ASSET_IDS, default_font_set_hash
    from ..render.html_renderer_spike.security import CspPolicy
    from ..render.html_layer.node_identity import extract_declared_nodes, extract_tokens
    csp_asset_ids = frozenset(bound_asset_ids)
    if font_lock_enabled:
        csp_asset_ids |= frozenset(DEFAULT_FONT_ASSET_IDS)
    csp = CspPolicy(
        allowed_asset_ids=csp_asset_ids,
        allow_network_resources=network_access,
        allowed_network_hosts=network_hosts,
        allow_inline_scripts=allow_inline_scripts,
        allow_inline_visual_resources=allow_inline_visual_resources,
    ).to_header()
    # HTML generated by the model declares editable DOM nodes with stable
    # ``data-topos-node-id`` attributes.  Materialize those declarations on
    # the layer automatically so the browser preview can select a node and
    # the Inspector can immediately offer a guarded text/token patch.
    if not editable_slots:
        nodes = extract_declared_nodes(outcome.sanitized_html)
        editable_slots = tuple(
            HtmlEditSlot(
                node_id=node_id,
                edit_mode=node.edit_mode,
                kind=("asset" if node.asset_slot else
                      "token" if node.edit_mode == "token_only" else "text"),
                label=node_id,
            )
            for node_id, node in sorted(nodes.items())
        )
    if not editable_tokens:
        editable_tokens = tuple(sorted(extract_tokens(outcome.sanitized_html)))
    return build_html_layer(
        layer_id=layer_id,
        sanitized_html=outcome.sanitized_html,
        authored_source=authored_source,
        viewport_width=viewport_width,
        viewport_height=viewport_height,
        duration_frames=duration_frames,
        fps_num=fps_num,
        fps_den=fps_den,
        bound_asset_ids=bound_asset_ids,
        editable_slots=editable_slots,
        editable_tokens=editable_tokens,
        sanitizer_version=sanitizer_version,
        renderer_build=renderer_build,
        csp=csp,
        font_set_hash=default_font_set_hash() if font_lock_enabled else "",
        metadata=layer_metadata,
    )


def build_html_layer_from_source(
    source: str,
    *,
    layer_id: str,
    authored_source: str | None = None,
    viewport_width: int = 1920,
    viewport_height: int = 1080,
    duration_frames: int = 375,
    fps_num: int = 25,
    fps_den: int = 1,
    bound_asset_ids: tuple[str, ...] = (),
    metadata: Mapping[str, Any] | None = None,
    editable_slots: tuple[Any, ...] | None = None,
) -> FreeformHtmlMotionLayer:
    """Build a Motion Graphic HTML layer from a user-authored document.

    ``editable_slots`` overrides the re-detected node list.  Template
    instantiation passes the promoted template's slots so labels and order
    survive the rebuild exactly as the author declared them.

    This is the import counterpart of :func:`build_html_layer_from_pipeline`.
    It deliberately uses the same sanitizer/page-deck/node-identity path as a
    full-source edit, but also works when a newly-created clip has no HTML
    layer yet.  The default profile is the existing freeform Motion Graphic
    browser profile (inline JS/CSS and network fonts are retained under the
    renderer's CSP), while callers may override any metadata flag explicitly.
    """
    from .html_fonts import DEFAULT_FONT_ASSET_IDS, default_font_set_hash
    from .html_page_deck import (
        PageDeckTiming,
        detect_page_count,
        ensure_page_markers,
        timing_metadata,
    )
    from ..render.html_layer.node_identity import extract_declared_nodes, extract_tokens
    from ..render.html_layer.sanitizer import HtmlSanitizer
    from ..render.html_renderer_spike.security import CspPolicy

    authored = str(source if authored_source is None else authored_source)
    if not authored.strip():
        raise ValueError("HTML source cannot be empty")
    layer_metadata: dict[str, Any] = {
        "font_lock": False,
        "render_profile": "motion_graphic_freeform",
        "network_access": True,
        "allow_inline_scripts": True,
        "allow_event_handlers": True,
        "allow_freeform_css": True,
        "allow_inline_visual_resources": True,
        "allow_gpu_apis": True,
    }
    if metadata:
        layer_metadata.update(dict(metadata))

    sanitizer = HtmlSanitizer(
        allow_network_resources=layer_metadata.get("network_access") is True,
        allow_inline_scripts=layer_metadata.get("allow_inline_scripts") is True,
        allow_event_handlers=layer_metadata.get("allow_event_handlers") is True,
        allow_freeform_css=layer_metadata.get("allow_freeform_css") is True,
        allow_inline_visual_resources=(
            layer_metadata.get("allow_inline_visual_resources") is True
        ),
    )
    result = sanitizer.sanitize(authored)
    if not result.html.strip():
        detail = ", ".join(result.report.notes) or "清洗后没有可用 HTML"
        raise ValueError(f"HTML source rejected: {detail}")

    sanitized_html = ensure_page_markers(result.html)
    page_count = max(1, detect_page_count(sanitized_html))
    total_seconds = max(0.001, int(duration_frames) / max(0.001, fps_num / fps_den))
    timing = PageDeckTiming(
        page_count,
        "default",
        total_seconds / page_count,
    )
    layer_metadata.update(timing_metadata(timing))
    if editable_slots is not None:
        slots = tuple(editable_slots)
    else:
        declared = extract_declared_nodes(sanitized_html)
        slots = tuple(
            HtmlEditSlot(
                node_id=node_id,
                edit_mode=node.edit_mode,
                kind=("asset" if node.asset_slot else
                      "token" if node.edit_mode == "token_only" else "text"),
                label=node_id,
            )
            for node_id, node in sorted(declared.items())
        )
    font_lock_enabled = layer_metadata.get("font_lock", True) is not False
    raw_hosts = layer_metadata.get("network_hosts", ())
    network_hosts = frozenset(
        str(host).strip().lower().rstrip(".")
        for host in (raw_hosts if isinstance(raw_hosts, (list, tuple, set)) else ())
        if str(host).strip()
    )
    csp_asset_ids = frozenset(str(asset_id) for asset_id in bound_asset_ids)
    if font_lock_enabled:
        csp_asset_ids |= frozenset(DEFAULT_FONT_ASSET_IDS)
    csp = CspPolicy(
        allowed_asset_ids=csp_asset_ids,
        allow_network_resources=layer_metadata.get("network_access") is True,
        allowed_network_hosts=network_hosts,
        allow_inline_scripts=layer_metadata.get("allow_inline_scripts") is True,
        allow_inline_visual_resources=(
            layer_metadata.get("allow_inline_visual_resources") is True
        ),
    ).to_header()
    return build_html_layer(
        layer_id=layer_id,
        sanitized_html=sanitized_html,
        authored_source=authored,
        viewport_width=max(1, int(viewport_width)),
        viewport_height=max(1, int(viewport_height)),
        duration_frames=max(1, int(duration_frames)),
        fps_num=max(1, int(fps_num)),
        fps_den=max(1, int(fps_den)),
        bound_asset_ids=tuple(bound_asset_ids),
        editable_slots=slots,
        editable_tokens=tuple(sorted(extract_tokens(sanitized_html))),
        sanitizer_version="html-sanitizer-v1",
        csp=csp,
        font_set_hash=default_font_set_hash() if font_lock_enabled else "",
        metadata=layer_metadata,
    )


__all__ = [
    "ApplyHtmlPatchCommand",
    "RemoveHtmlLayerCommand",
    "ReplaceHtmlSourceCommand",
    "SetHtmlLayerCommand",
    "SetHtmlPageTimingCommand",
    "build_html_layer_from_source",
    "build_html_layer_from_pipeline",
]
