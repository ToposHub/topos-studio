"""M13.5/M13.6 · 多机位自适应降档与多屏 DPI 恢复（模型层）。

- ``multicam_quality_policy``：按角度数/负载/代理可用性选择每角度
  预览分辨率档位（1=全分辨率 … 4=1/8）与帧率分频——九路不达标时
  **显式声明降档**（不承诺满帧）；四路全分辨率为已验收基线。
- ``dpi_restore_geometry``：拔屏/移屏后窗口几何恢复（屏不存在时
  的回退屏选择与钳制，避免窗口落在失效屏外）。
"""

from __future__ import annotations

from typing import Dict, Optional, Sequence, Tuple


def multicam_quality_policy(
    n_angles: int,
    *,
    program_load: str = "light",       # light / heavy（Program 轨负载）
    proxies_available: bool = True,
    measured_p95_budget_ms: Optional[float] = None,
    frame_budget_ms: float = 40.0,
) -> Dict[str, object]:
    """多机位网格的自适应质量策略（M13.5，纯决策函数）。

    决策表（按已验收的四路全分辨率基线外推；实测 p95 覆盖静态表）：
    - ≤4 角度：全分辨率（1.0），不分频；
    - 5–6 角度：heavy 或超预算 → 1/2，否则全分辨率；
    - 7–9 角度：1/2（轻负载无代理时 1/4）；
    - >9 角度：1/4 + 分频 2（显式声明：不承诺所有机器满帧）。

    Returns:
        {"scale": 1.0|0.5|0.25, "frame_divider": 1|2,
         "degraded": bool, "reason": str}
        ``degraded=True`` 时 UI 必须明示降档状态（不静默）。
    """
    n = max(1, int(n_angles))
    heavy = str(program_load).lower() == "heavy"
    over_budget = (measured_p95_budget_ms is not None
                   and measured_p95_budget_ms > frame_budget_ms)

    def _policy(scale: float, divider: int, reason: str):
        return {"scale": scale, "frame_divider": divider,
                "degraded": scale < 1.0 or divider > 1, "reason": reason}

    if measured_p95_budget_ms is not None and not over_budget:
        # 实测在预算内：不降档（哪怕角度多）
        return _policy(1.0, 1, f"实测 p95 {measured_p95_budget_ms:.1f}ms 在预算内")
    if n <= 4:
        if over_budget:
            return _policy(0.5, 1,
                           f"实测 p95 {measured_p95_budget_ms:.1f}ms 超预算 "
                           f"{frame_budget_ms:.0f}ms——降 1/2")
        return _policy(1.0, 1, "≤4 角度全分辨率（已验收基线）")
    if n <= 6:
        if heavy or over_budget:
            return _policy(0.5, 1, f"{n} 角度 + "
                           f"{'Program 重负载' if heavy else '超预算'}——降 1/2")
        return _policy(1.0, 1, f"{n} 角度轻负载——全分辨率")
    if n <= 9:
        if heavy or over_budget:
            return _policy(0.25, 1, f"{n} 角度 + "
                           f"{'重负载' if heavy else '超预算'}——降 1/4")
        return _policy(0.5, 1, f"{n} 角度——1/2")
    return _policy(0.25, 2,
                   f"{n} 角度超四路扩展——1/4 + 分频 2（显式降档，"
                   "不承诺所有机器满帧）")


def dpi_restore_geometry(
    window_geometry: Tuple[int, int, int, int],
    screen_geometries: Sequence[Tuple[int, int, int, int]],
    *,
    min_visible_ratio: float = 0.25,
) -> Tuple[int, int, int, int]:
    """拔屏/移屏后的窗口几何恢复（M13.6）。

    屏消失（窗口与现存屏的可视交叠 < ``min_visible_ratio``）时回退
    到首个屏的工作区内（保持窗口尺寸，钳制进屏）。窗口仍在某屏可视
    范围内时原样返回。
    """
    if not screen_geometries:
        return window_geometry
    wx, wy, ww, wh = (int(v) for v in window_geometry)
    for sx, sy, sw, sh in screen_geometries:
        ix = max(0, min(wx + ww, sx + sw) - max(wx, sx))
        iy = max(0, min(wy + wh, sy + sh) - max(wy, sy))
        visible = (ix * iy) / float(max(1, ww * wh))
        if visible >= min_visible_ratio:
            return window_geometry
    # 回退：首个屏，窗口尺寸保持，右/下越界钳制
    sx, sy, sw, sh = (int(v) for v in screen_geometries[0])
    x = min(max(sx, wx), sx + max(0, sw - ww))
    y = min(max(sy, wy), sy + max(0, sh - wh))
    return (x, y, ww, wh)


__all__ = ["multicam_quality_policy", "dpi_restore_geometry"]
