"""视频零拷贝链路的 MSL YUV→RGBA 转换 kernel（计划 Phase 5 P5-04）。

输入是 CVMetalTextureCache 导入的 NV12/P010 双平面纹理（P5-01/02）：

- plane 0：Y，``R8Unorm``（NV12）或 ``R16Unorm``（P010），尺寸 = 全幅。
- plane 1：UV 交织，``RG8Unorm`` / ``RG16Unorm``，尺寸 = 半幅。
- 采样返回归一化 float（unorm 硬件完成 /255 或 /65535），P010 的 10-bit
  左对齐进 16-bit 容器，归一化后与 8-bit 共享同一套码值公式。

数学约定（与 ``yuv_to_rgba_reference`` 逐条一致，不得单侧修改）：

- 全部在码值域计算：``code = normalized * max_code``（max_code = 2^bd-1）。
- limited range：Y 偏移 ``16*unit``、量程 ``219*unit``；色度中性点
  ``2^(bd-1)``、半量程 ``112*unit``；``unit = 2^(bd-8)``（10-bit 即
  64/876/512/896）。full range：Y 无偏移、量程 ``max_code``，色度半量程
  ``2^(bd-1)``。
- 矩阵（Y∈[0,1]、Cb/Cr∈[-0.5,0.5]）：R = Y + 1.5748·Cr，
  G = Y − 0.1873·Cb − 0.4681·Cr，B = Y + 1.8556·Cb（BT.709）；
  BT.601 / BT.2020 系数见 ``_MATRIX_COEFFS``。
- 输出 RGBA16Float，**不 clamp**： HDR 超界值留给上游调色链路处理。

参考实现（numpy）与 kernel 由同一 ``build_yuv_convert_params`` 驱动，
参数结构逐字段对齐（全 float32，自然 4 字节对齐，无填充分歧）。
"""

from __future__ import annotations

import numpy as np

from src.shared.media.gpu_frame import normalize_color_matrix, normalize_color_range

METAL_YUV_TO_RGBA_MSL = """
#include <metal_stdlib>
using namespace metal;

struct YuvConvertParams {
    float max_code;   // 2^bd - 1
    float y_offset;   // limited: 16*unit；full: 0
    float y_scale;    // 1 / y_range
    float c_neutral;  // 2^(bd-1)
    float c_scale;    // 1 / (2*色度半量程)
    float cr_to_r;    // 矩阵系数（BT.709: +1.5748）
    float cb_to_g;    //            -0.1873
    float cr_to_g;    //            -0.4681
    float cb_to_b;    //            +1.8556
};

kernel void yuv_to_rgba(
    texture2d<float, access::read> y_tex   [[texture(0)]],
    texture2d<float, access::read> uv_tex  [[texture(1)]],
    texture2d<float, access::write> rgba   [[texture(2)]],
    constant YuvConvertParams& p           [[buffer(0)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= rgba.get_width() || gid.y >= rgba.get_height()) { return; }
    float y_n = y_tex.read(gid).r;
    uint2 uv_gid = uint2(gid.x >> 1, gid.y >> 1);
    float u_n = uv_tex.read(uv_gid).r;
    float v_n = uv_tex.read(uv_gid).g;

    float y_code = y_n * p.max_code - p.y_offset;
    float cb_code = u_n * p.max_code - p.c_neutral;
    float cr_code = v_n * p.max_code - p.c_neutral;
    float y = y_code * p.y_scale;
    float cb = cb_code * p.c_scale;
    float cr = cr_code * p.c_scale;

    float r = y + p.cr_to_r * cr;
    float g = y + p.cb_to_g * cb + p.cr_to_g * cr;
    float b = y + p.cb_to_b * cb;
    rgba.write(float4(r, g, b, 1.0), gid);
}
"""

# 与 METAL_YUV_TO_RGBA_MSL 的 YuvConvertParams 逐字段对齐（全 float32）
YUV_CONVERT_PARAMS_DTYPE = np.dtype([
    ("max_code", "<f4"),
    ("y_offset", "<f4"),
    ("y_scale", "<f4"),
    ("c_neutral", "<f4"),
    ("c_scale", "<f4"),
    ("cr_to_r", "<f4"),
    ("cb_to_g", "<f4"),
    ("cr_to_g", "<f4"),
    ("cb_to_b", "<f4"),
])

# Y∈[0,1]、Cb/Cr∈[-0.5,0.5] 约定下的 (cr_to_r, cb_to_g, cr_to_g, cb_to_b)。
# 键取 gpu_frame.normalize_color_matrix 的归一化词汇表。
_MATRIX_COEFFS = {
    "bt709": (1.5748, -0.1873, -0.4681, 1.8556),
    "smpte170m": (1.402, -0.344136, -0.714136, 1.772),
    "bt470bg": (1.402, -0.344136, -0.714136, 1.772),
    "bt2020nc": (1.4746, -0.164553, -0.571353, 1.8814),
    "bt2020": (1.4746, -0.164553, -0.571353, 1.8814),
}


def build_yuv_convert_params(
    bit_depth: int = 8,
    color_range: str = "limited",
    color_matrix: str = "bt709",
) -> np.ndarray:
    """构造 kernel 参数（单元素结构化数组，供 create_buffer_from_numpy）。

    未知矩阵/位深显式报错（.clinerules:11），不得静默取默认——静默默认
    会让错误的色彩解释伪装成"正常画面"。
    """
    bd = int(bit_depth)
    if bd not in (8, 10):
        # AUD-1510：只挡下限会让 11–16-bit 静默通过——max_code/c_neutral
        # 按错误位深生成，12-bit 源配 R16Unorm 的码值解释整体错位，
        # 画面色彩错误却看起来"正常"。与 docstring/错误消息声明的
        # "覆盖 8/10-bit"保持一致：显式支持集，其余拒绝。
        raise ValueError(
            f"yuv convert: 不支持的位深 {bit_depth}（Phase 5 覆盖 8/10-bit）"
        )
    matrix = normalize_color_matrix(color_matrix)
    if matrix not in _MATRIX_COEFFS:
        raise ValueError(
            f"yuv convert: 未支持的色彩矩阵 {color_matrix!r}"
            f"（支持: {', '.join(sorted(_MATRIX_COEFFS))}）"
        )
    cr_to_r, cb_to_g, cr_to_g, cb_to_b = _MATRIX_COEFFS[matrix]

    max_code = float((1 << bd) - 1)
    unit = float(1 << (bd - 8))
    c_neutral = float(1 << (bd - 1))
    range_is_limited = normalize_color_range(color_range) == "limited"
    if range_is_limited:
        y_offset, y_scale = 16.0 * unit, 1.0 / (219.0 * unit)
        c_scale = 1.0 / (2.0 * 112.0 * unit)
    else:
        y_offset, y_scale = 0.0, 1.0 / max_code
        c_scale = 1.0 / (2.0 * c_neutral)

    params = np.zeros(1, dtype=YUV_CONVERT_PARAMS_DTYPE)
    params["max_code"] = max_code
    params["y_offset"] = y_offset
    params["y_scale"] = y_scale
    params["c_neutral"] = c_neutral
    params["c_scale"] = c_scale
    params["cr_to_r"] = cr_to_r
    params["cb_to_g"] = cb_to_g
    params["cr_to_g"] = cr_to_g
    params["cb_to_b"] = cb_to_b
    return params


def yuv_to_rgba_reference(
    y_plane: np.ndarray,
    uv_plane: np.ndarray,
    bit_depth: int = 8,
    color_range: str = "limited",
    color_matrix: str = "bt709",
) -> np.ndarray:
    """numpy 参考：与 METAL_YUV_TO_RGBA_MSL 逐条一致（测试画质门禁用）。

    Args:
        y_plane: (H, W) float 归一化 [0,1]
        uv_plane: (H/2, W/2, 2) float 归一化 [0,1]（[..., 0]=Cb, [..., 1]=Cr）
    Returns:
        (H, W, 3) float32，不 clamp。
    """
    p = build_yuv_convert_params(bit_depth, color_range, color_matrix)
    h, w = y_plane.shape
    y_code = y_plane.astype(np.float64) * float(p["max_code"][0]) - float(p["y_offset"][0])
    # 半分辨率色度上采样（最近邻，与 kernel 的 gid>>1 寻址一致）
    u = uv_plane[..., 0].astype(np.float64)
    v = uv_plane[..., 1].astype(np.float64)
    u_full = np.repeat(np.repeat(u, 2, axis=0), 2, axis=1)[:h, :w]
    v_full = np.repeat(np.repeat(v, 2, axis=0), 2, axis=1)[:h, :w]
    cb = (u_full * float(p["max_code"][0]) - float(p["c_neutral"][0])) * float(p["c_scale"][0])
    cr = (v_full * float(p["max_code"][0]) - float(p["c_neutral"][0])) * float(p["c_scale"][0])
    y = y_code * float(p["y_scale"][0])

    r = y + float(p["cr_to_r"][0]) * cr
    g = y + float(p["cb_to_g"][0]) * cb + float(p["cr_to_g"][0]) * cr
    b = y + float(p["cb_to_b"][0]) * cb
    return np.stack([r, g, b], axis=-1).astype(np.float32)
