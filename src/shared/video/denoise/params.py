"""视频降噪参数定义（时域 + 空域）。

对应 docs/video_denoise_implementation_plan.md 第 4 节参数规格。

参数对象是不可变的 dataclass，跨线程传递安全（UI 线程构建、渲染线程读取）。
序列化格式与图片降噪（source_bake）完全独立，使用项目顶层 "video_denoise" 字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, Tuple

# ---------------------------------------------------------------------------
# 枚举常量（避免引入 Qt 依赖，纯字符串，便于项目序列化）
# ---------------------------------------------------------------------------

SPATIAL_MODES = ("fast", "better", "enhanced")
MOTION_QUALITIES = ("fast", "balanced", "accurate")
QUALITY_MODES = ("preview", "paused", "export")

_TEMPORAL_FRAMES_CHOICES = (1, 2, 3)


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else (high if value > high else value)


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.lower() in ("1", "true", "yes", "on")
    return default


# ---------------------------------------------------------------------------
# 空域参数（单帧处理）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpatialDenoiseParams:
    """空域降噪参数，作用于单帧。"""

    enabled: bool = False
    mode: str = "fast"          # fast / better / enhanced
    radius: int = 3             # 1-10，采样半径（像素）
    luma: float = 0.0           # 0-100，亮度降噪强度
    chroma: float = 0.0         # 0-100，色度降噪强度
    threshold: float = 0.5      # 0-1，边缘保护阈值（越大越保留边缘）
    detail_recovery: float = 0.5  # 0-1，细节恢复强度

    def sanitized(self) -> "SpatialDenoiseParams":
        mode = self.mode if self.mode in SPATIAL_MODES else "fast"
        radius = min(max(_as_int(self.radius, 3), 1), 10)
        return replace(
            self,
            enabled=_as_bool(self.enabled, False),
            mode=mode,
            radius=radius,
            luma=_clamp(_as_float(self.luma, 0.0), 0.0, 100.0),
            chroma=_clamp(_as_float(self.chroma, 0.0), 0.0, 100.0),
            threshold=_clamp(_as_float(self.threshold, 0.5), 0.0, 1.0),
            detail_recovery=_clamp(_as_float(self.detail_recovery, 0.5), 0.0, 1.0),
        )

    def is_active(self) -> bool:
        p = self.sanitized()
        return p.enabled and (p.luma > 0.5 or p.chroma > 0.5)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "radius": self.radius,
            "luma": self.luma,
            "chroma": self.chroma,
            "threshold": self.threshold,
            "detail_recovery": self.detail_recovery,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SpatialDenoiseParams":
        if not isinstance(data, dict):
            return cls()
        return cls(
            enabled=data.get("enabled", False),
            mode=data.get("mode", "fast"),
            radius=data.get("radius", 3),
            luma=data.get("luma", 0.0),
            chroma=data.get("chroma", 0.0),
            threshold=data.get("threshold", 0.5),
            detail_recovery=data.get("detail_recovery", 0.5),
        ).sanitized()


# ---------------------------------------------------------------------------
# 时域参数（多帧处理）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemporalDenoiseParams:
    """时域降噪参数，需要视频渲染上下文（邻帧/历史帧）。"""

    enabled: bool = False
    frames: int = 2               # 1 / 2 / 3，累积历史深度
    luma: float = 0.0             # 0-100
    chroma: float = 0.0           # 0-100
    motion: str = "fast"          # fast / balanced / accurate
    threshold: float = 0.5        # 0-1，颜色相似度阈值（超过则不累积）
    blend: float = 0.8            # 0-1，历史混合比例上限
    scene_cut_sensitivity: float = 0.5  # 0-1，场景切换检测灵敏度

    def sanitized(self) -> "TemporalDenoiseParams":
        motion = self.motion if self.motion in MOTION_QUALITIES else "fast"
        frames = _as_int(self.frames, 2)
        if frames not in _TEMPORAL_FRAMES_CHOICES:
            frames = min(max(frames, 1), 3)
        return replace(
            self,
            enabled=_as_bool(self.enabled, False),
            frames=frames,
            motion=motion,
            luma=_clamp(_as_float(self.luma, 0.0), 0.0, 100.0),
            chroma=_clamp(_as_float(self.chroma, 0.0), 0.0, 100.0),
            threshold=_clamp(_as_float(self.threshold, 0.5), 0.0, 1.0),
            blend=_clamp(_as_float(self.blend, 0.5), 0.1, 1.0),
            scene_cut_sensitivity=_clamp(
                _as_float(self.scene_cut_sensitivity, 0.5), 0.0, 1.0
            ),
        )

    def is_active(self) -> bool:
        p = self.sanitized()
        return p.enabled and (p.luma > 0.5 or p.chroma > 0.5)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "frames": self.frames,
            "luma": self.luma,
            "chroma": self.chroma,
            "motion": self.motion,
            "threshold": self.threshold,
            "blend": self.blend,
            "scene_cut_sensitivity": self.scene_cut_sensitivity,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TemporalDenoiseParams":
        if not isinstance(data, dict):
            return cls()
        _default = cls()
        return cls(
            enabled=data.get("enabled", False),
            frames=data.get("frames", _default.frames),
            luma=data.get("luma", _default.luma),
            chroma=data.get("chroma", _default.chroma),
            motion=data.get("motion", _default.motion),
            threshold=data.get("threshold", _default.threshold),
            blend=data.get("blend", _default.blend),
            scene_cut_sensitivity=data.get(
                "scene_cut_sensitivity", _default.scene_cut_sensitivity
            ),
        ).sanitized()


# ---------------------------------------------------------------------------
# 组合参数 + 质量模式
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VideoDenoiseParams:
    """视频降噪总参数。quality_mode 由渲染上下文决定（preview/paused/export）。"""

    temporal: TemporalDenoiseParams = field(default_factory=TemporalDenoiseParams)
    spatial: SpatialDenoiseParams = field(default_factory=SpatialDenoiseParams)

    def sanitized(self) -> "VideoDenoiseParams":
        return VideoDenoiseParams(
            temporal=self.temporal.sanitized(),
            spatial=self.spatial.sanitized(),
        )

    def is_active(self) -> bool:
        return self.temporal.is_active() or self.spatial.is_active()

    # -- 质量模式裁剪（计划 4.3 节） -------------------------------------
    # Preview: 前向 1-2 帧 / fast 运动估计 / fast 空域（半径受限）
    # Paused : 前后 2-3 帧 / balanced / better
    # Export : 前后 3-5 帧 / accurate / enhanced

    def for_quality(self, quality_mode: str) -> "VideoDenoiseParams":
        """按处理质量模式返回裁剪后的有效参数。"""
        p = self.sanitized()
        if quality_mode == "preview":
            temporal = replace(
                p.temporal,
                frames=min(p.temporal.frames, 2),
                motion="fast",
            )
            spatial = replace(
                p.spatial,
                mode="fast",
                radius=min(p.spatial.radius, 4),
            )
        elif quality_mode == "paused":
            temporal = replace(
                p.temporal,
                frames=min(p.temporal.frames, 3),
                motion="balanced" if p.temporal.motion == "fast" else p.temporal.motion,
            )
            spatial = replace(
                p.spatial,
                mode="better" if p.spatial.mode == "fast" else p.spatial.mode,
            )
        else:  # export
            temporal = replace(p.temporal, frames=3, motion="accurate")
            spatial = replace(p.spatial, mode="enhanced")
        return VideoDenoiseParams(temporal=temporal, spatial=spatial)

    # -- 序列化 ----------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "temporal": self.temporal.to_dict(),
            "spatial": self.spatial.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VideoDenoiseParams":
        if not isinstance(data, dict):
            return cls()
        return cls(
            temporal=TemporalDenoiseParams.from_dict(data.get("temporal", {})),
            spatial=SpatialDenoiseParams.from_dict(data.get("spatial", {})),
        ).sanitized()

    def parameters_key(self) -> Tuple:
        """参数哈希键：用于时域历史失效判断（参数变化必须重置历史）。"""
        p = self.sanitized()
        return (
            p.temporal.to_dict().__str__(),
            p.spatial.to_dict().__str__(),
        )
