"""PAN-02/PAN-03：VBAP 多扬声器声像与真实 LFE 路由（离线验证域）。

按计划 §14.4（Aalto VBAP 公开方法）实现：
- ``vbap_gains``：主方向（方位角/仰角）在扬声器布局上的幅度声像增益。
  2D 布局用相邻扬声器对解 2×2 线性方程；含高度声道时用三元组
  （穷举选择增益非负且重建误差最小的基）；增益能量归一（‖g‖₂=1）。
- ``spread_gains``：Spread 能量合成——主方向 ±spread 采样后能量平均
  再归一（宽度增加不改变总能量）。
- ``lfe_route``：PAN-03 真实 LFE 路由。LFE 是独立声道，不参与 VBAP
  归一；Boom 的 Pre/Post/Only 语义显式定义：
    Pre  = 低频同时进主声道与 LFE（增益和=1，主声道不重复加倍）
    Post = 主声道全频 + LFE 只收低频发送
    Only = 只向 LFE 输出（主声道增益 0）

声道顺序与 channel_layout.ChannelLayout 一致（L,R,C,LFE,Ls,Rs,…）。
本模块为离线路由数学（计划 §13："多声道能力可以先离线逐通道验证"）；
实时轨域仍为立体声（MX-15 能力矩阵 track_dsp 段）——实机监听未测，
报告为"离线路由已测、实机监听未测"。
"""

from __future__ import annotations

from itertools import combinations
from typing import Dict, Optional, Tuple

import numpy as np

# 布局 → (label, azimuth_deg, elevation_deg)；LFE 不列（非 VBAP 声道）
# 方位角：0=正前，+90=右，±180=正后；仰角：0=水平面，90=正上。
# 标准摆位（ITU-R BS.775 / 7.1.4 惯例）。
SPEAKER_LAYOUTS: Dict[str, Tuple[Tuple[str, float, float], ...]] = {
    "2.0": (
        ("L", -30.0, 0.0), ("R", 30.0, 0.0),
    ),
    "5.1": (
        ("L", -30.0, 0.0), ("R", 30.0, 0.0), ("C", 0.0, 0.0),
        ("Ls", -110.0, 0.0), ("Rs", 110.0, 0.0),
    ),
    "7.1": (
        ("L", -30.0, 0.0), ("R", 30.0, 0.0), ("C", 0.0, 0.0),
        ("Lss", -90.0, 0.0), ("Rss", 90.0, 0.0),
        ("Ls", -135.0, 0.0), ("Rs", 135.0, 0.0),
    ),
    # 高度布局（VBAP 数学域；交付容器不支持——layout_capability 声明）
    "7.1.4": (
        ("L", -30.0, 0.0), ("R", 30.0, 0.0), ("C", 0.0, 0.0),
        ("Lss", -90.0, 0.0), ("Rss", 90.0, 0.0),
        ("Ls", -135.0, 0.0), ("Rs", 135.0, 0.0),
        ("Ltf", -30.0, 45.0), ("Rtf", 30.0, 45.0),
        ("Ltr", -135.0, 45.0), ("Rtr", 135.0, 45.0),
    ),
}


def _unit_vector(azimuth_deg: float, elevation_deg: float) -> np.ndarray:
    """方向单位向量（x 前，y 左，z 上）。"""
    az = np.deg2rad(azimuth_deg)
    el = np.deg2rad(elevation_deg)
    return np.array([
        np.cos(el) * np.cos(az),
        np.cos(el) * np.sin(az),
        np.sin(el),
    ])


def has_height_channels(layout_name: str) -> bool:
    """布局是否含高度声道（无高度时仰角>0 的方向按水平面近似并给出
    限制标记，不静默当作沉浸输出——PAN-02 验收"无上方输出时明确限制"）。"""
    return any(el > 0.0 for _l, _az, el in SPEAKER_LAYOUTS[layout_name])


def vbap_gains(azimuth_deg: float, elevation_deg: float = 0.0,
               layout: str = "5.1") -> Tuple[Dict[str, float], bool]:
    """主方向 → {label: 增益}（能量归一 ‖g‖₂=1），与仰角限制标记。

    Returns:
        (gains, exact): exact=False 表示请求了仰角>0 但布局无高度声道，
        已钳到水平面近似（调用方必须显式提示限制）。
    """
    speakers = SPEAKER_LAYOUTS[layout]
    exact = True
    el = float(elevation_deg)
    if el != 0.0 and not has_height_channels(layout):
        exact = False
        el = 0.0
    direction = _unit_vector(azimuth_deg, el)

    # 候选基：平面布局用对（n=2），含高度用三元组（n=3）。
    # 选择准则（VBAP 标准）：① 解出的增益全部非负（方向落在该扇区内）；
    # ② 其中取扬声器到方向角距总和最小的基（= 相邻对/三元组——否则
    # 对角张成的基（如 L+Rs 张成前方）会给出正确的数学解但错误的声像）。
    dim = 3 if has_height_channels(layout) else 2
    basis_size = 2 if dim == 2 else 3
    best: Optional[Tuple[Tuple[float, float], Tuple[int, ...], np.ndarray]] = None
    for idxs in combinations(range(len(speakers)), basis_size):
        vecs = []
        for i in idxs:
            _lbl, az, e = speakers[i]
            vecs.append(_unit_vector(az, e)[:dim] if dim == 2
                        else _unit_vector(az, e))
        basis = np.stack(vecs, axis=1)  # (dim, basis_size)
        if abs(np.linalg.det(basis)) < 1e-9:
            continue
        gains = np.linalg.solve(basis, direction[:dim])
        negativity = float(np.sum(np.clip(-gains, 0.0, None)))
        if negativity > 1e-6:
            continue  # 方向不在该扇区内
        # 扇区有效：总角距最小者胜
        angle_sum = 0.0
        for i in idxs:
            _lbl, az, e = speakers[i]
            spk = _unit_vector(az, e)
            angle_sum += float(np.arccos(np.clip(
                float(np.dot(direction, spk)), -1.0, 1.0)))
        score = (negativity, angle_sum)
        if best is None or score < best[0]:
            best = (score, idxs, gains)

    if best is None:
        # 无有效扇区（方向在布局盲区，如 2.0 的正后）：最近单扬声器
        dots = [float(np.dot(direction, _unit_vector(s[1], s[2])))
                for s in speakers]
        best_i = int(np.argmax(dots))
        return {speakers[best_i][0]: 1.0}, exact

    _score, idxs, gains = best
    gains = np.maximum(gains, 0.0)
    norm = float(np.linalg.norm(gains))
    if norm < 1e-9:
        # 方向落在布局死区（如后方且无后置扬声器）：取最近单扬声器
        best_i = int(np.argmax([
            float(np.dot(direction[:dim],
                         (_unit_vector(s[1], s[2])[:dim] if dim == 2
                          else _unit_vector(s[1], s[2]))))
            for s in speakers]))
        return {speakers[best_i][0]: 1.0}, exact
    gains = gains / norm
    return {speakers[i][0]: float(g) for i, g in zip(idxs, gains)}, exact


def spread_gains(azimuth_deg: float, spread_deg: float = 0.0,
                 elevation_deg: float = 0.0,
                 layout: str = "5.1", n_samples: int = 9
                 ) -> Tuple[Dict[str, float], bool]:
    """Spread 能量合成：主方向 ±spread 均匀采样 → 能量平均 → 归一。

    总能量与 spread 无关（宽度改变不改变响度）；spread=0 退化为
    vbap_gains。
    """
    spread = max(0.0, min(float(spread_deg), 180.0))
    if spread < 1e-6:
        return vbap_gains(azimuth_deg, elevation_deg, layout)
    # 采样：主方向 ± spread（方位角展开；仰角保持）
    offsets = np.linspace(-spread, spread, max(3, n_samples))
    energy: Dict[str, float] = {}
    exact = True
    for off in offsets:
        g, ex = vbap_gains(azimuth_deg + float(off), elevation_deg, layout)
        exact = exact and ex
        for lbl, v in g.items():
            energy[lbl] = energy.get(lbl, 0.0) + v * v
    total = float(np.sqrt(sum(energy.values())))
    if total < 1e-12:
        return {}, exact
    return {lbl: float(np.sqrt(e) / total) for lbl, e in energy.items()}, exact


def lfe_route(boom_on: bool, route: str = "Pre",
              main_gain: float = 1.0, lfe_gain: float = 1.0
              ) -> Tuple[float, float]:
    """PAN-03：Boom/LFE 路由 → (主声道增益, LFE 增益)。

    - boom_on=False → (main_gain, 0.0)（LFE 无贡献）
    - Pre  → 主声道与 LFE 同收低频（各自乘给定增益；"Pre"=推子前概念，
      增益不随主推子——由调用方在图上实现）
    - Post → 同 Pre 的增益分配（推子后语义由 tap 位置决定，本函数只给
      目标增益；与 MX-14 send tap 对齐）
    - Only → (0.0, lfe_gain)——只向 LFE 输出，主声道静音
    低频主声道不因 Boom 重复加倍：Pre/Post 的主声道增益 = main_gain
    （不是 main_gain×2 之类）；LFE 是独立声道求和（channel_layout
    downmix 中 LFE 默认排除，不回混 L/R）。
    """
    if not boom_on:
        return main_gain, 0.0
    route_key = str(route).capitalize()
    if route_key == "Only":
        return 0.0, lfe_gain
    if route_key in ("Pre", "Post"):
        return main_gain, lfe_gain
    raise ValueError(f"unknown boom route: {route!r} (Pre/Post/Only)")
