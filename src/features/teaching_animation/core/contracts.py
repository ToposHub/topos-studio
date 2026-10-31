"""Phase 0 contracts shared by preview, export and host feature facades.

These are deliberately dependency-light.  A scene is represented as JSON at
this stage; Phase 1 will add the validated ``TeachingSceneDocument`` model
without changing the host-facing facade signatures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, TypeAlias

from src.shared.keyframe import KeyframeTrack

SceneJson: TypeAlias = dict[str, Any]


class RenderErrorCode(str, Enum):
    """Stable diagnostic codes for render failures and graceful fallback."""

    CANCELLED = "cancelled"
    INVALID_SCENE = "invalid_scene"
    MISSING_ASSET = "missing_asset"
    MISSING_MODULE = "missing_module"
    UNSUPPORTED_BACKEND = "unsupported_backend"
    RENDER_FAILED = "render_failed"


@dataclass(frozen=True)
class RenderContext:
    """Explicit render inputs shared by preview and export."""

    fps: float = 24.0
    width: int = 1920
    height: int = 1080
    renderer_version: str = "teaching-animation-svg/0.1"
    backend: str = "svg"
    asset_root: str | None = None
    strict_assets: bool = True
    locale: str = "en-US"
    transparent: bool = False
    # Named quality policy shared by preview and export effect resolution.
    # Kept additive so legacy callers retain the preview behaviour.
    render_profile: str = "preview"
    working_color_space: str = "srgb"
    bit_depth: int = 8
    #: Device pixel ratio of the capture surface.  The V1 main chain renders
    #: design-pixels at DPR 1 (physical == CSS pixels); a DPR-aware path
    #: (retina export, proxy tiers) sets this so it participates in the render
    #: surface and cache key — the determinism contract (plan §3.1 / §2.3)
    #: lists DPR as a first-class key term, because the same physical
    #: dimensions at a different DPR rasterise a different CSS layout.
    dpr: float = 1.0
    # Optional host-owned background used only while validating HTML captures.
    # It makes an otherwise fully transparent page observable without changing
    # the scene's authored HTML or its final alpha/export semantics.  Appended
    # after the legacy fields to preserve positional-constructor compatibility.
    html_validation_background: str | None = None
    # Validation is fail-closed and rejects uniform browser captures. Final
    # frame delivery may opt into preserving a legitimate uniform frame (for
    # example a fade-in at frame 0) after the scene has passed that gate.
    allow_uniform_frames: bool = False

    def __post_init__(self) -> None:
        if self.fps <= 0.0:
            raise ValueError("fps must be positive")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("render dimensions must be positive")
        if self.dpr <= 0.0:
            raise ValueError("dpr must be positive")
        if self.render_profile not in {"draft", "interactive", "preview", "final"}:
            raise ValueError("render_profile must be draft, interactive, preview or final")
        if self.working_color_space not in {"srgb", "linear", "rec709", "rec2020", "acescg"}:
            raise ValueError("unsupported working_color_space")
        if self.bit_depth not in {8, 10, 12, 16, 32}:
            raise ValueError("bit_depth must be one of 8, 10, 12, 16 or 32")
        if self.asset_root is not None and not isinstance(self.asset_root, (str, Path)):
            raise TypeError("asset_root must be a path string or None")


@dataclass(frozen=True)
class AnimationFrame:
    """An integer project frame; time is never taken from a wall clock."""

    frame: int
    fps: float

    def __post_init__(self) -> None:
        if self.frame < 0:
            raise ValueError("frame must be non-negative")
        if self.fps <= 0.0:
            raise ValueError("fps must be positive")

    @property
    def seconds(self) -> float:
        return self.frame / self.fps


@dataclass(frozen=True)
class FrameAnimationContract:
    """Deterministic frame/easing contract for scene animation parameters.

    ``KeyframeTrack`` remains the source of truth for interpolation.  The
    adapter only supplies the project frame and default value, ensuring that
    preview and export ask the same track the same question.
    """

    tracks: Mapping[str, KeyframeTrack] = field(default_factory=dict)
    defaults: Mapping[str, float] = field(default_factory=dict)

    def value_at(self, parameter: str, frame: AnimationFrame) -> float:
        track = self.tracks.get(parameter)
        default = float(self.defaults.get(parameter, 0.0))
        if track is None:
            return default
        return track.get_value(frame.frame, default=default)


@dataclass(frozen=True)
class RenderedFrame:
    """Renderer output that can be passed to preview or export adapters."""

    frame: int
    width: int
    height: int
    pixel_format: str
    data: bytes
    backend: str = "svg"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.frame < 0:
            raise ValueError("frame must be non-negative")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("rendered frame dimensions must be positive")
        if not self.pixel_format:
            raise ValueError("pixel_format cannot be empty")
        if not isinstance(self.data, bytes):
            raise TypeError("rendered frame data must be bytes")


@dataclass(frozen=True)
class RenderError:
    """Structured error returned to callers instead of silent black frames."""

    code: RenderErrorCode
    message: str
    frame: int | None = None
    recoverable: bool = False
    fallback_available: bool = False
    cause: str | None = None


class SceneRenderer(Protocol):
    """Minimal renderer contract used by preview and export callers."""

    def prepare(self, scene: SceneJson, context: RenderContext) -> None:
        ...

    def render_frame(self, frame: AnimationFrame) -> RenderedFrame:
        ...

    def cleanup(self) -> None:
        ...


# ---------------------------------------------------------------------------
# W0-B frozen contracts (plan §3.2): the renderer-neutral minimal interface.
#
# These types are deliberately ADDITIVE.  The legacy ``RenderContext`` /
# ``AnimationFrame`` / ``SceneRenderer`` above remain the working preview/export
# path; the SVG renderer is adapted behind ``SceneRendererV2`` via an adapter so
# no existing behaviour changes.  Every frozen contract round-trips through
# ``to_dict``/``from_dict`` and preserves unknown fields (AQ-06).
# ---------------------------------------------------------------------------


class ProxyTier(str, Enum):
    """Render quality tier.  Preview/export share semantics, not necessarily resolution."""

    FINAL = "final"             # export-grade, full effects
    PREVIEW = "preview"         # full resolution, may drop expensive effects
    INTERACTIVE = "interactive" # reduced for live scrubbing
    DRAFT = "draft"             # cheapest proxy


class AlphaMode(str, Enum):
    """Alpha interpretation in the output byte buffer."""

    STRAIGHT = "straight"       # non-premultiplied alpha
    PREMULT = "premult"         # premultiplied alpha


class ColorSpace(str, Enum):
    """Working color space of the rendered pixels.  HDR/PQ deferred to RD-10."""

    SRGB = "srgb"
    LINEAR = "linear"


class Capability(str, Enum):
    """A backend ability a renderer/element/template may require and declare."""

    SVG_RASTER = "svg_raster"
    HTML_RENDERER = "html_renderer"
    GPU_EFFECT = "gpu_effect"
    FONT_PROVIDER = "font_provider"
    ASSET_PROVIDER = "asset_provider"
    MOTION_BLUR = "motion_blur"


def _copy_json(value: Any) -> Any:
    """Copy JSON-compatible values without sharing mutable state."""
    import copy as _copy
    return _copy.deepcopy(value)


@dataclass(frozen=True)
class CanvasSpace:
    """The authoring coordinate system, independent of the output pixel grid.

    Design pixels (CSS px) are the space authors edit in; ``RenderSurface``
    converts them to physical pixels via DPR.  Safe-area insets are in design px
    from each edge (left, top, right, bottom).  Freezing this here is SG-08.
    """

    design_width: float
    design_height: float
    origin: tuple[float, float] = (0.0, 0.0)
    safe_area: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    def __post_init__(self) -> None:
        if self.design_width <= 0.0 or self.design_height <= 0.0:
            raise ValueError("canvas design dimensions must be positive")
        if len(self.origin) != 2 or len(self.safe_area) != 4:
            raise ValueError("origin must be 2-tuple, safe_area must be 4-tuple")
        insets = self.safe_area
        if (insets[0] + insets[2]) >= self.design_width or (insets[1] + insets[3]) >= self.design_height:
            raise ValueError("safe-area insets leave no visible canvas")

    def to_dict(self) -> dict[str, Any]:
        return {
            "design_width": float(self.design_width),
            "design_height": float(self.design_height),
            "origin": [float(self.origin[0]), float(self.origin[1])],
            "safe_area": [float(v) for v in self.safe_area],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "CanvasSpace":
        values = data or {}
        origin = values.get("origin", (0.0, 0.0))
        safe = values.get("safe_area", (0.0, 0.0, 0.0, 0.0))
        return cls(
            design_width=float(values.get("design_width", values.get("width", 1920.0))),
            design_height=float(values.get("design_height", values.get("height", 1080.0))),
            origin=(float(origin[0]), float(origin[1])) if isinstance(origin, (tuple, list)) and len(origin) == 2 else (0.0, 0.0),
            safe_area=tuple(float(v) for v in safe) if isinstance(safe, (tuple, list)) and len(safe) == 4 else (0.0, 0.0, 0.0, 0.0),
        )

    @classmethod
    def from_dimensions(cls, width: int | float, height: int | float) -> "CanvasSpace":
        return cls(design_width=float(width), design_height=float(height))


@dataclass(frozen=True)
class RenderSurface:
    """The physical pixel grid a renderer paints into, plus DPR/color/alpha/proxy.

    ``FrameContext`` carries both ``CanvasSpace`` (design) and ``RenderSurface``
    (physical) so every renderer honours the same coordinate/DPR/alpha contract.
    """

    pixel_width: int
    pixel_height: int
    dpr: float = 1.0
    colorspace: ColorSpace = ColorSpace.SRGB
    alpha_mode: AlphaMode = AlphaMode.STRAIGHT
    proxy_tier: ProxyTier = ProxyTier.FINAL

    def __post_init__(self) -> None:
        if self.pixel_width <= 0 or self.pixel_height <= 0:
            raise ValueError("surface pixel dimensions must be positive")
        if self.dpr <= 0.0:
            raise ValueError("surface DPR must be positive")

    @property
    def physical_pixels(self) -> int:
        return self.pixel_width * self.pixel_height

    def to_dict(self) -> dict[str, Any]:
        return {
            "pixel_width": int(self.pixel_width),
            "pixel_height": int(self.pixel_height),
            "dpr": float(self.dpr),
            "colorspace": self.colorspace.value,
            "alpha_mode": self.alpha_mode.value,
            "proxy_tier": self.proxy_tier.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "RenderSurface":
        values = data or {}

        def _enum(enum_cls: type[Enum], raw: Any, default: Enum) -> Enum:
            try:
                return enum_cls(raw)
            except (TypeError, ValueError):
                return default

        return cls(
            pixel_width=int(values.get("pixel_width", values.get("width", 1920))),
            pixel_height=int(values.get("pixel_height", values.get("height", 1080))),
            dpr=float(values.get("dpr", 1.0)),
            colorspace=_enum(ColorSpace, values.get("colorspace"), ColorSpace.SRGB),  # type: ignore[arg-type]
            alpha_mode=_enum(AlphaMode, values.get("alpha_mode"), AlphaMode.STRAIGHT),  # type: ignore[arg-type]
            proxy_tier=_enum(ProxyTier, values.get("proxy_tier"), ProxyTier.FINAL),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class FrameContext:
    """The single, complete time+surface input to a deterministic evaluator.

    ``frame`` is scene-local and half-open ``[0, duration)``.  ``fps_num`` /
    ``fps_den`` are mandatory rationals — never store a bare float FPS, because
    float rounding breaks frame-identity across preview and export.
    """

    frame: int
    fps_num: int
    fps_den: int
    canvas: CanvasSpace
    surface: RenderSurface
    revision: str = ""
    render_profile_hash: str = ""
    # Passed through to the HTML renderer only for a validation probe.  It is
    # intentionally absent by default so normal preview/export pixels remain
    # unchanged.
    html_validation_background: str | None = None
    # See ``RenderContext.allow_uniform_frames``. Carry this through the
    # renderer-neutral contract so preview, validation and export agree.
    allow_uniform_frames: bool = False

    def __post_init__(self) -> None:
        if self.frame < 0:
            raise ValueError("frame must be non-negative")
        if self.fps_num <= 0 or self.fps_den <= 0:
            raise ValueError("fps_num and fps_den must be positive")

    @property
    def fps(self) -> float:
        return self.fps_num / self.fps_den

    @property
    def time_seconds(self) -> float:
        return self.frame * self.fps_den / self.fps_num

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame": int(self.frame),
            "fps_num": int(self.fps_num),
            "fps_den": int(self.fps_den),
            "canvas": self.canvas.to_dict(),
            "surface": self.surface.to_dict(),
            "revision": self.revision,
            "render_profile_hash": self.render_profile_hash,
            "html_validation_background": self.html_validation_background,
            "allow_uniform_frames": bool(self.allow_uniform_frames),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "FrameContext":
        values = data or {}
        return cls(
            frame=int(values.get("frame", 0)),
            fps_num=int(values.get("fps_num", 24)),
            fps_den=int(values.get("fps_den", 1)),
            canvas=CanvasSpace.from_dict(values.get("canvas")),
            surface=RenderSurface.from_dict(values.get("surface")),
            revision=str(values.get("revision", "")),
            render_profile_hash=str(values.get("render_profile_hash", "")),
            html_validation_background=(
                str(values["html_validation_background"])
                if values.get("html_validation_background") is not None else None
            ),
            allow_uniform_frames=bool(values.get("allow_uniform_frames", False)),
        )

    @classmethod
    def for_scene(
        cls,
        scene: Any,
        frame: int,
        *,
        dpr: float = 1.0,
        proxy_tier: ProxyTier = ProxyTier.FINAL,
        transparent: bool = False,
        revision: str = "",
    ) -> "FrameContext":
        """Build a FrameContext from a TeachingSceneDocument (or its JSON)."""
        width = int(getattr(scene, "width", 1920))
        height = int(getattr(scene, "height", 1080))
        fps_num = int(getattr(scene, "fps_num", 24))
        fps_den = int(getattr(scene, "fps_den", 1))
        canvas = CanvasSpace.from_dimensions(width, height)
        surface = RenderSurface(
            pixel_width=int(round(width * dpr)),
            pixel_height=int(round(height * dpr)),
            dpr=dpr,
            alpha_mode=AlphaMode.PREMULT if transparent else AlphaMode.STRAIGHT,
            proxy_tier=proxy_tier,
        )
        return cls(frame=frame, fps_num=fps_num, fps_den=fps_den, canvas=canvas, surface=surface, revision=revision)


@dataclass(frozen=True)
class RenderEvidence:
    """Provenance recorded for every rendered frame (cache key + diagnostics)."""

    renderer_id: str
    renderer_build: str
    font_set_hash: str = ""
    asset_set_hash: str = ""
    capability_fallback: tuple[str, ...] = ()
    render_duration_ms: float = 0.0
    frame_hash: str = ""
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "renderer_id": self.renderer_id,
            "renderer_build": self.renderer_build,
            "font_set_hash": self.font_set_hash,
            "asset_set_hash": self.asset_set_hash,
            "capability_fallback": list(self.capability_fallback),
            "render_duration_ms": float(self.render_duration_ms),
            "frame_hash": self.frame_hash,
            "diagnostics": _copy_json(dict(self.diagnostics)),
        }


@dataclass(frozen=True)
class RenderPass:
    """One ordered stage of a RenderPlan: source → transform/layout → effect → composite."""

    stage: str                          # source | transform | layout | effect | composite
    target: str = ""                    # element id, layer id, or ""
    capability: Capability | None = None
    params: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "target": self.target,
            "capability": self.capability.value if self.capability else None,
            "params": _copy_json(dict(self.params)),
        }


@dataclass(frozen=True)
class RenderPlan:
    """Renderer-neutral description of how to produce one frame.

    Any backend (SVG/HTML/GPU/Composite) consumes the same plan and reports
    which passes/capabilities it supports, instead of each recomputing scene
    semantics.  ``fail_policy`` decides what happens when a required capability
    is missing (RD-01).
    """

    passes: tuple[RenderPass, ...] = ()
    required_capabilities: tuple[Capability, ...] = ()
    cache_key_inputs: Mapping[str, str] = field(default_factory=dict)
    fail_policy: str = "preview_placeholder"     # preview_placeholder | export_block

    def to_dict(self) -> dict[str, Any]:
        return {
            "passes": [p.to_dict() for p in self.passes],
            "required_capabilities": [c.value for c in self.required_capabilities],
            "cache_key_inputs": _copy_json(dict(self.cache_key_inputs)),
            "fail_policy": self.fail_policy,
        }


@dataclass(frozen=True)
class RendererResult:
    """Output of a SceneRendererV2 render call: RGBA frame plus evidence."""

    frame: RenderedFrame               # pixel_format "rgba8" on the V2 path
    evidence: RenderEvidence


class RendererSession(Protocol):
    """Per-prepare renderer state, closed exactly once by the owning renderer."""

    def close(self) -> None:
        ...


class SceneRendererV2(Protocol):
    """Frozen W0-B renderer contract (plan §3.2).

    Implementations own ALL renderer resources: ``prepare`` creates a session,
    ``render`` produces one deterministic RGBA frame + evidence, ``close``
    releases everything idempotently.  GPU/browser resources are created, used
    and released entirely within this contract — never on the UI thread.
    """

    renderer_id: str
    renderer_build: str

    def prepare(self, scene: SceneJson, context: FrameContext) -> RendererSession:
        ...

    def render(
        self,
        session: RendererSession,
        context: FrameContext,
        cancel_token: Any | None = None,
    ) -> RendererResult:
        ...

    def close(self, session: RendererSession) -> None:
        ...
