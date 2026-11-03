"""R05: Corrector LUT 处理顺序与强度混合的版本化规格（v1）。

**顺序规格**（``CORRECTOR_PROCESS_ORDER_V1``）冻结生产 composition shader
（``shader_compiler.build_pipeline_shader`` 的代码顺序）与 CPU oracle 的
事实顺序。顺序变化（插入/移动域）必须 bump 版本并过非交换算子对照——
identity LUT 通过不能代替顺序正确。

**强度混合规格**（``blend_node_lut_strength``）是 ``shader_nodes.
LUT3D_SNIPPET`` 的 CPU 权威移植：

1. 采样域：``sdr = clamp(color, 0, 1)``（仅 LUT 采样域收窄，非输出 clamp）；
2. base 分支保持**完整输入**（含 HDR 与负值）——strength=0 严格恒等；
3. LUT 分支：``lut_hdr = lut(sdr)``；max(color)>1 且 max(sdr)>1e-5 时按
   ``max(color)/max(sdr)`` 比例重建 HDR（保留高光色彩比例，不全局 clamp）；
4. 混合：``out = mix(color, lut_hdr, strength) + neg*strength``
   （scene-linear 工作空间线性混合；负偏移按强度加权加法保留，暗部
   不发生色彩反转）；
5. strength=1 为完整 LUT（HDR 比例重建 + 负偏移全量加法）。

**DOMAIN 策略**：非单位 DOMAIN 的表在加载期经
``lut_loader.resample_to_unit_domain`` 重采样到 [0,1]（GPU 采样路径口径）；
DOMAIN 外输入按上式 1/3 的 clamp 只作用于 LUT 采样域，不全局 clamp 输出。

**消费投影**：图 Corrector 的 ``params.resources.lut_ref/lut_strength`` 经
``color_graph_param_source.node_layer_payload`` 投影为 legacy Layer 的
``active_lut_path/lut_intensity``（GL composition 与持久化同源）。CPU 图
路径对 LUT3D 维持显式 ``UnsupportedNode``（N14 逐域实现时以本规格为准）。
"""
from __future__ import annotations

from typing import Final, Mapping, Sequence

import numpy as np

#: Corrector 参数域处理顺序（版本化；变更必须 bump 并补非交换对照）。
CORRECTOR_PROCESS_ORDER_V1: Final[tuple[str, ...]] = (
    "fx_blur_sharpen",      # blur/sharpen 前置（后续 grading 作用于清晰信号）
    "primary",              # lift/gamma/gain/exposure/…
    "curves",               # RGB 曲线
    "hsl_curves",           # HSL 曲线
    "soft_clip",            # soft clip（肩/趾）
    "curves_soft_clip",     # 曲线面板四参数柔化裁切
    "mixer",                # 色轮 mixer
    "warper",               # warper 网格
    "lut3d",                # 节点 3D LUT（本规格主体）
    "plugins",              # .tglsl/.tai 插件实例（按实例顺序）
    "mask_blend",           # matte/blend_mode/opacity epilogue
)

CORRECTOR_PROCESS_ORDER_VERSION: Final[int] = 1

#: 强度混合规格版本（公式见模块 docstring；GPU/CPU parity 锁定）。
LUT_STRENGTH_BLEND_VERSION: Final[int] = 1

_F32 = np.float32


def blend_node_lut_strength(color: np.ndarray,
                            lut_result: np.ndarray,
                            strength: float) -> np.ndarray:
    """节点 LUT 强度混合的 CPU 权威实现（与 LUT3D_SNIPPET 逐式对应）。

    Args:
        color: 输入像素 (..., 3)（scene-linear，可含 >1 HDR 与负值）。
        lut_result: LUT 对 ``clamp(color, 0, 1)`` 的采样结果（调用方按
            网格点对齐采样；本函数只负责混合语义）。
        strength: [0, 1] 混合强度。

    Returns:
        (..., 3) float32：``mix(color, lut_hdr, s) + neg*s``——base 保持
        完整输入（s=0 严格恒等，含 HDR/负值）；LUT 分支按最大值比例重建
        HDR；负偏移按强度加权加法保留。
    """
    src = np.asarray(color, dtype=np.float64)
    sampled = np.asarray(lut_result, dtype=np.float64)
    s = float(strength)
    sdr = np.clip(src, 0.0, 1.0)
    lut_hdr = sampled
    max_orig = src.max(axis=-1, keepdims=True)
    max_sdr = sdr.max(axis=-1, keepdims=True)
    scale = np.where((max_orig > 1.0) & (max_sdr > 1e-5),
                     max_orig / np.where(max_sdr > 1e-5, max_sdr, 1.0), 1.0)
    lut_hdr = lut_hdr * scale
    negative_excess = src - np.clip(src, 0.0, 99999.0)
    out = src * (1.0 - s) + lut_hdr * s + negative_excess * s
    return out.astype(_F32)


def lut_strength_from_resources(resources: Mapping[str, Any] | None) -> float:
    """节点 resources 域 → 强度（缺省 1.0；非法值按权威旧值回退）。"""
    if not isinstance(resources, Mapping):
        return 1.0
    value = resources.get("lut_strength", 1.0)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 1.0
    if value != value or value in (float("inf"), float("-inf")):  # NaN/Inf
        return 1.0
    return min(1.0, max(0.0, value))


def node_lut_projection(resources: Mapping[str, Any] | None,
                        ) -> tuple[str | None, float]:
    """图 resources 域 → (legacy active_lut_path, lut_intensity) 投影。"""
    if not isinstance(resources, Mapping):
        return None, 1.0
    lut_ref = resources.get("lut_ref")
    lut_ref = lut_ref if isinstance(lut_ref, str) and lut_ref else None
    return lut_ref, lut_strength_from_resources(resources)


def assert_process_order(ordered_domains: Sequence[str]) -> None:
    """校验域序列符合 v1 规格（供测试/编译器自检；失配抛 AssertionError）。"""
    expected = tuple(d for d in CORRECTOR_PROCESS_ORDER_V1)
    got = tuple(ordered_domains)
    assert got == expected, f"处理顺序与规格 v1 不符: {got} != {expected}"
