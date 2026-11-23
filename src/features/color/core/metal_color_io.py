"""阶段 4.4 —— 输入解码级与输出展示级的 Metal kernel（帧描述驱动）。

两个边界，展示变换**只在输出边界施加一次**：

**输入边界**（:meth:`MetalColorIO.decode_to_working`）：
``ColorFrameDescription`` 驱动的双平面 YUV（NV12 R8Unorm/RG8Unorm、
P010 R16Unorm/RG16Unorm）或 RGBA16F 源 → 工作空间 RGBA16Float：

1. YUV→RGB 码值域换算（矩阵/range 与共享 ``kernels_video`` 同一套
   ``build_yuv_convert_params`` 数学——码值域、**不 clamp**）；
2. 色度采样位置：``left``（NV12 解码默认）＝最近邻 ``gid>>1``（与既有
   GL NV12 路径逐位一致）；``center`` / ``top*`` / ``bottom*`` ＝双线性
   + 位置偏移（±0.25 色度 texel）；
3. transfer 解码（bt709 OETF 逆 / sRGB EOTF 逆 / PQ→nit 再按 SDR
   参考白归一 / HLG→场景线性 / 线性直通）——数学与
   ``color_transform_provider``（builtin provider，OCIO 不可用时的
   生产回退）**逐条同源**，测试冻结该等价；
4. 色域→工作色域（bt709）3×3 线性变换，同样取自 provider 矩阵；
5. HDR 线性值 **不 clamp**（>1 保留；负值保留给调色链路）；Alpha 直通
   （YUV 源 Alpha=1）。

**输出边界**（:meth:`MetalColorIO.encode_for_display`）：
工作空间线性 → 展示码值（``DisplayOutputDescription.kind``）：
``sdr_passthrough`` 恒等 / ``sdr_encode`` 线性→sRGB（硬剪，F5.1 契约）/
``hdr_sdr_tonemap`` ACES 近似 tonemap→sRGB / ``hdr_pq`` 线性(nit)→PQ /
``edr_linear`` 线性×headroom 直通（EDR 层）。

线程边界：无 Qt；与 ``MetalComputeContext`` 约定一致——单线程（渲染
worker）独占；本类只 dispatch（编码进当前 command buffer，不提交），
提交/等待由调用方控制（生产用 ``submit_async`` + completion；验证路径
才 ``finish()``）。
"""

from __future__ import annotations

from typing import Any, Tuple

import numpy as np

from src.features.color.core.frame_color_description import (
    ColorFrameDescription,
    DisplayOutputDescription,
    SDR_REFERENCE_NITS,
)
from src.shared.gpu.metal.context import MetalComputeContext
from src.shared.gpu.metal.kernels_video import build_yuv_convert_params

# ── 参数布局（Python dtype ↔ MSL struct 逐字段对齐，全 float32/int32） ──

#: 输入解码参数（YuvConvertParams 9 float + transfer/primaries 扩展）
DECODE_PARAMS_DTYPE = np.dtype([
    # ── kernels_video.YuvConvertParams（码值域换算） ──
    ("max_code", "<f4"),
    ("y_offset", "<f4"),
    ("y_scale", "<f4"),
    ("c_neutral", "<f4"),
    ("c_scale", "<f4"),
    ("cr_to_r", "<f4"),
    ("cb_to_g", "<f4"),
    ("cr_to_g", "<f4"),
    ("cb_to_b", "<f4"),
    # ── 4.4 扩展：色度采样 + transfer + 色域 ──
    ("chroma_off_x", "<f4"),       # 色度 texel 偏移（bilinear 时）
    ("chroma_off_y", "<f4"),
    ("chroma_bilinear", "<i4"),    # 1=bilinear（center/top*/bottom*）
    ("transfer_mode", "<i4"),      # 0 恒等 / 1 srgb / 2 bt709 / 3 pq / 4 hlg
    ("linear_scale", "<f4"),       # transfer 解码后的线性缩放（PQ: 100/10000）
    # src→bt709 行主序 3×3（标量数组——避开 MSL float3x3/float3 在
    # constant buffer 里的 16 字节对齐填充，布局与 numpy dtype 逐字节一致）
    ("primaries_m", "<f4", (9,)),
])

#: 展示编码参数
DISPLAY_PARAMS_DTYPE = np.dtype([
    ("mode", "<i4"),               # 0 恒等 / 1 srgb / 2 tonemap+srgb /
                                   # 3 pq / 4 edr 线性×scale
    ("target_peak", "<f4"),        # tonemap 参考白（nit）
    ("out_scale", "<f4"),          # edr headroom / pq 的 nit 缩放
    ("composite_background", "<i4"),
])

#: transfer_mode 编码（与 MSL kernel 的 switch 一致）
_TRANSFER_MODES = {
    "linear": 0, "scene": 0,
    "srgb": 1,
    "bt709": 2,
    "pq": 3,
    "hlg": 4,
}

#: 展示 mode 编码
_DISPLAY_MODES = {
    "sdr_passthrough": 0,
    "sdr_encode": 1,
    "hdr_sdr_tonemap": 2,
    "hdr_pq": 3,
    "edr_linear": 4,
}

#: 工作色彩模式：
#: ``signal`` —— 码值直通（SDR 视频主路径；既有 GL 预览语义——调色在
#:               编码信号上进行，transfer 解码恒等、色域变换恒等，
#:               NV12 SDR 与 GL 路径逐位一致）；
#: ``linear``  —— 线性工作空间（HDR 必需；SDR 需要线性调色时显式选择）。
WORKING_MODES = ("signal", "linear")


def default_working_mode(description: ColorFrameDescription) -> str:
    """帧描述 → 默认工作模式：SDR=signal（GL 对拍口径），HDR=linear。"""
    return "linear" if description.is_hdr else "signal"

# 色度采样位置 → (bilinear, dx, dy)（单位：色度 texel）
_CHROMA_SITING: dict = {
    "left":        (0, 0.0, 0.0),
    "unknown":     (0, 0.0, 0.0),   # 解码默认 = left（最近邻，GL 对拍口径）
    "center":      (1, 0.0, 0.0),
    "topleft":     (1, -0.25, -0.25),
    "topright":    (1, 0.25, -0.25),
    "bottomleft":  (1, -0.25, 0.25),
    "bottomright": (1, 0.25, 0.25),
}

# ── MSL（严格类型：数字带 .0；无循环边界问题；HDR 不 clamp） ──

_MSML_YUV_DECODE = """
#include <metal_stdlib>
using namespace metal;

// 与 DECODE_PARAMS_DTYPE 逐字段对齐（kernels_video.YuvConvertParams 前缀）
struct DecodeParams {
    float max_code;
    float y_offset;
    float y_scale;
    float c_neutral;
    float c_scale;
    float cr_to_r;
    float cb_to_g;
    float cr_to_g;
    float cb_to_b;
    float chroma_off_x;
    float chroma_off_y;
    int chroma_bilinear;
    int transfer_mode;
    float linear_scale;
    float primaries_m[9];   // 行主序：m[r*3+c]，out = row·v
};

float3 apply_primaries(float3 v, constant float* m) {
    // m 为行主序（m[r*3+c]，out_r = Σ m[r*3+c]·v_c）。MSL 构造参数是
    // 矩阵的「列」：行主序数据的第 r 行作为第 r 个构造参数 → mat*v
    // 恰为 m·v（方向已由 test_metal_color_io 的非对称矩阵往返冻结）。
    float3x3 mat = float3x3(float3(m[0], m[1], m[2]),
                            float3(m[3], m[4], m[5]),
                            float3(m[6], m[7], m[8]));
    return v * mat;
}

float3 decode_transfer(float3 code, int mode) {
    if (mode == 1) {
        // sRGB EOTF 逆（provider _srgb_to_linear_np 同式）
        float3 lin;
        for (int i = 0; i < 3; i++) {
            float c = code[i];
            lin[i] = c <= 0.0405 ? c / 12.92
                : pow((c + 0.055) / 1.055, 2.4);
        }
        return lin;
    }
    if (mode == 2) {
        // BT.709 OETF 逆（provider _bt709_to_linear_np 同式）
        float3 lin;
        for (int i = 0; i < 3; i++) {
            float c = code[i];
            lin[i] = c < 0.081 ? c / 4.5
                : pow(max(c + 0.099, 0.0) / 1.099, 1.0 / 0.45);
        }
        return lin;
    }
    if (mode == 3) {
        // PQ 逆 EOTF → nit（provider _pq_to_linear_np 同式，输入码值剪到 [0,1]）
        float3 lin;
        for (int i = 0; i < 3; i++) {
            float c = clamp(code[i], 0.0, 1.0);
            float v = pow(c, 1.0 / 78.84375);
            float num = max(v - 0.8359375, 0.0);
            float den = max(18.8515625 - 18.6875 * v, 1e-10);
            lin[i] = pow(num / den, 1.0 / 0.1593017578125) * 10000.0;
        }
        return lin;
    }
    if (mode == 4) {
        // HLG 逆 OETF → 相对场景线性（provider _hlg_to_linear_np 同式）
        float3 lin;
        for (int i = 0; i < 3; i++) {
            float e = clamp(code[i], 0.0, 1.0);
            float low = (e * e) / 3.0;
            float high = (exp((e - 0.55991073) / 0.17883277) + 0.28466892) / 12.0;
            lin[i] = e <= 0.5 ? low : high;
        }
        return lin;
    }
    return code;  // linear / scene：直通（负值与 >1 保留）
}

// 手工双线性（access::read + 4 texel；边界 clamp-to-edge）。
// MetalComputeContext.dispatch 不绑定 sampler——不用 sampler 对象，插值
// 语义完全自持（与硬件 bilinear 的 u=t-0.5 权重公式一致，可对拍）。
float2 sample_bilinear_rg(texture2d<float, access::read> tex,
                          float2 texel_coord, uint2 size) {
    float2 u = texel_coord - 0.5;
    int2 i0 = int2(floor(u));
    float2 f = u - float2(i0);
    int2 i1 = i0 + int2(1);
    int2 c0 = clamp(i0, int2(0), int2(size) - int2(1));
    int2 c1 = clamp(i1, int2(0), int2(size) - int2(1));
    float2 v00 = tex.read(uint2(c0)).rg;
    float2 v10 = tex.read(uint2(c1.x, c0.y)).rg;
    float2 v01 = tex.read(uint2(c0.x, c1.y)).rg;
    float2 v11 = tex.read(uint2(c1)).rg;
    float2 a = mix(v00, v10, f.x);
    float2 b = mix(v01, v11, f.x);
    return mix(a, b, f.y);
}

kernel void yuv_planes_to_working(
    texture2d<float, access::read> y_tex  [[texture(0)]],
    texture2d<float, access::read> uv_tex [[texture(1)]],
    texture2d<float, access::write> out_tex [[texture(2)]],
    constant DecodeParams& p               [[buffer(0)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= out_tex.get_width() || gid.y >= out_tex.get_height()) {
        return;
    }
    uint2 uv_size = uint2(uv_tex.get_width(), uv_tex.get_height());
    float2 chroma;
    if (p.chroma_bilinear != 0) {
        // 连续色度 texel 坐标 + 位置偏移（center/top*/bottom*；双线性）
        float2 t = (float2(gid) + 0.5) * 0.5
                 + float2(p.chroma_off_x, p.chroma_off_y);
        chroma = sample_bilinear_rg(uv_tex, t, uv_size);
    } else {
        // left/unknown：最近邻 co-sited（与 GL NV12 的 gid>>1 逐位一致）
        chroma = uv_tex.read(uint2(gid.x >> 1, gid.y >> 1)).rg;
    }
    float y_n = y_tex.read(gid).r;

    float y_code = y_n * p.max_code - p.y_offset;
    float cb = (chroma.x * p.max_code - p.c_neutral) * p.c_scale;
    float cr = (chroma.y * p.max_code - p.c_neutral) * p.c_scale;
    float yv = y_code * p.y_scale;

    float3 code = float3(
        yv + p.cr_to_r * cr,
        yv + p.cb_to_g * cb + p.cr_to_g * cr,
        yv + p.cb_to_b * cb);
    // 码值域不 clamp（负值/超白保留）；transfer 解码同样不 clamp
    float3 lin = decode_transfer(code, p.transfer_mode) * p.linear_scale;
    lin = apply_primaries(lin, p.primaries_m);
    out_tex.write(float4(lin, 1.0), gid);
}

kernel void rgba_to_working(
    texture2d<float, access::read> src_tex  [[texture(0)]],
    texture2d<float, access::write> out_tex [[texture(1)]],
    constant DecodeParams& p               [[buffer(0)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= out_tex.get_width() || gid.y >= out_tex.get_height()) {
        return;
    }
    float4 src = src_tex.read(gid);
    float3 lin = decode_transfer(src.rgb, p.transfer_mode) * p.linear_scale;
    lin = apply_primaries(lin, p.primaries_m);
    out_tex.write(float4(lin, src.a), gid);   // Alpha 直通，不 clamp
}
"""

_MSML_DISPLAY_ENCODE = """
#include <metal_stdlib>
using namespace metal;

struct DisplayParams {
    int mode;
    float target_peak;
    float out_scale;
    int composite_background;
};

float linear_to_srgb(float c) {
    // provider _linear_to_srgb_np 同式（负段按线性延伸，外层统一剪）
    float x = max(c, 0.0);
    return x <= 0.0031308 ? 12.92 * x : 1.055 * pow(x, 1.0 / 2.4) - 0.055;
}

float3 aces_tonemap(float3 rgb, float peak) {
    // provider _tonemap_aces_np 同式（Narkowicz 近似）
    float3 x = rgb / peak;
    float3 num = x * (2.51 * x + 0.03);
    float3 den = x * (2.43 * x + 0.59) + 0.14;
    return clamp(num / den, 0.0, 1.0);
}

float linear_to_pq(float lin_nits) {
    // provider _linear_to_pq_np 同式
    float v = clamp(lin_nits / 10000.0, 0.0, 1.0);
    float p = pow(v, 0.1593017578125);
    return pow((0.8359375 + 18.8515625 * p) / (1.0 + 18.6875 * p), 78.84375);
}

kernel void working_to_display(
    texture2d<float, access::read> src_tex  [[texture(0)]],
    texture2d<float, access::write> out_tex [[texture(1)]],
    constant DisplayParams& p               [[buffer(0)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= out_tex.get_width() || gid.y >= out_tex.get_height()) {
        return;
    }
    float4 src = src_tex.read(gid);
    float3 rgb = src.rgb;
    if (p.mode == 1) {
        // SDR：线性→sRGB，>1 硬剪（F5.1 语义；Alpha 直通）
        rgb = float3(linear_to_srgb(rgb.r), linear_to_srgb(rgb.g),
                     linear_to_srgb(rgb.b));
        rgb = clamp(rgb, 0.0, 1.0);
    } else if (p.mode == 2) {
        // HDR→SDR：ACES tonemap（参考白归一）→ sRGB
        float3 mapped = aces_tonemap(rgb, p.target_peak);
        rgb = float3(linear_to_srgb(mapped.r), linear_to_srgb(mapped.g),
                     linear_to_srgb(mapped.b));
    } else if (p.mode == 3) {
        // HDR 展示：线性(nit)→PQ 码值 [0,1]
        rgb = float3(linear_to_pq(rgb.r * p.out_scale),
                     linear_to_pq(rgb.g * p.out_scale),
                     linear_to_pq(rgb.b * p.out_scale));
    } else if (p.mode == 4) {
        // EDR 线性直通：×headroom（>1 交给系统合成器，不剪）
        rgb = rgb * p.out_scale;
    }
    // mode 0（sdr_passthrough）：恒等——编码信号直通（F5.1 三路语义表）
    if (p.composite_background != 0) {
        rgb = mix(float3(0.1), rgb, src.a);
        src.a = 1.0;
    }
    out_tex.write(float4(rgb, src.a), gid);
}
"""


def primaries_matrix_to_working(src_primaries: str) -> np.ndarray:
    """线性 src 色域 → 工作色域（bt709）的 3×3 行主序矩阵。

    与 ``color_transform_provider._apply_primaries_np`` 同一矩阵来源
    （provider 的 to709/from709 组合），未知色域恒等（不冒充转换）。
    """
    from src.shared.color.color_transform_provider import (
        _AP1_TO_BT709,
        _BT709_TO_AP1,
        _BT709_TO_BT2020,
        _BT709_TO_P3D65,
    )

    src = str(src_primaries)
    if src in ("bt709", "unknown"):
        return np.eye(3, dtype=np.float32)
    to709 = {
        "bt2020": np.linalg.inv(_BT709_TO_BT2020),
        "p3": np.linalg.inv(_BT709_TO_P3D65),
        "ap1": _AP1_TO_BT709,
        "acescg": _AP1_TO_BT709,
    }.get(src)
    if to709 is None:
        return np.eye(3, dtype=np.float32)
    return np.ascontiguousarray(to709, dtype=np.float32)


def build_decode_params(description: ColorFrameDescription,
                        working_mode: str = "signal") -> np.ndarray:
    """构造输入解码参数（单元素结构化数组，供 create_buffer_from_numpy）。

    ``working_mode``（见 :data:`WORKING_MODES`）：``signal`` 下 transfer
    解码与色域变换恒等（码值直通，SDR 视频 GL 对拍口径）；``linear``
    下按帧描述的 transfer/primaries 解码到线性工作空间。
    """
    if working_mode not in WORKING_MODES:
        raise ValueError(f"未知工作模式: {working_mode!r}"
                         f"（支持: {', '.join(WORKING_MODES)}）")
    params = np.zeros(1, dtype=DECODE_PARAMS_DTYPE)
    if description.is_planar_yuv:
        yuv = build_yuv_convert_params(
            bit_depth=description.bit_depth,
            color_range=description.yuv_range,
            color_matrix=description.yuv_matrix,
        )
        for name in ("max_code", "y_offset", "y_scale", "c_neutral",
                     "c_scale", "cr_to_r", "cb_to_g", "cr_to_g", "cb_to_b"):
            params[name] = float(yuv[name][0])
    bilinear, off_x, off_y = _CHROMA_SITING.get(
        description.chroma_location, (0, 0.0, 0.0))
    params["chroma_off_x"] = float(off_x)
    params["chroma_off_y"] = float(off_y)
    params["chroma_bilinear"] = 1 if bilinear else 0
    if working_mode == "linear":
        params["transfer_mode"] = int(_TRANSFER_MODES[description.transfer])
        # PQ 解码出 nit（0..10000）；工作空间按 SDR 参考白归一（1.0=100nit）
        params["linear_scale"] = (
            SDR_REFERENCE_NITS / 10000.0
            if description.transfer == "pq" else 1.0)
        params["primaries_m"] = primaries_matrix_to_working(
            description.primaries).reshape(9)
    else:
        params["transfer_mode"] = 0
        params["linear_scale"] = 1.0
        params["primaries_m"] = np.eye(3, dtype=np.float32).reshape(9)
    return params


def build_display_params(output: DisplayOutputDescription) -> np.ndarray:
    """构造展示编码参数。"""
    params = np.zeros(1, dtype=DISPLAY_PARAMS_DTYPE)
    params["mode"] = int(_DISPLAY_MODES[output.kind])
    params["target_peak"] = float(output.target_peak_nits)
    # PQ 编码前把工作空间值还原为 nit（×10000/100）；EDR 用 headroom
    params["out_scale"] = (
        10000.0 / SDR_REFERENCE_NITS if output.kind == "hdr_pq"
        else float(output.edr_headroom))
    return params


class MetalColorIO:
    """帧描述驱动的输入解码级 / 输出展示级（渲染线程独占）。"""

    def __init__(self, ctx: MetalComputeContext) -> None:
        import Metal  # noqa: F401 —— 像素格式常量（macOS only）

        self._metal = Metal
        self.ctx = ctx
        self._decode_kernels: dict[str, Any] = {}
        self._display_kernel: Any = None
        self._param_buffers: dict[bytes, Any] = {}

    # ── 像素格式助手 ──

    @property
    def rgba16f(self) -> Any:
        return self._metal.MTLPixelFormatRGBA16Float

    def yuv_plane_formats(self, description: ColorFrameDescription) -> Tuple[Any, Any]:
        """(Y 平面格式, UV 平面格式)：NV12=R8/RG8Unorm，P010=R16/RG16Unorm。"""
        metal = self._metal
        if description.pixel_format == "nv12":
            return metal.MTLPixelFormatR8Unorm, metal.MTLPixelFormatRG8Unorm
        if description.pixel_format == "p010":
            return metal.MTLPixelFormatR16Unorm, metal.MTLPixelFormatRG16Unorm
        raise ValueError(
            f"{description.pixel_format} 不是双平面 YUV 输入（无平面格式）")

    # ── 输入侧 ──

    def upload_yuv_planes(
        self, y_plane: np.ndarray, uv_plane: np.ndarray,
        description: ColorFrameDescription,
    ) -> Tuple[Any, Any]:
        """上传 Y/UV 平面（unorm 纹理；行 0 = 图像顶部）。

        ``y_plane``: (H, W) uint8/uint16；``uv_plane``: (H/2, W/2, 2)。
        P010 的 10-bit 码值必须已左对齐进 16-bit 容器（解码器语义）。
        """
        y_fmt, uv_fmt = self.yuv_plane_formats(description)
        height, width = y_plane.shape[:2]
        chroma_h, chroma_w = uv_plane.shape[:2]
        if (width, height) != (description.width, description.height):
            raise ValueError(
                f"Y 平面 {width}x{height} 与帧描述 "
                f"{description.width}x{description.height} 不符")
        if (chroma_w, chroma_h) != description.chroma_size:
            raise ValueError(
                f"UV 平面 {chroma_w}x{chroma_h} 与帧描述色度尺寸 "
                f"{description.chroma_size} 不符")
        expected_dtype = np.uint16 if description.bit_depth > 8 else np.uint8
        y_data = np.ascontiguousarray(y_plane, dtype=expected_dtype)
        uv_data = np.ascontiguousarray(uv_plane, dtype=expected_dtype)
        if uv_data.ndim != 3 or uv_data.shape[-1] != 2:
            raise ValueError(f"UV 平面需为 (H/2, W/2, 2)，实际 {uv_data.shape}")
        y_tex = self.ctx.create_texture_from_numpy(
            y_data, y_fmt, width, height)
        # RG 双通道：replaceRegion 按行字节上传交错数据
        uv_tex = self.ctx.create_texture_2d(uv_fmt, chroma_w, chroma_h)
        uv_tex.replaceRegion_mipmapLevel_withBytes_bytesPerRow_(
            self._metal.MTLRegionMake2D(0, 0, chroma_w, chroma_h), 0,
            uv_data.tobytes(), int(uv_data.strides[0]))
        return y_tex, uv_tex

    def upload_rgba16f(self, rgba: np.ndarray) -> Any:
        """上传 (H, W, 4) float16/32 源纹理（HDR 线性值按原值上传）。"""
        if rgba.ndim != 3 or rgba.shape[-1] != 4:
            raise ValueError(f"RGBA 源需为 (H, W, 4)，实际 {rgba.shape}")
        height, width = rgba.shape[:2]
        data = np.ascontiguousarray(rgba, dtype=np.float16)
        return self.ctx.create_texture_from_numpy(
            data, self.rgba16f, width, height)

    def _decode_kernel(self, name: str) -> Any:
        kernel = self._decode_kernels.get(name)
        if kernel is None:
            kernel = self.ctx.compile_kernel(name, _MSML_YUV_DECODE)
            self._decode_kernels[name] = kernel
        return kernel

    def _param_buffer(self, params: np.ndarray) -> Any:
        key = params.tobytes()
        cached = self._param_buffers.get(key)
        if cached is None:
            cached = self.ctx.create_buffer_from_numpy(params)
            if len(self._param_buffers) >= 32:
                self._param_buffers.pop(next(iter(self._param_buffers)))
            self._param_buffers[key] = cached
        return cached

    def decode_to_working(
        self, source: Any, description: ColorFrameDescription,
        *, y_tex: Any = None, uv_tex: Any = None,
        working_mode: str = "",
    ) -> Any:
        """输入 → 工作空间 RGBA16Float 纹理（不提交；输出行 0 = 顶部）。

        ``source``：RGBA 源纹理（rgba16f 输入），或平面已单独给出时为
        ``None``。``working_mode`` 缺省按
        :func:`default_working_mode`（SDR=signal / HDR=linear）。返回新
        纹理；调用方负责生命周期（生产路径进 present slot / 调色图
        执行器；验证路径用完即弃，Python 引用离场释放）。
        """
        mode = working_mode or default_working_mode(description)
        width, height = description.width, description.height
        out = self.ctx.create_texture_2d(
            self.rgba16f, width, height, shader_write=True)
        params = self._param_buffer(build_decode_params(description, mode))
        if description.is_planar_yuv:
            if y_tex is None or uv_tex is None:
                raise ValueError("双平面 YUV 输入需给出 y_tex/uv_tex")
            self.ctx.dispatch(
                self._decode_kernel("yuv_planes_to_working"),
                buffers={0: params},
                textures={0: y_tex, 1: uv_tex, 2: out},
                threads=(width, height, 1))
        else:
            if source is None:
                raise ValueError("rgba16f 输入需给出源纹理")
            self.ctx.dispatch(
                self._decode_kernel("rgba_to_working"),
                buffers={0: params},
                textures={0: source, 1: out},
                threads=(width, height, 1))
        return out

    # ── 输出侧（展示变换施加点唯一） ──

    def encode_for_display(
        self, working_tex: Any, output: DisplayOutputDescription,
        *, target: Any = None, composite_viewport_background: bool = False,
    ) -> Any:
        """工作空间线性 → 展示码值纹理（不提交）。

        ``target`` 缺省时新建 RGBA16Float 输出（SDR 码值也在 float 容器
        中，供 present pass 采样或导出编码）；显式传入 present slot 纹理
        时直接写入（EDR/HDR 路径需要 float 容器——BGRA8 会把 >1 剪掉）。
        """
        width, height = int(working_tex.width()), int(working_tex.height())
        if target is None:
            target = self.ctx.create_texture_2d(
                self.rgba16f, width, height, shader_write=True)
        if self._display_kernel is None:
            self._display_kernel = self.ctx.compile_kernel(
                "working_to_display", _MSML_DISPLAY_ENCODE)
        params = build_display_params(output)
        params["composite_background"] = int(composite_viewport_background)
        self.ctx.dispatch(
            self._display_kernel,
            buffers={0: self._param_buffer(params)},
            textures={0: working_tex, 1: target},
            threads=(width, height, 1))
        return target

    # ── 验证路径（仅测试/诊断；生产不读回） ──

    def read_rgba16f(self, texture: Any) -> np.ndarray:
        width, height = int(texture.width()), int(texture.height())
        raw = self.ctx.read_texture(
            texture, width * 4, height, dtype=np.float16, region_width=width)
        return raw.reshape(height, width, 4).astype(np.float32)

    def release(self) -> None:
        self._decode_kernels.clear()
        self._display_kernel = None
        self._param_buffers.clear()


# ── 合成 HDR 验证数据（真实 HDR 素材本机没有——已知线性值的构造器） ──

def synthesize_p010_from_linear(
    linear_rgb: np.ndarray, description: ColorFrameDescription,
) -> Tuple[np.ndarray, np.ndarray]:
    """已知线性值 → P010 平面（编码方向参考，验证 decode 用）。

    链路：工作空间线性（1.0=100nit）→ nit → 还原源色域（bt2020）→
    PQ 编码 → BT.2020nc limited-range YUV → 10-bit 码值左对齐 uint16。
    与 decode_to_working 严格互逆（PQ/YUV 量化误差除外）。
    """
    if description.pixel_format != "p010":
        raise ValueError("合成器仅支持 p010（HDR 验证路径）")
    from src.shared.color.color_transform_provider import (
        _linear_to_pq_np,
        _apply_primaries_np,
    )

    rgb = np.asarray(linear_rgb, dtype=np.float32) * (
        10000.0 / SDR_REFERENCE_NITS)
    # 工作色域（bt709）→ 源色域（bt2020）
    src_rgb = _apply_primaries_np(rgb, "bt709", description.primaries)
    # _linear_to_pq_np 内部按 /10000 归一后剪 [0,1]——此处传入 nit 值，
    # 不得预先 clip（否则 100nit 白会被当作 1nit）
    code = _linear_to_pq_np(src_rgb)[..., :3]
    return _rgb_to_planar_yuv(
        code, description.bit_depth, description.yuv_matrix,
        description.yuv_range)


def _rgb_to_planar_yuv(
    code_rgb: np.ndarray, bit_depth: int, matrix: str, color_range: str,
) -> Tuple[np.ndarray, np.ndarray]:
    """full-range RGB 码值 [0,1] → (Y, UV) 平面（10-bit 左对齐 uint16）。"""
    matrix_coeffs = {
        "bt709": ((0.2126, 0.7152, 0.0722),
                  (-0.114572, -0.385428, 0.5), (0.5, -0.454153, -0.045847)),
        "bt2020nc": ((0.2627, 0.6780, 0.0593),
                     (-0.139630, -0.360370, 0.5),
                     (0.5, -0.459786, -0.040214)),
    }.get(matrix)
    if matrix_coeffs is None:
        raise ValueError(f"合成器不支持矩阵 {matrix!r}")
    (ry, rg, rb), (by, bg, bb), (vy, vg, vb) = matrix_coeffs
    rgb = np.asarray(code_rgb, dtype=np.float64)
    y = rgb @ np.array([ry, rg, rb])
    cb = rgb @ np.array([by, bg, bb])
    cr = rgb @ np.array([vy, vg, vb])
    unit = float(1 << (bit_depth - 8))
    max_code = float((1 << bit_depth) - 1)
    if color_range == "limited":
        y_code = np.clip(np.round(y * 219.0 * unit + 16.0 * unit), 0.0, max_code)
        c_code = np.clip(np.round((cb + 0.5) * 224.0 * unit + 16.0 * unit),
                         0.0, max_code)
        cr_code = np.clip(np.round((cr + 0.5) * 224.0 * unit + 16.0 * unit),
                          0.0, max_code)
    else:
        y_code = np.clip(np.round(y * max_code), 0.0, max_code)
        c_code = np.clip(np.round((cb + 0.5) * max_code), 0.0, max_code)
        cr_code = np.clip(np.round((cr + 0.5) * max_code), 0.0, max_code)
    # 4:2:0 下采样（2x2 box——与 kernel 的最近邻上采样配对的保守构造）
    cb2 = (c_code[0::2, 0::2] + c_code[0::2, 1::2]
           + c_code[1::2, 0::2] + c_code[1::2, 1::2]) * 0.25
    cr2 = (cr_code[0::2, 0::2] + cr_code[0::2, 1::2]
           + cr_code[1::2, 0::2] + cr_code[1::2, 1::2]) * 0.25
    y_plane = np.round(y_code).astype(np.uint16) << (16 - bit_depth)
    uv_plane = np.zeros(cb2.shape + (2,), dtype=np.uint16)
    uv_plane[..., 0] = np.round(cb2).astype(np.uint16) << (16 - bit_depth)
    uv_plane[..., 1] = np.round(cr2).astype(np.uint16) << (16 - bit_depth)
    return y_plane, uv_plane


__all__ = [
    "DECODE_PARAMS_DTYPE",
    "DISPLAY_PARAMS_DTYPE",
    "MetalColorIO",
    "build_decode_params",
    "build_display_params",
    "primaries_matrix_to_working",
    "synthesize_p010_from_linear",
]
