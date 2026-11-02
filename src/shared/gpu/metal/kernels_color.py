"""调色工具链的 MSL film grain / soft clip kernel（计划 P6-06）。

与 ``src/features/color/core/shader_nodes.py`` 的 ``FILM_GRAIN_SNIPPET`` /
``SOFT_CLIP_SNIPPET`` 逐条对齐，fragment 语义 → compute 形态移植：

- 输入输出均为 RGBA16Float 纹理（与调色管线浮点帧缓冲一致）；per-pixel
  纯函数，无邻域依赖，uv 取像素中心 ``(gid + 0.5) / tex_size``，与
  fragment 路径 ``v_texcoord`` 的插值语义一致。
- HDR 语义（P6-06 风险项）：全程 **不 clamp**。soft clip 保留负值直到
  toe 压缩，shoulder 只按比例缩放；film grain 显式"保留负值空间"。
  kernel 源里禁止出现 clamp —— 测试有回归断言。

随机数契约（P6-06 验收「固定 seed 一致」的实现手段，全部来自实测）：

- GLSL 哈希链（``fract`` 级联）对浮点求值顺序极端敏感：``fract`` 在
  整数边界不连续，链上 1 ulp 差异会翻转整格噪声值。MSL 侧
  **禁用 ``dot()``/``mix()``/``smoothstep()`` 内建**——实测内建
  ``dot()`` 引发 65536 点中 13 处整格翻转；展开为显式同序四则运算后
  哈希链与 numpy 参考**位级一致**（含整数边界、负坐标）。
- 源码头部 ``#pragma clang fp contract(off)`` 实测有效且必要
  （contract(fast) 下几乎全部采样点不一致）。
- 残余差异（不可消除）：``value_noise`` 的最终 lerp 形状
  ``m1*(1-t) + m2*t`` 会被 AIR 后端代数重写，contract(off) 与
  ``noinline`` 均无法阻止（实测）。噪声值偏差 ≤1 fp32 ulp，经 fp16
  输出量化后 **每像素 ≤1 fp16 ulp**；固定 seed 的颗粒图案结构、
  确定性（同设备两次运行位级一致，实测）与统计分布不受影响。
  门禁按此制定（确定性=位级；vs 参考=1 fp16 ulp）。
- numpy 参考与 kernel 由同一 ``build_film_grain_params`` /
  ``build_soft_clip_params`` 驱动，参数结构逐字段对齐。
- ``pow()``（soft clip）为 libm 与 air 的 ulp 级差异，同门禁覆盖。
- 当前 GLSL fragment 路径（GL 编译器）存在同类调度差异：单像素
  ±1ulp 级抖动、统计分布不变，属跨后端固有差异。Phase 6 渲染接入
  （P6-02~04）切换后以本 kernel 语义为准。

接口说明：GLSL ``apply_film_grain`` 签名里的 ``aspect_ratio`` 在原实现
中不参与任何计算（死参数），kernel 契约不收录；GLSL 侧若将来启用需
同步加回。GLSL toe 分支中 ``toe_diff`` 同为死代码，未移植。
"""

from __future__ import annotations

from typing import Tuple

import numpy as np

_F32 = np.float32

# contract(off)：禁止 a*b+c → FMA 收缩（实测必要：contract(fast) 下
# 几乎全部采样点不一致）。残余 ≤1ulp 差异来自 AIR 后端的 lerp 代数
# 重写，contract 不可阻止——见模块文档「随机数契约」。
METAL_FILM_GRAIN_MSL = """
#pragma clang fp contract(off)
#include <metal_stdlib>
using namespace metal;

// —— 顺序敏感区：随机原语全部展开为显式四则运算（内建原语的求值
//    顺序与 FMA 收缩不可控，实测会使哈希链整格翻转），与
//    kernels_color.film_grain_reference 逐项同序。残余 ≤1 fp32 ulp
//    差异来自 AIR 后端对插值形状的代数重写（contract(off) 不可阻止），
//    量化后 ≤1 fp16 ulp——测试门禁按此制定。不得单侧修改。

struct FilmGrainParams {
    float intensity;
    float size;
    float shadows;
    float midtones;
    float highlights;
    float seed;
    float rect_x;   // u_visible_rect.x（原图绝对物理像素）
    float rect_y;
    float rect_w;   // >0 才走视口映射分支
    float rect_h;
    float tex_w;    // u_tex_size.x（原始全分辨率，尺度补偿用）
    float tex_h;
    uint  monochrome;
};

float hash_grain(float px, float py) {
    float vx = px * 0.1031;
    float vy = py * 0.1031;
    float p3x = vx - floor(vx);
    float p3y = vy - floor(vy);
    float p3z = p3x;
    // GLSL: p3 += dot(p3, p3.yzx + 33.33) —— dot 展开为显式同序求和
    float d = p3x*(p3y + 33.33) + p3y*(p3z + 33.33) + p3z*(p3x + 33.33);
    p3x = p3x + d;
    p3y = p3y + d;
    p3z = p3z + d;
    float s = (p3x + p3y) * p3z;
    return s - floor(s);
}

float value_noise(float px, float py) {
    float ix = floor(px);
    float iy = floor(py);
    float fx = px - ix;
    float fy = py - iy;
    float ux = fx*fx*(3.0 - 2.0*fx);
    float uy = fy*fy*(3.0 - 2.0*fy);
    float a = hash_grain(ix + 0.0, iy + 0.0);
    float b = hash_grain(ix + 1.0, iy + 0.0);
    float c = hash_grain(ix + 0.0, iy + 1.0);
    float d = hash_grain(ix + 1.0, iy + 1.0);
    // mix(x, y, t) = x*(1-t) + y*t，显式展开
    float m1 = a*(1.0 - ux) + b*ux;
    float m2 = c*(1.0 - ux) + d*ux;
    return m1*(1.0 - uy) + m2*uy;
}

float fbm_grain(float px, float py, float seed) {
    float value = 0.0;
    float amplitude = 0.5;
    float qx = px + seed * 100.0;
    float qy = py + seed * 100.0;
    for (int i = 0; i < 3; i++) {
        value = value + amplitude * value_noise(qx, qy);
        qx = qx * 2.0;
        qy = qy * 2.0;
        amplitude = amplitude * 0.5;
    }
    return value;
}

float smooth_step(float e0, float e1, float x) {
    float t = (x - e0) / (e1 - e0);
    t = min(max(t, 0.0), 1.0);
    return t*t*(3.0 - 2.0*t);
}

kernel void film_grain(
    texture2d<float, access::read> src [[texture(0)]],
    texture2d<float, access::write> dst [[texture(1)]],
    constant FilmGrainParams& p [[buffer(0)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= dst.get_width() || gid.y >= dst.get_height()) { return; }
    float4 c = src.read(gid);
    if (p.intensity <= 0.0) { dst.write(c, gid); return; }

    // 1. 尺度补偿（2048px 基准）+ 2. 绝对物理像素坐标映射
    float scale_factor = max(1.0, p.tex_w / 2048.0);
    float physical_size = max(p.size * scale_factor, 1.0);
    float uvx = (float(gid.x) + 0.5) / p.tex_w;
    float uvy = (float(gid.y) + 0.5) / p.tex_h;
    float gx, gy;
    if (p.rect_w > 0.0 && p.rect_h > 0.0) {
        float ax = p.rect_x + uvx * p.rect_w;
        float ay = p.rect_y + uvy * p.rect_h;
        gx = ax / physical_size;
        gy = ay / physical_size;
    } else {
        gx = (uvx * p.tex_w) / physical_size;
        gy = (uvy * p.tex_h) / physical_size;
    }

    // 3. 有机银盐颗粒（单色共享图案 / 彩色三层乳剂错位）
    float nr, ng, nb;
    if (p.monochrome != 0) {
        float n = fbm_grain(gx, gy, p.seed);
        nr = n; ng = n; nb = n;
    } else {
        nr = fbm_grain(gx, gy, p.seed);
        ng = fbm_grain(gx + 12.3, gy + 45.6, p.seed);
        nb = fbm_grain(gx + 78.9, gy + 12.3, p.seed);
    }

    // 4. 亮度响应蒙版 + 5. soft light 混合与对比度保护
    float luma = c.r * 0.299 + c.g * 0.587 + c.b * 0.114;
    float mask;
    if (luma < 0.5) {
        float t = smooth_step(0.0, 0.5, luma);
        mask = p.shadows*(1.0 - t) + p.midtones*t;
    } else {
        float t = smooth_step(0.5, 1.0, luma);
        mask = p.midtones*(1.0 - t) + p.highlights*t;
    }
    float cp = max(1.0 - abs(luma - 0.5) * 1.5, 0.2);
    float off_r = ((nr - 0.5) * p.intensity) * mask * cp;
    float off_g = ((ng - 0.5) * p.intensity) * mask * cp;
    float off_b = ((nb - 0.5) * p.intensity) * mask * cp;

    // 不 clamp：保留负值空间，暗部细节可恢复
    dst.write(float4(c.r + off_r, c.g + off_g, c.b + off_b, c.a), gid);
}
"""

# 与 METAL_FILM_GRAIN_MSL 的 FilmGrainParams 逐字段对齐（4 字节 ×13=52）
FILM_GRAIN_PARAMS_DTYPE = np.dtype([
    ("intensity", "<f4"),
    ("size", "<f4"),
    ("shadows", "<f4"),
    ("midtones", "<f4"),
    ("highlights", "<f4"),
    ("seed", "<f4"),
    ("rect_x", "<f4"),
    ("rect_y", "<f4"),
    ("rect_w", "<f4"),
    ("rect_h", "<f4"),
    ("tex_w", "<f4"),
    ("tex_h", "<f4"),
    ("monochrome", "<u4"),
])


def build_film_grain_params(
    intensity: float,
    size: float,
    monochrome: bool,
    shadows: float,
    midtones: float,
    highlights: float,
    seed: float,
    visible_rect: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0),
    tex_size: Tuple[float, float] = (2048.0, 2048.0),
) -> np.ndarray:
    """构造 film_grain kernel 参数（单元素结构化数组）。

    ``visible_rect`` 为 u_visible_rect (x, y, w, h)，原图绝对物理像素；
    w/h 均 >0 走视口映射，否则回退纹理尺寸分支（与 GLSL 一致）。
    非有限输入显式报错（.clinerules:11）——NaN 静默进哈希链会把整幅
    颗粒变成 NaN 而表面"正常编译"。
    """
    scalars = [intensity, size, shadows, midtones, highlights, seed,
               *visible_rect, *tex_size]
    if not all(np.isfinite(v) for v in scalars):
        raise ValueError(f"film_grain: 参数含非有限值 {scalars!r}")
    params = np.zeros(1, dtype=FILM_GRAIN_PARAMS_DTYPE)
    params["intensity"] = intensity
    params["size"] = size
    params["shadows"] = shadows
    params["midtones"] = midtones
    params["highlights"] = highlights
    params["seed"] = seed
    params["rect_x"] = visible_rect[0]
    params["rect_y"] = visible_rect[1]
    params["rect_w"] = visible_rect[2]
    params["rect_h"] = visible_rect[3]
    params["tex_w"] = tex_size[0]
    params["tex_h"] = tex_size[1]
    params["monochrome"] = 1 if monochrome else 0
    return params


METAL_SOFT_CLIP_MSL = """
#pragma clang fp contract(off)
#include <metal_stdlib>
using namespace metal;

// —— 顺序敏感区：与 kernels_color.soft_clip_reference 逐项同序。

struct SoftClipParams {
    float high;
    float white_point;
    float shoulder_strength;
    float low;
    float black_point;
    float toe_strength;
    float exposure;
    float contrast;
};

float toe_compress(float c, float low, float black_point, float toe_strength) {
    // GLSL for 循环 res[i] 动态索引 → 逐通道显式调用（无语义差异：
    // 各通道公式相同且互不依赖）
    if (c < low && black_point < low) {
        float T = low;
        float B = black_point;
        float k = max(toe_strength, 0.01);
        float diff = T - c;
        float range = T - B;
        float pw = pow(1.0 + k * diff / range, -1.0 / k);
        return B + range * pw;
    } else if (c < black_point) {
        return black_point;
    }
    return c;
}

float contrast_curve(float c, float factor) {
    // sign ± pow(|x|/0.18, factor) ± 0.18：S 曲线延展到负数空间
    return sign(c) * pow(abs(c) / 0.18, factor) * 0.18;
}

kernel void soft_clip(
    texture2d<float, access::read> src [[texture(0)]],
    texture2d<float, access::write> dst [[texture(1)]],
    constant SoftClipParams& p [[buffer(0)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= dst.get_width() || gid.y >= dst.get_height()) { return; }
    float4 c4 = src.read(gid);
    float3 res = c4.rgb * pow(2.0, p.exposure);

    if (p.contrast != 0.0) {
        float factor = (p.contrast >= 0.0) ? (1.0 + p.contrast)
                                           : (1.0 / (1.0 + abs(p.contrast)));
        res = float3(contrast_curve(res.x, factor),
                     contrast_curve(res.y, factor),
                     contrast_curve(res.z, factor));
    }

    // 暗部 toe 逐通道压缩 + 通道平衡校正（GLSL 的 toe_diff 为死代码未移植）
    res.x = toe_compress(res.x, p.low, p.black_point, p.toe_strength);
    res.y = toe_compress(res.y, p.low, p.black_point, p.toe_strength);
    res.z = toe_compress(res.z, p.low, p.black_point, p.toe_strength);

    float toe_min = min(min(res.x, res.y), res.z);
    float toe_max = max(max(res.x, res.y), res.z);
    if (toe_max - toe_min > 0.2 && toe_min < p.low) {
        float balance_factor = min(max((toe_max - toe_min) * 0.25, 0.0), 0.15);
        float toe_luma = res.x * 0.299 + res.y * 0.587 + res.z * 0.114;
        float m = balance_factor * abs(toe_min);
        res = res * (1.0 - m) + toe_luma * m;
    }

    // 亮部 shoulder 比例缩放保护（等比 → 色彩饱和度/色相不变）
    float max_c = max(max(res.x, res.y), res.z);
    if (max_c > p.high && p.white_point > p.high) {
        float T = p.high;
        float W = p.white_point;
        float k = max(p.shoulder_strength, 0.01);
        float diff = max_c - T;
        float range = W - T;
        float mapped = W - range * pow(1.0 + k * diff / range, -1.0 / k);
        float scale = mapped / max_c;
        res = res * scale;
    } else if (max_c > p.white_point) {
        float scale = p.white_point / max_c;
        res = res * scale;
    }

    // 不 clamp：HDR 域输出，负值已由 toe 收束但公式本身不设界
    dst.write(float4(res, c4.a), gid);
}
"""

# 与 METAL_SOFT_CLIP_MSL 的 SoftClipParams 逐字段对齐（4 字节 ×8=32）
SOFT_CLIP_PARAMS_DTYPE = np.dtype([
    ("high", "<f4"),
    ("white_point", "<f4"),
    ("shoulder_strength", "<f4"),
    ("low", "<f4"),
    ("black_point", "<f4"),
    ("toe_strength", "<f4"),
    ("exposure", "<f4"),
    ("contrast", "<f4"),
])


def build_soft_clip_params(
    high: float,
    white_point: float,
    shoulder_strength: float,
    low: float,
    black_point: float,
    toe_strength: float,
    exposure: float,
    contrast: float,
) -> np.ndarray:
    """构造 soft_clip kernel 参数（单元素结构化数组）。"""
    scalars = [high, white_point, shoulder_strength, low, black_point,
               toe_strength, exposure, contrast]
    if not all(np.isfinite(v) for v in scalars):
        raise ValueError(f"soft_clip: 参数含非有限值 {scalars!r}")
    params = np.zeros(1, dtype=SOFT_CLIP_PARAMS_DTYPE)
    params["high"] = high
    params["white_point"] = white_point
    params["shoulder_strength"] = shoulder_strength
    params["low"] = low
    params["black_point"] = black_point
    params["toe_strength"] = toe_strength
    params["exposure"] = exposure
    params["contrast"] = contrast
    return params


# ---------------------------------------------------------------------------
# numpy 参考实现（测试画质/位级门禁用；与 kernel 逐项同序，不得单侧修改）
# ---------------------------------------------------------------------------


def _fract(x: np.ndarray) -> np.ndarray:
    return (x - np.floor(x)).astype(_F32)


def _hash_grain(px: np.ndarray, py: np.ndarray) -> np.ndarray:
    """hash_grain(vec2)：dot 显式展开（同序）是位级一致的关键。"""
    vx = px * _F32(0.1031)
    vy = py * _F32(0.1031)
    p3x = _fract(vx)
    p3y = _fract(vy)
    p3z = _fract(vx)
    d = (p3x * (p3y + _F32(33.33))
         + p3y * (p3z + _F32(33.33))
         + p3z * (p3x + _F32(33.33)))
    p3x = p3x + d
    p3y = p3y + d
    p3z = p3z + d
    return _fract((p3x + p3y) * p3z)


def _value_noise(px: np.ndarray, py: np.ndarray) -> np.ndarray:
    ix = np.floor(px)
    iy = np.floor(py)
    fx = px - ix
    fy = py - iy
    ux = fx * fx * (_F32(3.0) - _F32(2.0) * fx)
    uy = fy * fy * (_F32(3.0) - _F32(2.0) * fy)
    a = _hash_grain(ix + _F32(0.0), iy + _F32(0.0))
    b = _hash_grain(ix + _F32(1.0), iy + _F32(0.0))
    c = _hash_grain(ix + _F32(0.0), iy + _F32(1.0))
    d = _hash_grain(ix + _F32(1.0), iy + _F32(1.0))
    m1 = a * (_F32(1.0) - ux) + b * ux
    m2 = c * (_F32(1.0) - ux) + d * ux
    return m1 * (_F32(1.0) - uy) + m2 * uy


def _fbm_grain(px: np.ndarray, py: np.ndarray, seed: float) -> np.ndarray:
    value = np.zeros(px.shape, dtype=_F32)
    amplitude = _F32(0.5)
    qx = px + _F32(seed) * _F32(100.0)
    qy = py + _F32(seed) * _F32(100.0)
    for _ in range(3):
        value = value + amplitude * _value_noise(qx, qy)
        qx = qx * _F32(2.0)
        qy = qy * _F32(2.0)
        amplitude = _F32(amplitude * _F32(0.5))
    return value


def _smooth_step(e0, e1, x: np.ndarray) -> np.ndarray:
    t = (x - _F32(e0)) / _F32(_F32(e1) - _F32(e0))
    t = np.clip(t, _F32(0.0), _F32(1.0))
    return t * t * (_F32(3.0) - _F32(2.0) * t)


def film_grain_reference(
    color: np.ndarray,
    *,
    intensity: float,
    size: float,
    monochrome: bool,
    shadows: float,
    midtones: float,
    highlights: float,
    seed: float,
    visible_rect: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0),
    tex_size: Tuple[float, float] = (2048.0, 2048.0),
) -> np.ndarray:
    """numpy 参考：与 METAL_FILM_GRAIN_MSL 逐项同序（测试位级门禁用）。

    Args:
        color: (H, W, 3) float32，HDR 域（可 <0 / >1）。
    Returns:
        (H, W, 3) float32，不 clamp。
    """
    color = np.ascontiguousarray(color, dtype=_F32)
    if intensity <= 0.0:
        return color.copy()
    h, w = color.shape[:2]
    tex_w, tex_h = _F32(tex_size[0]), _F32(tex_size[1])

    scale_factor = max(_F32(tex_w / _F32(2048.0)), _F32(1.0))
    physical_size = max(_F32(size) * scale_factor, _F32(1.0))

    uvx = (np.arange(w, dtype=_F32) + _F32(0.5)) / tex_w
    uvy = (np.arange(h, dtype=_F32) + _F32(0.5)) / tex_h
    rect_x, rect_y, rect_w, rect_h = visible_rect
    if rect_w > 0.0 and rect_h > 0.0:
        gx = (_F32(rect_x) + uvx * _F32(rect_w)) / physical_size
        gy = (_F32(rect_y) + uvy * _F32(rect_h)) / physical_size
    else:
        gx = (uvx * tex_w) / physical_size
        gy = (uvy * tex_h) / physical_size
    gx = np.broadcast_to(gx[None, :], (h, w))
    gy = np.broadcast_to(gy[:, None], (h, w))

    if monochrome:
        n = _fbm_grain(gx, gy, seed)
        noise = (n, n, n)
    else:
        noise = (
            _fbm_grain(gx, gy, seed),
            _fbm_grain(gx + _F32(12.3), gy + _F32(45.6), seed),
            _fbm_grain(gx + _F32(78.9), gy + _F32(12.3), seed),
        )

    luma = (color[..., 0] * _F32(0.299)
            + color[..., 1] * _F32(0.587)
            + color[..., 2] * _F32(0.114))
    t_low = _smooth_step(0.0, 0.5, luma)
    t_high = _smooth_step(0.5, 1.0, luma)
    mask = np.where(
        luma < _F32(0.5),
        _F32(shadows) * (_F32(1.0) - t_low) + _F32(midtones) * t_low,
        _F32(midtones) * (_F32(1.0) - t_high) + _F32(highlights) * t_high,
    )
    cp = np.maximum(
        _F32(1.0) - np.abs(luma - _F32(0.5)) * _F32(1.5), _F32(0.2)
    )

    out = np.empty_like(color)
    for ch in range(3):
        # 同序：(noise-0.5)*intensity*mask（左结合）后再乘 cp
        off = ((noise[ch] - _F32(0.5)) * _F32(intensity)) * mask * cp
        out[..., ch] = color[..., ch] + off
    return out


def soft_clip_reference(
    color: np.ndarray,
    *,
    high: float,
    white_point: float,
    shoulder_strength: float,
    low: float,
    black_point: float,
    toe_strength: float,
    exposure: float,
    contrast: float,
) -> np.ndarray:
    """numpy 参考：与 METAL_SOFT_CLIP_MSL 逐项同序（测试 parity 门禁用）。

    Args:
        color: (H, W, 3) float32，HDR 域（可 <0 / >1）。
    Returns:
        (H, W, 3) float32，不 clamp。
    """
    color = np.ascontiguousarray(color, dtype=_F32)
    low32, black32 = _F32(low), _F32(black_point)
    high32, white32 = _F32(high), _F32(white_point)

    # 未选中分支会产生 inf/nan（range<=0 时 pow 发散），用 errstate 吞掉
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        res = color * _F32(np.power(_F32(2.0), _F32(exposure)))

        if contrast != 0.0:
            if contrast >= 0.0:
                factor = _F32(_F32(1.0) + _F32(contrast))
            else:
                # 全程 f32 除法：python 的 1.0/x 会走 f64 双舍入
                factor = _F32(_F32(1.0) / (_F32(1.0) + abs(_F32(contrast))))
            base = np.abs(res) / _F32(0.18)
            res = (np.sign(res) * np.power(base, factor)) * _F32(0.18)

        # toe：逐通道（GLSL for 循环 → 3 次向量化调用，语义等价）
        toe_k = max(_F32(toe_strength), _F32(0.01))
        cond1 = (res < low32) & (black32 < low32)
        diff = low32 - res
        rng = low32 - black32
        pw = np.power(_F32(1.0) + (toe_k * diff) / rng, _F32(-1.0) / toe_k)
        mapped = black32 + rng * pw
        res = np.where(cond1, mapped, np.where(res < black32, black32, res))

        # 暗部通道平衡
        toe_min = np.min(res, axis=-1)
        toe_max = np.max(res, axis=-1)
        need = ((toe_max - toe_min) > _F32(0.2)) & (toe_min < low32)
        balance_factor = np.clip(
            (toe_max - toe_min) * _F32(0.25), _F32(0.0), _F32(0.15)
        )
        toe_luma = (res[..., 0] * _F32(0.299)
                    + res[..., 1] * _F32(0.587)
                    + res[..., 2] * _F32(0.114))
        m = balance_factor * np.abs(toe_min)
        mixed = (res * (_F32(1.0) - m)[..., None]
                 + (toe_luma * m)[..., None])
        res = np.where(need[..., None], mixed, res)

        # shoulder：等比缩放
        max_c = np.max(res, axis=-1)
        sh_k = max(_F32(shoulder_strength), _F32(0.01))
        cond_s = (max_c > high32) & (white32 > high32)
        diff_s = max_c - high32
        rng_s = white32 - high32
        pw_s = np.power(_F32(1.0) + (sh_k * diff_s) / rng_s, _F32(-1.0) / sh_k)
        mapped_s = white32 - rng_s * pw_s
        scale = np.where(
            cond_s,
            mapped_s / max_c,
            np.where(max_c > white32, white32 / max_c, _F32(1.0)),
        )
        res = res * scale[..., None]
    return res.astype(_F32)


# ---------------------------------------------------------------------------
# 调色节点图算子 MSL compute kernels（计划 B12.3，E23；N10 执行器消费）
# ---------------------------------------------------------------------------

# 语义对齐要点：
# - 分支/键/通道算子与 docs/color_nodes_mixer_spec.md（v1 冻结）及
#   src/features/color/shaders/node_ops_gl.py 的 GL fragment 逐条对应；
#   buffer 布局为 RGBA16F 线性数组（half4 = 4×float16，与 kernels.py
#   的 CPU staging 桥约定一致）。
# - graph_grading 是 GRADING_SNIPPET/CURVES_SNIPPET 的 compute 形态翻译
#   （GLSL 权威文本的逐条同序移植，参照 shader_nodes_msl 的翻译契约；
#   门禁走 G2 容差：SDR ≤1/255、HDR ≤2e-3 rel）。curves LUT 以 768
#   float 线性 buffer 传入（texel 中心 LINEAR 采样 ≡ 整数索引取整，
#   曲线外线性外推 + 软性保护与 GLSL 同序）。
# - HDR 语义：RGB 路径不 clamp（负值/HDR >1 保留）；key 域按 spec 有界
#   [0,1]（key_mix/补集是 matte 语义，非图像域）。
# - 邻域参数（mid_detail/sharpness）、插件、mixer/warper/LUT3D/fx 不在
#   kernel 内——N10 执行器按能力矩阵 fail-closed 拒绝，不静默近似。

METAL_GRAPH_OPS_MSL = """
#include <metal_stdlib>
using namespace metal;

struct GraphSizeParams {
    uint32_t width;
    uint32_t height;
    float    a;        // 通用标量（opacity 等）
    int32_t  b;        // 通用枚举（mode/channel）
};

struct GraphGradingParams {
    float exposure;
    float temp;
    float tint;
    float contrast;
    float saturation;
    float vibrance;
    float shadow;
    float highlights;
    float hue_shift;
    float lift_saturation;
    float gamma_saturation;
    float gain_saturation;
    float lift_x;  float lift_y;  float lift_z;
    float gamma_x; float gamma_y; float gamma_z;
    float gain_x;  float gain_y;  float gain_z;
    float opacity;
    uint32_t mask_mode;   // 0 = 无 matte（k = opacity）；1 = matte buffer
    uint32_t has_curves;  // 0 = 跳过 curves
};

static inline float3 mix3(float3 x, float3 y, float t) {
    return x * (1.0 - t) + y * t;
}

kernel void graph_copy(
    device const half4* in  [[buffer(0)]],
    device half4*       out [[buffer(1)]],
    constant GraphSizeParams& p [[buffer(2)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= p.width || gid.y >= p.height) { return; }
    size_t idx = gid.y * (size_t)p.width + gid.x;
    out[idx] = in[idx];
}

// spec §1：out.rgb = mix(base.rgb, branch.rgb, k)；alpha = base.a
kernel void graph_parallel_mix(
    device const half4* base   [[buffer(0)]],
    device const half4* branch [[buffer(1)]],
    device const half4* key    [[buffer(2)]],
    device half4*       out    [[buffer(3)]],
    constant GraphSizeParams& p [[buffer(4)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= p.width || gid.y >= p.height) { return; }
    size_t idx = gid.y * (size_t)p.width + gid.x;
    float4 b = float4(base[idx]);
    float4 br = float4(branch[idx]);
    float k = clamp(float(key[idx].r), 0.0, 1.0);
    out[idx] = half4(float4(mix3(b.rgb, br.rgb, k), b.a));
}

// —— GRADING_SNIPPET.rgb2hsv/hsv2rgb 逐条翻译（与 MSL_HSV_FUNCS 同源；
//    需先于 graph_layer_mix 定义——v1.1 HSV 模式使用）——
static inline float3 graph_rgb2hsv(float3 c) {
    float4 K = float4(0.0, -1.0 / 3.0, 2.0 / 3.0, -1.0);
    float4 p = mix(float4(c.bg, K.wz), float4(c.gb, K.xy), step(c.b, c.g));
    float4 q = mix(float4(p.xyw, c.r), float4(c.r, p.yzx), step(p.x, c.r));
    float d = q.x - min(q.w, q.y);
    float e = 1.0e-10;
    return float3(abs(q.z + (q.w - q.y) / (6.0 * d + e)), d / (q.x + e), q.x);
}

static inline float3 graph_hsv2rgb(float3 c) {
    float4 K = float4(1.0, 2.0 / 3.0, 1.0 / 3.0, 3.0);
    float3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

static inline float graph_overlay(float b, float a) {
    return b < 0.5 ? (2.0 * b * a) : (1.0 - 2.0 * (1.0 - b) * (1.0 - a));
}

static inline float graph_soft_light(float b, float a) {
    return (a < 0.5) ? (2.0 * b * a + b * b * (1.0 - 2.0 * a))
                     : (sqrt(max(b, 0.0)) * (2.0 * a - 1.0)
                        + 2.0 * b * (1.0 - a));
}

// spec §2 v1 + §9 v1.1：mode 0-4 v1 内嵌式；5-20 =
// mix(bottom, apply_blend_mode(bottom, top), opacity)（权威同式）
kernel void graph_layer_mix(
    device const half4* bottom [[buffer(0)]],
    device const half4* top    [[buffer(1)]],
    device half4*       out    [[buffer(2)]],
    constant GraphSizeParams& p [[buffer(3)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= p.width || gid.y >= p.height) { return; }
    size_t idx = gid.y * (size_t)p.width + gid.x;
    float3 a = float4(bottom[idx]).rgb;
    float3 b = float4(top[idx]).rgb;
    float op = p.a;
    float3 outc;
    if (p.b == 0)      { outc = a * (1.0 - op) + b * op; }
    else if (p.b == 1) { outc = a + b * op; }
    else if (p.b == 2) { outc = a - b * op; }
    else if (p.b == 3) { outc = a * (1.0 - op + op * b); }
    else if (p.b == 4) { outc = 1.0 - (1.0 - a) * (1.0 - b * op); }
    else {
        float3 blended;
        if (p.b == 5) {
            blended = float3(graph_overlay(a.r, b.r), graph_overlay(a.g, b.g),
                             graph_overlay(a.b, b.b));
        } else if (p.b == 6) {
            blended = float3(graph_soft_light(a.r, b.r),
                             graph_soft_light(a.g, b.g),
                             graph_soft_light(a.b, b.b));
        } else if (p.b == 7) {
            blended = float3(graph_overlay(b.r, a.r), graph_overlay(b.g, a.g),
                             graph_overlay(b.b, a.b));
        } else if (p.b == 8)  { blended = a / max(float3(0.001), 1.0 - b); }
        else if (p.b == 9)  { blended = 1.0 - (1.0 - a) / max(float3(0.001), b); }
        else if (p.b == 10) { blended = min(a, b); }
        else if (p.b == 11) { blended = max(a, b); }
        else if (p.b == 12) { blended = abs(a - b); }
        else if (p.b == 13) { blended = a + b - 2.0 * a * b; }
        else if (p.b == 14) {
            float3 ha = graph_rgb2hsv(a); float3 hb = graph_rgb2hsv(b);
            blended = graph_hsv2rgb(float3(hb.x, ha.y, ha.z));
        } else if (p.b == 15) {
            float3 ha = graph_rgb2hsv(a); float3 hb = graph_rgb2hsv(b);
            blended = graph_hsv2rgb(float3(ha.x, hb.y, ha.z));
        } else if (p.b == 16) {
            float3 ha = graph_rgb2hsv(a); float3 hb = graph_rgb2hsv(b);
            blended = graph_hsv2rgb(float3(hb.x, hb.y, ha.z));
        } else if (p.b == 17) {
            float3 ha = graph_rgb2hsv(a); float3 hb = graph_rgb2hsv(b);
            blended = graph_hsv2rgb(float3(ha.x, ha.y, hb.z));
        } else if (p.b == 18) { blended = a + b; }
        else if (p.b == 19) { blended = max(a - b, 0.0); }
        else                { blended = a / max(float3(0.001), b); }
        outc = a * (1.0 - op) + blended * op;
    }
    out[idx] = half4(float4(outc, float4(bottom[idx]).a));
}

// spec §3：key 布尔混合（add=max / subtract / intersect），输出 R 通道
kernel void graph_key_mix(
    device const half4* ka [[buffer(0)]],
    device const half4* kb [[buffer(1)]],
    device half4*       out [[buffer(2)]],
    constant GraphSizeParams& p [[buffer(3)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= p.width || gid.y >= p.height) { return; }
    size_t idx = gid.y * (size_t)p.width + gid.x;
    float a = clamp(float(ka[idx].r), 0.0, 1.0);
    float b = clamp(float(kb[idx].r), 0.0, 1.0);
    float k;
    if (p.b == 0)      { k = max(a, b); }
    else if (p.b == 1) { k = clamp(a - b, 0.0, 1.0); }
    else               { k = a * b; }
    out[idx] = half4(float4(k, k, k, 1.0));
}

// spec §4：key 补集（matte 域有界）
kernel void graph_key_complement(
    device const half4* key [[buffer(0)]],
    device half4*       out [[buffer(1)]],
    constant GraphSizeParams& p [[buffer(2)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= p.width || gid.y >= p.height) { return; }
    size_t idx = gid.y * (size_t)p.width + gid.x;
    float k = clamp(float(key[idx].r), 0.0, 1.0);
    float c = 1.0 - k;
    out[idx] = half4(float4(c, c, c, 1.0));
}

kernel void graph_key_passthrough(
    device const half4* key [[buffer(0)]],
    device half4*       out [[buffer(1)]],
    constant GraphSizeParams& p [[buffer(2)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= p.width || gid.y >= p.height) { return; }
    size_t idx = gid.y * (size_t)p.width + gid.x;
    float k = clamp(float(key[idx].r), 0.0, 1.0);
    out[idx] = half4(float4(k, k, k, 1.0));
}

// spec §5：channel 0/1/2 → 灰度载体（alpha 随图像）
kernel void graph_splitter(
    device const half4* in  [[buffer(0)]],
    device half4*       out [[buffer(1)]],
    constant GraphSizeParams& p [[buffer(2)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= p.width || gid.y >= p.height) { return; }
    size_t idx = gid.y * (size_t)p.width + gid.x;
    float4 t = float4(in[idx]);
    float v = t.r;
    if (p.b == 1) { v = t.g; }
    else if (p.b == 2) { v = t.b; }
    out[idx] = half4(float4(float3(v), t.a));
}

// spec §5：R/G/B 载体重组（alpha 取 R 载体）
kernel void graph_combiner(
    device const half4* r_in [[buffer(0)]],
    device const half4* g_in [[buffer(1)]],
    device const half4* b_in [[buffer(2)]],
    device half4*       out  [[buffer(3)]],
    constant GraphSizeParams& p [[buffer(4)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= p.width || gid.y >= p.height) { return; }
    size_t idx = gid.y * (size_t)p.width + gid.x;
    float r = float(r_in[idx].r);
    float g = float(g_in[idx].g);
    float b = float(b_in[idx].b);
    float a = float(r_in[idx].a);
    out[idx] = half4(float4(r, g, b, a));
}

// composition main epilogue 的通用形态：out.rgb = mix(a.rgb, b.rgb, mask.r*op)
kernel void graph_masked_mix(
    device const half4* a_in [[buffer(0)]],
    device const half4* b_in [[buffer(1)]],
    device const half4* mask [[buffer(2)]],
    device half4*       out  [[buffer(3)]],
    constant GraphSizeParams& p [[buffer(4)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= p.width || gid.y >= p.height) { return; }
    size_t idx = gid.y * (size_t)p.width + gid.x;
    float4 a = float4(a_in[idx]);
    float4 b = float4(b_in[idx]);
    float k = clamp(float(mask[idx].r), 0.0, 1.0) * p.a;
    out[idx] = half4(float4(mix3(a.rgb, b.rgb, k), a.a));
}

// —— GRADING_SNIPPET.apply_grading 逐条翻译（点态基础域；mid_detail/
//    sharpness 邻域位不在此 kernel——执行器 fail-closed 拒绝）——
static inline float3 graph_apply_grading(float3 color, constant GraphGradingParams& p) {
    // 1. Exposure
    float exposure = p.exposure;
    float exp_stops = (exposure / 100.0) * (exposure / 100.0) * 6.0;
    if (exposure < 0.0) { exp_stops = -exp_stops; }
    color *= pow(2.0, exp_stops);

    // 2. White Balance
    float3 wb = float3(1.0 + p.temp / 200.0, 1.0 - p.tint / 200.0,
                       1.0 - p.temp / 200.0);
    color *= wb;

    // 3. Contrast（pivot 0.18）
    if (p.contrast != 0.0) {
        float c_factor = 1.0 + p.contrast / 100.0;
        color = sign(color) * pow(abs(color) / 0.18, float3(c_factor)) * 0.18;
    }

    // 4. Lift / Gamma / Gain（wheel 已转 RGB 增益）
    float3 lift = float3(p.lift_x, p.lift_y, p.lift_z);
    float3 gamma = float3(p.gamma_x, p.gamma_y, p.gamma_z);
    float3 gain = float3(p.gain_x, p.gain_y, p.gain_z);
    float3 l = (lift - 1.0) * 0.2;
    color = color * gain * (1.0 - l) + l;

    float3 safe_gamma = max(gamma, float3(0.01));
    float3 gm = float3(pow(2.0, -log2(safe_gamma.r)),
                       pow(2.0, -log2(safe_gamma.g)),
                       pow(2.0, -log2(safe_gamma.b)));
    float3 res;
    res.x = (color.x > 0.0 && color.x <= 1.0) ? pow(color.x, gm.x)
            : (color.x > 1.0) ? (gm.x * (color.x - 1.0) + 1.0) : gm.x * color.x;
    res.y = (color.y > 0.0 && color.y <= 1.0) ? pow(color.y, gm.y)
            : (color.y > 1.0) ? (gm.y * (color.y - 1.0) + 1.0) : gm.y * color.y;
    res.z = (color.z > 0.0 && color.z <= 1.0) ? pow(color.z, gm.z)
            : (color.z > 1.0) ? (gm.z * (color.z - 1.0) + 1.0) : gm.z * color.z;
    color = res;

    // 安全修复：暗部负值软性保护（luma 压缩前计算，与 GLSL 同序）
    float luma_post_grading = dot(color, float3(0.299, 0.587, 0.114));
    if (luma_post_grading < 0.0) {
        for (int i = 0; i < 3; i++) {
            float x = color[i];
            if (x < -0.5) { x = -0.5 - (abs(x + 0.5) * 0.1); }
            color[i] = x;
        }
        float min_channel = min(min(color.r, color.g), color.b);
        float max_channel = max(max(color.r, color.g), color.b);
        if (max_channel - min_channel > 0.15 && min_channel < 0.0) {
            float balance_factor = clamp(abs(min_channel) * 0.25, 0.0, 0.15);
            color = mix(color, float3(luma_post_grading), balance_factor);
        }
    }

    // 5. Shadows（power 曲线）
    if (p.shadow != 0.0) {
        float shadow_factor = 1.0 + (p.shadow / 100.0);
        if (shadow_factor > 0.0) {
            float3 clamped_c = clamp(color, 0.0, 1.0);
            color = pow(clamped_c, float3(1.0 / shadow_factor))
                    + (color - clamped_c);
        }
    }

    // 6. Highlights（Reinhard）
    if (p.highlights != 0.0) {
        float max_c = max(max(color.r, color.g), color.b);
        if (max_c > 0.1) {
            float mapped_max = max_c;
            if (p.highlights < 0.0) {
                float k = abs(p.highlights / 100.0) * 2.0;
                mapped_max = 0.1 + (max_c - 0.1) / (1.0 + k * (max_c - 0.1));
            } else {
                float k = (p.highlights / 100.0) * 0.5;
                mapped_max = 0.1 + (max_c - 0.1) * (1.0 + k);
            }
            color *= (mapped_max / max_c);
        }
    }

    // 6.5 色调区间饱和度
    if (p.lift_saturation != 0.0 || p.gamma_saturation != 0.0
            || p.gain_saturation != 0.0) {
        float tonal_lum = clamp(dot(color, float3(0.299, 0.587, 0.114)),
                                0.0, 1.0);
        float s0 = smoothstep(0.0, 0.5, tonal_lum);
        float s1 = smoothstep(0.5, 1.0, tonal_lum);
        float tonal_shadow_w = 1.0 - s0;
        float tonal_mid_w = s0 * (1.0 - s1);
        float tonal_high_w = s1;
        float tonal_amount = p.lift_saturation * tonal_shadow_w
                           + p.gamma_saturation * tonal_mid_w
                           + p.gain_saturation * tonal_high_w;
        if (tonal_amount != 0.0) {
            float tonal_gray = dot(color, float3(0.299, 0.587, 0.114));
            color = mix(float3(tonal_gray), color, 1.0 + tonal_amount / 100.0);
        }
    }

    // 7. Vibrance
    if (p.vibrance != 0.0) {
        float gray = dot(color, float3(0.299, 0.587, 0.114));
        float max_c = max(color.r, max(color.g, color.b));
        float sat = max_c - gray;
        float amt = p.vibrance / 100.0;
        float boost = amt * (1.0 - clamp(sat, 0.0, 1.0));
        color = mix(color, float3(gray), -boost);
    }

    // 9. Hue Shift（符号防护）
    if (p.hue_shift != 0.0) {
        float3 sign_c = sign(color);
        float3 hsv = graph_rgb2hsv(abs(color));
        hsv.x = fract(hsv.x + p.hue_shift / 360.0);
        color = sign_c * graph_hsv2rgb(hsv);
    }

    // 10. Saturation
    if (p.saturation != 0.0) {
        float gray = dot(color, float3(0.299, 0.587, 0.114));
        color = mix(float3(gray), color, 1.0 + p.saturation / 100.0);
    }
    return color;
}

// —— CURVES_SNIPPET.apply_curves 逐条翻译（LUT 为 768 float 线性 buffer）——
static inline float graph_curves_channel(float x, int ch,
                                         device const float* curves) {
    float v0 = curves[0 * 3 + ch];
    float v1 = curves[1 * 3 + ch];
    float v254 = curves[254 * 3 + ch];
    float v255 = curves[255 * 3 + ch];
    float slope_low = (v1 - v0) * 255.0;
    float slope_high = (v255 - v254) * 255.0;
    if (x < 0.0) { return v0 + x * slope_low; }
    if (x > 1.0) { return v255 + (x - 1.0) * slope_high; }
    int idx = (int)clamp(x * 255.0 + 0.5, 0.0, 255.0);
    return curves[idx * 3 + ch];
}

static inline float graph_curves_guard(float x) {
    if (x < -0.5) { x = -0.5 - (abs(x + 0.5) * 0.1); }
    if (x > 2.0) { x = 2.0 + (x - 2.0) * 0.1; }
    return x;
}

static inline float3 graph_apply_curves(float3 color, device const float* curves) {
    float3 res;
    res.r = graph_curves_channel(color.r, 0, curves);
    res.g = graph_curves_channel(color.g, 1, curves);
    res.b = graph_curves_channel(color.b, 2, curves);
    res.r = graph_curves_guard(res.r);
    res.g = graph_curves_guard(res.g);
    res.b = graph_curves_guard(res.b);
    float min_channel = min(min(res.r, res.g), res.b);
    float max_channel = max(max(res.r, res.g), res.b);
    if (max_channel - min_channel > 0.15 && min_channel < 0.0) {
        float3 diff = res - float3(min_channel);
        float balance_factor = clamp(abs(min_channel) * 0.3, 0.0, 0.2);
        res = res - diff * balance_factor;
    }
    return res;
}

// Corrector 单 pass：grading → curves → epilogue（mix(input, graded, k*op)）
kernel void graph_grading(
    device const half4* src      [[buffer(0)]],
    device const half4* mask     [[buffer(1)]],
    device half4*       out      [[buffer(2)]],
    device const float* curves   [[buffer(3)]],   // 768 float（恒绑定）
    constant GraphGradingParams& p [[buffer(4)]],
    constant GraphSizeParams& s [[buffer(5)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= s.width || gid.y >= s.height) { return; }
    size_t idx = gid.y * (size_t)s.width + gid.x;
    float4 src4 = float4(src[idx]);
    float3 color = src4.rgb;

    color = graph_apply_grading(color, p);
    if (p.has_curves != 0u) {
        color = graph_apply_curves(color, curves);
    }

    float k = p.opacity;
    if (p.mask_mode == 1u) {
        k = clamp(float(mask[idx].r), 0.0, 1.0) * p.opacity;
    }
    out[idx] = half4(float4(mix3(src4.rgb, color, k), src4.a));
}
"""

# GraphSizeParams / GraphGradingParams 的 numpy 布局（4 字节字段紧排）
GRAPH_SIZE_PARAMS_DTYPE = np.dtype([
    ("width", "<u4"), ("height", "<u4"),
    ("a", "<f4"), ("b", "<i4"),
])

GRAPH_GRADING_PARAMS_DTYPE = np.dtype([
    ("exposure", "<f4"), ("temp", "<f4"), ("tint", "<f4"),
    ("contrast", "<f4"), ("saturation", "<f4"), ("vibrance", "<f4"),
    ("shadow", "<f4"), ("highlights", "<f4"), ("hue_shift", "<f4"),
    ("lift_saturation", "<f4"), ("gamma_saturation", "<f4"),
    ("gain_saturation", "<f4"),
    ("lift_x", "<f4"), ("lift_y", "<f4"), ("lift_z", "<f4"),
    ("gamma_x", "<f4"), ("gamma_y", "<f4"), ("gamma_z", "<f4"),
    ("gain_x", "<f4"), ("gain_y", "<f4"), ("gain_z", "<f4"),
    ("opacity", "<f4"),
    ("mask_mode", "<u4"), ("has_curves", "<u4"),
])


def make_graph_size_params(width: int, height: int, a: float = 0.0,
                           b: int = 0) -> np.ndarray:
    """打包 MSL ``GraphSizeParams``（通用 size/标量/枚举槽）。"""
    params = np.zeros(1, dtype=GRAPH_SIZE_PARAMS_DTYPE)
    params["width"] = int(width)
    params["height"] = int(height)
    params["a"] = float(a)
    params["b"] = int(b)
    return params


def make_graph_grading_params(
    *, exposure: float, temp: float, tint: float, contrast: float,
    saturation: float, vibrance: float, shadow: float, highlights: float,
    hue_shift: float, lift_saturation: float, gamma_saturation: float,
    gain_saturation: float, lift, gamma, gain, opacity: float,
    mask_mode: int, has_curves: bool,
) -> np.ndarray:
    """打包 MSL ``GraphGradingParams``（字段与 struct 逐一对齐）。"""
    scalars = [exposure, temp, tint, contrast, saturation, vibrance, shadow,
               highlights, hue_shift, lift_saturation, gamma_saturation,
               gain_saturation, *lift, *gamma, *gain, opacity]
    if not all(np.isfinite(float(v)) for v in scalars):
        raise ValueError(f"graph_grading: 参数含非有限值 {scalars!r}")
    params = np.zeros(1, dtype=GRAPH_GRADING_PARAMS_DTYPE)
    for name, value in zip(params.dtype.names[:12],
                           scalars[:12]):
        params[name] = float(value)
    params["lift_x"], params["lift_y"], params["lift_z"] = map(float, lift)
    params["gamma_x"], params["gamma_y"], params["gamma_z"] = map(float, gamma)
    params["gain_x"], params["gain_y"], params["gain_z"] = map(float, gain)
    params["opacity"] = float(opacity)
    params["mask_mode"] = int(mask_mode)
    params["has_curves"] = 1 if has_curves else 0
    return params


def curves_lut_to_flat_buffer(lut) -> np.ndarray:
    """(256, 1, 3)/(256, 3) LUT → kernel 侧 768 float 线性 buffer。"""
    arr = np.asarray(lut, dtype=np.float32).reshape(256, 3)
    return np.ascontiguousarray(arr.reshape(-1))
