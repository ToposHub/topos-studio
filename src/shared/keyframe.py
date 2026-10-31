"""
Keyframe Animation System for Topos Color.

Provides keyframe-based parameter animation support:
- Keyframe: single keyframe with frame number, value, and interpolation type
- KeyframeTrack: collection of keyframes for a single parameter
- Interpolation: linear and bezier curve interpolation

Integration Points:
- ParamDef in base_node.py adds animated flag and keyframes list
- NodeSnapshot carries keyframe data to render thread
- GraphEvaluatorMixin applies interpolation during evaluation
- NodePropertiesPanel shows keyframe editor UI
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple, Type, ClassVar
from enum import Enum
import math
import bisect

from src.shared.extra_fields import preserves_extras, split_extras


class InterpolationType(Enum):
    """Interpolation method between keyframes."""
    LINEAR = "linear"          # Straight line between keyframes
    BEZIER = "bezier"          # Cubic bezier curve (smooth ease-in/out)
    HOLD = "hold"              # Hold previous value (no interpolation)
    EASE_IN = "ease_in"        # Ease in (slow start)
    EASE_OUT = "ease_out"      # Ease out (slow end)
    EASE_IN_OUT = "ease_in_out"  # Ease in and out


@dataclass
@preserves_extras
class Keyframe:
    """Single keyframe in an animation track.

    A keyframe defines the value of a parameter at a specific frame.
    The interpolation type determines how the value changes between
    this keyframe and the next one.

    Attributes:
        frame: Frame number (0-based index in video/sequence).
        value: Parameter value at this frame.
        interpolation: How to interpolate to the next keyframe.
        bezier_in: Bezier control point for incoming tangent (normalized 0-1).
            LEGACY format — kept for backward compatibility with old project
            files. New code prefers ``handle_left`` (absolute data-space).
        bezier_out: Bezier control point for outgoing tangent (normalized 0-1).
            LEGACY format — see ``bezier_in``.
        tangent_mode: Tangent behaviour (auto/smooth/broken/linear/flat/step).
            P1-1: drives how handles are interpreted + edited in the Spline.
        handle_left: Absolute data-space (frame_delta, value_delta) incoming
            handle. P1-1: authoritative on-disk format. None = derive from
            bezier_in or auto-tangent at evaluation time.
        handle_right: Absolute data-space (frame_delta, value_delta) outgoing
            handle. P1-1: see ``handle_left``.
    """
    frame: int
    value: Any
    interpolation: InterpolationType = InterpolationType.LINEAR
    # Bezier control points (relative to keyframe position, normalized 0-1)
    # Used only for BEZIER interpolation. LEGACY — prefer handle_left/right.
    bezier_in: Tuple[float, float] = (0.2, 0.0)   # (dx, dy) normalized
    bezier_out: Tuple[float, float] = (0.8, 1.0)  # (dx, dy) normalized
    # P1-1: authoritative handle format + tangent mode.
    tangent_mode: str = "auto"
    # (frame_delta, value_delta) relative to the key point. None = auto.
    handle_left: Optional[Tuple[float, float]] = None
    handle_right: Optional[Tuple[float, float]] = None
    # DESK-04A：未来 schema 未知键保留（load→save 不丢）
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    # to_dict 全部键 + from_dict 消费键（无 legacy 迁移键——P1-1 的
    # bezier_in/out 旧格式与新 handle_left/right 均为持久化键）
    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "frame", "value", "interpolation",
        "bezier_in", "bezier_out", "tangent_mode",
        "handle_left", "handle_right",
    })

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for save/load.

        P1-1: writes the authoritative handle_left/right + tangent_mode, and
        also keeps bezier_in/out for compatibility with older readers.
        """
        d = {
            "frame": self.frame,
            "value": self.value,
            "interpolation": self.interpolation.value,
            "bezier_in": list(self.bezier_in),
            "bezier_out": list(self.bezier_out),
        }
        # P1-1: authoritative handle + tangent format.
        if self.tangent_mode != "auto":
            d["tangent_mode"] = self.tangent_mode
        if self.handle_left is not None:
            d["handle_left"] = list(self.handle_left)
        if self.handle_right is not None:
            d["handle_right"] = list(self.handle_right)
        # DESK-04A：未知键原样回写
        d.update(self._extra_fields)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Keyframe':
        """Deserialize from dict.

        P1-1: prefers the new handle_left/handle_right + tangent_mode fields.
        Falls back to legacy bezier_in/out (normalized 0-1) when absent, and
        finally to None (auto-tangent computed at evaluation time).
        """
        handle_left = data.get("handle_left")
        handle_right = data.get("handle_right")
        return cls(
            frame=data.get("frame", 0),
            value=data.get("value", 0.0),
            interpolation=InterpolationType(data.get("interpolation", "linear")),
            bezier_in=tuple(data.get("bezier_in", [0.2, 0.0])),
            bezier_out=tuple(data.get("bezier_out", [0.8, 1.0])),
            tangent_mode=data.get("tangent_mode", "auto"),
            handle_left=tuple(handle_left) if handle_left is not None else None,
            handle_right=tuple(handle_right) if handle_right is not None else None,
            _extra_fields=split_extras(data, cls._KNOWN_KEYS),
        )

    def __lt__(self, other: 'Keyframe') -> bool:
        """Sort by frame number."""
        return self.frame < other.frame


@dataclass
@preserves_extras
class KeyframeTrack:
    """Animation track for a single parameter.

    A track is a collection of keyframes sorted by frame number.
    At any frame, the interpolated value is computed from the
    surrounding keyframes.

    Attributes:
        param_name: Name of the parameter this track animates.
        keyframes: List of keyframes (sorted by frame).
        min_val: Minimum parameter value (from ParamDef).
        max_val: Maximum parameter value (from ParamDef).
    """
    param_name: str
    keyframes: List[Keyframe] = field(default_factory=list)
    min_val: float = 0.0
    max_val: float = 1.0
    # Extrapolation outside the first/last key. Kept on the authoritative
    # track so Spline's dashed tails and the renderer share the same policy.
    pre_extrap: str = "constant"
    post_extrap: str = "constant"
    # DESK-04A：未来 schema 未知键保留（load→save 不丢）
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    # to_dict 全部键 + from_dict 消费键（value_type/dimension 由
    # keyframe_track_class 派发层消费，非本类 to_dict 写出，不入列——
    # 高版本写入的 value_type 应作为未知键原样保留）
    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "param_name", "keyframes", "min_val", "max_val",
        "pre_extrap", "post_extrap",
    })

    def time_scaled(self, factor) -> "KeyframeTrack":
        """FE-B01：序列改 FPS 时按比例缩放关键帧帧号（Fraction 精确）。

        降帧率导致多帧合并时保留后值（帧号递增序去重取后者），保持
        曲线形状；起始帧锚定 0 附近不动。
        """
        if not self.keyframes:
            return self
        from dataclasses import replace as _dc_replace
        from fractions import Fraction

        ratio = Fraction(factor).limit_denominator(1000000)

        def _scale_handle(handle):
            # F-R02/R21：绝对 data-space handle 的 x 是 frame_delta，必须
            # 与 key.frame 同倍率缩放，否则 FPS 变换后曲线形状改变
            #（审计实测 0→24 帧 Bezier 24→48 后同秒采样值 .034→.097）。
            if handle is None:
                return None
            dx, dy = handle
            return (float(dx) * float(ratio), dy)

        scaled = [
            _dc_replace(
                key,
                frame=int(round(key.frame * ratio)),
                handle_left=_scale_handle(key.handle_left),
                handle_right=_scale_handle(key.handle_right),
            )
            for key in self.keyframes
        ]
        deduped: list = []
        for key in sorted(scaled, key=lambda k: k.frame):
            if deduped and deduped[-1].frame == key.frame:
                deduped[-1] = key
            else:
                deduped.append(key)
        return _dc_replace(self, keyframes=tuple(deduped))

    def shifted(self, delta: int) -> "KeyframeTrack":
        """C13：整轨关键帧平移 delta 帧（copy-on-write，返回新轨）。

        clip 移动/波纹跟随等时间变换同步平移绝对时间域的关键帧，原首帧
        动画不再留在旧位置（审计实测 A 起点 0→200 后片段首帧 opacity
        从 0 变 1）。
        """
        if delta == 0 or not self.keyframes:
            return self
        from dataclasses import replace as _dc_replace

        return _dc_replace(
            self,
            keyframes=tuple(
                _dc_replace(key, frame=key.frame + delta)
                for key in self.keyframes
            ),
        )

    def add_keyframe(self, keyframe: Keyframe) -> None:
        """Add a keyframe, replacing any existing keyframe at the same frame.

        Duplicate frames used to be allowed (only validate_track() reported
        them), which produced unstable interpolation. Now adding a keyframe at
        a frame that already has one replaces it in place, preserving order.
        """
        # Replace in place if a keyframe at the same frame already exists
        for i, kf in enumerate(self.keyframes):
            if kf.frame == keyframe.frame:
                self.keyframes[i] = keyframe
                return
        self.keyframes.append(keyframe)
        self.keyframes.sort()

    def add_keyframes_bulk(self, keyframes: List[Keyframe]) -> None:
        """P-1: append many keyframes in ONE sort + ONE de-dup pass.

        The per-keyframe ``add_keyframe`` sorts the whole list after every
        append (O(n²log n) for n keyframes). Bulk loading from a saved
        project / a snapshot goes through this instead: extend all → one
        sort → one linear sweep that keeps the LAST keyframe at each frame
        (matching add_keyframe's "replace in place" semantics). This is the
        hot path during playback (the renderer rebuilds tracks from a
        snapshot every frame) so the n²→n log n reduction matters."""
        if not keyframes:
            return
        self.keyframes.extend(keyframes)
        self.keyframes.sort()
        # De-duplicate: keep the last entry at each frame (later in input
        # order wins, matching add_keyframe's replace-in-place). The list is
        # now sorted, so duplicates are adjacent.
        deduped: List[Keyframe] = []
        for kf in self.keyframes:
            if deduped and deduped[-1].frame == kf.frame:
                deduped[-1] = kf  # replace
            else:
                deduped.append(kf)
        self.keyframes = deduped

    def remove_keyframe(self, frame: int) -> bool:
        """Remove keyframe at given frame."""
        for i, kf in enumerate(self.keyframes):
            if kf.frame == frame:
                self.keyframes.pop(i)
                return True
        return False

    def get_keyframe_at(self, frame: int) -> Optional[Keyframe]:
        """Get keyframe exactly at given frame."""
        for kf in self.keyframes:
            if kf.frame == frame:
                return kf
        return None

    def _clamp_range(self, value: float) -> float:
        """Clamp a value to the track's [min_val, max_val] range.

        S2: keeps get_value/get_value_float consistent with the LINEAR branch
        and the module-level interpolate(), which both clamp. Boundaries and
        the no-keyframes default used to skip clamping, which could return
        out-of-range values (e.g. a stored keyframe value above max_val).
        """
        return max(self.min_val, min(self.max_val, value))

    def get_value(self, frame: int, default: float = 0.0) -> float:
        """Get interpolated value at given frame.

        If frame is before first keyframe, return first keyframe value.
        If frame is after last keyframe, return last keyframe value.
        If frame is between two keyframes, interpolate.

        Args:
            frame: Frame number to evaluate.
            default: Default value if no keyframes exist.

        Returns:
            Interpolated parameter value, clamped to [min_val, max_val].
        """
        if not self.keyframes:
            return self._clamp_range(default)

        # Before first keyframe. Exact keyframe values always win; only the
        # range outside the first key uses the selected extrapolation policy.
        if frame < self.keyframes[0].frame:
            if self.pre_extrap == "linear" and len(self.keyframes) >= 2:
                first, second = self.keyframes[:2]
                df = second.frame - first.frame
                if df > 0:
                    slope = (second.value - first.value) / df
                    return self._clamp_range(
                        first.value + slope * (frame - first.frame))
            return self._clamp_range(self.keyframes[0].value)
        if frame == self.keyframes[0].frame:
            return self._clamp_range(self.keyframes[0].value)

        # After last keyframe.
        if frame > self.keyframes[-1].frame:
            if self.post_extrap == "linear" and len(self.keyframes) >= 2:
                previous, last = self.keyframes[-2:]
                df = last.frame - previous.frame
                if df > 0:
                    slope = (last.value - previous.value) / df
                    return self._clamp_range(
                        last.value + slope * (frame - last.frame))
            return self._clamp_range(self.keyframes[-1].value)
        if frame == self.keyframes[-1].frame:
            return self._clamp_range(self.keyframes[-1].value)

        # P-3: find the surrounding keyframe pair by binary search (O(log n))
        # instead of a linear scan (O(n)). keyframes are kept sorted (an
        # invariant maintained by add_keyframe / add_keyframes_bulk), so
        # bisect is safe. This is the per-frame hot path during playback.
        # bisect_right finds the insertion point AFTER any equal frame, so
        # index `i` is the right keyframe of the bracketing pair (kf1=[i-1],
        # kf2=[i]).
        i = bisect.bisect_right(self.keyframes, frame,
                                key=lambda kf: kf.frame)
        if 1 <= i < len(self.keyframes):
            kf1 = self.keyframes[i - 1]
            kf2 = self.keyframes[i]
            return interpolate(frame, kf1, kf2, self.min_val, self.max_val)

        # Should not reach here (the frame-in-range case is handled above; the
        # before-first / after-last cases were handled earlier).
        return self._clamp_range(default)

    def get_value_ramp(
        self,
        start_frame: float,
        fps: float,
        num_samples: int,
        sample_rate: float,
        default: float = 0.0,
    ) -> List[float]:
        """逐样本插值（AM-07 阶段 3.2 自动化精度）。

        返回长度 num_samples 的值列表，每个元素对应该样本对应的帧号处的插值。
        替代此前"每帧取一个标量乘整个 block"的粗粒度做法——
        当一个 block 内 automation 有快速变化时，逐样本 ramp 避免阶跃 click，
        且斜坡段误差 < 0.1dB（计划表 §3.2 验收）。

        Args:
            start_frame: block 起始帧（浮点，允许子帧精度）
            fps: 帧率
            num_samples: block 样本数
            sample_rate: 采样率
            default: 无关键帧时的默认值

        Returns:
            长度 num_samples 的插值值列表
        """
        if not self.keyframes:
            return [default] * num_samples
        if fps <= 0 or sample_rate <= 0 or num_samples <= 0:
            return [default] * num_samples

        # 每样本对应的帧号增量
        frame_per_sample = fps / float(sample_rate)
        result: List[float] = []
        for i in range(num_samples):
            frame_f = start_frame + i * frame_per_sample
            result.append(self.get_value_float(frame_f, default))
        return result

    def get_value_float(self, frame_f: float, default: float = 0.0) -> float:
        """浮点帧号插值（get_value 的浮点版本，供 ramp 用）。

        与 get_value 的区别：接受浮点帧号，做子帧精度插值。
        对 LINEAR/HOLD 直接用浮点 t；对 BEZIER/EASE 用整帧 interpolate
        然后在相邻整帧间线性混合（误差 < 0.1dB，满足 §3.2 验收）。
        """
        if not self.keyframes:
            return self._clamp_range(default)
        # Extrapolation uses the end slope in data space. This also keeps
        # sub-frame sampling consistent with integer-frame evaluation.
        if frame_f < self.keyframes[0].frame:
            if self.pre_extrap == "linear" and len(self.keyframes) >= 2:
                first, second = self.keyframes[:2]
                df = second.frame - first.frame
                if df > 0:
                    slope = (second.value - first.value) / df
                    return self._clamp_range(
                        first.value + slope * (frame_f - first.frame))
            return self._clamp_range(self.keyframes[0].value)
        if frame_f == self.keyframes[0].frame:
            return self._clamp_range(self.keyframes[0].value)
        if frame_f > self.keyframes[-1].frame:
            if self.post_extrap == "linear" and len(self.keyframes) >= 2:
                previous, last = self.keyframes[-2:]
                df = last.frame - previous.frame
                if df > 0:
                    slope = (last.value - previous.value) / df
                    return self._clamp_range(
                        last.value + slope * (frame_f - last.frame))
            return self._clamp_range(self.keyframes[-1].value)
        if frame_f == self.keyframes[-1].frame:
            return self._clamp_range(self.keyframes[-1].value)
        # P-3: binary search for the bracketing pair (O(log n)), same as
        # get_value. keyframes are sorted by invariant.
        i = bisect.bisect_right(self.keyframes, frame_f,
                                key=lambda kf: kf.frame)
        if 1 <= i < len(self.keyframes):
            kf1 = self.keyframes[i - 1]
            kf2 = self.keyframes[i]
            if kf1.frame <= frame_f <= kf2.frame:
                frame_range = kf2.frame - kf1.frame
                if frame_range <= 0:
                    return kf1.value
                t = (frame_f - kf1.frame) / frame_range
                # LINEAR 与 HOLD 可直接用浮点 t 精确计算
                if kf1.interpolation == InterpolationType.LINEAR:
                    value = kf1.value + t * (kf2.value - kf1.value)
                    return max(self.min_val, min(self.max_val, value))
                if kf1.interpolation == InterpolationType.HOLD:
                    return self._clamp_range(kf1.value)
                # BEZIER/EASE：用整帧 interpolate 然后子帧线性混合
                from src.shared.keyframe import interpolate as _interp
                fi = int(frame_f)
                v1 = _interp(fi, kf1, kf2, self.min_val, self.max_val)
                v2 = _interp(fi + 1, kf1, kf2, self.min_val, self.max_val)
                return v1 + (v2 - v1) * (frame_f - fi)
        return self._clamp_range(default)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict."""
        data = {
            "param_name": self.param_name,
            "keyframes": [kf.to_dict() for kf in self.keyframes],
            "min_val": self.min_val,
            "max_val": self.max_val,
        }
        if self.pre_extrap != "constant":
            data["pre_extrap"] = self.pre_extrap
        if self.post_extrap != "constant":
            data["post_extrap"] = self.post_extrap
        # DESK-04A：未知键原样回写
        data.update(self._extra_fields)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'KeyframeTrack':
        """Deserialize from dict.

        S1: route every keyframe through add_keyframe() so duplicate frames
        (legacy/migrated files) are merged in place instead of leaving an
        unaddressable second point that silently skews interpolation.
        """
        track = cls(
            param_name=data.get("param_name", ""),
            min_val=data.get("min_val", 0.0),
            max_val=data.get("max_val", 1.0),
            pre_extrap=data.get("pre_extrap", "constant"),
            post_extrap=data.get("post_extrap", "constant"),
            _extra_fields=split_extras(data, cls._KNOWN_KEYS),
        )
        # P-1: bulk-load via ONE sort + de-dup instead of per-keyframe add
        # (which sorted the list after every append → O(n²log n)). Skip
        # malformed keyframes individually (mirrors the old tolerance).
        bulk: List[Keyframe] = []
        for kf_data in data.get("keyframes", []):
            try:
                bulk.append(Keyframe.from_dict(kf_data))
            except Exception:
                continue
        track.add_keyframes_bulk(bulk)
        return track


@dataclass
class ColorKeyframeTrack:
    """Animation track whose value is one RGBA color.

    Colors are atomic animation values.  A keyframe stores one four-element
    RGBA value and interpolation is performed component-wise at evaluation
    time.  Unlike the old UI aggregation approach, this class serializes one
    track under ``color``/``color2``; there are no four independent channel
    tracks to keep in sync.
    """

    param_name: str
    keyframes: List[Keyframe] = field(default_factory=list)
    min_val: float = 0.0
    max_val: float = 1.0
    pre_extrap: str = "constant"
    post_extrap: str = "constant"
    value_type: str = "color"

    def add_keyframe(self, keyframe: Keyframe) -> None:
        value = keyframe.value
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            raise ValueError("Color keyframes require four RGBA components")
        normalized = Keyframe(
            frame=int(keyframe.frame),
            value=tuple(float(component) for component in value),
            interpolation=keyframe.interpolation,
            bezier_in=keyframe.bezier_in,
            bezier_out=keyframe.bezier_out,
            tangent_mode=keyframe.tangent_mode,
            handle_left=keyframe.handle_left,
            handle_right=keyframe.handle_right,
        )
        for index, existing in enumerate(self.keyframes):
            if existing.frame == normalized.frame:
                self.keyframes[index] = normalized
                return
        self.keyframes.append(normalized)
        self.keyframes.sort()

    def remove_keyframe(self, frame: int) -> bool:
        for index, keyframe in enumerate(self.keyframes):
            if keyframe.frame == frame:
                self.keyframes.pop(index)
                return True
        return False

    def get_keyframe_at(self, frame: int) -> Optional[Keyframe]:
        return next((kf for kf in self.keyframes if kf.frame == frame), None)

    def _component_track(self, component: int) -> KeyframeTrack:
        scalar_keyframes = []
        for keyframe in self.keyframes:
            scalar_keyframes.append(Keyframe(
                frame=keyframe.frame,
                value=float(keyframe.value[component]),
                interpolation=keyframe.interpolation,
                bezier_in=keyframe.bezier_in,
                bezier_out=keyframe.bezier_out,
                tangent_mode=keyframe.tangent_mode,
                handle_left=keyframe.handle_left,
                handle_right=keyframe.handle_right,
            ))
        return KeyframeTrack(
            param_name=f"{self.param_name}[{component}]",
            keyframes=scalar_keyframes,
            min_val=self.min_val,
            max_val=self.max_val,
            pre_extrap=self.pre_extrap,
            post_extrap=self.post_extrap,
        )

    @staticmethod
    def _normalize_default(default: Any) -> Tuple[float, float, float, float]:
        if isinstance(default, (list, tuple)) and len(default) == 4:
            return tuple(float(value) for value in default)
        return (0.0, 0.0, 0.0, 1.0)

    def get_value(self, frame: int,
                  default: Any = (0.0, 0.0, 0.0, 1.0)) -> Tuple[float, ...]:
        fallback = self._normalize_default(default)
        return tuple(self._component_track(index).get_value(
            int(frame), default=fallback[index]) for index in range(4))

    def get_value_float(self, frame: float,
                        default: Any = (0.0, 0.0, 0.0, 1.0)) -> Tuple[float, ...]:
        fallback = self._normalize_default(default)
        return tuple(self._component_track(index).get_value_float(
            float(frame), default=fallback[index]) for index in range(4))

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "param_name": self.param_name,
            "value_type": self.value_type,
            "keyframes": [
                {**kf.to_dict(), "value": list(kf.value)}
                for kf in self.keyframes
            ],
            "min_val": self.min_val,
            "max_val": self.max_val,
        }
        if self.pre_extrap != "constant":
            data["pre_extrap"] = self.pre_extrap
        if self.post_extrap != "constant":
            data["post_extrap"] = self.post_extrap
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ColorKeyframeTrack":
        track = cls(
            param_name=data.get("param_name", ""),
            min_val=float(data.get("min_val", 0.0)),
            max_val=float(data.get("max_val", 1.0)),
            pre_extrap=data.get("pre_extrap", "constant"),
            post_extrap=data.get("post_extrap", "constant"),
        )
        for keyframe_data in data.get("keyframes", []):
            try:
                keyframe = Keyframe.from_dict(keyframe_data)
                track.add_keyframe(keyframe)
            except (TypeError, ValueError, KeyError):
                continue
        return track


@dataclass
class VectorKeyframeTrack:
    """Component-wise animation track for a fixed-size vector value.

    The timeline stores one atomic vec2/vec3 value per keyframe.  Evaluation
    reuses the scalar interpolation implementation for each component, which
    keeps linear/ease/Bezier behaviour identical to scalar tracks.
    """

    param_name: str
    dimension: int = 2
    keyframes: List[Keyframe] = field(default_factory=list)
    min_val: float = 0.0
    max_val: float = 1.0
    pre_extrap: str = "constant"
    post_extrap: str = "constant"

    @property
    def value_type(self) -> str:
        return f"vec{self.dimension}"

    def _normalize_value(self, value: Any) -> Tuple[float, ...]:
        if not isinstance(value, (list, tuple)) or len(value) != self.dimension:
            raise ValueError(
                f"{self.value_type} keyframes require {self.dimension} components")
        result = tuple(float(component) for component in value)
        if not all(math.isfinite(component) for component in result):
            raise ValueError(f"{self.value_type} keyframes require finite components")
        return result

    def add_keyframe(self, keyframe: Keyframe) -> None:
        normalized = Keyframe(
            frame=int(keyframe.frame),
            value=self._normalize_value(keyframe.value),
            interpolation=keyframe.interpolation,
            bezier_in=keyframe.bezier_in,
            bezier_out=keyframe.bezier_out,
            tangent_mode=keyframe.tangent_mode,
            handle_left=keyframe.handle_left,
            handle_right=keyframe.handle_right,
        )
        for index, existing in enumerate(self.keyframes):
            if existing.frame == normalized.frame:
                self.keyframes[index] = normalized
                return
        self.keyframes.append(normalized)
        self.keyframes.sort()

    def remove_keyframe(self, frame: int) -> bool:
        for index, keyframe in enumerate(self.keyframes):
            if keyframe.frame == frame:
                self.keyframes.pop(index)
                return True
        return False

    def get_keyframe_at(self, frame: int) -> Optional[Keyframe]:
        return next((kf for kf in self.keyframes if kf.frame == frame), None)

    def _component_track(self, component: int) -> KeyframeTrack:
        return KeyframeTrack(
            param_name=f"{self.param_name}[{component}]",
            keyframes=[
                Keyframe(
                    frame=kf.frame,
                    value=kf.value[component],
                    interpolation=kf.interpolation,
                    bezier_in=kf.bezier_in,
                    bezier_out=kf.bezier_out,
                    tangent_mode=kf.tangent_mode,
                    handle_left=kf.handle_left,
                    handle_right=kf.handle_right,
                )
                for kf in self.keyframes
            ],
            min_val=self.min_val,
            max_val=self.max_val,
            pre_extrap=self.pre_extrap,
            post_extrap=self.post_extrap,
        )

    def _default_value(self, default: Any) -> Tuple[float, ...]:
        try:
            return self._normalize_value(default)
        except (TypeError, ValueError):
            return tuple(0.0 for _ in range(self.dimension))

    def get_value(self, frame: int,
                  default: Any = (0.0, 0.0)) -> Tuple[float, ...]:
        fallback = self._default_value(default)
        return tuple(self._component_track(index).get_value(
            int(frame), default=fallback[index])
                     for index in range(self.dimension))

    def get_value_float(self, frame: float,
                        default: Any = (0.0, 0.0)) -> Tuple[float, ...]:
        fallback = self._default_value(default)
        return tuple(self._component_track(index).get_value_float(
            float(frame), default=fallback[index])
                     for index in range(self.dimension))

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "param_name": self.param_name,
            "value_type": self.value_type,
            "keyframes": [
                {**kf.to_dict(), "value": list(kf.value)}
                for kf in self.keyframes
            ],
            "min_val": self.min_val,
            "max_val": self.max_val,
        }
        if self.pre_extrap != "constant":
            data["pre_extrap"] = self.pre_extrap
        if self.post_extrap != "constant":
            data["post_extrap"] = self.post_extrap
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VectorKeyframeTrack":
        value_type = str(data.get("value_type", "vec2"))
        dimension = int(data.get("dimension", 3 if value_type == "vec3" else 2))
        if dimension not in (2, 3):
            raise ValueError(f"unsupported vector dimension: {dimension}")
        track = cls(
            param_name=str(data.get("param_name", "")),
            dimension=dimension,
            min_val=float(data.get("min_val", 0.0)),
            max_val=float(data.get("max_val", 1.0)),
            pre_extrap=str(data.get("pre_extrap", "constant")),
            post_extrap=str(data.get("post_extrap", "constant")),
        )
        for keyframe_data in data.get("keyframes", []):
            try:
                track.add_keyframe(Keyframe.from_dict(keyframe_data))
            except (TypeError, ValueError, KeyError):
                continue
        return track


@dataclass
class DiscreteKeyframeTrack:
    """Hold track for bool/option parameters.

    Discrete values never interpolate numerically: the preceding keyframe is
    held until the next keyframe's exact frame.  The requested interpolation
    label is still serialized for UI compatibility, but evaluation remains
    deterministic and type-preserving.
    """

    param_name: str
    value_type: str = "bool"
    keyframes: List[Keyframe] = field(default_factory=list)
    pre_extrap: str = "constant"
    post_extrap: str = "constant"

    def add_keyframe(self, keyframe: Keyframe) -> None:
        value = keyframe.value
        if isinstance(value, (list, tuple, dict)) or value is None:
            raise ValueError("discrete keyframes require a scalar value")
        if self.value_type == "bool" and not isinstance(value, bool):
            raise ValueError("bool keyframes require boolean values")
        if self.value_type == "option" and not isinstance(value, str):
            raise ValueError("option keyframes require string values")
        normalized = Keyframe(
            frame=int(keyframe.frame), value=value,
            interpolation=keyframe.interpolation,
            bezier_in=keyframe.bezier_in,
            bezier_out=keyframe.bezier_out,
            tangent_mode=keyframe.tangent_mode,
            handle_left=keyframe.handle_left,
            handle_right=keyframe.handle_right,
        )
        for index, existing in enumerate(self.keyframes):
            if existing.frame == normalized.frame:
                self.keyframes[index] = normalized
                return
        self.keyframes.append(normalized)
        self.keyframes.sort()

    def remove_keyframe(self, frame: int) -> bool:
        for index, keyframe in enumerate(self.keyframes):
            if keyframe.frame == frame:
                self.keyframes.pop(index)
                return True
        return False

    def get_keyframe_at(self, frame: int) -> Optional[Keyframe]:
        return next((kf for kf in self.keyframes if kf.frame == frame), None)

    def _value(self, frame: float, default: Any) -> Any:
        if not self.keyframes:
            return default
        if frame < self.keyframes[0].frame:
            return self.keyframes[0].value
        index = bisect.bisect_right(
            self.keyframes, frame, key=lambda keyframe: keyframe.frame) - 1
        return self.keyframes[max(0, index)].value

    def get_value(self, frame: int, default: Any = False) -> Any:
        return self._value(float(frame), default)

    def get_value_float(self, frame: float, default: Any = False) -> Any:
        return self._value(float(frame), default)

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "param_name": self.param_name,
            "value_type": self.value_type,
            "keyframes": [kf.to_dict() for kf in self.keyframes],
        }
        if self.pre_extrap != "constant":
            data["pre_extrap"] = self.pre_extrap
        if self.post_extrap != "constant":
            data["post_extrap"] = self.post_extrap
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DiscreteKeyframeTrack":
        value_type = str(data.get("value_type", "bool"))
        if value_type not in {"bool", "option"}:
            raise ValueError(f"unsupported discrete value type: {value_type}")
        track = cls(
            param_name=str(data.get("param_name", "")),
            value_type=value_type,
            pre_extrap=str(data.get("pre_extrap", "constant")),
            post_extrap=str(data.get("post_extrap", "constant")),
        )
        for keyframe_data in data.get("keyframes", []):
            try:
                track.add_keyframe(Keyframe.from_dict(keyframe_data))
            except (TypeError, ValueError, KeyError):
                continue
        return track


_VECTOR_VALUE_TYPES = frozenset({"vec2", "vec3"})
_DISCRETE_VALUE_TYPES = frozenset({"bool", "option"})


def keyframe_track_class(value_type: str) -> Type[Any]:
    """Return the track implementation for a parameter/value type."""
    value_type = str(value_type or "scalar")
    if value_type == "color":
        return ColorKeyframeTrack
    if value_type in _VECTOR_VALUE_TYPES:
        return VectorKeyframeTrack
    if value_type in _DISCRETE_VALUE_TYPES:
        return DiscreteKeyframeTrack
    return KeyframeTrack


def make_keyframe_track(param_name: str, value_type: str = "scalar",
                        min_val: float = 0.0, max_val: float = 1.0) -> Any:
    """Create an empty type-aware keyframe track."""
    track_type = str(value_type or "scalar")
    track_cls = keyframe_track_class(track_type)
    if track_cls is VectorKeyframeTrack:
        return track_cls(
            param_name=param_name,
            dimension=3 if track_type == "vec3" else 2,
            min_val=min_val,
            max_val=max_val,
        )
    if track_cls is DiscreteKeyframeTrack:
        return track_cls(param_name=param_name, value_type=track_type)
    return track_cls(param_name=param_name, min_val=min_val, max_val=max_val)


def keyframe_track_from_dict(data: Dict[str, Any],
                             value_type: Optional[str] = None) -> Any:
    """Deserialize a track using an explicit type or its serialized type."""
    resolved_type = str(value_type or data.get("value_type", "scalar"))
    return keyframe_track_class(resolved_type).from_dict(data)


# ── Interpolation Functions ──

def interpolate(frame: int, kf1: Keyframe, kf2: Keyframe,
                min_val: float = 0.0, max_val: float = 1.0) -> float:
    """Interpolate value between two keyframes.

    Args:
        frame: Current frame number.
        kf1: First (earlier) keyframe.
        kf2: Second (later) keyframe.
        min_val: Minimum parameter value (for clamping).
        max_val: Maximum parameter value (for clamping).

    Returns:
        Interpolated value clamped to [min_val, max_val].
    """
    # Exact match
    if frame == kf1.frame:
        return kf1.value
    if frame == kf2.frame:
        return kf2.value

    # Calculate normalized time (0-1 between keyframes)
    frame_range = kf2.frame - kf1.frame
    if frame_range <= 0:
        return kf1.value

    t = (frame - kf1.frame) / frame_range

    # Apply interpolation based on kf1's interpolation type
    if kf1.interpolation == InterpolationType.HOLD:
        # Hold kf1 value until kf2
        return kf1.value

    elif kf1.interpolation == InterpolationType.LINEAR:
        # Simple linear interpolation
        value = kf1.value + t * (kf2.value - kf1.value)

    elif kf1.interpolation == InterpolationType.BEZIER:
        # P1-2: use the authoritative 2D cubic solve so the evaluated value
        # matches the Spline curve the user sees (solves x(t)=frame).
        value = bezier_value_at_frame(frame, kf1, kf2)

    elif kf1.interpolation == InterpolationType.EASE_IN:
        # Ease in: slow start, fast end
        value = ease_in_interpolate(t, kf1, kf2)

    elif kf1.interpolation == InterpolationType.EASE_OUT:
        # Ease out: fast start, slow end
        value = ease_out_interpolate(t, kf1, kf2)

    elif kf1.interpolation == InterpolationType.EASE_IN_OUT:
        # Ease in and out: slow start and end
        value = ease_in_out_interpolate(t, kf1, kf2)

    else:
        # Default to linear
        value = kf1.value + t * (kf2.value - kf1.value)

    # Clamp to valid range
    return max(min_val, min(max_val, value))


def bezier_interpolate(t: float, kf1: Keyframe, kf2: Keyframe) -> float:
    """Cubic bezier interpolation between two keyframes (LEGACY, t-based).

    P1-2: this t-parameterized path is kept for callers that already have a
    normalized t. The authoritative value-at-frame path is
    ``bezier_value_at_frame()`` below, which solves the 2D cubic for x so the
    result matches what the Spline curve shows.

    Uses control points defined in kf1.bezier_out and kf2.bezier_in.
    Control points are normalized (0-1) relative to keyframe interval.

    Args:
        t: Normalized time (0-1 between keyframes).
        kf1: First keyframe (defines bezier_out control point).
        kf2: Second keyframe (defines bezier_in control point).

    Returns:
        Interpolated value.
    """
    # Normalize values to 0-1 range for bezier calculation
    value_range = kf2.value - kf1.value
    if abs(value_range) < 0.0001:
        return kf1.value

    # Control points (normalized coordinates)
    p0 = (0.0, 0.0)
    p1 = kf1.bezier_out
    p2 = kf2.bezier_in
    p3 = (1.0, 1.0)

    # Cubic bezier formula: B(t) = (1-t)^3 * P0 + 3*(1-t)^2*t * P1 + 3*(1-t)*t^2 * P2 + t^3 * P3
    u = 1.0 - t
    y = (u**3 * p0[1] +
         3 * u**2 * t * p1[1] +
         3 * u * t**2 * p2[1] +
         t**3 * p3[1])

    # Convert back to actual value range
    return kf1.value + y * value_range


def bezier_value_at_frame(frame: float, kf1: Keyframe, kf2: Keyframe) -> float:
    """Authoritative 2D cubic-bezier value at an absolute frame (P1-2).

    Builds data-space control points P0..P3 = (frame, value), solves x(t)=frame
    by bisection (handles are x-clamped to the segment so x is monotonic), then
    returns y(t). This is the SAME math the Spline view uses to sample its
    curve, so the editor preview and the render interpolation agree.

    Handle precedence (P1-1): absolute handle_left/handle_right win; otherwise
    legacy bezier_in/out (normalized deltas relative to the interval) are
    converted to data-space; otherwise flat handles are used.
    """
    f0, v0 = float(kf1.frame), float(kf1.value)
    f1, v1 = float(kf2.frame), float(kf2.value)
    df = f1 - f0
    if df <= 0:
        return v0
    dv = v1 - v0

    p1 = _resolve_handle(kf1.handle_right, kf1.bezier_out, f0, v0, df, dv, +1, f0, f1)
    p2 = _resolve_handle(kf2.handle_left, kf2.bezier_in, f1, v1, df, dv, -1, f0, f1)

    # Solve x(t) = frame by bisection. x is monotonic because handle x is
    # clamped to [f0, f1] during editing / resolution.
    lo, hi = 0.0, 1.0
    target = float(frame)
    for _ in range(24):
        mid = (lo + hi) * 0.5
        x = _bezier_x(mid, f0, p1[0], p2[0], f1)
        if x < target:
            lo = mid
        else:
            hi = mid
    t = (lo + hi) * 0.5
    return _bezier_y(t, v0, p1[1], p2[1], v1)


def _resolve_handle(handle: Optional[Tuple[float, float]],
                    legacy: Tuple[float, float],
                    kf_frame: float, kf_value: float,
                    df: float, dv: float, sign: int,
                    seg_lo: float, seg_hi: float) -> Tuple[float, float]:
    """Resolve a handle to absolute (frame, value) data-space coords.

    P1-1: absolute handle deltas win; legacy normalized bezier_in/out are
    converted (they were (normalized_x, normalized_y) in [0,1] relative to the
    interval + value range, with the convention bezier_out points forward and
    bezier_in points backward).

    P1-2: the resolved x is clamped to ``[seg_lo, seg_hi]`` so x(t) stays
    monotonic across the segment. Without this, a handle dragged past the
    neighbour key would make the cubic double back in time and the bisection
    in bezier_value_at_frame() would land on the wrong branch — diverging from
    the Spline view (which also clamps). This mirrors build_segments() in
    spline_model.py.
    """
    if handle is not None:
        # handle is a (frame_delta, value_delta) relative to the key.
        fx = kf_frame + handle[0]
        fy = kf_value + handle[1]
    else:
        # Legacy: normalized in [0,1]. bezier_out ≈ outgoing (forward in
        # time), bezier_in ≈ incoming (backward in time).
        nx, ny = legacy[0], legacy[1]
        if sign > 0:  # outgoing handle (bezier_out)
            fx = kf_frame + nx * df
        else:         # incoming handle (bezier_in)
            fx = kf_frame - (1.0 - nx) * df
        fy = kf_value + (ny - 0.0) * dv
    # Clamp x to the segment so time never reverses (matches Spline draw layer).
    fx = max(seg_lo, min(seg_hi, fx))
    return (fx, fy)


def _bezier_x(t: float, x0: float, x1: float, x2: float, x3: float) -> float:
    u = 1.0 - t
    return (u*u*u*x0 + 3*u*u*t*x1 + 3*u*t*t*x2 + t*t*t*x3)


def _bezier_y(t: float, y0: float, y1: float, y2: float, y3: float) -> float:
    u = 1.0 - t
    return (u*u*u*y0 + 3*u*u*t*y1 + 3*u*t*t*y2 + t*t*t*y3)


def ease_in_interpolate(t: float, kf1: Keyframe, kf2: Keyframe) -> float:
    """Ease in interpolation (quadratic slow start).

    Args:
        t: Normalized time (0-1).
        kf1: First keyframe.
        kf2: Second keyframe.

    Returns:
        Interpolated value.
    """
    # Quadratic ease in: t^2
    eased_t = t * t
    return kf1.value + eased_t * (kf2.value - kf1.value)


def ease_out_interpolate(t: float, kf1: Keyframe, kf2: Keyframe) -> float:
    """Ease out interpolation (quadratic slow end).

    Args:
        t: Normalized time (0-1).
        kf1: First keyframe.
        kf2: Second keyframe.

    Returns:
        Interpolated value.
    """
    # Quadratic ease out: 1 - (1-t)^2
    eased_t = 1.0 - (1.0 - t) * (1.0 - t)
    return kf1.value + eased_t * (kf2.value - kf1.value)


def ease_in_out_interpolate(t: float, kf1: Keyframe, kf2: Keyframe) -> float:
    """Ease in-out interpolation (cubic smooth start and end).

    Args:
        t: Normalized time (0-1).
        kf1: First keyframe.
        kf2: Second keyframe.

    Returns:
        Interpolated value.
    """
    # Cubic ease in-out: smooth acceleration and deceleration
    if t < 0.5:
        eased_t = 4 * t * t * t
    else:
        eased_t = 1.0 - (-2 * t + 2)**3 / 2.0
    return kf1.value + eased_t * (kf2.value - kf1.value)


# ── Utility Functions ──

def create_simple_track(param_name: str, start_frame: int, start_value: float,
                        end_frame: int, end_value: float,
                        interpolation: InterpolationType = InterpolationType.LINEAR,
                        min_val: float = 0.0, max_val: float = 1.0) -> KeyframeTrack:
    """Create a simple two-keyframe track.

    Convenience function for creating a basic animation.

    Args:
        param_name: Parameter name.
        start_frame: Starting frame.
        start_value: Value at start.
        end_frame: Ending frame.
        end_value: Value at end.
        interpolation: Interpolation type.
        min_val: Minimum value.
        max_val: Maximum value.

    Returns:
        KeyframeTrack with two keyframes.
    """
    track = KeyframeTrack(
        param_name=param_name,
        min_val=min_val,
        max_val=max_val,
    )
    track.add_keyframe(Keyframe(start_frame, start_value, interpolation))
    track.add_keyframe(Keyframe(end_frame, end_value, interpolation))
    return track


def validate_track(track: KeyframeTrack) -> List[str]:
    """Validate a keyframe track for errors.

    Checks for:
    - Duplicate keyframes at same frame
    - Keyframes outside parameter range
    - Invalid bezier control points

    Args:
        track: Track to validate.

    Returns:
        List of error messages (empty if valid).
    """
    errors = []

    # Check for duplicates
    frames_seen = set()
    for kf in track.keyframes:
        if kf.frame in frames_seen:
            errors.append(f"Duplicate keyframe at frame {kf.frame}")
        frames_seen.add(kf.frame)

        # Check value range
        if kf.value < track.min_val or kf.value > track.max_val:
            errors.append(
                f"Keyframe at frame {kf.frame} has value {kf.value} "
                f"outside range [{track.min_val}, {track.max_val}]"
            )

        # Check bezier control points
        if kf.interpolation == InterpolationType.BEZIER:
            for cp_name, cp in [("bezier_in", kf.bezier_in), ("bezier_out", kf.bezier_out)]:
                if cp[0] < 0 or cp[0] > 1 or cp[1] < 0 or cp[1] > 1:
                    errors.append(
                        f"Keyframe at frame {kf.frame} has invalid {cp_name} "
                        f"control point {cp} (must be in [0, 1])"
                    )

    return errors


def normalized_segment_controls(kf1: 'Keyframe', kf2: 'Keyframe') -> Optional[Tuple[float, float, float, float]]:
    """CSS ``cubic-bezier(x1,y1,x2,y2)``-compatible controls for one segment.

    Normalizes the authoritative data-space handles of ``bezier_value_at_frame``
    into unit square coordinates: x maps the frame interval to [0,1] (handles
    are already x-clamped to the segment, so x is CSS-legal), y stays unbounded
    because CSS cubic-bezier explicitly allows overshoot outside [0,1].

    Returns ``None`` when the segment cannot be normalized (zero frame span or
    zero value span — the easing is visually irrelevant there), letting the
    caller fall back to ``linear``.
    """
    f0, v0 = float(kf1.frame), float(kf1.value)
    f1, v1 = float(kf2.frame), float(kf2.value)
    df = f1 - f0
    dv = v1 - v0
    if df <= 0.0 or abs(dv) < 1e-9:
        return None
    p1 = _resolve_handle(kf1.handle_right, kf1.bezier_out, f0, v0, df, dv, +1, f0, f1)
    p2 = _resolve_handle(kf2.handle_left, kf2.bezier_in, f1, v1, df, dv, -1, f0, f1)
    x1 = (p1[0] - f0) / df
    x2 = (p2[0] - f0) / df
    y1 = (p1[1] - v0) / dv
    y2 = (p2[1] - v0) / dv
    return (x1, y1, x2, y2)
