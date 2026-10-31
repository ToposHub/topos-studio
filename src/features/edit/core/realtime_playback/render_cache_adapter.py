"""
Render Cache Adapter
渲染缓存管理适配器（规格书 §8 / Stage G）。

职责：
- 为 RealtimePlaybackPolicy / TimelineDecodeScheduler 提供渲染缓存查询。
- 演进现有 RenderCacheManifest / RenderCacheWorker，不重复造缓存系统。
- RenderCacheKey 包含完整 render hash（规格书 §8.1），参数变化时失效。
- 实时播放优先读 render cache，复杂效果/comp clip 不实时硬算。

关键不变量（规格书 §8）：
- 不把 render cache 和 proxy 混为一个状态。
- 没有 hash/version 不复用缓存。
- 不播放时临时同步渲染 composite graph。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# RenderCacheKey（规格书 §8.1）
# =============================================================================


@dataclass(frozen=True)
class RenderCacheKey:
    """
    渲染缓存 key。

    render_hash 必须包含（规格书 §8.1）：
    - clip source range
    - speed/retime
    - transform/crop/opacity
    - effects
    - composite graph id + graph version/hash
    - color stack id + version/hash
    - transition parameters
    - proxy/render resolution

    任何参数变化 → hash 变化 → 旧 cache 失效。
    """

    timeline_id: str
    clip_id: str
    asset_id: str
    source_path: str
    frame_rate_num: int
    frame_rate_den: int
    width: int
    height: int
    render_hash: str
    cache_version: int = 1

    def to_hash(self) -> str:
        """稳定 hash（用于快速比较）。"""
        raw = "|".join([
            self.timeline_id, self.clip_id, self.asset_id, self.source_path,
            str(self.frame_rate_num), str(self.frame_rate_den),
            str(self.width), str(self.height),
            self.render_hash, str(self.cache_version),
        ])
        return hashlib.md5(raw.encode("utf-8")).hexdigest()


def speed_params_signature(speed_params: Any) -> str:
    """A3.1: 速度参数签名（基础速度/插值/速度点结构含 easing）。

    变速点结构（offset/speed/easing）中任何变化都会改变签名，从而使
    render cache 失效。纯标量 speed 也参与签名。

    注意（2026-09-07 复查）：render cache 清单目前只覆盖合成片段（Comp
    Clip）与 Motion Graphic，其失效走 RenderCacheManifest 的
    effect_signature/revisions；本签名是变速片段接入缓存时的现成挂钩，
    尚未在生产路径调用。
    """
    if speed_params is None:
        return ""
    base_speed = getattr(speed_params, "speed", None)
    if base_speed is None:
        # 标量速度（旧模型）
        return f"scalar={float(speed_params):.6f}"
    interpolation = getattr(speed_params, "interpolation", "")
    points = getattr(speed_params, "speed_points", None) or ()
    parts = [
        f"{p.source_offset}:{p.speed:.6f}:{p.easing}"
        for p in points
    ]
    reverse = bool(getattr(speed_params, "reverse", False))
    return (
        f"v={float(base_speed):.6f}:i={interpolation}:r={int(reverse)}"
        f":{len(points)}[" + ",".join(parts) + "]"
    )


def build_render_hash(
    *,
    source_start: int,
    source_duration: int,
    speed: float,
    transform: Any,
    crop: Any,
    opacity: float,
    effects_signature: str = "",
    composite_graph_id: str = "",
    composite_graph_version: int = 0,
    color_stack_id: str = "",
    color_stack_version: int = 0,
    transition_signature: str = "",
    resolution_scale: float = 1.0,
    retime_signature: str = "",
) -> str:
    """
    构建 render hash（规格书 §8.1）。

    包含所有影响渲染输出的参数。任何变化都会改变 hash，使旧 cache 失效。
    """
    parts = [
        f"src={source_start}:{source_duration}",
        f"speed={speed:.6f}",
        f"opacity={opacity:.6f}",
        f"transform={_obj_signature(transform)}",
        f"crop={_obj_signature(crop)}",
        f"effects={effects_signature}",
        f"comp={composite_graph_id}:v{composite_graph_version}",
        f"color={color_stack_id}:v{color_stack_version}",
        f"transition={transition_signature}",
        f"scale={resolution_scale:.4f}",
        f"retime={retime_signature}",
    ]
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()


def build_effects_signature(effects: Any) -> str:
    """Build a stable signature string for a clip's effect chain.

    This is the value callers pass as ``effects_signature`` to
    :func:`build_render_hash`. It must change whenever anything that
    affects the rendered output changes, so the render cache invalidates
    correctly (spec §8.1).

    The signature covers, per effect (sorted by ``order``):
    - ``effect_type``  — which shader/packer runs
    - ``is_enabled``   — disabled effects are skipped at render time
    - ``params``       — static parameter values (sorted by key)
    - ``animated_params`` — full keyframe tracks (frame/value/interpolation),
      so dragging a keyframe invalidates the cache
    - ``order``        — reorder changes which effect runs first

    ``effect_id`` is deliberately **excluded**: two freshly-created
    effects with identical settings should produce the same signature
    (and hit the same cache), regardless of their UUIDs.

    Args:
        effects: an iterable of ``EffectInstance`` (or anything with a
            ``to_dict()``). Tuples, lists, and single instances are all
            accepted; ``None``/empty yields an empty signature.

    Returns:
        A short hex string (md5). Empty input → ``""`` so the existing
        ``build_render_hash(effects_signature="")`` fast path still
        matches effect-free clips.
    """
    if not effects:
        return ""

    # Normalise to a list. A single EffectInstance is iterable-free.
    if hasattr(effects, "to_dict") and not isinstance(effects, (list, tuple)):
        effect_list = [effects]
    else:
        try:
            effect_list = list(effects)
        except TypeError:
            return ""
    if not effect_list:
        return ""

    import json

    # Sort by order so reordering produces a different signature. The
    # tiebreaker chain is (order, effect_type, effect_id): two effects
    # with identical order+type need a deterministic relative order,
    # otherwise the signature would flip depending on tuple iteration
    # order and the cache would thrash. effect_id is used ONLY for sort
    # stability — it is stripped from the serialised data below, so two
    # effects with the same settings still hash equally regardless of id.
    def _sort_key(eff: Any) -> tuple:
        order = getattr(eff, "order", 0)
        etype = getattr(eff, "effect_type", "")
        eid = getattr(eff, "effect_id", "") or (
            eff.get("effect_id", "") if isinstance(eff, dict) else "")
        return (order, etype, eid)

    effect_list.sort(key=_sort_key)

    parts: list[str] = []
    for eff in effect_list:
        if hasattr(eff, "to_dict"):
            try:
                data = eff.to_dict()
            except Exception:  # noqa: BLE001 — never crash the cache key
                data = {}
        elif isinstance(eff, dict):
            data = eff
        else:
            data = {"repr": repr(eff)}
        # Drop the volatile id; keep everything that affects rendering.
        data.pop("effect_id", None)
        parts.append(json.dumps(data, sort_keys=True, default=str))

    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()



def _obj_signature(obj: Any) -> str:
    """把对象转为稳定签名字符串。"""
    if obj is None:
        return "none"
    if hasattr(obj, "to_dict"):
        try:
            import json
            return json.dumps(obj.to_dict(), sort_keys=True, default=str)
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        try:
            import json
            return json.dumps(obj.__dict__, sort_keys=True, default=str)
        except Exception:
            pass
    return str(obj)


# =============================================================================
# RenderCacheEntry
# =============================================================================


@dataclass
class RenderCacheEntry:
    """渲染缓存条目（适配层）。"""

    key: RenderCacheKey
    status: str  # "valid" | "stale" | "rendering" | "failed" | "missing"
    cache_path: Optional[str]
    width: int
    height: int
    start_frame: int
    duration_frames: int
    progress: float = 0.0
    error: Optional[str] = None

    @property
    def is_ready(self) -> bool:
        return self.status == "valid" and self.cache_path is not None

    @property
    def is_rendering(self) -> bool:
        return self.status == "rendering"


# =============================================================================
# RenderCacheAdapter
# =============================================================================


class RenderCacheAdapter:
    """
    渲染缓存查询适配器。

    包装现有 RenderCacheManifest 体系，给 RealtimePlaybackPolicy 提供快速查询。

    实现 RenderCacheStateSnapshot protocol（policy.py）：
        has_render_cache_ready -> bool
        render_cache_path -> str | None

    注意（AUD-17409）：上述全局属性是"任意 clip"语义，仅供诊断/统计；
    policy 的逐 layer 查询必须经 has_render_cache_ready_for /
    render_cache_path_for（SourceResolver 以 _PerClipRenderCacheState
    视图注入 policy），否则多 clip 时间线会把 clip A 的缓存判给 clip B。

    用法：
        adapter = RenderCacheAdapter()
        adapter.register_manifest(clip_id, manifest, render_hash, ...)
        entry = adapter.get_cache_entry(key)
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # clip_id -> RenderCacheEntry（内存索引）
        self._entries: Dict[str, RenderCacheEntry] = {}
        # key_hash -> clip_id（快速反查）
        self._key_index: Dict[str, str] = {}
        # clip_id -> 最后查询的 RenderCacheKey（用于 invalidate 判断）
        self._last_keys: Dict[str, RenderCacheKey] = {}

    # ------------------------------------------------------------------
    # register（从现有 RenderCacheManifest 注册）
    # ------------------------------------------------------------------

    def register_manifest(
        self,
        clip_id: str,
        manifest: Any,  # RenderCacheManifest
        render_hash: str,
        *,
        frame_rate_num: Optional[int] = None,
        frame_rate_den: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> RenderCacheEntry:
        """从现有 RenderCacheManifest 注册一个缓存条目。

        AUD-17410：不再硬编码 NTSC 帧率（24000/1001）与 1080p 默认值。
        fps/尺寸按以下优先级解析，全部失败时抛 ValueError（缺失时报错
        而非静默默认——否则 key 中的 fps 字段形同虚设，get_cache_entry
        的完整 key 比对永远失真）：
        1. 显式参数 frame_rate_num/frame_rate_den/width/height；
        2. manifest.frame_rate_num/frame_rate_den（int）；
        3. manifest.fps（Fraction/有限 float，经 limit_denominator 归一）。
        """
        resolved_num, resolved_den = self._resolve_manifest_frame_rate(
            manifest, frame_rate_num, frame_rate_den,
        )
        resolved_w = self._resolve_manifest_dimension(
            "width", manifest, width,
        )
        resolved_h = self._resolve_manifest_dimension(
            "height", manifest, height,
        )
        key = RenderCacheKey(
            timeline_id=str(getattr(manifest, "timeline_id", "") or ""),
            clip_id=clip_id,
            asset_id="",
            source_path="",
            frame_rate_num=resolved_num,
            frame_rate_den=resolved_den,
            width=resolved_w,
            height=resolved_h,
            render_hash=render_hash,
        )
        entry = RenderCacheEntry(
            key=key,
            status=getattr(manifest, "status", "missing"),
            cache_path=getattr(manifest, "path", None),
            width=resolved_w,
            height=resolved_h,
            start_frame=int(getattr(manifest, "start_frame", 0) or 0),
            duration_frames=int(getattr(manifest, "duration_frames", 0) or 0),
        )
        with self._lock:
            self._entries[clip_id] = entry
            self._key_index[key.to_hash()] = clip_id
            self._last_keys[clip_id] = key
        return entry

    @staticmethod
    def _as_int(value: Any) -> Optional[int]:
        """数值属性 → int（bool/非数值返回 None；Mock 属性自然落 None）。"""
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and float(value).is_integer():
            return int(value)
        return None

    @classmethod
    def _resolve_manifest_frame_rate(
        cls,
        manifest: Any,
        explicit_num: Optional[int],
        explicit_den: Optional[int],
    ) -> Tuple[int, int]:
        num = cls._as_int(explicit_num)
        den = cls._as_int(explicit_den)
        if num is not None and den is not None and num > 0 and den > 0:
            return num, den
        m_num = cls._as_int(getattr(manifest, "frame_rate_num", None))
        m_den = cls._as_int(getattr(manifest, "frame_rate_den", None))
        if m_num is not None and m_den is not None and m_num > 0 and m_den > 0:
            return m_num, m_den
        fps = getattr(manifest, "fps", None)
        if fps is not None:
            try:
                rate = Fraction(fps).limit_denominator(1001000)
                if rate.numerator > 0 and rate.denominator > 0:
                    return rate.numerator, rate.denominator
            except (TypeError, ValueError, ZeroDivisionError, OverflowError):
                pass
        raise ValueError(
            "register_manifest: manifest 未提供可用帧率（frame_rate_num/den "
            "或 fps）；AUD-17410 禁止静默回退硬编码 NTSC 帧率，注册被拒绝"
        )

    @classmethod
    def _resolve_manifest_dimension(
        cls,
        name: str,
        manifest: Any,
        explicit: Optional[int],
    ) -> int:
        value = cls._as_int(explicit)
        if value is None or value <= 0:
            value = cls._as_int(getattr(manifest, name, None))
        if value is None or value <= 0:
            raise ValueError(
                f"register_manifest: manifest 未提供有效 {name}；"
                "AUD-17410 禁止静默回退 1080p 默认值，注册被拒绝"
            )
        return value

    def upsert_entry(self, entry: RenderCacheEntry) -> None:
        with self._lock:
            self._entries[entry.key.clip_id] = entry
            self._key_index[entry.key.to_hash()] = entry.key.clip_id
            self._last_keys[entry.key.clip_id] = entry.key

    # ------------------------------------------------------------------
    # query（policy 热路径）
    # ------------------------------------------------------------------

    @property
    def has_render_cache_ready(self) -> bool:
        """是否有**任意** ready 的 render cache（仅诊断/全局统计用）。

        AUD-17409：policy 逐 layer 查询必须用 has_render_cache_ready_for
        /render_cache_path_for 的按 clip 语义；全局属性会把"任意 clip 就绪"
        误判为"本 clip 就绪"，多 clip 时间线下 layer 会取到其他 clip 的
        缓存路径 → 错误解码源/渲染错帧。
        """
        with self._lock:
            return any(e.is_ready for e in self._entries.values())

    @property
    def render_cache_path(self) -> Optional[str]:
        """返回第一个 ready 的 cache path（仅诊断用，语义见上）。"""
        with self._lock:
            for e in self._entries.values():
                if e.is_ready:
                    return e.cache_path
            return None

    def has_render_cache_ready_for(self, clip_id: str) -> bool:
        """指定 clip 是否有 ready 的 render cache（AUD-17409：policy 语义）。"""
        with self._lock:
            entry = self._entries.get(clip_id)
            return entry is not None and entry.is_ready

    def render_cache_path_for(self, clip_id: str) -> Optional[str]:
        """返回指定 clip 的 ready cache path（无/未就绪返回 None）。"""
        with self._lock:
            entry = self._entries.get(clip_id)
            if entry is not None and entry.is_ready:
                return entry.cache_path
            return None

    def get_cache_entry(self, key: RenderCacheKey) -> Optional[RenderCacheEntry]:
        """按 key 查询 cache entry。

        AUD-17410：失效校验比对完整 key（含 fps、cache_version、
        source_path、timeline_id、asset_id），任一字段变化即失效——
        旧实现只比对 render_hash/width/height，跨工程/换源复用同一
        clip_id 时可能命中陈旧缓存，违反 §8"没有 hash/version 不复用"。
        """
        with self._lock:
            entry = self._entries.get(key.clip_id)
            if entry is None:
                return None
            if entry.key != key:
                return None
            return entry

    def get_cache_entry_for_clip(self, clip_id: str) -> Optional[RenderCacheEntry]:
        """按 clip_id 查询（不做 hash 校验）。"""
        with self._lock:
            return self._entries.get(clip_id)

    def is_cache_ready(self, clip_id: str, key: Optional[RenderCacheKey] = None) -> bool:
        with self._lock:
            entry = self._entries.get(clip_id)
            if entry is None:
                return False
            if key is not None and entry.key != key:
                return False
            return entry.is_ready

    # ------------------------------------------------------------------
    # invalidate
    # ------------------------------------------------------------------

    def invalidate_by_clip(self, clip_id: str, reason: str = "") -> None:
        """按 clip 失效 cache（参数变化时）。"""
        with self._lock:
            entry = self._entries.get(clip_id)
            if entry is not None:
                entry.status = "stale"
                logger.debug(f"Render cache invalidated: clip={clip_id}, reason={reason}")

    def remove_by_clip(self, clip_id: str) -> None:
        with self._lock:
            entry = self._entries.pop(clip_id, None)
            if entry is not None:
                self._key_index.pop(entry.key.to_hash(), None)
            self._last_keys.pop(clip_id, None)

    # ------------------------------------------------------------------
    # open cache source
    # ------------------------------------------------------------------

    def open_cache_source(self, entry: RenderCacheEntry) -> Any:
        """打开 cache 文件用于解码（仅 valid 时）。"""
        if not entry.is_ready or entry.cache_path is None:
            return None
        try:
            from src.shared.media.factory import MediaSourceFactory
            return MediaSourceFactory.create(
                path=entry.cache_path,
                target_size=(entry.width, entry.height),
            )
        except Exception as e:
            logger.warning(f"open_cache_source failed for {entry.cache_path}: {e}")
            return None

    # ------------------------------------------------------------------
    # stats
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, int]:
        with self._lock:
            counts: Dict[str, int] = {}
            for e in self._entries.values():
                counts[e.status] = counts.get(e.status, 0) + 1
            counts["total"] = len(self._entries)
            return counts
