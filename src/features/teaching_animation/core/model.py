"""Pure data models for Teaching Animation scenes and host references.

The JSON scene is the source of truth.  Renderers, Edit clips and Composite
nodes only hold references to this model; they never embed HTML or duplicate a
scene document.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import copy
import json
import math
from typing import Any, Mapping
import uuid

from src.shared.keyframe import KeyframeTrack

from .html_layer import FreeformHtmlMotionLayer

SCENE_SCHEMA_VERSION = 3
PAYLOAD_SCHEMA_VERSION = 1
SUPPORTED_ELEMENT_TYPES = frozenset({"text", "formula", "shape", "image", "svg", "chart", "draw", "frame"})

# WP-3 layout contract: how a frame arranges its children in the DOM overlay.
# ``absolute`` keeps per-child local x/y; the flex/grid modes place children as
# CSS flow items.  ``fill``/``hug`` size policies let a child stretch along its
# container or shrink to its own content.
LAYOUT_MODES = frozenset({"absolute", "flex-row", "flex-column", "grid"})
SIZE_POLICIES = frozenset({"fixed", "fill", "hug"})
LAYOUT_JUSTIFY = frozenset({"start", "center", "end", "between", "around", "evenly"})
LAYOUT_ALIGN = frozenset({"start", "center", "end", "stretch"})

#: Scene fields that must deserialize from JSON arrays.  A corrupted project
#: file with a non-array value here fails loudly instead of silently loading
#: an empty scene (AUD-27641).
_SCENE_COLLECTION_FIELDS: tuple[str, ...] = (
    "pages", "elements", "animations", "markers", "asset_refs", "cues", "expressions",
)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _copy_json(value: Any) -> Any:
    """Copy JSON-compatible values without sharing mutable scene state."""
    return copy.deepcopy(value)


@dataclass(frozen=True)
class SceneTransform:
    """Canvas layout and transform in scene coordinates."""

    x: float = 0.0
    y: float = 0.0
    width: float = 100.0
    height: float = 100.0
    scale_x: float = 1.0
    scale_y: float = 1.0
    rotation: float = 0.0

    def __post_init__(self) -> None:
        values = (self.x, self.y, self.width, self.height,
                  self.scale_x, self.scale_y, self.rotation)
        if not all(isinstance(value, (int, float)) and math.isfinite(float(value))
                   for value in values):
            raise ValueError("SceneTransform values must be finite numbers")
        if self.width < 0.0 or self.height < 0.0:
            raise ValueError("SceneTransform width and height cannot be negative")
        if self.scale_x <= 0.0 or self.scale_y <= 0.0:
            raise ValueError("SceneTransform scales must be positive")

    def to_dict(self) -> dict[str, float]:
        return {
            "x": float(self.x),
            "y": float(self.y),
            "width": float(self.width),
            "height": float(self.height),
            "scale_x": float(self.scale_x),
            "scale_y": float(self.scale_y),
            "rotation": float(self.rotation),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "SceneTransform":
        values = data or {}
        return cls(
            x=float(values.get("x", 0.0)),
            y=float(values.get("y", 0.0)),
            width=float(values.get("width", 100.0)),
            height=float(values.get("height", 100.0)),
            scale_x=float(values.get("scale_x", 1.0)),
            scale_y=float(values.get("scale_y", 1.0)),
            rotation=float(values.get("rotation", 0.0)),
        )


@dataclass(frozen=True)
class SceneAssetRef:
    """Portable reference to an external scene asset."""

    asset_id: str
    kind: str = "image"
    path: str = ""
    fallback: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.asset_id:
            raise ValueError("asset_id cannot be empty")
        if not self.kind:
            raise ValueError("asset kind cannot be empty")
        if self.path.startswith("/"):
            raise ValueError("scene asset paths must be relative, not absolute")

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "kind": self.kind,
            "path": self.path,
            "fallback": self.fallback,
            "metadata": _copy_json(dict(self.metadata)),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SceneAssetRef":
        return cls(
            asset_id=str(data.get("asset_id", "")),
            kind=str(data.get("kind", "image")),
            path=str(data.get("path", "")),
            fallback=data.get("fallback"),
            metadata=_copy_json(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class SceneElement:
    """A structured, editable scene element.

    AUD-27644（只读约定）：本 dataclass 是 frozen，但 ``style`` /
    ``layout`` 等 ``Mapping`` 字段的 dict 本体仍是可变的；frozen 只冻结
    字段绑定。**约定：Mapping 字段只读**——任何修改必须走
    ``dataclasses.replace`` + 全新 dict，不得原地
    ``element.style["k"] = ...`` / ``element.style["k"].update(...)``。
    原地改动会同时穿透 undo/revision 栈中共享同一 dict 引用的所有历史
    快照（``with_element_updated`` 经 ``replace`` 传递的是原引用），
    且无异常、极难定位。构造/序列化路径已统一 deepcopy（``_copy_json``）。
    """

    element_id: str
    type: str
    z_index: int = 0
    visible: bool = True
    transform: SceneTransform = field(default_factory=SceneTransform)
    # AUD-27644: 只读约定（见类 docstring）——不要原地改写 dict 内容。
    style: Mapping[str, Any] = field(default_factory=dict)
    layout: Mapping[str, Any] = field(default_factory=dict)
    content: Any = ""
    asset_ref: str | None = None
    template_id: str | None = None
    local_animation_tracks: tuple[KeyframeTrack, ...] = ()
    # Owning page (artboard).  Empty means "first page" and is normalized on
    # document construction, so legacy scenes and quick constructions migrate.
    page_id: str = ""

    def __post_init__(self) -> None:
        if not self.element_id:
            raise ValueError("element_id cannot be empty")
        if self.type not in SUPPORTED_ELEMENT_TYPES:
            raise ValueError(f"Unsupported scene element type: {self.type}")
        if not isinstance(self.z_index, int):
            raise TypeError("z_index must be an integer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "element_id": self.element_id,
            "type": self.type,
            "z_index": self.z_index,
            "visible": self.visible,
            "transform": self.transform.to_dict(),
            "style": _copy_json(dict(self.style)),
            "layout": _copy_json(dict(self.layout)),
            "content": _copy_json(self.content),
            "asset_ref": self.asset_ref,
            "template_id": self.template_id,
            "local_animation_tracks": [
                track.to_dict() for track in self.local_animation_tracks
            ],
            "page_id": self.page_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SceneElement":
        tracks = tuple(
            KeyframeTrack.from_dict(track_data)
            for track_data in data.get("local_animation_tracks", [])
            if isinstance(track_data, Mapping)
        )
        layout = data.get("layout", {})
        return cls(
            element_id=str(data.get("element_id", "")),
            type=str(data.get("type", "text")),
            z_index=int(data.get("z_index", 0)),
            visible=bool(data.get("visible", True)),
            transform=SceneTransform.from_dict(data.get("transform")),
            style=_copy_json(data.get("style", {})),
            layout=_copy_json(layout if isinstance(layout, Mapping) else {}),
            content=_copy_json(data.get("content", "")),
            asset_ref=data.get("asset_ref"),
            template_id=data.get("template_id"),
            local_animation_tracks=tracks,
            page_id=str(data.get("page_id", "")),
        )


@dataclass(frozen=True)
class SceneAnimation:
    """One frame-based animation track attached to an element property."""

    animation_id: str
    element_id: str
    property: str
    track: KeyframeTrack

    def __post_init__(self) -> None:
        if not self.animation_id or not self.element_id:
            raise ValueError("animation_id and element_id cannot be empty")
        if not self.property:
            raise ValueError("animation property cannot be empty")
        if self.track.param_name and self.track.param_name != self.property:
            raise ValueError("track param_name must match animation property")

    def to_dict(self) -> dict[str, Any]:
        track_data = self.track.to_dict()
        track_data.update({
            "animation_id": self.animation_id,
            "element_id": self.element_id,
            "property": self.property,
        })
        return track_data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SceneAnimation":
        property_name = str(data.get("property", data.get("param_name", "")))
        track_data = dict(data)
        track_data["param_name"] = property_name
        return cls(
            animation_id=str(data.get("animation_id", _new_id("animation"))),
            element_id=str(data.get("element_id", "")),
            property=property_name,
            track=KeyframeTrack.from_dict(track_data),
        )


@dataclass(frozen=True)
class SceneMarker:
    """Named frame marker used by editors and AI context providers."""

    marker_id: str
    frame: int
    label: str
    color: str = "#f59e0b"

    def __post_init__(self) -> None:
        if not self.marker_id:
            raise ValueError("marker_id cannot be empty")
        if self.frame < 0:
            raise ValueError("marker frame cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "marker_id": self.marker_id,
            "frame": self.frame,
            "label": self.label,
            "color": self.color,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SceneMarker":
        return cls(
            marker_id=str(data.get("marker_id", _new_id("marker"))),
            frame=int(data.get("frame", 0)),
            label=str(data.get("label", "")),
            color=str(data.get("color", "#f59e0b")),
        )


@dataclass(frozen=True)
class SceneCue:
    """Frame-synchronised narration, subtitle or audio cue reference."""

    cue_id: str
    frame: int
    duration_frames: int = 1
    label: str = ""
    kind: str = "caption"
    asset_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.cue_id:
            raise ValueError("cue_id cannot be empty")
        if self.frame < 0 or self.duration_frames <= 0:
            raise ValueError("cue range is invalid")

    @property
    def end_frame(self) -> int:
        return self.frame + self.duration_frames

    def to_dict(self) -> dict[str, Any]:
        return {"cue_id": self.cue_id, "frame": self.frame, "duration_frames": self.duration_frames,
                "label": self.label, "kind": self.kind, "asset_ref": self.asset_ref}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SceneCue":
        return cls(str(data.get("cue_id", "")), int(data.get("frame", 0)), int(data.get("duration_frames", 1)),
                   str(data.get("label", "")), str(data.get("kind", "caption")), data.get("asset_ref"))


@dataclass(frozen=True)
class SceneExpression:
    """A safe expression bound to one element property."""

    expression_id: str
    element_id: str
    property: str
    source: str
    default: float = 0.0
    seed: int = 0
    version: int = 1
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.expression_id or not self.element_id or not self.property:
            raise ValueError("expression_id, element_id and property cannot be empty")
        if not self.source.strip() or len(self.source) > 4096:
            raise ValueError("expression source must be between 1 and 4096 characters")
        if not math.isfinite(float(self.default)):
            raise ValueError("expression default must be finite")
        if self.version <= 0:
            raise ValueError("expression version must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "expression_id": self.expression_id,
            "element_id": self.element_id,
            "property": self.property,
            "source": self.source,
            "default": float(self.default),
            "seed": int(self.seed),
            "version": int(self.version),
            "enabled": bool(self.enabled),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SceneExpression":
        return cls(
            expression_id=str(data.get("expression_id", "")),
            element_id=str(data.get("element_id", "")),
            property=str(data.get("property", "")),
            source=str(data.get("source", "")),
            default=float(data.get("default", 0.0)),
            seed=int(data.get("seed", 0)),
            version=int(data.get("version", 1)),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass(frozen=True)
class ScenePage:
    """One page (artboard) of a scene; pages partition the timeline in order.

    ``name``/``background`` are authored overrides — an empty name displays as
    "Page N" in the UI and an empty background falls back to the scene
    background.  Pages are additive schema (no version bump, tolerant
    parsing), following the ``metadata["hierarchy"]`` precedent.
    """

    page_id: str
    name: str = ""
    duration_frames: int = 150
    background: str = ""

    def __post_init__(self) -> None:
        if not self.page_id:
            raise ValueError("page_id cannot be empty")
        if not isinstance(self.duration_frames, int) or self.duration_frames < 1:
            raise ValueError("page duration must be a positive frame count")

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_id": self.page_id,
            "name": self.name,
            "duration_frames": self.duration_frames,
            "background": self.background,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ScenePage":
        return cls(
            page_id=str(data.get("page_id", "")),
            name=str(data.get("name", "")),
            duration_frames=max(1, int(data.get("duration_frames", 150))),
            background=str(data.get("background", "")),
        )


@dataclass(frozen=True)
class TeachingSceneDocument:
    """Versioned, serializable source of truth for one teaching scene.

    AUD-27644（只读约定）：``background`` / ``canvas`` / ``metadata`` 是
    frozen 文档上的可变 ``Mapping``——**只读**，改动必须走
    ``replace(...)`` + 新 dict（undo/revision 快照经 ``replace`` 共享
    同一 dict 引用，原地改写会污染全部历史快照）。
    """

    scene_id: str
    schema_version: int
    name: str
    width: int
    height: int
    fps_num: int
    fps_den: int
    duration_frames: int
    background: Mapping[str, Any]
    elements: tuple[SceneElement, ...]
    animations: tuple[SceneAnimation, ...]
    pages: tuple[ScenePage, ...] = ()
    markers: tuple[SceneMarker, ...] = ()
    asset_refs: tuple[SceneAssetRef, ...] = ()
    cues: tuple[SceneCue, ...] = ()
    expressions: tuple[SceneExpression, ...] = ()
    # AUD-27644: 只读约定（见类 docstring）——不要原地改写 dict 内容。
    canvas: Mapping[str, Any] = field(default_factory=dict)
    html_layer: FreeformHtmlMotionLayer | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.scene_id or not self.name:
            raise ValueError("scene_id and name cannot be empty")
        if self.schema_version < 1:
            raise ValueError("schema_version must be positive")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("scene dimensions must be positive")
        if self.fps_num <= 0 or self.fps_den <= 0:
            raise ValueError("scene fps must be positive")
        if self.duration_frames <= 0:
            raise ValueError("duration_frames must be positive")
        # Page normalization: always at least one page, unique ids, and every
        # element attached to an existing page.  This is what makes legacy
        # scenes (and bare SceneElement constructions) land on Page 1.
        object.__setattr__(self, "pages", self._normalized_pages())
        self._normalize_element_pages()

    def _normalized_pages(self) -> tuple[ScenePage, ...]:
        if not self.pages:
            if self.html_layer is not None:
                # Legacy HTML decks were saved before pages existed: derive
                # the page list from the layer's detected sections so every
                # consumer (navigation, Layers, timeline) sees them.
                from .pages import sync_pages_with_html_layer
                return sync_pages_with_html_layer(self).pages
            return (ScenePage(page_id="page_1", duration_frames=self.duration_frames),)
        seen: set[str] = set()
        pages: list[ScenePage] = []
        for page in self.pages:
            if page.page_id not in seen:
                seen.add(page.page_id)
                pages.append(page)
        return tuple(pages)

    def _normalize_element_pages(self) -> None:
        page_ids = frozenset(page.page_id for page in self.pages)
        if all(element.page_id in page_ids for element in self.elements):
            return
        fallback = self.pages[0].page_id
        elements = tuple(
            element if element.page_id in page_ids
            else replace(element, page_id=fallback)
            for element in self.elements
        )
        object.__setattr__(self, "elements", elements)

    @property
    def fps(self) -> float:
        return self.fps_num / self.fps_den

    @property
    def duration_seconds(self) -> float:
        return self.duration_frames / self.fps

    def active_cues(self, frame: int) -> tuple[SceneCue, ...]:
        """Return narration/caption cues active at an explicit project frame."""
        return tuple(cue for cue in self.cues if cue.frame <= frame < cue.end_frame)

    @property
    def page_ids(self) -> tuple[str, ...]:
        return tuple(page.page_id for page in self.pages)

    def page_by_id(self, page_id: str) -> ScenePage | None:
        return next((page for page in self.pages if page.page_id == page_id), None)

    def page_start_frames(self) -> tuple[int, ...]:
        """Cumulative start frame of each page in page order."""
        starts: list[int] = []
        cursor = 0
        for page in self.pages:
            starts.append(cursor)
            cursor += page.duration_frames
        return tuple(starts)

    def page_index_for_frame(self, frame: int) -> int:
        """Index of the page owning ``frame``; the last page absorbs the tail."""
        remaining = max(0, int(frame))
        for index, page in enumerate(self.pages):
            if remaining < page.duration_frames or index == len(self.pages) - 1:
                return index
            remaining -= page.duration_frames
        return 0

    def elements_for_page(self, page_id: str) -> tuple[SceneElement, ...]:
        return tuple(element for element in self.elements if element.page_id == page_id)

    def limited_to_page(self, page_id: str) -> "TeachingSceneDocument":
        """A render view containing only ``page_id``'s elements.

        Editor preview and hit-testing use this so the canvas shows one
        artboard at a time; export and render services keep the full document.
        The view also exposes the selected page as its only page so frame
        ownership and page-local range calculations cannot accidentally point
        at a different artboard.
        """
        page = self.page_by_id(page_id)
        if page is None:
            raise ValueError(f"Page not found: {page_id}")
        background = dict(self.background)
        if page.background.strip():
            key = "css" if "css" in background else "color"
            background[key] = page.background.strip()
        return replace(
            self,
            pages=(page,),
            elements=self.elements_for_page(page_id),
            background=background,
        )

    @classmethod
    def create_default(
        cls,
        name: str = "Teaching Scene",
        *,
        scene_id: str | None = None,
        width: int = 1920,
        height: int = 1080,
        fps_num: int = 24,
        fps_den: int = 1,
        duration_frames: int = 240,
    ) -> "TeachingSceneDocument":
        title = SceneElement(
            element_id=_new_id("title"),
            type="text",
            transform=SceneTransform(x=80.0, y=60.0, width=1000.0, height=100.0),
            style={"color": "#f9fafb", "font_size": 48.0},
            content=name,
        )
        return cls(
            scene_id=scene_id or _new_id("scene"),
            schema_version=SCENE_SCHEMA_VERSION,
            name=name,
            width=width,
            height=height,
            fps_num=fps_num,
            fps_den=fps_den,
            duration_frames=duration_frames,
            background={"color": "#111827"},
            elements=(title,),
            animations=(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "schema_version": self.schema_version,
            "name": self.name,
            "width": self.width,
            "height": self.height,
            "fps_num": self.fps_num,
            "fps_den": self.fps_den,
            "duration_frames": self.duration_frames,
            "background": _copy_json(dict(self.background)),
            "canvas": _copy_json(dict(self.canvas)),
            "pages": [page.to_dict() for page in self.pages],
            "elements": [element.to_dict() for element in self.elements],
            "animations": [animation.to_dict() for animation in self.animations],
            "markers": [marker.to_dict() for marker in self.markers],
            "asset_refs": [asset.to_dict() for asset in self.asset_refs],
            "cues": [cue.to_dict() for cue in self.cues],
            "expressions": [expression.to_dict() for expression in self.expressions],
            "html_layer": (self.html_layer.to_dict() if self.html_layer is not None else None),
            "metadata": _copy_json(dict(self.metadata)),
        }

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TeachingSceneDocument":
        from .migration import migrate_scene_json

        migrated = migrate_scene_json(dict(data))
        return cls._from_current_dict(migrated)

    @classmethod
    def _from_current_dict(cls, data: Mapping[str, Any]) -> "TeachingSceneDocument":
        # AUD-27641: corrupted project files must fail loudly, not load empty.
        # ``"elements": {}`` iterates as keys and the per-item Mapping filter
        # silently dropped them all — an open+save cycle then permanently lost
        # the scene content.  Reject non-array collection fields with a domain
        # ValueError, matching the v0 migration path's explicit validation.
        for field_name in _SCENE_COLLECTION_FIELDS:
            value = data.get(field_name, [])
            if not isinstance(value, list):
                raise ValueError(f"scene field {field_name!r} must be an array")
        return cls(
            scene_id=str(data.get("scene_id", "")),
            schema_version=int(data.get("schema_version", SCENE_SCHEMA_VERSION)),
            name=str(data.get("name", "Teaching Scene")),
            width=int(data.get("width", 1920)),
            height=int(data.get("height", 1080)),
            fps_num=int(data.get("fps_num", 24)),
            fps_den=int(data.get("fps_den", 1)),
            duration_frames=int(data.get("duration_frames", 240)),
            background=_copy_json(data.get("background", {"color": "#111827"})),
            canvas=_copy_json(data.get("canvas", {})),
            pages=tuple(
                ScenePage.from_dict(page)
                for page in data.get("pages", [])
                if isinstance(page, Mapping) and page.get("page_id")
            ),
            elements=tuple(
                SceneElement.from_dict(element)
                for element in data.get("elements", [])
                if isinstance(element, Mapping)
            ),
            animations=tuple(
                SceneAnimation.from_dict(animation)
                for animation in data.get("animations", [])
                if isinstance(animation, Mapping)
            ),
            markers=tuple(
                SceneMarker.from_dict(marker)
                for marker in data.get("markers", [])
                if isinstance(marker, Mapping)
            ),
            asset_refs=tuple(
                SceneAssetRef.from_dict(asset)
                for asset in data.get("asset_refs", [])
                if isinstance(asset, Mapping)
            ),
            cues=tuple(
                SceneCue.from_dict(cue)
                for cue in data.get("cues", [])
                if isinstance(cue, Mapping)
            ),
            expressions=tuple(
                SceneExpression.from_dict(expression)
                for expression in data.get("expressions", [])
                if isinstance(expression, Mapping)
            ),
            html_layer=(
                FreeformHtmlMotionLayer.from_dict(data["html_layer"])
                if isinstance(data.get("html_layer"), Mapping)
                else None
            ),
            metadata=_copy_json(data.get("metadata", {})),
        )

    @classmethod
    def from_json(cls, value: str) -> "TeachingSceneDocument":
        parsed = json.loads(value)
        if not isinstance(parsed, Mapping):
            raise TypeError("scene JSON root must be an object")
        return cls.from_dict(parsed)

    def element_by_id(self, element_id: str) -> SceneElement | None:
        return next((element for element in self.elements if element.element_id == element_id), None)

    def with_element_added(self, element: SceneElement, index: int | None = None) -> "TeachingSceneDocument":
        if self.element_by_id(element.element_id) is not None:
            raise ValueError(f"Element already exists: {element.element_id}")
        elements = list(self.elements)
        if index is None:
            elements.append(element)
        else:
            if index < 0 or index > len(elements):
                raise IndexError("element insertion index out of range")
            elements.insert(index, element)
        return replace(self, elements=tuple(elements))

    def with_element_removed(self, element_id: str) -> "TeachingSceneDocument":
        if self.element_by_id(element_id) is None:
            raise ValueError(f"Element not found: {element_id}")
        elements = tuple(element for element in self.elements if element.element_id != element_id)
        animations = tuple(animation for animation in self.animations if animation.element_id != element_id)
        return replace(self, elements=elements, animations=animations)

    def with_element_updated(self, element: SceneElement) -> "TeachingSceneDocument":
        if self.element_by_id(element.element_id) is None:
            raise ValueError(f"Element not found: {element.element_id}")
        return replace(
            self,
            elements=tuple(
                element if current.element_id == element.element_id else current
                for current in self.elements
            ),
        )

    def with_elements_reordered(self, element_id: str, index: int) -> "TeachingSceneDocument":
        current_index = next(
            (position for position, element in enumerate(self.elements)
             if element.element_id == element_id),
            None,
        )
        if current_index is None:
            raise ValueError(f"Element not found: {element_id}")
        if index < 0 or index >= len(self.elements):
            raise IndexError("element reorder index out of range")
        elements = list(self.elements)
        element = elements.pop(current_index)
        elements.insert(index, element)
        return replace(self, elements=tuple(elements))

    def with_duration(self, duration_frames: int) -> "TeachingSceneDocument":
        if duration_frames <= 0:
            raise ValueError("duration_frames must be positive")
        return replace(self, duration_frames=duration_frames)

    def with_marker_added(self, marker: SceneMarker) -> "TeachingSceneDocument":
        if any(item.marker_id == marker.marker_id for item in self.markers):
            raise ValueError(f"Marker already exists: {marker.marker_id}")
        if marker.frame >= self.duration_frames:
            raise ValueError("marker frame must be inside the scene duration")
        return replace(self, markers=self.markers + (marker,))

    def with_marker_removed(self, marker_id: str) -> "TeachingSceneDocument":
        if not any(item.marker_id == marker_id for item in self.markers):
            raise ValueError(f"Marker not found: {marker_id}")
        return replace(self, markers=tuple(item for item in self.markers if item.marker_id != marker_id))

    def with_animation_added(self, animation: SceneAnimation) -> "TeachingSceneDocument":
        if self.element_by_id(animation.element_id) is None:
            raise ValueError(f"Element not found: {animation.element_id}")
        if any(item.animation_id == animation.animation_id for item in self.animations):
            raise ValueError(f"Animation already exists: {animation.animation_id}")
        return replace(self, animations=self.animations + (animation,))

    def with_page_added(self, page: ScenePage, index: int | None = None) -> "TeachingSceneDocument":
        if self.page_by_id(page.page_id) is not None:
            raise ValueError(f"Page already exists: {page.page_id}")
        pages = list(self.pages)
        if index is None:
            pages.append(page)
        else:
            if index < 0 or index > len(pages):
                raise IndexError("page insertion index out of range")
            pages.insert(index, page)
        return replace(self, pages=tuple(pages))

    def with_page_updated(self, page: ScenePage) -> "TeachingSceneDocument":
        if self.page_by_id(page.page_id) is None:
            raise ValueError(f"Page not found: {page.page_id}")
        return replace(
            self,
            pages=tuple(
                page if current.page_id == page.page_id else current
                for current in self.pages
            ),
        )

    def with_page_removed(self, page_id: str) -> "TeachingSceneDocument":
        """Remove a page; its elements fall back to the previous (else next) page."""
        if self.page_by_id(page_id) is None:
            raise ValueError(f"Page not found: {page_id}")
        if len(self.pages) == 1:
            raise ValueError("a scene must keep at least one page")
        index = next(
            position for position, page in enumerate(self.pages)
            if page.page_id == page_id
        )
        fallback = self.pages[index - 1 if index > 0 else index + 1].page_id
        elements = tuple(
            replace(element, page_id=fallback) if element.page_id == page_id else element
            for element in self.elements
        )
        return replace(
            self,
            pages=tuple(page for page in self.pages if page.page_id != page_id),
            elements=elements,
        )

    def with_pages_reordered(self, page_id: str, index: int) -> "TeachingSceneDocument":
        current_index = next(
            (position for position, page in enumerate(self.pages)
             if page.page_id == page_id),
            None,
        )
        if current_index is None:
            raise ValueError(f"Page not found: {page_id}")
        if index < 0 or index >= len(self.pages):
            raise IndexError("page reorder index out of range")
        pages = list(self.pages)
        page = pages.pop(current_index)
        pages.insert(index, page)
        return replace(self, pages=tuple(pages))


@dataclass(frozen=True)
class MotionGraphicClip:
    """Edit timeline instance referencing a scene by ID."""

    clip_id: str
    scene_id: str
    template_id: str = "custom"
    timeline_start: int = 0
    timeline_duration: int = 1
    scene_in_frame: int = 0
    scene_out_frame: int | None = None
    composite_id: str | None = None
    render_cache_id: str | None = None
    source_snapshot: Mapping[str, Any] | None = None
    instance_params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.clip_id or not self.scene_id:
            raise ValueError("clip_id and scene_id cannot be empty")
        if self.timeline_start < 0 or self.timeline_duration <= 0:
            raise ValueError("invalid timeline clip range")
        if self.scene_in_frame < 0:
            raise ValueError("scene_in_frame cannot be negative")
        if self.scene_out_frame is not None and self.scene_out_frame < self.scene_in_frame:
            raise ValueError("scene_out_frame must not precede scene_in_frame")

    @property
    def timeline_end(self) -> int:
        return self.timeline_start + self.timeline_duration

    def to_dict(self) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "scene_id": self.scene_id,
            "template_id": self.template_id,
            "timeline_start": self.timeline_start,
            "timeline_duration": self.timeline_duration,
            "scene_in_frame": self.scene_in_frame,
            "scene_out_frame": self.scene_out_frame,
            "composite_id": self.composite_id,
            "render_cache_id": self.render_cache_id,
            "source_snapshot": _copy_json(self.source_snapshot),
            "instance_params": _copy_json(dict(self.instance_params)),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MotionGraphicClip":
        return cls(
            clip_id=str(data.get("clip_id", "")),
            scene_id=str(data.get("scene_id", "")),
            template_id=str(data.get("template_id", "custom")),
            timeline_start=int(data.get("timeline_start", 0)),
            timeline_duration=int(data.get("timeline_duration", 1)),
            scene_in_frame=int(data.get("scene_in_frame", 0)),
            scene_out_frame=(
                int(data["scene_out_frame"])
                if data.get("scene_out_frame") is not None else None
            ),
            composite_id=data.get("composite_id"),
            render_cache_id=data.get("render_cache_id"),
            source_snapshot=_copy_json(data.get("source_snapshot")),
            instance_params=_copy_json(data.get("instance_params", {})),
        )


@dataclass(frozen=True)
class MotionGraphicNode:
    """Composite graph instance referencing a scene by ID."""

    node_id: str
    scene_id: str
    template_id: str = "custom"
    composite_id: str | None = None
    render_cache_id: str | None = None
    instance_params: Mapping[str, Any] = field(default_factory=dict)
    source_snapshot: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.node_id or not self.scene_id:
            raise ValueError("node_id and scene_id cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "scene_id": self.scene_id,
            "template_id": self.template_id,
            "composite_id": self.composite_id,
            "render_cache_id": self.render_cache_id,
            "instance_params": _copy_json(dict(self.instance_params)),
            "source_snapshot": _copy_json(self.source_snapshot),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MotionGraphicNode":
        return cls(
            node_id=str(data.get("node_id", "")),
            scene_id=str(data.get("scene_id", "")),
            template_id=str(data.get("template_id", "custom")),
            composite_id=data.get("composite_id"),
            render_cache_id=data.get("render_cache_id"),
            instance_params=_copy_json(data.get("instance_params", {})),
            source_snapshot=_copy_json(data.get("source_snapshot")),
        )


@dataclass(frozen=True)
class TeachingAnimationProjectPayload:
    """Independent project payload; it does not alter Edit timeline JSON."""

    schema_version: int = PAYLOAD_SCHEMA_VERSION
    scenes: tuple[TeachingSceneDocument, ...] = ()
    clips: tuple[MotionGraphicClip, ...] = ()
    nodes: tuple[MotionGraphicNode, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scenes": [scene.to_dict() for scene in self.scenes],
            "clips": [clip.to_dict() for clip in self.clips],
            "nodes": [node.to_dict() for node in self.nodes],
            "metadata": _copy_json(dict(self.metadata)),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TeachingAnimationProjectPayload":
        from .migration import migrate_payload_json

        migrated = migrate_payload_json(dict(data))
        return cls(
            schema_version=int(migrated.get("schema_version", PAYLOAD_SCHEMA_VERSION)),
            scenes=tuple(
                TeachingSceneDocument.from_dict(scene)
                for scene in migrated.get("scenes", [])
                if isinstance(scene, Mapping)
            ),
            clips=tuple(
                MotionGraphicClip.from_dict(clip)
                for clip in migrated.get("clips", [])
                if isinstance(clip, Mapping)
            ),
            nodes=tuple(
                MotionGraphicNode.from_dict(node)
                for node in migrated.get("nodes", [])
                if isinstance(node, Mapping)
            ),
            metadata=_copy_json(migrated.get("metadata", {})),
        )

    def scene_by_id(self, scene_id: str) -> TeachingSceneDocument | None:
        return next((scene for scene in self.scenes if scene.scene_id == scene_id), None)
