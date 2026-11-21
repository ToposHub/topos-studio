"""PSD/PSB manifest, patch clipping and import planning (B07).

The composite keeps its own PSD decode wiring (psd-tools, optional at
runtime).  A worker probes the document into an immutable
:class:`PsdDocumentManifest` (document identity, per-layer PSD source id,
structure path, bbox, order, visibility, opacity, blend and group/clip/
effects flags); ambiguous association never falls back to display-name
guessing — entries without a PSD layer id carry a structure-path
fingerprint.  Import planning admits only fidelity-safe plain pixel
layers in the first batch; passthrough groups, adjustments, smart objects,
clipped stacks and style-bearing layers merge over their affected range
with the reason recorded.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:  # optional at runtime; the pure helpers below work without it
    from psd_tools import PSDImage  # type: ignore
except ImportError:  # pragma: no cover
    PSDImage = None  # type: ignore

PSD_TOOLS_REQUIRED_VERSION = "1.27"

# ── Support matrix (B07 §1) ────────────────────────────────────────────────
# Documents the *decode* capability actually exercised by this wiring.
SUPPORT_MATRIX: Dict[str, str] = {
    "psd": "supported",          # PSD (version 1) documents
    "psb": "supported",          # large-document format via psd-tools
    "merged_preview": "optional",  # documents without a merged image read layers
    "16bit": "supported",
    "32bit": "unsupported",
    "bitmap_gray_index": "flattened-only",
    "layer_styles": "merged",   # B08 will make selected styles editable
    "smart_objects": "merged",
    "clipping_groups": "merged",
    "adjustment_layers": "merged",
}


@dataclass(frozen=True)
class PsdLayerEntry:
    """One PSD layer from the manifest (document order, top first)."""

    layer_id: Optional[int]           # PSD source layer id when present
    name: str
    structure_path: Tuple[str, ...]   # group hierarchy, root → parent
    bbox: Tuple[int, int, int, int]   # left, top, right, bottom (exclusive)
    visible: bool = True
    opacity: int = 255                # PSD stores 0-255
    blend_mode: str = "normal"
    is_group: bool = False
    group_passthrough: bool = False
    adjustment: bool = False
    smart_object: bool = False
    clipped: bool = False
    has_effects: bool = False

    @property
    def fingerprint(self) -> str:
        """Structure-path identity when no PSD layer id exists (B07 §2)."""
        payload = "|".join((*self.structure_path, self.name,
                            *[str(v) for v in self.bbox],
                            str(self.opacity), str(self.visible)))
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    @property
    def selector(self) -> Tuple[str, str]:
        """Stable cache selector: layer id or fingerprint."""
        if self.layer_id is not None and self.layer_id >= 0:
            return ("id", str(self.layer_id))
        return ("fp", self.fingerprint)


@dataclass(frozen=True)
class PsdDocumentManifest:
    """Immutable probe result shared by preview and export (B07 §1)."""

    asset_id: str
    revision: str
    width: int
    height: int
    color_mode: str
    bit_depth: int
    has_merged_image: bool
    layers: Tuple[PsdLayerEntry, ...]   # document order (top first)

    def entries_named(self, name: str) -> List[PsdLayerEntry]:
        return [entry for entry in self.layers if entry.name == name]


# ── Canvas patch clipping (plan §B07 example) ───────────────────────────────


def clip_psd_patch(
        bbox: Tuple[int, int, int, int],
        canvas: Tuple[int, int],
) -> Optional[Tuple[Tuple[int, int], Tuple[slice, slice]]]:
    """Clip a layer bbox (left, top, right, bottom, exclusive) to the canvas.

    Returns ``((x, y), (row_slice, col_slice))`` in canvas coordinates with
    the matching layer-pixel slices, or None when the patch lies fully
    outside.  Left-top origin, right-bottom exclusive (B07 plan draft).
    """
    left, top, right, bottom = bbox
    width, height = canvas
    x0, y0 = max(0, left), max(0, top)
    x1, y1 = min(width, right), min(height, bottom)
    if x0 >= x1 or y0 >= y1:
        return None
    return (x0, y0), (slice(y0 - top, y1 - top), slice(x0 - left, x1 - left))


# ── Import planning (B07 §3) ────────────────────────────────────────────────


@dataclass(frozen=True)
class PsdPlannedLayer:
    """One manifest entry with its import decision."""

    entry: PsdLayerEntry
    importable: bool
    merge_reason: str = ""   # group_passthrough | adjustment | smart_object |
                             # clipping | effects | unsupported_depth

    @property
    def canvas_offset(self) -> Tuple[int, int]:
        """Canvas placement of the layer's own pixel space."""
        return self.entry.bbox[0], self.entry.bbox[1]


def plan_psd_import(manifest: PsdDocumentManifest
                    ) -> Tuple[PsdPlannedLayer, ...]:
    """First-batch policy: only fidelity-safe plain pixel layers import.

    Group containers, adjustments, smart objects, clipping stacks and
    style-bearing layers stay merged over their affected range with the
    reason recorded (B07 §3); nothing is silently dropped.
    """
    planned: List[PsdPlannedLayer] = []
    for entry in manifest.layers:
        reason = ""
        if entry.is_group:
            reason = "group_passthrough" if entry.group_passthrough else "group"
        elif entry.adjustment:
            reason = "adjustment"
        elif entry.smart_object:
            reason = "smart_object"
        elif entry.clipped:
            reason = "clipping"
        elif entry.has_effects:
            reason = "effects"
        elif manifest.bit_depth >= 32:
            reason = "unsupported_depth"
        planned.append(PsdPlannedLayer(entry=entry, importable=not reason,
                                       merge_reason=reason))
    return tuple(planned)


def import_cache_key(asset_revision: str, selector: Tuple[str, str],
                     workspace: str, precision: str) -> tuple:
    """Cache identity: asset revision × layer selector × workspace × precision."""
    return (str(asset_revision), selector[0], selector[1],
            str(workspace), str(precision))


# ── Probing and pixel decode (CPU worker side) ─────────────────────────────


def probe_psd_manifest(path: str, asset_id: str, revision: str
                       ) -> PsdDocumentManifest:
    """Probe a PSD/PSB document into the immutable manifest.

    Runs on a CPU worker (never the UI thread); raises when psd-tools is
    unavailable so the host can surface the dependency diagnosis.
    """
    if PSDImage is None:  # pragma: no cover - environment guard
        raise RuntimeError(
            f"psd-tools >= {PSD_TOOLS_REQUIRED_VERSION} is required for "
            "layered PSD import")
    psd = PSDImage.open(path)
    entries: List[PsdLayerEntry] = []
    _walk_psd_tree(psd, (), entries)
    bit_depth = int(getattr(psd, "depth", 8) or 8)
    color_mode = str(getattr(psd, "color_mode", "")).split(".")[-1]
    return PsdDocumentManifest(
        asset_id=asset_id, revision=revision,
        width=int(psd.width), height=int(psd.height),
        color_mode=color_mode, bit_depth=bit_depth,
        has_merged_image=bool(getattr(psd, "has_preview", True)),
        layers=tuple(entries),
    )


def _walk_psd_tree(group: Any, path: Tuple[str, ...],
                   out: List[PsdLayerEntry]) -> None:
    for layer in group:
        is_group = bool(layer.is_group())
        if is_group:
            out.append(_entry_for(layer, path, is_group=True))
            _walk_psd_tree(layer, (*path, layer.name), out)
        else:
            out.append(_entry_for(layer, path, is_group=False))


def _entry_for(layer: Any, path: Tuple[str, ...], *, is_group: bool
               ) -> PsdLayerEntry:
    blend = str(getattr(layer, "blend_mode", "normal"))
    blend = blend.split(".")[-1].lower() if "." in blend else blend.lower()
    passthrough = is_group and blend in {"passthrough", "pass through"}
    adjustment = (not is_group
                  and str(getattr(layer, "kind", "")).endswith("ADJUSTMENT"))
    smart = (not is_group
             and bool(getattr(layer, "smart_object", None)))
    clipped = bool(getattr(layer, "clipping_layer", False))
    effects = _layer_has_effects(layer)
    layer_id = getattr(layer, "layer_id", None)
    try:
        layer_id = int(layer_id) if layer_id is not None else None
    except (TypeError, ValueError):
        layer_id = None
    if layer_id is not None and layer_id < 0:
        # psd-tools reports -1 when the document carries no lyid record.
        layer_id = None
    return PsdLayerEntry(
        layer_id=layer_id,
        name=str(layer.name),
        structure_path=path,
        bbox=(int(layer.left), int(layer.top),
              int(layer.right), int(layer.bottom)),
        visible=bool(layer.visible),
        opacity=int(getattr(layer, "opacity", 255) or 255),
        blend_mode="passthrough" if passthrough else blend,
        is_group=is_group,
        group_passthrough=passthrough,
        adjustment=adjustment,
        smart_object=smart,
        clipped=clipped,
        has_effects=effects,
    )


def _layer_has_effects(layer: Any) -> bool:
    """Effects detection via the effects container, tolerating variants."""
    effects = getattr(layer, "effects", None)
    if not effects:
        return False
    try:
        return bool(list(effects))
    except TypeError:
        return False


def read_psd_layer_rgba(psd_path: str, entry: PsdLayerEntry
                        ) -> "Any":
    """Decode one layer's pixels as RGBA (alpha separate, not premultiplied).

    Alpha, fill and layer opacity stay separate values; compositing applies
    them exactly once (B07 §3 — no double alpha multiplication).
    """
    if PSDImage is None:  # pragma: no cover
        raise RuntimeError("psd-tools is required for layered PSD decode")
    import numpy as np

    psd = PSDImage.open(psd_path)
    target = _find_layer_by_entry(psd, entry)
    if target is None:
        raise LookupError(
            f"PSD layer {entry.selector} not found in {psd_path!r}")
    import PIL.Image as Image
    if target.is_group():
        # Group composites render at canvas size; crop to the group bbox.
        pil: Image.Image = target.composite().crop(
            (entry.bbox[0], entry.bbox[1], entry.bbox[2], entry.bbox[3]))
    else:
        # Pixel layers decode at their own bbox size already.
        pil = target.topil()
    if pil is None:  # empty/zero-size layer
        return np.zeros((0, 0, 4), dtype=np.float32)
    return np.asarray(pil.convert("RGBA"), dtype=np.float32) / 255.0


def _find_layer_by_entry(root: Any, entry: PsdLayerEntry) -> Optional[Any]:
    """Locate a layer by PSD id (or fingerprint fallback) — never by name."""
    def walk(group: Any, path: Tuple[str, ...]):
        for layer in group:
            if layer.is_group():
                found = walk(layer, (*path, layer.name))
                if found is not None:
                    return found
                continue
            if entry.layer_id is not None:
                if getattr(layer, "layer_id", None) == entry.layer_id:
                    return layer
            elif _entry_for(layer, path, is_group=False).fingerprint == \
                    entry.fingerprint:
                return layer
        return None

    return walk(root, ())
