"""P1-COLOR-03: 调色参数关键帧（Grade Keyframes）。

为 Layer 的 grading_params 提供随时间变化的动画轨道：

- 每个可动画参数一条轨道（``GradeKeyframeTrack``），关键帧存储该帧的
  **绝对值**（scalar 或 3 分量 vector），帧号为视频源帧号。
- 求值（``evaluate_grade_keyframes``）在 base grading_params 上打补丁：
  被轨道化的参数取插值，其余保持 base 值；首帧前/末帧后端点保持。
- 插值模式：hold（保持左值）、linear（线性）、ease（smoothstep 缓动）。
- engine 预览（GPU）与 timeline_export（CPU 导出）共用本模块，保证
  预览、保存、渲染和导出的帧值一致。

序列化格式嵌在 Layer.to_dict()['grade_keyframes']：
``{param_name: track.to_dict()}``，与 ColorStackData v2 layers 快照一同
持久化，保存/撤销自动覆盖。

线程边界：纯数据/纯函数模块。求值（evaluate_*）只读轨道并返回新
dict / 就地写回 shape 属性，被 GPU 预览线程与 CPU 导出 worker 共用；
关键帧编辑（set/remove/record）应仅在 UI 线程串行执行，跨线程并发
编辑同一轨道不受保护。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# 可动画参数白名单：grading_params 键 → 值形态
# vec3 参数是 (r/g/b 对称) 三元组，scalar 是单个 float。
GRADE_KEYFRAME_PARAMS: Dict[str, str] = {
    "lift": "vec3",
    "gamma": "vec3",
    "gain": "vec3",
    "exposure": "scalar",
    "temp": "scalar",
    "temperature": "scalar",
    "tint": "scalar",
    "contrast": "scalar",
    "shadow": "scalar",
    "highlights": "scalar",
    "saturation": "scalar",
    "vibrance": "scalar",
    "mid_detail": "scalar",
    "sharpness": "scalar",
    "hue_shift": "scalar",
}

INTERPOLATION_MODES = ("linear", "ease", "hold")


def _lerp(a: float, b: float, t: float) -> float:
    """线性插值 ``a + (b - a) * t``。"""
    return a + (b - a) * t


def _interp_factor(t: float, interpolation: str) -> float:
    """把线性进度 t ∈ [0,1] 映射为插值权重。

    hold → 恒 0（保持左值）；ease → smoothstep 缓动；其余按 linear
    原样返回。
    """
    if interpolation == "hold":
        return 0.0
    if interpolation == "ease":
        # smoothstep — 与 KeyframeTrack 的缓动语义一致
        return t * t * (3.0 - 2.0 * t)
    return t  # linear


@dataclass
class GradeKeyframe:
    """单个关键帧：frame 帧上的绝对值。

    Attributes:
        frame: 视频源帧号（构造时强转 int）。
        value: 该帧参数绝对值——float（scalar 轨道）或 3 元素
            tuple/list（vec3 轨道）。
        interpolation: 到**下一**关键帧的插值模式
            （"linear" / "ease" / "hold"，非法值回退 linear）。
    """

    frame: int
    value: Any  # float 或 3 元素 tuple/list
    interpolation: str = "linear"  # 到下一帧的插值模式

    def __post_init__(self) -> None:
        """规范化存储：frame 强转 int，非法插值模式回退 "linear"。"""
        self.frame = int(self.frame)
        if self.interpolation not in INTERPOLATION_MODES:
            self.interpolation = "linear"

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可 JSON 持久化的 dict（value 归一为 float 或 list[float]）。"""
        value = self.value
        if isinstance(value, (tuple, list)):
            value = [float(v) for v in value]
        else:
            value = float(value)
        return {
            "frame": self.frame,
            "value": value,
            "interpolation": self.interpolation,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GradeKeyframe":
        """从序列化 dict 重建关键帧（缺省 frame=0 / value=0.0 / linear）。

        AUD-0520：外部工程数据的 ``value`` 在加载期校验形态——标量须为
        int/float，序列的每个分量须可 float 化；str/dict/None/bool 等
        畸形值抛 ValueError，由 ``Track.from_dict`` 逐帧容错跳过，不再
        延迟到渲染/保存热路径（``value_at`` 的 float()/zip）才崩溃。
        """
        value = data.get("value", 0.0)
        if isinstance(value, (tuple, list)):
            if not value:
                raise ValueError("keyframe value: empty sequence")
            coerced: Any = tuple(float(component) for component in value)
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            # bool 是 int 子类，但作为关键帧值属畸形数据，显式拒绝
            raise ValueError(
                f"keyframe value: unsupported type {type(value).__name__}")
        else:
            coerced = float(value)
        return cls(
            frame=int(data.get("frame", 0)),
            value=coerced,
            interpolation=str(data.get("interpolation", "linear")),
        )

    def __lt__(self, other: "GradeKeyframe") -> bool:
        """按帧号比较（供 sort 维护轨道升序）。"""
        return self.frame < other.frame


def _expected_value_size(param_name: str) -> int | None:
    """AUD-0520：轨道参数的期望分量数（1=scalar/2=vec2/3=vec3）。

    参数不在 grade 白名单也不是合法 mask 轨道名时返回 None（未注册
    参数不校验分量数，保持向前兼容）。
    """
    shape = GRADE_KEYFRAME_PARAMS.get(param_name)
    if shape is None:
        parsed = parse_mask_track_name(param_name)
        if parsed is not None:
            shape = MASK_KEYFRAME_FIELDS.get(parsed[1])
    if shape is None:
        return None
    if shape == "scalar":
        return 1
    if shape == "vec3":
        return 3
    return 2


@dataclass
class GradeKeyframeTrack:
    """一个参数的关键帧轨道（按帧升序维护）。

    Attributes:
        param_name: 轨道名——grade 参数名（白名单键）或
            ``mask.<shape_id>.<field>`` 形式的 mask 轨道名。
        keyframes: 关键帧列表，编辑后始终保持帧号升序。
        _lock: AUD-0522 — 编辑（UI 线程 append→sort）与求值（导出线程
            value_at）之间的轻量锁。求值端经锁内快照读取，不会观察到
            "新帧已 append、尚未 sort" 的中间态。
    """

    param_name: str
    keyframes: List[GradeKeyframe] = field(default_factory=list)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False)

    # ------------------------------------------------------------------
    # AUD-0522: 锁不可 deepcopy/pickle（"cannot pickle '_thread.lock'"）。
    # Layer.copy_params_from 等调用方会对轨道做 copy.deepcopy——序列化
    # 协议里把锁排除、反序列化时重建新锁，保持既有复制行为不变。
    # ------------------------------------------------------------------

    def __getstate__(self) -> Dict[str, Any]:
        state = self.__dict__.copy()
        state["_lock"] = None
        return state

    def __setstate__(self, state: Dict[str, Any]) -> None:
        self.__dict__.update(state)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 编辑
    # ------------------------------------------------------------------

    def set_keyframe(self, frame: int, value: Any,
                     interpolation: str = "linear") -> None:
        """写入/覆盖某帧的关键帧。

        同帧覆盖原位替换；新帧追加后重排序，维持轨道升序不变式。
        """
        kf = GradeKeyframe(frame, value, interpolation)
        with self._lock:
            for index, existing in enumerate(self.keyframes):
                if existing.frame == kf.frame:
                    self.keyframes[index] = kf
                    return
            self.keyframes.append(kf)
            self.keyframes.sort()

    def remove_keyframe(self, frame: int) -> bool:
        """删除某帧的关键帧。

        Returns:
            bool: True 表示找到并删除；False 表示该帧无关键帧。
        """
        with self._lock:
            for index, kf in enumerate(self.keyframes):
                if kf.frame == frame:
                    self.keyframes.pop(index)
                    return True
            return False

    def get_keyframe(self, frame: int) -> GradeKeyframe | None:
        """取某帧的关键帧；不存在时返回 None。"""
        with self._lock:
            for kf in self.keyframes:
                if kf.frame == frame:
                    return kf
            return None

    def frames(self) -> List[int]:
        """轨道上全部关键帧帧号（升序，未去重——同帧本就不重复）。"""
        with self._lock:
            return [kf.frame for kf in self.keyframes]

    # ------------------------------------------------------------------
    # 求值
    # ------------------------------------------------------------------

    def value_at(self, frame: int, default: Any,
                 *, circular: bool = False) -> Any:
        """帧求值：端点保持外推，相邻关键帧间按插值模式混合。

        vector 值分量独立插值；default 与关键帧值形态不一致时按标量处理。
        ``circular``（标量专用，N10-B）：0-1 环形量按最短弧插值。
        """
        frame = int(frame)
        # AUD-0522: 锁内取快照——UI 线程 append→sort 的两步之间，导出线程
        # 拿到的要么是排序完成前的旧序列，要么是排序后的完整新序列，
        # 绝不是 keyframes[-1] 为未排序新帧的中间态。
        with self._lock:
            keyframes = list(self.keyframes)
        if not keyframes:
            return default

        is_vec = isinstance(keyframes[0].value, (tuple, list))

        # 端点外推：首帧前保持首值，末帧后保持末值
        if frame <= keyframes[0].frame:
            value = keyframes[0].value
            return tuple(value) if is_vec else float(value)
        if frame >= keyframes[-1].frame:
            value = keyframes[-1].value
            return tuple(value) if is_vec else float(value)

        for index in range(len(keyframes) - 1):
            left = keyframes[index]
            right = keyframes[index + 1]
            if left.frame <= frame <= right.frame:
                span = right.frame - left.frame
                t = 0.0 if span <= 0 else (frame - left.frame) / span
                t = _interp_factor(t, left.interpolation)
                if is_vec:
                    return tuple(
                        _lerp(float(a), float(b), t)
                        for a, b in zip(left.value, right.value)
                    )
                a = float(left.value)
                b = float(right.value)
                if circular:
                    # 最短弧：b 相对 a 的环差 ∈ (-0.5, 0.5]
                    delta = (b - a + 0.5) % 1.0 - 0.5
                    return (a + delta * t) % 1.0
                return _lerp(a, b, t)

        # 不可达（上面已覆盖端点与区间），防御返回末值
        value = keyframes[-1].value
        return tuple(value) if is_vec else float(value)

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """序列化轨道（param_name + 逐关键帧 to_dict），嵌入 Layer 快照。"""
        # AUD-0522: 与求值端一致，锁内快照，避免保存线程读到中间态。
        with self._lock:
            keyframes = list(self.keyframes)
        return {
            "param_name": self.param_name,
            "keyframes": [kf.to_dict() for kf in keyframes],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GradeKeyframeTrack":
        """从序列化 dict 重建轨道；坏关键帧条目（TypeError/ValueError）
        逐条跳过并记录 warning，其余照常载入并重排序。

        AUD-0520：值形态（可 float 化 + 分量数与参数白名单一致）在加载
        期校验，畸形关键帧在此丢弃，不再延迟到渲染/保存热路径。
        """
        track = cls(param_name=str(data.get("param_name", "")))
        expected = _expected_value_size(track.param_name)
        for kf_data in data.get("keyframes", []):
            try:
                kf = GradeKeyframe.from_dict(kf_data)
                if expected is not None:
                    size = len(kf.value) if isinstance(kf.value, tuple) else 1
                    if size != expected:
                        raise ValueError(
                            f"keyframe value has {size} component(s), "
                            f"expected {expected} for '{track.param_name}'")
                track.keyframes.append(kf)
            except (TypeError, ValueError) as exc:
                logger.warning("Dropped malformed keyframe on track '%s': %s",
                               track.param_name, exc)
                continue
        track.keyframes.sort()
        return track


def evaluate_grade_keyframes(
    tracks: Dict[str, GradeKeyframeTrack],
    frame: int,
    base_params: Dict[str, Any],
) -> Dict[str, Any]:
    """返回 base_params 的浅拷贝，被轨道化且在白名单内的参数替换为帧插值值。

    未轨道化/白名单外的参数、以及空轨道一律保持 base 值。这是预览与
    导出共用的单一求值入口。
    """
    result = dict(base_params)
    for name, track in tracks.items():
        if name not in GRADE_KEYFRAME_PARAMS or not track.keyframes:
            continue
        result[name] = track.value_at(frame, result.get(name, 0.0))
    return result


def all_keyframe_frames(tracks: Dict[str, GradeKeyframeTrack]) -> List[int]:
    """所有轨道出现过的帧号并集（升序去重），供 ruler 标记。

    含 mask.* 轨道——grade 与 mask 关键帧共用同一 ruler 基础设施。
    """
    frames: set[int] = set()
    for track in tracks.values():
        frames.update(track.frames())
    return sorted(frames)


# ---------------------------------------------------------------------------
# P1-COLOR-09: Mask keyframe（Power Window 窗口动画）
#
# mask 轨道与 grade 轨道共存于同一个 ``layer.grade_keyframes`` 字典：
# 轨道名 ``mask.<shape_id>.<field>``。序列化、ruler 标记、◆- 删除
# （remove_grade_keyframe 遍历全部轨道）随既有基础设施自动覆盖；
# ``evaluate_grade_keyframes`` 按 GRADE_KEYFRAME_PARAMS 白名单跳过
# mask.* 轨道，两套求值互不干扰。
# ---------------------------------------------------------------------------

#: 可动画的 MaskShape 数值字段 → 值形态（vec2 = 2 分量，其余标量）。
#: points/grad_points（点集）与离散字段（visible/invert/operation）不动画。
MASK_KEYFRAME_FIELDS: Dict[str, str] = {
    "center": "vec2",
    "size": "vec2",
    "rotation": "scalar",
    "feather": "scalar",
    "opacity": "scalar",
    "blur": "scalar",
    "clean_black": "scalar",
    "clean_white": "scalar",
    "softness": "scalar",
}

MASK_TRACK_PREFIX = "mask."


def mask_track_name(shape_id: str, field: str) -> str:
    """mask 轨道名：mask.<shape_id>.<field>。"""
    return f"{MASK_TRACK_PREFIX}{shape_id}.{field}"


def parse_mask_track_name(name: str) -> Tuple[str, str] | None:
    """解析 mask 轨道名 → (shape_id, field)；非 mask 轨道返回 None。"""
    if not name.startswith(MASK_TRACK_PREFIX):
        return None
    rest = name[len(MASK_TRACK_PREFIX):]
    shape_id, sep, field = rest.rpartition(".")
    if not shape_id or not sep or field not in MASK_KEYFRAME_FIELDS:
        return None
    return shape_id, field


def shape_has_mask_tracks(layer_tracks: Dict[str, Any], shape_id: str) -> bool:
    """形状是否已有任一 mask 动画轨道（自动记录语义的判定）。"""
    prefix = f"{MASK_TRACK_PREFIX}{shape_id}."
    return any(
        isinstance(name, str) and name.startswith(prefix)
        for name in (layer_tracks or {})
    )


def record_mask_keyframe(layer: Any, frame: int, shape_id: str,
                         fields: List[str],
                         interpolation: str = "linear") -> int:
    """把形状当前字段值记录为当前帧关键帧，返回写入的字段数。

    值直接从 shape 属性读取（与 grade 参数"当前值即关键帧"语义一致）；
    字段不在白名单或形状不存在时跳过。
    """
    shape = layer.get_mask_item_by_id(shape_id) if hasattr(
        layer, "get_mask_item_by_id") else None
    if shape is None:
        return 0
    tracks = getattr(layer, "grade_keyframes", None)
    if tracks is None:
        return 0
    wrote = 0
    for field_name in fields:
        if field_name not in MASK_KEYFRAME_FIELDS:
            continue
        value = getattr(shape, field_name, None)
        if value is None:
            continue
        name = mask_track_name(shape_id, field_name)
        track = tracks.get(name)
        if track is None:
            track = GradeKeyframeTrack(name)
            tracks[name] = track
        track.set_keyframe(int(frame),
                           tuple(value) if isinstance(value, (list, tuple))
                           else float(value),
                           interpolation)
        wrote += 1
    return wrote


def evaluate_mask_keyframes(layer: Any, frame: int) -> bool:
    """按帧求值 layer 上全部 mask 轨道，就地写回 shape 属性。

    关键帧存绝对值，求值幂等；shape_id 已失效（形状被删除）的轨道
    被跳过。返回是否有任何修改（调用方据此置 mask_dirty）。

    Args:
        layer: 持有 ``grade_keyframes`` 字典与 ``get_mask_item_by_id``
            的 Layer（或等价替身）。
        frame: 视频源帧号。

    Returns:
        bool: True 表示至少一个 shape 字段被改写。
    """
    tracks = getattr(layer, "grade_keyframes", None)
    if not tracks:
        return False
    changed = False
    frame = int(frame)
    for name, track in list(tracks.items()):
        parsed = parse_mask_track_name(str(name))
        if parsed is None or not track.keyframes:
            continue
        shape_id, field = parsed
        shape = (layer.get_mask_item_by_id(shape_id)
                 if hasattr(layer, "get_mask_item_by_id") else None)
        if shape is None:
            continue
        value = track.value_at(frame, getattr(shape, field, None))
        if value is None:
            continue
        if isinstance(value, tuple):
            value = list(value)
        setattr(shape, field, value)
        changed = True
    return changed


# ---------------------------------------------------------------------------
# F4.5 · Qualifier 亮度区间动画（计划选定参数：lum_low / lum_high）
#
# 五环：记录（record_qualifier_keyframe）→ 求值
# （evaluate_qualifier_keyframes，就地写回 layer.qualifier_params）→
# 曲线（GradeKeyframeTrack 通用轨道设施自动覆盖：ruler/序列化）→
# 逐参数复制（copy_named_tracks 按名过滤）→ 静态切换（清除全部
# qualifier.* 轨道即回静态值；求值对无轨道参数恒等）。
# 白名单外字段（sat/hue 区间等）不经本注册表不得被 ◆ 暗示可动画。
# ---------------------------------------------------------------------------

#: 可动画的 Qualifier 数值字段 → 值形态（亮度区间 = 本期选定的真实
#: 必要参数；其余字段显式标静态——扩展须按证据逐个登记）。
QUALIFIER_KEYFRAME_FIELDS: Dict[str, str] = {
    # N10-B：对齐 Resolve HSL Qualifier 动画核心域（全部标量）。离散
    # 开关（enabled/invert/…*_enabled）与 key 后处理强度
    # （blur/clean_*/denoise——涉 N02-B key_pipeline 逐帧声明）不收录，
    # 登记 v2（invert 如需动画应为强制 hold 的离散轨道）。
    "hue_center": "scalar",
    "hue_width": "scalar",
    "hue_soft": "scalar",
    "sat_low": "scalar",
    "sat_high": "scalar",
    "sat_soft_low": "scalar",
    "sat_soft_high": "scalar",
    "lum_low": "scalar",
    "lum_high": "scalar",
    "lum_soft_low": "scalar",
    "lum_soft_high": "scalar",
}

#: hue 是 0-1 环形量：这些字段的标量插值走最短弧（0.9→0.1 中点为
#: 0.0/1.0 而非 0.5），与 Resolve 的 Hue 轨道语义一致。
CIRCULAR_QUALIFIER_FIELDS: frozenset = frozenset({"hue_center"})

QUALIFIER_TRACK_PREFIX = "qualifier."


def qualifier_track_name(field: str) -> str:
    """qualifier 轨道名：qualifier.<field>（每层单限定器）。"""
    return f"{QUALIFIER_TRACK_PREFIX}{field}"


def parse_qualifier_track_name(name: str) -> str | None:
    """解析 qualifier 轨道名 → field；非 qualifier 轨道/白名单外返回 None。"""
    if not name.startswith(QUALIFIER_TRACK_PREFIX):
        return None
    field = name[len(QUALIFIER_TRACK_PREFIX):]
    return field if field in QUALIFIER_KEYFRAME_FIELDS else None


def is_qualifier_animatable(field: str) -> bool:
    """◆ 标记的唯一判据：字段在白名单内才可显示"可动画"暗示。"""
    return field in QUALIFIER_KEYFRAME_FIELDS


def record_qualifier_keyframe(layer: Any, frame: int, fields: List[str],
                              interpolation: str = "linear") -> int:
    """把限定器当前字段值记录为当前帧关键帧，返回写入的字段数。

    值直接从 ``layer.qualifier_params`` 读取（当前值即关键帧）；
    字段不在白名单或限定器未启用时跳过。
    """
    params = getattr(layer, "qualifier_params", None)
    if not params:
        return 0
    tracks = getattr(layer, "grade_keyframes", None)
    if tracks is None:
        return 0
    wrote = 0
    for field_name in fields:
        if field_name not in QUALIFIER_KEYFRAME_FIELDS:
            continue
        value = params.get(field_name)
        if value is None:
            continue
        name = qualifier_track_name(field_name)
        track = tracks.get(name)
        if track is None:
            track = GradeKeyframeTrack(name)
            tracks[name] = track
        track.set_keyframe(int(frame), float(value), interpolation)
        wrote += 1
    return wrote


def evaluate_qualifier_keyframes(layer: Any, frame: int) -> bool:
    """按帧求值 qualifier.* 轨道，就地写回 ``layer.qualifier_params``。

    关键帧存绝对值，求值幂等；限定器禁用（qualifier_params 为空/None）
    时轨道跳过。返回是否有任何修改（调用方据此置 is_dirty 重渲染）。
    """
    tracks = getattr(layer, "grade_keyframes", None)
    if not tracks:
        return False
    params = getattr(layer, "qualifier_params", None)
    if not params:
        return False
    changed = False
    frame = int(frame)
    for name, track in list(tracks.items()):
        field = parse_qualifier_track_name(str(name))
        if field is None or not track.keyframes:
            continue
        value = track.value_at(frame, params.get(field, 0.0))
        if params.get(field) != value:
            params[field] = value
            changed = True
    return changed


def evaluate_qualifier_params_tracks(
    tracks: Dict[str, Any], frame: int, base_qp: Dict[str, Any],
) -> Dict[str, Any]:
    """N10-B：qualifier.* 轨道的 dict 纯函数求值（图执行 provider 用）。

    与 layer 级 ``evaluate_qualifier_keyframes``（就地写回 layer 对象）
    同域同插值（含 hue_center 最短弧）；返回浅拷贝 patch 后的
    qualifier_params（原 dict 不动）。空轨道/空基底原样返回。
    """
    if not tracks or not base_qp:
        return dict(base_qp or {})
    patched = dict(base_qp)
    frame = int(frame)
    for name, track in tracks.items():
        field = parse_qualifier_track_name(str(name))
        if field is None:
            continue
        keyframes = getattr(track, "keyframes", None)
        if not keyframes:
            continue
        patched[field] = track.value_at(
            frame, patched.get(field, 0.0),
            circular=field in CIRCULAR_QUALIFIER_FIELDS)
    return patched


def copy_named_tracks(
    source_tracks: Dict[str, Any],
    names: List[str],
) -> Dict[str, "GradeKeyframeTrack"]:
    """按轨道名复制关键帧轨道（逐参数复制：只带走点名参数的动画）。

    返回新字典（源不被修改）；深拷贝轨道对象使目标层后续编辑隔离。
    """
    import copy as _copy
    return {
        name: _copy.deepcopy(source_tracks[name])
        for name in names
        if name in source_tracks
    }


def clear_qualifier_animation(tracks: Dict[str, Any]) -> int:
    """静态切换：清除全部 qualifier.* 轨道（参数回静态值），返回清除数。"""
    names = [name for name in list(tracks)
             if parse_qualifier_track_name(str(name)) is not None]
    for name in names:
        tracks.pop(name, None)
    return len(names)
