"""M13.3 · 关键帧前后导航与曲线插值入口（模型层）。

关键帧导航（↑↓ 在轨道上跳到前/后关键帧帧号——属性面板/曲线编辑器
共用）与曲线插值样条求值（Bezier/缓动插值的纯函数，供曲线 UI 编辑
入口）。与 GradeKeyframeTrack 既有线性/阶跃插值并行——曲线编辑器
的 Bezier 控制柄插值按显式声明的子集实现。
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def next_keyframe_frame(
    track, frame: int, *, direction: str = "next",
) -> Optional[int]:
    """轨道上前/后关键帧帧号（严格序；无返回 None）。

    M13.3：属性面板/曲线编辑器的 ↑↓ 导航共用。
    """
    frames = sorted(track.frames())
    if not frames:
        return None
    if direction == "next":
        for f in frames:
            if f > frame:
                return f
        return None
    for f in reversed(frames):
        if f < frame:
            return f
    return None


def bezier_value(
    p0: float, p1: float, p2: float, p3: float, t: float,
) -> float:
    """三次 Bezier 曲线值（控制柄 p1/p2，端点 p0/p3）。

    曲线编辑器的关键帧间插值：t∈[0,1]。
    """
    t = min(1.0, max(0.0, float(t)))
    u = 1.0 - t
    return (u * u * u * p0 + 3.0 * u * u * t * p1
            + 3.0 * u * t * t * p2 + t * t * t * p3)


def bezier_t_for_x(
    x0: float, x1: float, x2: float, x3: float, x: float,
    *, iterations: int = 24,
) -> float:
    """给定 x（时间轴），求三次 Bezier 的参数 t（x 单调时的二分反解）。

    曲线编辑器按时间轴取值：控制柄 x 分量须单调（编辑器约束），二分
    24 次精度 < 1e-7 相对量级。
    """
    if x <= min(x0, x3):
        return 0.0 if x <= x0 else 1.0
    if x >= max(x0, x3):
        return 1.0 if x >= x3 else 0.0
    lo, hi = 0.0, 1.0
    for _ in range(iterations):
        mid = (lo + hi) * 0.5
        u = 1.0 - mid
        xm = (u * u * u * x0 + 3.0 * u * u * mid * x1
              + 3.0 * u * mid * mid * x2 + mid * mid * mid * x3)
        if xm < x:
            lo = mid
        else:
            hi = mid
    return (lo + hi) * 0.5


def evaluate_bezier_segment(
    seg: Dict[str, Any], frame: float,
) -> Optional[float]:
    """按曲线段声明求值。

    ``seg`` = {"x0": f, "y0": f, "x1": f, "y1": f,
               "x2": f, "y2": f, "x3": f, "y3": f}
    （端点 (x0,y0)→(x3,y3)，控制柄 (x1,y1)/(x2,y2)，x=帧域）。
    帧在段外返回 None（调用方取最近端点/下一轨道值）。
    """
    try:
        x0, x3 = float(seg["x0"]), float(seg["x3"])
    except (KeyError, TypeError, ValueError):
        return None
    if frame < x0 or frame > x3:
        return None
    t = bezier_t_for_x(x0, float(seg["x1"]), float(seg["x2"]), x3, frame)
    return bezier_value(float(seg["y0"]), float(seg["y1"]),
                        float(seg["y2"]), float(seg["y3"]), t)


__all__ = [
    "next_keyframe_frame",
    "bezier_value",
    "bezier_t_for_x",
    "evaluate_bezier_segment",
]
