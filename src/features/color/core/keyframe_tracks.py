"""KF-01: 关键帧轨道共享描述与参数编解码（单一权威注册表）。

从 ``grade_keyframes.py``（Layer 域）与 ``color_graph_service.py``（图域）
提取的共同轨道描述层，四方共用：

- **稳定身份**：轨道名三域——grade 参数名、``mask.<shape_id>.<field>``
  （窗口）、``qualifier.<field>``。身份只来自稳定实体 ID（参数键 /
  shape uuid / 限定器域），显示名（shape.name、节点标题、翻译名）、
  改名与重排不影响轨道绑定；同形状的两个实例各持独立 uuid，天然
  不重绑。
- **参数类型**：每个字段的值形态（scalar / vec2 / vec3）、环形量与
  可动画性在此登记，Layer 白名单与图命令白名单都从本模块派生。
- **键型与动态属性**：``kind``（static/dynamic）+ 入出权重
  ``start``/``end``。旧插值模式与键型的等价映射：
  hold → static；linear → dynamic (1,1)；ease → dynamic (0,0)
  （Hermite 斜率 (0,0) 即 smoothstep）。段求值 ``A→B`` 读 A 的
  start、B 的 end，经统一权重函数 ``segment_weight``。
  非 (1,1)/(0,0) 档位的真实语义 **PENDING-KF00**（待 Resolve 实测
  回填，见 docs/plans/2026-11-06-color-keyframes/KF00_RESOLVE_FIXTURES.md）。
- **能力登记**：各域 read/validate/write/evaluate/preview/deliver/copy
  能力与未支持原因，``render_coverage_markdown()`` 生成工具覆盖清单。

序列化兼容约定：解码接受旧格式（``{frame,value,interpolation}``）与
显式键型（``kind``/``start``/``end``）并存；编码默认 canonical-minimal
（与插值模式等价的键型信息不落盘），保证既有载荷逐字节不变。显式
持久化与版本化由 KF-05 在保存边界启用（``encode_keyframe(explicit=)``）。

线程边界：纯数据/纯函数模块，与 grade_keyframes 相同。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

# ---------------------------------------------------------------------------
# 键型与动态属性
# ---------------------------------------------------------------------------

KEY_KIND_STATIC = "static"
KEY_KIND_DYNAMIC = "dynamic"
#: 全部合法键型。static：点右侧保持（hold 语义）；dynamic：点右侧
#: 按统一权重函数插值到下一键，即使后一键为 static（段语义由左键决定）。
KEY_KINDS = (KEY_KIND_STATIC, KEY_KIND_DYNAMIC)

#: 旧插值模式全集（grade_keyframes 既有语义，非法值回退 linear）。
INTERPOLATION_MODES = ("linear", "ease", "hold")

#: 动态权重的单调安全域：Hermite 斜率 m0,m1 ≥ 0 且 m0+m1 ≤ 3 时段内
#: 单调。Dissolve 滑杆到权重的真实档位映射 PENDING-KF00。
DYNAMIC_WEIGHT_MAX = 3.0


def legacy_mode_to_dynamics(interpolation: str) -> Tuple[str, float, float]:
    """旧插值模式 → (键型, start, end) 等价映射（无损往返）。"""
    if interpolation == "hold":
        return (KEY_KIND_STATIC, 1.0, 1.0)
    if interpolation == "ease":
        return (KEY_KIND_DYNAMIC, 0.0, 0.0)
    return (KEY_KIND_DYNAMIC, 1.0, 1.0)  # linear 与未知值


def dynamics_to_legacy_mode(kind: str, start: float, end: float) -> str:
    """(键型, start, end) → 最接近的旧插值模式（显示/迁移用）。

    非等价档位（如 (0.5, 0.5)）没有旧模式可表达，返回 "linear"——
    真实持久化走 encode_keyframe 的显式键型字段，不走此近似。
    """
    if kind == KEY_KIND_STATIC:
        return "hold"
    if abs(start - 1.0) <= 1e-9 and abs(end - 1.0) <= 1e-9:
        return "linear"
    if abs(start) <= 1e-9 and abs(end) <= 1e-9:
        return "ease"
    return "linear"


def validate_dynamic_weights(start: Any, end: Any) -> Optional[str]:
    """校验动态入出权重，返回原因码（合法返回 None）。

    原因码：``dynamic_weight_type``（非数值/bool）、
    ``dynamic_weight_out_of_range``（非有限值或超出 [0, DYNAMIC_WEIGHT_MAX]）。
    """
    for name, weight in (("start", start), ("end", end)):
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            return "dynamic_weight_type"
        value = float(weight)
        if not math.isfinite(value) or value < 0.0 or value > DYNAMIC_WEIGHT_MAX:
            return "dynamic_weight_out_of_range"
    return None


def segment_weight(u: float, start: float = 1.0, end: float = 1.0) -> float:
    """统一权重函数：段内线性进度 u ∈ [0,1] → 插值权重。

    三次 Hermite，端点斜率取左键出端权重 ``start`` 与右键入端权重
    ``end``（求 A→B 段时读 A 的 Start、B 的 End）：

    - (1, 1)：恒等映射（旧 linear）；
    - (0, 0)：smoothstep（旧 ease）；
    - 其余档位语义 PENDING-KF00，实测前按 Hermite 兼容语义求值。

    权重超出单调安全域时按比例压回（防御；编解码层已拒绝越界值）。
    """
    u = min(1.0, max(0.0, float(u)))
    m0 = max(0.0, float(start))
    m1 = max(0.0, float(end))
    total = m0 + m1
    if total > DYNAMIC_WEIGHT_MAX:
        scale = DYNAMIC_WEIGHT_MAX / total
        m0 *= scale
        m1 *= scale
    u2 = u * u
    u3 = u2 * u
    return (u3 - 2.0 * u2 + u) * m0 + (-2.0 * u3 + 3.0 * u2) + (u3 - u2) * m1


def segment_progress(u: float, left_kind: str, left_start: float,
                     right_end: float) -> float:
    """A→B 段的统一求值进度（键型语义 + 权重函数）。

    静态左键右侧保持（进度恒 0，即 hold）；动态左键按 ``segment_weight``
    混合到右键，即使右键为 static。求值接线（KF-03/KF-05）应遵守的
    优先级：**显式动态键型存在时**用本函数；键型与插值模式等价的
    legacy 键（见 ``keyframe_uses_legacy_interpolation``）继续走旧
    插值模式路径，保证首开帧值一致。
    """
    if left_kind == KEY_KIND_STATIC:
        return 0.0
    return segment_weight(u, left_start, right_end)


def keyframe_uses_legacy_interpolation(keyframe: DecodedKeyframe) -> bool:
    """键型是否与插值模式等价（legacy 键判据）。

    等价键的求值走旧插值模式路径（frame 值与历史版本逐位一致）；
    只有携带非等价显式动态属性的键才启用统一权重函数。
    """
    return (keyframe.kind, keyframe.start, keyframe.end) == (
        legacy_mode_to_dynamics(keyframe.interpolation))


# ---------------------------------------------------------------------------
# 关键帧编解码（legacy {frame,value,interpolation} ∪ 显式键型）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecodedKeyframe:
    """解码后的关键帧：显式键型 + 旧插值模式并存。

    ``interpolation`` 恒为合法旧模式（到下一帧的语义，供既有渲染路径
    直接消费）；``kind``/``start``/``end`` 是权威键型描述。二者经
    ``legacy_mode_to_dynamics`` 保持一致（static ⇔ hold 等）。
    """

    frame: int
    value: Any
    interpolation: str
    kind: str
    start: float
    end: float


def make_keyframe(frame: int, value: Any, *, interpolation: str = "linear",
                  kind: Optional[str] = None, start: float = 1.0,
                  end: float = 1.0) -> DecodedKeyframe:
    """构造关键帧：键型与插值模式互相同步（权威侧为 kind）。

    kind 显式给出时反推等价旧模式；未给出时由 interpolation 派生。
    权重越界抛 ValueError（原因码见 validate_dynamic_weights）。
    """
    if kind is None:
        if interpolation not in INTERPOLATION_MODES:
            raise ValueError("bad_interpolation")
        derived_kind, derived_start, derived_end = (
            legacy_mode_to_dynamics(interpolation))
        resolved_kind = derived_kind
        # (start, end) 未显式定制（保持默认 1.0）时跟随模式等价对
        resolved_start = derived_start if start == 1.0 else float(start)
        resolved_end = derived_end if end == 1.0 else float(end)
    else:
        if kind not in KEY_KINDS:
            raise ValueError("bad_key_kind")
        resolved_kind = kind
        resolved_start = float(start)
        resolved_end = float(end)
        interpolation = dynamics_to_legacy_mode(resolved_kind,
                                                resolved_start, resolved_end)
    reason = validate_dynamic_weights(resolved_start, resolved_end)
    if reason is not None:
        raise ValueError(reason)
    if int(frame) < 0:
        raise ValueError("negative_frame")
    return DecodedKeyframe(frame=int(frame), value=value,
                           interpolation=interpolation, kind=resolved_kind,
                           start=resolved_start, end=resolved_end)


def decode_keyframe(data: Mapping[str, Any]) -> DecodedKeyframe:
    """从序列化 dict 解码关键帧（旧格式与显式键型并存）。

    旧格式 ``{frame, value, interpolation}``：kind/start/end 由插值模式
    派生。显式 ``kind`` 存在时以键型为准（与 interpolation 冲突——如
    kind=dynamic 而 interpolation=hold——按键型归一 interpolation）。
    值必须非 None 且可 float 化（畸形值抛 ValueError，原因码前缀），
    分量数校验由 ``validate_value_shape`` 按 track 提供方另行执行。
    """
    frame = int(data.get("frame", 0))
    value = data.get("value")
    if value is None:
        raise ValueError("missing_value")
    if isinstance(value, bool) or not isinstance(value, (int, float, tuple,
                                                         list)):
        raise ValueError(f"bad_value_type:{type(value).__name__}")
    if isinstance(value, (tuple, list)):
        if not value:
            raise ValueError("empty_value_sequence")
        value = tuple(float(component) for component in value)
    else:
        if not math.isfinite(float(value)):
            raise ValueError("non_finite_value")
        value = float(value)

    interpolation = data.get("interpolation")
    kind = data.get("kind")
    if kind is not None:
        kind = str(kind)
        if kind not in KEY_KINDS:
            raise ValueError("bad_key_kind")
    start = data.get("start", 1.0)
    end = data.get("end", 1.0)

    if kind is None:
        mode = str(interpolation) if interpolation is not None else "linear"
        if mode not in INTERPOLATION_MODES:
            raise ValueError("bad_interpolation")
        return make_keyframe(frame, value, interpolation=mode)
    # kind 显式给出：键型为权威（与 interpolation 冲突时归一）。
    # start/end 缺省（1.0 哨兵与显式 1.0 不可区分）时继承插值模式的
    # 等价权重（ease→(0,0)），保证旧载荷往返无损。
    base_start, base_end = 1.0, 1.0
    if interpolation in INTERPOLATION_MODES:
        _, base_start, base_end = legacy_mode_to_dynamics(str(interpolation))
    resolved_start = float(start) if start != 1.0 else base_start
    resolved_end = float(end) if end != 1.0 else base_end
    return make_keyframe(frame, value, kind=kind,
                         start=resolved_start, end=resolved_end)


def encode_keyframe(keyframe: DecodedKeyframe, *,
                    explicit: bool = False) -> Dict[str, Any]:
    """序列化关键帧。canonical-minimal：与插值模式等价的键型信息不落盘，
    保证旧载荷逐字节不变；``explicit=True`` 恒写 kind/start/end（KF-05
    版本化保存边界使用）。"""
    value = keyframe.value
    if isinstance(value, (tuple, list)):
        value = [float(component) for component in value]
    else:
        value = float(value)
    payload: Dict[str, Any] = {
        "frame": int(keyframe.frame),
        "value": value,
        "interpolation": keyframe.interpolation,
    }
    equivalent = legacy_mode_to_dynamics(keyframe.interpolation)
    carries_dynamics = (keyframe.kind, keyframe.start, keyframe.end) != equivalent
    if explicit or carries_dynamics:
        payload["kind"] = keyframe.kind
        payload["start"] = float(keyframe.start)
        payload["end"] = float(keyframe.end)
    return payload


# ---------------------------------------------------------------------------
# 轨道注册表（单一权威；grade_keyframes.py 从此派生并再导出）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrackFieldSpec:
    """单个可动画字段的类型描述。

    Attributes:
        name: 字段名（轨道名的最后一段）。
        shape: 值形态——"scalar" / "vec2" / "vec3"。
        circular: 0-1 环形量（hue），插值走最短弧。
        note: 登记备注（别名、档位待实测等）。
    """

    name: str
    shape: str
    circular: bool = False
    note: str = ""


#: grade 域（Corrector grading_params）。temp/temperature 为同一参数的
#: 兼容双名（v1 工程兼容），两条目独立登记；shadow 同理对应 shadows。
GRADE_TRACK_FIELDS: Tuple[TrackFieldSpec, ...] = (
    TrackFieldSpec("lift", "vec3"),
    TrackFieldSpec("gamma", "vec3"),
    TrackFieldSpec("gain", "vec3"),
    TrackFieldSpec("exposure", "scalar"),
    TrackFieldSpec("temp", "scalar", note="temperature 兼容双名"),
    TrackFieldSpec("temperature", "scalar", note="temp 兼容双名"),
    TrackFieldSpec("tint", "scalar"),
    TrackFieldSpec("contrast", "scalar"),
    TrackFieldSpec("shadow", "scalar", note="shadows 兼容旧名（v1 归一）"),
    TrackFieldSpec("highlights", "scalar"),
    TrackFieldSpec("saturation", "scalar"),
    TrackFieldSpec("vibrance", "scalar"),
    TrackFieldSpec("mid_detail", "scalar"),
    TrackFieldSpec("sharpness", "scalar"),
    TrackFieldSpec("hue_shift", "scalar"),
)

#: 窗口域（mask.<shape_id>.<field>）。points/grad_points（点集）与离散
#: 字段（visible/invert/operation/locked）不动画。
WINDOW_TRACK_FIELDS: Tuple[TrackFieldSpec, ...] = (
    TrackFieldSpec("center", "vec2"),
    TrackFieldSpec("size", "vec2"),
    TrackFieldSpec("rotation", "scalar"),
    TrackFieldSpec("feather", "scalar"),
    TrackFieldSpec("opacity", "scalar"),
    TrackFieldSpec("blur", "scalar"),
    TrackFieldSpec("clean_black", "scalar"),
    TrackFieldSpec("clean_white", "scalar"),
    TrackFieldSpec("softness", "scalar"),
)

#: qualifier 域（每层单限定器，轨道名不含实例 ID）。离散开关与 key 后
#: 处理强度不收录（登记 v2，见 grade_keyframes 注释）。
QUALIFIER_TRACK_FIELDS: Tuple[TrackFieldSpec, ...] = (
    TrackFieldSpec("hue_center", "scalar", circular=True),
    TrackFieldSpec("hue_width", "scalar"),
    TrackFieldSpec("hue_soft", "scalar"),
    TrackFieldSpec("sat_low", "scalar"),
    TrackFieldSpec("sat_high", "scalar"),
    TrackFieldSpec("sat_soft_low", "scalar"),
    TrackFieldSpec("sat_soft_high", "scalar"),
    TrackFieldSpec("lum_low", "scalar"),
    TrackFieldSpec("lum_high", "scalar"),
    TrackFieldSpec("lum_soft_low", "scalar"),
    TrackFieldSpec("lum_soft_high", "scalar"),
)

#: 既有消费方的 Dict[str, str] 形态（Layer 白名单 / 期望分量数）。
GRADE_KEYFRAME_PARAMS: Dict[str, str] = {
    spec.name: spec.shape for spec in GRADE_TRACK_FIELDS}
MASK_KEYFRAME_FIELDS: Dict[str, str] = {
    spec.name: spec.shape for spec in WINDOW_TRACK_FIELDS}
QUALIFIER_KEYFRAME_FIELDS: Dict[str, str] = {
    spec.name: spec.shape for spec in QUALIFIER_TRACK_FIELDS}
CIRCULAR_QUALIFIER_FIELDS = frozenset(
    spec.name for spec in QUALIFIER_TRACK_FIELDS if spec.circular)

# ---------------------------------------------------------------------------
# 轨道名与地址解析（稳定身份）
# ---------------------------------------------------------------------------

MASK_TRACK_PREFIX = "mask."
QUALIFIER_TRACK_PREFIX = "qualifier."


def mask_track_name(shape_id: str, field: str) -> str:
    """窗口轨道名：mask.<shape_id>.<field>（身份 = shape uuid）。"""
    return f"{MASK_TRACK_PREFIX}{shape_id}.{field}"


def parse_mask_track_name(name: str) -> Optional[Tuple[str, str]]:
    """解析窗口轨道名 → (shape_id, field)；非窗口轨道/白名单外返回 None。"""
    if not isinstance(name, str) or not name.startswith(MASK_TRACK_PREFIX):
        return None
    rest = name[len(MASK_TRACK_PREFIX):]
    shape_id, sep, field = rest.rpartition(".")
    if not shape_id or not sep or field not in MASK_KEYFRAME_FIELDS:
        return None
    return shape_id, field


def qualifier_track_name(field: str) -> str:
    """qualifier 轨道名：qualifier.<field>（每层单限定器，无实例 ID）。"""
    return f"{QUALIFIER_TRACK_PREFIX}{field}"


def parse_qualifier_track_name(name: str) -> Optional[str]:
    """解析 qualifier 轨道名 → field；非 qualifier 轨道/白名单外返回 None。"""
    if not isinstance(name, str) or not name.startswith(QUALIFIER_TRACK_PREFIX):
        return None
    field = name[len(QUALIFIER_TRACK_PREFIX):]
    return field if field in QUALIFIER_KEYFRAME_FIELDS else None


def is_qualifier_animatable(field: str) -> bool:
    """◆ 标记的唯一判据：字段在白名单内才可显示"可动画"暗示。"""
    return field in QUALIFIER_KEYFRAME_FIELDS


@dataclass(frozen=True)
class TrackAddress:
    """解析后的轨道地址（稳定身份，显示名无关）。

    Attributes:
        domain: 轨道域（grade / window / qualifier / sizing / fx）。
        field: 域内字段名。
        owner_id: 域内实例身份——window 域为 shape uuid，其余为 None
            （grade/qualifier 每节点单实例；sizing/fx 登记后补槽位 ID）。
    """

    domain: str
    field: str
    owner_id: Optional[str] = None

    @property
    def track_name(self) -> str:
        """该地址的规范轨道名（与 parse_track_name 互逆）。"""
        if self.domain == "window":
            return mask_track_name(self.owner_id or "", self.field)
        if self.domain == "qualifier":
            return qualifier_track_name(self.field)
        return self.field

    @property
    def spec(self) -> Optional[TrackFieldSpec]:
        """字段的类型描述；未登记域返回 None。"""
        table = _SPEC_TABLES.get(self.domain)
        if table is None:
            return None
        return table.get(self.field)


_SPEC_TABLES: Dict[str, Dict[str, TrackFieldSpec]] = {
    "grade": {spec.name: spec for spec in GRADE_TRACK_FIELDS},
    "window": {spec.name: spec for spec in WINDOW_TRACK_FIELDS},
    "qualifier": {spec.name: spec for spec in QUALIFIER_TRACK_FIELDS},
}


def parse_track_name(name: str) -> Optional[TrackAddress]:
    """轨道名 → TrackAddress；未登记轨道返回 None。

    判定顺序：mask. 前缀 → window；qualifier. 前缀 → qualifier；
    其余按 grade 白名单键。sizing/fx 域轨道名尚未开放（登记于能力表）。
    """
    parsed = parse_mask_track_name(name)
    if parsed is not None:
        return TrackAddress("window", parsed[1], parsed[0])
    qualifier_field = parse_qualifier_track_name(name)
    if qualifier_field is not None:
        return TrackAddress("qualifier", qualifier_field)
    if isinstance(name, str) and name in GRADE_KEYFRAME_PARAMS:
        return TrackAddress("grade", name)
    return None


def expected_value_size(track_name: str) -> Optional[int]:
    """轨道的期望值分量数（1=scalar / 2=vec2 / 3=vec3）；未登记返回 None。"""
    address = parse_track_name(track_name)
    if address is None:
        return None
    shape = address.spec.shape if address.spec else None
    if shape == "scalar":
        return 1
    if shape == "vec3":
        return 3
    if shape == "vec2":
        return 2
    return None


def validate_value_shape(track_name: str, value: Any) -> Optional[str]:
    """校验关键帧值形态，返回原因码（合法返回 None）。

    原因码：``bad_value_type``（bool/str/dict/None）、
    ``empty_value_sequence``、``bad_value_arity``（分量数与登记形态不符）、
    ``non_finite_value``。
    """
    if value is None or isinstance(value, (bool, str, dict)):
        return "bad_value_type"
    if isinstance(value, (tuple, list)):
        if not value:
            return "empty_value_sequence"
        components = value
    else:
        components = (value,)
    for component in components:
        if isinstance(component, bool) or not isinstance(component, (int,
                                                                     float)):
            return "bad_value_type"
        if not math.isfinite(float(component)):
            return "non_finite_value"
    expected = expected_value_size(track_name)
    if expected is not None and len(components) != expected:
        return "bad_value_arity"
    return None


def node_command_track_whitelist() -> frozenset:
    """图命令（op_set/remove/move_node_keyframe）可写轨道名全集。

    grade ∪ qualifier（可枚举字段集）。窗口轨道名含 per-shape UUID
    不可枚举，经 ``track_name_is_writable`` 的解析谓词准入（形状存在
    性由命令目标校验补查）。
    """
    return frozenset(GRADE_KEYFRAME_PARAMS) | frozenset(
        qualifier_track_name(spec.name) for spec in QUALIFIER_TRACK_FIELDS)


def track_name_is_writable(name: str) -> bool:
    """图命令可写轨道判定：白名单 ∪ 窗口实例轨（解析谓词）。

    窗口轨 ``mask.<shape_id>.<field>`` 的字段集已登记（解析即校验）；
    shape_id 存在性在命令目标校验里 fail-closed（删除的形状不复活
    轨道）。Sizing/FX 未登记 → 解析为 None → 不可写。
    """
    if name in node_command_track_whitelist():
        return True
    address = parse_track_name(name)
    return address is not None and address.domain == "window"


# ---------------------------------------------------------------------------
# 能力登记与工具覆盖清单
# ---------------------------------------------------------------------------

#: 能力列（清单表头，顺序即输出顺序）。
CAPABILITY_COLUMNS = ("read", "validate", "write", "evaluate", "preview",
                      "deliver", "copy")

#: 各域当前真实能力（2026-11-07 核对）。False 项必须在 ``reasons`` 给出
#: 可见原因；接线推进时更新此处并重新生成清单。
DOMAIN_CAPABILITIES: Dict[str, Dict[str, Any]] = {
    "grade": {
        "write_domain": "graph+layer",
        "deliver": True,
        "reasons": {},
    },
    "window": {
        "write_domain": "graph+layer",
        "deliver": True,
        "reasons": {},
    },
    "qualifier": {
        "write_domain": "graph+layer",
        "deliver": True,
        "reasons": {},
    },
    "sizing": {
        "write_domain": None,
        "deliver": False,
        "reasons": {
            "read": "槽位身份（Input/Node/Output Sizing、Convergence 等）"
                    "仅存在于面板演示数据；后端尚未定义（KF-03 落槽位域）",
            "validate": "同上——无槽位注册表可校验",
            "write": "槽位→坐标系归属待 KF-00 OP-16 实测后接命令",
            "evaluate": "无后端轨道可求值",
            "preview": "无后端轨道可求值",
            "deliver": "无后端轨道可求值",
            "copy": "无后端轨道可复制",
        },
    },
    "fx": {
        "write_domain": None,
        "deliver": False,
        "reasons": {
            "read": "实例 ID（instance_id）与参数 schema（Effect Catalog）"
                    "已存在，但 animatable 能力标志尚未接入注册表（KF-03）",
            "validate": "无 per-instance schema 校验入口",
            "write": "FX 实例参数命令化待 KF-03",
            "evaluate": "无逐帧 FX 参数求值（KF-06 时域投影）",
            "preview": "无逐帧 FX 参数求值",
            "deliver": "无逐帧 FX 参数求值",
            "copy": "实例粘贴重建 instance_id（既有设计），参数级复制待命令化",
        },
    },
}


def _domain_row(domain: str) -> Dict[str, Any]:
    caps = DOMAIN_CAPABILITIES[domain]
    reasons: Dict[str, str] = caps.get("reasons", {})
    row = {"domain": domain, "write_domain": caps.get("write_domain")}
    for column in CAPABILITY_COLUMNS:
        row[column] = column not in reasons
    row["reasons"] = reasons
    return row


def capability_rows() -> List[Dict[str, Any]]:
    """工具覆盖清单行：每域一行能力汇总 + 每个已登记字段一行类型明细。"""
    rows: List[Dict[str, Any]] = []
    for domain in ("grade", "window", "qualifier", "sizing", "fx"):
        rows.append(_domain_row(domain))
    for domain, specs in (("grade", GRADE_TRACK_FIELDS),
                          ("window", WINDOW_TRACK_FIELDS),
                          ("qualifier", QUALIFIER_TRACK_FIELDS)):
        row = _domain_row(domain)
        for spec in specs:
            rows.append({
                "domain": domain,
                "field": spec.name,
                "shape": spec.shape,
                "circular": spec.circular,
                "note": spec.note,
                "capabilities": {c: row[c] for c in CAPABILITY_COLUMNS},
            })
    return rows


def render_coverage_markdown() -> str:
    """渲染工具覆盖清单 markdown（TOOL_COVERAGE.md 正文，可审查/可 diff）。"""
    lines = [
        "# 工具覆盖清单（KF-01 生成）",
        "",
        "由 `src/features/color/core/keyframe_tracks.py::"
        "render_coverage_markdown()` 生成——**勿手改**，改动先落在",
        "`DOMAIN_CAPABILITIES` / 各 `*_TRACK_FIELDS` 注册表，再重新生成"
        "（tests/color/test_keyframe_tracks.py 强制同步）。",
        "",
        "## 域能力汇总",
        "",
        "| 域 | 写入域 | " + " | ".join(CAPABILITY_COLUMNS) + " | 未支持原因 |",
        "| --- | --- | " + " | ".join(["---"] * len(CAPABILITY_COLUMNS))
        + " | --- |",
    ]
    label = {"grade": "Grade 调色参数", "window": "Window 窗口子轨",
             "qualifier": "Qualifier 限定器", "sizing": "Sizing",
             "fx": "Open FX / 独立 FX"}
    for domain in ("grade", "window", "qualifier", "sizing", "fx"):
        row = _domain_row(domain)
        cells = "、".join(
            f"{column}: {reason}" for column, reason in
            row["reasons"].items()) or "—"
        marks = " | ".join(
            "✓" if row[c] else "✗" for c in CAPABILITY_COLUMNS)
        write_domain = row["write_domain"] or "—"
        lines.append(
            f"| {label[domain]} | {write_domain} | {marks} | {cells} |")
    lines += ["", "## 字段登记（逐行往返/拒绝用例见 "
                  "tests/color/test_keyframe_tracks.py）", ""]
    for domain, table in (("grade", GRADE_TRACK_FIELDS),
                          ("window", WINDOW_TRACK_FIELDS),
                          ("qualifier", QUALIFIER_TRACK_FIELDS)):
        lines.append(f"### {label[domain]}（{domain}）")
        lines.append("")
        lines.append("| 字段 | 值形态 | 环形 | 备注 |")
        lines.append("| --- | --- | --- | --- |")
        for spec in table:
            note = spec.note or "—"
            circular = "是" if spec.circular else "—"
            lines.append(f"| {spec.name} | {spec.shape} | {circular} | {note} |")
        lines.append("")
    lines.append("PENDING-KF00：动态权重非 (1,1)/(0,0) 档位与 Dissolve 滑杆"
                 "档位映射待 Resolve 实测"
                 "（KF00_RESOLVE_FIXTURES.md）。")
    lines.append("")
    return "\n".join(lines)
