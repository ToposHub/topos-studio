"""
Edit Page GPU Preview Renderer — shared GL context render thread.

复用 Color 页面 AsyncRenderer 的共享上下文模式，在后台线程中：
1. 接收 GpuDecodedFrame（零拷贝 GPU 纹理）
2. 绑定外部纹理，YUV→RGB shader 渲染到 FBO
3. 多轨 GPU 合成（alpha blend）
4. 通知 UI 线程显示最终纹理

数据流:
  GpuDecodedFrame → _wrap_external_gpu_planes → YUV→RGB → Composite → frame_ready signal
"""

from __future__ import annotations

import logging
import math
import queue
import time
import threading
import os

import numpy as np
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional, Tuple, Any

from PyQt6.QtCore import QThread, pyqtSignal, QMutex, QTimer
from PyQt6.QtGui import QOpenGLContext, QOffscreenSurface

from src.features.edit.core.primary_grade_shader import PRIMARY_GRADE_FRAGMENT
from src.features.edit.core.playback_debug import playback_trace
from src.shared.gpu.present_frame import (
    GpuPresentFrame,
    OutputSlotPool,
)

from src.features.edit.core.blend_shader import (
    _BLEND_FRAGMENT_SHADER_SOURCE,
)
from src.features.edit.core.gpu_transition_shaders import (
    GPU_TRANSITION_FRAGMENT_SHADER_SOURCE,
    GPU_FULL_FRAME_TRANSITION_FRAGMENT_SHADER_SOURCE,
)
from src.shared.gpu.gl_backend import GLContextManager

logger = logging.getLogger(__name__)

_TRANSITION_FRAGMENT_SHADER_SOURCE = GPU_TRANSITION_FRAGMENT_SHADER_SOURCE
_FULL_FRAME_TRANSITION_FRAGMENT_SHADER_SOURCE = (
    GPU_FULL_FRAME_TRANSITION_FRAGMENT_SHADER_SOURCE
)

_DIP_FRAGMENT_SHADER_SOURCE = """
#version 330
uniform sampler2D u_tex;
uniform vec3 u_dip_color;
uniform float u_dip_factor;
in vec2 v_texcoord;
out vec4 f_color;
void main() {
    vec4 c = texture(u_tex, v_texcoord);
    f_color = vec4(
        c.rgb * (1.0 - u_dip_factor) + u_dip_color * u_dip_factor,
        c.a
    );
}
"""

_PREVIEW_PASS_VERTEX_SHADER_SOURCE = """
#version 330
in vec2 in_vert;
in vec2 in_texcoord;
out vec2 v_texcoord;
void main() {
    gl_Position = vec4(in_vert, 0.0, 1.0);
    v_texcoord = in_texcoord;
}
"""

def _blend_code_of(track_info: dict) -> int:
    """A1: track_info → GLSL u_blend_mode 编号（缺失/None → 0 = normal）。"""
    mode = track_info.get("blend_mode") if isinstance(track_info, dict) else None
    if mode is None:
        return 0
    return int(getattr(mode, "code", 0))


def _decoded_frame_color_tag(decoded_frame: Any, name: str,
                             default: str) -> str:
    """Read a color tag from a decoded frame without dropping metadata.

    ``DecodedFrame`` stores video color metadata in ``metadata``.  Some GPU
    frame wrappers expose the same fields directly, so prefer those when
    present and fall back to the canonical metadata object.  The timeline
    planar path previously used only ``getattr(decoded_frame, name, ...)``;
    that silently selected BT.709/limited for every CPU Topos frame and for
    any full-range or non-BT.709 source.
    """
    value = getattr(decoded_frame, name, None)
    if value in (None, ""):
        metadata = getattr(decoded_frame, "metadata", None)
        value = getattr(metadata, name, None) if metadata is not None else None
    return str(value or default).strip().lower()


# ================================================================
# GUI 线程 GL 栈深度计数（initializeGL/paintGL 在栈上时 >0）
# ================================================================
# QOpenGLWidget 在 paint 结束后保持 context current（Qt 的合成优化），
# 因此 "currentContext() 非 None" 不能作为"正在 GL 调用栈上"的判据——
# viewer 首帧绘制后它恒为真，会让延迟共享上下文初始化无限重派发，
# 重负载启动时 GPU 渲染线程等不到上下文直接退出，整条 GPU 呈现链路
# 静默退化为 CPU QImage 上传路径（paintGL 每帧 ~16ms 主线程阻塞）。
# ProgramViewer.initializeGL/paintGL 用 enter/exit 包裹，渲染线程据此
# 判断安全窗口。
_gui_gl_stack_depth: int = 0


def gui_gl_stack_depth() -> int:
    """GUI 线程当前 initializeGL/paintGL 调用栈深度（0=可安全操作上下文）。"""
    return _gui_gl_stack_depth


def enter_gui_gl_stack() -> None:
    """GUI 线程进入 initializeGL/paintGL 时调用。"""
    global _gui_gl_stack_depth
    _gui_gl_stack_depth += 1


def exit_gui_gl_stack() -> None:
    """GUI 线程离开 initializeGL/paintGL 时调用（与 enter 配对）。"""
    global _gui_gl_stack_depth
    _gui_gl_stack_depth = max(0, _gui_gl_stack_depth - 1)


# ================================================================
# Clip TransformParams → NDC mat3
# ================================================================
# TransformParams is now a pixel-space model matching composite's TransformNode:
#   translate_x/y : pixels (Y-down, positive = right/down)
#   scale_x/y     : 1.0 = 100%
#   rotation      : degrees, clockwise positive (Y-down convention)
#   pivot_mode    : "center" (image center) or "custom" (pivot_x/y)
#   pivot_x/y     : pixels (pivot_mode="custom" only)
# The YUV→track-FBO quad lives in NDC [-1,1] (Y up). We convert pixel-space
# translate/pivot to NDC using the native sequence canvas size, then compose a
# 2D affine mat3 so the quad is deformed before alpha-blending. The output FBO
# can be a smaller proxy target and is intentionally a separate dimension.
# Identity (no transform) → identity matrix (fast path).
IDENTITY_TRANSFORM_TUPLE = (0.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0)


def _transform_tuple_is_identity(t: tuple) -> bool:
    """True if the 8-tuple equals identity within tolerance."""
    if not t or len(t) != 8:
        return False
    # Compare numeric fields; pivot_mode (index 5) must be "center".
    try:
        if str(t[5]) != "center":
            return False
        nums = [float(t[i]) for i in (0, 1, 2, 3, 4, 6, 7)]
    except (TypeError, ValueError):
        return False
    id_nums = [0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 0.0]
    return all(abs(a - b) <= 1e-4 for a, b in zip(nums, id_nums))


def _transform_to_ndc_mat3_bytes(transform_tuple: tuple,
                                 tex_w: int = 1920, tex_h: int = 1080,
                                 track_fbo_y_flipped: bool = False,
                                 coordinate_width: Optional[int] = None,
                                 coordinate_height: Optional[int] = None) -> bytes:
    """Convert a pixel-space TransformParams 8-tuple to a column-major mat3.

    Args:
        transform_tuple: (translate_x, translate_y, scale_x, scale_y,
            rotation, pivot_mode, pivot_x, pivot_y). translate/pivot are in
            pixels (Y-down); scale/rotation are unitless.
        tex_w, tex_h: track FBO dimensions. These are retained as the default
            Transform coordinate space for callers that do not provide a
            separate canvas size.
        coordinate_width, coordinate_height: native Transform coordinate space
            in pixels. Preview FBOs can be downscaled, but translate/pivot
            remain expressed in the sequence canvas pixels; passing this size
            prevents downscaled previews from moving too far.
        track_fbo_y_flipped: When True, the result is rendered into an edit
            track FBO whose image Y orientation is flipped once more during
            final presentation. Conjugate the display-space matrix with that
            Y flip, so move and clockwise rotation retain the same direction
            as the composite Transform handles.

    The vertex shader applies this mat3 to a fullscreen NDC quad (Y-up,
    [-1,1]). Each quad vertex is an isotropic sub-sample of the image, so the
    quad already encodes the image's aspect ratio implicitly (one NDC-x unit
    = tex_w/2 px, one NDC-y unit = tex_h/2 px). A rotation/scale expressed in
    NDC therefore needs aspect-ratio cross terms (H/W and W/H) to match what
    composite's pixel-space ``to_ndc`` produces — otherwise non-square images
    rotate/scale distorted and the overlay handles drift from the image.

    Derivation (must equal composite ``compute_transform_handles``'s ``to_ndc``
    for every image-pixel point; see tests/edit/test_clip_transform_matrix_parity.py):
    A quad vertex (nx, ny) maps to native canvas pixels
    px = (nx+1)/2·W, py = (1-ny)/2·H.
    Solving composite's to_ndc (pivot-relative rotate+scale, then translate,
    Y flipped at the end) for (nx, ny) gives, with dx = W/2 - piv_x,
    dy = H/2 - piv_y, rad = radians(rotation), c = cos, s = sin:

        m00 = c·sx                 m01 = s·sy·(H/W)        m02 = (dx·(c·sx−1) − s·sy·dy + tx)·2/W
        m10 = −s·sx·(W/H)          m11 = c·sy             m12 = −(s·sx·dx + dy·(c·sy−1) + ty)·2/H

    mat3 layout (column-major, what GLSL expects):
        col0 = (m00, m10, 0)  col1 = (m01, m11, 0)  col2 = (m02, m12, 1)
    """
    import numpy as np

    if _transform_tuple_is_identity(transform_tuple):
        return np.array([1, 0, 0, 0, 1, 0, 0, 0, 1], dtype=np.float32).tobytes()

    tx, ty, sx, sy, rot_deg, pivot_mode, piv_x, piv_y = transform_tuple
    tx, ty, sx, sy, rot_deg = float(tx), float(ty), float(sx), float(sy), float(rot_deg)
    piv_x, piv_y = float(piv_x), float(piv_y)

    w = max(1, tex_w)
    h = max(1, tex_h)
    coordinate_w = max(1, coordinate_width or w)
    coordinate_h = max(1, coordinate_height or h)
    half_w = coordinate_w / 2.0
    half_h = coordinate_h / 2.0
    # Resolve pivot in pixel space (Y-down).
    if str(pivot_mode) == "custom":
        piv_px_x, piv_px_y = piv_x, piv_y
    else:  # "center"
        piv_px_x, piv_px_y = half_w, half_h
    # dx/dy: pivot offset from image centre (image-centred convention, like
    # composite's (piv - half) term). For "center" both are 0.
    dx = half_w - piv_px_x
    dy = half_h - piv_px_y

    rad = math.radians(rot_deg)
    cos_r = math.cos(rad)
    sin_r = math.sin(rad)

    aspect_x = coordinate_w / coordinate_h
    aspect_y = coordinate_h / coordinate_w

    m00 = cos_r * sx
    m01 = sin_r * sy * aspect_y          # H/W cross term
    m10 = -sin_r * sx * aspect_x         # W/H cross term
    m11 = cos_r * sy
    m02 = (dx * (cos_r * sx - 1.0) - sin_r * sy * dy + tx) * (2.0 / coordinate_w)
    m12 = -(sin_r * sx * dx + dy * (cos_r * sy - 1.0) + ty) * (2.0 / coordinate_h)

    # Track textures place image top at FBO bottom and are flipped exactly once
    # during the final composite/present pass.  Apply F·M·F here so that final
    # presentation is M, where M is the same Y-down transform used by
    # compute_transform_handles on the composite page.
    if track_fbo_y_flipped:
        m01 = -m01
        m10 = -m10
        m12 = -m12

    return np.array(
        [m00, m10, 0.0,  m01, m11, 0.0,  m02, m12, 1.0],
        dtype=np.float32,
    ).tobytes()


# Precomputed identity mat3 bytes (no per-frame allocation for the common case).
_IDENTITY_MAT3_BYTES = _transform_to_ndc_mat3_bytes(IDENTITY_TRANSFORM_TUPLE)


# ================================================================
# Render Task
# ================================================================

@dataclass
class EditRenderTask:
    """Single render task for the GPU preview renderer."""
    frame: int
    generation_id: int
    layers: list  # List of GpuDecodedFrame or (frame, track_info) tuples
    viewport_size: Tuple[int, int] = (1920, 1080)
    output_fbo_size: Tuple[int, int] = (1920, 1080)
    request_kind: str = ""
    scrub_request_token: int | None = None
    # Wall-clock deadline from the realtime playback engine.  A task that
    # misses this deadline while waiting in the GPU queue is no longer useful
    # to the current present tick and should release its sources immediately.
    deadline_ns: int = 0
    # Adjustment Clip layers (scoped post-composite pass). Each entry is the
    # T-P1-22 三元组 ``(ClipLayerInfo, effects_tuple, preview_grade)`` —
    # grade 为 ColorStack 参数快照（dict）或 None（与生产方
    # timeline_preview_renderer 一致；消费方对旧二元组兼容按 None 处理）；
    # the layer also carries the sampled Transform/Opacity values used on the
    # accumulated lower result. Empty when no adjustment clips cover this frame.
    adjustment_layers: list = None  # type: ignore[assignment]
    # Native sequence canvas used by pixel-space TransformParams. This is
    # intentionally separate from output_fbo_size, which may be a proxy-sized
    # preview target.
    coordinate_size: Optional[Tuple[int, int]] = None
    # Idle-prebuffer task: composite into the private background FBO, capture
    # a CPU snapshot for the frame cache, and never touch the present pool or
    # emit frame_ready (no viewer presentation).
    background: bool = False
    # P3.2: composite-texture cache hit. layers 为空；GL 线程把缓存纹理 blit
    # 进 present slot，跳过解码与合成。
    cached_composite: bool = False
    # P3.2: 合成完成后把结果存入复合纹理缓存（暂停/拖拽/后台任务；
    # 实时播放不存，避免顺序流冲刷 LRU）。
    cacheable: bool = False
    # P0-PREV-GPU-T：双输入转场任务。每项 = (TransitionBlendInfo, spec|None)：
    # - spec 为 pixel-free 控制块，GL 线程在合成前计算几何与源侧效果；
    # - spec.kind=4：全画面 GPU dissolve；
    # - spec 为 None 且类型为 dip_to_color：合成域内做颜色后处理；
    # - cross_dissolve 不产生任务（仅 worker 侧 opacity 重写）。
    # worker 已保证：存在任何 GPU 不支持的转场时整帧回退 CPU，不会带着
    # 不完整的任务进来。
    transition_jobs: tuple = ()

    def __post_init__(self) -> None:
        if self.adjustment_layers is None:
            self.adjustment_layers = []


# ================================================================
# Planar Format Classifier
# ================================================================

# 2-plane formats: Y + interleaved UV
_TWO_PLANE_FORMATS = frozenset({
    'nv12', 'nv21', 'nv16', 'nv24',       # 8-bit
    'p010', 'p010le', 'p016', 'p016le',     # 10/16-bit
    'nv12m', 'nv21m',                       # multi-plane variant
})

# 3-plane formats by chroma subsampling
# 注：yuva*（4 平面）也归入对应子采样类——分类只决定 YUV 上传路由，
# Alpha 由调用方按 len(planes)>=4 单独处理（P7-Alpha）。
_YUV444_FORMATS = frozenset({
    'yuv444p', 'yuvj444p', 'yuva444p',     # 8-bit
    'yuv444p10le', 'yuv444p10be',
    'yuva444p10le', 'yuva444p12le', 'yuva444p16le',
    'yuv444p12le', 'yuv444p12be',
    'yuv444p16le', 'yuv444p16be',
    'yuv444p9le', 'yuv444p9be',
    # R4.2：Topos 4:4:4（10/12-bit × Alpha 位深 8/10/12/16）
    'topos_yuva444p10a8', 'topos_yuva444p10a10',
    'topos_yuva444p10a12', 'topos_yuva444p10a16',
    'topos_yuva444p12a8', 'topos_yuva444p12a10',
    'topos_yuva444p12a12', 'topos_yuva444p12a16',
})

_YUV422_FORMATS = frozenset({
    'yuv422p', 'yuvj422p', 'yuya422p', 'uyva422p',
    'yuva422p',                              # 8-bit YUVA planar
    'yuv422p10le', 'yuv422p10be',
    'yuva422p10le', 'yuva422p12le', 'yuva422p16le',
    'yuv422p12le', 'yuv422p12be',
    'yuv422p16le', 'yuv422p16be',
    'yuv422p9le', 'yuv422p9be',
    # Topos Video Codec（自研）：YUV 4:2:2 10/12-bit（v1.2）+ 独立位深 Alpha
    'topos_yuva422p10a8', 'topos_yuva422p10a10',
    'topos_yuva422p10a12', 'topos_yuva422p10a16',
    'topos_yuva422p12a8', 'topos_yuva422p12a10',
    'topos_yuva422p12a12', 'topos_yuva422p12a16',
})

_YUV420_FORMATS = frozenset({
    'yuv420p', 'yuvj420p', 'yuva420p',
    'yuva420p10le', 'yuva420p12le', 'yuva420p16le',
    'yuv420p10le', 'yuv420p10be',
    'yuv420p12le', 'yuv420p12be',
    'yuv420p16le', 'yuv420p16be',
    'yuv420p9le', 'yuv420p9be',
})

_YUV440_FORMATS = frozenset({
    'yuv440p', 'yuvj440p',
    'yuv440p10le', 'yuv440p12le', 'yuv440p16le',
})

_YUV411_FORMATS = frozenset({
    'yuv411p',
})

_RGB_PLANAR_FORMATS = frozenset({
    'gbrp', 'gbrap',
    'gbrp10le', 'gbrp10be',
    'gbrp12le', 'gbrp12be',
    'gbrp16le', 'gbrp16be',
    'gbrap10le', 'gbrap12le', 'gbrap16le',
    # M1-F4：浮点样本域（Topos Image HALF，float16 线性值平面）
    'gbrph16le',
    # R4.3：Topos GBR（10/12-bit × Alpha 位深 8/10/12/16；平面序同 gbrp）
    'topos_gbrap10a8', 'topos_gbrap10a10',
    'topos_gbrap10a12', 'topos_gbrap10a16',
    'topos_gbrap12a8', 'topos_gbrap12a10',
    'topos_gbrap12a12', 'topos_gbrap12a16',
})

# AUD-16603：调整剪辑 grade scratch 的容量上限（稳定键 → 纹理，按需惰性
# 分配 + FIFO 淘汰）。scoped 合成逐条消费 grade 输出，2 张足够交替；绝不
# 再用 1000+ 的槽位索引预留轨槽数组（旧实现会顺序创建 1001 张全尺寸纹理，
# 1080p 画布约 8.3 GB 显存）。
_ADJUSTMENT_GRADE_SCRATCH_CAP = 2

# AUD-16603：轨槽式 grade 目标的索引护栏——超过该轨道数的 grade 请求直接
# 跳过（返回源纹理），防止畸形 track_index 触发无界纹理分配。
_MAX_GRADED_TRACK_TARGETS = 64

# AUD-16602：退役输出池的强制释放兜底时限（秒）。正常路径等 Viewer ack
# 全部回收后释放；超时未回收（Viewer 关闭/卡死）由 glFinish 后强释放，
# 防退役纹理无限滞留。
_RETIRED_OUTPUT_POOL_TIMEOUT_S = 2.0


def _classify_planar_format(pixel_format: str, num_planes: int, bit_depth: int) -> str:
    """
    Classify a planar pixel format into an upload mode.

    Handles all professional YUV/RGB planar formats:
    - NV12/P010/P016 (2-plane, Y + interleaved UV)
    - YUV444/422/420/440/411 (3-plane, separate Y, U, V)
    - GBRP (3-plane RGB, used by ProRes 4444 and some lossless codecs)
    - xyz12le (DCI cinema XYZ color space)

    Args:
        pixel_format: FFmpeg pixel format string (e.g. 'yuv422p10le')
        num_planes: Number of planes in the decoded frame
        bit_depth: Bits per component

    Returns:
        Upload mode string: 'nv12'|'p010'|'p016'|'yuv444'|'yuv422'|'yuv420'|
        'yuv440'|'yuv411'|'rgb_planar'|'unknown'
    """
    pf = pixel_format.lower().strip()

    # TRAW CFA（批 2/批 4）：4 相位平面 → GPU debayer 专用分支
    if pf in ('topos_cfa12le', 'topos_cfa16le'):
        return 'cfa'

    # Direct set lookup first (exact match)
    if pf in _TWO_PLANE_FORMATS:
        if pf in ('p016', 'p016le'):
            return 'p016'
        if pf in ('p010', 'p010le'):
            return 'p010'
        return 'nv12'
    if pf in _YUV444_FORMATS:
        return 'yuv444'
    if pf in _YUV422_FORMATS:
        return 'yuv422'
    if pf in _YUV420_FORMATS:
        return 'yuv420'
    if pf in _YUV440_FORMATS:
        return 'yuv440'
    if pf in _YUV411_FORMATS:
        return 'yuv411'
    if pf in _RGB_PLANAR_FORMATS:
        return 'rgb_planar'

    # DCI cinema XYZ (very rare, treat as YUV444)
    if pf == 'xyz12le' or pf == 'xyz12be':
        return 'yuv444'

    # Fallback: substring matching for unknown variants
    if pf.startswith('nv') or pf.startswith('p01'):
        if num_planes == 2:
            return 'p010' if bit_depth > 8 else 'nv12'
    if 'gbrp' in pf:
        return 'rgb_planar'

    # Substring matching for YUV subsampling (check 444 before 422 before 420)
    if '444' in pf:
        return 'yuv444'
    if '422' in pf:
        return 'yuv422'
    if '420' in pf:
        return 'yuv420'
    if '440' in pf:
        return 'yuv440'
    if '411' in pf:
        return 'yuv411'
    if 'nv12' in pf or 'nv21' in pf:
        return 'nv12'

    return 'unknown'


# ================================================================
# Preview-cost degradation
# ================================================================

# FX-M04: Preview degradation is now driven by the declarative policy engine
# in src.features.shared.effects.preview_policy. The old ad-hoc
# _PREVIEW_PARAM_CAPS dict has been superseded by DegradationDecl registrations.
# This thin wrapper preserves the call signature used in the render loops.
from src.features.shared.effects.preview_policy import (
    DegradationMode,
    PreviewPolicyDecision,
    PreviewPolicyEngine,
    PolicyContext,
    get_degradation_decl,
    resolve_params_for_decision,
)

_preview_policy_engine = PreviewPolicyEngine()


def _degrade_params_for_preview(spec, params: dict) -> dict:
    """Return a copy of ``params`` with costly values reduced (FX-M04).

    Delegates to the declarative DegradationDecl registry. Effects without
    a declaration (or with only FULL mode) pass through untouched.
    """
    decl = get_degradation_decl(spec.effect_type)
    if DegradationMode.PARAMETER_CAP not in decl.supported_modes:
        return params
    if not decl.param_caps:
        return params
    degraded = dict(params)
    any_capped = False
    for name, cap in decl.param_caps.items():
        if name in degraded:
            try:
                val = float(degraded[name])
                if val > cap:
                    degraded[name] = cap
                    any_capped = True
            except (TypeError, ValueError):
                continue
    return degraded if any_capped else params


# ================================================================
# ExternalTextureProxy
# ================================================================

class _ExternalTextureProxy:
    """Minimal moderngl.Texture-like wrapper around a foreign glo.

    The effect runner returns the glo of a texture it owns (its scratch
    pool), and the rest of this renderer treats track textures as
    moderngl.Texture objects with ``.glo`` / ``.use(unit)`` / ``.size``.
    Rather than wrap the glo in a real moderngl.Texture (which would take
    ownership and try to release it on GC, corrupting the runner's pool),
    this proxy exposes exactly the three attributes the renderer uses and
    forwards ``.use()`` to a raw ``glBindTexture`` on the caller's context
    (the same context the runner compiled on — they are the same thread).

    Lifetime: the underlying glo is valid only until the next
    ``EffectPassRunner.run()`` on the same context (the scratch may be
    reused). Callers consume the proxy immediately within the same frame.
    """

    __slots__ = ("_glo", "_size")

    def __init__(self, glo: int, size: Optional[tuple] = None) -> None:
        self._glo = glo
        self._size = size

    @property
    def glo(self) -> int:
        return self._glo

    @property
    def size(self) -> tuple:
        # ``size`` is informational; the renderer only reads it for
        # rebuild checks. The proxy is short-lived (one frame), so a stale
        # size on the very first frame has no observable effect.
        return self._size or (0, 0)

    def use(self, location: int = 0) -> None:
        """Bind the foreign glo to a texture unit.

        Mirrors ``EffectPassRunner._bind_external_texture`` but honours
        the requested unit (multi-track compositing binds base/overlay to
        different units).
        """
        from PyQt6.QtOpenGL import QOpenGLFunctions_4_1_Core
        gl = QOpenGLFunctions_4_1_Core()
        if gl.initializeOpenGLFunctions():
            # GL_TEXTURE0 = 0x84C0; location is 0-based unit index.
            gl.glActiveTexture(0x84C0 + location)
            gl.glBindTexture(0x0DE1, self._glo)  # GL_TEXTURE_2D


# ================================================================
# EditGpuPreviewRenderer
# ================================================================

class EditGpuPreviewRenderer(QThread):
    """
    Background render thread for edit page GPU-accelerated preview.

    Shares GL context with ProgramViewer's QOpenGLWidget.
    Receives GpuDecodedFrame from EditGpuDecodeManager,
    wraps external GPU texture planes, renders YUV→RGB with shaders,
    composites multiple tracks, and signals the UI thread with the
    final texture ID.

    Signals:
        frame_ready(int, int, int, int, int): frame, generation, tex_id, width, height
        render_error(int, str): frame, error_message
    """

    frame_ready = pyqtSignal(int, int, int, int, int)  # frame, generation, tex_id, width, height
    # Batch 1 (缺陷计划 RP-P0-01/12): 带 fence + slot_id 的 present descriptor。
    # 携带完整 GpuPresentFrame，让 Viewer 在自己 context 上 glWaitSync。
    present_frame_ready = pyqtSignal(object)  # GpuPresentFrame
    # Viewer ack 信号：Viewer 完成 blit 后通知 renderer 释放 slot。
    slot_acked = pyqtSignal(int)  # slot_id
    render_error = pyqtSignal(int, str)  # frame, error_message
    # 合成快照（1/4 分辨率 QImage）：暂停态 seek/scrub-settle 的帧回填
    # CPU 帧缓存，让"seek 回看刚看过的帧"无需重新解码。只在
    # arm_frame_snapshot() 布防后的下一帧触发，播放热路径零成本。
    snapshot_ready = pyqtSignal(int, int, object)  # frame, generation, QImage
    # W03（计划 W-I02）：cuda 驻留帧 GL 导入失败——按素材上报
    # (asset_path, reason, generation_id)，TPR 转交 EditGpuDecodeManager
    # .note_resident_import_failure 拉起"停新驻留请求 → 租约归零关旧源
    # → 下一帧下载 planar/CPU 重建"的真实恢复链。与 render_error（只记
    # 丢帧）互补：这里携带资产身份，故障隔离到单素材。
    cuda_resident_import_failed = pyqtSignal(str, str, int)
    # AUD-16605：GL 线程请求 GUI 线程销毁 shared context/surface（两个
    # Qt 对象都必须在其归属线程销毁；渲染线程无事件循环，无法自行处理
    # deleteLater/destroy）。跨线程 emit → 队列连接 → GUI 线程槽执行。
    _gl_teardown_requested = pyqtSignal(object, object)  # ctx, surface

    def __init__(self, main_context: QOpenGLContext, parent=None) -> None:
        """
        Initialize the GPU preview renderer.

        CRITICAL: Must be called on the GUI thread (same thread as main_context).

        Args:
            main_context: ProgramViewer's QOpenGLContext
            parent: Parent QObject
        """
        super().__init__(parent)
        self.setObjectName("EditGpuPreviewRenderer")

        self.main_context = main_context
        self._queue: queue.Queue = queue.Queue(maxsize=4)
        # Serialize submit-side queue coalescing. The render thread may
        # consume concurrently, but UI/background producers must not both
        # drain and refill the bounded queue at the same time.
        self._submit_lock = threading.Lock()
        # Renderer-local generation fence.  The timeline controller advances
        # this on seek/project changes so queued GPU work from an older
        # timeline state can be discarded before it reaches GL.  A value of
        # zero is also a valid first generation; submit() may monotonically
        # advance the fence for direct backend callers that submit generation
        # one before the controller's explicit reset arrives.
        self._generation_id = 0
        self._deadline_drop_count = 0
        self._running = True
        self._initialized = False  # Set True after successful GL init in run()
        # The QThread can begin before the GUI event loop reaches the deferred
        # context setup.  Events make that startup handshake bounded and,
        # crucially, let a timed-out thread cancel the pending GUI callback.
        # Without cancellation, that callback could later create a context and
        # move it to an already-exited QThread — an unsafe lifetime on macOS.
        self._context_setup_complete = threading.Event()
        self._context_setup_cancelled = threading.Event()
        self._startup_failure_reason = ""

        # GL resources (initialized in run())
        self.ctx = None  # ModernGL context
        self.surface: Optional[QOffscreenSurface] = None
        self.shared_pyqt_context: Optional[QOpenGLContext] = None

        # Effect chain runner (clip video filters: blur / sharpen / ...).
        # Lazily created in _init_shaders() once self.ctx is ready. Stays
        # None on this renderer if shader init failed, in which case effect
        # application becomes a silent no-op (see _apply_clip_effects).
        self._effect_runner = None

        # Preview-cost degradation flag. The playback engine sets this True
        # while actively playing back (so expensive effects downscale their
        # params for responsiveness) and False when paused/scrubbing (so the
        # user sees full quality). See _degrade_params_for_preview / the
        # spec.preview_cost annotation.
        self._preview_degraded: bool = False
        self._preview_gpu_budget_ms: float = 16.0
        self._last_preview_policy_decisions: tuple[PreviewPolicyDecision, ...] = ()

        # FX-M01: When True, effect scratch surfaces use RGBA16F (half-float)
        # to preserve HDR precision through the effect chain. Set by the
        # controller when the project/timeline uses HDR color spaces.
        self._effect_hdr: bool = False

        # Render resources
        self._composite_fbo = None
        self._composite_tex = None
        # P3.2 GPU 复合结果缓存：(generation, frame) -> 全分辨率合成纹理。
        # 命中时 worker 跳过解码、GPU 直接把缓存纹理 blit 进 present slot
        # （暂停态来回微调 / 重访已合成帧 = 纯拷贝）。generation 变化即失效
        # （与 CPU 帧缓存同一失效契约）；条目持有独立 tex+fbo，分配/释放只
        # 发生在 GL 线程，跨线程访问经 _composite_cache_lock。
        self._composite_cache: "OrderedDict[tuple, tuple]" = OrderedDict()
        self._composite_cache_lock = threading.Lock()
        self._composite_cache_pending_invalidate = False
        try:
            self._composite_cache_max = max(
                0, int(os.environ.get("TOPOS_GPU_COMPOSITE_CACHE_FRAMES", "32") or 32)
            )
        except (TypeError, ValueError):
            self._composite_cache_max = 32
        # 复合缓存总字节预算（条目数随画布分辨率自适应）。
        self._COMPOSITE_CACHE_BUDGET_BYTES = 268 * 1024 * 1024
        self._track_fbos: list = []
        self._track_texes: list = []
        # Secondary per-track targets keep source conversion and ColorStack
        # grading separate. Sampling from the same texture currently attached
        # to an FBO is undefined and was a source of mixed-frame artefacts.
        self._graded_track_fbos: list = []
        self._graded_track_texes: list = []
        # AUD-16603：调整剪辑 grade 的按需 scratch（稳定键 → 纹理/FBO，
        # 容量 _ADJUSTMENT_GRADE_SCRATCH_CAP，FIFO 淘汰）。调整剪辑绝不再
        # 用 1000+ 槽位索引预留上面的轨槽数组。
        self._adjustment_grade_texes: dict = {}
        self._adjustment_grade_fbos: dict = {}
        # Per-track targets for the clip-transform pass (planar/solid paths).
        self._transform_track_fbos: list = []
        self._transform_track_texes: list = []
        # Per-track targets for the crop matte pass.
        self._crop_track_fbos: list = []
        self._crop_track_texes: list = []
        self._yuv_program = None  # 2-plane (NV12/P010)
        self._yuv_rect_program = None  # 2-plane GL_TEXTURE_RECTANGLE
        self._yuv3_program = None  # 3-plane (YUV420/422/444)
        self._grade_program = None
        self._grade_vao = None
        self._transform_program = None
        self._transform_vao = None
        # FX-M06: crop matte pass. Compiled from the shared crop_render
        # fragment shader so the GPU output matches the CPU/export numpy
        # reference (see src/features/edit/core/crop_render.py). None until
        # _init_shaders() succeeds.
        self._crop_program = None
        self._crop_vao = None
        # Generator shader (checker/gradient/radial for Solid Color clips).
        # Compiled lazily on first non-solid generator render.
        self._generator_program = None
        self._generator_vao = None
        self._blend_program = None
        self._quad_vao = None
        # P0-PREV-GPU-T：双输入转场支持。所有内置双源转场都用 pixel-free
        # 控制块 + GL procedural pass；dip_to_color 用全屏颜色 pass。
        # 程序编译失败保持 None（fail-soft），worker 侧据此回退 CPU 合成。
        self._transition_program = None
        self._transition_vao = None
        self._full_frame_transition_program = None
        self._full_frame_transition_vao = None
        self._dip_program = None
        self._dip_vao = None
        # 转场混合结果 ping-pong（同域 scratch，方向与 track FBO 一致）
        self._transition_scratch_texes: list = []
        self._transition_scratch_fbos: list = []
        # 旧直接调用方的逐帧 mask 兼容纹理；生产转场路径不写入它。
        self._transition_mask_tex = None
        self._previous_gpu_frames: list = []
        self._deferred_gpu_frame_releases: list[tuple[int, list]] = []
        # Native pixel coordinate space for TransformParams. Set per render
        # task because output FBOs may be proxy-sized.
        self._active_transform_coordinate_size: Tuple[int, int] = (1920, 1080)

        # Per-task flag indicating that the composite sampled an external GPU
        # source (VT IOSurface or a CompositeGpuFrameHandle). When set,
        # _release_previous_frames inserts a GL sync barrier before returning
        # the source lease, so the next producer cannot recycle the texture
        # while this context is still sampling it.
        # Keep the historical name for compatibility with diagnostics/tests;
        # the flag now covers all external GPU sources, including comp handles.
        self._task_used_io_surface_source: bool = False

        # Rotating GPU output textures. Keep this larger than a classic
        # triple buffer because stale GPU results are filtered on the GUI
        # thread after the render thread has already written them. A deeper
        # pool gives the GUI event loop time to copy accepted textures before
        # the renderer reuses their slots during slow multi-track playback.
        self._output_pool: list = []  # list of (tex, fbo) tuples
        self._output_pool_index: int = 0
        self._output_pool_size: int = 24

        # Batch 1 (缺陷计划 RP-P0-01/02): present slot 生命周期管理。
        # OutputSlotPool 跟踪每个 output slot 的 FREE/WRITING/READY/
        # IN_VIEWER/RELEASABLE 状态。未 ack 的 slot 永不复用，根治混帧。
        # 与 _output_pool 共存：_output_pool 管 GL 资源，_present_pool 管生命周期。
        self._present_pool: OutputSlotPool = OutputSlotPool(size=self._output_pool_size)
        self._present_pool_registered: bool = False
        # 当前 task 正在写入的 slot lease（用于 fence + ack 跟踪）
        self._current_present_lease = None
        # AUD-16602：尺寸重建时退役的旧输出池（(pool, [(tex, fbo), ...],
        # retired_at) 列表）。在途未 ack 的 GpuPresentFrame 仍指向旧纹理，
        # 立即 tex.release() 会让 Viewer 的 glWaitSync/blit 采样已删除（且
        # 可能被复用）的 GL 名——退役纹理延迟到全部 slot 回收（或超时
        # 兜底）后才释放。
        self._retired_output_pools: list = []

        # P0-03: GL timer query（合成 pass GPU 时间）。GL 线程 lazy 创建
        # （需要 current context）；不支持时显式 gpu_time_unavailable。
        self._gl_pass_timer = None

        # YUV GPU uploader for planar CPU frames (ProRes etc.)
        self._yuv_uploader = None
        self._cfa_debayer = None  # TRAW CFA debayer（懒创建，GL 线程专用）
        # PBO upload is beneficial for large CPU-planar frames (notably 4K
        # ProRes) in a real shared QOpenGLContext, but costs more for normal
        # 1080p material.  The mode is selected per render task below.
        self._planar_upload_pbo_mode: tuple[bool, int] | None = None
        self._vt_import_bridge = None
        # B2.1（计划表 B）：Windows cuda 驻留帧 interop 会话（GL 线程专属，
        # 惰性创建；私有 CUDA 上下文，主上下文零触碰——见
        # src/shared/media/windows_hw/cuda_resident.py 头注）。
        self._cuda_interop_session = None

        # 合成快照（seek 快速路径数据源）：UI 线程 arm_frame_snapshot()
        # 布防 → 渲染线程在下一个合成帧 finalize 时做 1/4 分辨率 readback
        # → snapshot_ready 信号回填 CPU 帧缓存。播放热路径不布防，零成本。
        self._snapshot_armed: bool = False
        self._snapshot_tex = None
        self._snapshot_fbo = None
        self._snapshot_program = None
        self._snapshot_vao = None

        # Mutex for thread-safe GL access
        self._mutex = QMutex()

        # Deferred shared context creation.
        # CRITICAL: Must NOT call _init_shared_context here because this __init__
        # may be called from inside ProgramViewer.initializeGL() (via the
        # gl_initialized signal).  Calling doneCurrent()/makeCurrent() on the
        # main context while initializeGL() is in progress causes a native
        # segfault on macOS.  Defer with QTimer.singleShot(0) so the shared
        # context is created in the next GUI event-loop iteration, after
        # initializeGL() has fully returned.
        self._main_context_ref = main_context
        # AUD-16605：GL 线程退出时经该信号把 context/surface 销毁移交
        # GUI 线程（本对象归属 GUI 线程，AutoConnection 跨线程为队列）。
        self._gl_teardown_requested.connect(self._teardown_gl_handles_on_gui)
        QTimer.singleShot(0, self._deferred_init_shared_context)

    def _deferred_init_shared_context(self) -> None:
        """Deferred shared context creation (called from GUI event loop)."""
        if self._context_setup_cancelled.is_set():
            self._main_context_ref = None
            self._context_setup_complete.set()
            return
        if self._main_context_ref is None:
            self._context_setup_complete.set()
            return
        # 安全点检查：singleShot(0) 可能在 ProgramViewer 的 initializeGL/
        # paintGL 仍在栈上时被嵌套派发（那些路径会处理事件）。此刻对主
        # 上下文 doneCurrent() 并创建共享上下文会破坏 Qt 的 GL 状态记账，
        # 随后的 QOpenGLContext.create() 直接原生 abort（Qt6Core fail-fast
        # 0xc0000409）。
        # 注意判据必须是栈深度而非 currentContext()：QOpenGLWidget 在
        # paint 后保持 context current，"currentContext() 非 None" 在
        # viewer 首帧后就恒为真，会导致本初始化无限重派发（见模块头
        # _gui_gl_stack_depth 注释）。
        if _gui_gl_stack_depth > 0:
            QTimer.singleShot(0, self._deferred_init_shared_context)
            return
        ctx = self._main_context_ref
        self._main_context_ref = None  # Release reference
        try:
            self._init_shared_context(ctx)
        finally:
            self._context_setup_complete.set()

    def _init_shared_context(self, main_context: QOpenGLContext) -> None:
        """Create shared GL context on the GUI thread."""
        # CRITICAL: QOffscreenSurface must be created on the GUI thread.
        # On macOS this works from any thread, but Windows WGL requires it.
        if self._context_setup_cancelled.is_set():
            return
        self.surface = QOffscreenSurface()
        self.surface.setFormat(main_context.format())
        self.surface.create()
        if not self.surface.isValid():
            logger.error("EditGpuPreviewRenderer: failed to create offscreen surface")
            self.surface.destroy()
            self.surface = None
            return

        # Only release a context that is actually current on this GUI thread.
        # Calling doneCurrent() merely because the context has a surface can
        # corrupt Qt's native bookkeeping during a nested paint on macOS.
        current_surface = main_context.surface()
        restore_main_context = (
            current_surface is not None
            and QOpenGLContext.currentContext() is main_context
        )
        if restore_main_context:
            main_context.doneCurrent()

        try:
            # Create shared context.
            shared_context = QOpenGLContext()
            shared_context.setFormat(main_context.format())
            shared_context.setShareContext(main_context)
            if not shared_context.create():
                logger.error("EditGpuPreviewRenderer: Failed to create shared context!")
                shared_context.deleteLater()
                self.surface.destroy()
                self.surface = None
                return
            if self._context_setup_cancelled.is_set():
                shared_context.deleteLater()
                self.surface.destroy()
                self.surface = None
                return
            self.shared_pyqt_context = shared_context
            # Transfer ownership to this QThread only while it is still alive.
            self.shared_pyqt_context.moveToThread(self)
        finally:
            if restore_main_context:
                main_context.makeCurrent(current_surface)

        logger.info("EditGpuPreviewRenderer: shared GL context created")

    # ==================== Task Submission ====================

    def submit(self, task: EditRenderTask) -> bool:
        """
        Submit a render task with latest-only realtime QoS.

        Queued interactive work is stale as soon as a newer interactive task
        arrives, so retaining it only burns a present slot and decode/GPU
        leases. Background cache-fill work may be coalesced or rejected, but
        it is never allowed to evict an interactive task.

        Returns:
            True if task was accepted, False if dropped or renderer not ready.
        """
        if not self._initialized:
            playback_trace("gpu_renderer.submit_reject_not_initialized", frame=task.frame, generation=task.generation_id)
            return False
        # Use __dict__ for compatibility with lightweight QThread __new__
        # test doubles: QObject attribute lookup can reject an object whose
        # C++ base constructor was intentionally skipped.
        submit_lock = self.__dict__.get("_submit_lock")
        if submit_lock is None:
            # Lightweight __new__ test doubles from older callers do not run
            # __init__; lazily repair that state without changing the public
            # construction contract.
            submit_lock = threading.Lock()
            self._submit_lock = submit_lock

        with submit_lock:
            try:
                task_generation = int(getattr(task, "generation_id", 0))
                current_generation = int(
                    self.__dict__.get("_generation_id", 0) or 0
                )
                if task_generation < current_generation:
                    playback_trace(
                        "gpu_renderer.submit_reject_stale_generation",
                        frame=task.frame,
                        generation=task_generation,
                        current_generation=current_generation,
                    )
                    return False
                if task_generation > current_generation:
                    # Keep lightweight/direct backend callers safe too.  The
                    # normal timeline path calls set_generation_id() before
                    # submitting, but advancing here prevents a late older
                    # task from entering the queue when that handoff races.
                    self._generation_id = task_generation
                    stale_tasks = self._drop_stale_generation_tasks(
                        task_generation
                    )
                    for stale_task in stale_tasks:
                        self._release_task_sources(stale_task)
                    playback_trace(
                        "gpu_renderer.generation_advance_on_submit",
                        frame=task.frame,
                        generation=task_generation,
                        previous_generation=current_generation,
                        dropped=len(stale_tasks),
                    )
                deadline_ns = int(getattr(task, "deadline_ns", 0) or 0)
                if deadline_ns > 0 and time.time_ns() >= deadline_ns:
                    self._deadline_drop_count = int(
                        self.__dict__.get("_deadline_drop_count", 0) or 0
                    ) + 1
                    playback_trace(
                        "gpu_renderer.submit_reject_expired_deadline",
                        frame=task.frame,
                        generation=task_generation,
                        deadline_ns=deadline_ns,
                    )
                    return False
                if task.request_kind == "scrub":
                    dropped_scrub = self._drop_queued_scrub_tasks()
                    if dropped_scrub:
                        playback_trace(
                            "gpu_renderer.submit_drop_queued_scrub",
                            frame=task.frame,
                            generation=task.generation_id,
                            scrub_token=task.scrub_request_token,
                            dropped=dropped_scrub,
                        )

                # Keep only the newest task in each QoS class. A queued
                # interactive task is superseded by any newer interactive
                # task; a background task only supersedes older background
                # work and can never remove an interactive task.
                incoming_background = bool(
                    getattr(task, "background", False))
                dropped_tasks = self._coalesce_pending_tasks(
                    drop_background=incoming_background,
                )
                if dropped_tasks:
                    for dropped in dropped_tasks:
                        self._release_task_sources(dropped)
                    playback_trace(
                        "gpu_renderer.submit_coalesce",
                        frame=task.frame,
                        generation=task.generation_id,
                        dropped=len(dropped_tasks),
                        background=incoming_background,
                    )

                # A background task never evicts an interactive task. An
                # interactive task may evict one queued background task to
                # make room, preserving the foreground deadline.
                if self._queue.full():
                    if incoming_background:
                        playback_trace(
                            "gpu_renderer.submit_reject_full_background",
                            frame=task.frame,
                            generation_id=task.generation_id,
                        )
                        return False
                    dropped = self._drop_one_background_task()
                    if dropped is None:
                        try:
                            dropped = self._queue.get_nowait()
                            self._queue.task_done()
                        except queue.Empty:
                            dropped = None
                    if dropped is not None:
                        self._release_task_sources(dropped)
                        playback_trace(
                            "gpu_renderer.submit_drop_oldest",
                            dropped_frame=getattr(dropped, "frame", None),
                            frame=task.frame,
                        )
                self._queue.put_nowait(task)
                playback_trace(
                    "gpu_renderer.submit_accept",
                    frame=task.frame,
                    generation=task.generation_id,
                    output_size=task.output_fbo_size,
                    queue_size=self._queue.qsize(),
                    request_kind=task.request_kind,
                    scrub_token=task.scrub_request_token,
                )
                return True
            except queue.Full:
                playback_trace(
                    "gpu_renderer.submit_reject_full",
                    frame=task.frame,
                    generation=task.generation_id,
                )
                return False

    def set_generation_id(self, generation_id: int) -> None:
        """Advance the renderer generation and purge queued stale work.

        This is a queue-side cancellation boundary.  A task already taken by
        the render thread may finish, but its generation-tagged output is
        rejected by the timeline presenter; tasks still waiting in the
        bounded queue are released immediately so their decoded frame leases
        and composite GPU handles do not survive a seek.
        """
        target_generation = int(generation_id)
        submit_lock = self.__dict__.get("_submit_lock")
        if submit_lock is None:
            submit_lock = threading.Lock()
            self._submit_lock = submit_lock

        with submit_lock:
            previous_generation = int(
                self.__dict__.get("_generation_id", 0) or 0
            )
            if target_generation <= previous_generation:
                if target_generation < previous_generation:
                    playback_trace(
                        "gpu_renderer.generation_reset_reject_older",
                        generation=target_generation,
                        current_generation=previous_generation,
                    )
                return
            self._generation_id = target_generation
            stale_tasks = self._drop_stale_generation_tasks(target_generation)
            for stale_task in stale_tasks:
                self._release_task_sources(stale_task)

        playback_trace(
            "gpu_renderer.generation_reset",
            generation=target_generation,
            previous_generation=previous_generation,
            dropped=len(stale_tasks),
        )

    def _drop_stale_generation_tasks(
        self, generation_id: int,
    ) -> list[EditRenderTask]:
        """Remove queued tasks that do not belong to ``generation_id``."""
        queue_obj = self.__dict__.get("_queue")
        if queue_obj is None:
            return []

        kept: list[EditRenderTask] = []
        dropped: list[EditRenderTask] = []
        while True:
            try:
                queued_task = queue_obj.get_nowait()
                queue_obj.task_done()
            except queue.Empty:
                break
            if int(getattr(queued_task, "generation_id", 0)) == generation_id:
                kept.append(queued_task)
            else:
                dropped.append(queued_task)

        for queued_task in kept:
            try:
                queue_obj.put_nowait(queued_task)
            except queue.Full:
                # A concurrent consumer can only reduce occupancy, but a
                # defensive release keeps the queue ownership invariant true
                # if a test double or external producer refills it here.
                dropped.append(queued_task)
        return dropped

    def _coalesce_pending_tasks(
        self, *, drop_background: bool,
    ) -> list[EditRenderTask]:
        """Remove queued work superseded by the incoming task.

        ``drop_background`` selects the incoming class: when true, only old
        background work is removed; when false, all queued interactive work
        is removed. Tasks already taken by the render thread are not in the
        queue and therefore cannot be cancelled here.
        """
        kept: list[EditRenderTask] = []
        dropped: list[EditRenderTask] = []
        while True:
            try:
                queued_task = self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break
            queued_background = bool(
                getattr(queued_task, "background", False))
            should_drop = (
                queued_background if drop_background else not queued_background
            )
            if should_drop:
                dropped.append(queued_task)
            else:
                kept.append(queued_task)

        for queued_task in kept:
            try:
                self._queue.put_nowait(queued_task)
            except queue.Full:
                # A concurrent producer may fill the queue while it is being
                # restored. Release anything that cannot be restored.
                dropped.append(queued_task)
        return dropped

    def _drop_one_background_task(self) -> EditRenderTask | None:
        """Drop one queued background task, preserving interactive order."""
        kept: list[EditRenderTask] = []
        dropped: EditRenderTask | None = None
        while True:
            try:
                queued_task = self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break
            if dropped is None and bool(
                    getattr(queued_task, "background", False)):
                dropped = queued_task
            else:
                kept.append(queued_task)

        for queued_task in kept:
            try:
                self._queue.put_nowait(queued_task)
            except queue.Full:
                self._release_task_sources(queued_task)
        return dropped

    def _drop_queued_scrub_tasks(self) -> int:
        """Remove unstarted scrub render tasks so the latest drag position wins."""
        kept: list[EditRenderTask] = []
        dropped = 0
        while True:
            try:
                queued_task = self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break
            if getattr(queued_task, "request_kind", "") == "scrub":
                dropped += 1
                self._release_task_sources(queued_task)
            else:
                kept.append(queued_task)

        for queued_task in kept:
            try:
                self._queue.put_nowait(queued_task)
            except queue.Full:
                dropped += 1
                self._release_task_sources(queued_task)
        return dropped

    @property
    def is_initialized(self) -> bool:
        """Check if the renderer has completed GL initialization."""
        return self._initialized

    @property
    def deadline_drop_count(self) -> int:
        """Number of GPU tasks discarded after their present deadline expired."""
        return int(self.__dict__.get("_deadline_drop_count", 0) or 0)

    def stop(self) -> None:
        """Stop the render thread."""
        self._running = False
        if not self._initialized:
            self._context_setup_cancelled.set()
            self._context_setup_complete.set()

    def _process_task_with_gpu_lane(self, task: EditRenderTask) -> None:
        """Run one task while holding the shared GPU command lane.

        The Edit presenter and the timeline Comp Clip producer use different
        shared OpenGL contexts, but they share textures, fences and driver
        state.  The producer already serializes its work through
        ``GLContextManager.mutex``; the presenter must join the same lane so
        a consumer cannot sample/recycle a shared resource while the
        producer is submitting commands.  This is deliberately scoped to
        task processing: queue management and CPU-side decode stay outside
        the lane, and the lock is released even when a frame fails.
        """
        gpu_mutex = GLContextManager.instance().mutex
        gpu_mutex.lock()
        try:
            self._process_task(task)
            self._drain_deferred_gpu_frame_releases(block=False)
        finally:
            gpu_mutex.unlock()

    # ==================== Thread Main Loop ====================

    def run(self) -> None:
        """Thread entry point: init GL resources, then process tasks."""
        # Wait for the GUI-side context handshake.  Do not spin on a nullable
        # pointer: a late callback after this thread exits used to produce a
        # QOpenGLContext whose affinity pointed at a dead thread.
        # Under heavy import/waveform workloads, the GUI event loop may defer
        # the shared-context callback for several seconds. Keep the bounded
        # Event wait (so stop() can cancel the callback safely), but allow the
        # longer startup window needed by the GPU presentation path.
        if not self._context_setup_complete.wait(timeout=30.0):
            self._startup_failure_reason = "shared context not created in time"
            self._context_setup_cancelled.set()
            self._running = False
            logger.error("EditGpuPreviewRenderer: %s", self._startup_failure_reason)
            return
        if self._context_setup_cancelled.is_set() or self.shared_pyqt_context is None or self.surface is None:
            self._startup_failure_reason = "shared context setup cancelled or failed"
            self._running = False
            logger.warning("EditGpuPreviewRenderer: %s; using CPU preview fallback", self._startup_failure_reason)
            return

        # Activate shared context on this thread
        if not self.shared_pyqt_context.makeCurrent(self.surface):
            logger.error("EditGpuPreviewRenderer: failed to make context current")
            return

        # Initialize ModernGL
        try:
            import moderngl
            self.ctx = moderngl.create_context()
        except Exception as e:
            logger.error(f"EditGpuPreviewRenderer: failed to create ModernGL context: {e}")
            return

        # Compile shaders
        if not self._init_shaders():
            logger.error("EditGpuPreviewRenderer: failed to compile shaders")
            return

        # Create quad geometry
        self._init_quad()

        # Initialize YUV GPU uploader for planar CPU frames (ProRes etc.)
        try:
            from src.shared.video.yuv_gpu_upload import YUVGPUUploader
            self._yuv_uploader = YUVGPUUploader(self.ctx, pool_size=3)
            logger.info("EditGpuPreviewRenderer: YUVGPUUploader initialized")
        except Exception as e:
            logger.warning(f"EditGpuPreviewRenderer: YUVGPUUploader init failed: {e}")
            self._yuv_uploader = None
        # GL 重初始化时 CFA debayer 同步重建（旧对象挂旧 context 资源；
        # 旧 context 仍存活时显式 release，避免 4×R16UI/program/VAO 泄漏）
        if self._cfa_debayer is not None:
            try:
                self._cfa_debayer.release()
            except Exception:
                pass
        self._cfa_debayer = None
        self._cfa_debayer_bd = None
        self._cfa_gamut_unknown_logged = False  # RC3：未知相机仅提示一次

        logger.info("EditGpuPreviewRenderer: initialized and running")
        self._initialized = True

        # Task loop
        while self._running:
            try:
                task = self._queue.get(timeout=0.016)  # ~60Hz poll
            except queue.Empty:
                continue

            task_generation = int(getattr(task, "generation_id", 0))
            current_generation = int(
                self.__dict__.get("_generation_id", 0) or 0
            )
            if task_generation != current_generation:
                # set_generation_id() purges queued work, but a task may have
                # crossed the queue boundary just before the reset.  Do not
                # let that race reach GL; its sources are still owned by the
                # task because the render thread has only dequeued it.
                self._release_task_sources(task)
                self._queue.task_done()
                playback_trace(
                    "gpu_renderer.drop_stale_generation",
                    frame=task.frame,
                    generation=task_generation,
                    current_generation=current_generation,
                )
                continue

            deadline_ns = int(getattr(task, "deadline_ns", 0) or 0)
            if deadline_ns > 0 and time.time_ns() >= deadline_ns:
                self._release_task_sources(task)
                self._queue.task_done()
                self._deadline_drop_count = int(
                    self.__dict__.get("_deadline_drop_count", 0) or 0
                ) + 1
                playback_trace(
                    "gpu_renderer.drop_expired_deadline",
                    frame=task.frame,
                    generation=task_generation,
                    deadline_ns=deadline_ns,
                )
                continue

            self._mutex.lock()
            try:
                playback_trace(
                    "gpu_renderer.process_start",
                    frame=task.frame,
                    generation=task.generation_id,
                    output_size=task.output_fbo_size,
                    layers=len(task.layers),
                )
                self._process_task_with_gpu_lane(task)
            except Exception as e:
                logger.error(f"EditGpuPreviewRenderer: task error frame={task.frame}: {e}")
                playback_trace("gpu_renderer.process_error", frame=task.frame, generation=task.generation_id, error=e)
                # AUD-16601：异常路径回收未 finalize 的 WRITING 槽位
                #（_finalize_present_frame 成功后 lease 已置 None，此处
                # abandon 对已上屏帧是 no-op）。
                self._abandon_current_present_lease()
                self._release_task_sources(task)
                self.render_error.emit(task.frame, str(e))
            finally:
                self._mutex.unlock()
                self._queue.task_done()

        # Cleanup
        self._cleanup()

    # ==================== Shader Initialization ====================

    def _init_shaders(self) -> bool:
        """Compile YUV→RGB (2-plane and 3-plane) and blend shaders."""
        try:
            # Common vertex shader.
            # u_transform is a 2D affine mat3 applied in NDC space so each
            # clip's TransformParams (position/scale/rotation/anchor) deforms
            # its own track FBO quad. Programs that shouldn't be transformed
            # (grade / blend) set it to identity. Column-major, written via
            # program['u_transform'].write(bytes).
            vert = """
            #version 330
            in vec2 in_vert;
            in vec2 in_texcoord;
            uniform mat3 u_transform;
            out vec2 v_texcoord;
            void main() {
                gl_Position = vec4((u_transform * vec3(in_vert, 1.0)).xy, 0.0, 1.0);
                v_texcoord = in_texcoord;
            }
            """

            # Shared YUV→RGB conversion logic
            yuv_to_rgb_body = """
                float range_mul = u_color_range == 1 ? 1.0 : (219.0 / 255.0);
                float range_off = u_color_range == 1 ? 0.0 : (16.0 / 255.0);
                y = (y - range_off) / range_mul;
                y = max(y, 0.0);
                // Limited range 色度：中性点 128/255，码值跨度 224/255，
                // 归一化到 [-0.5, 0.5] 需乘 255/224（与 YUVGPUUploader /
                // color_decode CPU 参考一致）。旧版 (x-0.5)*224/219 色度增益
                // 低约 10%，导致编辑页画面红色偏淡、肤色苍白。
                float c_off = u_color_range == 1 ? 0.5 : (128.0 / 255.0);
                float c_scale = u_color_range == 1 ? 1.0 : (255.0 / 224.0);
                cb = (cb - c_off) * c_scale;
                cr = (cr - c_off) * c_scale;

                float r, g, b;
                // The chroma channels are Cb (blue difference) and Cr (red
                // difference). Keep their order explicit in the green term;
                // swapping them makes skin tones visibly yellow and weakens red.
                if (u_color_matrix == 0) { // BT.601 / SMPTE 170M
                    r = y + 1.402 * cr;
                    g = y - 0.344136 * cb - 0.714136 * cr;
                    b = y + 1.772 * cb;
                } else if (u_color_matrix == 1) { // BT.709
                    r = y + 1.5748 * cr;
                    g = y - 0.1873 * cb - 0.4681 * cr;
                    b = y + 1.8556 * cb;
                } else { // BT.2020
                    r = y + 1.4746 * cr;
                    g = y - 0.1646 * cb - 0.5714 * cr;
                    b = y + 1.8814 * cb;
                }
                f_color = vec4(clamp(r, 0.0, 1.0), clamp(g, 0.0, 1.0), clamp(b, 0.0, 1.0), 1.0);
            """

            # 2-plane YUV shader (NV12/P010: Y + interleaved UV)
            self._yuv_program = self.ctx.program(
                vertex_shader=vert,
                fragment_shader=f"""
                #version 330
                uniform sampler2D u_y_plane;
                uniform sampler2D u_uv_plane;
                uniform int u_color_range;
                uniform int u_color_matrix;
                uniform int u_uv_component_mode;
                in vec2 v_texcoord;
                out vec4 f_color;
                void main() {{
                    float y = texture(u_y_plane, v_texcoord).r;
                    vec4 uv_sample = texture(u_uv_plane, v_texcoord);
                    vec2 uv = u_uv_component_mode == 1 ? uv_sample.ra : uv_sample.rg;
                    float cb = uv.r;
                    float cr = uv.g;
                    {yuv_to_rgb_body}
                }}
                """,
            )

            # 2-plane YUV shader for IOSurface profiles that can only bind
            # as GL_TEXTURE_RECTANGLE. Rectangle samplers use pixel coords,
            # so the normalized quad UV is scaled by each plane's dimensions.
            self._yuv_rect_program = self.ctx.program(
                vertex_shader=vert,
                fragment_shader=f"""
                #version 330
                #extension GL_ARB_texture_rectangle : enable
                uniform sampler2DRect u_y_plane;
                uniform sampler2DRect u_uv_plane;
                uniform vec2 u_y_size;
                uniform vec2 u_uv_size;
                uniform int u_color_range;
                uniform int u_color_matrix;
                uniform int u_uv_component_mode;
                in vec2 v_texcoord;
                out vec4 f_color;
                void main() {{
                    float y = texture(u_y_plane, v_texcoord * u_y_size).r;
                    vec4 uv_sample = texture(u_uv_plane, v_texcoord * u_uv_size);
                    vec2 uv = u_uv_component_mode == 1 ? uv_sample.ra : uv_sample.rg;
                    float cb = uv.r;
                    float cr = uv.g;
                    {yuv_to_rgb_body}
                }}
                """,
            )

            # 3-plane YUV shader (YUV420/422/444: separate Y, U, V)
            self._yuv3_program = self.ctx.program(
                vertex_shader=vert,
                fragment_shader=f"""
                #version 330
                uniform sampler2D u_y_plane;
                uniform sampler2D u_u_plane;
                uniform sampler2D u_v_plane;
                uniform int u_color_range;
                uniform int u_color_matrix;
                in vec2 v_texcoord;
                out vec4 f_color;
                void main() {{
                    float y = texture(u_y_plane, v_texcoord).r;
                    float cb = texture(u_u_plane, v_texcoord).r;
                    float cr = texture(u_v_plane, v_texcoord).r;
                    {yuv_to_rgb_body}
                }}
                """,
            )

            # Realtime primary ColorStack pass.  It is deliberately optional:
            # a driver-specific failure here must never make the base YUV
            # preview renderer fail to initialize and freeze all raw clips.
            try:
                self._grade_program = self.ctx.program(
                    vertex_shader=vert,
                    fragment_shader=PRIMARY_GRADE_FRAGMENT,
                )
            except Exception as error:
                self._grade_program = None
                logger.warning(
                    "EditGpuPreviewRenderer: ColorStack grade pass disabled: %s",
                    error,
                )

            # Simple passthrough shader with u_transform for applying clip
            # transform to planar/solid path textures (which don't go through
            # the YUV vertex shader that already has u_transform).
            self._transform_program = self.ctx.program(
                vertex_shader=vert,
                fragment_shader="""
                #version 330
                uniform sampler2D u_tex;
                in vec2 v_texcoord;
                out vec4 f_color;
                void main() {
                    f_color = texture(u_tex, v_texcoord);
                }
                """,
            )
            self._transform_vao = None  # created in _init_quad

            # FX-M06: crop matte pass. The fragment shader source lives in the
            # shared crop_render module so its geometry is reviewable next to
            # the numpy reference the CPU/export paths use. It is compiled
            # with the canonical ``vert`` (u_transform applied as identity by
            # _apply_crop) and reads ``v_texcoord`` to match the rest of the
            # track-FBO pipeline (non-flipped quad).
            from src.features.edit.core.crop_render import CROP_FRAGMENT_SHADER
            try:
                self._crop_program = self.ctx.program(
                    vertex_shader=vert, fragment_shader=CROP_FRAGMENT_SHADER,
                )
                self._crop_vao = None  # created in _init_quad
            except Exception as error:  # noqa: BLE001
                self._crop_program = None
                logger.warning(
                    "EditGpuPreviewRenderer: crop matte pass disabled: %s",
                    error,
                )

            # Alpha blend shader
            self._blend_program = self.ctx.program(
                vertex_shader=_PREVIEW_PASS_VERTEX_SHADER_SOURCE,
                fragment_shader=_BLEND_FRAGMENT_SHADER_SOURCE,
            )

            # P0-PREV-GPU-T：spatial 转场混合 pass（源码见模块头注释）。
            try:
                self._transition_program = self.ctx.program(
                    vertex_shader=_PREVIEW_PASS_VERTEX_SHADER_SOURCE,
                    fragment_shader=_TRANSITION_FRAGMENT_SHADER_SOURCE,
                )
            except Exception as error:  # noqa: BLE001
                self._transition_program = None
                logger.warning(
                    "EditGpuPreviewRenderer: transition pass disabled: %s",
                    error,
                )

            # Full-frame dissolve pass.  Blur/Defocus/Film are intentionally
            # separate from the geometric mask shader so a future mask change
            # cannot silently reintroduce a directional boundary.
            try:
                self._full_frame_transition_program = self.ctx.program(
                    vertex_shader=_PREVIEW_PASS_VERTEX_SHADER_SOURCE,
                    fragment_shader=_FULL_FRAME_TRANSITION_FRAGMENT_SHADER_SOURCE,
                )
            except Exception as error:  # noqa: BLE001
                self._full_frame_transition_program = None
                logger.warning(
                    "EditGpuPreviewRenderer: full-frame transition pass disabled: %s",
                    error,
                )

            # P0-PREV-GPU-T：dip_to_color 全屏颜色 pass。
            try:
                self._dip_program = self.ctx.program(
                    vertex_shader=_PREVIEW_PASS_VERTEX_SHADER_SOURCE,
                    fragment_shader=_DIP_FRAGMENT_SHADER_SOURCE,
                )
            except Exception as error:  # noqa: BLE001
                self._dip_program = None
                logger.warning(
                    "EditGpuPreviewRenderer: dip pass disabled: %s",
                    error,
                )

            # Build the effect-pass runner on this context. Compiled shaders
            # are cached by (effect_type, structural_params) so subsequent
            # frames reuse them — see EffectPassRunner._get_program.
            # Failure here is non-fatal: _apply_clip_effects treats a None
            # runner as a no-op, so playback continues without filters.
            try:
                from src.features.shared.effects.effect_pass_runner import (
                    EffectPassRunner,
                )
                self._effect_runner = EffectPassRunner(self.ctx)
            except Exception as effect_err:  # noqa: BLE001
                logger.warning(
                    "EditGpuPreviewRenderer: EffectPassRunner disabled: %s",
                    effect_err,
                )
                self._effect_runner = None

            return True
        except Exception as e:
            logger.error(f"EditGpuPreviewRenderer: shader compile error: {e}")
            return False

    def _init_quad(self) -> None:
        """Create fullscreen quad VBO and VAOs for each shader program."""
        import numpy as np
        vertices = np.array([
            # pos       texcoord
            -1.0, -1.0,  0.0, 1.0,  # bottom-left
             1.0, -1.0,  1.0, 1.0,  # bottom-right
            -1.0,  1.0,  0.0, 0.0,  # top-left
             1.0,  1.0,  1.0, 0.0,  # top-right
        ], dtype=np.float32)

        self._quad_vbo = self.ctx.buffer(vertices)

        # Blend quad：不翻转的 UV（bottom-left → UV(0,0)）。
        # blend shader 在两个 FBO 纹理之间合成，input 和 output 都是 GL FBO
        # 方向（y=0 在底部），不需要 Y 翻转。如果和 YUV quad 共用翻转 UV，
        # 多轨 ping-pong 每次都翻转一次，奇数次 blend 后画面会上下颠倒。
        blend_vertices = np.array([
            # pos       texcoord（标准 GL 方向，不翻转）
            -1.0, -1.0,  0.0, 0.0,  # bottom-left
             1.0, -1.0,  1.0, 0.0,  # bottom-right
            -1.0,  1.0,  0.0, 1.0,  # top-left
             1.0,  1.0,  1.0, 1.0,  # top-right
        ], dtype=np.float32)
        self._blend_quad_vbo = self.ctx.buffer(blend_vertices)

        # Create separate VAO for each program to avoid attribute binding conflicts
        # YUV VAO 也用不翻转 quad，和 YUVGPUUploader（Planar 路径）保持一致。
        # 这样所有 track FBO 方向统一为"不翻转"（图像顶部=FBO 底部），
        # composite 最后一步统一翻转一次到正确方向。
        self._yuv2_vao = self.ctx.vertex_array(
            self._yuv_program,
            [(self._blend_quad_vbo, "2f 2f", "in_vert", "in_texcoord")],
        )
        self._yuv_rect_vao = self.ctx.vertex_array(
            self._yuv_rect_program,
            [(self._blend_quad_vbo, "2f 2f", "in_vert", "in_texcoord")],
        )
        self._yuv3_vao = self.ctx.vertex_array(
            self._yuv3_program,
            [(self._blend_quad_vbo, "2f 2f", "in_vert", "in_texcoord")],
        )
        if self._grade_program is not None:
            self._grade_vao = self.ctx.vertex_array(
                self._grade_program,
                [(self._blend_quad_vbo, "2f 2f", "in_vert", "in_texcoord")],
            )
        # Transform passthrough VAO (non-flipped, track FBO space)
        if self._transform_program is not None:
            self._transform_vao = self.ctx.vertex_array(
                self._transform_program,
                [(self._blend_quad_vbo, "2f 2f", "in_vert", "in_texcoord")],
            )
        # FX-M06: crop matte VAO (non-flipped, track FBO space — same as
        # transform/grade so the cropped track keeps the pipeline orientation).
        if self._crop_program is not None:
            self._crop_vao = self.ctx.vertex_array(
                self._crop_program,
                [(self._blend_quad_vbo, "2f 2f", "in_vert", "in_texcoord")],
            )
        # blend 用不翻转 quad（FBO→FBO 合成，方向一致）
        self._blend_vao = self.ctx.vertex_array(
            self._blend_program,
            [(self._blend_quad_vbo, "2f 2f", "in_vert", "in_texcoord")],
        )
        # 翻转 blend VAO：用于最终 composite blit，把"图像顶部=FBO 底部"
        # 翻转为"图像顶部=FBO 顶部"，让 Viewer 用 is_gl_oriented=True 正确显示。
        self._flip_blend_vao = self.ctx.vertex_array(
            self._blend_program,
            [(self._quad_vbo, "2f 2f", "in_vert", "in_texcoord")],
        )
        # P0-PREV-GPU-T：转场/dip VAO（不翻转，track FBO 同域）
        self._flip_dip_vao = None
        if self._transition_program is not None:
            self._transition_vao = self.ctx.vertex_array(
                self._transition_program,
                [(self._blend_quad_vbo, "2f 2f", "in_vert", "in_texcoord")],
            )
        if self._full_frame_transition_program is not None:
            self._full_frame_transition_vao = self.ctx.vertex_array(
                self._full_frame_transition_program,
                [(self._blend_quad_vbo, "2f 2f", "in_vert", "in_texcoord")],
            )
        if self._dip_program is not None:
            self._dip_vao = self.ctx.vertex_array(
                self._dip_program,
                [(self._blend_quad_vbo, "2f 2f", "in_vert", "in_texcoord")],
            )
            # flip 变体：adjustment 路径的 dip 后处理把 composite（top=top）
            # 经 scratch 往返时恢复方向用。
            self._flip_dip_vao = self.ctx.vertex_array(
                self._dip_program,
                [(self._quad_vbo, "2f 2f", "in_vert", "in_texcoord")],
            )
        self._quad_vao = self._blend_vao
        # Keep backward compat alias
        self._quad_vao = self._blend_vao

    # ==================== External Texture Wrapping ====================

    def _ensure_vt_import_bridge(self):
        """Create the IOSurface import bridge in the renderer GL thread."""
        if self._vt_import_bridge is not None:
            return self._vt_import_bridge
        try:
            from src.shared.media.macos_vt.iosurface_gl_bridge import IOSurfaceGLBridge

            # Source texture slots stay leased until their source-read fence
            # signals. Keep bounded headroom for queued/presented frames.
            bridge = IOSurfaceGLBridge(ring_size=8, max_ring_size=32)
            if not bridge.initialize(planes_per_frame=2):
                return None
            self._vt_import_bridge = bridge
            return bridge
        except Exception as e:
            logger.warning(f"VT import bridge init failed: {e}")
            return None

    def _import_vt_pixel_buffer_frame(self, vt_frame):
        """
        Import an unbound VT/CVPixelBuffer frame into GL textures.

        Must run on this renderer thread while the shared GL context is current.
        """
        from src.shared.media.gpu_frame import GpuDecodedFrame, GpuFrameRelease
        from src.shared.media.frame_path import DecodePath

        bridge = self._ensure_vt_import_bridge()
        if bridge is None:
            release = getattr(vt_frame, "release", None)
            if callable(release):
                release()
            return None

        pixel_buffer = getattr(vt_frame, "pixel_buffer", None)
        if pixel_buffer is None:
            release = getattr(vt_frame, "release", None)
            if callable(release):
                release()
            return None

        result = bridge.bind_pixel_buffer(
            pixel_buffer=pixel_buffer,
            frame_index=int(getattr(vt_frame, "frame_index", 0) or 0),
            pixel_format=str(getattr(vt_frame, "pixel_format", "nv12") or "nv12"),
            bit_depth=int(getattr(vt_frame, "bit_depth", 8) or 8),
            decode_path=DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY.value,
        )
        if result is None:
            release = getattr(vt_frame, "release", None)
            if callable(release):
                release()
            playback_trace(
                "gpu_renderer.vt_import_failed",
                frame=getattr(vt_frame, "frame_index", None),
                reason=getattr(bridge, "last_fallback_reason", None),
            )
            return None

        planes, bridge_release = result

        def _release() -> None:
            try:
                bridge_release.release()
            finally:
                release = getattr(vt_frame, "release", None)
                if callable(release):
                    release()

        self._note_zero_copy_import(vt_frame)
        return GpuDecodedFrame(
            frame_index=int(getattr(vt_frame, "frame_index", 0) or 0),
            pts=getattr(vt_frame, "pts", None),
            time_seconds=float(getattr(vt_frame, "time_seconds", 0.0) or 0.0),
            width=int(getattr(vt_frame, "width", 0) or 0),
            height=int(getattr(vt_frame, "height", 0) or 0),
            visible_rect=(
                0,
                0,
                int(getattr(vt_frame, "width", 0) or 0),
                int(getattr(vt_frame, "height", 0) or 0),
            ),
            pixel_format=str(getattr(vt_frame, "pixel_format", "nv12") or "nv12"),
            bit_depth=int(getattr(vt_frame, "bit_depth", 8) or 8),
            planes=planes,
            color_range=str(getattr(vt_frame, "color_range", "limited") or "limited"),
            color_matrix=str(getattr(vt_frame, "color_matrix", "bt709") or "bt709"),
            color_primaries=getattr(vt_frame, "color_primaries", None),
            color_transfer=getattr(vt_frame, "color_transfer", None),
            decode_path=DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY.value,
            zero_copy=True,
            release=GpuFrameRelease(release_callback=_release),
        )

    def _note_zero_copy_import(self, vt_frame) -> None:
        """A0.1：零拷贝 IOSurface 导入成功时为该帧登记 import_upload 段
        "not_applicable" 证据（无 CPU 上传字节；TPR 消费后按
        import_upload_status='not_applicable' annotate，与 planar 上传的
        measured / 无证据的 unset 三态区分）。
        """
        self._store_planar_upload_stats(
            int(getattr(vt_frame, "frame_index", 0) or 0),
            {
                "bytes": 0,
                "ms": 0.0,
                "tracks": 0,
                "mode": "iosurface_zero_copy",
                "pixel_format": str(getattr(vt_frame, "pixel_format", "nv12") or "nv12"),
                "gbr": False,
            },
        )

    def _ensure_cuda_interop_session(self):
        """Create the Windows cuda-resident interop session (GL thread)."""
        if self._cuda_interop_session is not None:
            return self._cuda_interop_session
        try:
            from src.shared.media.windows_hw.cuda_resident import (
                CudaGlInteropSession,
            )

            session = CudaGlInteropSession(ring_size=8, max_ring_size=32)
            if not session.initialize():
                playback_trace(
                    "gpu_renderer.cuda_interop_init_failed",
                    reason=session.last_fallback_reason,
                )
                return None
            self._cuda_interop_session = session
            return session
        except Exception as e:
            logger.warning(f"CUDA interop session init failed: {e}")
            return None

    def _import_cuda_resident_frame(self, cu_frame, asset_path: str = "",
                                    generation_id: int = 0):
        """
        Import a Windows cuda-resident frame into persistent GL textures.

        B2.1（计划表 B）：设备帧经私有上下文 interop 拷入会话纹理池
        （map→DtoA→unmap，同步由 map/unmap 提供）；拷贝完成后源 av 帧
        立即释放（GL 纹理已独立）。Must run on this renderer GL thread.

        W03（计划 W-I02）：失败时除既有全局门闩记账外，按素材上报
        cuda_resident_import_failed(asset_path, reason, generation_id)，
        让解码管理器停用该素材的驻留路由并重建下载/CPU 源——单素材
        故障不拖垮其它轨；会话失效（destroyed）时丢弃缓存的死会话引用，
        下一次导入自动重建新会话。
        """
        from src.shared.media.gpu_frame import GpuDecodedFrame, GpuFrameRelease
        from src.shared.media.frame_path import DecodePath

        session = self._ensure_cuda_interop_session()
        if session is None:
            from src.shared.media.windows_hw.cuda_resident import (
                register_import_failure,
            )

            register_import_failure()  # B2.1 降级门闩（连续失败停新源路由）
            cu_frame.release()
            self.cuda_resident_import_failed.emit(
                str(asset_path or ''), 'interop_session_unavailable',
                int(generation_id))
            return None

        result = session.bind_resident_frame(
            cu_frame, decode_path=DecodePath.WINDOWS_CUDA_GL_ZERO_COPY.value)
        if result is None:
            from src.shared.media.windows_hw.cuda_resident import (
                register_import_failure,
            )

            register_import_failure()
            reason = str(getattr(session, 'last_fallback_reason', '') or
                         'cuda_import_failed')
            cu_frame.release()
            playback_trace(
                "gpu_renderer.cuda_import_failed",
                frame=getattr(cu_frame, "frame_index", None),
                reason=reason,
            )
            self.cuda_resident_import_failed.emit(
                str(asset_path or ''), reason, int(generation_id))
            # 会话失效恢复：destroyed 的会话引用立即丢弃，下一次导入走
            # _ensure_cuda_interop_session 重建新会话（不永久卡死在死句柄）。
            if session.stats.get("destroyed"):
                playback_trace(
                    "gpu_renderer.cuda_interop_session_dropped",
                    reason=reason,
                )
                self._cuda_interop_session = None
            return None

        from src.shared.media.windows_hw.cuda_resident import (
            register_import_success,
        )

        register_import_success()

        planes, lease = result
        # 设备侧拷贝已完成，源 av 帧引用可立即归还（纹理内容独立）
        cu_frame.release()

        def _release() -> None:
            try:
                lease.release()
            finally:
                cu_frame.release()

        self._note_cuda_zero_copy_import(cu_frame)
        return GpuDecodedFrame(
            frame_index=int(getattr(cu_frame, "frame_index", 0) or 0),
            pts=getattr(cu_frame, "pts", None),
            time_seconds=float(getattr(cu_frame, "time_seconds", 0.0) or 0.0),
            width=int(getattr(cu_frame, "width", 0) or 0),
            height=int(getattr(cu_frame, "height", 0) or 0),
            visible_rect=(
                0,
                0,
                int(getattr(cu_frame, "width", 0) or 0),
                int(getattr(cu_frame, "height", 0) or 0),
            ),
            pixel_format=str(getattr(cu_frame, "pixel_format", "nv12") or "nv12"),
            bit_depth=int(getattr(cu_frame, "bit_depth", 8) or 8),
            planes=planes,
            color_range=str(getattr(cu_frame, "color_range", "limited") or "limited"),
            color_matrix=str(getattr(cu_frame, "color_matrix", "bt709") or "bt709"),
            color_primaries=getattr(cu_frame, "color_primaries", None),
            color_transfer=getattr(cu_frame, "color_transfer", None),
            decode_path=DecodePath.WINDOWS_CUDA_GL_ZERO_COPY.value,
            zero_copy=True,
            release=GpuFrameRelease(release_callback=_release,
                                    owner_backend="cuda"),
        )

    def _note_cuda_zero_copy_import(self, cu_frame) -> None:
        """B2.1：cuda 驻留导入成功时登记 import_upload 段 "not_applicable"
        证据（设备内拷贝，无 CPU 上传字节；与 VT 零拷贝同口径）。"""
        self._store_planar_upload_stats(
            int(getattr(cu_frame, "frame_index", 0) or 0),
            {
                "bytes": 0,
                "ms": 0.0,
                "tracks": 0,
                "mode": "cuda_resident_zero_copy",
                "pixel_format": str(getattr(cu_frame, "pixel_format", "nv12") or "nv12"),
                "gbr": False,
            },
        )

    def _wrap_external_gpu_planes(self, planes: list, gpu_frame) -> Optional[tuple]:
        """
        Wrap external GPU texture IDs into ExternalGLTexture objects.

        Directly ported from AsyncRenderer._wrap_external_gpu_planes().

        Args:
            planes: List of GpuFramePlane from GpuDecodedFrame
            gpu_frame: The GpuDecodedFrame containing metadata

        Returns:
            Tuple of ExternalGLTexture objects, or None on failure.
        """
        from src.shared.media.gpu_frame import GpuFramePlane
        from src.shared.media.macos_vt.external_texture import ExternalGLTexture

        from OpenGL.GL import GL_TEXTURE_2D, GL_TEXTURE_RECTANGLE

        wrapped = []
        for plane in planes:
            if not isinstance(plane, GpuFramePlane):
                logger.warning(f"unexpected plane type {type(plane)}")
                return None

            if plane.texture_id == 0:
                logger.warning("plane has texture_id=0")
                return None

            target = GL_TEXTURE_2D
            if plane.target == 'rectangle':
                target = GL_TEXTURE_RECTANGLE
            elif plane.target == 'external':
                target = 36197  # GL_TEXTURE_EXTERNAL_OES

            wrapped.append(ExternalGLTexture(
                texture_id=plane.texture_id,
                target=target,
                size=(plane.width, plane.height),
                internal_format=plane.internal_format,
                pixel_format=plane.pixel_format,
                pixel_type=plane.pixel_type,
            ))

        if len(wrapped) == 2:
            return (wrapped[0], wrapped[1])
        elif len(wrapped) == 3:
            return (wrapped[0], wrapped[1], wrapped[2])
        elif len(wrapped) >= 4:
            return (wrapped[0], wrapped[1], wrapped[2])
        else:
            logger.warning(f"unexpected plane count {len(wrapped)}")
            return None

    # ==================== Render Processing ====================

    def _process_task(self, task: EditRenderTask) -> None:
        """Process a single render task."""
        from src.shared.media.gpu_frame import (
            is_gpu_decoded_frame, safe_release_gpu_frame,
        )

        w, h = task.output_fbo_size
        if w <= 0 or h <= 0:
            self._release_task_sources(task)
            return
        # P3.2：处理挂起的复合缓存失效（UI 线程 invalidate 置位，GL 线程释放）。
        self._drain_composite_cache_invalidate()
        # AUD-16602：排水退役输出池（尺寸重建后旧纹理延迟释放）。
        self._drain_retired_output_pools()
        # All per-track conversion passes target this preview canvas. Keeping
        # it per task avoids creating source-resolution RGB intermediates.
        self._active_output_size = (w, h)
        self._configure_planar_upload_mode(task, w, h)
        coordinate_size = task.coordinate_size
        if coordinate_size is not None and coordinate_size[0] > 0 and coordinate_size[1] > 0:
            self._active_transform_coordinate_size = (
                int(coordinate_size[0]), int(coordinate_size[1])
            )
        else:
            self._active_transform_coordinate_size = (w, h)

        # P3.2 复合缓存命中：跳过全部解码与合成，直接把缓存纹理拷进
        # present slot 上屏（worker 已在解码前确认命中）。
        if task.cached_composite:
            cached_tex = self._get_cached_composite_tex(
                task.frame, task.generation_id, (w, h)
            )
            if cached_tex is not None and self._ensure_composite_fbo(
                w, h, generation_id=task.generation_id, frame_index=task.frame
            ):
                if self._blit_cached_composite(cached_tex, w, h):
                    playback_trace(
                        "gpu_renderer.composite_cache_hit",
                        frame=task.frame,
                        generation=task.generation_id,
                    )
                    present_frame = self._finalize_present_frame(
                        final_tex=self._composite_tex,
                        frame=task.frame,
                        generation_id=task.generation_id,
                        width=w,
                        height=h,
                        track_count=0,
                    )
                    if present_frame is not None:
                        self.frame_ready.emit(
                            task.frame, task.generation_id,
                            self._composite_tex.glo, w, h,
                        )
                    return
            self._release_task_sources(task)
            # AUD-16601：cached 分支 blit 失败——acquire 的槽位回收。
            self._abandon_current_present_lease()
            self.render_error.emit(task.frame, "composite_cache_miss")
            return

        # Ensure output FBO exists and acquire an ack-safe present slot.
        # Background (idle-prebuffer) tasks must never acquire a present
        # slot or emit render_error into the interactive chain; they
        # composite into a private FBO and only produce a snapshot.
        if task.background:
            if not self._ensure_background_composite_fbo(w, h):
                self._release_task_sources(task)
                return
        elif not self._ensure_composite_fbo(
            w, h, generation_id=task.generation_id, frame_index=task.frame
        ):
            self.render_error.emit(task.frame, "present_slot_unavailable")
            self._release_task_sources(task)
            return

        if not task.layers:
            # AUD-16601：acquire 之后未走到 finalize 的提前返回必须回收
            # WRITING 槽位，否则 24 个 present slot 会被逐帧永久消耗。
            self._abandon_current_present_lease()
            return

        # Render each layer to its own FBO (YUV→RGB conversion)
        track_textures = []
        # FX-C02: parallel list of the track_index (z-order) for each entry in
        # track_textures, captured so the scoped composite can interleave
        # adjustment layers at their correct z-position instead of applying
        # them all as a final full-screen pass.
        track_texture_indices = []
        # P0-PREV-GPU-T：与 track_textures 平行的 clip_id 列表，转场预混合
        # 用它定位左右两侧条目（混合后移除两侧、注入单纹理）。
        track_clip_ids: list = []
        failed_tracks = 0
        # BUG FIX (画面重叠): Track whether this task samples any
        # IOSurface-backed (VT zero-copy) source texture, so we can insert a
        # GL sync barrier before releasing the CVPixelBuffer backing.
        self._task_used_io_surface_source = False
        for layer_data in task.layers:
            gpu_frame = layer_data.get('gpu_frame')
            vt_pixel_buffer = layer_data.get('vt_pixel_buffer')
            decoded_frame = layer_data.get('decoded_frame')
            comp_gpu_handle = layer_data.get('gpu_handle')
            frame_type = layer_data.get('frame_type', '')
            track_info = layer_data.get('track_info', {})

            if comp_gpu_handle is not None or frame_type == 'comp_gpu_handle':
                # TL-P1-01: consume the CompositeGpuFrameHandle directly in
                # the shared GL context. The handle remains leased until the
                # source-read fence at the end of this task, because the
                # producer's texture is not owned by this renderer.
                comp_tex, comp_error = self._prepare_comp_gpu_texture(
                    comp_gpu_handle, task.generation_id,
                )
                if comp_tex is None:
                    try:
                        if comp_gpu_handle is not None:
                            comp_gpu_handle.release()
                    except Exception:
                        pass
                    failed_tracks += 1
                    playback_trace(
                        "gpu_renderer.track_skip_comp_gpu_handle",
                        frame=task.frame,
                        generation=task.generation_id,
                        track_index=len(track_textures) + failed_tracks,
                        reason=comp_error or "unknown",
                    )
                    continue

                opacity = track_info.get('opacity', 1.0)
                track_tex = self._apply_clip_transform(
                    comp_tex, len(track_textures), w, h,
                    track_info.get('transform'),
                )
                track_tex = self._apply_crop(
                    track_tex, len(track_textures), w, h,
                    track_info.get('crop'),
                )
                graded_tex = self._apply_preview_grade(
                    track_tex,
                    len(track_textures),
                    w,
                    h,
                    track_info.get('preview_grade'),
                )
                final_track_tex = self._apply_clip_effects(
                    graded_tex,
                    track_info.get('clip_effects') or (),
                    w,
                    h,
                    source_frame=int(track_info.get('source_frame', 0)),
                    timeline_frame=task.frame,
                )
                track_textures.append(
                    (final_track_tex, opacity, _blend_code_of(track_info))
                )
                track_texture_indices.append(
                    track_info.get('track_index', len(track_textures) - 1)
                )
                track_clip_ids.append(track_info.get('clip_id', ''))
                # A CompositeGpuFrameHandle is not a GpuDecodedFrame, so it
                # must be retained here and released by the generalized
                # source-release path below.
                self._previous_gpu_frames.append(comp_gpu_handle)
                self._task_used_io_surface_source = True
                playback_trace(
                    "gpu_renderer.comp_gpu_handle_consumed",
                    frame=task.frame,
                    generation=task.generation_id,
                    texture_id=int(getattr(comp_gpu_handle, 'texture_id', 0) or 0),
                )
                continue

            cuda_resident = layer_data.get('cuda_resident')
            if cuda_resident is not None and frame_type == 'cuda_resident':
                # B2.1：Windows cuda 驻留帧——本 GL 线程导入为常驻纹理对，
                # 失败按 track_skip 显式回退（不交付半帧）。
                # W03：asset_path/generation 随层传递，导入失败按素材上报
                # （decode manager 停该素材驻留路由并重建下载/CPU 源）。
                gpu_frame = self._import_cuda_resident_frame(
                    cuda_resident,
                    asset_path=str(layer_data.get('asset_path') or ''),
                    generation_id=int(getattr(task, 'generation_id', 0) or 0),
                )
                if gpu_frame is None:
                    failed_tracks += 1
                    playback_trace(
                        "gpu_renderer.track_skip_cuda_import_failed",
                        frame=task.frame,
                        track_index=len(track_textures) + failed_tracks,
                    )
                    continue
                frame_type = 'gpu'

            if vt_pixel_buffer is not None and frame_type == 'vt_pixel_buffer':
                gpu_frame = self._import_vt_pixel_buffer_frame(vt_pixel_buffer)
                if gpu_frame is None:
                    failed_tracks += 1
                    playback_trace(
                        "gpu_renderer.track_skip_vt_import_failed",
                        frame=task.frame,
                        track_index=len(track_textures) + failed_tracks,
                    )
                    continue
                frame_type = 'gpu'

            if gpu_frame is not None and frame_type == 'gpu':
                # GPU zero-copy path: external texture planes
                if not is_gpu_decoded_frame(gpu_frame):
                    safe_release_gpu_frame(gpu_frame)
                    failed_tracks += 1
                    playback_trace(
                        "gpu_renderer.track_skip_not_gpu",
                        frame=task.frame,
                        track_index=len(track_textures) + failed_tracks,
                        reason="not_gpu_decoded_frame",
                    )
                    continue

                wrapped = self._wrap_external_gpu_planes(gpu_frame.planes, gpu_frame)
                if wrapped is None:
                    safe_release_gpu_frame(gpu_frame)
                    failed_tracks += 1
                    playback_trace(
                        "gpu_renderer.track_skip_wrap_failed",
                        frame=task.frame,
                        track_index=len(track_textures) + failed_tracks,
                        reason="wrap_external_planes_failed",
                    )
                    continue

                track_fbo, track_tex = self._ensure_track_fbo(
                    len(track_textures), w, h)
                # clip TransformParams (7-tuple) from track_info → deforms the
                # YUV→track-FBO quad in NDC space. Identity when absent.
                track_transform = track_info.get('transform')
                self._render_yuv_to_track_fbo(
                    wrapped, gpu_frame, track_fbo, track_tex, track_transform)

                # Mark that this task sampled IOSurface-backed textures so
                # the release path knows to insert a GL sync barrier before
                # freeing the CVPixelBuffer backing.
                decode_path = getattr(gpu_frame, 'decode_path', '') or ''
                if getattr(gpu_frame, 'zero_copy', False) or 'iosurface' in decode_path or 'vt' in decode_path:
                    self._task_used_io_surface_source = True

                # FX-M06: apply the clip's crop matte after the source is on
                # the track FBO, before grading/effects. Identity crops skip.
                track_tex = self._apply_crop(
                    track_tex, len(track_textures), w, h,
                    track_info.get('crop'),
                )

                opacity = track_info.get('opacity', 1.0)
                graded_tex = self._apply_preview_grade(
                    track_tex,
                    len(track_textures),
                    w,
                    h,
                    track_info.get('preview_grade'),
                )
                # Apply the clip's video filter chain (blur / sharpen / ...)
                # after grading, before multi-track compositing. No-op when
                # the clip has no effects or the runner is unavailable.
                final_track_tex = self._apply_clip_effects(
                    graded_tex,
                    track_info.get('clip_effects') or (),
                    w,
                    h,
                    source_frame=int(track_info.get('source_frame', 0)),
                    timeline_frame=task.frame,
                )
                track_textures.append((final_track_tex, opacity, _blend_code_of(track_info)))
                track_texture_indices.append(track_info.get('track_index', len(track_textures) - 1))
                track_clip_ids.append(track_info.get('clip_id', ''))
                self._previous_gpu_frames.append(gpu_frame)

                playback_trace(
                    "gpu_renderer.track_ok",
                    frame=task.frame,
                    track_index=len(track_textures) - 1,
                    frame_type='gpu',
                )

            elif decoded_frame is not None and frame_type in ('planar', 'vt_planar'):
                # Planar YUV path: upload planes to GPU, then render with shader.
                # vt_planar keeps VideoToolbox hardware decode visible in metrics.
                _upload_stats: dict = {}
                result = self._upload_and_render_planar(
                    decoded_frame, len(track_textures), track_info=track_info,
                    stats_out=_upload_stats)
                if result is not None:
                    if _upload_stats:
                        self._store_planar_upload_stats(int(task.frame), _upload_stats)
                    track_tex, fw, fh = result
                    # Apply clip transform (planar path doesn't go through the
                    # YUV vertex shader that has u_transform built-in).
                    track_transform = track_info.get('transform')
                    track_tex = self._apply_clip_transform(
                        track_tex, len(track_textures), w, h, track_transform)
                    # FX-M06: crop matte after transform, before grade.
                    track_tex = self._apply_crop(
                        track_tex, len(track_textures), w, h,
                        track_info.get('crop'),
                    )
                    opacity = track_info.get('opacity', 1.0)
                    graded_tex = self._apply_preview_grade(
                        track_tex,
                        len(track_textures),
                        w,
                        h,
                        track_info.get('preview_grade'),
                    )
                    # Same effect-chain hook as the GPU zero-copy path above.
                    final_track_tex = self._apply_clip_effects(
                        graded_tex,
                        track_info.get('clip_effects') or (),
                        w,
                        h,
                        source_frame=int(track_info.get('source_frame', 0)),
                    timeline_frame=task.frame,
                    )
                    track_textures.append((final_track_tex, opacity, _blend_code_of(track_info)))
                    track_texture_indices.append(track_info.get('track_index', len(track_textures) - 1))
                    track_clip_ids.append(track_info.get('clip_id', ''))

                    playback_trace(
                        "gpu_renderer.track_ok",
                        frame=task.frame,
                        track_index=len(track_textures) - 1,
                        frame_type=frame_type,
                    )
                else:
                    failed_tracks += 1
                    pixel_format = getattr(decoded_frame, 'pixel_format', '?')
                    playback_trace(
                        "gpu_renderer.track_skip_upload_failed",
                        frame=task.frame,
                        track_index=len(track_textures) + failed_tracks,
                        reason="planar_upload_failed",
                        pixel_format=pixel_format,
                    )
                    logger.warning(
                        f"Planar upload failed for frame {task.frame}, "
                        f"pixel_format={pixel_format}"
                    )

            elif frame_type == 'solid' and track_info.get('is_solid'):
                # Solid Color generator: paint the track FBO with the asset's
                # colour (or procedural pattern), then run the standard grade +
                # clip-effects + composite path so a solid behaves like any
                # other video layer (supports opacity, can be graded/filtered,
                # participates in occlusion).
                solid_rgb = track_info.get('solid_color') or (0.0, 0.6, 0.8)
                gen_params = track_info.get('generator_params')
                track_fbo, track_tex = self._paint_solid_to_track_fbo(
                    len(track_textures), w, h, solid_rgb, gen_params
                )
                # Apply clip transform (solid path also bypasses YUV vertex shader).
                track_transform = track_info.get('transform')
                track_tex = self._apply_clip_transform(
                    track_tex, len(track_textures), w, h, track_transform)
                # FX-M06: crop matte after transform, before grade.
                track_tex = self._apply_crop(
                    track_tex, len(track_textures), w, h,
                    track_info.get('crop'),
                )
                opacity = track_info.get('opacity', 1.0)
                graded_tex = self._apply_preview_grade(
                    track_tex,
                    len(track_textures),
                    w,
                    h,
                    track_info.get('preview_grade'),
                )
                final_track_tex = self._apply_clip_effects(
                    graded_tex,
                    track_info.get('clip_effects') or (),
                    w,
                    h,
                    source_frame=int(track_info.get('source_frame', 0)),
                    timeline_frame=task.frame,
                )
                track_textures.append((final_track_tex, opacity, _blend_code_of(track_info)))
                track_texture_indices.append(track_info.get('track_index', len(track_textures) - 1))
                track_clip_ids.append(track_info.get('clip_id', ''))
                playback_trace(
                    "gpu_renderer.track_ok",
                    frame=task.frame,
                    track_index=len(track_textures) - 1,
                    frame_type='solid',
                )

            else:
                failed_tracks += 1
                playback_trace(
                    "gpu_renderer.track_skip_unsupported",
                    frame=task.frame,
                    track_index=len(track_textures) + failed_tracks,
                    reason=f"unsupported_type:{frame_type}",
                )
                continue

        # Fix 1: If any track failed, don't show an incomplete composite.
        # Emit error so the caller can fall back to CPU path instead.
        if not track_textures:
            # AUD-16601：回收本任务已 acquire 的 present 槽位。
            self._abandon_current_present_lease()
            if task.background:
                self._release_previous_frames()
                return
            self.render_error.emit(task.frame, "No GPU frames rendered")
            self._release_previous_frames()
            return

        if failed_tracks > 0:
            logger.warning(
                f"GPU render incomplete: {len(track_textures)}/{len(task.layers)} "
                f"tracks for frame {task.frame} ({failed_tracks} failed)"
            )
            # AUD-16601：回收本任务已 acquire 的 present 槽位。
            self._abandon_current_present_lease()
            self._release_previous_frames()
            if task.background:
                return
            self.render_error.emit(task.frame, "gpu_track_render_incomplete")
            return

        # P0-PREV-GPU-T：所有双源转场在 GPU 纹理域预混合；任务只携带
        # pixel-free spec，不允许 CPU 逐像素 mask 或中间帧进入此处。
        transition_error = self._preblend_transition_tracks(
            task, track_textures, track_clip_ids, track_texture_indices, w, h,
        )
        if transition_error is not None:
            # AUD-16601：回收本任务已 acquire 的 present 槽位。
            self._abandon_current_present_lease()
            self._release_previous_frames()
            if task.background:
                return
            self.render_error.emit(task.frame, transition_error)
            return
        # dip_to_color 任务（spec=None）在合成域内/后处理；双源任务已被
        # 预混合消费。
        dip_jobs = tuple(
            (trans, spec) for trans, spec in (task.transition_jobs or ())
            if spec is None and trans.transition_type == "dip_to_color"
        )

        # P0-03: GL timer query lazy 创建（GL 线程 ctx current）+ 本帧测量。
        if self._gl_pass_timer is None:
            try:
                from src.shared.gpu.gl_pass_timer import GlPassTimer
                self._gl_pass_timer = GlPassTimer(self.ctx)
            except Exception:
                self._gl_pass_timer = None
        _timer = self._gl_pass_timer
        _use_gpu_timer = _timer is not None and not _timer.unsupported
        if _use_gpu_timer:
            # A0.1：tag=timeline frame——query 完成读回时据此归因到发起帧。
            _timer.begin_frame(tag=int(task.frame))

        def _do_composite():
            # FX-C02: when Adjustment Clips are present, composite with SCOPE —
            # each adjustment's effect chain is applied to the accumulated buffer
            # at the adjustment's own z-position (only the layers below it), never
            # to the layers above. The old path composited ALL tracks first and
            # then ran adjustments as a full-screen post pass, so an adjustment
            # leaked into the tracks above it. When there are no adjustments the
            # scoped path reduces to the same bottom-up blend, but we keep the
            # dedicated fast path (_composite_tracks) for the common no-adjustment
            # case to avoid any behaviour change there.
            if task.adjustment_layers:
                ftex = self._composite_with_adjustment_scope(
                    track_textures, track_texture_indices,
                    task.adjustment_layers, w, h, task.frame,
                )
                if ftex is not None and dip_jobs:
                    ftex = self._apply_dip_post_pass(
                        ftex, dip_jobs, w, h,
                    )
                return ftex
            return self._composite_tracks(
                track_textures, w, h, dip_jobs=dip_jobs,
            )

        if _use_gpu_timer:
            with _timer.measure("composite"):
                final_tex = _do_composite()
            # P0-03: 读回 RING_DEPTH-1 帧前的到期 query（无同步等待）。
            # A0.1：完成结果（含发起帧 tag）暂存，由 TPR 的 GL 完成回调
            # drain 后按发起帧 trace_id 回填——本帧不得直接取用旧值。
            _completed = _timer.collect()
            if _completed:
                _pending = getattr(self, '_completed_gpu_queries', None)
                if _pending is None:
                    from collections import deque
                    _pending = deque(maxlen=16)
                    self._completed_gpu_queries = _pending
                _pending.extend(_completed)
        else:
            final_tex = _do_composite()

        if final_tex is not None:
            # P3.2：暂停/拖拽/后台任务的合成结果存入复合纹理缓存（实时
            # 播放不存，顺序流会冲刷 LRU 且几乎不重访）。
            if (task.cacheable or task.background) and not task.cached_composite:
                self._store_composite_cache(
                    final_tex, w, h, task.frame, task.generation_id,
                )
            # Idle-prebuffer task: capture the cache snapshot only. No present
            # slot, no fence, no frame_ready — the viewer never shows it.
            if task.background:
                self._capture_frame_snapshot(
                    final_tex, w, h, task.frame, task.generation_id,
                    kind="background_snapshot",
                )
                self._release_previous_frames()
                return
            # 合成快照（seek 快速路径）：UI 线程布防后捕获本帧
            if self._snapshot_armed:
                self._snapshot_armed = False
                self._capture_frame_snapshot(
                    final_tex, w, h, task.frame, task.generation_id,
                    kind="seek_snapshot",
                )
            # Batch 1 (缺陷计划 RP-P0-01): 正式 fence + slot ready 协议。
            # 替代临时 glFinish() 止血——用 glFenceSync 让 Viewer 在自己的
            # context 上 glWaitSync 等待 renderer 写完，不阻塞 GPU pipeline。
            present_frame = self._finalize_present_frame(
                final_tex=final_tex,
                frame=task.frame,
                generation_id=task.generation_id,
                width=w,
                height=h,
                track_count=len(track_textures),
            )

            if present_frame is not None:
                # 发射 GpuPresentFrame（携带 fence + slot_id）。
                # 旧 frame_ready 信号仍传裸 (frame, gen, tex_id, w, h) 以兼容
                # 现有 _on_gpu_frame_ready；GpuPresentFrame 通过 present_frame_ready
                # 信号传递（Batch 3 将统一为单一入口）。
                self.frame_ready.emit(
                    task.frame, task.generation_id, final_tex.glo, w, h,
                )
            else:
                # fence 失败：回退到 glFinish 保证正确性（rare path）。
                try:
                    from OpenGL.GL import glFinish
                    glFinish()
                except Exception:
                    pass
                playback_trace(
                    "gpu_renderer.present_fence_failed_fallback_finish",
                    frame=task.frame,
                )
                self.frame_ready.emit(
                    task.frame, task.generation_id, final_tex.glo, w, h,
                )
        else:
            # AUD-16601：合成未产出纹理（_do_composite 返回 None）——回收
            # 已 acquire 的 present 槽位，不让 WRITING 槽位泄漏。
            self._abandon_current_present_lease()

        # Release previous GPU frames (keep current for display)
        self._release_previous_frames()

    def _configure_planar_upload_mode(
        self,
        task: EditRenderTask,
        output_width: int,
        output_height: int,
    ) -> None:
        """Enable PBO uploads only where their pipeline overlap pays off.

        The normal 1080p planar path is already fast with direct uploads.  A
        4K CPU-decoded intra frame, however, transfers tens of megabytes of
        YUV data per visible layer.  For that case a small PBO ring lets the
        driver overlap DMA with the following conversion/composite passes.
        This never changes the timeline/FBO output size.
        """
        uploader = self._yuv_uploader
        configure = getattr(uploader, "enable_pbo", None)
        if not callable(configure):
            return

        output_pixels = max(1, int(output_width)) * max(1, int(output_height))
        large_planar_layers = 0
        for layer_data in getattr(task, "layers", ()) or ():
            if not isinstance(layer_data, dict):
                continue
            decoded = layer_data.get("decoded_frame")
            if decoded is None or not getattr(decoded, "is_planar", False):
                continue
            source_pixels = max(0, int(getattr(decoded, "width", 0))) * max(
                0, int(getattr(decoded, "height", 0))
            )
            if source_pixels >= output_pixels * 2:
                large_planar_layers += 1

        enabled = large_planar_layers > 0
        # One ring slot per simultaneously visible large plane set avoids
        # immediate reuse during 2-3 track ProRes composites. Three is enough
        # for the supported realtime preview track count and bounds memory.
        slots = 3 if large_planar_layers >= 2 else 2
        desired_mode = (enabled, slots)
        if desired_mode == self._planar_upload_pbo_mode:
            return
        try:
            configure(enabled, num_slots=slots)
            self._planar_upload_pbo_mode = desired_mode
        except Exception as error:
            logger.debug("EditGpuPreviewRenderer: PBO mode change failed: %s", error)

    def _render_yuv_to_track_fbo(self, wrapped_planes, gpu_frame, track_fbo, track_tex,
                                 transform_tuple=None) -> None:
        """Render YUV planes to an RGB track FBO.

        Args:
            transform_tuple: Optional clip TransformParams 7-tuple. When given
                (and non-identity) the track-FBO quad is deformed in NDC space
                by ``u_transform`` (see ``_transform_to_ndc_mat3_bytes``).
        """
        track_fbo.use()
        self.ctx.viewport = (0, 0, track_tex.size[0], track_tex.size[1])
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        # Bind external Y and UV planes
        import moderngl
        y_plane = wrapped_planes[0]
        uv_plane = wrapped_planes[1] if len(wrapped_planes) >= 2 else None

        if uv_plane is None:
            logger.warning("No UV plane in wrapped external textures")
            return

        from OpenGL.GL import GL_TEXTURE_2D, GL_TEXTURE_RECTANGLE
        use_rectangle = (
            getattr(y_plane, 'target', GL_TEXTURE_2D) == GL_TEXTURE_RECTANGLE
            or getattr(uv_plane, 'target', GL_TEXTURE_2D) == GL_TEXTURE_RECTANGLE
        )
        program = self._yuv_rect_program if use_rectangle else self._yuv_program
        vao = self._yuv_rect_vao if use_rectangle else self._yuv2_vao
        if program is None or vao is None:
            logger.warning("YUV shader program is not initialized")
            return

        # Set uniforms
        color_range = 0 if getattr(gpu_frame, 'color_range', 'limited') == 'limited' else 1
        # 内联 shader 编号：0=bt601, 1=bt709, 2=bt2020。
        # gpu_frame.normalize_color_matrix 会输出 smpte170m/bt470bg/bt2020nc
        # 等规范名，必须全部映射，否则 BT.601 素材会落到默认 bt709 分支。
        color_matrix_map = {
            'bt601': 0, 'smpte170m': 0, 'bt470bg': 0, 'fcc': 0,
            'bt709': 1,
            'bt2020': 2, 'bt2020nc': 2,
        }
        color_matrix = color_matrix_map.get(getattr(gpu_frame, 'color_matrix', 'bt709'), 1)
        uv_component_mode = 1 if getattr(uv_plane, 'internal_format', '') == 'GL_LUMINANCE_ALPHA' else 0

        # NOTE: moderngl 5.12 never writes sampler2DRect uniform values to GL
        # (verified via glGetUniformiv; sampler2D works). Use raw glUniform1i
        # for sampler uniforms on both rect and 2D YUV programs.
        from OpenGL.GL import glUseProgram, glUniform1i, glGetUniformLocation
        glUseProgram(program.glo)
        glUniform1i(glGetUniformLocation(program.glo, 'u_y_plane'), 0)
        glUniform1i(glGetUniformLocation(program.glo, 'u_uv_plane'), 1)
        program['u_color_range'] = color_range
        program['u_color_matrix'] = color_matrix
        program['u_uv_component_mode'] = uv_component_mode

        # Apply clip transform (translate/scale/rotation/pivot) to the quad.
        # Identity mat3 when no transform or identity tuple → no-op visually.
        # Pass both the output FBO size and native canvas size so pixel-space
        # translate/pivot convert to NDC without proxy-resolution scaling.
        # Every track FBO is flipped once during final presentation.  Therefore
        # its local transform must use F·M·F to preserve composite-page Y-down
        # move/clockwise-rotation semantics on screen.
        tw, th = track_tex.size[0], track_tex.size[1]
        coordinate_w, coordinate_h = self._active_transform_coordinate_size
        mat3_bytes = (_transform_to_ndc_mat3_bytes(
                          transform_tuple, tw, th,
                          track_fbo_y_flipped=True,
                          coordinate_width=coordinate_w,
                          coordinate_height=coordinate_h)
                      if transform_tuple else _IDENTITY_MAT3_BYTES)
        try:
            program['u_transform'].write(mat3_bytes)
        except (KeyError, AttributeError):
            # Program compiled without u_transform (e.g. older shader) — skip.
            pass

        if use_rectangle:
            program['u_y_size'].value = (float(y_plane.size[0]), float(y_plane.size[1]))
            program['u_uv_size'].value = (float(uv_plane.size[0]), float(uv_plane.size[1]))

        from OpenGL.GL import glActiveTexture, glBindTexture, GL_TEXTURE0

        glActiveTexture(GL_TEXTURE0)
        glBindTexture(getattr(y_plane, 'target', GL_TEXTURE_2D), y_plane.glo)
        glActiveTexture(GL_TEXTURE0 + 1)
        glBindTexture(getattr(uv_plane, 'target', GL_TEXTURE_2D), uv_plane.glo)

        # Render fullscreen quad using the 2-plane YUV VAO
        vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)

    def _render_cfa_frame(self, decoded_frame, planes, track_fbo,
                          track_info: dict) -> bool:
        """TRAW CFA 4 相位平面 → CfaGpuDebayer → track FBO（批 2 接线）。"""
        try:
            from src.shared.video.cfa_gpu_debayer import CfaGpuDebayer
        except Exception as e:  # pragma: no cover - GL 库缺失环境
            logger.warning(f"CFA debayer unavailable: {e}")
            return False
        if len(planes) != 4:
            return False
        # 位深判定（2026-09-13 复查加固）：优先 plane_infos[0].bit_depth
        # （IDSC 透传），缺失时按 pixel_format 名兜底（topos_cfa16le → 16）；
        # 两者都缺才默认 12——避免 16-bit 数据静默走 12-bit LOG0 曲线
        bit_depth = 12
        if getattr(decoded_frame, 'plane_infos', None):
            bit_depth = int(getattr(decoded_frame.plane_infos[0], 'bit_depth', 12) or 12)
        else:
            fmt_name = str(getattr(decoded_frame, 'pixel_format', '') or '')
            if 'cfa16' in fmt_name:
                bit_depth = 16
                logger.debug("CFA frame without plane_infos: bit_depth from "
                             f"format name {fmt_name!r}")
        if self._cfa_debayer is None or getattr(self, '_cfa_debayer_bd', None) != bit_depth:
            if self._cfa_debayer is not None:
                self._cfa_debayer.release()
            self._cfa_debayer = CfaGpuDebayer(self.ctx, bit_depth=bit_depth)
            self._cfa_debayer_bd = bit_depth
        raw = track_info.get('raw_params')
        trwm = (getattr(decoded_frame, 'extra', None) or {}).get(
            'topos_trwm')
        # RC3/RC6：RAW 参数三链同源解析（用户 clip RawParams > TRWM
        # as-shot > 单位默认；gamma/gamut 同点折算）——见
        # camera_gamut_matrices.resolve_debayer_params。
        from src.shared.video.camera_gamut_matrices import (
            matrix3_column_major,
            resolve_debayer_params,
        )
        rp = resolve_debayer_params(raw, trwm)
        if not rp['explicit_gamut'] and not getattr(
                self, '_cfa_gamut_unknown_logged', False):
            logger.info(
                "TRAW CFA：未指定相机色空间（无 RAW 面板选择且 TRWM 无 "
                "sensor_matrix_id）——按 camera-native identity 直出")
            self._cfa_gamut_unknown_logged = True
        ph, pw = planes[0].shape[:2]
        # 帧尺寸优先取元数据全尺寸声明（奇数分辨率时 pw*2 会多出一行/
        # 列；topos_source pf=3 语义 width/height = 全帧，平面 = ceil(W/2)）
        fw = int(getattr(decoded_frame, 'width', 0) or 0) or (int(pw) * 2)
        fh = int(getattr(decoded_frame, 'height', 0) or 0) or (int(ph) * 2)
        self._cfa_debayer.upload_and_render(
            planes, fw, fh, track_fbo,
            wb_gains=rp['wb_gains'],
            exposure_stops=rp['exposure_stops'],
            display_ootf=rp['gamma_mode'] == 1,
            black_level=rp['black_level'],
            gamut_matrix=matrix3_column_major(rp['matrix']),
            gamma_mode=rp['gamma_mode'])
        return True

    def _upload_and_render_planar(self, decoded_frame, track_index: int,
                                  track_info: Optional[dict] = None,
                                  stats_out: Optional[dict] = None):
        """
        Upload CPU planar YUV planes to GPU and render to track FBO.

        Uses YUVGPUUploader (same as Color page) to upload YUV planes,
        then uses YUVGPUUploader.render_yuv_to_rgb() for 3-plane formats,
        or the built-in 2-plane shader for NV12/P010.

        重要：YUVGPUUploader 内部用不翻转 quad（pos bottom-left → UV 0,0），
        渲染到 track FBO 后方向是"图像顶部=FBO 底部"。但 IOSurface 零拷贝路径
        （_render_yuv_to_track_fbo）用翻转 quad，方向是"图像顶部=FBO 顶部"。
        为了让 blend 合成时两条路径方向一致，_upload_and_render_planar 在
        YUVGPUUploader 渲染后额外做一次翻转 blit，把 track FBO 方向纠正为
        "图像顶部=FBO 顶部"（和 IOSurface 路径一致）。

        Args:
            decoded_frame: DecodedFrame with planar YUV data
            track_index: Track slot index for FBO allocation
            stats_out: 可选累积 dict（T-03/P0-04 per-frame 证据）：成功时
                累加 bytes/ms 并记录 mode/pixel_format，供 GL 完成回调
                合并进帧 trace。

        Returns:
            (track_tex, width, height) or None on failure
        """
        if self._yuv_uploader is None:
            return None

        if not getattr(decoded_frame, 'is_planar', False):
            return None

        planes = decoded_frame.planes
        if not planes or len(planes) < 2:
            return None

        width = decoded_frame.width
        height = decoded_frame.height
        pixel_format = getattr(decoded_frame, 'pixel_format', '')
        frame_idx = getattr(decoded_frame, 'frame_index', 0)

        # Determine bit depth
        bit_depth = 8
        if hasattr(decoded_frame, 'plane_infos') and decoded_frame.plane_infos:
            bit_depth = decoded_frame.plane_infos[0].bit_depth

        # YUVA 第四平面：Alpha 位深独立于 luma（Topos a8/a10/a12/a16 vs 10），
        # 从 plane_infos[3] 取；形状不符或缺失则按不透明渲染。CFA（pf=3）
        # 第 4 平面是 B 相位面（半尺寸）而非 alpha——跳过，免每帧噪音日志。
        _planar_t0 = time.perf_counter()
        _fmt_probe = str(getattr(decoded_frame, 'pixel_format', '') or '').lower()
        _is_cfa_frame = 'cfa' in _fmt_probe
        alpha_plane = None
        alpha_bit_depth = bit_depth
        alpha_premult = False
        if len(planes) >= 4 and not _is_cfa_frame:
            candidate = planes[3]
            if candidate.ndim == 2 and candidate.shape[:2] == (height, width):
                alpha_plane = candidate
                if decoded_frame.plane_infos and len(decoded_frame.plane_infos) >= 4:
                    alpha_bit_depth = int(
                        getattr(decoded_frame.plane_infos[3], 'bit_depth', bit_depth)
                        or bit_depth)
                alpha_premult = bool(
                    (getattr(decoded_frame, 'extra', None) or {}).get(
                        'topos_alpha_premultiplied', False))
            else:
                logger.debug(
                    f"Planar alpha plane shape mismatch: {candidate.shape[:2]} "
                    f"!= {(height, width)}, rendering opaque")

        # Color matrix/range from frame metadata (for YUVGPUUploader config).
        # YUVGPUUploader 编号：0=BT.709, 1=BT.2020, 2=BT.601。
        # 旧版误用内联 shader 编号（0=bt601, 1=bt709, 2=bt2020），导致
        # BT.709 内容被按 BT.2020 矩阵解码（R 降 G 升，肤色蜡黄）。
        cm_map = {
            'bt709': 0,
            'bt2020': 1, 'bt2020nc': 1,
            'bt601': 2, 'smpte170m': 2, 'bt470bg': 2, 'fcc': 2,
        }
        color_matrix_tag = _decoded_frame_color_tag(
            decoded_frame, 'color_matrix', 'bt709')
        color_range_tag = _decoded_frame_color_tag(
            decoded_frame, 'color_range', 'limited')
        color_matrix = cm_map.get(color_matrix_tag, 0)
        color_range = 1 if color_range_tag in ('full', 'pc', 'jpeg') else 0

        # The uploader has its own enum order (BT.709=0, BT.2020=1,
        # BT.601=2), which is different from the inline zero-copy shader.
        # Apply the per-frame metadata before every planar render; merely
        # computing these values is not enough because the uploader keeps its
        # previous frame's state otherwise.
        self._yuv_uploader.set_yuv_matrix(color_matrix)
        self._yuv_uploader.set_color_range(color_range)

        # Render directly into the preview canvas. A 4K source must not first
        # create a 4K RGB intermediate only to be downscaled for a 1080p viewer.
        # Texture sampling performs the scale in this YUV -> RGB pass.
        output_size = getattr(self, '_active_output_size', (width, height))
        track_width = max(1, int(output_size[0]))
        track_height = max(1, int(output_size[1]))
        track_fbo, track_tex = self._ensure_track_fbo(
            track_index, track_width, track_height)

        # Upload YUV planes to GPU textures and render via YUVGPUUploader
        #
        # Format routing strategy:
        #   1. 2-plane formats (NV12/P010/P016) — Y + interleaved UV
        #   2. 3-plane YUV by chroma subsampling: 444, 422, 420
        #   3. RGB planar (gbrp*) — treated as 3-plane with identity matrix
        #   4. Fallback: try generic 3-plane upload
        rendered_ok = False
        try:
            upload_mode = _classify_planar_format(pixel_format, len(planes), bit_depth)

            if upload_mode == 'cfa':
                # TRAW CFA：4 相位平面 R/Gr/Gb/B → GPU debayer 直渲 track FBO。
                # WB/EI/OOTF 为非破坏 uniform（track_info['raw_params']，
                # 拖动零重解码）；OOTF 默认开（预览语义）。
                rendered_ok = self._render_cfa_frame(
                    decoded_frame, planes, track_fbo, track_info or {})

            elif upload_mode == 'nv12':
                y_tex, uv_tex = self._yuv_uploader.upload_nv12(
                    planes[0], planes[1], frame_idx, bit_depth)
                self._yuv_uploader.render_nv12_to_rgb(
                    y_tex, uv_tex, track_fbo, bit_depth=bit_depth)
                rendered_ok = True

            elif upload_mode == 'p010':
                y_tex, uv_tex = self._yuv_uploader.upload_p010(
                    planes[0], planes[1], frame_idx)
                self._yuv_uploader.render_nv12_to_rgb(
                    y_tex, uv_tex, track_fbo, bit_depth=10)
                rendered_ok = True

            elif upload_mode == 'p016':
                # bit_depth 必须贯通到上传（float 回退路径在上传时按
                # (1<<16)-1 归一化；R16UI 直传原始码值、渲染时归一化）
                y_tex, uv_tex = self._yuv_uploader.upload_p010(
                    planes[0], planes[1], frame_idx, bit_depth=16)
                self._yuv_uploader.render_nv12_to_rgb(
                    y_tex, uv_tex, track_fbo, bit_depth=16)
                rendered_ok = True

            elif upload_mode == 'yuv422':
                texes = self._yuv_uploader.upload_yuv422(
                    planes[0], planes[1], planes[2], frame_idx, bit_depth,
                    a=alpha_plane, alpha_bit_depth=alpha_bit_depth)
                self._yuv_uploader.render_yuv_to_rgb(
                    texes[0], texes[1], texes[2], track_fbo, bit_depth=bit_depth,
                    a_tex=texes[3] if len(texes) >= 4 else None,
                    alpha_bit_depth=alpha_bit_depth,
                    alpha_premultiplied=alpha_premult)
                rendered_ok = True

            elif upload_mode == 'yuv444':
                texes = self._yuv_uploader.upload_yuv444(
                    planes[0], planes[1], planes[2], frame_idx, bit_depth,
                    a=alpha_plane, alpha_bit_depth=alpha_bit_depth)
                self._yuv_uploader.render_yuv_to_rgb(
                    texes[0], texes[1], texes[2], track_fbo, bit_depth=bit_depth,
                    a_tex=texes[3] if len(texes) >= 4 else None,
                    alpha_bit_depth=alpha_bit_depth,
                    alpha_premultiplied=alpha_premult)
                rendered_ok = True

            elif upload_mode == 'yuv420':
                texes = self._yuv_uploader.upload_yuv420(
                    planes[0], planes[1], planes[2], frame_idx, bit_depth,
                    a=alpha_plane, alpha_bit_depth=alpha_bit_depth)
                self._yuv_uploader.render_yuv_to_rgb(
                    texes[0], texes[1], texes[2], track_fbo, bit_depth=bit_depth,
                    a_tex=texes[3] if len(texes) >= 4 else None,
                    alpha_bit_depth=alpha_bit_depth,
                    alpha_premultiplied=alpha_premult)
                rendered_ok = True

            elif upload_mode == 'yuv440':
                # YUV440 — rare, treat as YUV444 (nearest-neighbor chroma)
                texes = self._yuv_uploader.upload_yuv444(
                    planes[0], planes[1], planes[2], frame_idx, bit_depth,
                    a=alpha_plane, alpha_bit_depth=alpha_bit_depth)
                self._yuv_uploader.render_yuv_to_rgb(
                    texes[0], texes[1], texes[2], track_fbo, bit_depth=bit_depth,
                    a_tex=texes[3] if len(texes) >= 4 else None,
                    alpha_bit_depth=alpha_bit_depth,
                    alpha_premultiplied=alpha_premult)
                rendered_ok = True

            elif upload_mode == 'yuv411':
                # YUV411 — very rare, treat as YUV420
                texes = self._yuv_uploader.upload_yuv420(
                    planes[0], planes[1], planes[2], frame_idx, bit_depth,
                    a=alpha_plane, alpha_bit_depth=alpha_bit_depth)
                self._yuv_uploader.render_yuv_to_rgb(
                    texes[0], texes[1], texes[2], track_fbo, bit_depth=bit_depth,
                    a_tex=texes[3] if len(texes) >= 4 else None,
                    alpha_bit_depth=alpha_bit_depth,
                    alpha_premultiplied=alpha_premult)
                rendered_ok = True

            elif upload_mode == 'rgb_planar':
                # gbrp*/gbrap* — planes are G, B, R in native Topos order.
                # Upload in native order (y_tex=G, u_tex=B, v_tex=R) and let
                # the shader bypass the YUV matrix (gbr_mode: rgb=vec3(v,y,u));
                # 走 YUV 矩阵渲染 GBR 平面会得到错误颜色（T-03 修正，
                # 与 color 端 async_renderer 的规范用法一致）。
                if planes[0].dtype == np.float16:
                    # M1-F4：浮点样本域（'gbrph16le'）half-float 纹理直传——
                    # 不做整数归一化（R16UI/normalize 两路径都会错标线性值）；
                    # shader 同样走 gbr_mode 旁路，采样值即线性 RGB。
                    texes = self._yuv_uploader.upload_half_planar(
                        planes[0], planes[1], planes[2], frame_idx)
                    self._yuv_uploader.render_yuv_to_rgb(
                        texes[0], texes[1], texes[2], track_fbo, bit_depth=16,
                        a_tex=None, alpha_bit_depth=None,
                        alpha_premultiplied=False,
                        gbr_mode=True)
                    rendered_ok = True
                else:
                    texes = self._yuv_uploader.upload_yuv444(
                        planes[0], planes[1], planes[2], frame_idx, bit_depth,
                        a=alpha_plane, alpha_bit_depth=alpha_bit_depth)
                    self._yuv_uploader.render_yuv_to_rgb(
                        texes[0], texes[1], texes[2], track_fbo, bit_depth=bit_depth,
                        a_tex=texes[3] if len(texes) >= 4 else None,
                        alpha_bit_depth=alpha_bit_depth,
                        alpha_premultiplied=alpha_premult,
                        gbr_mode=True)
                    rendered_ok = True

            else:
                # Unknown: try 3-plane YUV420 as safest default
                if len(planes) >= 3:
                    logger.debug(f"Unknown planar format {pixel_format}, trying 3-plane upload")
                    texes = self._yuv_uploader.upload_yuv420(
                        planes[0], planes[1], planes[2], frame_idx, bit_depth,
                        a=alpha_plane, alpha_bit_depth=alpha_bit_depth)
                    self._yuv_uploader.render_yuv_to_rgb(
                        texes[0], texes[1], texes[2], track_fbo, bit_depth=bit_depth,
                        a_tex=texes[3] if len(texes) >= 4 else None,
                        alpha_bit_depth=alpha_bit_depth,
                        alpha_premultiplied=alpha_premult)
                    rendered_ok = True
                elif len(planes) == 2:
                    logger.debug(f"Unknown 2-plane format {pixel_format}, trying NV12")
                    y_tex, uv_tex = self._yuv_uploader.upload_nv12(
                        planes[0], planes[1], frame_idx, bit_depth)
                    self._yuv_uploader.render_nv12_to_rgb(
                        y_tex, uv_tex, track_fbo, bit_depth=bit_depth)
                    rendered_ok = True
                else:
                    logger.warning(f"Cannot handle planar format {pixel_format} with {len(planes)} planes")
                    return None

        except Exception as e:
            logger.warning(f"YUV GPU upload/render failed: {e}")
            return None

        if not rendered_ok:
            return None

        # T-03/P0-04: 本轨上传证据（字节 + 上传模式 + 耗时）。r16ui_direct
        # = uint16 码值直传（无 CPU 像素转换）；float16 = R16UI self-test
        # 失败的 CPU 归一化回退（trace 里可见，不是静默路径）。
        if stats_out is not None:
            try:
                _upload_mode = ''
                _timing = getattr(self._yuv_uploader, 'get_last_upload_timing', None)
                if callable(_timing):
                    _upload_mode = str((_timing() or {}).get('mode', '') or '')
                stats_out['bytes'] = int(stats_out.get('bytes', 0)) + sum(
                    int(p.nbytes) for p in planes)
                stats_out['ms'] = float(stats_out.get('ms', 0.0)) + max(
                    0.0, (time.perf_counter() - _planar_t0) * 1000.0)
                stats_out['tracks'] = int(stats_out.get('tracks', 0)) + 1
                stats_out['mode'] = _upload_mode
                stats_out['pixel_format'] = str(pixel_format or '')
                stats_out['gbr'] = (upload_mode == 'rgb_planar')
            except Exception:
                pass

        # P0-04: planar 路径每次真实发生 CPU→GPU 平面拷贝，按 plane 字节
        # 计入全局审计。零拷贝 IOSurface 路径不经过这里，天然计 0。
        # （per-frame 归因由 stats_out → consume_planar_upload_stats →
        # FrameTraceCollector.annotate 完成。）
        try:
            from src.shared.video.pixel_copy_counters import (
                CopyKind,
                add_pixel_copy,
            )
            planar_bytes = sum(int(p.nbytes) for p in planes)
            add_pixel_copy(CopyKind.CPU_UPLOAD, planar_bytes)
            add_pixel_copy(CopyKind.TEXTURE_WRITE, planar_bytes)
        except Exception:
            pass

        # 所有 YUV 路径（IOSurface 和 Planar）现在都用不翻转 quad，
        # track FBO 方向统一为"图像顶部=FBO 底部"（GL 默认）。
        # composite 最后一步统一翻转一次到正确方向，不需要每轨单独翻转。
        return (track_tex, width, height)

    def _store_planar_upload_stats(self, frame: int, stats: dict) -> None:
        """按帧累积 planar 上传证据（多轨合并；有界 16 帧，T-03/P0-04）。"""
        store = getattr(self, '_planar_upload_stats', None)
        if store is None:
            store = {}
            self._planar_upload_stats = store
        existing = store.get(int(frame))
        if existing:
            existing['bytes'] = int(existing.get('bytes', 0)) + int(stats.get('bytes', 0))
            existing['ms'] = float(existing.get('ms', 0.0)) + float(stats.get('ms', 0.0))
            existing['tracks'] = int(existing.get('tracks', 0)) + int(stats.get('tracks', 0))
            existing['mode'] = stats.get('mode', '')
            existing['pixel_format'] = stats.get('pixel_format', '')
            existing['gbr'] = bool(stats.get('gbr', False))
        else:
            store[int(frame)] = dict(stats)
            while len(store) > 16:
                oldest = next(iter(store))
                store.pop(oldest, None)

    def consume_planar_upload_stats(self, frame: int) -> Optional[dict]:
        """
        GL 完成侧消费本帧 planar 上传证据（取走即删）。

        返回 dict：bytes / ms / tracks / mode（'r16ui_direct'=uint16 码值
        直传无 CPU 像素转换；'float16'=R16UI self-test 失败的 CPU 归一化
        回退）/ pixel_format / gbr；该帧无 planar 轨时 None。
        """
        store = getattr(self, '_planar_upload_stats', None)
        if not store:
            return None
        return store.pop(int(frame), None)

    def drain_completed_gpu_queries(self) -> list:
        """A0.1：取走已完成的 GPU timer query 结果（含发起帧 tag）。

        每项：{"pass": str, "ms": float|None, "frame_seq": int, "tag": frame}。
        结果属于 RING_DEPTH-1 帧前的**发起帧**（tag），消费方必须回填到
        发起帧的 trace；ms=None 表示读回失败（unavailable，不得当 0.0）。
        deque(maxlen=16)：消费方每帧 drain 则不会滞留。
        """
        pending = getattr(self, '_completed_gpu_queries', None)
        if not pending:
            return []
        results = list(pending)
        pending.clear()
        return results

    def _store_readback_stats(self, frame: int, nbytes: int, kind: str) -> None:
        """A0.1：按帧暂存 GPU→CPU 回读证据（快照路径；有界 16 帧）。

        kind ∈ READBACK_KINDS（seek_snapshot / background_snapshot）。
        与 planar 上传统计同款"GL 线程暂存、完成回调取走"模式。
        """
        store = getattr(self, '_readback_stats', None)
        if store is None:
            store = {}
            self._readback_stats = store
        entry = store.get(int(frame))
        if entry is None:
            entry = {"bytes": 0, "kinds": []}
            store[int(frame)] = entry
        entry["bytes"] += int(nbytes)
        if kind not in entry["kinds"]:
            entry["kinds"].append(kind)
        while len(store) > 16:
            oldest = next(iter(store))
            store.pop(oldest, None)

    def consume_readback_stats(self, frame: int) -> Optional[dict]:
        """A0.1：取走本帧的回读证据（{"bytes": int, "kinds": [str, ...]}）。"""
        store = getattr(self, '_readback_stats', None)
        if not store:
            return None
        return store.pop(int(frame), None)

    def _composite_tracks(
        self, track_textures: list, w: int, h: int, dip_jobs: tuple = (),
    ):
        """Composite multiple track textures into final output.

        ``dip_jobs``：P0-PREV-GPU-T 的 dip_to_color 任务
        ``((TransitionBlendInfo, None), ...)``。在合成 ping-pong 域
        （top=bottom）内做全屏颜色混合后再统一翻转 blit，与 CPU 在整帧
        合成完成后叠加 dip 的顺序一致。
        """
        if not track_textures:
            return None

        import moderngl

        self._composite_fbo.use()
        self.ctx.viewport = (0, 0, w, h)

        if len(track_textures) == 1 and not dip_jobs:
            # Single track: render directly to composite FBO
            # 用翻转 VAO：track FBO 方向是"图像顶部=FBO 底部"（GL 默认），
            # 翻转后 composite FBO 方向 = "图像顶部=FBO 顶部"，Viewer 正确显示。
            self.ctx.clear(0.0, 0.0, 0.0, 1.0)
            tex, opacity, *rest = track_textures[0]
            self._blend_program['u_base'] = 0
            self._blend_program['u_overlay'] = 1
            self._blend_program['u_opacity'] = opacity
            # 底层片段没有下垫画面：与 CPU 预览/导出一致，混合模式不生效
            # （multiply 对黑底会把整层压成全黑）。
            self._blend_program['u_blend_mode'] = 0

            # Create a dummy black base texture
            base_tex = self._get_black_texture(w, h)
            base_tex.use(0)
            tex.use(1)

            self._flip_blend_vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)
            return self._composite_tex

        # Multi-track: blend bottom-up with ping-pong.
        # 重要：ping-pong 使用独立的 scratch 纹理，不借用 output_pool 的其他 slot。
        # 旧实现用 _output_pool[(index+1)%size] 作为 pong，会破坏其他 slot 的内容
        # （可能正被 Viewer 持有未 ack），导致多轨画面错误。
        scratch_a, scratch_b, scratch_a_fbo, scratch_b_fbo = self._ensure_composite_scratch(w, h)

        # First track: render to scratch_a
        # 首层按自身 opacity 对黑底绘制——与 CPU _alpha_blend（acc 从 0
        # 开始：acc = layer*op）以及单轨路径、adjustment 路径的种子语义
        # 一致。旧实现写死 1.0，半透明底层在 GPU 预览会比 CPU 回退偏亮。
        first_tex, first_opacity, *first_rest = track_textures[0]
        scratch_a_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 1.0)
        self._blend_program['u_base'] = 0
        self._blend_program['u_overlay'] = 1
        self._blend_program['u_opacity'] = first_opacity
        # 底层不应用混合模式（同上）；也避免复用上一帧残留的 u_blend_mode。
        self._blend_program['u_blend_mode'] = 0

        base = self._get_black_texture(w, h)
        base.use(0)
        first_tex.use(1)

        self._blend_vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)

        # Overlay remaining tracks using ping-pong between scratch_a and scratch_b
        current_read_tex, current_read_fbo = scratch_a, scratch_a_fbo
        current_write_tex, current_write_fbo = scratch_b, scratch_b_fbo

        for tex, opacity, *rest in track_textures[1:]:
            # Read from current_read, write to current_write (different textures!)
            current_write_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 1.0)

            self._blend_program['u_base'] = 0
            self._blend_program['u_overlay'] = 1
            self._blend_program['u_opacity'] = opacity
            # A1: 该层的图层混合模式（normal 编码为 0 = 纯 alpha over）
            self._blend_program['u_blend_mode'] = int(rest[0]) if rest else 0

            current_read_tex.use(0)
            tex.use(1)

            self._blend_vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)

            # Swap read/write for next iteration
            current_read_tex, current_write_tex = current_write_tex, current_read_tex
            current_read_fbo, current_write_fbo = current_write_fbo, current_read_fbo

        # P0-PREV-GPU-T：dip_to_color 颜色后处理（合成域内，多个 dip 按
        # CPU 顺序逐个叠加）。CPU 同公式：color*f + frame*(1-f)。
        for dip_trans, _dip_mask in dip_jobs:
            dip_factor = float(dip_trans.get_color_factor())
            if dip_factor <= 0.0:
                continue
            if self._dip_program is None or self._dip_vao is None:
                logger.warning(
                    "EditGpuPreviewRenderer: dip pass unavailable; "
                    "dip_to_color skipped on GPU path"
                )
                continue
            current_write_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 1.0)
            self._dip_program['u_tex'] = 0
            # 纹理是 RGB 通道序（YUV shader 直接写 vec4(r,g,b,1)），
            # blend_color 本就是 RGB，无需 CPU 路径的 BGR 交换。
            self._dip_program['u_dip_color'] = tuple(
                max(0.0, min(1.0, float(c))) for c in dip_trans.blend_color
            )
            self._dip_program['u_dip_factor'] = dip_factor
            current_read_tex.use(0)
            self._dip_vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)
            current_read_tex, current_write_tex = current_write_tex, current_read_tex
            current_read_fbo, current_write_fbo = current_write_fbo, current_read_fbo

        # The result is in current_read_tex (last written = last read after swap).
        # Blit the result to the composite FBO (current output slot).
        # 用翻转 VAO：scratch 里的合成结果是"图像顶部=FBO 底部"方向
        # （因为所有 YUV 渲染和 scratch blend 都用不翻转 quad）。
        # 翻转 blit 后 composite FBO 方向 = "图像顶部=FBO 顶部"，Viewer 正确显示。
        self._composite_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 1.0)
        self._blend_program['u_base'] = 0
        self._blend_program['u_overlay'] = 1
        self._blend_program['u_opacity'] = 1.0
        # 纯拷贝 blit：必须显式回到 normal，否则会沿用最顶层的混合模式
        # 把整帧合成结果对黑底再混合一次（multiply/darken 等模式 → 全黑）。
        self._blend_program['u_blend_mode'] = 0
        base = self._get_black_texture(w, h)
        base.use(0)
        current_read_tex.use(1)
        self._flip_blend_vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)

        return self._composite_tex

    @staticmethod
    def _build_scoped_composite_work(
        track_textures: list,
        track_texture_indices: list,
        adjustment_layers: list,
    ) -> list:
        """Build the z-ordered operation list for scoped compositing.

        Pure data — the scope contract lives here and is unit-tested without
        any GL. Each work item is a tuple whose third element is the kind:
        ``'layer'`` (composite a normal track onto the accumulator) or
        ``'adjustment'`` (run that adjustment's effect chain on the
        accumulator). Items are sorted by track_index ascending (bottom→top);
        at a tie, a normal layer comes before an adjustment at the same index
        (the equal-index layer is the topmost "below" the adjustment).

        ``adjustment_layers`` entries are the T-P1-22 三元组
        ``(ClipLayerInfo, effects_tuple, preview_grade)`` as produced by
        ``timeline_preview_renderer``（grade 为 ColorStack 参数快照或 None）；
        旧二元组 ``(ClipLayerInfo, effects_tuple)`` 仍兼容（grade 按 None）。
        The grade is carried on the work item so the renderer passes it
        through to :meth:`_apply_adjustment_clips` instead of dropping it.

        Because adjustments are inserted IN PLACE at their z-position (not
        appended after all layers), a renderer walking this list applies each
        adjustment only to the accumulator built from layers below it — it can
        never see the upper layers. That is the FX-C02 scope guarantee.
        """
        work: list = []
        for entry, idx in zip(track_textures, track_texture_indices):
            tex, opacity = entry[0], entry[1]
            blend_code = int(entry[2]) if len(entry) > 2 else 0
            work.append((idx, 0, 'layer', tex, opacity, blend_code))
        for adj_entry in adjustment_layers:
            # AUD-16600：按生产方 T-P1-22 三元组解包（旧二元组兼容，grade=None）。
            adj_layer, effects = adj_entry[0], adj_entry[1]
            grade = adj_entry[2] if len(adj_entry) > 2 else None
            adj_idx = getattr(adj_layer, 'track_index', 0)
            work.append((adj_idx, 1, 'adjustment', adj_layer, effects, grade))
        work.sort(key=lambda item: (item[0], item[1]))
        return work

    def _composite_with_adjustment_scope(
        self,
        track_textures: list,
        track_texture_indices: list,
        adjustment_layers: list,
        w: int,
        h: int,
        frame: int,
    ):
        """Composite tracks WITH Adjustment Clips applied at their z-position.

        FX-C02 fix. The legacy path composited every track first, then ran
        all adjustments as a final full-screen post pass — so an adjustment
        leaked into tracks ABOVE it. This method interleaves adjustments at
        their own track_index: it walks layers bottom→top, blending normal
        tracks onto the accumulator, and whenever an Adjustment Clip is the
        next layer in z-order it runs that adjustment's effect chain on the
        accumulator (everything below it so far) BEFORE compositing the
        layers above. An adjustment can therefore never see the upper layers.

        Args:
            track_textures: ``[(tex, opacity), ...]`` for normal layers.
            track_texture_indices: parallel ``[track_index, ...]`` for each
                entry in track_textures (the z-order).
            adjustment_layers: ``[(ClipLayerInfo, effects_tuple,
                preview_grade), ...]``（T-P1-22 三元组，与生产方
                timeline_preview_renderer 一致；grade 为 ColorStack 参数
                快照或 None，旧二元组兼容按 None 处理）；每个 ClipLayerInfo
                携带 ``track_index``。
            w, h: composite dimensions.
            frame: global timeline frame (for FX-C05 keyframe sampling).

        Returns:
            The composite texture (``self._composite_tex``), like
            :meth:`_composite_tracks`.
        """
        import moderngl

        if not track_textures and not adjustment_layers:
            return None

        # Build a single z-ordered work list (pure data — see the static
        # helper, which is unit-tested for the scope contract without GL).
        work = self._build_scoped_composite_work(
            track_textures, track_texture_indices, adjustment_layers,
        )

        self._composite_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 1.0)

        # Ping-pong buffers (same ones _composite_tracks uses).
        scratch_a, scratch_b, scratch_a_fbo, scratch_b_fbo = self._ensure_composite_scratch(w, h)

        # Seed the accumulator with the first item.
        first = work[0]
        kind = first[2]
        if kind == 'layer':
            _, _, _, tex, opacity, *_rest_first = first
            scratch_a_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 1.0)
            self._blend_program['u_base'] = 0
            self._blend_program['u_overlay'] = 1
            self._blend_program['u_opacity'] = opacity
            # 底层片段对黑底不应用混合模式（与 CPU 预览/导出对齐）。
            self._blend_program['u_blend_mode'] = 0
            base = self._get_black_texture(w, h)
            base.use(0)
            tex.use(1)
            self._blend_vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)
            accumulator = scratch_a  # moderngl.Texture — has .use()
        else:
            # First item is an adjustment with nothing below it → no-op, but
            # still need a valid accumulator texture. Paint black on scratch_a.
            _, _, _, adj_layer, effects, *grade_rest_first = first
            grade_first = grade_rest_first[0] if grade_rest_first else None
            scratch_a_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 1.0)
            if effects or grade_first:
                # AUD-16600：三元组整体透传，grade 不再被静默丢弃。
                accumulator = self._apply_adjustment_clips(
                    scratch_a, [(adj_layer, effects, grade_first)], w, h, frame
                )
            else:
                accumulator = scratch_a

        current_read = accumulator          # what the next op reads from
        current_write_fbo = scratch_b_fbo

        for item in work[1:]:
            kind = item[2]
            if kind == 'layer':
                _, _, _, tex, opacity, *rest_item = item
                blend_code_item = int(rest_item[0]) if rest_item else 0
                current_write_fbo.use()
                self.ctx.viewport = (0, 0, w, h)
                self.ctx.clear(0.0, 0.0, 0.0, 1.0)
                self._blend_program['u_base'] = 0
                self._blend_program['u_overlay'] = 1
                self._blend_program['u_opacity'] = opacity
                self._blend_program['u_blend_mode'] = blend_code_item
                current_read.use(0)
                tex.use(1)
                self._blend_vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)
                # Result is now in current_write_fbo's texture.
                new_read = scratch_a if current_write_fbo is scratch_a_fbo else scratch_b
                current_read = new_read
                current_write_fbo = scratch_a_fbo if current_write_fbo is scratch_b_fbo else scratch_b_fbo
            else:
                # Adjustment: run its chain and geometry on the accumulator
                # (current_read), then mix the result using the adjustment's
                # own opacity.  The operation is still in z-order, so upper
                # layers are not part of this adjustment's input.
                _, _, _, adj_layer, effects, *grade_rest_item = item
                grade_item = grade_rest_item[0] if grade_rest_item else None
                # AUD-16600：三元组整体透传，grade 不再被静默丢弃。
                adjusted = self._apply_adjustment_clips(
                    current_read, [(adj_layer, effects, grade_item)], w, h, frame
                )
                adjustment_opacity = max(
                    0.0, min(1.0, float(getattr(adj_layer, 'opacity', 1.0)))
                )
                transform = getattr(adj_layer, "transform", None)
                has_transform = bool(
                    transform and not _transform_tuple_is_identity(transform)
                )
                # At full opacity, Transform replaces the accumulated lower
                # composite.  Mixing it over current_read here would leave
                # the untransformed large image visible around a scaled-down
                # result ("small image over big image").
                if has_transform and adjustment_opacity >= 1.0 - 1e-4:
                    current_read = adjusted
                    continue
                current_write_fbo.use()
                self.ctx.viewport = (0, 0, w, h)
                self.ctx.clear(0.0, 0.0, 0.0, 0.0)
                self._blend_program['u_base'] = 0
                self._blend_program['u_overlay'] = 1
                self._blend_program['u_opacity'] = adjustment_opacity
                # 纯不透明度混合：显式 normal，避免沿用上一层的混合模式。
                self._blend_program['u_blend_mode'] = 0
                current_read.use(0)
                adjusted.use(1)
                self._blend_vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)
                new_read = scratch_a if current_write_fbo is scratch_a_fbo else scratch_b
                current_read = new_read
                current_write_fbo = (
                    scratch_a_fbo if current_write_fbo is scratch_b_fbo else scratch_b_fbo
                )

        # Blit the final accumulator to the composite FBO (flip for Viewer).
        self._composite_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 1.0)
        self._blend_program['u_base'] = 0
        self._blend_program['u_overlay'] = 1
        self._blend_program['u_opacity'] = 1.0
        # 纯拷贝 blit：显式 normal，避免沿用最顶层的混合模式。
        self._blend_program['u_blend_mode'] = 0
        base = self._get_black_texture(w, h)
        base.use(0)
        current_read.use(1)
        self._flip_blend_vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)
        return self._composite_tex

    def _ensure_composite_scratch(self, w: int, h: int):
        """Get or create independent scratch textures for multi-track ping-pong.

        这些纹理不来自 output_pool，避免借用其他 present slot 导致内容串扰
        或破坏 Viewer 正在显示的未 ack 纹理。
        """
        need_rebuild = (
            not hasattr(self, '_composite_scratch_a')
            or self._composite_scratch_a is None
            or self._composite_scratch_a.size != (w, h)
        )
        if need_rebuild:
            try:
                if hasattr(self, '_composite_scratch_a') and self._composite_scratch_a is not None:
                    self._composite_scratch_a.release()
                    self._composite_scratch_a_fbo.release()
                    self._composite_scratch_b.release()
                    self._composite_scratch_b_fbo.release()
            except Exception:
                pass
            self._composite_scratch_a = self.ctx.texture((w, h), components=4, dtype='f1')
            self._composite_scratch_a_fbo = self.ctx.framebuffer(color_attachments=[self._composite_scratch_a])
            self._composite_scratch_b = self.ctx.texture((w, h), components=4, dtype='f1')
            self._composite_scratch_b_fbo = self.ctx.framebuffer(color_attachments=[self._composite_scratch_b])
        return (
            self._composite_scratch_a,
            self._composite_scratch_b,
            self._composite_scratch_a_fbo,
            self._composite_scratch_b_fbo,
        )

    def has_transition_support(self) -> bool:
        """P0-PREV-GPU-T：转场 GPU 程序是否就绪（worker 提交前门禁）。

        dip/spatial 任一程序编译失败都会让 worker 把转场帧整体回退
        CPU 合成，绝不半支持。全画面 dissolve 在实际任务进入
        ``_preblend_transition_tracks`` 时再检查专用 shader，这样旧的
        cross/spatial 转场不会因可选 dissolve shader 失败而全部降级。
        """
        return (
            self._transition_program is not None
            and self._dip_program is not None
        )

    def has_full_frame_transition_support(self) -> bool:
        """Return whether the dedicated full-frame dissolve pass is ready.

        The common spatial/dip shaders may compile successfully even when the
        optional dissolve shader does not.  Keeping this as a separate
        capability lets the worker choose the normal CPU fallback before it
        submits a frame that the GPU thread cannot present.
        """
        return (
            self._full_frame_transition_program is not None
            and self._full_frame_transition_vao is not None
        )

    def has_comp_clip_gpu_support(self) -> bool:
        """TL-C01/TL-P1-01: whether this compositor consumes comp_gpu_handle
        layers (fence wait + lease into a track slot).

        A comp clip is consumable only after the Edit render thread has its
        shared GL context and the normal blend pipeline. The actual texture
        is sampled directly by ``_process_task``; it is not wrapped as an
        owning ModernGL texture and is released only after the source-read
        fence. Returning a strict bool is intentional: capability probes must
        not be enabled by a truthy test double.
        """
        return bool(
            getattr(self, "_initialized", False)
            and getattr(self, "ctx", None) is not None
            and getattr(self, "_blend_program", None) is not None
            and getattr(self, "_blend_vao", None) is not None
        )

    def _prepare_comp_gpu_texture(
        self, handle: Any, generation_id: int,
    ) -> tuple[Optional[Any], str | None]:
        """Validate and prepare a non-owning view of a composite GPU handle.

        The producer and this renderer use a shared OpenGL share group. The
        handle's fence is therefore waited with ``glWaitSync`` (server-side),
        and the texture name is sampled directly. No CPU readback or texture
        upload is allowed here. The returned proxy deliberately does not own
        the texture; ownership remains with ``handle`` until the source-read
        fence in ``_release_previous_frames`` has completed.
        """
        if handle is None:
            return None, "comp_gpu_handle_missing"
        is_presentable = getattr(handle, "is_presentable", None)
        if not callable(is_presentable):
            return None, "comp_gpu_handle_invalid_contract"
        try:
            if not bool(is_presentable(int(generation_id))):
                return None, "comp_gpu_handle_stale_or_unavailable"
        except Exception:
            return None, "comp_gpu_handle_validation_failed"

        if str(getattr(handle, "backend", "opengl") or "opengl").lower() != "opengl":
            return None, "comp_gpu_handle_backend_mismatch"
        try:
            texture_id = int(getattr(handle, "texture_id", 0) or 0)
            width = int(getattr(handle, "width", 0) or 0)
            height = int(getattr(handle, "height", 0) or 0)
        except (TypeError, ValueError):
            return None, "comp_gpu_handle_dimensions_invalid"
        if texture_id <= 0 or width <= 0 or height <= 0:
            return None, "comp_gpu_handle_texture_invalid"

        fence_ns = int(getattr(handle, "fence_ns", 0) or 0)
        if fence_ns:
            try:
                from src.shared.gpu.composite_gpu_frame_handle import wait_fence

                if not wait_fence(fence_ns):
                    return None, "comp_gpu_handle_fence_unavailable"
            except Exception as exc:  # noqa: BLE001
                logger.debug("comp GPU handle fence wait failed: %s", exc)
                return None, "comp_gpu_handle_fence_wait_failed"

        return _ExternalTextureProxy(texture_id, (width, height)), None

    def _ensure_transition_resources(self, w: int, h: int) -> bool:
        """P0-PREV-GPU-T：转场混合资源（两张 ping-pong scratch）。

        方向约定与 _composite_scratch 一致（top=bottom 域，非翻转 VAO）。
        兼容纹理仍按需分配，但 pixel-free 生产任务不会写入它。
        """
        if (
            len(self._transition_scratch_texes) == 2
            and self._transition_scratch_texes[0] is not None
            and self._transition_scratch_texes[0].size == (w, h)
            and self._transition_mask_tex is not None
        ):
            return True
        try:
            for tex in self._transition_scratch_texes:
                if tex is not None:
                    tex.release()
            for fbo in self._transition_scratch_fbos:
                if fbo is not None:
                    fbo.release()
            if self._transition_mask_tex is not None:
                self._transition_mask_tex.release()
            self._transition_scratch_texes = []
            self._transition_scratch_fbos = []
            for _ in range(2):
                tex = self.ctx.texture((w, h), components=4, dtype='f1')
                fbo = self.ctx.framebuffer(color_attachments=[tex])
                self._transition_scratch_texes.append(tex)
                self._transition_scratch_fbos.append(fbo)
            self._transition_mask_tex = self.ctx.texture(
                (w, h), components=1, dtype='f4',
            )
            self._transition_mask_tex.filter = (
                self.ctx.NEAREST, self.ctx.NEAREST,
            )
            self._transition_mask_tex.repeat_x = False
            self._transition_mask_tex.repeat_y = False
            return True
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "EditGpuPreviewRenderer: transition resources unavailable: %s",
                error,
            )
            return False

    def _preblend_transition_tracks(
        self,
        task: "EditRenderTask",
        track_textures: list,
        track_clip_ids: list,
        track_texture_indices: list,
        w: int,
        h: int,
    ) -> Optional[str]:
        """P0-PREV-GPU-T：GPU 预合成双源转场为单纹理。

        两侧条目已完成各自 per-track 链（YUV→crop→grade→effects），
        然后由统一 GPU transition shader 计算几何 mask、源侧模糊或
        full-frame dissolve，再在左侧位置注入（opacity=1.0）。CPU
        只上传少量 uniform 控制数据，不上传逐像素 mask。

        Returns:
            None 成功；字符串为错误码（调用方回退 CPU 重渲染本帧）。
        """
        jobs = [
            (trans, spec) for trans, spec in (task.transition_jobs or ())
            if spec is not None
        ]
        if not jobs:
            return None
        if any(int(getattr(spec, "kind", -1)) != 4
               for _trans, spec in jobs) and (
            self._transition_program is None or self._transition_vao is None
        ):
            return "transition_gpu_unavailable"
        if any(int(getattr(spec, "kind", -1)) == 4
               for _trans, spec in jobs) and (
            self._full_frame_transition_program is None
            or self._full_frame_transition_vao is None
        ):
            return "full_frame_transition_gpu_unavailable"
        if not self._ensure_transition_resources(w, h):
            return "transition_gpu_resources_unavailable"

        scratch_index = 0
        for trans, spec in jobs:
            try:
                left_index = track_clip_ids.index(trans.left_clip_id)
            except ValueError:
                left_index = -1
            try:
                right_index = track_clip_ids.index(trans.right_clip_id)
            except ValueError:
                right_index = -1
            if left_index < 0 or right_index < 0 or left_index == right_index:
                return "transition_side_missing"
            left_tex = track_textures[left_index][0]
            right_tex = track_textures[right_index][0]
            target_tex = self._transition_scratch_texes[scratch_index]
            target_fbo = self._transition_scratch_fbos[scratch_index]
            scratch_index = (scratch_index + 1) % 2
            target_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 1.0)
            left_tex.use(0)
            right_tex.use(1)
            program = (
                self._full_frame_transition_program
                if int(getattr(spec, "kind", -1)) == 4
                else self._transition_program
            )
            vao = (
                self._full_frame_transition_vao
                if int(getattr(spec, "kind", -1)) == 4
                else self._transition_vao
            )
            if program is None or vao is None:
                return "transition_gpu_program_unavailable"
            self._set_transition_uniforms(program, trans, spec)
            vao.render(mode=self.ctx.TRIANGLE_STRIP, vertices=4)

            blended_entry = (target_tex, 1.0, 0)
            new_textures: list = []
            new_ids: list = []
            # AUD-16604：三个平行列表必须同步收缩——track_texture_indices
            # 若保持原长，转场后 zip 会把上层轨配到被移除侧的旧 z 槽，
            # scoped 合成的调整剪辑作用域随之错轨。
            new_indices: list = []
            for index, entry in enumerate(track_textures):
                if index == left_index:
                    new_textures.append(blended_entry)
                    new_ids.append(track_clip_ids[left_index])
                    new_indices.append(track_texture_indices[left_index])
                elif index == right_index:
                    continue
                else:
                    new_textures.append(entry)
                    new_ids.append(track_clip_ids[index])
                    new_indices.append(track_texture_indices[index])
            track_textures[:] = new_textures
            track_clip_ids[:] = new_ids
            track_texture_indices[:] = new_indices
        return None

    @staticmethod
    def _set_transition_uniforms(program: Any, trans: Any, spec: Any) -> None:
        """Upload only scalar/control data for a GPU transition pass."""
        import numpy as np

        program["u_left_tex"] = 0
        program["u_right_tex"] = 1
        program["u_progress"] = float(getattr(spec, "progress", 0.0))
        program["u_left_opacity"] = float(getattr(trans, "left_opacity", 1.0))
        program["u_right_opacity"] = float(getattr(trans, "right_opacity", 1.0))
        program["u_blur_ratio"] = float(getattr(spec, "blur_ratio", 0.045))
        program["u_transition_mode"] = int(getattr(spec, "transition_mode", 0))
        program["u_use_procedural"] = 1
        program["u_transition_kind"] = int(getattr(spec, "kind", 0))
        program["u_direction"] = int(getattr(spec, "direction", 0))
        program["u_reverse"] = 1 if bool(getattr(spec, "reverse", False)) else 0
        program["u_angle"] = float(getattr(spec, "angle", 0.0))
        program["u_softness"] = float(getattr(spec, "softness", 0.02))
        vertices = np.zeros((48, 2), dtype=np.float32)
        raw_vertices = tuple(getattr(spec, "vertices", ()) or ())[:48]
        if raw_vertices:
            vertices[:len(raw_vertices)] = np.asarray(raw_vertices, dtype=np.float32)
        program["u_vertex_count"] = len(raw_vertices)
        program["u_vertices"].write(vertices.tobytes())
        program["u_effect_mode"] = int(getattr(spec, "effect_mode", 0))
        program["u_blur_radius"] = float(getattr(spec, "blur_radius", 0.0))
        program["u_blur_mix"] = float(getattr(spec, "blur_mix", 0.0))
        program["u_shutter"] = float(getattr(spec, "shutter", 1.0))
        program["u_blur_strength"] = float(getattr(spec, "blur_strength", 0.18))
        program["u_spin_angle"] = float(getattr(spec, "spin_angle", 18.0))
        program["u_split_amount"] = float(getattr(spec, "split_amount", 0.045))
        program["u_split_mix"] = float(getattr(spec, "split_mix", 0.96))
        # LIB1（L03）：新增 uniform 未被旧 spec 使用时由链接器剔除，
        # 以成员存在性守护（缺省值=旧路径语义）。
        if "u_center" in program:
            center = getattr(spec, "center", (0.5, 0.5)) or (0.5, 0.5)
            program["u_center"].value = (float(center[0]), float(center[1]))
        if "u_resample_mode" in program:
            program["u_resample_mode"].value = int(
                getattr(spec, "resample_mode", 0))
        if "u_push_dir" in program:
            program["u_push_dir"].value = int(
                getattr(spec, "push_direction", 0))
        if "u_mask_p0" in program:
            import numpy as _np
            mask_params = tuple(getattr(spec, "mask_params", ()) or ())[:8]
            packed = _np.zeros((2, 4), dtype=np.float32)
            if mask_params:
                packed[0, :len(mask_params)] = mask_params[:4]
                if len(mask_params) > 4:
                    packed[1, :len(mask_params) - 4] = mask_params[4:8]
            program["u_mask_p0"].write(packed[0].tobytes())
            program["u_mask_p1"].write(packed[1].tobytes())

    def _apply_dip_post_pass(
        self, final_tex, dip_jobs: tuple, w: int, h: int,
    ):
        """adjustment 路径的 dip 后处理：composite（top=top）经 scratch
        往返应用颜色 pass，返回仍写回 composite 纹理。"""
        if not dip_jobs:
            return final_tex
        if self._dip_program is None or self._dip_vao is None:
            return final_tex
        if not self._ensure_transition_resources(w, h):
            return final_tex
        import moderngl

        pending = [
            (trans, float(trans.get_color_factor()))
            for trans, _mask in dip_jobs
            if float(trans.get_color_factor()) > 0.0
        ]
        if not pending:
            return final_tex
        work_tex = self._transition_scratch_texes[0]
        work_fbo = self._transition_scratch_fbos[0]
        for trans, factor in pending:
            # 1) composite → scratch（flip 拷贝：top=top → top=bottom 域）
            work_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 1.0)
            self._blend_program['u_base'] = 0
            self._blend_program['u_overlay'] = 1
            self._blend_program['u_opacity'] = 1.0
            self._blend_program['u_blend_mode'] = 0
            self._get_black_texture(w, h).use(0)
            final_tex.use(1)
            self._flip_blend_vao.render(
                mode=moderngl.TRIANGLE_STRIP, vertices=4,
            )
            # 2) dip：读 scratch（top=bottom），flip VAO 写回 composite
            #    （恢复 top=top 方向）
            self._composite_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 1.0)
            self._dip_program['u_tex'] = 0
            self._dip_program['u_dip_color'] = tuple(
                max(0.0, min(1.0, float(c))) for c in trans.blend_color
            )
            self._dip_program['u_dip_factor'] = factor
            work_tex.use(0)
            self._flip_dip_vao.render(
                mode=moderngl.TRIANGLE_STRIP, vertices=4,
            )
        return self._composite_tex

    # ==================== Resource Management ====================

    def _release_task_sources(self, task: EditRenderTask | None) -> None:
        """Release decoded resources owned by a queued task that will not render."""
        if task is None:
            return
        try:
            layers = list(getattr(task, "layers", []) or [])
        except Exception:
            layers = []
        if not layers:
            return

        from src.shared.media.gpu_frame import safe_release_gpu_frame

        for layer_data in layers:
            if not isinstance(layer_data, dict):
                continue
            handle = layer_data.get("gpu_handle")
            if handle is not None:
                release = getattr(handle, "release", None)
                if callable(release):
                    try:
                        release()
                    except Exception as e:
                        logger.debug(f"release queued composite handle failed: {e}")
            resource = (
                layer_data.get("gpu_frame")
                or layer_data.get("vt_pixel_buffer")
                or layer_data.get("cuda_resident")
                or layer_data.get("decoded_frame")
            )
            if resource is None:
                continue
            release = getattr(resource, "release", None)
            if callable(release):
                try:
                    release()
                    continue
                except Exception as e:
                    logger.debug(f"release task resource failed: {e}")
            safe_release_gpu_frame(resource)

    def _ensure_composite_fbo(
        self, w: int, h: int, *, generation_id: int = 0, frame_index: int = -1
    ) -> bool:
        """Ensure the output composite FBO exists at the right size (from the pool).

        Batch 1 (缺陷计划 RP-P0-02): output slot 选择现在由 OutputSlotPool
        控制——未 ack 的 slot 永不复用，根治播放快时的混帧。
        """
        # Allocate pool on first call or when size changes
        need_rebuild = (
            not self._output_pool
            or self._output_pool[0][0].size != (w, h)
        )

        if need_rebuild:
            # AUD-16602：旧池整体退役，不再立即 tex.release() +
            # release_all()——在途未 ack 的 READY/IN_VIEWER slot 仍可能被
            # Viewer 等待/采样。旧纹理进入延迟释放队列，全部 slot 回收
            # （或超时兜底）后才真正删除；ack 也同步送达退役池。
            retired_pool = self._present_pool
            retired_entries = [(tex, fbo) for tex, fbo in self._output_pool]
            self._output_pool = []

            # Rebuild present pool for the new size
            self._present_pool = OutputSlotPool(size=self._output_pool_size)
            self._present_pool_registered = False

            # Create new pool entries
            for slot_id in range(self._output_pool_size):
                tex = self.ctx.texture((w, h), components=4, dtype='f1')
                fbo = self.ctx.framebuffer(color_attachments=[tex])
                self._output_pool.append((tex, fbo))
                # Register slot with present pool (texture_glo = tex.glo)
                self._present_pool.register_slot(
                    slot_id, texture_glo=tex.glo, width=w, height=h,
                )
            self._present_pool_registered = True

            self._output_pool_index = 0
            if retired_entries:
                self._retired_output_pools.append(
                    (retired_pool, retired_entries, time.monotonic())
                )
                self._drain_retired_output_pools()

        # Batch 1: 通过 present pool 选一个可写 slot（未 ack 的不复用）。
        # 注意：这个方法只确保 FBO 存在并设置 _composite_tex/_composite_fbo；
        # 真正的 slot 生命周期 acquire 在 _process_task 入口完成（带 generation/frame）。
        # 这里仅作为 fallback：present pool 不可用时退回旧的 +2 推进。
        if self._present_pool_registered:
            lease = self._present_pool.acquire_for_writing(
                generation_id=generation_id, frame_index=frame_index,
            )
            if lease is not None:
                self._output_pool_index = lease.slot_id
                self._current_present_lease = lease
            else:
                # 全部 slot 未 ack：保留当前 index（_process_task 会决定 hold/drop）
                # 这是 back-pressure 的体现，不应静默复用未 ack 的 slot。
                playback_trace(
                    "gpu_renderer.present_pool_full",
                    frame=frame_index,
                    generation=generation_id,
                    pool_stats=self._present_pool.stats(),
                )
                self._current_present_lease = None
                return False
        else:
            # Fallback：旧 +2 推进（present pool 未注册时）
            self._output_pool_index = (
                (self._output_pool_index + 2) % self._output_pool_size
            )

        self._composite_tex, self._composite_fbo = self._output_pool[self._output_pool_index]
        return True

    def _abandon_current_present_lease(self) -> None:
        """AUD-16601：任务失败/提前返回时回收 WRITING 槽位。

        正常路径由 :meth:`_finalize_present_frame` 走 ``mark_ready``
        （READY 等 Viewer ack）。本方法用于 acquire 之后未能走到 finalize
        的路径（空 layers / 无可用轨 / 部分轨失败 / 转场失败 / 合成失败 /
        渲染异常）：lease 直接 release 回 FREE（``TextureLease.release``
        对 WRITING 有 force 语义并清理未消费 fence）。此前这些路径让
        WRITING 槽位永久退出轮换，累计 24 次后 present_pool 常驻满载、
        GPU 预览链路整体死亡。
        """
        lease = self._current_present_lease
        if lease is None:
            return
        self._current_present_lease = None
        try:
            lease.release()
        except Exception as e:
            logger.debug(
                f"EditGpuPreviewRenderer: abandon present lease failed: {e}"
            )

    def _drain_retired_output_pools(self, *, force: bool = False) -> None:
        """AUD-16602：释放已退役旧输出池的纹理（延迟释放队列排水）。

        每帧渲染前调用：旧池全部 slot 回到可写态（FREE/RELEASABLE，即
        Viewer 已 ack、不再采样旧纹理）才真正 ``tex.release()``；超过
        ``_RETIRED_OUTPUT_POOL_TIMEOUT_S`` 仍未回收的由 glFinish 兜底强
        释放，防止 Viewer 永不 ack 时退役纹理无限滞留。``force=True``
        （shutdown/_cleanup 用）无条件释放。
        """
        if not self._retired_output_pools:
            return
        now = time.monotonic()
        remaining: list = []
        for pool, entries, retired_at in self._retired_output_pools:
            states = pool.stats().get("states", {})
            all_reclaimed = not (
                states.get("READY", 0)
                or states.get("IN_VIEWER", 0)
                or states.get("WRITING", 0)
            )
            if (
                not all_reclaimed
                and not force
                and (now - retired_at) < _RETIRED_OUTPUT_POOL_TIMEOUT_S
            ):
                remaining.append((pool, entries, retired_at))
                continue
            if not all_reclaimed:
                # 超时兜底：确保本 context 提交的 GPU 命令完成后才删除
                # 共享对象名（与项目 deferred-release 模式一致）。用
                # ModernGL 的 ctx.finish()（本线程 current 的上下文），
                # 不直接触 PyOpenGL——无 GL 进程里解析/调用 GL 入口点
                # 本身就是段错误。
                ctx = getattr(self, "ctx", None)
                if ctx is not None:
                    try:
                        ctx.finish()
                    except Exception:
                        pass
                playback_trace(
                    "gpu_renderer.retired_output_pool_force_release",
                    pending_states=states,
                    age_s=round(now - retired_at, 3),
                )
            try:
                pool.release_all()
            except Exception:
                pass
            for tex, fbo in entries:
                try:
                    tex.release()
                except Exception:
                    pass
                try:
                    fbo.release()
                except Exception:
                    pass
        self._retired_output_pools = remaining

    def _ensure_background_composite_fbo(self, w: int, h: int) -> bool:
        """Ensure the private composite FBO used by background cache-fill tasks.

        These tasks never present: writing into a present-pool slot texture
        would corrupt a frame the Viewer may still be waiting to ack. A
        dedicated texture is safe because it only lives between the composite
        draw and the snapshot readback on this same thread.
        """
        current = getattr(self, "_bg_composite_tex", None)
        if (
            current is not None
            and getattr(self, "_bg_composite_size", None) == (w, h)
        ):
            self._composite_tex = self._bg_composite_tex
            self._composite_fbo = self._bg_composite_fbo
            return True
        try:
            if current is not None:
                try:
                    self._bg_composite_tex.release()
                except Exception:
                    pass
                try:
                    self._bg_composite_fbo.release()
                except Exception:
                    pass
            tex = self.ctx.texture((w, h), components=4, dtype='f1')
            fbo = self.ctx.framebuffer(color_attachments=[tex])
            self._bg_composite_tex = tex
            self._bg_composite_fbo = fbo
            self._bg_composite_size = (w, h)
            self._composite_tex = tex
            self._composite_fbo = fbo
            return True
        except Exception as e:
            logger.debug(f"background composite fbo creation failed: {e}")
            self._bg_composite_tex = None
            self._bg_composite_fbo = None
            self._bg_composite_size = None
            return False

    # ==================== P3.2 Composite Texture Cache ====================

    def has_cached_composite(
        self, frame: int, generation_id: int, size: Tuple[int, int]
    ) -> bool:
        """Worker 线程查询：该帧的全分辨率合成结果是否已缓存。

        命中意味着可以跳过整条解码+合成链，直接 blit 上屏。
        """
        if self._composite_cache_max <= 0:
            return False
        with self._composite_cache_lock:
            if self._composite_cache_pending_invalidate:
                return False
            entry = self._composite_cache.get((int(generation_id), int(frame)))
            if entry is None:
                return False
            return entry[2] == (int(size[0]), int(size[1]))

    def invalidate_composite_cache(self) -> None:
        """作废全部复合纹理缓存（任意线程调用；GL 线程在下个任务释放）。"""
        with self._composite_cache_lock:
            self._composite_cache_pending_invalidate = True

    def _drain_composite_cache_invalidate(self) -> None:
        """GL 线程：处理挂起的失效请求，释放全部缓存纹理。"""
        with self._composite_cache_lock:
            pending = self._composite_cache_pending_invalidate
            self._composite_cache_pending_invalidate = False
            entries = list(self._composite_cache.items()) if pending else []
            if pending:
                self._composite_cache.clear()
        for _key, (tex, fbo, _size) in entries:
            try:
                tex.release()
                fbo.release()
            except Exception:
                pass

    def _ensure_copy_program(self):
        """快照/缓存复制的全屏三角形 copy program（惰性创建）。"""
        if self._snapshot_program is None:
            self._snapshot_program = self.ctx.program(
                vertex_shader=self._SNAPSHOT_VERTEX_SHADER,
                fragment_shader=self._SNAPSHOT_FRAGMENT_SHADER,
            )
            self._snapshot_vao = self.ctx.vertex_array(self._snapshot_program, [])
        return self._snapshot_program, self._snapshot_vao

    def _get_cached_composite_tex(
        self, frame: int, generation_id: int, size: Tuple[int, int]
    ):
        """GL 线程查找缓存条目（命中同时把它移到 LRU 尾部）。"""
        with self._composite_cache_lock:
            key = (int(generation_id), int(frame))
            entry = self._composite_cache.get(key)
            if entry is None or entry[2] != (int(size[0]), int(size[1])):
                return None
            self._composite_cache.move_to_end(key)
            return entry[0]

    def _store_composite_cache(self, final_tex, w: int, h: int, frame: int, generation_id: int) -> None:
        """GL 线程：把合成结果拷贝进缓存条目（独立 tex+fbo，LRU 淘汰）。"""
        if self._composite_cache_max <= 0 or final_tex is None:
            return
        import moderngl
        try:
            # 字节预算自适应：预缓冲窗口 ±12 需要 ~25 条；1080p 画布下 32
            # 条（~268MB），4K 预览画布自动降到 ~7 条，GPU 显存不超标。
            bytes_per_entry = max(1, int(w) * int(h) * 4)
            effective_max = max(
                2, min(self._composite_cache_max, self._COMPOSITE_CACHE_BUDGET_BYTES // bytes_per_entry)
            )
            key = (int(generation_id), int(frame))
            with self._composite_cache_lock:
                existing = self._composite_cache.get(key)
                if existing is not None and existing[2] == (w, h):
                    self._composite_cache.move_to_end(key)
                    return  # 已有同尺寸条目
            program, vao = self._ensure_copy_program()
            tex = self.ctx.texture((w, h), components=4, dtype='f1')
            fbo = self.ctx.framebuffer(color_attachments=[tex])
            fbo.use()
            final_tex.use(0)
            program["composite_tex"].value = 0
            vao.render(mode=moderngl.TRIANGLES, vertices=3)
            with self._composite_cache_lock:
                self._composite_cache[key] = (tex, fbo, (w, h))
                self._composite_cache.move_to_end(key)
                evicted = []
                while len(self._composite_cache) > effective_max:
                    _old_key, old_entry = self._composite_cache.popitem(last=False)
                    evicted.append(old_entry)
            for old_tex, old_fbo, _size in evicted:
                try:
                    old_tex.release()
                    old_fbo.release()
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"composite cache store failed: {e}")

    def _blit_cached_composite(self, cached_tex, w: int, h: int) -> bool:
        """GL 线程：把缓存纹理拷进当前 composite FBO（present slot 纹理）。"""
        import moderngl
        try:
            program, vao = self._ensure_copy_program()
            self._composite_fbo.use()
            cached_tex.use(0)
            program["composite_tex"].value = 0
            vao.render(mode=moderngl.TRIANGLES, vertices=3)
            return True
        except Exception as e:
            logger.debug(f"composite cache blit failed: {e}")
            return False

    def _ensure_track_fbo(self, index: int, w: int, h: int):
        """Get or create a track FBO at the given index."""

        while len(self._track_texes) <= index:
            tex = self.ctx.texture((w, h), components=4, dtype='f1')
            fbo = self.ctx.framebuffer(color_attachments=[tex])
            self._track_texes.append(tex)
            self._track_fbos.append(fbo)

        tex = self._track_texes[index]
        fbo = self._track_fbos[index]

        # Resize if needed
        if tex.size != (w, h):
            tex.release()
            fbo.release()
            tex = self.ctx.texture((w, h), components=4, dtype='f1')
            fbo = self.ctx.framebuffer(color_attachments=[tex])
            self._track_texes[index] = tex
            self._track_fbos[index] = fbo

        return fbo, tex

    def _ensure_graded_track_fbo(self, index: int, w: int, h: int):
        """Get a separate target for a per-track ColorStack grade pass.

        AUD-16603 索引护栏：本方法按索引顺序填充轨槽数组——畸形/超界的
        index 会触发无界纹理分配（历史缺陷：调整剪辑用 1000+ 索引导致
        1001 张全尺寸纹理）。索引护栏在调用方 :meth:`_apply_preview_grade`
        实施（超界直接跳过 grade）；调整剪辑请改用
        :meth:`_ensure_adjustment_grade_fbo` 的稳定键分配。
        """
        while len(self._graded_track_texes) <= index:
            tex = self.ctx.texture((w, h), components=4, dtype='f1')
            fbo = self.ctx.framebuffer(color_attachments=[tex])
            self._graded_track_texes.append(tex)
            self._graded_track_fbos.append(fbo)

        tex = self._graded_track_texes[index]
        fbo = self._graded_track_fbos[index]
        if tex.size != (w, h):
            tex.release()
            fbo.release()
            tex = self.ctx.texture((w, h), components=4, dtype='f1')
            fbo = self.ctx.framebuffer(color_attachments=[tex])
            self._graded_track_texes[index] = tex
            self._graded_track_fbos[index] = fbo
        return fbo, tex

    def _release_adjustment_grade_entry(self, key: str) -> None:
        """AUD-16603：释放并移除一个调整剪辑 grade scratch 条目。"""
        tex = self._adjustment_grade_texes.pop(key, None)
        fbo = self._adjustment_grade_fbos.pop(key, None)
        if tex is not None:
            try:
                tex.release()
            except Exception:
                pass
        if fbo is not None:
            try:
                fbo.release()
            except Exception:
                pass

    def _ensure_adjustment_grade_fbo(
        self, key: str, w: int, h: int, *, pin_glo: Optional[int] = None,
    ):
        """AUD-16603：调整剪辑 grade 目标按稳定键惰性分配。

        旧实现把调整剪辑的 grade 目标塞进轨槽数组（索引 1000+），首个
        带 ColorStack 的调整剪辑帧会顺序创建 1001 张全尺寸 RGBA 纹理
        （1080p 约 8.3 GB）。本方法按稳定键（调整剪辑 clip_id）在 dict 中
        按需分配，容量 ``_ADJUSTMENT_GRADE_SCRATCH_CAP``、FIFO 淘汰。

        Args:
            key: 稳定键（调整剪辑 clip_id；缺省时调用方给的序号键）。
            w, h: 目标尺寸（与画布一致；尺寸变更时重建该键条目）。
            pin_glo: 当前链路仍在引用的纹理 GL 名——淘汰时跳过该条目，
                防止正在被 effects/transform 消费的 grade 输出被释放
                （scoped 合成同步消费，同一时刻至多一个在用）。

        Returns:
            (fbo, tex) 目标对。
        """
        tex = self._adjustment_grade_texes.get(key)
        fbo = self._adjustment_grade_fbos.get(key)
        if tex is not None and fbo is not None and tex.size == (w, h):
            return fbo, tex
        if tex is not None:
            # 尺寸变更：该键条目重建。
            self._release_adjustment_grade_entry(key)
        # FIFO 淘汰到容量上限内（跳过仍在引用的 pin_glo 条目）。
        while len(self._adjustment_grade_texes) >= _ADJUSTMENT_GRADE_SCRATCH_CAP:
            oldest = next(iter(self._adjustment_grade_texes), None)
            if oldest is None:
                break
            pinned = (
                pin_glo is not None
                and self._adjustment_grade_texes[oldest].glo == pin_glo
            )
            if pinned:
                # 最旧条目在用：找下一个可淘汰键。
                oldest = next(
                    (k for k, t in self._adjustment_grade_texes.items()
                     if t.glo != pin_glo),
                    None,
                )
                if oldest is None:
                    break
            self._release_adjustment_grade_entry(oldest)
        tex = self.ctx.texture((w, h), components=4, dtype='f1')
        fbo = self.ctx.framebuffer(color_attachments=[tex])
        self._adjustment_grade_texes[key] = tex
        self._adjustment_grade_fbos[key] = fbo
        return fbo, tex

    def _ensure_transform_track_fbo(self, index: int, w: int, h: int):
        """Get a separate target for the per-track clip-transform pass."""
        while len(self._transform_track_texes) <= index:
            tex = self.ctx.texture((w, h), components=4, dtype='f1')
            fbo = self.ctx.framebuffer(color_attachments=[tex])
            self._transform_track_texes.append(tex)
            self._transform_track_fbos.append(fbo)

        tex = self._transform_track_texes[index]
        fbo = self._transform_track_fbos[index]
        if tex.size != (w, h):
            tex.release()
            fbo.release()
            tex = self.ctx.texture((w, h), components=4, dtype='f1')
            fbo = self.ctx.framebuffer(color_attachments=[tex])
            self._transform_track_texes[index] = tex
            self._transform_track_fbos[index] = fbo
        return fbo, tex

    def _apply_clip_transform(self, source_tex, index: int, w: int, h: int,
                              transform_tuple) -> object:
        """Apply clip transform (translate/scale/rotation) to a track texture.

        Used by the planar and solid paths which don't go through the YUV
        vertex shader (which already has u_transform). Renders source_tex
        through a passthrough shader with the transform mat3 applied in the
        vertex stage, deforming the fullscreen quad in NDC space.

        Returns the transformed texture, or source_tex unchanged when the
        transform is identity or the shader is unavailable.
        """
        if not transform_tuple or _transform_tuple_is_identity(transform_tuple):
            return source_tex
        if self._transform_program is None or self._transform_vao is None:
            return source_tex

        import moderngl

        target_fbo, target_tex = self._ensure_transform_track_fbo(index, w, h)
        target_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        self._transform_program['u_tex'] = 0
        # Planar and solid sources use the same track-FBO → final-present
        # flip as the zero-copy YUV path.  Omitting this compensation inverted
        # vertical moves and rotation only on these common clip paths.
        coordinate_w, coordinate_h = self._active_transform_coordinate_size
        mat3_bytes = _transform_to_ndc_mat3_bytes(
            transform_tuple, w, h, track_fbo_y_flipped=True,
            coordinate_width=coordinate_w,
            coordinate_height=coordinate_h)
        try:
            self._transform_program['u_transform'].write(mat3_bytes)
        except (KeyError, AttributeError):
            pass
        source_tex.use(0)
        self._transform_vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)
        return target_tex

    def _ensure_crop_track_fbo(self, index: int, w: int, h: int):
        """Get a separate target for the per-track crop matte pass (FX-M06).

        Mirrors :meth:`_ensure_transform_track_fbo` / :meth:`_ensure_graded_track_fbo`:
        one texture+FBO per track slot, rebuilt on size change. The dtype is
        ``f1`` to match the rest of the track-FBO pipeline (8-bit preview).
        """
        while len(self._crop_track_texes) <= index:
            tex = self.ctx.texture((w, h), components=4, dtype='f1')
            fbo = self.ctx.framebuffer(color_attachments=[tex])
            self._crop_track_texes.append(tex)
            self._crop_track_fbos.append(fbo)

        tex = self._crop_track_texes[index]
        fbo = self._crop_track_fbos[index]
        if tex.size != (w, h):
            tex.release()
            fbo.release()
            tex = self.ctx.texture((w, h), components=4, dtype='f1')
            fbo = self.ctx.framebuffer(color_attachments=[tex])
            self._crop_track_texes[index] = tex
            self._crop_track_fbos[index] = fbo
        return fbo, tex

    def _apply_crop(self, source_tex, index: int, w: int, h: int,
                    crop_tuple) -> object:
        """Apply a clip's CropParams matte to a track texture (FX-M06).

        Crops the frame to the kept rectangle, zeroing alpha (and RGB, via
        premultiplication) outside it. ``feather`` widens the boundary with a
        smoothstep alpha ramp. Runs in track-FBO space (non-flipped), so the
        orientation contract (image top = low v_texcoord.y) is handled by
        :func:`crop_render.crop_gl_uniforms` with ``fbo_y_flipped=False``.

        Returns the cropped texture, or ``source_tex`` unchanged when the crop
        is identity (no pass added — audit FX-M06) or the shader is unavailable.

        Args:
            crop_tuple: ``(left, right, top, bottom, feather)`` floats, or
                None/empty for no crop.
        """
        if not crop_tuple:
            return source_tex
        if self._crop_program is None or self._crop_vao is None:
            return source_tex

        from src.features.edit.core.clip_params import CropParams
        from src.features.edit.core import crop_render

        try:
            crop = CropParams(
                left=float(crop_tuple[0]), right=float(crop_tuple[1]),
                top=float(crop_tuple[2]), bottom=float(crop_tuple[3]),
                feather=float(crop_tuple[4]),
            )
        except (TypeError, ValueError, IndexError):
            return source_tex

        # Identity crop adds no pass — output equivalent to input.
        if crop_render.crop_is_identity(crop):
            return source_tex

        target_fbo, target_tex = self._ensure_crop_track_fbo(index, w, h)
        target_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        gl_rect, feather_uv = crop_render.crop_gl_uniforms(
            crop, w, h, fbo_y_flipped=False,
        )
        self._crop_program['u_texture'] = 0
        # Crop operates in track-FBO space — must NOT apply clip transform.
        try:
            self._crop_program['u_transform'].write(_IDENTITY_MAT3_BYTES)
        except (KeyError, AttributeError):
            pass
        try:
            self._crop_program['u_crop_rect'] = gl_rect
            self._crop_program['u_feather'] = feather_uv
        except (KeyError, AttributeError):
            pass
        source_tex.use(0)
        import moderngl
        self._crop_vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)
        return target_tex

    @staticmethod
    def _grade_params_to_uniforms(grade: dict) -> dict:
        """Translate persisted ColorStack controls to shader-ready uniforms."""
        from src.features.color.core.the_decompiler import GradingMath

        params = dict((grade or {}).get('grading_params', {}) or {})

        def scalar(name: str) -> float:
            try:
                return float(params.get(name, 0.0))
            except (TypeError, ValueError):
                return 0.0

        def wheel(name: str) -> tuple[float, float, float]:
            raw = params.get(name, (0.0, 0.0, 0.0))
            try:
                hue, saturation, brightness = (float(value) for value in raw)
            except (TypeError, ValueError):
                hue, saturation, brightness = (0.0, 0.0, 0.0)
            values = GradingMath.params_to_rgb(hue, saturation, brightness)
            return (float(values[0]), float(values[1]), float(values[2]))

        return {
            'u_lift': wheel('lift'),
            'u_gamma': wheel('gamma'),
            'u_gain': wheel('gain'),
            'u_exposure': scalar('exposure'),
            'u_temp': scalar('temp'),
            'u_tint': scalar('tint'),
            'u_contrast': scalar('contrast'),
            'u_shadow': scalar('shadow'),
            'u_highlights': scalar('highlights'),
            'u_saturation': scalar('saturation'),
            'u_vibrance': scalar('vibrance'),
            'u_hue_shift': scalar('hue_shift'),
        }

    def _apply_preview_grade(
        self,
        source_tex,
        index: int,
        w: int,
        h: int,
        grade: Optional[dict],
        *,
        scratch_key: Optional[str] = None,
        pin_glo: Optional[int] = None,
    ):
        """Run an active primary ColorStack on one track without CPU readback.

        Args:
            source_tex: input texture (track texture or composite proxy).
            index: track-slot index（轨道路径用；``scratch_key`` 给定时忽略）。
            w, h: target dimensions.
            grade: ColorStack 参数快照。
            scratch_key: AUD-16603——调整剪辑专用：按稳定键在 dict 中惰性
                分配 grade 目标（容量上限 + FIFO 淘汰），不再占用轨槽索引。
                None 表示普通轨道路径（按 index 走轨槽数组）。
            pin_glo: 调整剪辑链当前仍引用的纹理 GL 名（FIFO 淘汰时跳过，
                防止正在被后续 effects/transform 消费的 grade 输出被释放）。
        """
        if not grade:
            return source_tex
        if self._grade_program is None or self._grade_vao is None:
            logger.warning("EditGpuPreviewRenderer: ColorStack grade shader unavailable")
            return source_tex

        # M05（S08）：链段按 _chain_segment 分侧——pre 段先于主栈过
        # shader（scratch 乒乓），主栈读 pre 输出；trim/post 段后置。
        # 未标记载荷按主栈之后处理（与旧行为一致，不猜段身份）。
        extras = [
            e for e in ((grade or {}).get("chain_extra_params") or ())
            if isinstance(e, dict)]
        pre_extras = [
            e for e in extras
            if e.get("_chain_segment") == "pre"
            and not e.get("_unsupported_advanced", False)]
        after_extras = [
            e for e in extras
            if e.get("_chain_segment") != "pre"
            and not e.get("_unsupported_advanced", False)]

        def _chain_grade_tex(input_tex, extra, scratch_key):
            return self._apply_preview_grade(
                input_tex, index, w, h,
                {"grading_params": {
                    k: v for k, v in extra.items()
                    if k not in ("_unsupported_advanced", "_chain_segment")}},
                scratch_key=scratch_key,
            )

        pre_tex = source_tex
        for i, extra in enumerate(pre_extras):
            pre_tex = _chain_grade_tex(pre_tex, extra, f"color-chain-pre-{i}")

        graph = grade.get("color_graph")
        if graph is not None:
            if any(e.get("_unsupported_advanced") for e in extras):
                raise RuntimeError("Color graph chain contains an unsupported advanced grade")
            renderer = getattr(self, "_color_graph_texture_renderer", None)
            if renderer is None:
                from src.features.color.core.color_graph_texture import ColorGraphTextureRenderer
                renderer = ColorGraphTextureRenderer(self.ctx)
                self._color_graph_texture_renderer = renderer
            result = renderer.render(pre_tex, graph, int(grade.get("source_frame", 0)))
            # Preserve the track output when the next track uses graph scratch.
            if scratch_key is not None:
                target_fbo, target_tex = self._ensure_adjustment_grade_fbo(scratch_key, w, h, pin_glo=pin_glo)
            elif 0 <= index <= _MAX_GRADED_TRACK_TARGETS:
                target_fbo, target_tex = self._ensure_graded_track_fbo(index, w, h)
            else:
                raise ValueError("Color graph track index is out of range")
            from src.shared.gpu.framebuffer_scope import temporary_framebuffer
            with temporary_framebuffer(self.ctx, [result]) as read_fbo:
                self.ctx.copy_framebuffer(target_fbo, read_fbo)
            for i, extra in enumerate(after_extras):
                target_tex = _chain_grade_tex(target_tex, extra, f"color-chain-{i}")
            return target_tex

        if scratch_key is not None:
            target_fbo, target_tex = self._ensure_adjustment_grade_fbo(
                scratch_key, w, h, pin_glo=pin_glo,
            )
        else:
            # AUD-16603 索引护栏：超界 index 直接跳过 grade（返回源纹理），
            # 防止畸形 track_index 触发无界纹理分配。
            if index < 0 or index > _MAX_GRADED_TRACK_TARGETS:
                logger.warning(
                    "EditGpuPreviewRenderer: grade track index %d out of "
                    "bounds (max %d); skipping grade pass",
                    index, _MAX_GRADED_TRACK_TARGETS,
                )
                return source_tex
            target_fbo, target_tex = self._ensure_graded_track_fbo(index, w, h)
        target_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)
        self._grade_program['u_input_tex'] = 0
        # Grade operates in track-FBO space — must NOT apply clip transform.
        try:
            self._grade_program['u_transform'].write(_IDENTITY_MAT3_BYTES)
        except (KeyError, AttributeError):
            pass
        pre_tex.use(0)
        for name, value in self._grade_params_to_uniforms(grade).items():
            self._grade_program[name].value = value
        import moderngl
        self._grade_vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)
        # F4.1: 调色链段（trim → post_group）主栈后置段顺序 GPU 应用。
        # 每段经独立 scratch 纹理（避免读写同一目标），与 CPU 顺序应用
        # 逐段一致；高级标记段跳过（primary 域不可表示，同 CPU 语义）。
        chain_tex = target_tex
        for i, extra in enumerate(after_extras):
            chain_tex = _chain_grade_tex(chain_tex, extra, f"color-chain-{i}")
        return chain_tex if extras else target_tex

    def _apply_clip_effects(
        self,
        source_tex,
        effects: tuple,
        w: int,
        h: int,
        source_frame: int = 0,
        timeline_frame: int | None = None,
    ):
        """Apply the clip's video filter chain to one track texture.

        Runs after :meth:`_apply_preview_grade` on the per-track texture,
        before the track is added to ``track_textures`` and composited.
        Each effect is applied in ``order`` ascending; disabled effects
        are skipped.

        ``source_tex`` is a moderngl ``Texture`` whose ``.glo`` we feed to
        the runner; the runner returns a different glo (its scratch
        texture) which we must wrap back into something the rest of this
        renderer can ``.use()``. When the chain is empty or the runner is
        unavailable, ``source_tex`` is returned unchanged.

        Args:
            source_tex: graded track texture (moderngl.Texture).
            effects: tuple of ``EffectInstance`` (frozen, thread-safe).
            w, h: texture dimensions.
            source_frame: clip-local SOURCE frame index. Kept for backward
                compatibility but NO LONGER used for keyframe sampling (see
                FX-C05).
            timeline_frame: GLOBAL timeline frame for keyframe interpolation
                (FX-C05). Effect keyframes are evaluated in timeline space
                so preview/export/scrub all agree regardless of trim/speed.
                Falls back to ``source_frame`` only when the caller did not
                pass a timeline frame (legacy callers).
        """
        if not effects or not source_tex:
            return source_tex
        runner = self._effect_runner
        if runner is None:
            # Runner disabled (shader init failed) — silently skip so the
            # grade/composite still work; only filters are dropped.
            return source_tex

        from src.features.shared.effects import get_effect_catalog
        catalog = get_effect_catalog()

        evaluated: list[tuple[object, object, dict[str, object]]] = []
        for effect in sorted(effects, key=lambda e: getattr(e, "order", 0)):
            if not getattr(effect, "is_enabled", True):
                continue
            spec = catalog.get_spec(effect.effect_type)
            if spec is None:
                continue
            # FX-C05: resolve parameter values at the GLOBAL timeline frame
            # (not source_frame). Effect keyframes live in timeline space so
            # preview / CPU fallback / export / scrub all sample the same
            # frame regardless of clip trim or speed. Source frame is for
            # media DECODE only and must never reach get_param_at_frame.
            keyframe_frame = timeline_frame if timeline_frame is not None else source_frame
            try:
                params = {
                    name: effect.get_param_at_frame(name, keyframe_frame)
                    for name in effect.params
                }
            except Exception:  # noqa: BLE001 — corrupt params shouldn't crash
                logger.warning(
                    "EditGpuPreviewRenderer: failed to sample params for "
                    "effect '%s'; skipping",
                    effect.effect_type,
                )
                continue
            evaluated.append((effect, spec, params))

        decisions = _preview_policy_engine.evaluate_chain(
            PolicyContext(
                width=w,
                height=h,
                is_playing=self._preview_degraded,
                gpu_budget_ms=self._preview_gpu_budget_ms,
                force_full=not self._preview_degraded,
            ),
            [
                (str(getattr(effect, "effect_id", "")), spec.effect_type, params)
                for effect, spec, params in evaluated
            ],
        )
        self._last_preview_policy_decisions = tuple(decisions)

        current_glo = source_tex.glo
        ran_any = False
        for (_effect, spec, original_params), decision in zip(evaluated, decisions):
            params = resolve_params_for_decision(original_params, decision)
            if params is None:
                # Explicit policy bypass: visible in diagnostics and confined
                # to this frame. The EffectInstance is never modified.
                continue
            current_glo = runner.run(
                spec, current_glo, params, w, h,
                dtype="f2" if self._effect_hdr else "f1",
            )
            ran_any = True

        if not ran_any:
            return source_tex

        # Wrap the resulting glo in a moderngl.Texture-like handle the rest
        # of this renderer can bind via .use(). We create a thin wrapper
        # rather than a real Texture because the glo is owned by the
        # runner's scratch pool and must not be released by us.
        return _ExternalTextureProxy(current_glo)

    # ── Generator shader (checker / gradient / radial) ──
    _GENERATOR_VERT = """
#version 330
in vec2 in_vert;
in vec2 in_texcoord;
out vec2 v_uv;
void main() {
    gl_Position = vec4(in_vert, 0.0, 1.0);
    v_uv = in_texcoord;
}
"""
    _GENERATOR_FRAG = """
#version 330
uniform int   u_mode;          // 1=checker, 2=linear gradient, 3=radial
uniform vec4  u_color1;
uniform vec4  u_color2;
uniform vec2  u_start_point;   // linear gradient start (normalized)
uniform vec2  u_end_point;     // linear gradient end (normalized)
uniform vec2  u_center;        // radial center (normalized)
uniform float u_radius;        // radial radius (normalized, short-edge basis)
uniform float u_feather;       // -1..1
uniform float u_checker_size;  // checker square size in pixels
uniform vec2  u_resolution;
in  vec2 v_uv;
out vec4 fragColor;

float apply_feather(float t, float f) {
    float tc = clamp(t, 0.0, 1.0);
    if (abs(f) <= 0.001) return tc;
    if (f > 0.0) {
        float pad = min(f, 0.999) * 0.5;
        if (tc <= pad) return 0.0;
        if (tc >= 1.0 - pad) return 1.0;
        float x = (tc - pad) / (1.0 - 2.0 * pad);
        return x * x * (3.0 - 2.0 * x);
    }
    float a = min(-f, 0.999) * 0.5;
    float s = tc * tc * (3.0 - 2.0 * tc);
    return a + s * (1.0 - 2.0 * a);
}

void main() {
    vec2 uv = vec2(v_uv.x, 1.0 - v_uv.y);
    if (u_mode == 1) {
        // Checker
        vec2 px = uv * u_resolution;
        float cs = max(u_checker_size, 1.0);
        float cx = floor(px.x / cs);
        float cy = floor(px.y / cs);
        float pattern = mod(cx + cy, 2.0);
        fragColor = mix(u_color1, u_color2, pattern);
    } else if (u_mode == 3) {
        // Radial gradient
        vec2 px = uv * u_resolution;
        vec2 cp = u_center * u_resolution;
        float short_edge = min(u_resolution.x, u_resolution.y);
        float d = distance(px, cp);
        float t = d / max(u_radius * short_edge, 0.0001);
        t = apply_feather(t, u_feather);
        fragColor = mix(u_color1, u_color2, t);
    } else {
        // Linear gradient (u_mode == 2)
        vec2 dir = u_end_point - u_start_point;
        float len2 = dot(dir, dir);
        float t = (len2 < 0.0001) ? 0.0 : dot(uv - u_start_point, dir) / len2;
        t = apply_feather(t, u_feather);
        fragColor = mix(u_color1, u_color2, t);
    }
}
"""

    def _ensure_generator_program(self):
        """Lazily compile the generator shader program + VAO."""
        if getattr(self, '_generator_program', None) is not None:
            return
        try:
            self._generator_program = self.ctx.program(
                vertex_shader=self._GENERATOR_VERT,
                fragment_shader=self._GENERATOR_FRAG,
            )
            self._generator_vao = self.ctx.vertex_array(
                self._generator_program,
                [(self._blend_quad_vbo, "2f 2f", "in_vert", "in_texcoord")],
            )
        except Exception as e:
            logger.warning(f"Failed to compile generator shader: {e}")
            self._generator_program = None
            self._generator_vao = None

    def _paint_solid_to_track_fbo(self, index: int, w: int, h: int,
                                   rgb: tuple, generator_params: dict | None = None) -> tuple:
        """Paint a generator pattern into the per-track FBO.

        Supports all Constant-node modes:
        - solid:    Framebuffer.clear (fastest, no shader)
        - checker:  Two-color checkerboard via generator shader
        - gradient: Linear gradient via generator shader
        - radial:   Radial gradient via generator shader

        Returns ``(track_fbo, track_tex)`` like the YUV/planar paths.
        """
        track_fbo, track_tex = self._ensure_track_fbo(index, w, h)
        track_fbo.use()
        self.ctx.viewport = (0, 0, w, h)

        mode = "solid"
        if generator_params and isinstance(generator_params, dict):
            mode = generator_params.get("mode", "solid")

        # Fast path: solid color (no shader needed)
        if mode == "solid" or generator_params is None:
            if generator_params and isinstance(generator_params, dict):
                r = float(generator_params.get("color_r", 0.0))
                g = float(generator_params.get("color_g", 0.6))
                b = float(generator_params.get("color_b", 0.8))
            else:
                r = float(rgb[0]) if len(rgb) > 0 else 0.0
                g = float(rgb[1]) if len(rgb) > 1 else 0.6
                b = float(rgb[2]) if len(rgb) > 2 else 0.8
            track_fbo.clear(red=r, green=g, blue=b, alpha=1.0)
            return track_fbo, track_tex

        # Shader path for checker / gradient / radial
        self._ensure_generator_program()
        if self._generator_program is None:
            # Fallback: just clear with primary color
            r = float(generator_params.get("color_r", 0.0))
            g = float(generator_params.get("color_g", 0.0))
            b = float(generator_params.get("color_b", 0.0))
            track_fbo.clear(red=r, green=g, blue=b, alpha=1.0)
            return track_fbo, track_tex

        # Clear stale state: disable culling/depth/blend that may linger from
        # previous render passes (culling causes one triangle of the quad strip
        # to be discarded → visible diagonal artifact).
        import moderngl
        self.ctx.disable(moderngl.CULL_FACE)
        self.ctx.disable(moderngl.DEPTH_TEST)
        self.ctx.disable(moderngl.BLEND)

        prog = self._generator_program
        # Map mode string to int
        mode_int = {"checker": 1, "gradient": 2, "radial": 3}.get(mode, 2)
        prog["u_mode"].value = mode_int
        prog["u_color1"].value = (
            float(generator_params.get("color_r", 0.0)),
            float(generator_params.get("color_g", 0.0)),
            float(generator_params.get("color_b", 0.0)),
            float(generator_params.get("color_a", 1.0)),
        )
        prog["u_color2"].value = (
            float(generator_params.get("color2_r", 1.0)),
            float(generator_params.get("color2_g", 1.0)),
            float(generator_params.get("color2_b", 1.0)),
            float(generator_params.get("color2_a", 1.0)),
        )
        prog["u_start_point"].value = (
            float(generator_params.get("start_x", 0.0)),
            float(generator_params.get("start_y", 0.5)),
        )
        prog["u_end_point"].value = (
            float(generator_params.get("end_x", 1.0)),
            float(generator_params.get("end_y", 0.5)),
        )
        prog["u_center"].value = (
            float(generator_params.get("center_x", 0.5)),
            float(generator_params.get("center_y", 0.5)),
        )
        prog["u_radius"].value = float(generator_params.get("radial_radius", 0.5))
        prog["u_feather"].value = float(generator_params.get("feather", 0.0))
        prog["u_checker_size"].value = float(generator_params.get("checker_size", 64))
        prog["u_resolution"].value = (float(w), float(h))

        self._generator_vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)
        return track_fbo, track_tex

    def _apply_adjustment_clips(self, composite_tex, adjustment_layers, w: int, h: int, frame: int):
        """Apply an Adjustment Clip's effects and Transform to its input.

        Mirrors :meth:`_apply_clip_effects` but operates on the accumulated
        lower-layer texture instead of a per-track texture. Transform is
        applied to the filtered result here; Opacity is applied by the scoped
        compositor when it mixes that result back with the unfiltered input.

        Returns ``composite_tex`` unchanged when there are no adjustment
        layers or the runner is unavailable; otherwise returns an
        ``_ExternalTextureProxy`` wrapping the runner's scratch glo.
        """
        if not adjustment_layers or not composite_tex:
            return composite_tex
        runner = self._effect_runner

        from src.features.shared.effects import get_effect_catalog
        catalog = get_effect_catalog()

        current_glo = composite_tex.glo
        changed = False
        all_decisions: list[PreviewPolicyDecision] = []
        for adj_index, adj_entry in enumerate(adjustment_layers):
            # T-P1-22：三元组 (layer, effects, grade)；旧二元组兼容
            adj_layer, effects = adj_entry[0], adj_entry[1]
            grade = adj_entry[2] if len(adj_entry) > 2 else None
            if grade:
                # ColorStack 原位 grade（与导出/预览 CPU 顺序一致：
                # grade → effects）。AUD-16603：目标按稳定键（调整剪辑
                # clip_id）在 dict 中按需分配——旧实现用 1000+ 槽位索引
                # 预留轨槽数组，首个带 grade 的调整剪辑帧会顺序创建
                # 1001 张全尺寸纹理；pin_glo 保护当前链路仍在引用的
                # grade 输出不被 FIFO 淘汰释放。
                source_for_grade = (
                    _ExternalTextureProxy(current_glo)
                    if current_glo != composite_tex.glo else composite_tex)
                grade_key = str(
                    getattr(adj_layer, 'clip_id', '') or f'adj_{adj_index}'
                )
                graded = self._apply_preview_grade(
                    source_for_grade, 0, w, h, grade,
                    scratch_key=grade_key, pin_glo=current_glo,
                )
                graded_glo = getattr(graded, 'glo', None)
                if graded_glo is not None and graded_glo != current_glo:
                    current_glo = graded_glo
                    changed = True
            if not effects or runner is None:
                source_tex = (
                    composite_tex
                    if current_glo == composite_tex.glo
                    else _ExternalTextureProxy(current_glo)
                )
                transformed = self._apply_adjustment_transform(
                    source_tex, adj_layer, w, h
                )
                transformed_glo = getattr(transformed, "glo", current_glo)
                if transformed_glo != current_glo:
                    current_glo = transformed_glo
                    changed = True
                continue
            # FX-C05: adjustment effect keyframes are evaluated at the GLOBAL
            # timeline frame (the ``frame`` arg == task.frame), NOT the
            # adjustment layer's source_frame. Source frame is decode-only.
            evaluated: list[tuple[object, object, dict[str, object]]] = []
            for effect in sorted(effects, key=lambda e: getattr(e, "order", 0)):
                if not getattr(effect, "is_enabled", True):
                    continue
                spec = catalog.get_spec(effect.effect_type)
                if spec is None:
                    continue
                try:
                    params = {
                        name: effect.get_param_at_frame(name, frame)
                        for name in effect.params
                    }
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "EditGpuPreviewRenderer: failed to sample params for "
                        "adjustment effect '%s'; skipping",
                        effect.effect_type,
                    )
                    continue
                evaluated.append((effect, spec, params))

            decisions = _preview_policy_engine.evaluate_chain(
                PolicyContext(
                    width=w,
                    height=h,
                    is_playing=self._preview_degraded,
                    gpu_budget_ms=self._preview_gpu_budget_ms,
                    force_full=not self._preview_degraded,
                ),
                [
                    (str(getattr(effect, "effect_id", "")), spec.effect_type, params)
                    for effect, spec, params in evaluated
                ],
            )
            all_decisions.extend(decisions)
            for (_effect, spec, original_params), decision in zip(evaluated, decisions):
                params = resolve_params_for_decision(original_params, decision)
                if params is None:
                    continue
                current_glo = runner.run(
                    spec, current_glo, params, w, h,
                    dtype="f2" if self._effect_hdr else "f1",
                )
                changed = True

            source_tex = (
                composite_tex
                if current_glo == composite_tex.glo
                else _ExternalTextureProxy(current_glo)
            )
            transformed = self._apply_adjustment_transform(
                source_tex, adj_layer, w, h
            )
            transformed_glo = getattr(transformed, "glo", current_glo)
            if transformed_glo != current_glo:
                current_glo = transformed_glo
                changed = True

        self._last_preview_policy_decisions = tuple(all_decisions)
        if not changed:
            return composite_tex
        return _ExternalTextureProxy(current_glo)

    def _apply_adjustment_transform(self, source_tex, adj_layer, w: int, h: int):
        """Apply an Adjustment Clip's sampled Transform to an input texture."""
        transform = getattr(adj_layer, "transform", None)
        if not transform or _transform_tuple_is_identity(transform):
            return source_tex
        return self._apply_clip_transform(
            source_tex,
            int(getattr(adj_layer, "track_index", 0)),
            w,
            h,
            transform,
        )

    def set_preview_degraded(self, degraded: bool) -> None:
        """Toggle preview-cost degradation for clip effects.

        When True (typically during active playback), expensive effects
        (``spec.preview_cost == "high"``) have their costly parameters
        reduced so the viewer stays responsive; when False (paused /
        scrubbing) the original full-quality values are used.

        The playback engine should call this on play start/stop. Until
        it does, ``_preview_degraded`` stays False and behaviour is
        unchanged from before this hook existed.
        """
        self._preview_degraded = bool(degraded)

    def get_preview_policy_diagnostics(self) -> tuple[PreviewPolicyDecision, ...]:
        """Return the immutable decisions used by the most recent chain."""
        return self._last_preview_policy_decisions

    def set_effect_hdr(self, enabled: bool) -> None:
        """FX-M01: Enable/disable HDR precision for effect intermediate surfaces."""
        self._effect_hdr = enabled

    def _get_black_texture(self, w: int, h: int):
        """Get or create a black texture for compositing base."""
        import numpy as np

        if not hasattr(self, '_black_tex') or self._black_tex is None:
            data = np.zeros((h, w, 4), dtype=np.uint8)
            self._black_tex = self.ctx.texture((w, h), components=4, dtype='f1')
            self._black_tex.write(data)
        elif self._black_tex.size != (w, h):
            self._black_tex.release()
            data = np.zeros((h, w, 4), dtype=np.uint8)
            self._black_tex = self.ctx.texture((w, h), components=4, dtype='f1')
            self._black_tex.write(data)
        return self._black_tex

    # ==================== Present Frame Protocol (Batch 1) ====================

    # ==================== Composite Snapshot (seek fast path) ====================

    _SNAPSHOT_VERTEX_SHADER = """
        #version 330
        out vec2 uv;
        void main() {
            vec2 pos = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
            uv = pos;
            gl_Position = vec4(pos * 2.0 - 1.0, 0.0, 1.0);
        }
    """

    _SNAPSHOT_FRAGMENT_SHADER = """
        #version 330
        in vec2 uv;
        out vec4 out_color;
        uniform sampler2D composite_tex;
        void main() {
            out_color = texture(composite_tex, uv);
        }
    """

    def arm_frame_snapshot(self) -> None:
        """UI 线程布防：下一帧合成完成后做一次快照 readback。

        只在暂停态 seek / scrub settle 前布防；连续布防只对下一帧生效，
        播放中每帧都布防会 reintroduce 同步 readback 成本——调用方自律。
        """
        self._snapshot_armed = True

    def _capture_frame_snapshot(
        self, final_tex, w: int, h: int, frame: int, generation_id: int,
        kind: str = "seek_snapshot",
    ) -> None:
        # A snapshot is optional; failure must not abandon the main present
        # lease before _finalize_present_frame. Keep allocation and binding
        # queries inside this boundary as well as the readback itself.
        from src.shared.gpu.framebuffer_scope import preserve_framebuffer
        try:
            with preserve_framebuffer(self.ctx):
                self._capture_frame_snapshot_impl(final_tex, w, h, frame, generation_id, kind)
        except Exception as exc:
            logger.debug("frame snapshot capture failed: %s", exc)

    def _capture_frame_snapshot_impl(
        self, final_tex, w: int, h: int, frame: int, generation_id: int,
        kind: str,
    ) -> None:
        """把合成结果缩到 1/4 分辨率 readback 成 QImage 并发射（渲染线程）。

        专用小 FBO + copy shader，不触碰 present pool 的纹理状态。best-effort：
        任何异常都吞掉（快照只是 seek 快速路径的数据源，不是关键路径）。
        """
        import moderngl
        import numpy as np
        from PyQt6.QtGui import QImage

        # 行跨距必须 4 字节对齐（Qt QImage 与 GL UNPACK_ALIGNMENT 的约定）：
        # 非 4 对齐跨距的图像被 viewer 上传纹理时每行起点错位，画面呈现
        # 对角线剪切条纹（真实 App 复现：黑白斜条纹带）。宽度向下取整到
        # 4 的倍数，RGB888 跨距 = 宽*3 即 12 的倍数。
        small_h = max(320, h // 4)
        small_w = max(4, (round(w * small_h / h) // 4) * 4)

        if (
            self._snapshot_tex is None
            or self._snapshot_fbo is None
            or self._snapshot_tex.size != (small_w, small_h)
        ):
            texture = self.ctx.texture((small_w, small_h), components=3, dtype='f1')
            try:
                framebuffer = self.ctx.framebuffer(color_attachments=[texture])
            except Exception:
                texture.release()
                raise
            old_texture, old_framebuffer = self._snapshot_tex, self._snapshot_fbo
            self._snapshot_tex, self._snapshot_fbo = texture, framebuffer
            if old_framebuffer is not None:
                old_framebuffer.release()
            if old_texture is not None:
                old_texture.release()

        if self._snapshot_program is None:
            program = self.ctx.program(
                vertex_shader=self._SNAPSHOT_VERTEX_SHADER,
                fragment_shader=self._SNAPSHOT_FRAGMENT_SHADER,
            )
            try:
                vao = self.ctx.vertex_array(program, [])
            except Exception:
                program.release()
                raise
            self._snapshot_program, self._snapshot_vao = program, vao

        try:
            self._snapshot_fbo.use()
            # W05 快照 pass 状态卫生：渲染前显式归位 scissor/viewport 并
            # 清底。旧"底部带陈旧纹素"缺陷经本包定位为测试侧 QImage
            # RGB888 越界读取（见 docs/validation/.../W05 报告），产品
            # 快照链本身逐字节确定；但共享上下文上任何残留裁剪/视口状态
            # 仍可能裁掉全屏三角——归位 + 清底把这类异常显性化为可检黑带
            # （清底后覆盖区必被重写，未覆盖区为黑而非陈旧帧内容，异常
            # 快照不再可能冒充有效内容进入 seek 缓存）。
            self.ctx.scissor = None
            self.ctx.viewport = (0, 0, int(small_w), int(small_h))
            self._snapshot_fbo.clear(0.0, 0.0, 0.0, 1.0)
            final_tex.use(0)
            self._snapshot_program["composite_tex"].value = 0
            self._snapshot_vao.render(mode=moderngl.TRIANGLES, vertices=3)
            data = self._snapshot_fbo.read(components=3, alignment=1)
            # P0-04: GPU→CPU readback 字节计入审计（这是受控的 1/4 分辨率
            # 快照路径，不是全帧回退；量级必须可见才能证明它保持受控）。
            try:
                from src.shared.video.pixel_copy_counters import (
                    CopyKind,
                    add_pixel_copy,
                )
                add_pixel_copy(CopyKind.CPU_DOWNLOAD, len(data))
            except Exception:
                pass
            # A0.1：回读证据按帧暂存（GL 线程），TPR 完成回调取走后
            # 按发起帧 trace_id 以 readback_kind=kind annotate。
            self._store_readback_stats(frame, len(data), kind)
            # GL FBO 是 y=0 在底，QImage 需要 y=0 在顶：垂直翻转
            arr = np.frombuffer(data, dtype=np.uint8).reshape(small_h, small_w, 3)
            image = QImage(
                np.ascontiguousarray(arr[::-1]).tobytes(),
                small_w, small_h, small_w * 3, QImage.Format.Format_RGB888,
            ).copy()
            self.snapshot_ready.emit(int(frame), int(generation_id), image)
            playback_trace(
                "gpu_renderer.snapshot_ready",
                frame=frame, generation=generation_id,
                width=small_w, height=small_h,
            )
        except Exception as e:
            logger.debug(f"frame snapshot capture failed: {e}")

    def _finalize_present_frame(
        self,
        *,
        final_tex,
        frame: int,
        generation_id: int,
        width: int,
        height: int,
        track_count: int,
    ) -> Optional[GpuPresentFrame]:
        """
        Composite 完成后：插入 GL fence，标记 slot READY，构造 GpuPresentFrame。

        替代旧的 glFinish() 止血（缺陷计划 RP-P0-01）。fence 让 Viewer 在
        自己的 GL context 上 glWaitSync 等待 renderer 写完，不阻塞 GPU pipeline。

        Returns:
            GpuPresentFrame 或 None（fence 创建失败时）。
        """
        # 获取当前 task 的 slot lease（由 _ensure_composite_fbo 设置）
        lease = self._current_present_lease

        # A0.1：本帧自己的 GPU query 尚未完成（读回延迟 RING_DEPTH-1 帧）。
        # 旧实现把 last_ms（约 2 帧前的结果）写进当前帧 extra，属于
        # "旧 query 冒充当前帧"——现在 descriptor 只声明 deferred 语义；
        # 发起帧的真实值由 TPR 完成 drain_completed_gpu_queries 后按
        # trace_id 回填。timer 不可用时保持 fail-closed。
        _timer = self._gl_pass_timer
        gpu_time_unavailable = _timer is None or _timer.unsupported
        gpu_extra = {
            "gpu_submit_ms": None,
            "gpu_time_unavailable": gpu_time_unavailable,
            "gpu_query_deferred": not gpu_time_unavailable,
        }

        if lease is None or not self._present_pool_registered:
            # present pool 未启用（旧路径或测试），构造无 fence 的 descriptor
            present = GpuPresentFrame(
                texture_id=final_tex.glo,
                width=width, height=height,
                frame=frame, generation_id=generation_id,
                slot_id=-1,  # 无 slot 跟踪
                fence_ns=0,
                # GPU composite FBO 渲染的纹理是标准 GL 方向（y=0 在底部）。
                # 必须标 True，否则 Viewer 会按 CPU QImage 的方式多翻一次 Y，
                # 导致画面上下颠倒。
                is_gl_oriented=True,
                extra=gpu_extra,
            )
            playback_trace(
                "gpu_renderer.frame_ready_emit",
                frame=frame, generation=generation_id,
                tex_id=final_tex.glo, width=width, height=height,
                tracks=track_count, has_fence=False,
                gpu_submit_ms=gpu_extra["gpu_submit_ms"],
                gpu_time_unavailable=gpu_time_unavailable,
            )
            self.present_frame_ready.emit(present)
            self._current_present_lease = None
            return present

        # 创建 GL fence（在 renderer context 上）
        fence_ns = 0
        fence_cleanup = None
        try:
            from OpenGL.GL import (
                glFlush, glFenceSync,
                GL_SYNC_GPU_COMMANDS_COMPLETE,
            )
            fence = glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0)
            # Make the newly-created fence visible to the shared Viewer context.
            glFlush()
            if fence is not None and fence != 0:
                # W05 修复：PyOpenGL 的 GLsync 在本机（Windows/PyOpenGL
                # 组合）以原始 bytes 返回，int(fence) 抛 "invalid
                # literal"——旧实现每帧吞异常落 fence_ns=0，Viewer 拿不
                # 到 fence 就不等渲染写入完成直接 blit（present 链潜在
                # 撕裂）。按句柄字节小端取整数（fence ID 即指针位型）。
                if isinstance(fence, bytes):
                    fence_ns = int.from_bytes(fence, "little")
                else:
                    try:
                        fence_ns = int(fence)
                    except (TypeError, ValueError):
                        fence_ns = int.from_bytes(bytes(fence), "little")
                # cleanup: Viewer 等待完成后由 ack 触发 glDeleteSync
                # （闭包持有原始 GLsync 对象，不做 int 转换）
                def _cleanup_fence(f=fence) -> None:
                    try:
                        from OpenGL.GL import glDeleteSync
                        glDeleteSync(f)
                    except Exception:
                        pass
                fence_cleanup = _cleanup_fence
        except Exception as e:
            logger.debug(f"_finalize_present_frame fence failed: {e}")
            fence_ns = 0

        # 标记 slot READY（携带 fence）
        try:
            lease.mark_ready(fence_ns=fence_ns, fence_cleanup=fence_cleanup)
        except RuntimeError as e:
            logger.debug(f"slot mark_ready failed: {e}")
            # lease 状态异常：构造无 slot 的 fallback descriptor
            present = GpuPresentFrame(
                texture_id=final_tex.glo,
                width=width, height=height,
                frame=frame, generation_id=generation_id,
                # GPU FBO 纹理：标准 GL 方向（y=0 在底部）。
                slot_id=-1, fence_ns=0, is_gl_oriented=True,
                extra=gpu_extra,
            )
            self.present_frame_ready.emit(present)
            self._current_present_lease = None
            return present

        # 构造 GpuPresentFrame（携带 slot_id + fence）
        present = lease.make_present_frame(
            frame=frame, generation_id=generation_id,
            # GPU composite FBO 渲染的纹理是标准 GL 方向。
            is_gl_oriented=True,
            extra=gpu_extra,
        )

        playback_trace(
            "gpu_renderer.frame_ready_emit",
            frame=frame, generation=generation_id,
            tex_id=final_tex.glo, width=width, height=height,
            tracks=track_count, has_fence=fence_ns != 0,
            slot_id=lease.slot_id,
            gpu_submit_ms=gpu_extra["gpu_submit_ms"],
            gpu_time_unavailable=gpu_time_unavailable,
        )

        self.present_frame_ready.emit(present)
        # lease 留在 READY/IN_VIEWER 状态，等 Viewer ack 后回收
        self._current_present_lease = None
        return present

    def ack_present_slot(self, slot_id: int) -> None:
        """
        Viewer 完成 blit 后调用，释放 slot（缺陷计划 RP-P0-02 ack 协议）。

        可在任意线程调用（OutputSlotPool 内部加锁）。Viewer 的 paintGL 完成
        glBlitFramebuffer 后应调用此方法，让 slot 回到 RELEASABLE -> FREE。
        """
        if slot_id < 0:
            return  # 无 slot 跟踪的 fallback frame
        self._present_pool.ack(slot_id)
        # AUD-16602：在途旧池帧的 ack 同步送达退役池，让其 slot 转为
        # RELEASABLE——退役纹理因此具备延迟释放条件。（重复 ack 到无
        # 关池由 OutputSlotPool.ack 内部吞掉并 debug 记录，不会崩溃。）
        for pool, *_rest in self._retired_output_pools:
            pool.ack(slot_id)
        self.slot_acked.emit(slot_id)

    def present_pool_stats(self) -> dict:
        """返回 present pool 统计（telemetry / debug 用）。"""
        return self._present_pool.stats()

    def gpu_pass_timer_stats(self) -> dict:
        """P0-03: 合成 pass GPU 时间摘要（HUD / benchmark 用）。

        unavailable=True 表示驱动/上下文不支持 timer query，
        消费方必须显示 gpu_time_unavailable。
        A0.1：附计时器健康统计（在飞 query 数 / 溢出丢弃数）；
        注意 last_ms 是"最近完成"值，不属于当前帧——帧级归因必须走
        drain_completed_gpu_queries 的发起帧回填。
        """
        timer = self._gl_pass_timer
        if timer is None or timer.unsupported:
            return {"unavailable": True, "gpu_submit_ms": None}
        ms = timer.last_ms("composite")
        stats = {
            "unavailable": False,
            # None = 尚无读回结果（启动后前几帧），与 0.0 区分
            "gpu_submit_ms": ms,
        }
        health = getattr(timer, "stats", None)
        if callable(health):
            try:
                stats["timer_health"] = health()
            except Exception:
                pass
        return stats


    def _release_previous_frames(self) -> None:
        """Release previous GPU frames after compositing.

        External GPU sources cannot be released until the GPU has finished
        sampling them. This covers both IOSurface-backed source frames and
        CompositeGpuFrameHandle leases. Instead of blocking the render thread
        every frame, attach a fence to the source release and drain completed
        fences on later iterations.
        """
        frames = list(self._previous_gpu_frames)
        self._previous_gpu_frames.clear()
        if not frames:
            self._task_used_io_surface_source = False
            self._drain_deferred_gpu_frame_releases(block=False)
            return

        if not getattr(self, "_task_used_io_surface_source", False):
            self._release_gpu_frame_list(frames)
            self._drain_deferred_gpu_frame_releases(block=False)
            return

        sync = self._create_source_release_fence()
        self._task_used_io_surface_source = False
        if sync:
            self._deferred_gpu_frame_releases.append((sync, frames))
            self._drain_deferred_gpu_frame_releases(block=False)
            if len(self._deferred_gpu_frame_releases) > 8:
                self._drain_deferred_gpu_frame_releases(block=True, max_to_drain=1)
            return

        # Rare fallback: if fence creation is unavailable, block for correctness.
        self._sync_gpu_source_reads(force=True)
        self._release_gpu_frame_list(frames)

    def _release_gpu_frame_list(self, frames: list) -> None:
        from src.shared.media.gpu_frame import safe_release_gpu_frame

        for frame in frames:
            release = getattr(frame, "release", None)
            if callable(release):
                try:
                    release()
                    continue
                except Exception:
                    pass
            safe_release_gpu_frame(frame)

    def _create_source_release_fence(self):
        """Create a GL fence after source textures have been sampled."""
        try:
            from OpenGL.GL import (
                glFlush,
                glFenceSync,
                GL_SYNC_GPU_COMMANDS_COMPLETE,
            )

            sync = glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0)
            # Polling/reusing a source slot is only safe after the fence itself
            # has entered the GPU command stream.
            glFlush()
            if sync is None or sync == 0:
                return None
            return sync
        except Exception as e:
            logger.debug(f"source release fence creation failed: {e}")
            return None

    def _drain_deferred_gpu_frame_releases(
        self, *, block: bool = False, max_to_drain: int | None = None
    ) -> None:
        """Release external source leases whose GPU-read fence completed."""
        pending = getattr(self, "_deferred_gpu_frame_releases", [])
        if not pending:
            return

        try:
            from OpenGL.GL import (
                glClientWaitSync,
                glDeleteSync,
                glFinish,
                GL_ALREADY_SIGNALED,
                GL_CONDITION_SATISFIED,
            )
        except Exception:
            if block:
                try:
                    # Fence API unavailable: block-only fallback before releasing frames.
                    from OpenGL.GL import glFinish
                    glFinish()
                except Exception:
                    pass
                for _, frames in pending:
                    self._release_gpu_frame_list(frames)
                pending.clear()
            return

        remaining: list[tuple[int, list]] = []
        drained = 0
        timeout_ns = 50_000_000 if block else 0
        for sync, frames in pending:
            if max_to_drain is not None and drained >= max_to_drain:
                remaining.append((sync, frames))
                continue
            try:
                result = glClientWaitSync(sync, 0, timeout_ns)
            except Exception:
                result = None
            if result in (GL_ALREADY_SIGNALED, GL_CONDITION_SATISFIED):
                try:
                    glDeleteSync(sync)
                except Exception:
                    pass
                self._release_gpu_frame_list(frames)
                drained += 1
            elif block:
                try:
                    # Fence still pending in block cleanup: rare fallback before forced release.
                    glFinish()
                except Exception:
                    pass
                try:
                    glDeleteSync(sync)
                except Exception:
                    pass
                self._release_gpu_frame_list(frames)
                drained += 1
            else:
                remaining.append((sync, frames))
        self._deferred_gpu_frame_releases = remaining

    def _sync_gpu_source_reads(self, *, force: bool = False) -> None:
        """
        Insert a GL sync barrier so the GPU finishes sampling source textures
        (especially IOSurface-backed VT planes) before their backing is freed
        or rebound on the next frame.

        Uses glFlush() + a fence sync with bounded wait. On macOS this is
        required because CGLTexImageIOSurface2D textures are backed by an
        IOSurface that the VT session may recycle as soon as the
        CVPixelBuffer's retain count hits zero.

        Cheap on the render thread (which is already serial per-task); the
        GPU typically finishes a 1080p/4K NV12 composite within the frame
        budget.
        """
        # Track whether the current task sampled any IOSurface-backed (VT
        # zero-copy) source texture. Only fence when needed.
        if not force and not getattr(self, "_task_used_io_surface_source", False):
            return
        try:
            from OpenGL.GL import (
                glFlush,
                glFenceSync,
                glClientWaitSync,
                glDeleteSync,
                GL_SYNC_GPU_COMMANDS_COMPLETE,
            )

            sync = glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0)
            # Submit the fence itself before waiting on it. Flushing first can
            # leave the marker buffered, making a zero-flag client wait stale.
            glFlush()
            if sync is None or sync == 0:
                return
            try:
                # Bounded wait: poll a few times, then give up and rely on
                # the next glFinish-equivalent (glFlush at task boundary).
                # In practice the fence is signalled within ~1-3 ms for a
                # single composite pass.
                timeout_ns = 50_000_000  # 50 ms hard cap
                result = glClientWaitSync(sync, 0, timeout_ns)
                # result is GL_ALREADY_SIGNALED / GL_CONDITION_SATISFIED /
                # GL_TIMEOUT_EXPIRED / GL_WAIT_FAILED
                _ = result  # result logged at debug if needed
            finally:
                try:
                    glDeleteSync(sync)
                except Exception:
                    pass
        except ImportError:
            # PyOpenGL sync APIs unavailable — fall back to glFinish for
            # correctness over speed. Worst case a few ms of pipeline stall.
            try:
                from OpenGL.GL import glFinish
                glFinish()
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"_sync_gpu_source_reads fence failed: {e}")
            try:
                from OpenGL.GL import glFinish
                glFinish()
            except Exception:
                pass
        finally:
            self._task_used_io_surface_source = False

    def _cleanup(self) -> None:
        """Release all GL resources."""
        graph_renderer = getattr(self, "_color_graph_texture_renderer", None)
        if graph_renderer is not None:
            graph_renderer.release()
            self._color_graph_texture_renderer = None
        # Release queued tasks that were accepted but never processed.
        while True:
            try:
                pending = self._queue.get_nowait()
            except Exception:
                break
            self._release_task_sources(pending)

        # Sync before releasing any remaining IOSource-backed frames.
        self._task_used_io_surface_source = bool(self._previous_gpu_frames)
        self._release_previous_frames()
        self._drain_deferred_gpu_frame_releases(block=True)

        for tex in self._track_texes:
            try:
                tex.release()
            except Exception:
                pass
        self._track_texes.clear()

        for fbo in self._track_fbos:
            try:
                fbo.release()
            except Exception:
                pass
        self._track_fbos.clear()

        for tex in self._graded_track_texes:
            try:
                tex.release()
            except Exception:
                pass
        self._graded_track_texes.clear()

        for fbo in self._graded_track_fbos:
            try:
                fbo.release()
            except Exception:
                pass
        self._graded_track_fbos.clear()

        # AUD-16603：调整剪辑 grade scratch（稳定键 dict，容量上限内）。
        for key in list(self._adjustment_grade_texes):
            self._release_adjustment_grade_entry(key)

        # AUD-16602：退役输出池无条件排空（shutdown 时 Viewer 不再 ack）。
        self._drain_retired_output_pools(force=True)

        # CFA debayer（TRAW 回放）：4×R16UI 纹理 + program/VAO/VBO
        if self._cfa_debayer is not None:
            try:
                self._cfa_debayer.release()
            except Exception:
                pass
        self._cfa_debayer = None
        self._cfa_debayer_bd = None
        self._cfa_gamut_unknown_logged = False  # RC3：未知相机仅提示一次

        for tex in self._transform_track_texes:
            try:
                tex.release()
            except Exception:
                pass
        self._transform_track_texes.clear()

        for fbo in self._transform_track_fbos:
            try:
                fbo.release()
            except Exception:
                pass
        self._transform_track_fbos.clear()

        # FX-M06: crop matte pass track textures/FBOs.
        for tex in self._crop_track_texes:
            try:
                tex.release()
            except Exception:
                pass
        self._crop_track_texes.clear()

        for fbo in self._crop_track_fbos:
            try:
                fbo.release()
            except Exception:
                pass
        self._crop_track_fbos.clear()

        # P0-PREV-GPU-T: release the transition ping-pong surfaces and the
        # legacy compatibility mask texture. Production jobs only upload a
        # small uniform/control block and never write this texture.
        for tex in self._transition_scratch_texes:
            try:
                tex.release()
            except Exception:
                pass
        self._transition_scratch_texes.clear()
        for fbo in self._transition_scratch_fbos:
            try:
                fbo.release()
            except Exception:
                pass
        self._transition_scratch_fbos.clear()
        if self._transition_mask_tex is not None:
            try:
                self._transition_mask_tex.release()
            except Exception:
                pass
            self._transition_mask_tex = None
        for attr in (
            "_full_frame_transition_vao",
            "_full_frame_transition_program",
        ):
            resource = getattr(self, attr, None)
            if resource is not None:
                try:
                    resource.release()
                except Exception:
                    pass
                setattr(self, attr, None)

        # FX-M01: release the effect runner's scratch pool / program cache.
        # Previously this was leaked — _cleanup released every other GL
        # resource but never called runner.release(), leaving the runner's
        # three scratch textures + compiled effect programs dangling until
        # the context itself was torn down.
        if self._effect_runner is not None:
            try:
                self._effect_runner.release()
            except Exception:
                pass

        # Release output pool
        for tex, fbo in self._output_pool:
            try:
                tex.release()
            except Exception:
                pass
            try:
                fbo.release()
            except Exception:
                pass
        self._output_pool.clear()
        self._composite_tex = None
        self._composite_fbo = None

        # Release multi-track composite scratch textures
        for attr in ('_composite_scratch_a', '_composite_scratch_b',
                     '_composite_scratch_a_fbo', '_composite_scratch_b_fbo'):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.release()
                except Exception:
                    pass
                setattr(self, attr, None)

        if hasattr(self, '_black_tex') and self._black_tex is not None:
            try:
                self._black_tex.release()
            except Exception:
                pass

        if self._vt_import_bridge is not None:
            try:
                self._vt_import_bridge.destroy()
            except Exception:
                pass
            self._vt_import_bridge = None

        # B2.1：cuda 驻留 interop 会话（GL 线程 destroy：注销全部资源并
        # 释放会话纹理）。
        if self._cuda_interop_session is not None:
            try:
                self._cuda_interop_session.destroy()
            except Exception:
                pass
            self._cuda_interop_session = None

        # AUD-16605：复合纹理缓存（≤268MB 预算，条目为全分辨率 tex+fbo）
        # 此前从未在 _cleanup 释放——renderer stop/重建（非进程退出）时
        # 全部驻留共享 share group 直至进程消亡。先挂起失效标记，再由
        # GL 线程 drain 释放全部条目（与 1984 行运行期失效同一套机制）。
        self.invalidate_composite_cache()
        self._drain_composite_cache_invalidate()

        # AUD-16605：bg composite / snapshot FBO 与惰性创建的快照 copy
        # program/VAO 此前同样泄漏。
        for attr in ('_bg_composite_tex', '_bg_composite_fbo',
                     '_snapshot_tex', '_snapshot_fbo'):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.release()
                except Exception:
                    pass
                setattr(self, attr, None)
        self._bg_composite_size = None

        # AUD-16605：YUV/grade/transform/crop/generator/blend/transition/
        # dip/snapshot program、各 VAO 与 quad VBO 此前从未释放——
        # _init_shaders/_init_quad 编译的全部着色器与顶点数组在 renderer
        # 重建时反复泄漏。（_quad_vao 是 _blend_vao 的别名，重复 release
        # 由 try/except 吞掉。）
        for attr in (
            '_yuv_program', '_yuv_rect_program', '_yuv3_program',
            '_grade_program', '_grade_vao',
            '_transform_program', '_transform_vao',
            '_crop_program', '_crop_vao',
            '_generator_program', '_generator_vao',
            '_blend_program', '_quad_vao', '_blend_vao', '_flip_blend_vao',
            '_yuv2_vao', '_yuv_rect_vao', '_yuv3_vao',
            '_transition_program', '_transition_vao',
            '_dip_program', '_dip_vao', '_flip_dip_vao',
            '_snapshot_program', '_snapshot_vao',
            '_quad_vbo', '_blend_quad_vbo',
        ):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.release()
                except Exception:
                    pass
                setattr(self, attr, None)

        # AUD-16605：GL timer query（in-flight query 对象）与 YUV 上传器
        # （PBO/纹理池 + program）自身的 GL 资源一并释放。
        if self._gl_pass_timer is not None:
            try:
                self._gl_pass_timer.release()
            except Exception:
                pass
            self._gl_pass_timer = None
        if self._yuv_uploader is not None:
            try:
                self._yuv_uploader.release()
            except Exception:
                pass
            self._yuv_uploader = None

        # AUD-16605：成功路径显式销毁 shared context/surface。此前两者
        # 随进程消亡——现代码里 moderngl ctx.release() 不会替我们销毁
        # QOpenGLContext/QOffscreenSurface。上下文此前 moveToThread 到
        # 渲染线程且该线程无事件循环，deleteLater 不会被处理：先把亲和
        # 性移回 GUI 线程，再通过 _gl_teardown_requested 信号（队列连
        # 接）让 GUI 线程执行 deleteLater/surface.destroy()。进程正在退
        # 出（无 QCoreApplication）时跳过，由 OS 回收。顺序：先 release
        # moderngl（需要 ctx current），再 doneCurrent/移交销毁。
        ctx_handle = self.shared_pyqt_context
        surface_handle = self.surface
        self.shared_pyqt_context = None
        self.surface = None

        if self.ctx is not None:
            self.ctx.release()
            self.ctx = None

        if ctx_handle is not None or surface_handle is not None:
            from PyQt6.QtCore import QCoreApplication
            app = QCoreApplication.instance()
            if app is not None:
                if ctx_handle is not None:
                    try:
                        ctx_handle.doneCurrent()
                    except Exception:
                        pass
                    try:
                        ctx_handle.moveToThread(app.thread())
                    except Exception:
                        pass
                self._gl_teardown_requested.emit(ctx_handle, surface_handle)

        logger.info("EditGpuPreviewRenderer: cleaned up")

    @staticmethod
    def _teardown_gl_handles_on_gui(
        ctx_handle: Optional[QOpenGLContext],
        surface_handle: Optional[QOffscreenSurface],
    ) -> None:
        """GUI 线程槽：销毁 renderer 的 shared context/surface（AUD-16605）。"""
        try:
            if ctx_handle is not None:
                ctx_handle.deleteLater()
        except Exception:
            pass
        try:
            if surface_handle is not None:
                surface_handle.destroy()
        except Exception:
            pass
