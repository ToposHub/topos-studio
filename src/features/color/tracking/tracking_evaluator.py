"""
跟踪结果求值与应用（阶段 6）。

原则（计划 §阶段6）：
- 不逐帧复制 ``MaskShape``：运行时按当前帧对 ``base_geometry + tracking_transform``
  求值，原始形状数据不被覆盖。
- 参考帧恒等：参考帧变换为单位矩阵，求值结果必须与基准几何完全一致
  （不双重变换）。
- 手动修正优先于自动结果（复用 ``TrackingBinding.transform_at``）。
- 关闭 binding 或无结果时回退原始几何（返回 None，调用方用原始形状）。
- 失败帧不输出假有效几何：回退基准几何（失败策略的保持上一帧/重捕获
  属于阶段 7）。
- 渲染链只消费求值后的几何字典，不访问 tracking store。

坐标约定：``TrackTransform.matrix`` 为**原始分辨率像素坐标**下的
参考帧→当前帧单应性；蒙版几何为归一化坐标（0-1）。求值时做共轭换算
``H_norm = N @ H @ N⁻¹``（N = diag(1/w, 1/h, 1)）。
"""

from __future__ import annotations

import copy
import math
from typing import Dict, Optional, Tuple

import numpy as np

from src.shared.keyframe import InterpolationType, Keyframe, KeyframeTrack

from src.shared.tracking.coordinate_map import ClipFrameMapper
from src.shared.tracking.track_data import TrackingBinding
from .tracking_store import TrackingStore
from src.shared.tracking.types import TrackStatus

FrameSize = Tuple[int, int]

# 烘焙关键帧输出的轨道名
BAKE_TRACK_NAMES = ("center_x", "center_y", "size_x", "size_y", "rotation")


# ================================================================
# 几何快照
# ================================================================

def capture_base_geometry(shape) -> dict:
    """从蒙版形状捕获几何快照（求值基准，参考帧时刻）。

    支持 ``MaskShape`` 对象或等价字典。曲线点做深拷贝，
    之后的形状编辑不影响快照。
    """
    get = shape.get if isinstance(shape, dict) else lambda k, d=None: getattr(shape, k, d)
    shape_type = get("type", "")
    base: dict = {
        "type": shape_type,
        "center": list(get("center", [0.5, 0.5])),
        "size": list(get("size", [0.0, 0.0])),
        "rotation": float(get("rotation", 0.0)),
    }
    if shape_type == "curve":
        points = get("points") or []
        # 保留每个点的完整字段（fi/fo 逐点羽化、type 平滑角点等），
        # 只把几何键 pos/c1/c2 规整为列表，供后续单应性变换。
        base["points"] = [
            {
                **copy.deepcopy(p),
                "pos": list(p.get("pos", [0.0, 0.0])),
                "c1": list(p.get("c1", [0.0, 0.0])),
                "c2": list(p.get("c2", [0.0, 0.0])),
            }
            for p in points
        ]
    elif shape_type == "gradient":
        base["grad_points"] = [list(point) for point in get(
            "grad_points", [[0.4, 0.5], [0.6, 0.5]])]
    return base


# ================================================================
# 变换求值（纯函数）
# ================================================================

def _normalized_homography(matrix_np: np.ndarray, width: int, height: int) -> np.ndarray:
    """像素单应性 → 归一化坐标单应性（共轭换算）。"""
    n = np.diag([1.0 / width, 1.0 / height, 1.0])
    n_inv = np.diag([float(width), float(height), 1.0])
    return n @ matrix_np @ n_inv


def _apply_norm_point(h: np.ndarray, x: float, y: float) -> Tuple[float, float]:
    v = h @ np.array([x, y, 1.0], dtype=np.float64)
    w_ = v[2] if abs(v[2]) > 1e-12 else 1.0
    return float(v[0] / w_), float(v[1] / w_)


def _is_identity(matrix_np: np.ndarray) -> bool:
    return bool(np.allclose(matrix_np, np.eye(3), atol=1e-12))


def _copy_geometry(value):
    """几何字典专用轻量深拷贝（AUD-24902）。

    基准几何结构仅含 dict/list/标量（见 ``capture_base_geometry``），
    递归重建容器即可获得与 ``copy.deepcopy`` 等价的隔离效果，但免去
    deepcopy 的 memo/弱引用机制开销——本函数位于播放热路径
    （每形状每帧一次），持续分配必须廉价。非容器值（含 tuple、
    np 标量等不可变对象）原样共享，与 deepcopy 的语义一致。
    """
    if isinstance(value, dict):
        return {k: _copy_geometry(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy_geometry(v) for v in value]
    return value


def _transform_rect_geometry(base: dict, h_px: np.ndarray,
                             width: int, height: int) -> dict:
    """circle/rect：在**像素空间**变换四角后重建 center/size/rotation。

    像素空间各向同性，旋转角度拟合不受帧宽高比影响；
    结果再归一化回 0-1 坐标。
    """
    cx, cy = base["center"][0] * width, base["center"][1] * height
    w_px, hh_px = base["size"][0] * width, base["size"][1] * height
    theta = math.radians(base["rotation"])
    ct, st = math.cos(theta), math.sin(theta)

    local = [(-w_px / 2, -hh_px / 2), (w_px / 2, -hh_px / 2),
             (w_px / 2, hh_px / 2), (-w_px / 2, hh_px / 2)]
    world = [
        (cx + lx * ct - ly * st, cy + lx * st + ly * ct) for lx, ly in local
    ]
    mapped = [_apply_norm_point(h_px, x, y) for x, y in world]

    new_cx = sum(p[0] for p in mapped) / 4.0
    new_cy = sum(p[1] for p in mapped) / 4.0
    e_top = (mapped[1][0] - mapped[0][0], mapped[1][1] - mapped[0][1])
    e_left = (mapped[3][0] - mapped[0][0], mapped[3][1] - mapped[0][1])
    new_w = math.hypot(*e_top)
    new_h = math.hypot(*e_left)
    new_rot = math.degrees(math.atan2(e_top[1], e_top[0]))

    result = _copy_geometry(base)
    result["center"] = [new_cx / width, new_cy / height]
    result["size"] = [new_w / width, new_h / height]
    result["rotation"] = new_rot
    return result


def _transform_curve_geometry(base: dict, h: np.ndarray) -> dict:
    """curve：对所有 pos/c1/c2 施加同一变换。"""
    result = _copy_geometry(base)
    for p in result["points"]:
        for key in ("pos", "c1", "c2"):
            x, y = p[key]
            p[key] = list(_apply_norm_point(h, x, y))
    return result


def _window_rotation(base: dict, frame_size: FrameSize) -> np.ndarray:
    """Rotation metadata uses the window's center in pixel coordinates."""
    width, height = frame_size
    cx, cy = base["center"][0] * width, base["center"][1] * height
    angle = math.radians(float(base.get("rotation", 0.0)))
    ct, st = math.cos(angle), math.sin(angle)
    return np.array([[ct, -st, cx * (1.0 - ct) + cy * st],
                     [st, ct, cy * (1.0 - ct) - cx * st],
                     [0.0, 0.0, 1.0]])


def apply_transform_to_geometry(base: dict, matrix_np: np.ndarray,
                                frame_size: FrameSize) -> dict:
    """把跟踪变换施加到基准几何，返回新的几何字典（不修改入参）。

    参考帧（单位矩阵）直接返回基准几何副本，不做双重变换。
    不支持几何点变换的类型（brush/bitmap）原样返回副本。
    """
    # Editor metadata may contain many frame overrides. Keep it out of the
    # per-frame geometry copies and out of recursive transforms.
    base = {key: value for key, value in base.items() if not key.startswith("_")}
    width, height = frame_size
    if width <= 0 or height <= 0:
        raise ValueError("帧尺寸必须为正")
    if _is_identity(np.asarray(matrix_np, dtype=np.float64).reshape(3, 3)):
        return _copy_geometry(base)
    m = np.asarray(matrix_np, dtype=np.float64).reshape(3, 3)
    shape_type = base.get("type")
    if shape_type in ("circle", "rect"):
        # rect/circle 在像素空间拟合（各向同性，旋转角不受宽高比影响）
        return _transform_rect_geometry(base, m, width, height)
    if shape_type == "curve":
        h = _normalized_homography(m @ _window_rotation(base, frame_size), width, height)
        result = _transform_curve_geometry(base, h)
        result["center"] = list(_apply_norm_point(
            _normalized_homography(m, width, height), *base["center"]))
        result["rotation"] = 0.0
        return result
    if shape_type == "gradient" and "grad_points" in base:
        result = _copy_geometry(base)
        # Bake rotation once so the renderer and overlay use identical points.
        h = _normalized_homography(m @ _window_rotation(base, frame_size), width, height)
        result["grad_points"] = [list(_apply_norm_point(h, *point))
                                 for point in base.get("grad_points", [])]
        result["center"] = list(_apply_norm_point(
            _normalized_homography(m, width, height), *base["center"]))
        result["rotation"] = 0.0
        return result
    return _copy_geometry(base)


# ================================================================
# 求值器（面向 store 的门面）
# ================================================================

class TrackingEvaluator:
    """按当前帧求值「基准几何 + 跟踪变换」。

    渲染链只调用 ``evaluate_geometry`` 拿到普通字典，不接触 tracking store。
    """

    def __init__(self, store: TrackingStore) -> None:
        self._store = store
        self._enabled: Dict[str, bool] = {}

    # ---- binding 开关 ----

    def set_binding_enabled(self, binding_id: str, enabled: bool) -> None:
        self._enabled[binding_id] = bool(enabled)

    def is_binding_enabled(self, binding_id: str) -> bool:
        return self._enabled.get(binding_id, True)

    # ---- 求值 ----

    def evaluate_geometry(self, clip_id: str, shape_id: str, source_frame: int,
                          frame_size: FrameSize) -> Optional[dict]:
        """求值指定帧的蒙版几何。

        返回 None 表示没有可用跟踪（无 binding / 已禁用 / 帧越界 /
        无基准几何），调用方应使用原始形状几何。
        """
        binding = self._store.get_binding(clip_id, shape_id)
        if binding is None or not self.is_binding_enabled(binding.binding_id):
            return None
        if binding.base_geometry is None or not binding.contains_frame(source_frame):
            return None
        return _evaluate_binding_frame(binding, source_frame, frame_size)

    def evaluate_geometry_at_timeline(
            self, clip_id: str, shape_id: str, timeline_frame: int,
            mapper: ClipFrameMapper, frame_size: FrameSize) -> Optional[dict]:
        """timeline frame 入口：先映射到 source frame 再求值。"""
        try:
            source_frame = mapper.timeline_to_source_frame(timeline_frame)
        except ValueError:
            return None
        return self.evaluate_geometry(clip_id, shape_id, source_frame, frame_size)


# ================================================================
# 烘焙为关键帧
# ================================================================

def bake_binding_keyframes(
        binding: TrackingBinding,
        frame_size: FrameSize,
        interval: int = 1,
) -> Dict[str, KeyframeTrack]:
    """把跟踪结果烘焙为关键帧轨道（采样间隔可设）。

    - 只烘焙有结果且非失败的帧；失败帧跳过（不写入假值）。
    - 烘焙不修改 binding 本身（原始跟踪数据保留）。
    - 输出轨道：center_x / center_y / size_x / size_y / rotation。
    """
    if interval < 1:
        raise ValueError("interval 必须 >= 1")
    rng = binding.frame_range_with_results()
    tracks = {name: KeyframeTrack(name) for name in BAKE_TRACK_NAMES}
    if rng is None or binding.base_geometry is None:
        return tracks
    start, end = rng
    frames = list(range(start, end + 1, interval))
    if frames[-1] != end:
        frames.append(end)
    evaluator_geometry = lambda f: _evaluate_binding_frame(binding, f, frame_size)
    for frame in frames:
        transform = binding.transform_at(frame)
        if transform is None or transform.status == TrackStatus.FAILED:
            continue
        geometry = evaluator_geometry(frame)
        if geometry is None:
            continue
        values = {
            "center_x": geometry["center"][0],
            "center_y": geometry["center"][1],
            "size_x": geometry["size"][0],
            "size_y": geometry["size"][1],
            "rotation": geometry["rotation"],
        }
        for name, track in tracks.items():
            track.add_keyframe(Keyframe(
                frame=frame, value=float(values[name]),
                interpolation=InterpolationType.LINEAR,
            ))
    return tracks


def _evaluate_binding_frame(binding: TrackingBinding, source_frame: int,
                            frame_size: FrameSize) -> Optional[dict]:
    """单 binding 求值（供烘焙使用，不走 store）。"""
    base = binding.base_geometry
    if base is None or not binding.contains_frame(source_frame):
        return None
    overrides = base.get("_frame_geometry", {})
    override = overrides.get(str(source_frame)) if isinstance(overrides, dict) else None
    manual = binding.manual_corrections.get(source_frame)
    if isinstance(override, dict) and manual is not None and manual.status != TrackStatus.FAILED:
        return _copy_geometry(override)
    transform = binding.transform_at(source_frame)
    if transform is None or transform.status == TrackStatus.FAILED:
        return _copy_geometry({key: value for key, value in base.items()
                               if not key.startswith("_")})
    from .window_edit import filter_motion_matrix
    matrix = filter_motion_matrix(transform.matrix_np, base, frame_size,
                                  base.get("_motion_channels", {}))
    return apply_transform_to_geometry(
        base, matrix, frame_size)
