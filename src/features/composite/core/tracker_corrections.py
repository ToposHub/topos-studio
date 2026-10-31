"""M23.1（R08）· Tracker 手工修正/失败段增量重跟的领域服务。

接既有 TrackerNode 的 track_data（节点参数，序列化随工程往返）：

- ``manual_corrections``：帧 → 用户修正变换。手工修正**覆盖**该帧的
  自动结果（status="manual"），并作为增量重跟的锚点；
- ``plan_incremental_retrack``：给定失败段（status != valid 的连续帧）
  与锚点（段边界两侧最近的 valid/manual 帧），产出重跟计划（起止帧、
  起点锚变换）——只重分析失败段，不整条重跑；
- ``apply_track_update``：把增量结果合并回 track_data（手工修正保留，
  仅覆盖计划区间内的自动帧）——整体经节点 set_track_data 写回
  （参数 revision 自增 → 缓存失效），一次 Undo 由页面命令层包裹。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

TRACK_STATUS_VALID = "valid"
TRACK_STATUS_MANUAL = "manual"


@dataclass(frozen=True)
class RetrackSegment:
    """一个待重跟的失败段（闭区间帧号）。"""

    start_frame: int
    end_frame: int
    anchor_frame: int          # 重跟起点锚（段前最近 valid/manual 帧）
    anchor_transform: Dict[str, Any]


@dataclass(frozen=True)
class IncrementalRetrackPlan:
    """增量重跟计划（只含失败段；空 = 无需重跟）。"""

    segments: Tuple[RetrackSegment, ...] = ()
    manual_corrections: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def total_frames(self) -> int:
        return sum(s.end_frame - s.start_frame + 1 for s in self.segments)


def get_manual_corrections(track_data: Optional[Dict[str, Any]]
                           ) -> Dict[str, Dict[str, Any]]:
    """读取手工修正表（帧号 str → transform dict）。"""
    if not track_data:
        return {}
    raw = track_data.get("manual_corrections")
    if not isinstance(raw, dict):
        return {}
    return {str(int(k)): dict(v) for k, v in raw.items()
            if isinstance(v, dict)}


def set_manual_correction(track_data: Dict[str, Any], frame: int,
                          transform: Dict[str, Any]) -> Dict[str, Any]:
    """记录/更新一帧手工修正（返回**新 dict**，不改入参）。

    修正同时写入 transforms（status="manual"）——bake/下游按帧取变换
    时即消费修正值；manual_corrections 表保留修正历史供增量重跟锚定。
    """
    out = dict(track_data)
    transforms = dict(out.get("transforms") or {})
    corrections = dict(out.get("manual_corrections") or {})
    key = str(int(frame))
    entry = dict(transform)
    entry["status"] = TRACK_STATUS_MANUAL
    transforms[key] = entry
    corrections[key] = dict(transform)
    out["transforms"] = transforms
    out["manual_corrections"] = corrections
    return out


def _sorted_frames(transforms: Dict[str, Any]) -> List[int]:
    frames = []
    for key in (transforms or {}):
        try:
            frames.append(int(float(key)))
        except (TypeError, ValueError):
            continue
    return sorted(frames)


def plan_incremental_retrack(
    track_data: Optional[Dict[str, Any]],
) -> IncrementalRetrackPlan:
    """找出全部失败段并产出增量重跟计划。

    失败帧 = transforms 中 status 既非 valid 也非 manual 的帧（分析
    失败/置信不足）；手工修正帧视为锚点（不重跟）。段锚 = 段起点之前
    最近的 valid/manual 帧及其变换（无前锚——段从头开始——用参考帧
    或第一帧的变换，缺省 identity translation）。
    """
    if not track_data:
        return IncrementalRetrackPlan(
            manual_corrections=get_manual_corrections(track_data))
    transforms = track_data.get("transforms") or {}
    corrections = get_manual_corrections(track_data)

    good: Dict[int, Dict[str, Any]] = {}
    bad: List[int] = []
    for key, value in transforms.items():
        try:
            frame = int(float(key))
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        status = str(value.get("status", ""))
        if status in (TRACK_STATUS_VALID, TRACK_STATUS_MANUAL):
            good[frame] = value
        else:
            bad.append(frame)
    bad.sort()
    if not bad:
        return IncrementalRetrackPlan(manual_corrections=corrections)

    # 连续段划分
    segments_raw: List[List[int]] = []
    for frame in bad:
        if segments_raw and frame == segments_raw[-1][-1] + 1:
            segments_raw[-1].append(frame)
        else:
            segments_raw.append([frame])

    segments: List[RetrackSegment] = []
    for seg in segments_raw:
        start, end = seg[0], seg[-1]
        # 段前最近锚
        anchor_frame = -1
        for gf in sorted(good):
            if gf < start:
                anchor_frame = gf
        if anchor_frame >= 0:
            anchor_t = dict(good[anchor_frame])
        else:
            ref = int(track_data.get("reference_frame", 0) or 0)
            anchor_frame = ref
            anchor_t = dict(transforms.get(str(ref), {})
                            or {"tx": 0.0, "ty": 0.0})
        segments.append(RetrackSegment(
            start_frame=start, end_frame=end,
            anchor_frame=anchor_frame, anchor_transform=anchor_t))
    return IncrementalRetrackPlan(
        segments=tuple(segments), manual_corrections=corrections)


def apply_track_update(
    track_data: Dict[str, Any],
    new_transforms: Dict[int, Dict[str, Any]],
    plan: IncrementalRetrackPlan,
) -> Dict[str, Any]:
    """把增量重跟结果合并回 track_data。

    只覆盖计划区间内的帧（``new_transforms``）；区间外自动结果与全部
    手工修正保留——增量结果不触碰段外内容，手工修正（锚）不被覆盖。
    """
    out = dict(track_data)
    transforms = dict(out.get("transforms") or {})
    in_plan = {f for seg in plan.segments
               for f in range(seg.start_frame, seg.end_frame + 1)}
    corrections = get_manual_corrections(out)
    for frame_str, value in new_transforms.items():
        frame = int(frame_str)
        if frame not in in_plan:
            continue
        key = str(frame)
        entry = dict(value)
        # 手工修正帧不被增量结果覆盖（锚语义）
        if key in corrections:
            continue
        entry.setdefault("status", TRACK_STATUS_VALID)
        transforms[key] = entry
    out["transforms"] = transforms
    return out


# ── N13 · 真实 CPU 重跟引擎（平移模式，归一化互相关模板匹配） ──────


def retrack_translation_segment(
    frame_getter,
    segment: "RetrackSegment",
    pattern_rect: Tuple[int, int, int, int],
    *,
    search_radius: int = 48,
    is_cancelled=None,
) -> Tuple[Dict[str, Dict[str, Any]], str]:
    """对一个失败段做真实 CPU 重跟（N13，OpenCV 模板匹配）。

    以锚帧 ``pattern_rect=(x, y, w, h)`` 的图像窗为模板，逐帧在预测
    位置 ± ``search_radius`` 的搜索窗内做归一化互相关，峰值偏移即该
    帧相对锚的平移增量；输出变换 = 锚变换 + 累积偏移（tx/ty，像素）。

    Args:
        frame_getter: ``frame -> ndarray(HW3 uint8)|None``（生产由
            控制器从节点上游逐帧源构建；None 帧中止该段）。
        pattern_rect: 锚帧模板窗（像素坐标）。
        is_cancelled: 可取消回调（协作中止，返回部分结果 + 说明）。

    Returns:
        (transforms_by_frame, error)：error 空 = 全段成功；非空 = 中止
        原因（部分结果仍返回，不冒充成功——调用方按失败段保留）。
    """
    try:
        import cv2
        import numpy as np  # noqa: F401 — 依赖探测（cv2/numpy 缺失时按失败段返回）
    except ImportError:  # pragma: no cover — cv2/numpy 为核心依赖
        return {}, "opencv/numpy unavailable"

    anchor_img = frame_getter(segment.anchor_frame)
    if anchor_img is None:
        return {}, f"anchor frame {segment.anchor_frame} unavailable"
    px, py, pw, ph = (int(v) for v in pattern_rect)
    if pw <= 4 or ph <= 4:
        return {}, "pattern too small (<=4px)"
    h_img, w_img = anchor_img.shape[:2]
    px = max(0, min(px, w_img - pw - 1))
    py = max(0, min(py, h_img - ph - 1))
    anchor_crop = anchor_img[py:py + ph, px:px + pw]
    if anchor_crop.shape[0] < ph or anchor_crop.shape[1] < pw:
        return {}, "anchor pattern out of bounds"
    template = cv2.cvtColor(anchor_crop, cv2.COLOR_BGR2GRAY)

    base_tx = float(segment.anchor_transform.get("tx", 0.0) or 0.0)
    base_ty = float(segment.anchor_transform.get("ty", 0.0) or 0.0)
    results: Dict[str, Dict[str, Any]] = {}
    cum_dx, cum_dy = 0.0, 0.0
    for frame in range(segment.start_frame, segment.end_frame + 1):
        if is_cancelled is not None and is_cancelled():
            return results, "cancelled"
        img = frame_getter(frame)
        if img is None:
            return results, f"frame {frame} unavailable"
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) \
            if img.ndim == 3 else img
        cx = px + int(round(cum_dx))
        cy = py + int(round(cum_dy))
        x0 = max(0, cx - search_radius)
        y0 = max(0, cy - search_radius)
        x1 = min(gray.shape[1], cx + pw + search_radius)
        y1 = min(gray.shape[0], cy + ph + search_radius)
        if x1 - x0 < pw or y1 - y0 < ph:
            return results, f"frame {frame} search window too small"
        search = gray[y0:y1, x0:x1]
        matched = cv2.matchTemplate(
            search, template, cv2.TM_CCOEFF_NORMED)
        _, _, _, max_loc = cv2.minMaxLoc(matched)
        new_dx = (x0 + max_loc[0]) - px
        new_dy = (y0 + max_loc[1]) - py
        # 累积平滑：段内偏移连续（帧间跳变 >2×搜索半径视为匹配失败）
        if abs(new_dx - cum_dx) > 2 * search_radius or \
                abs(new_dy - cum_dy) > 2 * search_radius:
            return results, (
                f"frame {frame} match jumped beyond search radius")
        cum_dx, cum_dy = new_dx, new_dy
        results[str(frame)] = {
            "tx": base_tx + float(cum_dx),
            "ty": base_ty + float(cum_dy),
            "status": TRACK_STATUS_VALID,
        }
    return results, ""


def retrack_plan_async(frame_getter, track_data: Dict[str, Any],
                       pattern_rect: Tuple[int, int, int, int],
                       *, is_cancelled=None,
                       ) -> Tuple[Dict[str, Dict[str, Any]], str]:
    """对整个增量计划逐段重跟（N13 引擎的批量入口）。

    任一段失败即停（后续段依赖前一段的锚定语义）；已成功段的结果
    保留返回，error 说明中止段。
    """
    plan = plan_incremental_retrack(track_data)
    merged: Dict[str, Dict[str, Any]] = {}
    for seg in plan.segments:
        part, error = retrack_translation_segment(
            frame_getter, seg, pattern_rect,
            is_cancelled=is_cancelled)
        merged.update(part)
        if error:
            return merged, f"segment {seg.start_frame}-{seg.end_frame}: {error}"
    return merged, ""


__all__ = [
    "RetrackSegment",
    "IncrementalRetrackPlan",
    "TRACK_STATUS_MANUAL",
    "TRACK_STATUS_VALID",
    "get_manual_corrections",
    "set_manual_correction",
    "plan_incremental_retrack",
    "apply_track_update",
    "retrack_translation_segment",
    "retrack_plan_async",
]
