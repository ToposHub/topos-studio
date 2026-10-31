"""Delivery profiles for Motion Graphic clips (MG-11).

MG-11 removes the hardcoded ``1920x1080`` assumption baked into the system
prompt and the scene builder.  A :class:`DeliveryProfile` captures everything
that varies by destination: pixel size, fps, aspect, safe area, subtitle
region, codec/encoder hints, plus platform-specific constraints (max duration
for shorts, loudness target, etc.).

The profile is *data*; the planner reads it to lay out the scene inside the
safe area, the renderer reads it to pick the resolution, and the export job
reads it to pick the codec.  Keeping it in one typed object means "produce a
9:16 version" is a profile switch, not a rewrite of the scene.

Design rules:

* closed vocabulary of platform / codec identifiers so the LLM cannot invent
  one;
* safe area / subtitle region expressed as fractions (0..0.5) so the same
  profile works at any resolution;
* responsive layout helper produces element transforms that respect the
  profile, so the same brief can be re-rendered at 16:9, 9:16 and 1:1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .brief import (
    CANVAS_1_1,
    CANVAS_16_9,
    CANVAS_9_16,
    DEFAULT_CANVAS_SIZES,
    SUPPORTED_CANVAS_ASPECTS,
)


# --------------------------------------------------------------------------- #
# Codecs / containers
# --------------------------------------------------------------------------- #


CODEC_H264 = "h264"
CODEC_H265 = "h265"
CODEC_PRORES = "prores"
CODEC_VP9 = "vp9"
CODEC_AV1 = "av1"
SUPPORTED_CODECS: frozenset[str] = frozenset({
    CODEC_H264, CODEC_H265, CODEC_PRORES, CODEC_VP9, CODEC_AV1,
})

CONTAINER_MP4 = "mp4"
CONTAINER_MOV = "mov"
CONTAINER_WEBM = "webm"
SUPPORTED_CONTAINERS: frozenset[str] = frozenset({CONTAINER_MP4, CONTAINER_MOV, CONTAINER_WEBM})


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SafeArea:
    """Margins (fractions of width / height) reserved as safe area.

    ``title_safe`` is the inner region where titles must stay; ``action_safe``
    is the larger region where important action should stay.  Both are pairs
    of (dx, dy) fractions in ``0..0.5``.
    """

    title_safe_x: float = 0.04
    title_safe_y: float = 0.04
    action_safe_x: float = 0.02
    action_safe_y: float = 0.02

    def __post_init__(self) -> None:
        for name in ("title_safe_x", "title_safe_y", "action_safe_x", "action_safe_y"):
            value = getattr(self, name)
            if not 0.0 <= value <= 0.5:
                raise ValueError(f"SafeArea.{name} must be in [0, 0.5]")

    def title_rect(self, width: int, height: int) -> tuple[int, int, int, int]:
        """Return ``(x, y, w, h)`` of the title-safe rectangle in pixels."""
        dx = int(round(width * self.title_safe_x))
        dy = int(round(height * self.title_safe_y))
        return (dx, dy, width - 2 * dx, height - 2 * dy)

    def to_dict(self) -> dict[str, float]:
        return {
            "title_safe_x": self.title_safe_x,
            "title_safe_y": self.title_safe_y,
            "action_safe_x": self.action_safe_x,
            "action_safe_y": self.action_safe_y,
        }


@dataclass(frozen=True)
class SubtitleRegion:
    """Where subtitles may sit, expressed as fractions of the canvas.

    ``bottom_margin`` is the gap from the bottom; ``max_width`` is the widest
    a subtitle box should grow.  Both are fractions of height / width.
    """

    bottom_margin: float = 0.08
    max_width: float = 0.86
    align: str = "center"           # left / center / right

    def __post_init__(self) -> None:
        if not 0.0 <= self.bottom_margin <= 0.4:
            raise ValueError("SubtitleRegion.bottom_margin must be in [0, 0.4]")
        if not 0.0 <= self.max_width <= 1.0:
            raise ValueError("SubtitleRegion.max_width must be in [0, 1.0]")
        if self.align not in {"left", "center", "right"}:
            raise ValueError(f"Unknown subtitle align: {self.align!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "bottom_margin": self.bottom_margin,
            "max_width": self.max_width,
            "align": self.align,
        }


@dataclass(frozen=True)
class DeliveryProfile:
    """One target delivery specification (MG-11).

    Attributes:
        profile_id: Stable identifier.
        name: Human label ("YouTube 横屏 1080p")。
        canvas_aspect: One of :data:`SUPPORTED_CANVAS_ASPECTS`.
        width / height: Pixel dimensions.
        fps: Frames per second for the timeline + export.
        safe_area: Title / action safe margins.
        subtitle_region: Where captions may sit.
        codec / container: Output format hints for the export job.
        platform: Destination platform id (links to platform constraints).
        max_duration_seconds: Optional platform cap (e.g. 60s for shorts).
        loudness_lufs: Optional loudness target (e.g. -14 LUFS for web).
        transparent_supported: Whether alpha export is supported here.
        metadata: Extra platform hints (frame stride, color tag, etc.).
    """

    profile_id: str
    name: str
    canvas_aspect: str
    width: int
    height: int
    fps: float = 24.0
    safe_area: SafeArea = field(default_factory=SafeArea)
    subtitle_region: SubtitleRegion = field(default_factory=SubtitleRegion)
    codec: str = CODEC_H264
    container: str = CONTAINER_MP4
    platform: str = "generic"
    max_duration_seconds: float | None = None
    loudness_lufs: float | None = None
    transparent_supported: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.profile_id:
            raise ValueError("DeliveryProfile.profile_id is required")
        if not self.name:
            raise ValueError("DeliveryProfile.name is required")
        if self.canvas_aspect not in SUPPORTED_CANVAS_ASPECTS:
            raise ValueError(f"Unsupported canvas_aspect: {self.canvas_aspect!r}")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("DeliveryProfile.width/height must be positive")
        if self.fps <= 0:
            raise ValueError("DeliveryProfile.fps must be positive")
        if self.codec not in SUPPORTED_CODECS:
            raise ValueError(f"Unsupported codec: {self.codec!r}")
        if self.container not in SUPPORTED_CONTAINERS:
            raise ValueError(f"Unsupported container: {self.container!r}")
        if self.max_duration_seconds is not None and self.max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive")

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height

    def duration_frames(self, seconds: float) -> int:
        return max(1, int(round(seconds * self.fps)))

    def title_safe_rect(self) -> tuple[int, int, int, int]:
        return self.safe_area.title_rect(self.width, self.height)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "canvas_aspect": self.canvas_aspect,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "safe_area": self.safe_area.to_dict(),
            "subtitle_region": self.subtitle_region.to_dict(),
            "codec": self.codec,
            "container": self.container,
            "platform": self.platform,
            "max_duration_seconds": self.max_duration_seconds,
            "loudness_lufs": self.loudness_lufs,
            "transparent_supported": self.transparent_supported,
            "metadata": dict(self.metadata),
        }


# --------------------------------------------------------------------------- #
# Built-in profiles
# --------------------------------------------------------------------------- #


def _profile(
    profile_id: str,
    name: str,
    aspect: str,
    *,
    platform: str = "generic",
    **kwargs: Any,
) -> DeliveryProfile:
    width, height = DEFAULT_CANVAS_SIZES[aspect]
    base = dict(
        profile_id=profile_id,
        name=name,
        canvas_aspect=aspect,
        width=width,
        height=height,
        platform=platform,
    )
    base.update(kwargs)
    return DeliveryProfile(**base)


BUILTIN_PROFILES: tuple[DeliveryProfile, ...] = (
    _profile("youtube_1080p_landscape", "YouTube 1080p 横屏", CANVAS_16_9, platform="youtube", fps=30.0),
    _profile("youtube_1080p_portrait", "YouTube Shorts 1080×1920", CANVAS_9_16, platform="shorts", fps=30.0, max_duration_seconds=60.0),
    _profile("instagram_square", "Instagram 1:1", CANVAS_1_1, platform="instagram", fps=30.0),
    _profile("instagram_reel", "Instagram Reel 9:16", CANVAS_9_16, platform="instagram", fps=30.0, max_duration_seconds=90.0),
    _profile("web_1080p", "Web 通用 1080p", CANVAS_16_9, platform="web"),
    _profile("presentation_1080p", "演示 1080p", CANVAS_16_9, platform="presentation", fps=24.0),
    _profile(
        "prores_4k_landscape", "ProRes 4K 横屏", CANVAS_16_9,
        platform="web", width=3840, height=2160, fps=24.0,
        codec=CODEC_PRORES, container=CONTAINER_MOV, transparent_supported=True,
    ),
)


def builtin_profile(profile_id: str) -> DeliveryProfile:
    for profile in BUILTIN_PROFILES:
        if profile.profile_id == profile_id:
            return profile
    raise KeyError(f"Unknown built-in delivery profile: {profile_id!r}")


# --------------------------------------------------------------------------- #
# Profile registry
# --------------------------------------------------------------------------- #


class DeliveryProfileRegistry:
    """Append-only registry of delivery profiles.

    The registry starts with the built-in profiles; the host may register
    project-specific profiles (e.g. a brand's house 2.35:1 spec).  Profiles
    are keyed by id; re-registering the same id replaces (for upgrades) but
    keeps the old version available through :meth:`version_of`.
    """

    def __init__(self, extras: tuple[DeliveryProfile, ...] = ()) -> None:
        self._profiles: dict[str, DeliveryProfile] = {}
        # Superseded (older) profiles per id, oldest first (AUD-26802).
        self._versions: dict[str, tuple[DeliveryProfile, ...]] = {}
        for profile in BUILTIN_PROFILES:
            self._install(profile)
        for profile in extras:
            self._install(profile)

    def _install(self, profile: DeliveryProfile) -> None:
        """Insert/replace a profile, recording any displaced older version."""

        displaced = self._profiles.get(profile.profile_id)
        if displaced is not None:
            self._versions[profile.profile_id] = (
                self._versions.get(profile.profile_id, ()) + (displaced,)
            )
        self._profiles[profile.profile_id] = profile

    def register(self, profile: DeliveryProfile) -> None:
        self._install(profile)

    def version_of(self, profile_id: str) -> tuple[DeliveryProfile, ...]:
        """Return the superseded (older) versions of ``profile_id``, oldest first.

        Empty when the profile was never re-registered or is unknown.
        """

        return self._versions.get(profile_id, ())

    def get(self, profile_id: str) -> DeliveryProfile | None:
        return self._profiles.get(profile_id)

    def require(self, profile_id: str) -> DeliveryProfile:
        profile = self.get(profile_id)
        if profile is None:
            raise KeyError(f"Unknown delivery profile: {profile_id!r}")
        return profile

    def all_profiles(self) -> tuple[DeliveryProfile, ...]:
        return tuple(self._profiles.values())

    def profiles_for_aspect(self, aspect: str) -> tuple[DeliveryProfile, ...]:
        return tuple(p for p in self._profiles.values() if p.canvas_aspect == aspect)


# --------------------------------------------------------------------------- #
# Responsive layout helper
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ResponsiveLayout:
    """Result of mapping a design intent onto a delivery profile.

    The planner produces a *normalized* design (fractions of canvas, in
    [0, 1]); this helper multiplies by the profile's pixel size and clamps
    inside the title-safe rect so the same design renders correctly on every
    aspect.
    """

    profile: DeliveryProfile

    def pixel_rect(
        self,
        norm_x: float,
        norm_y: float,
        norm_w: float,
        norm_h: float,
        *,
        clamp_to_safe: bool = True,
    ) -> tuple[int, int, int, int]:
        width = self.profile.width
        height = self.profile.height
        x = int(round(norm_x * width))
        y = int(round(norm_y * height))
        w = int(round(norm_w * width))
        h = int(round(norm_h * height))
        if clamp_to_safe:
            sx, sy, sw, sh = self.profile.title_safe_rect()
            x = max(sx, min(x, sx + sw - w))
            y = max(sy, min(y, sy + sh - h))
        return (x, y, w, h)


__all__ = [
    "DeliveryProfile",
    "DeliveryProfileRegistry",
    "SafeArea",
    "SubtitleRegion",
    "ResponsiveLayout",
    "BUILTIN_PROFILES",
    "SUPPORTED_CODECS",
    "SUPPORTED_CONTAINERS",
    "CODEC_H264",
    "CODEC_H265",
    "CODEC_PRORES",
    "CODEC_VP9",
    "CODEC_AV1",
    "CONTAINER_MP4",
    "CONTAINER_MOV",
    "CONTAINER_WEBM",
    "builtin_profile",
]
