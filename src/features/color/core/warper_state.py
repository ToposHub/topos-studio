# -*- coding: utf-8 -*-
"""Color Warper 版本化领域模型(CW-B01)。

计划:`docs/plans/2026-11-12-color-warper-backend-plan.md` §4 CW-B01。

职责
----
1. **版本化 schema(v2)**:非方阵网格、双具名网格、CW 笔画/Pin、稳定 ID、
   色彩空间与坐标约定、算法版本号。旧 ``warper_params``(方阵 ``(N,N,2)``
   ndarray)继续按 ``legacy_hsv_v1`` 解释,本模块只做包装,不改数值。
2. **校验与冻结**:数值必须 finite、尺寸有上限、ID 唯一;ndarray 载荷
   构造即深拷贝并置只读(渲染线程/历史快照不共享可变引用)。
3. **未知字段保留**:仿 ``shared/extra_fields.py`` 契约,未知键深拷贝并
   在 ``to_dict`` 原样回写;未知 schema 版本**显式拒绝**(不静默降级为
   identity/HSV),由调用方保留原文并标记不可编辑/不可渲染。
4. **重采样**:分辨率变更用显式双线性重采样(Δhue 分量做圆周插值),
   Pin 的 point_id 映射到新网格最近格点,不依赖变化后的数组索引。
5. **能力契约**:{enabled, reason, supported_modes, backend, revision}。
   B01 阶段后端只有 legacy HSV 路径;新模式如实报告 unimplemented。
6. **请求 envelope 契约**:B02 命令适配层的 begin/update/commit/cancel
   身份字段;本模块只定义与校验,不做提交。

线程边界:纯数据类、无锁、无 Qt/GPU 依赖;任意线程只读共享安全。
"""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np

__all__ = [
    "WARPER_SCHEMA_VERSION",
    "LEGACY_ALGORITHM",
    "SUPPORTED_MODES",
    "SUPPORTED_COLOR_SPACES",
    "WarperStateError",
    "WarperGrid",
    "WarperStroke",
    "WarperPinPoint",
    "WarperStateV2",
    "WarperCapabilities",
    "WarperCommandRequest",
    "legacy_from_ndarray",
    "legacy_ndarray",
    "resample_grid",
    "describe_warper_capabilities",
]

#: 当前 schema 版本。未知更高版本必须显式拒绝(计划 §2.3)。
WARPER_SCHEMA_VERSION = 2

#: 旧 ndarray 数据的算法标识(计划 §4 CW-B01 步骤 3)。
LEGACY_ALGORITHM = "legacy_hsv_v1"

#: 新模型算法版本(随 B03+ 冻结的常数演进时递增)。
CURRENT_ALGORITHM = "topos_warper_v1"

SUPPORTED_MODES = ("hue_saturation", "chroma_luma", "chroma_warp")
SUPPORTED_COLOR_SPACES = ("hsv", "hsl", "hsp")

#: UI 候选 6/8/12/16/24(HS)/4..24(CL);上限取一档余量,越界即拒绝。
MAX_GRID_DIM = 64
#: 每分量位移幅度上限(|Δ|≤MAX_OFFSET_ABS),超出视为病态数据。
MAX_OFFSET_ABS = 10.0
MAX_STROKES = 256
MAX_PATH_POINTS = 4096
MAX_PINS = 1024

#: 每网格分量语义(声明,禁止把 sat 解释成 luma——计划 CW-B05)。
GRID_COMPONENT_SEMANTICS = {
    "hue_saturation": (("dh", "ds"), ("dh", "ds", "dl")),
    "chroma_luma": (("dc", "dl"),),
}

#: CW 笔画/Pin 参数域(B00 行为矩阵:默认值为实机观察,范围是 Topos 声明)。
STROKE_PARAM_DOMAINS = {
    "chroma_range": (0.0, 1.0, 0.040),
    "tonal_low": (0.0, 1.0, 1.0),
    "tonal_high": (0.0, 1.0, 1.0),
    "pivot": (0.0, 1.0, 0.5),
    "exposure": (-8.0, 8.0, 0.0),
}

#: HS 网格名与 CL 双网格名(具名,不靠数组顺序)。
HS_GRID_NAME = "hs"
CL_GRID_NAMES = ("grid1", "grid2")

STROKE_MODES = ("normal", "point_to_point")
COMMAND_PHASES = ("begin", "update", "commit", "cancel")


class WarperStateError(ValueError):
    """WarperStateV2 校验失败。code 是稳定诊断码(计划 §2.3)。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def _require_finite(array: np.ndarray, what: str) -> None:
    if not np.isfinite(array).all():
        raise WarperStateError("not_finite", f"{what} 含 NaN/Inf")


def _as_offsets(raw: Any, rows: int, cols: int, components: int, what: str) -> np.ndarray:
    try:
        array = np.asarray(raw, dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise WarperStateError("invalid_array", f"{what} 无法转为 float32 数组: {exc}") from exc
    if array.shape != (rows, cols, components):
        raise WarperStateError(
            "invalid_array",
            f"{what} 形状 {array.shape} != 期望 {(rows, cols, components)}")
    _require_finite(array, what)
    if np.abs(array).max() > MAX_OFFSET_ABS:
        raise WarperStateError("size_exceeded", f"{what} 分量幅度超过 {MAX_OFFSET_ABS}")
    frozen = array.copy()
    frozen.setflags(write=False)
    return frozen


def _validate_param(name: str, value: Any) -> float:
    low, high, _default = STROKE_PARAM_DOMAINS[name]
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise WarperStateError("invalid_param", f"参数 {name} 不是数值: {value!r}") from exc
    if not math.isfinite(number) or not low <= number <= high:
        raise WarperStateError("invalid_param", f"参数 {name}={number} 越界 [{low}, {high}]")
    return number


def _validate_params_dict(params: Mapping[str, Any]) -> None:
    """已知参数键的域校验(构造路径与 from_dict 一致,不允许旁路)。"""
    for name in STROKE_PARAM_DOMAINS:
        if name in params:
            _validate_param(name, params[name])


def default_stroke_params() -> dict[str, float]:
    return {name: default for name, (_low, _high, default) in STROKE_PARAM_DOMAINS.items()}


def _parse_params(raw: Any, what: str) -> dict[str, Any]:
    if raw is None:
        return default_stroke_params()
    if not isinstance(raw, Mapping):
        raise WarperStateError("corrupt_payload", f"{what} params 需要 Mapping,得到 {type(raw).__name__}")
    params: dict[str, Any] = {}
    for name in STROKE_PARAM_DOMAINS:
        if name in raw:
            params[name] = _validate_param(name, raw[name])
    missing = set(STROKE_PARAM_DOMAINS) - set(params)
    for name in missing:
        params[name] = STROKE_PARAM_DOMAINS[name][2]
    # 未知参数键原样保留(深拷贝),to_dict 回写不丢(DESK-04A 契约)。
    for key, value in raw.items():
        if key not in STROKE_PARAM_DOMAINS:
            params[key] = copy.deepcopy(value)
    return params


@dataclass(frozen=True)
class WarperPin:
    """网格点 Pin:约束求解输入(不是显示状态)。point_id 稳定跨重采样映射。"""
    point_id: str
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    _KNOWN_KEYS = ("point_id",)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"point_id": self.point_id}
        data.update(copy.deepcopy(self._extra_fields))
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WarperPin":
        if not isinstance(data, Mapping):
            raise WarperStateError("corrupt_payload", f"pin 需要 Mapping,得到 {type(data).__name__}")
        point_id = data.get("point_id")
        if not isinstance(point_id, str) or not point_id:
            raise WarperStateError("invalid_id", f"pin point_id 非法: {point_id!r}")
        extras = {key: copy.deepcopy(value) for key, value in data.items()
                  if key not in frozenset(cls._KNOWN_KEYS)}
        return cls(point_id=point_id, _extra_fields=extras)


@dataclass(frozen=True)
class WarperGrid:
    """一个具名网格:``(rows, cols, components)`` 位移 + Pin 集合。

    rows=sat/chroma 环(V 轴),cols=hue 射线(U 轴);HS 网格 components∈{2,3}
    (dh,ds[,dl]),CL 网格 components=2(dc,dl)。offsets 只读。extras 保留未知键。
    """
    name: str
    rows: int
    cols: int
    components: int
    semantics: tuple[str, ...]
    offsets: np.ndarray
    pins: tuple[WarperPin, ...] = ()
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    _KNOWN_KEYS = ("name", "rows", "cols", "components", "semantics", "offsets", "pins")

    def __post_init__(self) -> None:
        for bound, label in ((self.rows, "rows"), (self.cols, "cols")):
            if not 2 <= bound <= MAX_GRID_DIM:
                raise WarperStateError("size_exceeded", f"{self.name} {label}={bound} 越界 [2, {MAX_GRID_DIM}]")
        if self.offsets.shape != (self.rows, self.cols, self.components):
            raise WarperStateError("invalid_array", f"{self.name} offsets 形状与 rows/cols/components 不符")
        if self.offsets.flags.writeable:
            raise WarperStateError("corrupt_payload", "offsets 必须只读(冻结)")
        if len(set(p.point_id for p in self.pins)) != len(self.pins):
            raise WarperStateError("invalid_id", f"{self.name} pin point_id 重复")

    @staticmethod
    def identity(name: str, rows: int, cols: int, semantics: tuple[str, ...]) -> "WarperGrid":
        """构造零位移恒等网格。"""
        components = len(semantics)
        return WarperGrid(
            name=name, rows=rows, cols=cols, components=components, semantics=tuple(semantics),
            offsets=_as_offsets(np.zeros((rows, cols, components), np.float32), rows, cols, components, name),
        )

    def mutable_offsets(self) -> np.ndarray:
        """返回可写深拷贝(编辑器/B02 命令层用)。"""
        return np.array(self.offsets, dtype=np.float32, copy=True)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "rows": self.rows,
            "cols": self.cols,
            "semantics": list(self.semantics),
            "offsets": self.offsets.tolist(),
        }
        if self.pins:
            data["pins"] = [pin.to_dict() for pin in self.pins]
        data.update(copy.deepcopy(self._extra_fields))
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, mode: str) -> "WarperGrid":
        if not isinstance(data, Mapping):
            raise WarperStateError("corrupt_payload", f"grid 需要 Mapping,得到 {type(data).__name__}")
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise WarperStateError("corrupt_payload", f"grid name 非法: {name!r}")
        try:
            rows = int(data["rows"])
            cols = int(data["cols"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WarperStateError("corrupt_payload", f"grid {name} rows/cols 非法") from exc
        semantics_raw = data.get("semantics")
        allowed = GRID_COMPONENT_SEMANTICS.get(mode, ())
        if isinstance(semantics_raw, (list, tuple)) and tuple(semantics_raw) in allowed:
            semantics = tuple(str(s) for s in semantics_raw)
        else:
            raise WarperStateError(
                "invalid_semantics",
                f"grid {name} semantics {semantics_raw!r} 不属于模式 {mode} 的合法分量集 {allowed}")
        components = len(semantics)
        offsets = _as_offsets(data.get("offsets"), rows, cols, components, f"grid {name}")
        pins_raw = data.get("pins") or ()
        if not isinstance(pins_raw, (list, tuple)) or len(pins_raw) > MAX_PINS:
            raise WarperStateError("size_exceeded", f"grid {name} pins 数量非法")
        pins = tuple(WarperPin.from_dict(pin) for pin in pins_raw)
        known = frozenset(cls._KNOWN_KEYS) | {"components"}
        extras = {key: copy.deepcopy(value) for key, value in data.items() if key not in known}
        return cls(name=name, rows=rows, cols=cols, components=components,
                   semantics=semantics, offsets=offsets, pins=pins, _extra_fields=extras)

    def with_offsets(self, offsets: np.ndarray) -> "WarperGrid":
        """派生:替换 offsets(深拷贝冻结),携带 extras 与 pins。"""
        frozen = _as_offsets(offsets, self.rows, self.cols, self.components, f"grid {self.name}")
        return WarperGrid(name=self.name, rows=self.rows, cols=self.cols, components=self.components,
                          semantics=self.semantics, offsets=frozen, pins=self.pins,
                          _extra_fields=copy.deepcopy(self._extra_fields))


def _map_index(index: int, old_len: int, new_len: int) -> int:
    """重采样时的离散格点映射(端点对齐,与 resample_grid 连续映射一致)。"""
    if old_len == 1:
        return 0
    return int(round(index * (new_len - 1) / (old_len - 1)))


def _interp_axis(array: np.ndarray, axis: int, new_len: int, wrap: bool) -> np.ndarray:
    """沿一轴双线性重采样;wrap=True 时按圆周量插值(Δhue 环绕)。"""
    old_len = array.shape[axis]
    if new_len == old_len:
        return np.array(array, copy=True)
    source = np.arange(old_len, dtype=np.float64)
    positions = np.linspace(0.0, 1.0, new_len) * (old_len - 1)
    moved = np.moveaxis(np.asarray(array, dtype=np.float64), axis, 0)
    flat = moved.reshape(old_len, -1)
    out = np.empty((new_len, flat.shape[1]), dtype=np.float64)
    if not wrap:
        for column in range(flat.shape[1]):
            out[:, column] = np.interp(positions, source, flat[:, column])
    else:
        # 圆周插值:exp(2πi·x) 复平面线性插值后取角,结果 ∈ (-0.5, 0.5]。
        angles = np.exp(2j * np.pi * flat)
        real = np.empty_like(out)
        imag = np.empty_like(out)
        for column in range(flat.shape[1]):
            real[:, column] = np.interp(positions, source, angles[:, column].real)
            imag[:, column] = np.interp(positions, source, angles[:, column].imag)
        out = np.arctan2(imag, real) / (2.0 * np.pi)
    restored = out.reshape((new_len,) + moved.shape[1:])
    return np.moveaxis(restored, 0, axis).astype(np.float32)


def resample_grid(grid: WarperGrid, rows: int, cols: int) -> WarperGrid:
    """显式重采样(CW-B01 步骤 2):双线性 + Δhue 圆周插值;Pin 映射最近格点。"""
    if not (2 <= rows <= MAX_GRID_DIM and 2 <= cols <= MAX_GRID_DIM):
        raise WarperStateError("size_exceeded", f"重采样目标 ({rows}, {cols}) 越界")
    wrap_components = frozenset(i for i, sem in enumerate(grid.semantics) if sem == "dh")
    result = grid.offsets.astype(np.float32, copy=True)
    # 先行(sat/chroma 轴)后列(hue 轴);wrap 只作用于 dh 分量。
    for axis, new_len in ((0, rows), (1, cols)):
        planes = []
        for comp in range(result.shape[2]):
            plane = np.take(result, comp, axis=2)
            planes.append(_interp_axis(plane, axis, new_len, wrap=comp in wrap_components))
        result = np.stack(planes, axis=-1).astype(np.float32)
    if result.shape != (rows, cols, grid.components):
        raise WarperStateError("invalid_array", f"重采样结果形状 {result.shape} 异常")
    # Pin 的 point_id 映射到新网格(端点对齐最近格点);重合时去重。
    remapped: dict[str, WarperPin] = {}
    for pin in grid.pins:
        prefix, row, col = _split_point_id(pin.point_id)
        new_id = make_point_id(prefix, _map_index(row, grid.rows, rows), _map_index(col, grid.cols, cols))
        remapped.setdefault(new_id, WarperPin(point_id=new_id))
    return WarperGrid(name=grid.name, rows=rows, cols=cols, components=grid.components,
                      semantics=grid.semantics,
                      offsets=_as_offsets(result, rows, cols, grid.components, f"grid {grid.name}"),
                      pins=tuple(remapped.values()),
                      _extra_fields=copy.deepcopy(grid._extra_fields))


def make_point_id(grid_name: str, row: int, col: int) -> str:
    return f"{grid_name}:{row}:{col}"


def _split_point_id(point_id: str) -> tuple[str, int, int]:
    try:
        prefix, row, col = point_id.rsplit(":", 2)
        return prefix, int(row), int(col)
    except ValueError as exc:
        raise WarperStateError("invalid_id", f"point_id 无法解析: {point_id!r}") from exc


@dataclass(frozen=True)
class WarperStroke:
    """CW 笔画:颜色约束(源/目标 RGB)是语义本体;path 仅交互回放展示。"""
    stroke_id: str
    mode: str  # STROKE_MODES
    source_rgb: tuple[float, float, float]
    target_rgb: tuple[float, float, float]
    params: dict[str, float]
    path: tuple[tuple[float, float], ...] = ()
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    _KNOWN_KEYS = ("stroke_id", "mode", "source_rgb", "target_rgb", "params", "path")

    def __post_init__(self) -> None:
        if self.mode not in STROKE_MODES:
            raise WarperStateError("invalid_mode", f"stroke mode {self.mode!r} 非法")
        if self.params is None:
            object.__setattr__(self, "params", default_stroke_params())
        _validate_params_dict(self.params)
        for label in ("source_rgb", "target_rgb"):
            values = getattr(self, label)
            if len(values) != 3 or not all(math.isfinite(v) for v in values):
                raise WarperStateError("not_finite", f"stroke {label} 必须 3 个有限值")
        if self.mode == "point_to_point" and len(self.path) not in (0, 2):
            raise WarperStateError(
                "invalid_array", "point_to_point 笔画 path 只能为空(仅颜色约束)或恰好 2 个端点")
        if len(self.path) > MAX_PATH_POINTS:
            raise WarperStateError("size_exceeded", f"path 点数超过 {MAX_PATH_POINTS}")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "stroke_id": self.stroke_id,
            "mode": self.mode,
            "source_rgb": list(self.source_rgb),
            "target_rgb": list(self.target_rgb),
            "params": dict(self.params),
            "path": [list(point) for point in self.path],
        }
        data.update(copy.deepcopy(self._extra_fields))
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WarperStroke":
        if not isinstance(data, Mapping):
            raise WarperStateError("corrupt_payload", f"stroke 需要 Mapping,得到 {type(data).__name__}")
        stroke_id = data.get("stroke_id")
        if not isinstance(stroke_id, str) or not stroke_id:
            raise WarperStateError("invalid_id", f"stroke_id 非法: {stroke_id!r}")
        path_raw = data.get("path") or ()
        if not isinstance(path_raw, (list, tuple)):
            raise WarperStateError("corrupt_payload", "path 需要 list/tuple")
        path = tuple((float(p[0]), float(p[1])) for p in path_raw)
        known = frozenset(cls._KNOWN_KEYS)
        extras = {key: copy.deepcopy(value) for key, value in data.items() if key not in known}
        return cls(
            stroke_id=stroke_id,
            mode=str(data.get("mode", "normal")),
            source_rgb=tuple(float(v) for v in data.get("source_rgb", (0.0, 0.0, 0.0))),
            target_rgb=tuple(float(v) for v in data.get("target_rgb", (0.0, 0.0, 0.0))),
            params=_parse_params(data.get("params"), f"stroke {stroke_id}"),
            path=path,
            _extra_fields=extras,
        )


@dataclass(frozen=True)
class WarperPinPoint:
    """CW 独立 Pin(非网格点):chromaticity 位置 + 同域参数(B00 实测默认值)。"""
    pin_id: str
    position: tuple[float, float]
    params: dict[str, float]
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    _KNOWN_KEYS = ("pin_id", "position", "params")

    def __post_init__(self) -> None:
        if self.params is None:
            object.__setattr__(self, "params", default_stroke_params())
        _validate_params_dict(self.params)
        if not all(math.isfinite(v) for v in self.position):
            raise WarperStateError("not_finite", "pin position 必须有限")
        if not all(0.0 <= v <= 1.0 for v in self.position):
            raise WarperStateError("invalid_param", "pin position 必须在 [0,1]²")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "pin_id": self.pin_id,
            "position": list(self.position),
            "params": dict(self.params),
        }
        data.update(copy.deepcopy(self._extra_fields))
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WarperPinPoint":
        if not isinstance(data, Mapping):
            raise WarperStateError("corrupt_payload", f"pin 需要 Mapping,得到 {type(data).__name__}")
        pin_id = data.get("pin_id")
        if not isinstance(pin_id, str) or not pin_id:
            raise WarperStateError("invalid_id", f"pin_id 非法: {pin_id!r}")
        position = data.get("position")
        if not isinstance(position, (list, tuple)) or len(position) != 2:
            raise WarperStateError("corrupt_payload", "pin position 需要 [u, v]")
        known = frozenset(cls._KNOWN_KEYS)
        extras = {key: copy.deepcopy(value) for key, value in data.items() if key not in known}
        return cls(
            pin_id=pin_id,
            position=(float(position[0]), float(position[1])),
            params=_parse_params(data.get("params"), f"pin {pin_id}"),
            _extra_fields=extras,
        )


@dataclass(frozen=True)
class WarperStateV2:
    """Warper 调色状态的版本化领域模型。

    契约要点:
    - ``algorithm_version`` 标记算法代数;legacy ndarray 包装固定
      ``legacy_hsv_v1``(迁移不得改变画面)。
    - grids 具名:HS 模式一个 ``hs`` 网格;CL 模式 ``grid1``/``grid2``。
    - 模式与网格结构必须匹配(chroma_warp 无网格)。
    - 未知字段保留在 ``_extra_fields``;未知 schema 版本在 ``from_dict``
      抛 ``schema_too_new``(调用方保留原文,不降级)。
    """
    schema_version: int
    algorithm_version: str
    mode: str
    color_space: str
    axis_angle: float
    grids: dict[str, WarperGrid]
    strokes: tuple[WarperStroke, ...] = ()
    pin_points: tuple[WarperPinPoint, ...] = ()
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    _KNOWN_KEYS = ("schema_version", "algorithm_version", "mode", "color_space",
                   "axis_angle", "grids", "strokes", "pins")

    def __post_init__(self) -> None:
        if self.mode not in SUPPORTED_MODES:
            raise WarperStateError("invalid_mode", f"mode {self.mode!r} 非法")
        if self.color_space not in SUPPORTED_COLOR_SPACES:
            raise WarperStateError("invalid_mode", f"color_space {self.color_space!r} 非法")
        if not math.isfinite(self.axis_angle) or not -180.0 <= self.axis_angle <= 180.0:
            raise WarperStateError("invalid_param", f"axis_angle {self.axis_angle} 越界 [-180, 180]")
        if self.mode == "hue_saturation":
            required, forbidden = (HS_GRID_NAME,), CL_GRID_NAMES
        elif self.mode == "chroma_luma":
            required, forbidden = CL_GRID_NAMES, (HS_GRID_NAME,)
        else:  # chroma_warp:无网格,颜色约束在 strokes/pins
            required, forbidden = (), (HS_GRID_NAME,) + CL_GRID_NAMES
        for name in required:
            if name not in self.grids:
                raise WarperStateError("corrupt_payload", f"模式 {self.mode} 缺少网格 {name}")
        for name in forbidden:
            if name in self.grids:
                raise WarperStateError("corrupt_payload", f"模式 {self.mode} 不允许网格 {name}")
        if len(self.strokes) > MAX_STROKES:
            raise WarperStateError("size_exceeded", f"strokes 超过 {MAX_STROKES}")
        if len(self.pin_points) > MAX_PINS:
            raise WarperStateError("size_exceeded", f"pins 超过 {MAX_PINS}")
        stroke_ids = [s.stroke_id for s in self.strokes]
        if len(set(stroke_ids)) != len(stroke_ids):
            raise WarperStateError("invalid_id", "stroke_id 重复")
        pin_ids = [p.pin_id for p in self.pin_points]
        if len(set(pin_ids)) != len(pin_ids):
            raise WarperStateError("invalid_id", "pin_id 重复")

    # ---- 序列化 ----

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "algorithm_version": self.algorithm_version,
            "mode": self.mode,
            "color_space": self.color_space,
            "axis_angle": self.axis_angle,
            "grids": {name: grid.to_dict() for name, grid in sorted(self.grids.items())},
            "strokes": [stroke.to_dict() for stroke in self.strokes],
            "pins": [pin.to_dict() for pin in self.pin_points],
        }
        data.update(copy.deepcopy(self._extra_fields))
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WarperStateV2":
        if not isinstance(data, Mapping):
            raise WarperStateError("corrupt_payload", f"warper_state 需要 Mapping,得到 {type(data).__name__}")
        version = data.get("schema_version")
        if not isinstance(version, int):
            raise WarperStateError("corrupt_payload", f"schema_version 非法: {version!r}")
        if version > WARPER_SCHEMA_VERSION:
            raise WarperStateError(
                "schema_too_new",
                f"warper_state schema {version} 高于本版 {WARPER_SCHEMA_VERSION},保留原文且不可编辑/渲染")
        if version < WARPER_SCHEMA_VERSION:
            raise WarperStateError(
                "schema_too_old",
                f"warper_state schema {version} 低于本版 {WARPER_SCHEMA_VERSION}(暂无更低版本,载荷可疑)")
        mode = data.get("mode")
        if mode not in SUPPORTED_MODES:
            raise WarperStateError("invalid_mode", f"mode {mode!r} 非法")
        grids_raw = data.get("grids") or {}
        if not isinstance(grids_raw, Mapping):
            raise WarperStateError("corrupt_payload", "grids 需要 Mapping")
        grids = {name: WarperGrid.from_dict(grid, mode=mode) for name, grid in grids_raw.items()}
        strokes_raw = data.get("strokes") or ()
        if not isinstance(strokes_raw, (list, tuple)):
            raise WarperStateError("corrupt_payload", "strokes 需要 list/tuple")
        strokes = tuple(WarperStroke.from_dict(s) for s in strokes_raw)
        pins_raw = data.get("pins") or ()
        if not isinstance(pins_raw, (list, tuple)):
            raise WarperStateError("corrupt_payload", "pins 需要 list/tuple")
        pin_points = tuple(WarperPinPoint.from_dict(p) for p in pins_raw)
        known = frozenset(cls._KNOWN_KEYS)
        extras = {key: copy.deepcopy(value) for key, value in data.items() if key not in known}
        try:
            axis_angle = float(data.get("axis_angle", 0.0))
        except (TypeError, ValueError) as exc:
            raise WarperStateError("corrupt_payload", f"axis_angle 非法: {data.get('axis_angle')!r}") from exc
        return cls(
            schema_version=version,
            algorithm_version=str(data.get("algorithm_version", CURRENT_ALGORITHM)),
            mode=mode,
            color_space=str(data.get("color_space", "hsv")),
            axis_angle=axis_angle,
            grids=grids,
            strokes=strokes,
            pin_points=pin_points,
            _extra_fields=extras,
        )

    # ---- 派生 ----

    def copy(self) -> "WarperStateV2":
        """深拷贝(ndarray 重新冻结,extras 深拷贝)。"""
        return WarperStateV2.from_dict(self.to_dict())

    def with_grid(self, grid: WarperGrid) -> "WarperStateV2":
        """替换/新增一个网格,派生新状态。"""
        grids = dict(self.grids)
        grids[grid.name] = grid
        return WarperStateV2(
            schema_version=self.schema_version, algorithm_version=self.algorithm_version,
            mode=self.mode, color_space=self.color_space, axis_angle=self.axis_angle,
            grids=grids, strokes=self.strokes, pin_points=self.pin_points,
            _extra_fields=copy.deepcopy(self._extra_fields))

    def is_identity(self) -> bool:
        """全部网格零位移、无笔画/Pin 时为恒等(可安全丢弃不参与渲染)。"""
        if self.strokes or self.pin_points:
            return False
        return all(not grid.offsets.any() for grid in self.grids.values())


# ---- legacy 桥(计划 §4 CW-B01 步骤 3:旧数据按原坐标解释,bypass 保持) ----

def legacy_from_ndarray(array: np.ndarray | None) -> "WarperStateV2 | None":
    """把 legacy ``(N,N,2)`` HSV 位移方阵包装为 v2 状态;None 保持 bypass。"""
    if array is None:
        return None
    raw = np.asarray(array)
    if raw.ndim != 3 or raw.shape[2] != 2 or raw.shape[0] != raw.shape[1]:
        raise WarperStateError(
            "invalid_array", f"legacy warper 需要 (N,N,2) 方阵,得到 {raw.shape}")
    grid = WarperGrid(
        name=HS_GRID_NAME, rows=raw.shape[0], cols=raw.shape[1], components=2,
        semantics=("dh", "ds"),
        offsets=_as_offsets(raw, raw.shape[0], raw.shape[1], 2, "legacy warper"))
    return WarperStateV2(
        schema_version=WARPER_SCHEMA_VERSION,
        algorithm_version=LEGACY_ALGORITHM,
        mode="hue_saturation",
        color_space="hsv",
        axis_angle=0.0,
        grids={HS_GRID_NAME: grid},
    )


def legacy_ndarray(state: WarperStateV2 | None) -> np.ndarray | None:
    """v2 状态若可无损表达为 legacy 方阵则导出,否则 None(调用方按能力拒绝)。"""
    if state is None:
        return None
    if state.algorithm_version != LEGACY_ALGORITHM or state.mode != "hue_saturation":
        return None
    grid = state.grids.get(HS_GRID_NAME)
    if grid is None or grid.rows != grid.cols or grid.components != 2:
        return None
    return np.array(grid.offsets, dtype=np.float32, copy=True)


# ---- 能力契约(计划 §2.3) ----

@dataclass(frozen=True)
class WarperCapabilities:
    enabled: bool
    reason: str
    supported_modes: tuple[str, ...]
    backend: str
    revision: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "reason": self.reason,
            "supported_modes": list(self.supported_modes),
            "backend": self.backend,
            "revision": self.revision,
        }


def describe_warper_capabilities(
    state: WarperStateV2 | None = None,
    *,
    legacy: np.ndarray | None = None,
    revision: int | None = None,
) -> WarperCapabilities:
    """当前后端只有 legacy HSV GLSL/MSL 路径;新空间/模式如实报 unimplemented。"""
    supported = ("hue_saturation",)
    if state is None:
        if legacy is not None:
            return WarperCapabilities(True, "ok", supported, "legacy_hsv_glsl", revision)
        return WarperCapabilities(True, "bypass", supported, "none", revision)
    if state.algorithm_version == LEGACY_ALGORITHM:
        return WarperCapabilities(True, "ok", supported, "legacy_hsv_glsl", revision)
    if state.mode == "hue_saturation" and state.color_space == "hsv" \
            and legacy_ndarray(state) is not None:
        return WarperCapabilities(True, "legacy_expressible", supported, "legacy_hsv_glsl", revision)
    return WarperCapabilities(
        False, "backend_unimplemented", supported, "none", revision)


# ---- 请求 envelope 契约(CW-B02 的输入;计划 §2.1 建议字段) ----

@dataclass(frozen=True)
class WarperCommandRequest:
    """UI → host 的命令信封。身份从 host 上下文取得,UI 不自行推断目标。"""
    request_id: str
    phase: str  # COMMAND_PHASES
    operation: str
    payload: Mapping[str, Any]
    gesture_id: str | None = None
    base_revision: int | None = None
    context_revision: int | None = None
    project_id: str | None = None
    clip_id: str | None = None
    corrector_id: str | None = None
    layer_id: str | None = None

    def diagnostics(self) -> list[str]:
        issues: list[str] = []
        if not isinstance(self.request_id, str) or not self.request_id:
            issues.append("request_id 必须非空")
        if self.phase not in COMMAND_PHASES:
            issues.append(f"phase {self.phase!r} 不在 {COMMAND_PHASES}")
        if not isinstance(self.operation, str) or not self.operation:
            issues.append("operation 必须非空")
        if not isinstance(self.payload, Mapping):
            issues.append("payload 需要 Mapping")
        if self.phase in ("begin", "update") and self.gesture_id is None:
            issues.append(f"phase {self.phase} 需要 gesture_id")
        return issues
