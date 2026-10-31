#!/usr/bin/env python3
"""
时间线视口预览帧速基准测试。

模拟完整的时间线播放管线：
  Decode → GPU Upload → YUV→RGB Shader → Composite → Viewport

测试场景：
  1. 单轨道播放 (single-track)
  2. 双轨道同时播放 (dual-track composite) — 同编码 + 跨编码

覆盖编码/分辨率：
  - 2K H.264 / H.265 / ProRes 422 HQ / DNxHR HQX 12-bit
  - 4K H.264 / H.265 / ProRes 422 HQ / DNxHR HQX 12-bit

用法:
    .venv/bin/python tests/benchmark_viewport_playback.py
"""

from __future__ import annotations

import os
import sys
import time
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple, Any

# 抑制 playback_trace 日志输出
os.environ["TOPOS_PLAYBACK_TRACE"] = "0"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

TEST_VIDEOS_DIR = PROJECT_ROOT / "tests" / "test_videos"

# ── 测试参数 ──────────────────────────────────────────────────────────
WARMUP_FRAMES = 5
BENCH_FRAMES = 60
VIEWPORT_W, VIEWPORT_H = 1920, 1080

logging.getLogger().setLevel(logging.WARNING)

# ── 视频优先级顺序 ───────────────────────────────────────────────────
VIDEO_PRIORITY = [
    "2K_h264.mp4",
    "2K_H265.mov",
    "2K_prores422HQ.mov",
    "2K_DNxHR HQX 12-bit.mov",
    "4K_H264.mov",
    "4K_H265.mov",
    "4K_prores422HQ.mov",
    "4K_DNxHR HQX 12-bit.mov",
]


# ── 数据结构 ──────────────────────────────────────────────────────────
@dataclass
class BenchResult:
    """一项测试的结果"""
    label: str = ""
    fps: float = 0.0
    avg_ms: float = 0.0
    min_ms: float = float('inf')
    max_ms: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    avg_decode_ms: float = 0.0
    avg_upload_ms: float = 0.0
    avg_composite_ms: float = 0.0
    resolution: str = ""
    codec: str = ""
    frame_type: str = ""
    tracks: int = 1
    errors: int = 0
    video_name: str = ""


# ── 视频发现 ──────────────────────────────────────────────────────────
def get_test_videos() -> dict[str, Path]:
    videos = {}
    if TEST_VIDEOS_DIR.exists():
        for f in TEST_VIDEOS_DIR.iterdir():
            if f.suffix.lower() in ('.mp4', '.mov') and not f.name.startswith('.'):
                videos[f.name] = f
    return videos


# ── 视口渲染器 (简化版, 不需要 Qt) ────────────────────────────────────
class ViewportRenderer:
    """
    简化版 GPU 视口渲染器，使用 moderngl standalone context。
    完整模拟 EditGpuPreviewRenderer 的关键路径：
      1. YUV plane upload → YUV→RGB shader (planar 路径)
      2. RGB texture upload (packed CPU 路径，用于 hwaccel 回退)
      3. Multi-track alpha composite
    """

    def __init__(self, viewport_size: Tuple[int, int] = (VIEWPORT_W, VIEWPORT_H)):
        import moderngl
        import numpy as np

        self.ctx = moderngl.create_standalone_context()
        self.vp_w, self.vp_h = viewport_size

        # ── YUV GPU Uploader ──
        from src.shared.video.yuv_gpu_upload import YUVGPUUploader
        self.uploader = YUVGPUUploader(self.ctx, pool_size=8)

        # ── Shaders ──
        vert = """
        #version 330
        in vec2 in_vert;
        in vec2 in_texcoord;
        out vec2 v_texcoord;
        void main() {
            gl_Position = vec4(in_vert, 0.0, 1.0);
            v_texcoord = in_texcoord;
        }
        """

        yuv_to_rgb_body = """
            float range_mul = u_color_range == 1 ? 1.0 : (219.0 / 255.0);
            float range_off = u_color_range == 1 ? 0.0 : (16.0 / 255.0);
            y = (y - range_off) / range_mul;
            y = max(y, 0.0);
            // Limited range 色度：中性点 128/255，跨度 224/255，归一化乘 255/224
            // （与 edit_gpu_preview_renderer / YUVGPUUploader 一致）
            float c_off = u_color_range == 1 ? 0.5 : (128.0 / 255.0);
            float c_scale = u_color_range == 1 ? 1.0 : (255.0 / 224.0);
            cb = (cb - c_off) * c_scale;
            cr = (cr - c_off) * c_scale;
            float r, g, b;
            if (u_color_matrix == 0) {
                r = y + 1.402 * cr;
                g = y - 0.344136 * cb - 0.714136 * cr;
                b = y + 1.772 * cb;
            } else if (u_color_matrix == 1) {
                r = y + 1.5748 * cr;
                g = y - 0.1873 * cb - 0.4681 * cr;
                b = y + 1.8556 * cb;
            } else {
                r = y + 1.4746 * cr;
                g = y - 0.1646 * cb - 0.5714 * cr;
                b = y + 1.8814 * cb;
            }
            f_color = vec4(clamp(r, 0.0, 1.0), clamp(g, 0.0, 1.0), clamp(b, 0.0, 1.0), 1.0);
        """

        # 2-plane YUV→RGB
        self._yuv2_prog = self.ctx.program(
            vertex_shader=vert,
            fragment_shader=f"""
            #version 330
            uniform sampler2D u_y_plane;
            uniform sampler2D u_uv_plane;
            uniform int u_color_range;
            uniform int u_color_matrix;
            in vec2 v_texcoord;
            out vec4 f_color;
            void main() {{
                float y = texture(u_y_plane, v_texcoord).r;
                vec2 uv = texture(u_uv_plane, v_texcoord).rg;
                float cb = uv.r;
                float cr = uv.g;
                {yuv_to_rgb_body}
            }}
            """,
        )

        # 3-plane YUV→RGB
        self._yuv3_prog = self.ctx.program(
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

        # RGB passthrough (for packed CPU frames)
        self._rgb_prog = self.ctx.program(
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

        # Alpha blend shader
        self._blend_prog = self.ctx.program(
            vertex_shader=vert,
            fragment_shader="""
            #version 330
            uniform sampler2D u_base;
            uniform sampler2D u_overlay;
            uniform float u_opacity;
            in vec2 v_texcoord;
            out vec4 f_color;
            void main() {
                vec4 base = texture(u_base, v_texcoord);
                vec4 overlay = texture(u_overlay, v_texcoord);
                float a = u_opacity * overlay.a;
                f_color = vec4(mix(base.rgb, overlay.rgb, a), 1.0);
            }
            """,
        )

        # Fullscreen quad
        quad = np.array([
            -1.0, -1.0, 0.0, 1.0,
             1.0, -1.0, 1.0, 1.0,
            -1.0,  1.0, 0.0, 0.0,
             1.0,  1.0, 1.0, 0.0,
        ], dtype=np.float32)
        self._quad_buf = self.ctx.buffer(quad)
        self._yuv2_vao = self.ctx.vertex_array(
            self._yuv2_prog, [(self._quad_buf, "2f 2f", "in_vert", "in_texcoord")])
        self._yuv3_vao = self.ctx.vertex_array(
            self._yuv3_prog, [(self._quad_buf, "2f 2f", "in_vert", "in_texcoord")])
        self._rgb_vao = self.ctx.vertex_array(
            self._rgb_prog, [(self._quad_buf, "2f 2f", "in_vert", "in_texcoord")])
        self._blend_vao = self.ctx.vertex_array(
            self._blend_prog, [(self._quad_buf, "2f 2f", "in_vert", "in_texcoord")])

        # 2-plane YUV→RGB for IOSurface rectangle textures (VT zero-copy)
        self._yuv_rect_prog = self.ctx.program(
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
            in vec2 v_texcoord;
            out vec4 f_color;
            void main() {{
                float y = texture(u_y_plane, v_texcoord * u_y_size).r;
                vec2 uv = texture(u_uv_plane, v_texcoord * u_uv_size).rg;
                float cb = uv.r;
                float cr = uv.g;
                {yuv_to_rgb_body}
            }}
            """,
        )
        self._yuv_rect_vao = self.ctx.vertex_array(
            self._yuv_rect_prog, [(self._quad_buf, "2f 2f", "in_vert", "in_texcoord")])

        # VT zero-copy bridge (lazy) + deferred-release queue
        self._vt_bridge = None
        self._zc_pending: list = []

        # Viewport FBO
        self._vp_tex = self.ctx.texture((self.vp_w, self.vp_h), components=4, dtype='f1')
        self._vp_fbo = self.ctx.framebuffer(color_attachments=[self._vp_tex])

        # Track FBOs
        self._track_texes: list = []
        self._track_fbos: list = []

        # Ping-pong FBOs
        self._pp_tex_a = self.ctx.texture((self.vp_w, self.vp_h), components=4, dtype='f1')
        self._pp_fbo_a = self.ctx.framebuffer(color_attachments=[self._pp_tex_a])
        self._pp_tex_b = self.ctx.texture((self.vp_w, self.vp_h), components=4, dtype='f1')
        self._pp_fbo_b = self.ctx.framebuffer(color_attachments=[self._pp_tex_b])

        # Black base texture
        black = np.zeros((self.vp_h, self.vp_w, 4), dtype=np.uint8)
        self._black_tex = self.ctx.texture((self.vp_w, self.vp_h), components=4, dtype='f1')
        self._black_tex.write(black)

        # Format classifier
        from src.features.edit.core.edit_gpu_preview_renderer import _classify_planar_format
        self._classify_format = _classify_planar_format

    def _get_track_fbo(self, index: int, w: int, h: int):
        import moderngl
        # Cap track FBO at viewport size — no point rendering 4K intermediate
        # when viewport is only 1080p (saves ~4x shader work for 4K content)
        w = min(w, self.vp_w)
        h = min(h, self.vp_h)
        while len(self._track_texes) <= index:
            tex = self.ctx.texture((w, h), components=4, dtype='f1')
            fbo = self.ctx.framebuffer(color_attachments=[tex])
            self._track_texes.append(tex)
            self._track_fbos.append(fbo)
        tex = self._track_texes[index]
        fbo = self._track_fbos[index]
        if tex.size != (w, h):
            tex.release()
            fbo.release()
            tex = self.ctx.texture((w, h), components=4, dtype='f1')
            fbo = self.ctx.framebuffer(color_attachments=[tex])
            self._track_texes[index] = tex
            self._track_fbos[index] = fbo
        return fbo, tex

    def upload_and_render(self, decoded_frame, frame_type: str, track_index: int) -> Optional[Any]:
        """
        上传解码帧到 GPU 并渲染到 track FBO。
        支持三种路径：
          1. planar YUV → YUVGPUUploader + YUV→RGB shader
          2. packed NV12 (hwaccel 2D array) → 拆分 Y/UV → NV12 上传
          3. packed RGB (hwaccel 回退) → RGBA texture 直传
        """
        if frame_type == 'vt_pixel_buffer':
            return self._upload_vt_zero_copy(decoded_frame, track_index)
        if frame_type in ('planar', 'vt_planar') and getattr(decoded_frame, 'is_planar', False):
            return self._upload_planar(decoded_frame, track_index)
        elif frame_type in ('cpu', 'gpu') or not getattr(decoded_frame, 'is_planar', False):
            # Try NV12 split first (hwaccel returns 2D packed NV12)
            result = self._try_upload_nv12_packed(decoded_frame, track_index)
            if result is not None:
                return result
            # Fallback to packed RGB upload
            return self._upload_packed(decoded_frame, track_index)
        return None

    def _ensure_vt_bridge(self):
        if self._vt_bridge is None:
            from src.shared.media.macos_vt.iosurface_gl_bridge import IOSurfaceGLBridge
            bridge = IOSurfaceGLBridge(ring_size=8, max_ring_size=32)
            if not bridge.initialize(planes_per_frame=2):
                return None
            self._vt_bridge = bridge
        return self._vt_bridge

    def _upload_vt_zero_copy(self, vt_frame, track_index: int) -> Optional[Any]:
        """IOSurface 零拷贝路径：CVPixelBuffer → GL rect 纹理 → YUV→RGB。"""
        import moderngl
        from OpenGL.GL import (glActiveTexture, glBindTexture, glUseProgram,
                               glUniform1i, glGetUniformLocation,
                               GL_TEXTURE0, GL_TEXTURE_2D, GL_TEXTURE_RECTANGLE)

        bridge = self._ensure_vt_bridge()
        pixel_buffer = getattr(vt_frame, 'pixel_buffer', None)
        if bridge is None or pixel_buffer is None:
            return None

        result = bridge.bind_pixel_buffer(
            pixel_buffer=pixel_buffer,
            frame_index=int(getattr(vt_frame, 'frame_index', 0) or 0),
            pixel_format=str(getattr(vt_frame, 'pixel_format', 'nv12') or 'nv12'),
            bit_depth=int(getattr(vt_frame, 'bit_depth', 8) or 8),
            decode_path='macos_vt_iosurface_zero_copy',
        )
        if result is None:
            return None
        planes, bridge_release = result
        if len(planes) < 2:
            bridge_release.release()
            return None
        y_plane, uv_plane = planes[0], planes[1]

        width = int(getattr(vt_frame, 'width', 0) or y_plane.width)
        height = int(getattr(vt_frame, 'height', 0) or y_plane.height)
        track_fbo, track_tex = self._get_track_fbo(track_index, width, height)

        track_fbo.use()
        self.ctx.viewport = (0, 0, track_tex.size[0], track_tex.size[1])
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        for unit, plane in ((0, y_plane), (1, uv_plane)):
            target = GL_TEXTURE_RECTANGLE if plane.target == 'rectangle' else GL_TEXTURE_2D
            glActiveTexture(GL_TEXTURE0 + unit)
            glBindTexture(target, plane.texture_id)

        use_rectangle = any(p.target == 'rectangle' for p in (y_plane, uv_plane))
        prog = self._yuv_rect_prog if use_rectangle else self._yuv2_prog
        vao = self._yuv_rect_vao if use_rectangle else self._yuv2_vao

        cm_map = {'bt601': 0, 'bt709': 1, 'bt2020': 2}
        color_matrix = cm_map.get(getattr(vt_frame, 'color_matrix', 'bt709'), 1)
        color_range = 0 if getattr(vt_frame, 'color_range', 'limited') == 'limited' else 1

        # NOTE: moderngl 5.12 never writes sampler2DRect uniform values to GL
        # (verified via glGetUniformiv). Write sampler uniforms with raw GL.
        glUseProgram(prog.glo)
        glUniform1i(glGetUniformLocation(prog.glo, 'u_y_plane'), 0)
        glUniform1i(glGetUniformLocation(prog.glo, 'u_uv_plane'), 1)
        prog['u_color_range'] = color_range
        prog['u_color_matrix'] = color_matrix
        if use_rectangle:
            prog['u_y_size'].value = (float(y_plane.width), float(y_plane.height))
            prog['u_uv_size'].value = (float(uv_plane.width), float(uv_plane.height))

        vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)

        # Defer release of ring lease + CVPixelBuffer until GPU is done with
        # them (released after a few frames of lag).
        self._zc_pending.append((bridge_release, vt_frame))
        while len(self._zc_pending) > 4:
            old_release, old_frame = self._zc_pending.pop(0)
            try:
                old_release.release()
            except Exception:
                pass
            release_fn = getattr(old_frame, 'release', None)
            if callable(release_fn):
                try:
                    release_fn()
                except Exception:
                    pass
        return track_tex

    def _upload_planar(self, decoded_frame, track_index: int) -> Optional[Any]:
        """Planar YUV 上传 + YUV→RGB shader render"""
        planes = decoded_frame.planes
        if not planes or len(planes) < 2:
            return None

        width = decoded_frame.width
        height = decoded_frame.height
        pixel_format = getattr(decoded_frame, 'pixel_format', '')
        frame_idx = getattr(decoded_frame, 'frame_index', 0)

        bit_depth = 8
        if hasattr(decoded_frame, 'plane_infos') and decoded_frame.plane_infos:
            bit_depth = decoded_frame.plane_infos[0].bit_depth

        cm_map = {'bt601': 0, 'bt709': 1, 'bt2020': 2}
        color_matrix = cm_map.get(getattr(decoded_frame, 'color_matrix', 'bt709'), 1)
        color_range = 0 if getattr(decoded_frame, 'color_range', 'limited') == 'limited' else 1

        track_fbo, track_tex = self._get_track_fbo(track_index, width, height)
        upload_mode = self._classify_format(pixel_format, len(planes), bit_depth)

        try:
            if upload_mode == 'nv12':
                y_tex, uv_tex = self.uploader.upload_nv12(
                    planes[0], planes[1], frame_idx, bit_depth)
                self._render_2plane(y_tex, uv_tex, track_fbo, track_tex, color_matrix, color_range)
                return track_tex

            elif upload_mode == 'p010':
                y_tex, uv_tex = self.uploader.upload_p010(
                    planes[0], planes[1], frame_idx)
                self._render_2plane(y_tex, uv_tex, track_fbo, track_tex, color_matrix, color_range)
                return track_tex

            elif upload_mode in ('yuv422', 'yuv444', 'yuv420', 'yuv440', 'yuv411', 'rgb_planar'):
                if upload_mode == 'yuv422':
                    y_tex, u_tex, v_tex = self.uploader.upload_yuv422(
                        planes[0], planes[1], planes[2], frame_idx, bit_depth)
                elif upload_mode == 'yuv420':
                    y_tex, u_tex, v_tex = self.uploader.upload_yuv420(
                        planes[0], planes[1], planes[2], frame_idx, bit_depth)
                else:
                    y_tex, u_tex, v_tex = self.uploader.upload_yuv444(
                        planes[0], planes[1], planes[2], frame_idx, bit_depth)
                self._render_3plane(y_tex, u_tex, v_tex, track_fbo, track_tex, color_matrix, color_range, bit_depth)
                return track_tex

            else:
                if len(planes) >= 3:
                    y_tex, u_tex, v_tex = self.uploader.upload_yuv420(
                        planes[0], planes[1], planes[2], frame_idx, bit_depth)
                    self._render_3plane(y_tex, u_tex, v_tex, track_fbo, track_tex, color_matrix, color_range, bit_depth)
                    return track_tex
                elif len(planes) == 2:
                    y_tex, uv_tex = self.uploader.upload_nv12(
                        planes[0], planes[1], frame_idx, bit_depth)
                    self._render_2plane(y_tex, uv_tex, track_fbo, track_tex, color_matrix, color_range)
                    return track_tex
        except Exception:
            return None
        return None

    def _try_upload_nv12_packed(self, decoded_frame, track_index: int) -> Optional[Any]:
        """
        尝试将 hwaccel 返回的 2D packed NV12 数据拆分为 Y + UV 并上传。
        hwaccel 回退路径返回 data shape=(h*1.5, w) 的 uint8 数组（Y + UV 连续存储）。
        """
        import numpy as np
        data = getattr(decoded_frame, 'data', None)
        if data is None or not isinstance(data, np.ndarray):
            return None
        if data.ndim != 2 or data.dtype != np.uint8:
            return None

        width = decoded_frame.width
        height = decoded_frame.height
        # NV12: data shape should be (height * 3/2, width) = (h + h/2, w)
        expected_rows = height + height // 2
        if data.shape[0] != expected_rows or data.shape[1] != width:
            return None

        # Split Y and UV planes
        y_plane = data[:height, :]                              # (h, w)
        uv_raw = data[height:, :]                               # (h/2, w) — interleaved bytes
        uv_plane = uv_raw.reshape(height // 2, width // 2, 2)  # (h/2, w/2, 2)

        frame_idx = getattr(decoded_frame, 'frame_index', 0)

        cm_map = {'bt601': 0, 'bt709': 1, 'bt2020': 2}
        color_matrix = cm_map.get(getattr(decoded_frame, 'color_matrix', 'bt709'), 1)
        color_range = 0 if getattr(decoded_frame, 'color_range', 'limited') == 'limited' else 1

        track_fbo, track_tex = self._get_track_fbo(track_index, width, height)

        try:
            y_tex, uv_tex = self.uploader.upload_nv12(y_plane, uv_plane, frame_idx, bit_depth=8)
            self._render_2plane(y_tex, uv_tex, track_fbo, track_tex, color_matrix, color_range)
            return track_tex
        except Exception:
            return None

    def _upload_packed(self, decoded_frame, track_index: int) -> Optional[Any]:
        """
        Packed RGB 上传：将 CPU 解码的 packed RGB 数据上传到 GPU 纹理。
        支持 8-bit RGB/BGR 和 16-bit RGB (rgb48le 等 hwaccel 回退格式)。
        """
        import numpy as np

        width = decoded_frame.width
        height = decoded_frame.height

        # 尝试获取 pixel 数据
        pixels = None
        if hasattr(decoded_frame, 'pixels') and decoded_frame.pixels is not None:
            pixels = decoded_frame.pixels
        elif hasattr(decoded_frame, 'image') and decoded_frame.image is not None:
            img = decoded_frame.image
            if hasattr(img, 'bits'):
                buf = img.bits()
                buf.setsize(height * width * 4)
                pixels = np.frombuffer(buf, dtype=np.uint8).reshape((height, width, 4))
            else:
                pixels = np.array(img)
        elif hasattr(decoded_frame, 'data') and decoded_frame.data is not None:
            pixels = decoded_frame.data

        if pixels is None:
            return None

        # Ensure numpy array
        if not isinstance(pixels, np.ndarray):
            try:
                pixels = np.array(pixels)
            except Exception:
                return None

        if pixels.ndim != 3 or pixels.shape[2] < 3:
            return None

        # 转为 uint8 RGBA — 处理 uint16 (rgb48le) 和 uint8 (rgb24/bgr24) 两种格式
        try:
            if pixels.dtype == np.uint16:
                # 16-bit RGB → 8-bit (右移 8 位)
                pixels = (pixels >> 8).astype(np.uint8)
            elif pixels.dtype != np.uint8:
                pixels = np.clip(pixels).astype(np.uint8)

            channels = pixels.shape[2]
            if channels == 3:
                pixels = np.ascontiguousarray(
                    np.dstack([pixels, np.full((height, width, 1), 255, dtype=np.uint8)]))
            elif channels == 4:
                pixels = np.ascontiguousarray(pixels)
            else:
                return None
        except Exception:
            return None

        # 验证数据大小匹配 RGBA f1 纹理
        expected_bytes = width * height * 4
        if pixels.nbytes != expected_bytes:
            return None

        import moderngl  # ensure available in scope

        track_fbo, track_tex = self._get_track_fbo(track_index, width, height)

        try:
            track_tex.write(pixels.tobytes())
        except Exception:
            return None

        # Render passthrough (already RGB, just copy to track FBO)
        track_fbo.use()
        self.ctx.viewport = (0, 0, width, height)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)
        self._rgb_prog['u_tex'] = 0
        track_tex.use(0)
        self._rgb_vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)
        return track_tex

    def _render_2plane(self, y_tex, uv_tex, fbo, tex, color_matrix, color_range):
        import moderngl
        fbo.use()
        self.ctx.viewport = (0, 0, tex.size[0], tex.size[1])
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)
        self._yuv2_prog['u_y_plane'] = 0
        self._yuv2_prog['u_uv_plane'] = 1
        self._yuv2_prog['u_color_range'] = color_range
        self._yuv2_prog['u_color_matrix'] = color_matrix
        y_tex.use(0)
        uv_tex.use(1)
        self._yuv2_vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)

    def _render_3plane(self, y_tex, u_tex, v_tex, fbo, tex, color_matrix, color_range,
                        bit_depth=8):
        import moderngl
        fbo.use()
        self.ctx.viewport = (0, 0, tex.size[0], tex.size[1])
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)
        # Use uploader's render which auto-detects R16UI vs R8 textures
        self.uploader.render_yuv_to_rgb(y_tex, u_tex, v_tex, fbo, bit_depth=bit_depth)

    def composite(self, track_textures: list) -> None:
        """合成多轨道到 viewport FBO。track_textures: [(tex, opacity), ...]"""
        import moderngl
        if not track_textures:
            return

        self._vp_fbo.use()
        self.ctx.viewport = (0, 0, self.vp_w, self.vp_h)

        if len(track_textures) == 1:
            self.ctx.clear(0.0, 0.0, 0.0, 1.0)
            tex, opacity = track_textures[0]
            self._blend_prog['u_base'] = 0
            self._blend_prog['u_overlay'] = 1
            self._blend_prog['u_opacity'] = opacity
            self._black_tex.use(0)
            tex.use(1)
            self._blend_vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)
            return

        # Multi-track: ping-pong compositing
        first_tex, _ = track_textures[0]
        self._pp_fbo_a.use()
        self.ctx.viewport = (0, 0, self.vp_w, self.vp_h)
        self.ctx.clear(0.0, 0.0, 0.0, 1.0)
        self._blend_prog['u_base'] = 0
        self._blend_prog['u_overlay'] = 1
        self._blend_prog['u_opacity'] = 1.0
        self._black_tex.use(0)
        first_tex.use(1)
        self._blend_vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)

        read_tex, read_fbo = self._pp_tex_a, self._pp_fbo_a
        write_tex, write_fbo = self._pp_tex_b, self._pp_fbo_b

        for tex, opacity in track_textures[1:]:
            write_fbo.use()
            self.ctx.viewport = (0, 0, self.vp_w, self.vp_h)
            self._blend_prog['u_base'] = 0
            self._blend_prog['u_overlay'] = 1
            self._blend_prog['u_opacity'] = opacity
            read_tex.use(0)
            tex.use(1)
            self._blend_vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)
            read_tex, write_tex = write_tex, read_tex
            read_fbo, write_fbo = write_fbo, read_fbo

        self._vp_fbo.use()
        self.ctx.viewport = (0, 0, self.vp_w, self.vp_h)
        self._blend_prog['u_base'] = 0
        self._blend_prog['u_overlay'] = 1
        self._blend_prog['u_opacity'] = 1.0
        self._black_tex.use(0)
        read_tex.use(1)
        self._blend_vao.render(mode=moderngl.TRIANGLE_STRIP, vertices=4)

    def cleanup(self):
        for t in self._track_texes:
            try: t.release()
            except: pass
        for f in self._track_fbos:
            try: f.release()
            except: pass
        for r in [self._vp_tex, self._vp_fbo, self._pp_tex_a, self._pp_fbo_a,
                   self._pp_tex_b, self._pp_fbo_b, self._black_tex, self._quad_buf]:
            try: r.release()
            except: pass
        self.ctx.release()


# ── 基准测试函数 ──────────────────────────────────────────────────────
def _get_video_info(video_path: str) -> Tuple[str, str, int]:
    """Probe video resolution, codec, total_frames. Returns (res_str, codec_str, total_frames)."""
    try:
        from src.shared.media.factory import MediaSourceFactory
        meta = MediaSourceFactory.probe_video_metadata(video_path)
        if meta:
            res = f"{meta.width}x{meta.height}"
            codec = meta.codec_name or ""
            total = meta.total_frames or 300
            return res, codec, total
    except Exception:
        pass
    return "", "", 300


def benchmark_single_track(
    video_path: str,
    video_name: str,
    renderer: ViewportRenderer,
    num_frames: int = BENCH_FRAMES,
) -> BenchResult:
    """单轨道播放测试：Decode → Upload → YUV→RGB → Composite → Viewport"""
    from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager

    mgr = EditGpuDecodeManager()
    res_str, codec, total_frames = _get_video_info(video_path)

    # 预热
    for i in range(WARMUP_FRAMES):
        mgr.get_frame(video_path, i)

    # 正式测试
    decode_times = []
    upload_times = []
    composite_times = []
    total_times = []
    errors = 0
    first_frame_type = ""

    for i in range(num_frames):
        frame_idx = i % total_frames
        t0 = time.perf_counter()

        # Step 1: Decode
        td0 = time.perf_counter()
        frame, frame_type = mgr.get_frame(video_path, frame_idx)
        decode_ms = (time.perf_counter() - td0) * 1000.0

        if frame is None:
            errors += 1
            continue
        if not first_frame_type:
            first_frame_type = frame_type

        # Step 2: Upload + Render to track FBO
        tu0 = time.perf_counter()
        track_tex = renderer.upload_and_render(frame, frame_type, track_index=0)
        upload_ms = (time.perf_counter() - tu0) * 1000.0

        if track_tex is None:
            errors += 1
            continue

        # Step 3: Composite to viewport
        tc0 = time.perf_counter()
        renderer.composite([(track_tex, 1.0)])
        composite_ms = (time.perf_counter() - tc0) * 1000.0

        total_ms = (time.perf_counter() - t0) * 1000.0

        decode_times.append(decode_ms)
        upload_times.append(upload_ms)
        composite_times.append(composite_ms)
        total_times.append(total_ms)

    mgr.close_all()

    return _build_result(
        label="1-Track (single)",
        total_times=total_times,
        decode_times=decode_times,
        upload_times=upload_times,
        composite_times=composite_times,
        errors=errors,
        resolution=res_str,
        codec=codec,
        frame_type=first_frame_type,
        tracks=1,
        video_name=video_name,
    )


def benchmark_multi_track(
    video_paths: List[str],
    video_names: List[str],
    renderer: ViewportRenderer,
    num_frames: int = BENCH_FRAMES,
) -> BenchResult:
    """
    多轨道同时播放测试。每帧解码 N 个视频，上传到 N 个 track FBO，alpha blend 合成。
    """
    from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager

    mgr = EditGpuDecodeManager()
    n_tracks = len(video_paths)

    # Probe first video info
    res_str, codec, total_frames = _get_video_info(video_paths[0])

    # 预热（每个 track 用独立 stream_key，模拟真实时间线同素材多 clip）
    for i in range(WARMUP_FRAMES):
        for t_idx, path in enumerate(video_paths):
            mgr.get_frame(path, i, stream_key=f"track-{t_idx}")

    decode_times = []
    upload_times = []
    composite_times = []
    total_times = []
    errors = 0
    first_frame_type = ""

    for i in range(num_frames):
        frame_idx = i % total_frames
        t0 = time.perf_counter()

        # Step 1: Decode all tracks（每个 track 独立 stream_key，避免 cursor 抢占）
        td0 = time.perf_counter()
        decoded = []
        for t_idx, path in enumerate(video_paths):
            frame, ft = mgr.get_frame(path, frame_idx, stream_key=f"track-{t_idx}")
            if frame is None:
                break
            decoded.append((frame, ft))
            if not first_frame_type:
                first_frame_type = ft
        decode_ms = (time.perf_counter() - td0) * 1000.0

        if len(decoded) != n_tracks:
            errors += 1
            continue

        # Step 2: Upload + Render all tracks
        tu0 = time.perf_counter()
        track_textures = []
        ok = True
        for t_idx, (frame, ft) in enumerate(decoded):
            track_tex = renderer.upload_and_render(frame, ft, track_index=t_idx)
            if track_tex is None:
                ok = False
                break
            opacity = 1.0 if t_idx == 0 else 0.8
            track_textures.append((track_tex, opacity))
        upload_ms = (time.perf_counter() - tu0) * 1000.0

        if not ok or len(track_textures) != n_tracks:
            errors += 1
            continue

        # Step 3: Composite
        tc0 = time.perf_counter()
        renderer.composite(track_textures)
        composite_ms = (time.perf_counter() - tc0) * 1000.0

        total_ms = (time.perf_counter() - t0) * 1000.0

        decode_times.append(decode_ms)
        upload_times.append(upload_ms)
        composite_times.append(composite_ms)
        total_times.append(total_ms)

    mgr.close_all()

    # 区分同编码双轨和跨编码双轨：所有 video_names 相同 = same，否则 = cross
    is_same = len(set(video_names)) == 1
    tag = "same" if is_same else "cross"
    label = f"{n_tracks}-Track ({tag})"
    return _build_result(
        label=label,
        total_times=total_times,
        decode_times=decode_times,
        upload_times=upload_times,
        composite_times=composite_times,
        errors=errors,
        resolution=res_str,
        codec=codec,
        frame_type=first_frame_type,
        tracks=n_tracks,
        video_name=video_names[0],
    )


def _build_result(
    label: str,
    total_times: List[float],
    decode_times: List[float],
    upload_times: List[float],
    composite_times: List[float],
    errors: int,
    resolution: str,
    codec: str,
    frame_type: str,
    tracks: int,
    video_name: str,
) -> BenchResult:
    n = len(total_times)
    if n == 0:
        return BenchResult(
            label=label, errors=errors, resolution=resolution,
            codec=codec, frame_type=frame_type, tracks=tracks, video_name=video_name,
        )

    sorted_times = sorted(total_times)
    return BenchResult(
        label=label,
        fps=n / sum(total_times) * 1000,
        avg_ms=sum(total_times) / n,
        min_ms=sorted_times[0],
        max_ms=sorted_times[-1],
        p50_ms=sorted_times[n // 2],
        p95_ms=sorted_times[int(n * 0.95)] if n > 1 else sorted_times[0],
        avg_decode_ms=sum(decode_times) / n,
        avg_upload_ms=sum(upload_times) / n,
        avg_composite_ms=sum(composite_times) / n,
        resolution=resolution,
        codec=codec,
        frame_type=frame_type,
        tracks=tracks,
        errors=errors,
        video_name=video_name,
    )


# ── 报告输出 ──────────────────────────────────────────────────────────
def print_separator(char: str = "─", width: int = 100):
    print(char * width)


def print_result(r: BenchResult):
    if r.fps <= 0:
        print(f"  {r.label:<36s}  ❌ 无有效帧 (errors={r.errors})")
        return

    if r.fps >= 24:
        badge = "✅ ≥24fps"
    elif r.fps >= 12:
        badge = "⚠️  <24fps"
    else:
        badge = "❌ <12fps"

    print(f"  {r.label:<36s}  {r.fps:>7.1f} fps  avg={r.avg_ms:.1f}ms  p95={r.p95_ms:.1f}ms  [{badge}]")
    print(f"  {'':36s}  decode={r.avg_decode_ms:.1f}ms  upload={r.avg_upload_ms:.1f}ms  composite={r.avg_composite_ms:.1f}ms  type={r.frame_type}")
    if r.errors > 0:
        print(f"  {'':36s}  ⚠️  {r.errors} errors")


def print_summary_table(results: List[BenchResult]):
    """打印汇总对比表 — 按视频分组"""
    print()
    print("=" * 110)
    print("  📊 视口预览帧速汇总")
    print("=" * 110)
    print()

    # Group by video_name
    from collections import OrderedDict
    by_video: OrderedDict[str, List[BenchResult]] = OrderedDict()
    for r in results:
        key = r.video_name
        if key not in by_video:
            by_video[key] = []
        by_video[key].append(r)

    # ── Table header ──
    print(f"  {'Video':<32s} {'Res':>10s} {'Type':>6s} {'Tracks':>18s} "
          f"{'FPS':>8s} {'Avg':>6s} {'P95':>6s} {'Dec':>6s} {'Upl':>6s} {'Cmp':>6s}")
    print(f"  {'─'*32} {'─'*10} {'─'*6} {'─'*18} {'─'*8} {'─'*6} {'─'*6} {'─'*6} {'─'*6} {'─'*6}")

    for vname, items in by_video.items():
        for r in sorted(items, key=lambda x: x.tracks):
            if r.fps <= 0:
                print(f"  {vname:<32s} {r.resolution:>10s} {r.frame_type:>6s} {r.label:>18s}  ❌ failed")
                continue
            print(f"  {vname:<32s} {r.resolution:>10s} {r.frame_type:>6s} {r.label:>18s}  "
                  f"{r.fps:>7.1f}f {r.avg_ms:>5.1f}  {r.p95_ms:>5.1f}  "
                  f"{r.avg_decode_ms:>5.1f}  {r.avg_upload_ms:>5.1f}  {r.avg_composite_ms:>5.1f}")

    # ── Playback capability summary ──
    print()
    print("  播放能力评估 (≥24fps = 实时播放):")
    print(f"  {'─'*90}")
    for vname, items in by_video.items():
        parts = []
        for r in sorted(items, key=lambda x: x.tracks):
            if r.fps >= 24:
                b = "✅"
            elif r.fps >= 12:
                b = "⚠️"
            else:
                b = "❌"
            # 用 label 简写区分同编码/跨编码双轨
            tag = r.label.split('(')[-1].rstrip(')') if '(' in r.label else f"{r.tracks}T"
            parts.append(f"{tag}={b}{r.fps:.0f}fps")
        print(f"  {vname:<32s}  {'  '.join(parts)}")


# ── Helper: find cross-codec video ────────────────────────────────────
def _codec_family(name: str) -> str:
    n = name.lower()
    if 'h264' in n: return 'h264'
    if 'h265' in n: return 'h265'
    if 'prores' in n: return 'prores'
    if 'dnxhr' in n: return 'dnxhr'
    return 'other'


def _find_cross_codec_video(current_name: str, videos: dict) -> Optional[Tuple[str, Path]]:
    is_2k = current_name.startswith("2K")
    current_family = _codec_family(current_name)
    for name, path in videos.items():
        if name == current_name:
            continue
        same_res = (is_2k and name.startswith("2K")) or (not is_2k and name.startswith("4K"))
        if not same_res:
            continue
        if _codec_family(name) != current_family:
            return (name, path)
    return None


# ── Main ──────────────────────────────────────────────────────────────
def main():
    videos = get_test_videos()
    if not videos:
        print("❌ 没有找到测试视频！")
        return

    try:
        import moderngl
    except ImportError:
        print("❌ moderngl 未安装。请运行: pip install moderngl")
        return

    print("=" * 110)
    print("  时间线视口预览帧速基准测试")
    print(f"  视口: {VIEWPORT_W}x{VIEWPORT_H}  |  每项测试: {BENCH_FRAMES} 帧  |  预热: {WARMUP_FRAMES} 帧")
    print("=" * 110)
    print()

    print("🔧 初始化 GPU 渲染器...")
    try:
        renderer = ViewportRenderer()
        print("   ✅ moderngl standalone context + YUV uploader 就绪")
    except Exception as e:
        print(f"   ❌ GPU 初始化失败: {e}")
        return
    print()

    all_results: List[BenchResult] = []

    for name in VIDEO_PRIORITY:
        if name not in videos:
            continue
        path = str(videos[name])

        print_separator()
        print(f"📹 {name}")
        print_separator()

        # ── 1. 单轨道 ──
        try:
            r = benchmark_single_track(path, name, renderer)
            print_result(r)
            all_results.append(r)
        except Exception as e:
            print(f"  Single Track                         ❌ {e}")

        # ── 2. 双轨道同编码 ──
        try:
            r = benchmark_multi_track([path, path], [name, name], renderer)
            print_result(r)
            all_results.append(r)
        except Exception as e:
            print(f"  Dual Track (same)                    ❌ {e}")

        # ── 3. 双轨道跨编码 ──
        cross = _find_cross_codec_video(name, videos)
        if cross is not None:
            cross_name, cross_path = cross
            try:
                r = benchmark_multi_track(
                    [path, str(cross_path)],
                    [name, cross_name],
                    renderer)
                print_result(r)
                all_results.append(r)
            except Exception as e:
                print(f"  Dual Track (cross)                   ❌ {e}")

        print()

    print_summary_table(all_results)
    renderer.cleanup()
    print()
    print("🏁 测试完成")


if __name__ == "__main__":
    main()
