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
import json
import math
import os
from collections import Counter
from dataclasses import dataclass, replace
from functools import lru_cache
from threading import Lock
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional, Tuple

import numpy as np

from .layer_model import LAYER_BLEND_MODES
from .layer_styles import (
    LayerStyleEffect, STYLE_KIND_COLOR_OVERLAY, STYLE_KIND_DROP_SHADOW,
    STYLE_KIND_OUTER_GLOW, plan_style_stack,
)

if TYPE_CHECKING:
    from .media_drop_payload import MediaDropPayload

try:  # optional at runtime; the pure helpers below work without it
    from psd_tools import PSDImage  # type: ignore
    from psd_tools.api.layers import AdjustmentLayer
    from psd_tools.constants import BlendMode, GlowTechnique, Resource, Tag
    from psd_tools.psd.descriptor import Descriptor, List as DescriptorList
    from psd_tools.terminology import Key, Klass
except ImportError:  # pragma: no cover
    PSDImage = None  # type: ignore

PSD_TOOLS_REQUIRED_VERSION = "1.27.0"
PSD_LAYER_REFERENCE_VERSION = 1
_document_lock = Lock()


@lru_cache(maxsize=2)
def _open_document(path: str, mtime_ns: int, ctime_ns: int, byte_size: int) -> Any:
    return PSDImage.open(path)


def _open_psd(path: str) -> tuple[Any, str]:
    stat = os.stat(path)
    with _document_lock:
        document = _open_document(path, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
    return document, f"{stat.st_mtime_ns}:{stat.st_size}"

# ── Support matrix (B07 §1) ────────────────────────────────────────────────
# Documents the *decode* capability actually exercised by this wiring.
SUPPORT_MATRIX: Dict[str, str] = {
    "psd": "supported",          # PSD (version 1) documents
    "psb": "supported",          # large-document format via psd-tools
    "merged_preview": "optional",  # documents without a merged image read layers
    "16bit": "supported",
    "32bit": "unsupported",
    "bitmap_gray_index": "flattened-only",
    "layer_styles": "editable_rgb_normal_subset",
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
    fill_opacity: int = 255
    masked: bool = False
    kind: str = "pixel"
    styles: tuple[LayerStyleEffect, ...] = ()

    @property
    def fingerprint(self) -> str:
        """Structure-path identity when no PSD layer id exists (B07 §2)."""
        payload = json.dumps((self.structure_path, self.name, self.bbox,
                              self.opacity, self.visible, self.is_group),
                             ensure_ascii=False, separators=(",", ":"))
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
    has_color_profile: bool = False
    color_profile_srgb: bool = False

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
    merge_reason: str = ""

    @property
    def canvas_offset(self) -> Tuple[int, int]:
        """Canvas placement of the layer's own pixel space."""
        return self.entry.bbox[0], self.entry.bbox[1]


def plan_psd_import(manifest: PsdDocumentManifest
                    ) -> Tuple[PsdPlannedLayer, ...]:
    """First-batch policy: only fidelity-safe plain pixel layers import.

    Groups and effects that depend on layers beneath them mark those
    contributors as requiring the same merged fallback.
    """
    planned: List[PsdPlannedLayer] = []
    groups = {(*entry.structure_path, entry.name) for entry in manifest.layers if entry.is_group}
    selector_counts = Counter(entry.selector for entry in manifest.layers)
    pending_clipping_bases = set()
    backdrop_reasons = {}
    for entry in manifest.layers:
        path = entry.structure_path
        clipping_base = path in pending_clipping_bases and not entry.clipped
        if entry.clipped:
            pending_clipping_bases.add(path)
        elif clipping_base:
            pending_clipping_bases.remove(path)
        backdrop_reason = backdrop_reasons.get(path, "")
        if entry.visible and not entry.clipped:
            if entry.adjustment:
                backdrop_reasons[path] = "adjustment_backdrop"
            elif entry.group_passthrough:
                backdrop_reasons[path] = "group_backdrop"
        reason = ""
        if selector_counts[entry.selector] > 1:
            reason = "ambiguous_selector"
        elif any(path[:depth] in groups for depth in range(1, len(path) + 1)):
            reason = "group_contents"
        elif entry.is_group:
            reason = "group_passthrough" if entry.group_passthrough else "group"
        elif entry.adjustment:
            reason = "adjustment"
        elif entry.smart_object:
            reason = "smart_object"
        elif entry.clipped:
            reason = "clipping"
        elif entry.has_effects and not _supports_editable_styles(entry, manifest):
            reason = "effects"
        elif entry.masked:
            reason = "layer_mask"
        elif entry.kind != "pixel":
            reason = "unsupported_layer_kind"
        elif entry.blend_mode not in LAYER_BLEND_MODES:
            reason = "unsupported_blend"
        elif manifest.color_mode != "RGB":
            reason = "unsupported_color_mode"
        elif manifest.has_color_profile and not manifest.color_profile_srgb:
            reason = "color_profile"
        elif manifest.bit_depth not in (8, 16):
            reason = "unsupported_depth"
        elif clipping_base:
            reason = "clipping_base"
        elif backdrop_reason:
            reason = backdrop_reason
        planned.append(PsdPlannedLayer(entry=entry, importable=not reason,
                                       merge_reason=reason))
    return tuple(planned)


def _supports_editable_styles(entry: PsdLayerEntry, manifest: PsdDocumentManifest) -> bool:
    editable, diagnostics = plan_style_stack(entry.styles)
    # The native node's order is overlay over pixels, glow, then shadow.
    # Layer and fill-opacity semantics diverge in some PSD style modes.
    left, top, right, bottom = entry.bbox
    fully_inside = 0 <= left <= right <= manifest.width and 0 <= top <= bottom <= manifest.height
    return (bool(editable) and not diagnostics
            and entry.opacity == entry.fill_opacity == 255
            and entry.blend_mode == "normal"
            and (fully_inside or all(style.kind == STYLE_KIND_COLOR_OVERLAY for style in editable))
            and len({style.kind for style in editable}) == len(editable))


def plan_psd_import_units(manifest: PsdDocumentManifest,
                          planned: tuple[PsdPlannedLayer, ...]) -> tuple[tuple[PsdPlannedLayer, ...], ...] | None:
    """Partition supported bottom-up ranges; None selects the merged document."""
    if (not planned or manifest.color_mode != "RGB" or manifest.bit_depth not in (8, 16)
            or (manifest.has_color_profile and not manifest.color_profile_srgb)
            or any(item.merge_reason in ("ambiguous_selector", "effects", "unsupported_blend")
                   or (item.entry.has_effects and not _supports_editable_styles(item.entry, manifest))
                   for item in planned)):
        return None
    roots = [item for item in planned if not item.entry.structure_path]
    units = []
    cursor = 0
    while cursor < len(roots):
        item = roots[cursor]
        if item.importable:
            units.append((item,))
            cursor += 1
            continue
        if item.merge_reason in ("group_passthrough", "adjustment") or item.entry.blend_mode not in (
                "normal", "passthrough"):
            # These operations need every lower contributor in the same
            # isolated backdrop. The upper unrelated layers stay editable.
            units.append(tuple(roots[cursor:]))
            break
        end = cursor + 1
        if item.entry.clipped:
            while end < len(roots) and roots[end].entry.clipped:
                end += 1
            end = min(end + 1, len(roots))
            if any(layer.entry.blend_mode != "normal" for layer in roots[cursor:end]):
                units.append(tuple(roots[cursor:]))
                break
        units.append(tuple(roots[cursor:end]))
        cursor = end
    return tuple(units)


def import_cache_key(asset_revision: str, selector: Tuple[str, str],
                     workspace: str, precision: str) -> tuple:
    """Cache identity: asset revision × layer selector × workspace × precision."""
    return (str(asset_revision), selector[0], selector[1],
            str(workspace), str(precision))


# ── Probing and pixel decode (CPU worker side) ─────────────────────────────


def probe_psd_manifest(path: str, asset_id: str, revision: str = ""
                       ) -> PsdDocumentManifest:
    """Probe a PSD/PSB document into the immutable manifest.

    Runs on a CPU worker (never the UI thread); raises when psd-tools is
    unavailable so the host can surface the dependency diagnosis.
    """
    if PSDImage is None:  # pragma: no cover - environment guard
        raise RuntimeError(f"psd-tools {PSD_TOOLS_REQUIRED_VERSION} is required for layered PSD import")
    psd, file_revision = _open_psd(path)
    entries: List[PsdLayerEntry] = []
    _walk_psd_tree(psd, (), entries)
    bit_depth = int(getattr(psd, "depth", 8) or 8)
    color_mode = psd.color_mode.name
    profile = psd.image_resources.get_data(Resource.ICC_PROFILE)
    return PsdDocumentManifest(
        asset_id=asset_id, revision=revision or file_revision,
        width=int(psd.width), height=int(psd.height),
        color_mode=color_mode, bit_depth=bit_depth,
        has_merged_image=psd.has_preview(),
        layers=tuple(entries),
        has_color_profile=Resource.ICC_PROFILE in psd.image_resources,
        color_profile_srgb=bool(profile and color_mode == "RGB" and _is_srgb_profile(profile)),
    )


@lru_cache(maxsize=8)
def _is_srgb_profile(profile: bytes) -> bool:
    from src.shared.media.icc_rgb import icc_rgb_to_srgb

    samples = np.linspace(0.0, 1.0, 6, dtype=np.float32)
    rgb = np.stack(np.meshgrid(samples, samples, samples), axis=-1).reshape(1, -1, 3)
    try:
        converted = icc_rgb_to_srgb(rgb, profile)
    except (RuntimeError, ValueError):
        return False
    return bool(np.allclose(rgb, converted, atol=1.0 / 255.0, rtol=0.0))


def prepare_psd_drop(payloads: list[MediaDropPayload]) -> list[MediaDropPayload]:
    """Probe every PSD before the host publishes any of the drop."""
    prepared = []
    for payload in payloads:
        if not payload.path.lower().endswith((".psd", ".psb")):
            prepared.append(payload)
            continue
        asset_id = payload.asset_id
        if asset_id.startswith("path:"):
            asset_id = "psd-" + hashlib.sha256(os.fsencode(payload.path)).hexdigest()[:24]
        manifest = probe_psd_manifest(payload.path, asset_id)
        if manifest.bit_depth not in (1, 8, 16) or (manifest.bit_depth == 16 and manifest.color_mode != "RGB"):
            raise ValueError(f"{payload.display_name}: unsupported {manifest.bit_depth}-bit {manifest.color_mode} PSD")
        prepared.append(replace(
            payload, asset_id=asset_id, width=manifest.width, height=manifest.height,
            extras={**payload.extras, "psd_manifest": manifest},
        ))
    return prepared


def _walk_psd_tree(group: Any, path: Tuple[str, ...],
                   out: List[PsdLayerEntry]) -> None:
    # psd-tools iterates background first; the managed stack wants the
    # foreground first, preserving the input file's visual stacking.
    for layer in reversed(group):
        is_group = bool(layer.is_group())
        if is_group:
            out.append(_entry_for(layer, path, is_group=True))
            _walk_psd_tree(layer, (*path, layer.name), out)
        else:
            out.append(_entry_for(layer, path, is_group=False))


def _entry_for(layer: Any, path: Tuple[str, ...], *, is_group: bool
               ) -> PsdLayerEntry:
    blend = layer.blend_mode.name.lower()
    passthrough = is_group and blend == "pass_through"
    adjustment = isinstance(layer, AdjustmentLayer)
    smart = layer.kind == "smartobject"
    clipped = layer.clipping
    styles = _read_layer_styles(layer)
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
        opacity=int(layer.opacity),
        blend_mode="passthrough" if passthrough else blend,
        is_group=is_group,
        group_passthrough=passthrough,
        adjustment=adjustment,
        smart_object=smart,
        clipped=clipped,
        has_effects=bool(styles),
        fill_opacity=int(layer.fill_opacity),
        masked=layer.has_mask() or layer.has_vector_mask(),
        kind=layer.kind,
        styles=styles,
    )


def _read_layer_styles(layer: Any) -> tuple[LayerStyleEffect, ...]:
    """Read descriptors directly so unsupported effects survive provenance."""
    effects = layer.effects
    tags = (Tag.OBJECT_BASED_EFFECTS_LAYER_INFO,
            Tag.OBJECT_BASED_EFFECTS_LAYER_INFO_V0, Tag.OBJECT_BASED_EFFECTS_LAYER_INFO_V1)
    descriptor = next((layer.tagged_blocks.get_data(tag) for tag in tags
                       if tag in layer.tagged_blocks), None)
    if descriptor is None:
        unreadable_tags = [tag.value.decode("ascii") for tag in tags if tag in layer.tagged_blocks]
        if unreadable_tags:
            return (LayerStyleEffect("unreadable_effects", 0, source_description=", ".join(unreadable_tags)),)
        return ((LayerStyleEffect("legacy_effects", 0, source_description="lrFX"),)
                if Tag.EFFECTS_LAYER in layer.tagged_blocks else ())
    if not isinstance(descriptor, Descriptor):
        return (LayerStyleEffect("unreadable_effects", 0, source_description=repr(descriptor)),)
    if not effects.enabled:
        return ()

    known = {id(effect.descriptor): effect for effect in effects.items}
    styles = []
    order_by_kind = {STYLE_KIND_COLOR_OVERLAY: 0, STYLE_KIND_OUTER_GLOW: 1, STYLE_KIND_DROP_SHADOW: 2}
    for item in descriptor.values():
        for entry in item if isinstance(item, DescriptorList) else (item,):
            if not isinstance(entry, Descriptor) or not bool(entry.get(b"present", False)):
                continue
            if not bool(entry.get(Key.Enabled, False)):
                continue
            style_kind = {
                Klass.SolidFill: STYLE_KIND_COLOR_OVERLAY,
                Klass.DropShadow: STYLE_KIND_DROP_SHADOW,
                Klass.OuterGlow: STYLE_KIND_OUTER_GLOW,
            }.get(entry.classID)
            source = repr(entry)
            effect = known.get(id(entry))
            params = _convert_style_params(effect, style_kind, effects.scale)
            if params is None:
                styles.append(LayerStyleEffect(
                    f"unsupported:{entry.classID!r}", len(styles), source_description=source))
            else:
                styles.append(LayerStyleEffect(style_kind, order_by_kind[style_kind], params, source))
    return tuple(styles)


def _convert_style_params(effect: Any, kind: str | None, scale: float) -> dict[str, Any] | None:
    if kind is None or effect is None:
        return None
    try:
        color = effect.color
        if color is None or color.classID != Klass.RGBColor:
            return None
        rgb = tuple(float(color[key]) / 255.0 for key in (Key.Red, Key.Green, Key.Blue))
        opacity = float(effect.opacity) / 100.0
        if not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in (*rgb, opacity)):
            return None
        blend = effect.blend_mode
        if blend != BlendMode.NORMAL and not (
                (kind == STYLE_KIND_DROP_SHADOW and blend == BlendMode.MULTIPLY and rgb == (0.0, 0.0, 0.0))
                or (kind == STYLE_KIND_OUTER_GLOW and blend == BlendMode.SCREEN and rgb == (1.0, 1.0, 1.0))):
            return None
        params: dict[str, Any] = {"color": rgb, "opacity": opacity}
        if kind == STYLE_KIND_COLOR_OVERLAY:
            return params
        if effect.choke or effect.noise or not _has_linear_contour(effect.contour):
            return None
        # Photoshop's outer-glow blur descriptor contains the resultant
        # document pixel radius even when the layer's overall FX scale
        # differs from 100%. Keep unverified shadow scale variants merged.
        radius = effect.size
        if not 0.0 <= radius <= 128.0:
            return None
        params["radius"] = radius
        if kind == STYLE_KIND_DROP_SHADOW:
            if scale != 100.0:
                return None
            angle = math.radians(effect.angle)
            distance = effect.distance
            params.update(offset_x=-distance * math.cos(angle), offset_y=distance * math.sin(angle))
            if not all(-512.0 <= params[key] <= 512.0 for key in ("offset_x", "offset_y")):
                return None
        else:
            if (effect.gradient or effect.quality_jitter
                    or effect.glow_type not in (None, GlowTechnique.SOFT_MATTE)
                    or effect.quality_range not in (0.0, 50.0)):
                return None
            params["range"] = effect.quality_range / 100.0 if effect.quality_range else 1.0
        return params
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
        return None


def _has_linear_contour(contour: Any) -> bool:
    if contour is None:
        return True
    points = contour.get(Key.Curve, ())
    return len(points) == 2 and all(
        float(point[Key.Horizontal]) == float(point[Key.Vertical]) == expected
        for point, expected in zip(points, (0.0, 255.0))
    )


def read_psd_document_rgba(path: str) -> tuple[np.ndarray, int]:
    """Read a flattened RGB PSD/PSB in straight-alpha, normalized RGB.

    A real merged preview preserves the originating renderer's compositing.
    Documents without that preview are flattened by psd-tools at stored
    precision. The returned bit depth belongs to these decoded pixels.
    """
    if PSDImage is None:  # pragma: no cover
        raise RuntimeError("psd-tools is required for PSD/PSB decode")
    psd, _ = _open_psd(path)
    depth = psd.depth
    if depth not in (1, 8, 16):
        raise ValueError("merged RGBA decode requires 1-, 8- or 16-bit PSD/PSB")
    if psd.color_mode.name != "RGB":
        if depth == 16:
            raise ValueError("16-bit merged PSD/PSB decode requires RGB")
        image = psd.composite(apply_icc=True)
        return np.asarray(image.convert("RGBA"), dtype=np.float32) / 255.0, 8
    if psd.has_preview():
        from psd_tools.api.utils import get_transparency_index, has_transparency

        merged = psd.numpy()
        rgb = merged[..., :3]
        if has_transparency(psd):
            index = get_transparency_index(psd)
            index = index if index >= 0 else 3
            alpha = merged[..., index:index + 1]
        else:
            alpha = np.ones((*rgb.shape[:2], 1), dtype=np.float32)
    else:
        from psd_tools.composite import composite

        rgb, _, alpha = composite(psd, viewport=(0, 0, psd.width, psd.height))
    profile = psd.image_resources.get_data(Resource.ICC_PROFILE)
    if profile:
        from src.shared.media.icc_rgb import icc_rgb_to_srgb

        try:
            rgb = icc_rgb_to_srgb(rgb, profile)
        except RuntimeError:
            if depth != 8:
                raise
            image = psd.composite(apply_icc=True)
            return np.asarray(image.convert("RGBA"), dtype=np.float32) / 255.0, 8
    return np.concatenate((rgb, alpha), axis=2), depth


def read_psd_layer_rgba(psd_path: str, entry: PsdLayerEntry
                        ) -> np.ndarray:
    """Decode one layer's pixels as RGBA (alpha separate, not premultiplied).

    Alpha, fill and layer opacity stay separate values; compositing applies
    them exactly once (B07 §3 — no double alpha multiplication).
    """
    if PSDImage is None:  # pragma: no cover
        raise RuntimeError("psd-tools is required for layered PSD decode")
    psd, _ = _open_psd(psd_path)
    target = _find_layer_by_selector(psd, entry.selector)
    if target is None:
        raise LookupError(
            f"PSD layer {entry.selector} not found in {psd_path!r}")
    return _read_layer_pixels(psd, target, entry.bbox)


def _read_layer_pixels(psd: Any, target: Any, bbox: tuple[int, int, int, int]) -> np.ndarray:
    if psd.color_mode.name != "RGB" or psd.depth not in (8, 16):
        raise ValueError("layer RGBA decode requires an 8- or 16-bit RGB PSD/PSB")
    left, top, right, bottom = bbox
    if left >= right or top >= bottom:
        return np.zeros((0, 0, 4), dtype=np.float32)
    if target.is_group():
        from psd_tools.composite import composite

        # The viewport includes the document-space origin. Composite only
        # the group's children so the group opacity stays separate.
        color, _, alpha = composite(target, viewport=bbox)
        return np.concatenate((color, alpha), axis=2)
    # PIL quantizes 16-bit RGB. Native channels retain the stored precision
    # and leave the resulting straight coverage and source color intact.
    pixels = target.numpy()
    if pixels is None:
        return np.zeros((0, 0, 4), dtype=np.float32)
    if pixels.shape[2] == 3:
        alpha = np.ones((*pixels.shape[:2], 1), dtype=np.float32)
        return np.concatenate((pixels, alpha), axis=2)
    return pixels[..., :4]


def _find_layer_by_selector(root: Any, selector: tuple[str, str]) -> Optional[Any]:
    """Locate a layer by PSD id (or fingerprint fallback) — never by name."""
    def walk(group: Any, path: Tuple[str, ...]) -> Iterator[Tuple[Any, Tuple[str, ...]]]:
        for layer in group:
            yield layer, path
            if layer.is_group():
                yield from walk(layer, (*path, layer.name))

    matches = []
    for layer, path in walk(root, ()):
        if selector[0] == "id":
            accepted = str(layer.layer_id) == selector[1]
        else:
            accepted = _entry_for(layer, path, is_group=layer.is_group()).fingerprint == selector[1]
        if accepted:
            matches.append(layer)
            if len(matches) > 1:
                raise LookupError(f"PSD layer selector {selector!r} is ambiguous")
    return matches[0] if matches else None


def make_psd_layer_reference(entry: PsdLayerEntry, manifest: PsdDocumentManifest) -> dict[str, Any]:
    """Persist a layer selector and project metadata independently of the file path."""
    return {
        "version": PSD_LAYER_REFERENCE_VERSION,
        "selector": list(entry.selector),
        "revision": manifest.revision,
        "structure_path": list(entry.structure_path),
        "bbox": list(entry.bbox),
        "fill_opacity": entry.fill_opacity,
        "styles": [style.to_dict() for style in entry.styles],
    }


def read_psd_canvas_layer_rgba(path: str, reference: dict[str, Any]) -> tuple[np.ndarray, int]:
    """Decode a selected PSD layer in document coordinates with straight Alpha."""
    if PSDImage is None:
        raise RuntimeError("psd-tools is required for layered PSD decode")
    if reference.get("version") != PSD_LAYER_REFERENCE_VERSION:
        raise ValueError("Invalid PSD layer reference")
    psd, _ = _open_psd(path)
    if psd.color_mode.name != "RGB" or psd.depth not in (8, 16):
        raise ValueError("Layered PSD/PSB decode requires 8- or 16-bit RGB")
    from psd_tools.api.utils import resolve_alloc_budget

    budget = resolve_alloc_budget(psd.max_alloc_bytes)
    if budget is not None and psd.width * psd.height * 16 > budget:
        raise ValueError("The PSD/PSB layer canvas exceeds the image allocation limit")
    profile = psd.image_resources.get_data(Resource.ICC_PROFILE)
    if profile and not _is_srgb_profile(profile):
        raise ValueError("Layered PSD decode requires an sRGB-compatible source")
    if "merged_selectors" in reference:
        selections = reference["merged_selectors"]
        if not isinstance(selections, list) or not selections:
            raise ValueError("Invalid PSD merge range")
        return _read_psd_range_rgba(psd, selections, profile), psd.depth
    target = _resolve_psd_selector(psd, reference.get("selector"))
    fill = int(reference.get("fill_opacity", 255))
    if not 0 <= fill <= 255:
        raise ValueError("Invalid PSD layer fill opacity")
    canvas = np.zeros((psd.height, psd.width, 4), dtype=np.float32)
    bbox = tuple(target.bbox)
    placement = clip_psd_patch(bbox, (psd.width, psd.height))
    if placement is not None:
        pixels = _read_layer_pixels(psd, target, bbox)
        (x, y), slices = placement
        patch = pixels[slices]
        h, w = patch.shape[:2]
        canvas[y:y + h, x:x + w] = patch
        if profile:
            from src.shared.media.icc_rgb import icc_rgb_to_srgb

            canvas[y:y + h, x:x + w, :3] = icc_rgb_to_srgb(patch[..., :3], profile)
        canvas[y:y + h, x:x + w, 3] *= fill / 255
    return canvas, psd.depth


def _resolve_psd_selector(psd: Any, selector: Any) -> Any:
    if (not isinstance(selector, (tuple, list)) or len(selector) != 2
            or selector[0] not in ("id", "fp") or not isinstance(selector[1], str)):
        raise ValueError("Invalid PSD layer selector")
    target = _find_layer_by_selector(psd, (selector[0], selector[1]))
    if target is None:
        raise LookupError(f"PSD layer selector {selector!r} is missing")
    return target


def _read_psd_range_rgba(psd: Any, selectors: list[Any], profile: bytes | None) -> np.ndarray:
    from psd_tools.composite import composite

    selected = {_resolve_psd_selector(psd, selector) for selector in selectors}

    def include(layer: Any) -> bool:
        if not layer.is_visible():
            return False
        current = layer
        while current is not psd and current is not None:
            if current in selected:
                return True
            current = current.parent
        return False

    rgb, _, alpha = composite(psd, viewport=(0, 0, psd.width, psd.height), layer_filter=include)
    if profile:
        from src.shared.media.icc_rgb import icc_rgb_to_srgb

        rgb = icc_rgb_to_srgb(rgb, profile)
    return np.concatenate((rgb, alpha), axis=2)
