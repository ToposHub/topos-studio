"""
Corner pin 导出（阶段 8，L2 平面/透视跟踪）。

把平面跟踪结果导出为逐帧四角坐标（corner pin 数据），供外部
合成软件使用：

- 角序固定 ``(TL, TR, BR, BL)``，与素材真值约定一致；
- 坐标为 **source frame 原始分辨率像素**；
- 手动修正优先于自动结果（``TrackingBinding.transform_at``）；
- 失败帧与无结果帧不输出坐标（不造假数据），记录在 ``skipped`` 中。
"""

from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from src.shared.tracking.track_data import TrackingBinding
from src.shared.tracking.types import TrackStatus

# 导出文件格式版本
CORNER_PIN_VERSION = 1

# 角序常量（与 fixture ground truth 约定一致）
CORNER_ORDER = ("TL", "TR", "BR", "BL")

Point = Tuple[float, float]
Rect = Tuple[float, float, float, float]


def corner_positions(
    matrix: np.ndarray, pattern_rect_px: Rect
) -> Optional[List[Point]]:
    """pattern 四角 (TL, TR, BR, BL) 经单应性变换后的像素坐标。

    Args:
        matrix: 3x3 单应性（参考帧 → 当前帧，与 ``TrackTransform`` 同语义）。
        pattern_rect_px: pattern 区域（原始分辨率像素 (x, y, w, h)）。

    出现非有限值或齐次除零时返回 None（不输出爆炸坐标）。
    """
    x, y, w, h = pattern_rect_px
    corners = np.array(
        [[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.float64
    )
    m = np.asarray(matrix, dtype=np.float64).reshape(3, 3)
    proj = (m @ np.column_stack([corners, np.ones(4)]).T).T
    if not np.all(np.isfinite(proj)):
        return None
    wz = proj[:, 2]
    if np.any(np.abs(wz) < 1e-9):
        return None
    pts = proj[:, :2] / wz[:, None]
    if not np.all(np.isfinite(pts)):
        return None
    return [(float(px), float(py)) for px, py in pts]


def normalized_corners(
    corners: List[Point], width: int, height: int
) -> Optional[List[Point]]:
    """像素四角 → 归一化四角（供 overlay 显示）。"""
    if width <= 0 or height <= 0:
        return None
    return [(px / float(width), py / float(height)) for px, py in corners]


def build_corner_pin(
    binding: TrackingBinding,
    pattern_rect_px: Rect,
    frame_start: Optional[int] = None,
    frame_end: Optional[int] = None,
) -> Dict:
    """从 binding 构建 corner pin 数据结构。

    - 逐帧用 ``binding.transform_at``（手动优先）求四角；
    - 失败/无结果/非有限帧跳过并记录到 ``skipped``；
    - ``frame_start``/``frame_end`` 缺省时取有结果的最小/最大帧。

    Returns:
        ``{"reference_frame", "pattern_rect", "frames", "skipped"}``
    """
    rng = binding.frame_range_with_results()
    if rng is None:
        return {
            "reference_frame": binding.reference_frame,
            "pattern_rect": [float(v) for v in pattern_rect_px],
            "frames": {},
            "skipped": [],
        }
    start = rng[0] if frame_start is None else int(frame_start)
    end = rng[1] if frame_end is None else int(frame_end)

    frames: Dict[int, List[Point]] = {}
    skipped: List[int] = []
    for frame in range(start, end + 1):
        transform = binding.transform_at(frame)
        if transform is None or transform.status == TrackStatus.FAILED:
            skipped.append(frame)
            continue
        matrix = transform.matrix_np
        if not np.all(np.isfinite(matrix)):
            skipped.append(frame)
            continue
        corners = corner_positions(matrix, pattern_rect_px)
        if corners is None:
            skipped.append(frame)
            continue
        frames[frame] = corners

    return {
        "reference_frame": binding.reference_frame,
        "pattern_rect": [float(v) for v in pattern_rect_px],
        "frames": frames,
        "skipped": skipped,
    }


def export_corner_pin_json(
    binding: TrackingBinding,
    pattern_rect_px: Rect,
    path,
    frame_size: Optional[Tuple[int, int]] = None,
    frame_start: Optional[int] = None,
    frame_end: Optional[int] = None,
) -> Dict:
    """导出 corner pin JSON 文件，返回写入的数据结构。

    JSON 结构::

        {
            "format": "corner_pin", "version": 1,
            "reference_frame": ..., "frame_size": [w, h] | null,
            "corner_order": ["TL", "TR", "BR", "BL"],
            "pattern_rect": [x, y, w, h],
            "frames": {"<frame>": [[x1, y1], ... x4]},
            "skipped": [frame, ...]
        }
    """
    data = build_corner_pin(binding, pattern_rect_px, frame_start, frame_end)
    payload = {
        "format": "corner_pin",
        "version": CORNER_PIN_VERSION,
        "reference_frame": int(data["reference_frame"]),
        "frame_size": [int(v) for v in frame_size] if frame_size else None,
        "corner_order": list(CORNER_ORDER),
        "pattern_rect": data["pattern_rect"],
        "frames": {
            str(frame): [[round(px, 4), round(py, 4)] for px, py in corners]
            for frame, corners in sorted(data["frames"].items())
        },
        "skipped": data["skipped"],
    }
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def corner_pin_error(
    frames: Dict[int, List[Point]],
    truth_corners: Dict[int, np.ndarray],
) -> Dict[int, float]:
    """逐帧四角平均误差（与真值比较，供验收测试）。

    只统计双方都有的帧；返回 ``{frame: mean_corner_error_px}``。
    """
    errors: Dict[int, float] = {}
    for frame, corners in frames.items():
        truth = truth_corners.get(frame)
        if truth is None:
            continue
        truth = np.asarray(truth, dtype=np.float64).reshape(4, 2)
        est = np.asarray(corners, dtype=np.float64).reshape(4, 2)
        errors[frame] = float(np.mean(np.linalg.norm(truth - est, axis=1)))
    return errors


# ----------------------------------------------------------------
# 阶段 8 兼容入口
# ----------------------------------------------------------------

@dataclass(frozen=True)
class CornerPinValidation:
    """四角几何诊断；保留给 UI 和离线验收使用。"""

    valid: bool
    area: float
    area_ratio: float
    convex: bool
    finite: bool
    reason: str = ""


def transform_corners(
    matrix: Sequence[float] | np.ndarray,
    pattern_region: Sequence[float],
    source_width: int,
    source_height: int,
) -> Tuple[Point, Point, Point, Point]:
    """把归一化 pattern 区域按 source frame 单应性转换为像素四角。"""
    if len(pattern_region) != 4:
        raise ValueError("pattern_region 必须为 (x, y, w, h)")
    x, y, w, h = (float(v) for v in pattern_region)
    rect = (x * source_width, y * source_height,
            w * source_width, h * source_height)
    corners = corner_positions(np.asarray(matrix), rect)
    if corners is None:
        raise ValueError("单应性投影出现非有限值或齐次除零")
    return tuple(corners)  # type: ignore[return-value]


def polygon_signed_area(points: Sequence[Sequence[float]]) -> float:
    p = np.asarray(points, dtype=np.float64)
    if p.shape != (4, 2):
        raise ValueError("corners 必须是 4 个二维点")
    return float(0.5 * np.sum(
        p[:, 0] * np.roll(p[:, 1], -1)
        - p[:, 1] * np.roll(p[:, 0], -1)
    ))


def polygon_is_convex(points: Sequence[Sequence[float]], eps: float = 1e-9) -> bool:
    p = np.asarray(points, dtype=np.float64)
    if p.shape != (4, 2) or not np.all(np.isfinite(p)):
        return False
    crosses = []
    for i in range(4):
        a = p[(i + 1) % 4] - p[i]
        b = p[(i + 2) % 4] - p[(i + 1) % 4]
        crosses.append(float(a[0] * b[1] - a[1] * b[0]))
    return all(value > eps for value in crosses)


def validate_corner_pin(
    corners: Sequence[Sequence[float]],
    reference_corners: Optional[Sequence[Sequence[float]]] = None,
    min_area_ratio: float = 0.10,
    max_area_ratio: float = 10.0,
) -> CornerPinValidation:
    """检查四角有限性、凸性和相对于参考平面的面积比例。"""
    try:
        p = np.asarray(corners, dtype=np.float64)
        if p.shape != (4, 2):
            raise ValueError
    except (TypeError, ValueError):
        return CornerPinValidation(False, 0.0, 0.0, False, False, "四角格式错误")
    finite = bool(np.all(np.isfinite(p)))
    area = abs(polygon_signed_area(p)) if finite else 0.0
    convex = polygon_is_convex(p) if finite else False
    ref_area = area
    if reference_corners is not None:
        try:
            ref_area = abs(polygon_signed_area(reference_corners))
        except (TypeError, ValueError):
            ref_area = 0.0
    area_ratio = area / ref_area if ref_area > 1e-12 else 0.0
    if not finite:
        return CornerPinValidation(False, area, area_ratio, False, False, "四角含非有限值")
    if not convex:
        return CornerPinValidation(False, area, area_ratio, False, True, "四角退化或顶点顺序反转")
    if area <= 1e-12:
        return CornerPinValidation(False, area, area_ratio, True, True, "四角面积为零")
    if reference_corners is not None and area_ratio < min_area_ratio:
        return CornerPinValidation(False, area, area_ratio, True, True, "透视过度，接近侧面")
    if reference_corners is not None and area_ratio > max_area_ratio:
        return CornerPinValidation(False, area, area_ratio, True, True, "四角面积爆炸")
    return CornerPinValidation(True, area, area_ratio, True, True, "")


def build_corner_pin_export(
    binding: TrackingBinding,
    source_width: int,
    source_height: int,
    include_missing: bool = True,
) -> dict:
    """构造带状态/置信度的 Corner Pin JSON 对象。

    该入口与旧的 ``build_corner_pin`` 并存：旧接口保持紧凑的帧字典，
    新接口给面板和诊断使用更完整的逐帧记录。
    """
    if source_width <= 0 or source_height <= 0:
        raise ValueError("source_width/source_height 必须为正数")
    pattern = tuple(float(v) for v in binding.pattern_region)
    reference = transform_corners(np.eye(3), pattern, source_width, source_height)
    frames = set(binding.transforms) | set(binding.manual_corrections)
    frames.update(range(int(binding.source_frame_start), int(binding.source_frame_end) + 1))
    entries = []
    for frame in sorted(frames):
        transform = binding.transform_at(frame)
        if transform is None:
            if include_missing:
                entries.append({
                    "frame": int(frame), "status": TrackStatus.FAILED.value,
                    "confidence": 0.0, "failure_reason": "missing",
                    "corners": None, "matrix": None,
                })
            continue
        entry = {
            "frame": int(frame),
            "status": transform.status.value,
            "confidence": float(transform.confidence),
            "failure_reason": transform.failure_reason.value,
            "corners": None,
            "matrix": list(transform.matrix),
        }
        if transform.status != TrackStatus.FAILED:
            matrix = np.asarray(transform.matrix, dtype=np.float64).reshape(3, 3)
            if not np.all(np.isfinite(matrix)):
                # 单个坏帧（非有限矩阵）只标记自身，不输出坐标、不造假
                # 数据，也不让整次导出失败（与 build_corner_pin 的 skip
                # 行为对齐；TRS/AFFINE 模式无单应性稳定性检查兜底）
                entry["area"] = 0.0
                entry["area_ratio"] = 0.0
                entry["valid_geometry"] = False
                entry["geometry_reason"] = "变换矩阵含非有限值"
            else:
                try:
                    corners = transform_corners(
                        transform.matrix, pattern, source_width, source_height
                    )
                except ValueError:
                    # 有限矩阵仍可能齐次除零（w 分量近 0）：按坏帧标记而非
                    # 让整次导出抛异常
                    entry["area"] = 0.0
                    entry["area_ratio"] = 0.0
                    entry["valid_geometry"] = False
                    entry["geometry_reason"] = "单应性投影出现非有限值或齐次除零"
                else:
                    validation = validate_corner_pin(corners, reference)
                    entry["corners"] = [{"x": float(x), "y": float(y)} for x, y in corners]
                    entry["area"] = validation.area
                    entry["area_ratio"] = validation.area_ratio
                    entry["valid_geometry"] = validation.valid
                    if not validation.valid:
                        entry["geometry_reason"] = validation.reason
        entries.append(entry)
    return {
        "schema": "topos.color.corner_pin",
        "schema_version": 1,
        "binding_id": binding.binding_id,
        "clip_id": binding.clip_id,
        "shape_id": binding.shape_id,
        "mode": binding.mode.value,
        "reference_frame": int(binding.reference_frame),
        "source_frame_start": int(binding.source_frame_start),
        "source_frame_end": int(binding.source_frame_end),
        "source_size": {"width": int(source_width), "height": int(source_height)},
        "pattern_region": list(pattern),
        "corner_order": list(CORNER_ORDER),
        "reference_corners": [{"x": float(x), "y": float(y)} for x, y in reference],
        "frames": entries,
    }


def write_corner_pin_export(
    binding: TrackingBinding,
    source_width: int,
    source_height: int,
    output_path,
    include_missing: bool = True,
) -> dict:
    data = build_corner_pin_export(
        binding, source_width, source_height, include_missing=include_missing
    )
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data
