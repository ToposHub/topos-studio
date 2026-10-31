"""
macOS VideoToolbox Zero-Copy Probe
独立 probe、benchmark、颜色自检。

## 职责

- 检测 macOS VT 零拷贝能力
- 验证 IOSurface -> GL texture bridge
- 运行 benchmark（decode + bridge + YUV shader render）
- 可选颜色 self-test
- 输出 JSON 结果

## Usage

    python -m src.core.media.macos_vt.vt_zero_copy_probe \
        --file "/path/to/4k_h264.mp4" \
        --frames 120 \
        --stage full \
        --output-json tests/media/results/macos_iosurface_gl_bridge_probe.json

## 禁止

- frame.to_ndarray()
- frame.transfer()
- Texture.write(video_frame_bytes)
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import platform
import sys
import time
from pathlib import Path
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))


@dataclass
class ZeroCopyProbeResult:
    """零拷贝 probe 结果"""
    probe_success: bool = False
    platform: str = ""
    stage: str = "decode-only"
    vt_session_created: bool = False
    decoded_pixel_buffers: int = 0
    cvpixelbuffer_iosurface_backed: bool = False
    pixel_format: str = ""
    cpu_download_frames: int = 0
    iosurface_present: bool = False
    gl_context_created: bool = False
    bridge_initialized: bool = False
    gl_texture_created: bool = False
    texture_target: str = ""
    planes_bound: int = 0
    planes: list[dict] = field(default_factory=list)
    texture_write_frames: int = 0
    render_fbo_created: bool = False
    yuv_shader_compiled: bool = False
    frames_rendered: int = 0
    decode_path: str = ""
    zero_copy: bool = False
    frames_tested: int = 0
    fallback_reason: str | None = None
    decode_ms: dict = field(default_factory=dict)
    external_bind_ms: dict = field(default_factory=dict)
    yuv_render_ms: dict = field(default_factory=dict)
    total_ms: dict = field(default_factory=dict)
    color_verification: dict | None = None


def _percentile(values: list[float], p: float) -> float:
    """计算百分位值"""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = int(len(sorted_vals) * p / 100.0)
    idx = min(idx, len(sorted_vals) - 1)
    return sorted_vals[idx]


def _stats_dict(values: list[float]) -> dict:
    """生成统计字典"""
    if not values:
        return {"p50": 0, "p95": 0, "max": 0, "avg": 0, "count": 0}
    return {
        "p50": round(_percentile(values, 50), 3),
        "p95": round(_percentile(values, 95), 3),
        "max": round(max(values), 3),
        "avg": round(sum(values) / len(values), 3),
        "count": len(values),
    }


def _yuv_sample_to_rgb(
    y_value: float,
    cb_value: float,
    cr_value: float,
    *,
    color_range: str,
    color_matrix: str,
) -> list[int]:
    """Convert one normalized YUV sample with the preview shader's math."""
    range_mul = 1.0 if color_range == 'full' else 219.0 / 255.0
    range_off = 0.0 if color_range == 'full' else 16.0 / 255.0
    y = max((y_value - range_off) / range_mul, 0.0)
    c_off = 0.0 if color_range == 'full' else 128.0 / 255.0
    c_scale = 1.0 if color_range == 'full' else 255.0 / 224.0
    cb = (cb_value - (0.5 if color_range == 'full' else c_off)) * c_scale
    cr = (cr_value - (0.5 if color_range == 'full' else c_off)) * c_scale
    if color_matrix == 'bt601':
        rgb = (y + 1.402 * cr, y - 0.344136 * cb - 0.714136 * cr, y + 1.772 * cb)
    elif color_matrix == 'bt709':
        rgb = (y + 1.5748 * cr, y - 0.1873 * cb - 0.4681 * cr, y + 1.8556 * cb)
    else:
        rgb = (y + 1.4746 * cr, y - 0.1646 * cb - 0.5714 * cr, y + 1.8814 * cb)
    return [int(round(max(0.0, min(1.0, value)) * 255.0)) for value in rgb]


def _pixel_buffer_center_rgb_reference(
    pixel_buffer: object,
    *,
    width: int,
    height: int,
    pixel_format: str,
    color_range: str = 'limited',
    color_matrix: str = 'bt709',
) -> list[int] | None:
    """Read one VT pixel-buffer sample for off-hot-path probe validation."""
    try:
        from .vt_session import _CV_LIB
    except ImportError:
        from src.shared.media.macos_vt.vt_session import _CV_LIB

    if _CV_LIB is None:
        return None
    try:
        if isinstance(pixel_buffer, int):
            pointer = int(pixel_buffer)
        else:
            import objc
            pointer = int(objc.pyobjc_id(pixel_buffer))
        if pointer == 0:
            return None

        cv_buffer = ctypes.c_void_p(pointer)
        if _CV_LIB.CVPixelBufferLockBaseAddress(cv_buffer, 1) != 0:
            return None
        try:
            x = width // 2
            y = height // 2
            y_base = int(_CV_LIB.CVPixelBufferGetBaseAddressOfPlane(cv_buffer, 0) or 0)
            uv_base = int(_CV_LIB.CVPixelBufferGetBaseAddressOfPlane(cv_buffer, 1) or 0)
            y_stride = int(_CV_LIB.CVPixelBufferGetBytesPerRowOfPlane(cv_buffer, 0))
            uv_stride = int(_CV_LIB.CVPixelBufferGetBytesPerRowOfPlane(cv_buffer, 1))
            if not y_base or not uv_base or not y_stride or not uv_stride:
                return None

            normalized_format = pixel_format.lower()
            if normalized_format == 'p010':
                y_raw = ctypes.c_uint16.from_address(y_base + y * y_stride + x * 2).value
                uv_offset = (y // 2) * uv_stride + (x // 2) * 4
                cb_raw = ctypes.c_uint16.from_address(uv_base + uv_offset).value
                cr_raw = ctypes.c_uint16.from_address(uv_base + uv_offset + 2).value
                denominator = 65535.0
            else:
                y_raw = ctypes.c_ubyte.from_address(y_base + y * y_stride + x).value
                uv_offset = (y // 2) * uv_stride + (x // 2) * 2
                cb_raw = ctypes.c_ubyte.from_address(uv_base + uv_offset).value
                cr_raw = ctypes.c_ubyte.from_address(uv_base + uv_offset + 1).value
                denominator = 255.0

            return _yuv_sample_to_rgb(
                y_raw / denominator,
                cb_raw / denominator,
                cr_raw / denominator,
                color_range=color_range,
                color_matrix=color_matrix,
            )
        finally:
            _CV_LIB.CVPixelBufferUnlockBaseAddress(cv_buffer, 1)
    except Exception as error:
        logger.debug(f"Zero-copy probe reference read failed: {error}")
        return None


def _pixel_buffer_has_iosurface(pixel_buffer: object) -> bool:
    """检测 CVPixelBuffer 是否有 IOSurface backing（AUD-35315）。

    优先相对导入（包内运行），失败退回绝对导入；两条导入路径都不可用
    或探测调用失败时显式记日志并返回 False——此前循环内硬编码绝对导入
    且被裸 except 静默吞掉，相对导入环境下 probe 会把真零拷贝误判为
    非零拷贝。
    """
    try:
        from .vt_session import _CV_LIB
    except ImportError:
        try:
            from src.shared.media.macos_vt.vt_session import _CV_LIB
        except ImportError as error:
            logger.warning(f"Zero-copy probe: vt_session import failed: {error}")
            return False
    if _CV_LIB is None:
        return False
    try:
        if isinstance(pixel_buffer, int):
            io_ptr = _CV_LIB.CVPixelBufferGetIOSurface(
                ctypes.c_void_p(pixel_buffer))
            return io_ptr is not None and io_ptr != 0
        from Quartz.CoreVideo import CVPixelBufferGetIOSurface
        return CVPixelBufferGetIOSurface(pixel_buffer) is not None
    except (ImportError, AttributeError, TypeError, ctypes.ArgumentError) as error:
        logger.debug(f"Zero-copy probe: IOSurface detection failed: {error}")
        return False


# 内联 NV12/P010 YUV -> RGB shader（最小化依赖，不引入 YUVGPUUploader）
_NV12_YUV_SHADER = '''
#version 330
uniform sampler2D y_plane;
uniform sampler2D uv_plane;
uniform int yuv_matrix;      // 0=BT.601, 1=BT.709, 2=BT.2020
uniform int color_range;     // 0=limited (16-235), 1=full (0-255)
uniform int uv_component_mode; // 0=RG, 1=RA (legacy GL_LUMINANCE_ALPHA)
in vec2 v_texcoord;
out vec4 frag_color;

// BT.601 coefficients (SDR)
const mat3 BT601 = mat3(
    1.164,  1.164,  1.164,
    0.0,   -0.391,  2.017,
    1.596, -0.813,  0.0
);
// BT.709 coefficients (HD)
const mat3 BT709 = mat3(
    1.164,  1.164,  1.164,
    0.0,   -0.213,  2.112,
    1.793, -0.533,  0.0
);
// BT.2020 coefficients (UHD)
const mat3 BT2020 = mat3(
    1.164,  1.164,  1.164,
    0.0,   -0.164,  2.112,
    1.793, -0.533,  0.0  // simplified
);

void main() {
    float y = texture(y_plane, v_texcoord).r;
    vec4 uv_sample = texture(uv_plane, v_texcoord);
    vec2 uv = uv_component_mode == 1 ? uv_sample.ra : uv_sample.rg;
    float cb = uv.r;
    float cr = uv.g;
    float range_mul = color_range == 1 ? 1.0 : (219.0 / 255.0);
    float range_off = color_range == 1 ? 0.0 : (16.0 / 255.0);
    float chroma_off = color_range == 1 ? 0.5 : (128.0 / 255.0);
    float chroma_scale = color_range == 1 ? 1.0 : (255.0 / 224.0);
    y = max((y - range_off) / range_mul, 0.0);
    cb = (cb - chroma_off) * chroma_scale;
    cr = (cr - chroma_off) * chroma_scale;
    float r;
    float g;
    float b;
    if (yuv_matrix == 0) {
        r = y + 1.402 * cr;
        g = y - 0.344136 * cb - 0.714136 * cr;
        b = y + 1.772 * cb;
    } else if (yuv_matrix == 1) {
        r = y + 1.5748 * cr;
        g = y - 0.1873 * cb - 0.4681 * cr;
        b = y + 1.8556 * cb;
    } else {
        r = y + 1.4746 * cr;
        g = y - 0.1646 * cb - 0.5714 * cr;
        b = y + 1.8814 * cb;
    }
    frag_color = vec4(clamp(r, 0.0, 1.0), clamp(g, 0.0, 1.0), clamp(b, 0.0, 1.0), 1.0);
}
'''

_NV12_YUV_RECT_SHADER = '''
#version 330
#extension GL_ARB_texture_rectangle : enable
uniform sampler2DRect y_plane;
uniform sampler2DRect uv_plane;
uniform vec2 y_size;
uniform vec2 uv_size;
uniform int yuv_matrix;      // 0=BT.601, 1=BT.709, 2=BT.2020
uniform int color_range;     // 0=limited (16-235), 1=full (0-255)
uniform int uv_component_mode; // 0=RG, 1=RA (legacy GL_LUMINANCE_ALPHA)
in vec2 v_texcoord;
out vec4 frag_color;

const mat3 BT601 = mat3(
    1.164,  1.164,  1.164,
    0.0,   -0.391,  2.017,
    1.596, -0.813,  0.0
);
const mat3 BT709 = mat3(
    1.164,  1.164,  1.164,
    0.0,   -0.213,  2.112,
    1.793, -0.533,  0.0
);
const mat3 BT2020 = mat3(
    1.164,  1.164,  1.164,
    0.0,   -0.164,  2.112,
    1.793, -0.533,  0.0
);

void main() {
    float y = texture(y_plane, v_texcoord * y_size).r;
    vec4 uv_sample = texture(uv_plane, v_texcoord * uv_size);
    vec2 uv = uv_component_mode == 1 ? uv_sample.ra : uv_sample.rg;
    float cb = uv.r;
    float cr = uv.g;
    float range_mul = color_range == 1 ? 1.0 : (219.0 / 255.0);
    float range_off = color_range == 1 ? 0.0 : (16.0 / 255.0);
    float chroma_off = color_range == 1 ? 0.5 : (128.0 / 255.0);
    float chroma_scale = color_range == 1 ? 1.0 : (255.0 / 224.0);
    y = max((y - range_off) / range_mul, 0.0);
    cb = (cb - chroma_off) * chroma_scale;
    cr = (cr - chroma_off) * chroma_scale;
    float r;
    float g;
    float b;
    if (yuv_matrix == 0) {
        r = y + 1.402 * cr;
        g = y - 0.344136 * cb - 0.714136 * cr;
        b = y + 1.772 * cb;
    } else if (yuv_matrix == 1) {
        r = y + 1.5748 * cr;
        g = y - 0.1873 * cb - 0.4681 * cr;
        b = y + 1.8556 * cb;
    } else {
        r = y + 1.4746 * cr;
        g = y - 0.1646 * cb - 0.5714 * cr;
        b = y + 1.8814 * cb;
    }
    frag_color = vec4(clamp(r, 0.0, 1.0), clamp(g, 0.0, 1.0), clamp(b, 0.0, 1.0), 1.0);
}
'''

_VERT_SHADER = '''
#version 330
in vec2 in_vert;
in vec2 in_text;
out vec2 v_texcoord;
void main() {
    gl_Position = vec4(in_vert, 0.0, 1.0);
    v_texcoord = in_text;
}
'''


def _create_standalone_gl_context():
    """创建 standalone ModernGL context（headless）"""
    try:
        import moderngl
        ctx = moderngl.create_standalone_context(require=330)
        return ctx
    except Exception as e:
        logger.error(f"Failed to create standalone GL context: {e}")
        return None


def _create_yuv_shader(ctx, rectangle: bool = False):
    """编译内联 YUV shader"""
    try:
        shader = _NV12_YUV_RECT_SHADER if rectangle else _NV12_YUV_SHADER
        prog = ctx.program(vertex_shader=_VERT_SHADER, fragment_shader=shader)
        return prog
    except Exception as e:
        logger.error(f"Failed to compile YUV shader: {e}")
        return None


def _create_quad_vao(ctx, prog):
    """创建 fullscreen quad VAO"""
    try:
        import array
        vertices = ctx.buffer(array.array('f', [
            -1.0, -1.0, 0.0, 0.0,
            -1.0,  1.0, 0.0, 1.0,
             1.0, -1.0, 1.0, 0.0,
            -1.0,  1.0, 0.0, 1.0,
             1.0, -1.0, 1.0, 0.0,
             1.0,  1.0, 1.0, 1.0,
        ]).tobytes())
        vao = ctx.simple_vertex_array(
            prog, vertices,
            'in_vert', 'in_text',
        )
        return vao
    except Exception as e:
        logger.error(f"Failed to create quad VAO: {e}")
        return None


def _bind_external_texture(texture_id: int, target: int, location: int) -> None:
    """使用 PyOpenGL 绑定外部纹理到指定 texture unit"""
    try:
        from OpenGL.GL import glActiveTexture, glBindTexture, GL_TEXTURE0
        glActiveTexture(GL_TEXTURE0 + location)
        glBindTexture(target, texture_id)
    except Exception as e:
        logger.warning(f"Failed to bind external texture: {e}")


def run_probe(
    file_path: str,
    frames: int = 120,
    warmup: int = 10,
    stage: str = "full",
    verify_color: bool = False,
) -> ZeroCopyProbeResult:
    """
    运行零拷贝 probe。

    Args:
        file_path: 视频文件路径
        frames: 测试帧数
        warmup: 预热帧数
        stage: 测试阶段（decode-only, bridge-only, full）
        verify_color: 是否进行颜色自检

    Returns:
        ZeroCopyProbeResult
    """
    result = ZeroCopyProbeResult(
        platform=platform.system(),
        stage=stage,
        decode_path="macos_vt_iosurface_zero_copy",
    )

    if platform.system() != 'Darwin':
        result.fallback_reason = "not_macos"
        logger.warning(f"Zero-copy probe: not on macOS (platform={platform.system()})")
        return result

    try:
        from .packet_demux import PacketDemuxer
        from .vt_session import VtSession, is_vt_available
        from .iosurface_gl_bridge import IOSurfaceGLBridge
        # 可用性探测导入（触发 try/except 回退路径），名字本身不在此使用
        from .external_texture import ExternalGLTexture  # noqa: F401
        from ..gpu_frame import GpuFramePlane, GpuFrameRelease  # noqa: F401
    except ImportError:
        from src.shared.media.macos_vt.packet_demux import PacketDemuxer
        from src.shared.media.macos_vt.vt_session import VtSession, is_vt_available
        from src.shared.media.macos_vt.iosurface_gl_bridge import IOSurfaceGLBridge

    if not is_vt_available():
        result.fallback_reason = "vt_not_available"
        logger.warning("Zero-copy probe: VT not available (PyObjC missing?)")
        return result

    # Stage-specific setup
    ctx = None
    bridge = None
    yuv_prog = None
    yuv_rect_prog = None
    quad_vao = None
    quad_rect_vao = None
    output_fbo = None
    output_tex = None

    if stage in ("bridge-only", "full"):
        ctx = _create_standalone_gl_context()
        if ctx is None:
            result.fallback_reason = "gl_context_creation_failed"
            return result
        result.gl_context_created = True
        logger.info("Zero-copy probe: standalone GL context created")

        bridge = IOSurfaceGLBridge(ring_size=4)
        if not bridge.initialize(planes_per_frame=2):
            result.fallback_reason = "bridge_init_failed"
            ctx.release()
            return result
        result.bridge_initialized = True
        logger.info("Zero-copy probe: IOSurfaceGLBridge initialized")

    if stage == "full":
        yuv_prog = _create_yuv_shader(ctx)
        if yuv_prog is None:
            result.fallback_reason = "yuv_shader_compile_failed"
            if bridge:
                bridge.destroy()
            ctx.release()
            return result
        yuv_rect_prog = _create_yuv_shader(ctx, rectangle=True)
        result.yuv_shader_compiled = True

        quad_vao = _create_quad_vao(ctx, yuv_prog)
        if quad_vao is None:
            result.fallback_reason = "quad_vao_creation_failed"
            if bridge:
                bridge.destroy()
            ctx.release()
            return result
        if yuv_rect_prog is not None:
            quad_rect_vao = _create_quad_vao(ctx, yuv_rect_prog)

        logger.info("Zero-copy probe: YUV shader and VAO ready")

    demuxer = PacketDemuxer()
    try:
        info = demuxer.open(file_path)
        logger.info(f"Zero-copy probe: opened {file_path}, codec={info.codec_name}, {info.width}x{info.height}")

        session = VtSession()
        pixel_format = 'nv12' if info.bit_depth <= 8 else 'p010'
        created = session.create(
            width=info.width,
            height=info.height,
            codec=info.codec_name,
            pixel_format=pixel_format,
            extradata=info.extradata,
        )
        result.vt_session_created = created

        if not created:
            result.fallback_reason = "vt_session_creation_failed"
            # AUD-35313：早退路径必须清理已创建的 bridge 与 standalone GL
            # context——此前仅成功路径末尾与异常路径清理，此分支泄漏 GL
            # 纹理环与离屏上下文。
            if bridge is not None:
                try:
                    bridge.destroy()
                except Exception as cleanup_error:
                    logger.warning(
                        f"Zero-copy probe: bridge cleanup failed: {cleanup_error}"
                    )
            if ctx is not None:
                try:
                    ctx.release()
                except Exception as cleanup_error:
                    logger.warning(
                        f"Zero-copy probe: GL context cleanup failed: {cleanup_error}"
                    )
            return result

        decode_times: list[float] = []
        bind_times: list[float] = []
        render_times: list[float] = []
        total_times: list[float] = []
        decoded_count = 0
        iosurface_count = 0
        planes_bound_count = 0
        frames_rendered_count = 0
        total_frames = warmup + frames

        for pkt in demuxer.packets(max_packets=total_frames):
            if decoded_count >= total_frames:
                break

            frame_start = time.perf_counter()

            decode_start = time.perf_counter()
            pb = session.decode_packet(
                packet_data=pkt.data,
                pts=pkt.pts,
                is_keyframe=pkt.is_keyframe,
            )
            decode_ms = (time.perf_counter() - decode_start) * 1000.0

            if pb is None:
                if not pkt.is_keyframe and decoded_count == 0:
                    continue
                decoded_count += 1
                continue

            decode_times.append(decode_ms)

            # Check IOSurface backing
            # pb.pixel_buffer may be a raw int pointer (from ctypes callback)
            # or a PyObjC object; the helper handles both import styles
            # (AUD-35315) and never swallows import errors silently.
            if pb.pixel_buffer is not None and _pixel_buffer_has_iosurface(pb.pixel_buffer):
                iosurface_count += 1

            expected_center_rgb = None
            # AUD-35319：CPU 端 reference 读取（Lock/Unlock + 裸指针读）成本高，
            # 而结果只在首帧渲染后（frames_rendered_count == 1）消费一次。
            # 仅在首帧渲染前（frames_rendered_count == 0）计算，避免后续每帧
            # 的多余读取污染 probe 计时口径。
            if verify_color and frames_rendered_count == 0 and pb.pixel_buffer is not None:
                expected_center_rgb = _pixel_buffer_center_rgb_reference(
                    pb.pixel_buffer,
                    width=info.width,
                    height=info.height,
                    pixel_format=pixel_format,
                )

            # Bridge stage: bind IOSurface to GL textures
            if stage in ("bridge-only", "full") and bridge is not None and pb.pixel_buffer is not None:
                bind_start = time.perf_counter()
                
                bind_result = bridge.bind_pixel_buffer(
                    pixel_buffer=pb.pixel_buffer,
                    frame_index=decoded_count,
                    pixel_format=pixel_format,
                    bit_depth=info.bit_depth,
                    decode_path='macos_vt_iosurface_zero_copy',
                )
                
                bind_ms = (time.perf_counter() - bind_start) * 1000.0
                bind_times.append(bind_ms)

                if bind_result is not None:
                    gpu_planes, gpu_release = bind_result
                    planes_bound_count += 1
                    
                    if len(gpu_planes) > 0:
                        result.gl_texture_created = True
                        result.texture_target = gpu_planes[0].target
                        plane_info = [
                            {
                                'component': p.component,
                                'texture_id': p.texture_id,
                                'width': p.width,
                                'height': p.height,
                                'target': p.target,
                            }
                            for p in gpu_planes
                        ]
                        if len(result.planes) < 5:
                            result.planes.append({
                                'frame_index': decoded_count,
                                'planes': plane_info,
                            })

                    # Full stage: render YUV to RGB
                    if stage == "full" and yuv_prog is not None and quad_vao is not None:
                        render_start = time.perf_counter()
                        import moderngl
                        
                        # Create/reuse output FBO
                        if output_tex is None or output_fbo is None:
                            output_size = (
                                (info.width, info.height)
                                if verify_color
                                else (max(1, info.width // 4), max(1, info.height // 4))
                            )
                            output_tex = ctx.texture(output_size, 4, dtype='f1')
                            output_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
                            output_fbo = ctx.framebuffer(color_attachments=[output_tex])
                            result.render_fbo_created = True
                        
                        output_fbo.use()
                        ctx.viewport = (0, 0, output_tex.size[0], output_tex.size[1])
                        ctx.clear(0.0, 0.0, 0.0, 1.0)

                        # Bind external textures via PyOpenGL
                        from OpenGL.GL import GL_TEXTURE_2D, GL_TEXTURE_RECTANGLE
                        for i, plane in enumerate(gpu_planes):
                            target = GL_TEXTURE_RECTANGLE if plane.target == 'rectangle' else GL_TEXTURE_2D
                            _bind_external_texture(plane.texture_id, target, i)

                        use_rectangle = any(p.target == 'rectangle' for p in gpu_planes)
                        active_prog = yuv_rect_prog if use_rectangle else yuv_prog
                        active_vao = quad_rect_vao if use_rectangle else quad_vao
                        if active_prog is None or active_vao is None:
                            result.fallback_reason = "rectangle_yuv_shader_unavailable"
                            break

                        # Set shader uniforms
                        # NOTE: moderngl 5.12 never writes sampler2DRect uniform
                        # values to GL (verified via glGetUniformiv; plain
                        # sampler2D works). All samplers stay at default 0 and
                        # every sampler samples texture unit 0. Write sampler
                        # uniforms with raw GL for both rect and 2D programs.
                        from OpenGL.GL import glUseProgram, glUniform1i, glGetUniformLocation
                        glUseProgram(active_prog.glo)
                        glUniform1i(glGetUniformLocation(active_prog.glo, 'y_plane'), 0)
                        glUniform1i(glGetUniformLocation(active_prog.glo, 'uv_plane'), 1)
                        active_prog['yuv_matrix'].value = 1  # BT.709
                        active_prog['color_range'].value = 0  # limited
                        uv_mode = 0
                        if len(gpu_planes) > 1 and gpu_planes[1].internal_format == 'GL_LUMINANCE_ALPHA':
                            uv_mode = 1
                        active_prog['uv_component_mode'].value = uv_mode
                        if use_rectangle:
                            active_prog['y_size'].value = (
                                float(gpu_planes[0].width),
                                float(gpu_planes[0].height),
                            )
                            active_prog['uv_size'].value = (
                                float(gpu_planes[1].width),
                                float(gpu_planes[1].height),
                            )

                        # Render fullscreen quad
                        active_vao.render(moderngl.TRIANGLES)

                        render_ms = (time.perf_counter() - render_start) * 1000.0
                        render_times.append(render_ms)
                        frames_rendered_count += 1

                        # Color verification (optional)
                        if verify_color and frames_rendered_count == 1:
                            try:
                                raw_data = output_fbo.read(
                                    components=4,
                                    alignment=1,
                                    dtype='f1'
                                )
                                import numpy as np
                                img = np.frombuffer(raw_data, dtype=np.uint8).reshape(
                                    output_tex.size[1], output_tex.size[0], 4
                                )
                                # Sample center region
                                h, w = img.shape[:2]
                                center = img[h//2, w//2]
                                r, g, b, _ = center[0], center[1], center[2], center[3]
                                actual_rgb = [int(r), int(g), int(b)]
                                max_error = (
                                    max(abs(actual - expected) for actual, expected in zip(actual_rgb, expected_center_rgb))
                                    if expected_center_rgb is not None
                                    else None
                                )
                                result.color_verification = {
                                    'passed': max_error is not None and max_error <= 12,
                                    'center_rgb': [int(r), int(g), int(b)],
                                    'expected_center_rgb': expected_center_rgb,
                                    'max_channel_error': max_error,
                                    'sample_size': f"{w}x{h}",
                                    'note': 'Compared against the decoded CVPixelBuffer center sample',
                                }
                                logger.info(
                                    "Color verification sample: center RGB=%s expected=%s error=%s",
                                    actual_rgb,
                                    expected_center_rgb,
                                    max_error,
                                )
                            except Exception as e:
                                result.color_verification = {
                                    'passed': False,
                                    'error': str(e),
                                }
                                logger.warning(f"Color verification readback failed: {e}")

                    # The probe has no Viewer context. Finish this isolated
                    # render before returning the source slot to the bridge.
                    if ctx is not None:
                        ctx.finish()
                    gpu_release.release()
                    session.release_decoded_frame(pb)
                else:
                    session.release_decoded_frame(pb)
            else:
                # AUD-35312：decode-only 阶段（或无 bridge/GL 阶段）此前
                # 从不调用 release_decoded_frame，每个解码帧泄漏一个被
                # retain 的 CVPixelBuffer/IOSurface。
                session.release_decoded_frame(pb)

            decoded_count += 1
            if decoded_count > warmup:
                total_ms = (time.perf_counter() - frame_start) * 1000.0
                total_times.append(total_ms)

        result.decoded_pixel_buffers = decoded_count
        result.frames_tested = max(0, decoded_count - warmup)
        result.cvpixelbuffer_iosurface_backed = iosurface_count > 0
        result.pixel_format = info.pixel_format
        result.cpu_download_frames = 0
        result.texture_write_frames = 0
        result.iosurface_present = iosurface_count > 0
        result.planes_bound = planes_bound_count
        result.frames_rendered = frames_rendered_count
        
        # zero_copy 判定：IOSurface 存在 + 无 CPU download + 无 texture.write
        result.zero_copy = (
            iosurface_count > 0 
            and result.cpu_download_frames == 0
            and result.texture_write_frames == 0
            and (stage == "decode-only" or planes_bound_count > 0)
            and (not verify_color or bool((result.color_verification or {}).get('passed')))
        )

        if verify_color and not bool((result.color_verification or {}).get('passed')):
            result.fallback_reason = 'color_verification_failed'

        if decode_times:
            result.decode_ms = _stats_dict(decode_times)
        if bind_times:
            result.external_bind_ms = _stats_dict(bind_times)
        if render_times:
            result.yuv_render_ms = _stats_dict(render_times)
        if total_times:
            result.total_ms = _stats_dict(total_times)

        result.probe_success = True

        # Cleanup
        if bridge is not None:
            bridge.destroy()
        if output_fbo is not None:
            output_fbo.release()
        if output_tex is not None:
            output_tex.release()
        if ctx is not None:
            ctx.release()
        session.destroy()

    except Exception as e:
        result.fallback_reason = f"probe_error: {e}"
        logger.error(f"Zero-copy probe error: {e}", exc_info=True)
        # Cleanup on error
        if bridge is not None:
            try:
                bridge.destroy()
            except Exception as cleanup_error:
                logger.warning(f"Zero-copy probe: bridge cleanup failed: {cleanup_error}")
        if ctx is not None:
            try:
                ctx.release()
            except Exception as cleanup_error:
                logger.warning(f"Zero-copy probe: GL context cleanup failed: {cleanup_error}")
    finally:
        demuxer.close()

    return result


def main() -> None:
    """命令行入口"""
    parser = argparse.ArgumentParser(description="macOS VT zero-copy probe")
    parser.add_argument("--file", required=True, help="Video file path")
    parser.add_argument("--frames", type=int, default=120, help="Number of frames to test")
    parser.add_argument("--warmup", type=int, default=10, help="Number of warmup frames")
    parser.add_argument("--stage", default="full", choices=["decode-only", "bridge-only", "full"],
                        help="Probe stage")
    parser.add_argument("--verify-color", action="store_true", help="Enable color verification")
    parser.add_argument("--output-json", default=None, help="Output JSON file path")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    result = run_probe(
        file_path=args.file,
        frames=args.frames,
        warmup=args.warmup,
        stage=args.stage,
        verify_color=args.verify_color,
    )

    result_dict = asdict(result)

    if args.output_json:
        import os
        # AUD-35314：纯文件名（无目录部分）时 os.path.dirname 返回 ''，
        # os.makedirs('') 抛 FileNotFoundError；目录为空跳过创建。
        output_dir = os.path.dirname(args.output_json)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        with open(args.output_json, 'w') as f:
            json.dump(result_dict, f, indent=2)
        logger.info(f"Probe result saved to {args.output_json}")
    else:
        print(json.dumps(result_dict, indent=2))

    if not result.probe_success or not result.zero_copy:
        sys.exit(1)


if __name__ == "__main__":
    main()
