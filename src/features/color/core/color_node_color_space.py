"""N05：Node Color Space / Gamma 的后端消费（规格 v1）。

语义（计划 N05：定义输入/输出/工作空间变换位置）：

- 节点级「工作空间切换」：进入 Corrector 前把输入从**节点标记空间**
  （IDT）变换到线性工作域，grading 链（含 LUT——LUT 顺序=在变换内
  侧，与 R05 处理顺序一致）执行后再逆变换（ODT）回上游空间——下游
  节点看到的仍是同一工作流空间（非破坏性，不与 primary gamma 元组
  冲突：那是 grade 域参数，本域在 params["color"]，独立字段）。
- ``Use Timeline``（默认）= 恒等变换，**关选项 = 原结果**（位级）。
- 版本化：``NODE_COLOR_SCHEMA_VERSION``；枚举值与菜单字符串一致，
  持久化即 params["color"]["color_space"/"gamma"]（服务端
  ``set_node_option`` 已写入）。

实现分三端**同源**：

- 本模块 = numpy 参考实现 + GLSL uniform 提取（矩阵/传递函数种类）；
- GL 执行器：包裹 pass（utility shader，uniform = 3×3 矩阵 + kind）；
- CPU oracle：直接调用本模块 numpy；
- Metal：v1 显式 unsupported（``production_layer_domain_errors`` 拒
  绝非 timeline 的 color 域——按能力声明，不静默近似）。

 primaries 矩阵（线性 RGB↔RGB，经 XYZ 中转，D65 白点除 P3-D65 自带
 与 AP0(APD65) 外均按各自标准白点归一）与传递函数公式为业界标准常
 数（BT.709/BT.2020/SMPTE RP 431-2/SMPTE ST 2085/ACES 1.0/ST 2084/
 BT.2100 HLG/DaVinci Intermediate）。
"""
from __future__ import annotations

from typing import Any, Mapping

import numpy as np

NODE_COLOR_SCHEMA_VERSION = 1

#: 菜单枚举（与 node_editor_view 的值集合一致）
COLOR_SPACES = ("Use Timeline", "Rec.709", "Rec.2020", "P3-D65",
                "ACES AP0", "ACES AP1", "DaVinci Wide Gamut")
GAMMAS = ("Use Timeline", "Linear", "Gamma 2.2", "Gamma 2.4", "sRGB",
          "ST2084", "HLG", "DaVinci Intermediate")

# 传递函数种类（GLSL switch 与 numpy 同 kind 分派）
TF_IDENTITY = 0
TF_G22 = 1
TF_G24 = 2
TF_SRGB = 3
TF_ST2084 = 4
TF_HLG = 5
TF_DAVINCI_INT = 6

_GAMMA_KIND = {
    "Use Timeline": TF_IDENTITY, "Linear": TF_IDENTITY,
    "Gamma 2.2": TF_G22, "Gamma 2.4": TF_G24, "sRGB": TF_SRGB,
    "ST2084": TF_ST2084, "HLG": TF_HLG,
    "DaVinci Intermediate": TF_DAVINCI_INT,
}

# ── primaries：RGB→XYZ 矩阵（行向量 rgb·M = XYZ）───────────────────────
_RGB_TO_XYZ = {
    "Rec.709": np.array([
        [0.412391, 0.357584, 0.180481],
        [0.212639, 0.715169, 0.072192],
        [0.019331, 0.119195, 0.950690]]),
    "Rec.2020": np.array([
        [0.636958, 0.144617, 0.168881],
        [0.262700, 0.677998, 0.059302],
        [0.000000, 0.028073, 1.060985]]),
    "P3-D65": np.array([
        [0.486571, 0.265668, 0.198217],
        [0.228975, 0.691739, 0.079287],
        [0.000000, 0.045113, 1.043944]]),
    "ACES AP0": np.array([
        [0.952552, 0.000000, 0.000094],
        [0.343966, 0.728166, -0.072132],
        [0.000000, 0.000000, 1.008825]]),
    "ACES AP1": np.array([
        [0.698466, 0.084598, 0.111370],
        [0.305440, 0.663769, 0.030791],
        [0.000000, 0.025182, 1.052900]]),
    "DaVinci Wide Gamut": np.array([
        [0.716104, 0.100930, 0.147183],
        [0.258044, 0.724913, 0.017043],
        [-0.000000, 0.000000, 1.000000]]),
}


def _rgb_to_rgb(src: str, dst: str) -> np.ndarray:
    """线性 RGB(src) → 线性 RGB(dst) 的 3×3 矩阵（同空间=恒等）。"""
    if src == dst:
        return np.eye(3)
    return np.linalg.inv(_RGB_TO_XYZ[dst]) @ _RGB_TO_XYZ[src]


def node_color_is_identity(color_space: Any, gamma: Any) -> bool:
    return (str(color_space or "Use Timeline") == "Use Timeline"
            and str(gamma or "Use Timeline") == "Use Timeline")


def node_color_transform_spec(color_space: Any, gamma: Any) -> dict | None:
    """节点 color 域 → 变换规格（None/identity = 原结果）。

    规格含：正/逆 3×3 矩阵（numpy + 行主序 list，供 GLSL uniform）与
    传递函数 kind（编码=linear→encoding，解码=其逆）。
    """
    cs = str(color_space or "Use Timeline")
    g = str(gamma or "Use Timeline")
    if node_color_is_identity(cs, g):
        return None
    if cs not in _RGB_TO_XYZ or g not in _GAMMA_KIND:
        return None  # 未知值 fail-closed 为恒等（不崩渲染）
    return {
        "schema_version": NODE_COLOR_SCHEMA_VERSION,
        "color_space": cs,
        "gamma": g,
        "tf_kind": _GAMMA_KIND[g],
        # 输入（编码域）→ 工作线性：decode = 逆传递 + gamut→工作
        "m_to_linear": _rgb_to_rgb(cs, "Rec.709"),
        "m_from_linear": _rgb_to_rgb("Rec.709", cs),
    }


# ── numpy 参考实现（CPU oracle 与 GL 同公式）───────────────────────────

def _tf_encode(x: np.ndarray, kind: int) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    if kind == TF_IDENTITY:
        return x
    if kind == TF_G22:
        return np.power(np.clip(x, 0.0, None), 1.0 / 2.2)
    if kind == TF_G24:
        return np.power(np.clip(x, 0.0, None), 1.0 / 2.4)
    if kind == TF_SRGB:
        lo = x < 0.0031308
        out = np.where(lo, 12.92 * x,
                       1.055 * np.power(np.clip(x, 0.0, None),
                                        1.0 / 2.4) - 0.055)
        return out
    if kind == TF_ST2084:
        # BT.2100 PQ 逆 EOTF：E′ = ((c1 + c2·E^m1)/(1 + c3·E^m1))^m2，
        # E = L/10000；×128 整数形 (107 + 2413c)/(128 + 2392c)
        m1, m2 = 2610.0 / 16384.0, 2523.0 / 4096.0 * 128.0
        c = np.power(np.clip(x, 0.0, 1.0), m1)
        return np.power((107.0 + 2413.0 * c)
                        / (128.0 + 2392.0 * c), m2)
    if kind == TF_HLG:
        # BT.2100 HLG OETF（场景线性 E∈[0,1]；E=1/12 ↔ 信号 0.5）
        a, b, c = 0.17883277, 0.28466892, 0.55991073
        lo_mask = x <= (1.0 / 12.0)
        safe_hi = np.where(lo_mask, 1.0 / 12.0, x)
        return np.where(lo_mask, np.sqrt(3.0 * np.maximum(x, 0.0)),
                        a * np.log(12.0 * safe_hi - b) + c)
    if kind == TF_DAVINCI_INT:
        # DaVinci Intermediate（连续化构造）：L≤0.00262409 为 10× 线性
        # 段；其上 log2 段在锚点与线性段**斜率/值双连续**
        # （a = 10·kink·ln2；Blackmagic 未公开完整常数集——不臆造）
        kink = 0.00262409
        slope = 10.0 * kink * np.log(2.0)
        lo = x <= kink
        safe_hi = np.where(lo, kink, x)
        return np.where(lo, x * 10.0,
                        np.log2(safe_hi / kink) * slope + kink * 10.0)
    raise ValueError(f"未知传递函数 kind={kind}")


def _tf_decode(x: np.ndarray, kind: int) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    if kind == TF_IDENTITY:
        return x
    if kind == TF_G22:
        return np.power(np.clip(x, 0.0, None), 2.2)
    if kind == TF_G24:
        return np.power(np.clip(x, 0.0, None), 2.4)
    if kind == TF_SRGB:
        lo = x < 0.04045
        return np.where(lo, x / 12.92,
                        np.power(np.clip(x + 0.055, 0.0, None) / 1.055,
                                 2.4))
    if kind == TF_ST2084:
        # BT.2100 PQ EOTF：E = ((128·E′^(1/m2) − 107)/(2413 − 2392·E′^(1/m2)))^(1/m1)
        m1, m2 = 2610.0 / 16384.0, 2523.0 / 4096.0 * 128.0
        p = np.power(np.clip(x, 0.0, 1.0), 1.0 / m2)
        return np.power(
            np.maximum(128.0 * p - 107.0, 0.0)
            / np.maximum(2413.0 - 2392.0 * p, 1e-12), 1.0 / m1)
    if kind == TF_HLG:
        # BT.2100 HLG EOTF 逆（信号 α∈[0,1] → 场景线性）
        a, b, c = 0.17883277, 0.28466892, 0.55991073
        s = np.clip(x, 0.0, 1.0)
        return np.where(s <= 0.5, s * s / 3.0,
                        (np.exp((s - c) / a) + b) / 12.0)
    if kind == TF_DAVINCI_INT:
        kink = 0.00262409
        slope = 10.0 * kink * np.log(2.0)
        lo = x <= kink * 10.0
        return np.where(lo, x / 10.0,
                        kink * np.power(
                            2.0, (x - kink * 10.0) / slope))
    raise ValueError(f"未知传递函数 kind={kind}")
    raise ValueError(f"未知传递函数 kind={kind}")


def apply_node_color_space_cpu(rgb: np.ndarray, spec: Mapping[str, Any],
                               *, inverse: bool = False) -> np.ndarray:
    """CPU 参考：编码域⇄工作线性（逆=inverse）。

    正向（进节点）：decode TF → gamut 矩阵到 Rec.709 线性。
    （PQ 域为归一线性 [0,1] = L/10000；HLG 为 BT.2100 场景线性。）
    逆向（出节点）：gamut 矩阵回节点空间 → encode TF。
    """
    if not spec:
        return rgb
    kind = int(spec["tf_kind"])
    if not inverse:
        out = _tf_decode(np.asarray(rgb, dtype=np.float64), kind)
        return out @ np.asarray(spec["m_to_linear"], dtype=np.float64).T
    out = np.asarray(rgb, dtype=np.float64) @ np.asarray(
        spec["m_from_linear"], dtype=np.float64).T
    return _tf_encode(out, kind)


def color_space_gl_uniforms(spec: Mapping[str, Any] | None) -> dict | None:
    """GLSL uniform 提取（与 numpy 同源；None = 恒等，不注入 pass）。"""
    if not spec:
        return None
    return {
        "tf_kind": int(spec["tf_kind"]),
        "m_to_linear": [float(v) for v in np.asarray(
            spec["m_to_linear"]).reshape(-1)],
        "m_from_linear": [float(v) for v in np.asarray(
            spec["m_from_linear"]).reshape(-1)],
    }
