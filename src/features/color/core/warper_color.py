# -*- coding: utf-8 -*-
"""CW-B03:Warper 色彩空间/坐标约定的 CPU 参考实现(冻结规范)。

计划:`docs/plans/2026-11-12-color-warper-backend-plan.md` §4 CW-B03。

冻结的坐标与色彩约定(B00 行为矩阵 + 本模块测试共同锁定)
------------------------------------------------------
1. **HSV(legacy)**:逐句镜像 `shader_nodes.COMMON_DEFINITIONS` 的
   GLSL ``rgb2hsv``/``hsv2rgb``(K 向量算法,含 1e-10 分母防护)。
   hue 原点 = 红(0.0),向黄绿方向增大;方向与环绕以 GLSL 实现为准。
2. **HSL**:标准精确公式,L=(max+min)/2,零色度 S=0。
3. **HSP(Topos 声明)**:感知亮度 ``P = sqrt(0.299R²+0.587G²+0.114B²)``
   (常数在测试中冻结);H/S 与 HSV 同式(S=(max-min)/max)。反演利用
   平方和的齐次性:RGB₀=hsv2rgb(H,S,1),k=P/P(RGB₀),RGB=k·RGB₀ —— 数学
   精确(浮点误差内),零色度时 RGB=(P,P,P)。P₀=sqrt(0.299+0.587+0.114)=1。
4. **零色度政策**:max==min 时 H=0、S=0(定义值,非噪声);legacy 路径
   另有 sat<0.02 直接 bypass + smoothstep(0.02,0.15) 衰减,双保险。
5. **HDR 政策**:全部转换对任意有限 float 定义;负通道以逐通道 sign/abs
   处理(与 legacy shader 的 ``sign(color)``/``abs(color)`` 技巧一致);
   除 legacy 语义显式声明处(hue ``fract``、sat clamp [0,1])外不做
   0–1 截断;RGB>1、极小/极大有限值均连续可解释,无 NaN/Inf。
6. **网格纹理坐标**:U=hue∈[0,1) 环绕(经 fract),V=sat∈[0,1];
   legacy 纹理 LINEAR+CLAMP_TO_EDGE —— 色相接缝两侧各自取边缘格,
   不跨缝混合(已知 legacy 特性,记录不修改)。行=环(sat 半径),
   列=射线(hue);中心=零色度,外圈=满饱和。显示几何(六角/方形场)
   只是投影,算法坐标一律走本模块。
7. **legacy 行为不迁移不改值**:`apply_warper_legacy_hsv` 是
   ``WARPER_SNIPPET`` 的逐句 CPU 镜像,GPU/GLSL/MSL parity 由
   ``tests/color/test_warper_color_parity.py`` 用真实 fixture 比对。

线程边界:纯函数、numpy 向量化;无状态无锁,任意线程只读。
"""
from __future__ import annotations

import numpy as np

__all__ = [
    "HSP_WEIGHTS",
    "rgb_to_hsv",
    "hsv_to_rgb",
    "rgb_to_hsl",
    "hsl_to_rgb",
    "rgb_to_hsp",
    "hsp_to_rgb",
    "smoothstep",
    "sample_warper_bilinear",
    "apply_warper_legacy_hsv",
    "apply_warper_in_space",
    "WARPER_SPACE_INDEX",
]

#: CW-B08:shader ``u_warper_space`` 的取值(0=HSV/1=HSL/2=HSP)。
WARPER_SPACE_INDEX = {"hsv": 0, "hsl": 1, "hsp": 2}

#: HSP 感知权重(经典 NTSC 系数;冻结于测试,改动即破坏 round-trip 语义)。
HSP_WEIGHTS = (0.299, 0.587, 0.114)


def _as_float_array(color) -> np.ndarray:
    array = np.asarray(color, dtype=np.float64)
    if array.shape != (3,):
        raise ValueError(f"颜色需要 3 分量,得到 {array.shape}")
    if not np.isfinite(array).all():
        raise ValueError("颜色必须为有限值")
    return array


def smoothstep(edge0: float, edge1: float, x) -> np.ndarray | float:
    """GLSL smoothstep 镜像:t=clamp((x-e0)/(e1-e0),0,1);t²(3-2t)。"""
    t = np.clip((np.asarray(x, dtype=np.float64) - edge0) / (edge1 - edge0), 0.0, 1.0)
    result = t * t * (3.0 - 2.0 * t)
    return result


def _require_nonnegative(c: np.ndarray, space: str) -> None:
    """转换域声明:HSV/HSL 接受非负幅度(与 GLSL 输入 abs(color) 后的域一致)。

    负通道不在裸转换的连续可解释域内——它们经 ``apply_warper_legacy_hsv``
    /``rgb_to_hsp`` 的 sign/abs 包装处理;裸转换喂负值是调用错误,显式报错
    而不是产出无定义结果。
    """
    if (c < 0).any():
        raise ValueError(f"{space} 转换域:输入必须为非负幅度(负通道经 sign/abs 包装)")


def rgb_to_hsv(color) -> np.ndarray:
    """RGB→HSV,逐句镜像 GLSL rgb2hsv(K 向量算法,含 1e-10 防护)。"""
    c = _as_float_array(color)
    _require_nonnegative(c, "HSV")
    # p = mix(vec4(c.bg, K.wz), vec4(c.gb, K.xy), step(c.b, c.g))
    #   step(c.b, c.g): g >= b ? 1 : 0 → 选 vec4(c.gb, K.xy)
    if c[1] >= c[2]:
        p = np.array([c[1], c[2], 0.0, -1.0 / 3.0])
    else:
        p = np.array([c[2], c[1], -1.0, 2.0 / 3.0])
    # q = mix(vec4(p.xyw, c.r), vec4(c.r, p.yzx), step(p.x, c.r))
    #   r >= p.x → 选 vec4(c.r, p.y, p.z, p.x)
    if c[0] >= p[0]:
        q = np.array([c[0], p[1], p[2], p[0]])
    else:
        q = np.array([p[0], p[1], p[3], c[0]])
    d = q[0] - min(q[3], q[1])
    e = 1.0e-10
    return np.array([
        abs(q[2] + (q[3] - q[1]) / (6.0 * d + e)),
        d / (q[0] + e),
        q[0],
    ])


def hsv_to_rgb(hsv) -> np.ndarray:
    """HSV→RGB,逐句镜像 GLSL hsv2rgb。"""
    c = _as_float_array(hsv)
    k = np.array([1.0, 2.0 / 3.0, 1.0 / 3.0, 3.0])
    p = np.abs((np.mod(c[0] + k[:3], 1.0)) * 6.0 - k[3])
    # mix(K.xxx, clamp(p - K.xxx, 0, 1), c.y) * c.z
    return c[2] * ((1.0 - c[1]) * 1.0 + c[1] * np.clip(p - 1.0, 0.0, 1.0))


def rgb_to_hsl(color) -> np.ndarray:
    """标准 HSL(精确公式);零色度 H=S=0。

    域声明(Topos):经典 HSL 的 S 分母在 ``mx+mn>2``(L>1,超高亮 HDR)
    为负、语义不连续。按 B03"不无理由 clamp"原则,超域显式拒绝——
    超亮 HDR 请用 HSV/HSP(两者对任意有限幅度连续)。
    """
    c = _as_float_array(color)
    _require_nonnegative(c, "HSL")
    mx, mn = c.max(), c.min()
    lightness = (mx + mn) * 0.5
    if mx == mn:
        return np.array([0.0, 0.0, lightness])
    if mx + mn >= 2.0:
        # 边界 mx+mn==2(L==1)时分母为 0(S→∞),同样不在连续域内
        raise ValueError("HSL 域:mx+mn≥2(L≥1 超高亮)无连续定义,请用 HSV/HSP")
    d = mx - mn
    saturation = d / (2.0 - mx - mn) if lightness > 0.5 else d / (mx + mn)
    if mx == c[0]:
        hue = ((c[1] - c[2]) / d) % 6.0
    elif mx == c[1]:
        hue = (c[2] - c[0]) / d + 2.0
    else:
        hue = (c[0] - c[1]) / d + 4.0
    return np.array([hue / 6.0, saturation, lightness])


def hsl_to_rgb(hsl) -> np.ndarray:
    """标准 HSL→RGB(C/X/m 精确公式)。"""
    c = _as_float_array(hsl)
    hue, sat, lig = c
    if sat == 0.0:
        return np.array([lig, lig, lig])
    chroma = (1.0 - abs(2.0 * lig - 1.0)) * sat
    # 标准 X 分量:X = C·(1−|(h·6 mod 2)−1|),上升/下降扇区统一公式
    h6 = hue * 6.0
    sector = int(np.floor(h6)) % 6
    x = chroma * (1.0 - abs((h6 % 2.0) - 1.0))
    table = {
        0: (chroma, x, 0.0), 1: (x, chroma, 0.0), 2: (0.0, chroma, x),
        3: (0.0, x, chroma), 4: (x, 0.0, chroma), 5: (chroma, 0.0, x),
    }
    r, g, b = table[sector]
    m = lig - chroma * 0.5
    return np.array([r + m, g + m, b + m])


def rgb_to_hsp(color) -> np.ndarray:
    """RGB→HSP(Topos 声明):H/S 同 HSV,P=加权平方和开方。转换域=非负幅度。"""
    c = _as_float_array(color)
    _require_nonnegative(c, "HSP")
    magnitude = c
    hsv = rgb_to_hsv(magnitude)
    brightness = float(np.sqrt(np.dot(HSP_WEIGHTS, magnitude * magnitude)))
    return np.array([hsv[0], hsv[1], brightness])


def hsp_to_rgb(hsp) -> np.ndarray:
    """HSP→RGB:利用平方和齐次性精确反演;k=P/P(RGB₀),RGB=k·RGB₀。"""
    c = _as_float_array(hsp)
    hue, sat, brightness = c
    if sat <= 0.0:
        return np.array([brightness, brightness, brightness])
    base = hsv_to_rgb(np.array([hue, sat, 1.0]))
    base_p = float(np.sqrt(np.dot(HSP_WEIGHTS, base * base)))
    if base_p <= 0.0:
        return np.zeros(3)
    return base * (brightness / base_p)


def sample_warper_bilinear(texels: np.ndarray, u: float, v: float) -> np.ndarray:
    """镜像 GPU LINEAR+CLAMP_TO_EDGE 采样(RG32F 网格纹理)。

    ``texels`` 形状 (rows, cols, C):行=环(sat,V 轴),列=射线(hue,U 轴)。
    坐标先 clamp 到 [0,1](GPU CLAMP_TO_EDGE 语义),再按纹素中心双线性。
    色相环绕只发生在 shader 的 fract 之后;接缝两侧各自取边缘纹素。
    """
    if texels.ndim != 3:
        raise ValueError(f"网格纹理需要 (rows, cols, C),得到 {texels.shape}")
    rows, cols, _ = texels.shape
    x = min(max(u, 0.0), 1.0) * cols - 0.5
    y = min(max(v, 0.0), 1.0) * rows - 0.5
    x0, y0 = int(np.floor(x)), int(np.floor(y))
    fx, fy = x - x0, y - y0
    def fetch(ix: int, iy: int) -> np.ndarray:
        cx = min(max(ix, 0), cols - 1)
        cy = min(max(iy, 0), rows - 1)
        return texels[cy, cx]
    top = fetch(x0, y0) * (1.0 - fx) + fetch(x0 + 1, y0) * fx
    bottom = fetch(x0, y0 + 1) * (1.0 - fx) + fetch(x0 + 1, y0 + 1) * fx
    return top * (1.0 - fy) + bottom * fy


def apply_warper_legacy_hsv(
    rgb,
    texels: np.ndarray,
    *,
    grid_size: float | None = None,  # 与 shader ABI 一致:不参与计算
) -> np.ndarray:
    """``WARPER_SNIPPET`` 的逐句 CPU 镜像(legacy_hsv_v1 行为基准)。

    语义(与 GLSL/MSL 逐句等价):
    - 保留逐通道符号:sign(color)*hsv2rgb(...)(负值/超白先 abs 转换);
    - sat<0.02 直接 bypass;
    - 采样 (hue, sat) 得 (Δhue, Δsat),乘 smoothstep(0.02,0.15,sat) 衰减;
    - 输出 hue 经 fract 环绕,sat clamp [0,1](legacy 显式语义)。
    """
    color = _as_float_array(rgb)
    sign_c = np.sign(color)
    hsv = rgb_to_hsv(np.abs(color))
    if hsv[1] < 0.02:
        return color
    delta = sample_warper_bilinear(texels, hsv[0], hsv[1])
    sat_fade = smoothstep(0.02, 0.15, hsv[1])
    out_hue = np.mod(hsv[0] + delta[0] * sat_fade, 1.0)
    out_sat = min(max(hsv[1] + delta[1] * sat_fade, 0.0), 1.0)
    return sign_c * hsv_to_rgb(np.array([out_hue, out_sat, hsv[2]]))


def apply_warper_in_space(rgb, texels: np.ndarray, space: str) -> np.ndarray:
    """CW-B08:多空间网格 warper 的 CPU 参考(镜像 WARPER_SPACE_SNIPPET)。

    语义与 legacy 族一致:sign 乘法(0 通道输出恒 0)、sat<0.02 旁路、
    smoothstep(0.02,0.15) 衰减、hue fract、sat clamp;HSL 非灰超亮域
    (mx+mn>=2)与 shader 一致地旁路。space ∈ {"hsv","hsl","hsp"}。
    """
    index = WARPER_SPACE_INDEX.get(space)
    if index is None:
        raise ValueError(f"未知 warper 空间 {space!r}")
    color = _as_float_array(rgb)
    sign_c = np.sign(color)
    magnitude = np.abs(color)
    if index == 1:
        mx, mn = magnitude.max(), magnitude.min()
        if mx != mn and mx + mn >= 2.0:
            return color  # HSL 声明域外:旁路(与 shader 一致)
        coords = rgb_to_hsl(magnitude)
    elif index == 2:
        coords = rgb_to_hsp(magnitude)
    else:
        coords = rgb_to_hsv(magnitude)
    if coords[1] < 0.02:
        return color
    delta = sample_warper_bilinear(texels, coords[0], coords[1])
    fade = smoothstep(0.02, 0.15, coords[1])
    out_hue = np.mod(coords[0] + delta[0] * fade, 1.0)
    out_sat = min(max(coords[1] + delta[1] * fade, 0.0), 1.0)
    warped = np.array([out_hue, out_sat, coords[2]])
    if index == 1:
        rgb_out = hsl_to_rgb(warped)
    elif index == 2:
        rgb_out = hsp_to_rgb(warped)
    else:
        rgb_out = hsv_to_rgb(warped)
    return sign_c * rgb_out
