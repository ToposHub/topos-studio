"""TRAW CFA 4 相位平面的 Metal debayer（Metal 迁移计划 7.2）。

:cfa_gpu_debayer.CfaGpuDebayer`（OpenGL）的 Metal 对应物，与
`cfa_cpu_debayer.debayer_cfa_planes`（CPU 参考）共用同一套语义：

    1. RGGB 相位网格双线性 debayer（四位点方向平均，边界 clamp）
    2. 码值域黑位减除（先平均后减，clamp 0）
    3. 12-bit：PWL-log12 逆曲线 → 线性 e- 域；16-bit：码值 / 65535 直接归一化
    4. WB gain（线性域乘法）+ EI/曝光（2^n）
    5. IDT gamut（camera-native → BT.709 linear，行主序 3x3；None = identity）
    6. clamp [0,1] → 查看伽马（0=linear / 1=gamma2.2 / 2=sRGB）
    7. 输出 RGBA16Float（与 GL 路径的 ``f2`` FBO 同精度）或 RGBA32Float

调用契约（单线程所有者，与 GL 版「只允许持锁的渲染线程调用」同约束）：

- 输入是 **CPU 平面**：4 个 R/Gr/Gb/B 行主序数组，numpy 形状（行, 列）必须
  恰为 ``(ceil(H/2), ceil(W/2))``（帧尺寸按元数据全尺寸声明，奇数尺寸用
  ceil；与 GL 版的校验同一布局），
  整数 dtype、取值 0..65535，总字节数不超过 ``max_upload_bytes``。任何不符
  都显式抛 ``ValueError``，不做静默截断或补齐。
- 输出是 **Metal 纹理**：``CfaMetalFrame.texture``，并由 ``resource``
  （后端中立的 `GpuPlaneResource`）描述，可直接登记进
  ``OutputSlotPool.register_slot(..., resource=...)`` 发布原生帧。行 0 是
  图像顶行（与 GL FBO 读回同序）。
- 提交是**异步**的：`debayer` 编码并提交后立即返回。纹理内容只在
  ``frame.completed`` 之后才对 CPU 或其它命令队列有效；同一命令队列上的
  后续 GPU 工作按提交序看到完整结果。完成回调只在驱动线程记录事实。
- 资源复用受完成事件约束：相位平面纹理、输出纹理和参数缓冲按环形槽复用，
  一个槽只在它上一帧 **已完成且无保留** 后才可改写，所以异步提交不会让
  第 N 帧的上传覆盖仍在被 GPU 读取的第 N-1 帧平面。跨帧持有纹理的一方
  （显式读回、监看）先 ``frame.retain()``，用完 ``frame.release()``。
- 无 Metal 设备时构造抛 ``MetalComputeError``；调用方回退现有 GL / CPU 路径。
  本模块不自动选择后端，只提供 `cfa_metal_debayer_enabled`（显式开关，
  默认关）。

显式读回（``readback``）是诊断 / 测试同步点，会阻塞所有者线程直到 GPU
完成；生产路径的静帧搬运走 ``explicit_readback_queue``。

关停：先 ``drain()``（等全部在途帧完成并等完成线程收尾）返回 ``True``，再
``release()``。带着在途帧退出进程会确定性 SIGILL——Metal 完成回调要拿
GIL，而解释器正在收尾；详见 `CfaMetalDebayer.drain` 的实测记录。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, Optional, Sequence, Tuple

import numpy as np

from src.shared.gpu.present_frame import (
    NATIVE_ORIGIN_TOP_LEFT,
    NativePresentDescriptor,
    PresentColorInfo,
)
from src.shared.media.gpu_frame import GpuPlaneResource
from src.shared.video.cfa_cpu_debayer import (
    TRAW_CURVE_FW_E,
    TRAW_CURVE_TOE_E,
    traw_log12_constants,
)

try:
    import objc  # pyobjc-core（macOS）
except ImportError:  # 非 macOS 宿主：只有纯逻辑（参数整备）可用
    objc = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


@contextmanager
def _autoreleasepool() -> Iterator[None]:
    """块结束时清空 autorelease 池（同 ``context.py`` 的 AUD-1402 约定）。"""
    if objc is None:
        yield
        return
    with objc.autorelease_pool():
        yield


#: 显式开关（默认关）：仅当值为 ``1/true/on/yes`` 且平台为 macOS 时启用。
ENV_CFA_METAL_DEBAYER = "TOPOS_CFA_METAL_DEBAYER"
_TRUTHY = frozenset({"1", "true", "on", "yes"})

#: 支持的位深：12 = PWL-log12（RAW Log 制作档），16 = linear（raw16-* 归档档）。
#: GL 版把 ``>= 16`` 当 linear、其余当 log，CPU 版把 ``<= 12`` 当 log；13–15
#: 两条现有路径的解释互不相同，这里显式拒绝而不选边。
SUPPORTED_BIT_DEPTHS = (12, 16)
#: 支持的查看伽马：0 = linear / 1 = gamma2.2 / 2 = sRGB。
SUPPORTED_GAMMA_MODES = (0, 1, 2)
#: 16-bit linear 归档档的满码（``transfer=linear`` 契约）。
LINEAR16_FULL_SCALE = 65535.0
#: 默认上传上限：4 张 8K（7680×4320）相位平面 ≈ 66 MB 的两倍。
DEFAULT_MAX_UPLOAD_BYTES = 128 << 20
#: 查看伽马 → 共享帧色彩四元组的 transfer 词。
_TRANSFER_BY_GAMMA = {0: "linear", 1: "gamma22", 2: "srgb"}
#: 输出格式名 → (MTLPixelFormat 常量名, numpy 元素类型)。
_OUTPUT_FORMATS: Dict[str, Tuple[str, type]] = {
    "rgba16float": ("MTLPixelFormatRGBA16Float", np.float16),
    "rgba32float": ("MTLPixelFormatRGBA32Float", np.float32),
}


def cfa_metal_debayer_enabled() -> bool:
    """Metal CFA debayer 是否被显式开启（默认关）。

    与 ``metal_preview_mode`` 同一约定：只读本模块的显式开关，不读图形后端
    选择——Metal 视口开启不等于 CFA 也走 Metal。
    """
    if sys.platform != "darwin":
        return False
    return os.getenv(ENV_CFA_METAL_DEBAYER, "").strip().lower() in _TRUTHY


class CfaMetalDebayerError(RuntimeError):
    """Metal CFA debayer 的设备、编译、提交或 GPU 完成错误（显式诊断）。"""


# 与 MSL 的 ``CfaDebayerParams`` 逐字段对齐：全部 float32 / int32，自然 4 字节
# 对齐，无填充（不放 float2/float3/float4，避免 MSL 的 8/16 字节对齐歧义）。
CFA_DEBAYER_PARAMS_DTYPE = np.dtype([
    ("out_w", "<f4"),
    ("out_h", "<f4"),
    ("wb_r", "<f4"),
    ("wb_g", "<f4"),
    ("wb_b", "<f4"),
    ("exposure_stops", "<f4"),
    ("black_level", "<f4"),
    ("fw_e", "<f4"),
    ("curve_a", "<f4"),
    ("curve_k", "<f4"),
    ("curve_c0", "<f4"),
    ("curve_s", "<f4"),
    ("m00", "<f4"), ("m01", "<f4"), ("m02", "<f4"),
    ("m10", "<f4"), ("m11", "<f4"), ("m12", "<f4"),
    ("m20", "<f4"), ("m21", "<f4"), ("m22", "<f4"),
    ("gamma_mode", "<i4"),
    ("log_curve", "<i4"),
    ("pad0", "<i4"),
    ("pad1", "<i4"),
])

CFA_DEBAYER_MSL = """
#include <metal_stdlib>
using namespace metal;

struct CfaDebayerParams {
    float out_w;          // 全尺寸 W（像素）
    float out_h;          // 全尺寸 H（像素）
    float wb_r;           // 线性域白平衡 gain
    float wb_g;
    float wb_b;
    float exposure_stops; // EI/曝光补偿（2^n）
    float black_level;    // 码值域黑位（fetch 并平均后减除，clamp 0）
    float fw_e;           // 归一化分母（12-bit: 满阱 FW）
    float curve_a;        // PWL-log12 常量（Python 同源注入）
    float curve_k;
    float curve_c0;
    float curve_s;
    float m00;            // IDT gamut，行主序（camera-native → 709 linear）
    float m01;
    float m02;
    float m10;
    float m11;
    float m12;
    float m20;
    float m21;
    float m22;
    int gamma_mode;       // 0=linear / 1=gamma2.2 / 2=sRGB
    int log_curve;        // 1 = PWL-log12 逆曲线（12-bit）/ 0 = 线性归一化（16-bit）
    int pad0;
    int pad1;
};

// shader fetch(tex, tc, size) 的 clamp 边界语义：越界坐标复制边界值。
inline uint cfa_fetch(texture2d<uint, access::read> plane, int2 tc, int2 size) {
    int2 c = clamp(tc, int2(0), size - int2(1));
    return plane.read(uint2(c)).r;
}

// PWL-log12 逆映射（topos_traw_format_plan §3.2）：c<A 走线性趾，否则 log 段；
// 结果 clamp 到 [0, FW]。
inline float traw_log_inverse(float c, constant CfaDebayerParams& p) {
    float x;
    if (c < p.curve_a) {
        x = (c - p.curve_c0) / p.curve_k;
    } else {
        x = p.curve_s * (exp2(c / p.curve_a) - 1.0);
    }
    return clamp(x, 0.0, p.fw_e);
}

kernel void cfa_debayer(
    texture2d<uint, access::read> plane_r  [[texture(0)]],
    texture2d<uint, access::read> plane_gr [[texture(1)]],
    texture2d<uint, access::read> plane_gb [[texture(2)]],
    texture2d<uint, access::read> plane_b  [[texture(3)]],
    texture2d<float, access::write> out_rgba [[texture(4)]],
    constant CfaDebayerParams& p           [[buffer(0)]],
    uint2 gid [[thread_position_in_grid]])
{
    int2 out_size = int2(int(p.out_w), int(p.out_h));
    if (any(int2(gid) >= out_size)) { return; }
    // 相位平面尺寸 = ceil(帧尺寸/2)，与上传校验同式；输出像素即传感器整数坐标。
    int2 psize = int2((float2(p.out_w, p.out_h) + 1.0) * 0.5);
    int2 pix = int2(gid);
    int i = pix.x >> 1;
    int j = pix.y >> 1;
    int ex = pix.x & 1;
    int ey = pix.y & 1;
    int2 tc = int2(i, j);

    // RGGB 相位网格（平面索引 (i,j) → 传感器 (2i+ex, 2j+ey)）：
    //   R[j,i]=(2i,2j)  Gr[j,i]=(2i+1,2j)  Gb[j,i]=(2i,2j+1)  B[j,i]=(2i+1,2j+1)
    float r;
    float g;
    float b;
    if (ex == 0 && ey == 0) {
        // R 位点：G = 右 Gr + 下 Gb；B = 4 对角平均
        r = float(cfa_fetch(plane_r, tc, psize));
        g = 0.5 * (float(cfa_fetch(plane_gr, tc, psize))
                   + float(cfa_fetch(plane_gb, tc, psize)));
        b = 0.25 * (float(cfa_fetch(plane_b, tc, psize))
                    + float(cfa_fetch(plane_b, tc - int2(1, 0), psize))
                    + float(cfa_fetch(plane_b, tc - int2(0, 1), psize))
                    + float(cfa_fetch(plane_b, tc - int2(1, 1), psize)));
    } else if (ex == 1 && ey == 0) {
        // Gr 位点：R = 左右平均；B = 上下平均
        r = 0.5 * (float(cfa_fetch(plane_r, tc, psize))
                   + float(cfa_fetch(plane_r, tc + int2(1, 0), psize)));
        g = float(cfa_fetch(plane_gr, tc, psize));
        b = 0.5 * (float(cfa_fetch(plane_b, tc, psize))
                   + float(cfa_fetch(plane_b, tc - int2(0, 1), psize)));
    } else if (ex == 0 && ey == 1) {
        // Gb 位点：R = 上下平均；B = 左右平均
        r = 0.5 * (float(cfa_fetch(plane_r, tc, psize))
                   + float(cfa_fetch(plane_r, tc + int2(0, 1), psize)));
        g = float(cfa_fetch(plane_gb, tc, psize));
        b = 0.5 * (float(cfa_fetch(plane_b, tc, psize))
                   + float(cfa_fetch(plane_b, tc - int2(1, 0), psize)));
    } else {
        // B 位点：G = 右上 Gr + 左 Gb；R = 4 对角平均
        b = float(cfa_fetch(plane_b, tc, psize));
        g = 0.5 * (float(cfa_fetch(plane_gr, tc, psize))
                   + float(cfa_fetch(plane_gb, tc, psize)));
        r = 0.25 * (float(cfa_fetch(plane_r, tc, psize))
                    + float(cfa_fetch(plane_r, tc + int2(1, 0), psize))
                    + float(cfa_fetch(plane_r, tc + int2(0, 1), psize))
                    + float(cfa_fetch(plane_r, tc + int2(1, 1), psize)));
    }

    // 码值域黑位减除（先平均后减，clamp 0——防负值进 log 段）
    r = max(r - p.black_level, 0.0);
    g = max(g - p.black_level, 0.0);
    b = max(b - p.black_level, 0.0);

    float3 lin;
    if (p.log_curve != 0) {
        // LOG0 逆曲线 → 线性 e- 域 → WB → 归一化（/满阱 FW）
        lin = float3(traw_log_inverse(r, p) * p.wb_r,
                     traw_log_inverse(g, p) * p.wb_g,
                     traw_log_inverse(b, p) * p.wb_b);
        lin *= exp2(p.exposure_stops) / p.fw_e;
    } else {
        // 16-bit linear：码值 = e- × 65535/FW，直接归一化，无逆曲线
        lin = float3(r, g, b) * (float3(p.wb_r, p.wb_g, p.wb_b) * (1.0 / 65535.0));
        lin *= exp2(p.exposure_stops);
    }

    // IDT gamut（变换在 clamp 前，允许暂时超界）。参数是行主序；MSL 的
    // float3x3(...) 构造函数按**列**接收分量，所以这里按列填入各行，
    // gamut * lin 才是 matrix · color（否则等于用了转置矩阵）。
    float3x3 gamut = float3x3(p.m00, p.m10, p.m20,
                              p.m01, p.m11, p.m21,
                              p.m02, p.m12, p.m22);
    lin = gamut * lin;
    lin = clamp(lin, 0.0, 1.0);

    // 查看伽马
    if (p.gamma_mode == 1) {
        lin = pow(lin, float3(1.0 / 2.2));
    } else if (p.gamma_mode == 2) {
        float3 hi = 1.055 * pow(max(lin, float3(0.0)), float3(1.0 / 2.4)) - 0.055;
        lin = mix(lin * 12.92, hi, step(0.0031308, lin));
    }
    out_rgba.write(float4(lin, 1.0), gid);
}
"""

_KERNEL_NAME = "cfa_debayer"
_PLANE_NAMES = ("r", "gr", "gb", "b")
# 关停哨兵：只写一个 uint 的空操作命令，用来在关停时排在全部帧之后。
_SENTINEL_NAME = "cfa_debayer_shutdown_sentinel"
_SENTINEL_MSL = """
#include <metal_stdlib>
using namespace metal;
kernel void cfa_debayer_shutdown_sentinel(
    device uint *out [[buffer(0)]], uint gid [[thread_position_in_grid]]) {
    out[gid] = gid;
}
"""


@dataclass(frozen=True)
class CfaDebayerParams:
    """debayer 渲染参数（与 GL / CPU 版同名参数同语义）。

    Attributes:
        bit_depth: 12 = PWL-log12 逆曲线；16 = 线性归一化。其它值显式拒绝。
        wb_gains: 线性域乘性 gain（R, G, B），非破坏参数，调整零重解码。
        exposure_stops: EI/曝光补偿（+1 = ×2 线性）。
        black_level: 码值域黑位（debayer 平均后减除、clamp 0；0 = 不减除）。
        gamma_mode: 查看伽马 0=linear / 1=gamma2.2 / 2=sRGB。默认 1 与
            GL 版 ``display_ootf=True`` 的折算一致（线性域直出在无查看
            变换的管线里不可判读）。
        gamut_matrix: IDT gamut，**行主序** 3x3（camera-native → BT.709
            linear，见 `camera_gamut_matrices.resolve_debayer_params` 的
            ``matrix``）；None = identity。形状不是 (3, 3) 显式拒绝——
            GL 版接受任意 9 元扁平序列（列主序），CPU 版静默忽略非 (3,3)
            输入，这里不沿用两种不一致的行为。
    """

    bit_depth: int = 12
    wb_gains: Tuple[float, float, float] = (1.0, 1.0, 1.0)
    exposure_stops: float = 0.0
    black_level: float = 0.0
    gamma_mode: int = 1
    gamut_matrix: Optional[np.ndarray] = None


def validated_gamut_matrix(gamut_matrix: Optional[Any]) -> np.ndarray:
    """行主序 3x3 → float64 ndarray；None → identity；其它形状显式拒绝。"""
    if gamut_matrix is None:
        return np.eye(3, dtype=np.float64)
    matrix = np.asarray(gamut_matrix, dtype=np.float64)
    if matrix.shape != (3, 3):
        raise ValueError(
            f"CFA gamut_matrix 必须是行主序 3x3，得到形状 {matrix.shape}"
        )
    if not np.isfinite(matrix).all():
        raise ValueError("CFA gamut_matrix 含非有限值")
    return matrix


def build_cfa_debayer_params(
    frame_width: int,
    frame_height: int,
    params: CfaDebayerParams,
) -> np.ndarray:
    """构造 kernel 参数（单元素结构化数组，字段与 MSL 结构逐一对齐）。

    位深、伽马、gain 个数、数值有限性不合法都显式抛 ``ValueError``。
    """
    bit_depth = int(params.bit_depth)
    if bit_depth not in SUPPORTED_BIT_DEPTHS:
        raise ValueError(
            f"CFA Metal debayer 不支持的位深 {params.bit_depth}"
            f"（支持: {', '.join(str(value) for value in SUPPORTED_BIT_DEPTHS)}）"
        )
    gamma_mode = int(params.gamma_mode)
    if gamma_mode not in SUPPORTED_GAMMA_MODES:
        raise ValueError(
            f"CFA gamma_mode={params.gamma_mode!r} 不合法"
            f"（支持: {', '.join(str(value) for value in SUPPORTED_GAMMA_MODES)}）"
        )
    if len(params.wb_gains) != 3:
        raise ValueError(f"CFA wb_gains 需要 3 个分量，得到 {len(params.wb_gains)}")
    wb = tuple(float(value) for value in params.wb_gains)
    exposure = float(params.exposure_stops)
    black_level = float(params.black_level)
    if not all(np.isfinite(value) for value in wb + (exposure, black_level)):
        raise ValueError(
            "CFA debayer 参数含非有限值（wb_gains / exposure_stops / black_level）"
        )
    matrix = validated_gamut_matrix(params.gamut_matrix)

    values = np.zeros(1, dtype=CFA_DEBAYER_PARAMS_DTYPE)
    values["out_w"] = np.float32(float(frame_width))
    values["out_h"] = np.float32(float(frame_height))
    values["wb_r"], values["wb_g"], values["wb_b"] = wb
    if bit_depth == 12:
        curve_a, curve_k, curve_c0 = traw_log12_constants()
        values["fw_e"] = np.float32(TRAW_CURVE_FW_E)
        values["curve_a"] = np.float32(curve_a)
        values["curve_k"] = np.float32(curve_k)
        values["curve_c0"] = np.float32(curve_c0)
        values["curve_s"] = np.float32(TRAW_CURVE_TOE_E)
        values["log_curve"] = 1
    else:
        values["fw_e"] = np.float32(LINEAR16_FULL_SCALE)
        values["log_curve"] = 0
    values["exposure_stops"] = np.float32(exposure)
    values["black_level"] = np.float32(black_level)
    for row in range(3):
        for column in range(3):
            values[f"m{row}{column}"] = np.float32(matrix[row, column])
    values["gamma_mode"] = gamma_mode
    return values


def prepare_cfa_planes(
    planes: Sequence[Any],
    frame_width: int,
    frame_height: int,
) -> Tuple[np.ndarray, ...]:
    """校验并整备 4 个相位平面为连续 uint16 数组（明确的有界上传）。

    每个平面是行主序图像数组，numpy 形状（行, 列）必须恰为
    ``(ceil(H/2), ceil(W/2))``——与 GL 版 ``pw=shape[1]``、``ph=shape[0]``
    对 ``(W/2, H/2)`` 的校验同一布局；整数 dtype、取值 0..65535。
    浮点平面、形状不符或越界值都显式抛 ``ValueError``（GL 版的
    ``ascontiguousarray(dtype=uint16)`` 会静默截断浮点，这里不沿用）。
    """
    if len(planes) != 4:
        raise ValueError(f"TRAW 需要 4 相位平面，得到 {len(planes)}")
    width = int(frame_width)
    height = int(frame_height)
    if width <= 0 or height <= 0:
        raise ValueError(f"CFA 帧尺寸必须为正，得到 {width}×{height}")
    # (行数, 列数)：奇数帧尺寸按 ceil（native pf=3 平面几何同为 ceil）。
    expected = ((height + 1) // 2, (width + 1) // 2)
    prepared: list[np.ndarray] = []
    for index, plane in enumerate(planes):
        array = np.asarray(plane)
        name = _PLANE_NAMES[index]
        if not np.issubdtype(array.dtype, np.integer):
            raise ValueError(
                f"CFA 相位平面 {name} 必须是整数 dtype，得到 {array.dtype}"
            )
        if array.shape != expected:
            raise ValueError(
                f"CFA 相位平面 {name} 形状（行, 列）{array.shape} 与帧 "
                f"{width}×{height} 不符（期望 {expected}）"
            )
        if array.size and (int(array.min()) < 0 or int(array.max()) > 65535):
            raise ValueError(f"CFA 相位平面 {name} 取值越界（需 0..65535）")
        prepared.append(np.ascontiguousarray(array, dtype=np.uint16))
    return tuple(prepared)


def frame_completion(frame: "CfaMetalFrame") -> Callable[[Any], None]:
    """只记录事实的完成回调（驱动线程执行，不跑用户代码）。"""

    def completed(command_buffer: Any) -> None:
        reported: Optional[str] = None
        try:
            error = command_buffer.error()
        except Exception as exc:  # pragma: no cover - defensive PyObjC edge
            error = exc
        if error is not None:
            reported = str(error)
        frame._settle(reported)

    return completed


class CfaMetalFrame:
    """一次 Metal debayer 的输出：纹理、共享帧描述与完成事件。"""

    def __init__(
        self,
        *,
        sequence: int,
        frame_index: int,
        width: int,
        height: int,
        texture: Any,
        resource_format: str,
        device_identity: str,
        color: PresentColorInfo,
        bit_depth: int,
    ) -> None:
        self.sequence = int(sequence)
        self.frame_index = int(frame_index)
        self.width = int(width)
        self.height = int(height)
        #: 源相位平面的位深档（12 = PWL-log12，16 = linear）。
        self.bit_depth = int(bit_depth)
        #: MTLTexture（行 0 = 图像顶行）。
        self.texture = texture
        #: 后端中立的平面描述，供 ``TextureLease.from_resource`` / 原生发布使用。
        self.resource = GpuPlaneResource(
            api="metal",
            native_handle=texture,
            resource_format=str(resource_format),
            width=int(width),
            height=int(height),
            ownership="renderer_owned",
            device_identity=str(device_identity),
        )
        #: 输出像素的色彩语义：display RGB、full range、BT.709。
        self.color = color
        self.error: Optional[str] = None
        self.submitted: bool = False
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._retains = 0

    # -- 完成事件 ---------------------------------------------------------

    @property
    def completed(self) -> bool:
        """GPU 是否已完成写入（完成回调到过）。"""
        return self._done.is_set()

    def wait(self, timeout_s: Optional[float] = None) -> bool:
        """阻塞所有者线程直到 GPU 完成；``False`` 表示超时。

        这是诊断 / 关停同步点，不得出现在逐帧热循环。
        """
        return self._done.wait(timeout_s)

    def _settle(self, error: Optional[str]) -> None:
        """完成回调调用（驱动线程或所有者线程的 poll）：只记录结果并置位。"""
        self.error = error
        self._done.set()

    # -- 保留（借用共享平面） ----------------------------------------------

    def retain(self) -> None:
        """跨帧持有纹理（显式读回、监看）；与 `release` 成对。"""
        with self._lock:
            self._retains += 1

    def release(self) -> None:
        """归还一次 `retain`；多于保留次数的调用不生效。"""
        with self._lock:
            if self._retains > 0:
                self._retains -= 1

    @property
    def retained(self) -> bool:
        """是否仍被某一方跨帧持有。"""
        with self._lock:
            return self._retains > 0

    @property
    def idle(self) -> bool:
        """已完成且无保留——它的环形槽可以被下一帧改写。"""
        return self._done.is_set() and not self.retained

    # -- 发布 ---------------------------------------------------------------

    def native_descriptor(self) -> NativePresentDescriptor:
        """把输出描述成可发布的原生帧（供 ``TextureLease.make_present_frame``）。"""
        return NativePresentDescriptor.from_resource(
            self.resource, origin=NATIVE_ORIGIN_TOP_LEFT, color=self.color,
        )


@dataclass
class _RingSlot:
    """一组可复用的上传纹理、输出纹理与参数缓冲。"""

    planes: list = None  # type: ignore[assignment]
    output: Any = None
    params: Any = None
    size: Tuple[int, int] = (0, 0)   # (W, H)
    frame: Optional[CfaMetalFrame] = None


class CfaMetalDebayer:
    """TRAW CFA Metal debayer（4×R16Uint 上传 + 双线性 + 逆曲线 + WB/EI）。

    单线程所有者：与 `CfaGpuDebayer` 同约束，只允许拥有 Metal 上下文的线程
    调用。``ring_size`` 组资源轮换，组内资源只在上一帧 ``idle`` 后复用。
    """

    def __init__(
        self,
        context: Any,
        *,
        output_format: str = "rgba16float",
        ring_size: int = 3,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    ) -> None:
        """
        Args:
            context: `MetalComputeContext`（无设备时由它的构造显式报错）。
            output_format: ``"rgba16float"``（默认，与 GL ``f2`` 同精度）或
                ``"rgba32float"``。
            ring_size: 资源环深度（>= 2：异步提交时至少留一组在途）。
            max_upload_bytes: 单帧 4 平面总字节上限（明确的有界上传）。
        """
        if output_format not in _OUTPUT_FORMATS:
            raise ValueError(
                f"CFA Metal debayer 不支持的输出格式 {output_format!r}"
                f"（支持: {', '.join(sorted(_OUTPUT_FORMATS))}）"
            )
        if int(ring_size) < 2:
            raise ValueError("ring_size 必须 >= 2（异步提交需要至少一组在途资源）")
        if int(max_upload_bytes) <= 0:
            raise ValueError("max_upload_bytes 必须为正")
        import Metal  # pyobjc-framework-Metal；缺失时由调用方回退 GL/CPU

        self._metal = Metal
        self._context = context
        self._output_format = str(output_format)
        self._max_upload_bytes = int(max_upload_bytes)
        self._kernel: Any = None
        self._ring = [_RingSlot() for _ in range(int(ring_size))]
        self._next_slot = 0
        self._sequence = 0
        self._sentinel: Any = None
        self._released = False

    # -- 查询 --------------------------------------------------------------

    @property
    def output_format(self) -> str:
        """输出格式名（后端中立格式词）。"""
        return self._output_format

    @property
    def max_upload_bytes(self) -> int:
        """单帧上传字节上限。"""
        return self._max_upload_bytes

    @property
    def in_flight(self) -> int:
        """尚未完成 GPU 写入的帧数。"""
        return sum(
            1 for slot in self._ring
            if slot.frame is not None and not slot.frame.completed
        )

    def stats(self) -> Dict[str, Any]:
        """诊断快照。"""
        return {
            "backend": "metal_cfa_debayer",
            "output_format": self._output_format,
            "ring_size": len(self._ring),
            "in_flight": self.in_flight,
            "max_upload_bytes": self._max_upload_bytes,
        }

    # -- 资源 --------------------------------------------------------------

    def _ensure_kernel(self) -> Any:
        if self._kernel is None:
            try:
                self._kernel = self._context.compile_kernel(_KERNEL_NAME, CFA_DEBAYER_MSL)
            except Exception as exc:
                raise CfaMetalDebayerError(f"CFA MSL 编译失败: {exc}") from exc
        return self._kernel

    def _ensure_slot_resources(self, slot: _RingSlot, width: int, height: int) -> None:
        key = (int(width), int(height))
        if slot.size == key and slot.output is not None and len(slot.planes) == 4:
            return
        # 重建：先把槽置空，任何一步失败都不会留下「尺寸相符但资源不全」的槽。
        slot.planes = []
        slot.output = None
        slot.params = None
        slot.size = (0, 0)
        metal = self._metal
        storage = (
            metal.MTLStorageModeShared if self._context.device.hasUnifiedMemory()
            else metal.MTLStorageModeManaged
        )
        with _autoreleasepool():
            for _ in range(4):
                descriptor = metal.MTLTextureDescriptor.texture2DDescriptorWithPixelFormat_width_height_mipmapped_(
                    metal.MTLPixelFormatR16Uint, (width + 1) // 2, (height + 1) // 2, False,
                )
                descriptor.setUsage_(metal.MTLTextureUsageShaderRead)
                descriptor.setStorageMode_(storage)
                texture = self._context.device.newTextureWithDescriptor_(descriptor)
                if texture is None:
                    raise CfaMetalDebayerError(
                        f"CFA 相位平面纹理创建失败（{(width + 1) // 2}x"
                        f"{(height + 1) // 2} R16Uint）"
                    )
                slot.planes.append(texture)
        slot.output = self._context.create_texture_2d(
            int(getattr(metal, _OUTPUT_FORMATS[self._output_format][0])),
            width, height, shader_write=True,
        )
        slot.params = self._context.create_buffer(CFA_DEBAYER_PARAMS_DTYPE.itemsize)
        slot.size = key

    def _upload(
        self,
        slot: _RingSlot,
        planes: Sequence[np.ndarray],
        width: int,
        height: int,
    ) -> None:
        metal = self._metal
        with _autoreleasepool():
            region = metal.MTLRegionMake2D(0, 0, int(width), int(height))
            for texture, plane in zip(slot.planes, planes):
                texture.replaceRegion_mipmapLevel_withBytes_bytesPerRow_(
                    region, 0, plane.tobytes(), int(plane.strides[0]),
                )

    def _free_slot(self) -> Optional[_RingSlot]:
        """从上次使用的下一组起，找第一组「无帧或上一帧 idle」的资源组。"""
        count = len(self._ring)
        for offset in range(count):
            index = (self._next_slot + offset) % count
            slot = self._ring[index]
            if slot.frame is None or slot.frame.idle:
                self._next_slot = (index + 1) % count
                return slot
        return None

    def _acquire_slot(self, timeout_s: float) -> _RingSlot:
        """取一个可改写的资源组：上一帧已完成且无保留。

        全部在途时在所有者线程上等最旧的未完成帧，直到超时——有界等待，
        不会无限阻塞。所有帧都已完成却仍被跨帧保留时立即报错：等待改变
        不了这个状态，说明调用方漏了 ``release()``。
        """
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            slot = self._free_slot()
            if slot is not None:
                return slot
            pending = [
                item.frame for item in self._ring
                if item.frame is not None and not item.frame.completed
            ]
            if not pending:
                raise CfaMetalDebayerError(
                    "CFA debayer 资源环的每一组都仍被跨帧保留；"
                    "持有方用完纹理后必须调用 CfaMetalFrame.release()"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                raise CfaMetalDebayerError(
                    f"CFA debayer 资源环在 {float(timeout_s):.3f}s 内没有空闲组"
                    f"（in_flight={len(pending)}）；GPU 完成事件未到"
                )
            oldest = min(pending, key=lambda frame: frame.sequence)
            oldest.wait(remaining)

    # -- 主入口 ------------------------------------------------------------

    def encode(
        self,
        planes: Sequence[Any],
        frame_width: int,
        frame_height: int,
        *,
        params: CfaDebayerParams,
        frame_index: int = -1,
        timeout_s: float = 2.0,
    ) -> CfaMetalFrame:
        """校验并上传 4 相位平面，把 debayer kernel 编码进当前命令缓冲。

        不提交。所有者随后用 `submit` 走上下文自己的提交，或者用自己的
        `MetalSubmissionScheduler.submit_reserved` 提交并把
        ``on_complete=lambda submission, error: frame._settle(error)`` 交给它。
        编码失败时整组丢弃（``discard_pending``），不把半编码的命令留给下一帧。
        """
        if self._released:
            raise CfaMetalDebayerError("CfaMetalDebayer 已释放")
        width = int(frame_width)
        height = int(frame_height)
        uploads = prepare_cfa_planes(planes, width, height)
        total = sum(int(plane.nbytes) for plane in uploads)
        if total > self._max_upload_bytes:
            raise ValueError(
                f"CFA 平面上传 {total} 字节超过上限 {self._max_upload_bytes}"
            )
        kernel = self._ensure_kernel()
        param_values = build_cfa_debayer_params(width, height, params)
        slot = self._acquire_slot(timeout_s)
        try:
            self._ensure_slot_resources(slot, width, height)
            self._upload(slot, uploads, (width + 1) // 2, (height + 1) // 2)
            self._context.write_buffer(slot.params, param_values)
            textures: Dict[int, Any] = {
                index: texture for index, texture in enumerate(slot.planes)
            }
            textures[4] = slot.output
            self._context.dispatch(
                kernel, buffers={0: slot.params}, textures=textures,
                threads=(width, height, 1),
            )
        except Exception:
            # 建资源 / 上传 / 编码任一步失败：把上下文里这一帧的命令整组丢弃
            # （含所有者在本帧早先编码的其它 kernel——帧事务以帧为单位失败，
            # 与 MetalFrameTransactionPool.abort 同语义），然后原样重抛。
            # 这里不吞错：清理之后一定 raise。
            self._context.discard_pending()
            raise
        self._sequence += 1
        bit_depth = int(params.bit_depth)
        frame = CfaMetalFrame(
            sequence=self._sequence,
            frame_index=frame_index,
            width=width,
            height=height,
            texture=slot.output,
            resource_format=self._output_format,
            device_identity=str(self._context.device_identity),
            color=PresentColorInfo(
                color_range="full",
                color_matrix="bt709",
                color_primaries="bt709",
                color_transfer=_TRANSFER_BY_GAMMA[int(params.gamma_mode)],
            ),
            bit_depth=bit_depth,
        )
        slot.frame = frame
        return frame

    def submit(self, frame: CfaMetalFrame, *, wait: bool = False) -> CfaMetalFrame:
        """提交已编码的帧（异步；``wait=True`` 仅诊断 / 测试用）。"""
        if frame.submitted:
            raise CfaMetalDebayerError(
                f"CFA 帧 {frame.sequence} 已提交过，不能重复提交"
            )
        try:
            command_buffer = self._context.submit(
                completion=frame_completion(frame), wait=wait,
            )
        except Exception as exc:
            raise CfaMetalDebayerError(f"CFA 命令缓冲提交失败: {exc}") from exc
        if command_buffer is None:
            raise CfaMetalDebayerError(
                "CFA debayer 没有待提交的命令：encode 之后、submit 之前命令被丢弃"
            )
        frame.submitted = True
        return frame

    def debayer(
        self,
        planes: Sequence[Any],
        frame_width: int,
        frame_height: int,
        *,
        params: CfaDebayerParams,
        frame_index: int = -1,
        timeout_s: float = 2.0,
    ) -> CfaMetalFrame:
        """上传 + 编码 + 异步提交，返回输出帧（``debayer`` 的便捷入口）。"""
        return self.submit(
            self.encode(
                planes, frame_width, frame_height,
                params=params, frame_index=frame_index, timeout_s=timeout_s,
            ),
        )

    def readback(self, frame: CfaMetalFrame, *, timeout_s: float = 5.0) -> np.ndarray:
        """同步读回输出纹理（所有者线程；诊断 / 显式读回同步点）。

        返回 ``(H, W, 4)``，dtype 随输出格式（float16 / float32），行 0 是
        图像顶行。阻塞所有者线程直到该帧 GPU 完成，并按显式读回计数。
        """
        from src.shared.video.explicit_readback_queue import note_explicit_readback
        from src.shared.video.pixel_copy_counters import CopyKind, add_pixel_copy

        dtype = _OUTPUT_FORMATS[self._output_format][1]
        if not frame.wait(timeout_s):
            raise CfaMetalDebayerError(
                f"CFA 帧 {frame.sequence} 在 {float(timeout_s):.3f}s 内未完成，拒绝读回"
            )
        if frame.error is not None:
            raise CfaMetalDebayerError(f"CFA 帧 {frame.sequence} GPU 执行失败: {frame.error}")
        image = self._context.read_texture(
            frame.texture,
            width=frame.width * 4,
            height=frame.height,
            dtype=dtype,
            region_width=frame.width,
        ).reshape(frame.height, frame.width, 4)
        add_pixel_copy(CopyKind.CPU_DOWNLOAD, int(image.nbytes))
        note_explicit_readback(
            purpose="explicit_snapshot", backend="metal", nbytes=int(image.nbytes),
        )
        return image

    def drain(self, *, timeout_s: float = 5.0) -> bool:
        """关停边界：等全部在途帧的 GPU 完成事件到齐，再等完成线程收尾。

        ``True`` 表示可以 ``release()`` 并让进程退出；``False`` 表示仍有帧
        未在 ``timeout_s`` 内完成（或哨兵命令失败）——保留资源、上报原因，
        不要退出解释器。

        实测依据（AMD RX 6900 XT / Intel macOS，2026-11-23）：带着在途帧
        退出进程会**确定性**触发 SIGILL（40/40），崩溃线程是 Metal 的
        ``com.Metal.CompletionQueueDispatch``——完成回调（及其 block 的释放）
        要拿 GIL，而 Python 正在收尾。只等各帧的完成事件后退出：早先一组
        5 次里崩过 1 次（崩溃栈正是完成线程释放回调 block 时取 GIL），之后
        130 次（其中 40 次在 CPU 超订下）未再复现——所以这个残余竞争存在，
        但没有复现办法。

        因此本方法在事件到齐后，再于**同一命令队列**上、排在全部帧之后提交
        一个空操作命令并同步等待（哨兵）：完成派发队列是串行的，且本机驱动
        上 ``waitUntilCompleted`` 返回时完成派发已经执行（1500 次无一例外）。
        这是有推理依据的缓解，**不是**已被实验证明能堵住上述低概率竞争的
        修复——那次竞争没有复现办法，所以哨兵与「只排空」在实验里无法区分
        （哨兵 50 次、其中 40 次在 CPU 超订下，全部干净）。
        """
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        for slot in self._ring:
            frame = slot.frame
            if frame is None or frame.completed:
                continue
            if not frame.wait(max(0.0, deadline - time.monotonic())):
                logger.warning(
                    "CfaMetalDebayer.drain: 帧 %s 未在 %.3fs 内完成；资源必须保留",
                    frame.sequence, float(timeout_s),
                )
                return False
        if self._sequence == 0:
            return True   # 从未提交过：不会再有完成回调
        return self._wait_completion_tail()

    def _wait_completion_tail(self) -> bool:
        """哨兵：同队列上排在全部帧之后的空操作命令，提交并同步等待。"""
        try:
            sentinel = self._context.compile_kernel(_SENTINEL_NAME, _SENTINEL_MSL)
            self._context.dispatch(
                sentinel, buffers={0: self._sentinel_buffer()}, threads=(1, 1, 1),
            )
            self._context.finish()
        except Exception:
            # 不吞错：返回 False 让调用方保留资源，并把原因写进日志。
            logger.warning(
                "CfaMetalDebayer.drain: 关停哨兵命令失败；GPU 可能仍有在途工作",
                exc_info=True,
            )
            return False
        return True

    def _sentinel_buffer(self) -> Any:
        if self._sentinel is None:
            self._sentinel = self._context.create_buffer(4)
        return self._sentinel

    def release(self) -> None:
        """释放全部资源引用（所有者线程；关停路径）。

        **先 ``drain()`` 返回 ``True`` 再调用。** 对 Metal 资源而言带在途帧
        调用是安全的（在途命令缓冲会保住它们引用的资源直到 GPU 完成），但
        对**进程**不安全：带着在途帧退出解释器会确定性崩溃（见 `drain`）。
        已交出去的 ``CfaMetalFrame`` 继续持有各自的纹理对象。
        """
        for slot in self._ring:
            slot.planes = []
            slot.output = None
            slot.params = None
            slot.size = (0, 0)
            slot.frame = None
        self._released = True


__all__ = [
    "CFA_DEBAYER_MSL",
    "CFA_DEBAYER_PARAMS_DTYPE",
    "CfaDebayerParams",
    "CfaMetalDebayer",
    "CfaMetalDebayerError",
    "CfaMetalFrame",
    "DEFAULT_MAX_UPLOAD_BYTES",
    "ENV_CFA_METAL_DEBAYER",
    "SUPPORTED_BIT_DEPTHS",
    "SUPPORTED_GAMMA_MODES",
    "build_cfa_debayer_params",
    "cfa_metal_debayer_enabled",
    "frame_completion",
    "prepare_cfa_planes",
    "validated_gamut_matrix",
]
