"""复现 Color 页播放黑屏：真实渲染线程 + planar YUV 路径 + 降噪开关。

读回前台纹理（渲染线程与主线程共享上下文），统计像素均值判断黑屏。
"""
import os
import sys
import time

import numpy as np

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QOpenGLContext, QOffscreenSurface, QSurfaceFormat

from src.shared.media.frame import DecodedFrame
from src.shared.video.render_commands import RenderCommand
from src.shared.video.denoise.params import VideoDenoiseParams, TemporalDenoiseParams, SpatialDenoiseParams
from src.features.color.core.async_renderer import AsyncRenderer

W, H = 320, 180


def wait_until(cond, timeout_s=15.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def make_planar_frame(idx, noise=0.04):
    """BT.709 limited YUV420P 彩条+噪声帧。"""
    rng = np.random.default_rng(idx)
    yy, xx = np.mgrid[0:H, 0:W]
    r = 0.5 + 0.4 * np.sin(xx / 40.0 + idx)
    g = 0.5 + 0.3 * np.cos(yy / 30.0)
    b = 0.4 + 0.3 * np.sin((xx + yy) / 50.0)
    rgb = np.stack([r, g, b], axis=-1)
    rgb = np.clip(rgb + rng.normal(0, noise, rgb.shape), 0, 1)
    # RGB→YUV BT.709 limited
    y = 16 + 219 * (0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2])
    u = 128 + 224 * 0.5 * (b - (0.2126 * r + 0.7152 * g + 0.0722 * b)) / 0.5  # 近似即可，非精确
    v = 128 + 224 * 0.5 * (r - (0.2126 * r + 0.7152 * g + 0.0722 * b)) / 0.627
    y8 = np.clip(y, 0, 255).astype(np.uint8)
    u8 = np.clip(u, 0, 255).astype(np.uint8)
    v8 = np.clip(v, 0, 255).astype(np.uint8)
    return DecodedFrame(
        frame_index=idx, pts=None, time_seconds=idx / 24.0,
        planes=(y8, u8[::2, ::2], v8[::2, ::2]),
        pixel_format='yuv420p', width=W, height=H, is_proxy=False,
    )


def main():
    app = QApplication([])

    fmt = QSurfaceFormat()
    fmt.setVersion(4, 1)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    main_ctx = QOpenGLContext()
    main_ctx.setFormat(fmt)
    assert main_ctx.create(), "main context failed"
    surface = QOffscreenSurface()
    surface.setFormat(fmt)
    surface.create()
    main_ctx.makeCurrent(surface)

    import moderngl
    mgl_main = moderngl.create_context()  # 主线程 ModernGL（与渲染线程共享）

    r = AsyncRenderer(main_ctx)
    r.start()
    assert wait_until(lambda: r.video_denoise_pipeline is not None), "pipeline init failed"
    print("[ok] render thread + pipeline up")

    def submit_denoise(on: bool):
        if on:
            p = VideoDenoiseParams(
                temporal=TemporalDenoiseParams(enabled=True, frames=2, luma=70.0, chroma=60.0),
                spatial=SpatialDenoiseParams(enabled=True, mode='fast', luma=50.0, chroma=40.0),
            )
        else:
            p = VideoDenoiseParams()
        r.submit(RenderCommand.SET_VIDEO_DENOISE,
                 {'params': p.to_dict(), 'quality_mode': 'preview'})
        time.sleep(0.15)

    def read_front():
        tex_id = r.current_front_tex_id
        size = r.current_front_size
        if not tex_id or not size or size[0] <= 0:
            return None
        try:
            tex = mgl_main.external_texture(
                tex_id, size, components=4, dtype='f2', samples=0)
            data = tex.read(alignment=1)
            tex.release()
            arr = np.frombuffer(data, dtype=np.float16).reshape(size[1], size[0], 4)
            return arr.astype(np.float32)
        except Exception as exc:
            print("  [read_front failed]", exc)
            return None

    def play_planar(tag, n=6):
        r.video_playback_active = True
        ok_frames = 0
        for i in range(n):
            r.submit(RenderCommand.RENDER_VIDEO_FRAME, {
                'decoded_frame': make_planar_frame(i),
                'frame_index': i,
                'generation_id': 1,
                'render_params': {},
                'reason': 'playback',
                'quality': 'proxy',
            })
            if not wait_until(lambda i=i: r.debug_last_rendered_video_frame_index == i, 10.0):
                print(f"  [{tag}] frame {i} NOT processed")
                break
            ok_frames += 1
            time.sleep(0.05)
        r.video_playback_active = False
        time.sleep(0.2)
        arr = read_front()
        if arr is None:
            print(f"[{tag}] processed={ok_frames}/{n}  front read: FAILED")
            return
        rgb_mean = arr[..., :3].mean()
        print(f"[{tag}] processed={ok_frames}/{n}  front size={r.current_front_size} "
              f"mean={rgb_mean:.4f} max={arr.max():.3f} -> "
              f"{'BLACK!' if rgb_mean < 0.02 else 'ok'}")

    # 场景 1：降噪关（基线）
    submit_denoise(False)
    play_planar("planar, denoise OFF")

    # 场景 2：降噪开（时域+空域）
    submit_denoise(True)
    play_planar("planar, denoise ON ")

    # 场景 3：降噪开 + 暂停态重绘（grading）
    r.submit(RenderCommand.RENDER_FRAME, {
        'render_reason': 'grading',
        'viewport_size': (W, H), 'layers': [],
        'raw_preview': {'exposure': 0.0, 'gains': (1.0, 1.0, 1.0)},
        'film_grain': {}, 'visible_rect': (0, 0, float(W), float(H)),
        'show_mask': False, 'active_layer_index': 0,
    })
    time.sleep(0.4)
    arr = read_front()
    if arr is not None:
        print(f"[paused grading] mean={arr[..., :3].mean():.4f} -> "
              f"{'BLACK!' if arr[..., :3].mean() < 0.02 else 'ok'}")
    else:
        print("[paused grading] front read FAILED")

    stats = r.video_denoise_pipeline.get_stats()
    print("pipeline stats:", {k: stats.get(k) for k in ('frames', 'last_total_ms', 'window_frames')})

    r.is_running = False
    r.wait(5000)
    surface.destroy()


if __name__ == '__main__':
    main()
