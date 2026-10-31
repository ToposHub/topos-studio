#!/usr/bin/env python3
"""调色页调色工具性能基准 — 真实视频 + 真实 Color 页 AsyncRenderer GPU 管线。

用法:
    .venv/bin/python tools/bench_color_tools.py                     # 2K + 4K 全场景
    .venv/bin/python tools/bench_color_tools.py --video 2K --only baseline primary grain
    .venv/bin/python tools/bench_color_tools.py --frames 96 --warmup 12

测量口径:
  - PyAV 预解码真实测试视频帧(yuv420p 平面), 解码不占渲染预算(单独报告解码 fps)
  - 每帧 submit(RENDER_VIDEO_FRAME) -> 轮询渲染完成, video_render_sync_mode='finish'
    保证 GPU 计入耗时; 帧率 = 渲染吞吐上限(无 24fps 节流)
  - 场景含: 单工具 / 多工具组合 / 多图层 / 播放降级(preview_quality)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# macOS: offscreen QPA 无法创建 GL context, 用原生平台 + QOffscreenSurface
# (同 tools/e2e_playback_bench.py 的做法)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import logging
logging.getLogger().setLevel(logging.ERROR)

import numpy as np
import av
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QOpenGLContext, QOffscreenSurface, QSurfaceFormat

from src.features.color.core.async_renderer import AsyncRenderer, RenderCommand
from src.features.color.core.plugin_manager import get_plugin_manager
from src.shared.media.frame import DecodedFrame
from src.shared.effects.preview_quality import get_effect_preview_quality_manager

# 真实应用在 plugin 面板加载时扫描 plugins/ 目录; headless 基准需手动触发
get_plugin_manager().scan_plugins()

VIDEOS = {
    "2K": ROOT / "tests/test_videos/2K_h264.mp4",
    "4K": ROOT / "tests/test_videos/4K_H264.mov",
}
LUT_PATH = ROOT / "luts/cinematic.cube"

# ---------------------------------------------------------------------------
# 帧解码
# ---------------------------------------------------------------------------

def plane_to_array(plane) -> np.ndarray:
    arr = np.frombuffer(plane, dtype=np.uint8)
    return np.ascontiguousarray(arr.reshape(plane.height, plane.line_size)[:, :plane.width])


def decode_frames(path: Path, count: int):
    t0 = time.perf_counter()
    container = av.open(str(path))
    stream = container.streams.video[0]
    frames = []
    for frame in container.decode(stream):
        if frame.format.name not in ("yuv420p", "yuvj420p"):
            frame = frame.reformat(format="yuv420p")
        y = plane_to_array(frame.planes[0])
        u = plane_to_array(frame.planes[1])
        v = plane_to_array(frame.planes[2])
        frames.append((frame.width, frame.height, y, u, v))
        if len(frames) >= count:
            break
    container.close()
    dt = time.perf_counter() - t0
    return frames, len(frames) / dt  # frames, decode fps


# ---------------------------------------------------------------------------
# 场景构建 — layer dict / aux 纹理上传
# ---------------------------------------------------------------------------

IDENTITY_GP = {
    'lift': (0, 0, 0), 'gamma': (0, 0, 0), 'gain': (0, 0, 0),
    'exposure': 0.0, 'temp': 0.0, 'tint': 0.0, 'contrast': 0.0,
    'shadow': 0.0, 'shadows': 0.0, 'highlights': 0.0,
    'saturation': 0.0, 'vibrance': 0.0,
    'mid_detail': 0.0, 'sharpness': 0.0, 'hue_shift': 0.0,
}

PRIMARY_GP = {
    'lift': (0.02, 0.15, -0.05), 'gamma': (0.55, 0.08, 0.02), 'gain': (0.95, 0.05, 0.1),
    'exposure': 0.35, 'temp': 0.12, 'tint': 0.03, 'contrast': 0.18,
    'shadow': 0.06, 'shadows': 0.06, 'highlights': -0.08,
    'saturation': 0.12, 'vibrance': 0.15,
    'mid_detail': 0.0, 'sharpness': 0.0, 'hue_shift': 0.0,
}

SHARPEN_FX = {
    'enabled': True, 'mode': 0, 'sharpen': 0.5, 'texture': 0.2, 'halo': 0.1,
    'radius': 1.5, 'iterations': 2,
    'blur_enabled': False, 'blur_mode': 0, 'blur_intensity': 0.0,
    'blur_detail': 0.0, 'blur_radius': 0.0, 'blur_bokeh': 0.0, 'blur_angle': 0.0,
}


def blur_fx(mode: int, radius: float = 10.0, iterations: int = 1) -> dict:
    return {
        'enabled': False, 'mode': 0, 'sharpen': 0.0, 'texture': 0.0, 'halo': 0.0,
        'radius': 0.0, 'iterations': 1,
        'blur_enabled': True, 'blur_mode': mode, 'blur_intensity': 0.8,
        'blur_detail': 0.5, 'blur_radius': radius, 'blur_bokeh': 0.5,
        'blur_angle': 30.0,
    }


def make_curves_lut() -> np.ndarray:
    """非恒等 RGB 曲线 LUT (256,1,3) float32 — 轻微 S 曲线。"""
    x = np.arange(256, dtype=np.float32) / 255.0
    s = np.clip(0.5 + (x - 0.5) * 1.18, 0.0, 1.0)          # 对比 S 曲线
    lift = np.clip(s * 0.94 + 0.02, 0.0, 1.0)
    return np.stack([lift, s, np.clip(s * 0.97 + 0.01, 0, 1)], axis=-1).reshape(256, 1, 3).astype(np.float32)


def make_hsl_lut() -> np.ndarray:
    """非恒等 HSL 曲线 LUT (6,256) float32。identity = ones 且 [1,:]=0。"""
    arr = np.ones((6, 256), dtype=np.float32)
    arr[1, :] = 0.0                       # HueHue 保持恒等
    arr[0, :] = np.linspace(0.92, 1.18, 256)   # LumSat
    arr[4, :] = 1.12                      # SatSat
    return arr


def make_warper_grid(n: int = 8) -> np.ndarray:
    """warper 网格 (n,n,2) float32, 小幅色相/饱和扰动。"""
    g = np.zeros((n, n, 2), dtype=np.float32)
    g[..., 0] = np.linspace(-0.04, 0.04, n)[:, None]   # hue delta
    g[..., 1] = np.linspace(0.02, -0.02, n)[None, :]   # sat delta
    return g


def circle_mask(inside: bool = True) -> dict:
    return {
        "id": "bench-shape-1", "type": "circle", "name": None,
        "visible": True, "invert": not inside, "operation": "add",
        "feather": 50, "opacity": 1.0, "blur": 0.0,
        "clean_black": 0.0, "clean_white": 0.0,
        "center": [0.5, 0.45], "size": [0.35, 0.35], "rotation": 0.0,
        "points": [], "grad_points": [[0.4, 0.5], [0.6, 0.5]], "softness": 0.2,
    }


QUALIFIER_HUE = {
    "enabled": True, "hue_enabled": True, "hue_center": 0.95, "hue_width": 0.18,
    "hue_soft": 0.06,
    "sat_enabled": True, "sat_low": 0.2, "sat_high": 1.0,
    "sat_soft_low": 0.05, "sat_soft_high": 0.0,
    "lum_enabled": True, "lum_low": 0.1, "lum_high": 0.9,
    "lum_soft_low": 0.05, "lum_soft_high": 0.05,
    "blur": 0.0, "clean_black": 0.0, "clean_white": 0.0, "invert": False,
}

SOFT_CLIP = {
    "enabled": True, "high": 1.0, "white_point": 1.1, "shoulder_strength": 1.0,
    "low": 0.0, "black_point": -0.1, "toe_strength": 1.0,
    "exposure": 0.0, "contrast": 0.0,
}

MIXER = {
    'red': [0.04, 0.1, 0.0], 'orange': [0.02, 0.05, 0.0],
    'yellow': [0.0, -0.06, 0.0], 'green': [-0.03, 0.08, 0.0],
    'aqua': [0.0, 0.0, 0.02], 'blue': [0.03, -0.05, 0.04],
    'purple': [0.0, 0.06, 0.0], 'magenta': [-0.02, 0.04, 0.0],
}

GRAIN = {'enabled': True, 'intensity': 0.3, 'size': 1.5, 'monochrome': False,
         'shadows': 0.5, 'midtones': 0.5, 'highlights': 0.5, 'seed': 7}


def plugin_instance(pid: str, params: dict) -> dict:
    return {'plugin_id': pid, 'instance_id': f'{pid}-inst-0',
            'enabled': True, 'params': params, 'type': 'dctl'}


def base_layer(lid: str, **over) -> dict:
    layer = {
        'id': lid, 'visible': True, 'opacity': 1.0, 'blend_mode': 'Normal',
        'grading_params': dict(IDENTITY_GP),
        'active_lut_path': None, 'lut_intensity': 1.0,
        'soft_clip_params': None, 'qualifier_params': None,
        'curves_lut': None, 'hsl_curve_lut': None,
        'warper_params': None, 'mixer_params': None, 'effects_params': None,
        'mask_shapes': [], 'has_mask': False, 'ai_matte_enabled': False,
        'plugins': [],
    }
    layer.update(over)
    return layer


def apply_playback_degrade(layers, film_grain):
    """复刻 video_playback._build_render_params 在播放时施加的 preview_quality 降级。"""
    layers = [dict(l) for l in layers]
    for l in layers:
        fx = l.get('effects_params')
        if fx and fx.get('blur_enabled') and fx.get('blur_radius', 0.0) > 20.0:
            fx = dict(fx)
            fx['blur_radius'] = 20.0
            fx['iterations'] = 1
            l['effects_params'] = fx
    if film_grain and film_grain.get('enabled'):
        film_grain = get_effect_preview_quality_manager().get_degraded_params(
            'film_grain', film_grain)
    return layers, film_grain


# 场景注册表: name -> (描述, builder)
# builder 返回 (layers, aux_payloads, film_grain)

def build_scenarios():
    S = {}

    def add(name, desc, layers, aux=None, grain=None, dynamic=None):
        S[name] = (desc, layers, aux or [], grain, dynamic)

    # ---- 单工具 ----
    add('baseline', '无调色(恒等参数)', [base_layer('L0')])
    add('primary', '一级调色:色轮+曝光+对比+饱和',
        [base_layer('L0', grading_params=dict(PRIMARY_GP))])
    add('detail', '中间调细节+锐化(grading 内 8 采样)',
        [base_layer('L0', grading_params={**IDENTITY_GP, 'mid_detail': 0.5, 'sharpness': 0.4})])
    curves = make_curves_lut()
    add('curves', 'RGB 曲线',
        [base_layer('L0', curves_lut=curves)],
        aux=[{'type': 'curves', 'layer_id': 'L0', 'data': curves.tobytes()}])
    hsl = make_hsl_lut()
    add('hsl_curves', 'HSL 曲线(6条)',
        [base_layer('L0', hsl_curve_lut=hsl)],
        aux=[{'type': 'hsl_curve', 'layer_id': 'L0', 'data': hsl.tobytes()}])
    add('mixer', 'RGB 混色器(8带)', [base_layer('L0', mixer_params=dict(MIXER))])
    wgrid = make_warper_grid(8)
    add('warper', '色彩扭曲器(8x8)',
        [base_layer('L0', warper_params=wgrid)],
        aux=[{'type': 'warper', 'layer_id': 'L0', 'size': (8, 8), 'data': wgrid.tobytes()}])
    add('lut3d', '3D LUT (cinematic.cube)',
        [base_layer('L0', active_lut_path=str(LUT_PATH))],
        aux=[{'type': 'lut3d', 'layer_id': 'L0', 'path': str(LUT_PATH)}])
    add('softclip', '柔化裁切', [base_layer('L0', soft_clip_params=dict(SOFT_CLIP))])
    add('sharpen_usm', 'USM 锐化(r=1.5,iters=2)',
        [base_layer('L0', effects_params=dict(SHARPEN_FX))])
    add('blur_soften', '质感模糊 bilateral(r=10)',
        [base_layer('L0', effects_params=blur_fx(0, 10))])
    add('blur_optical', '光学虚化 bokeh(r=10)',
        [base_layer('L0', effects_params=blur_fx(1, 10))])
    add('blur_motion', '动感模糊(r=10)',
        [base_layer('L0', effects_params=blur_fx(2, 10))])
    add('blur_fast', '快速模糊(r=10)',
        [base_layer('L0', effects_params=blur_fx(3, 10))])
    add('grain', '胶片颗粒(彩色,intensity .3)',
        [base_layer('L0')], grain=dict(GRAIN))
    add('qualifier', '限定器(HSL抠像)',
        [base_layer('L0', qualifier_params=dict(QUALIFIER_HUE))])
    qf = {**QUALIFIER_HUE, 'blur': 8.0, 'clean_black': 5.0, 'clean_white': 5.0}
    add('qualifier_fuzz', '限定器+羽化+去噪散点',
        [base_layer('L0', qualifier_params=qf)])
    add('mask_circle', '圆形 Power Window',
        [base_layer('L0', mask_shapes=[circle_mask()], has_mask=True)])
    add('mask_qualifier', 'Window+限定器组合',
        [base_layer('L0', mask_shapes=[circle_mask()], has_mask=True,
                    qualifier_params=dict(QUALIFIER_HUE))])
    add('plugin_skin', 'DCTL:双边滤波磨皮(64采样)',
        [base_layer('L0', plugins=[plugin_instance(
            'bilateral_skin_smooth', {'strength': 0.8, 'radius': 0.0,
                                      'threshold': 0.1, 'recover': 0.4})])])
    add('plugin_hpf', 'DCTL:高频修复',
        [base_layer('L0', plugins=[plugin_instance(
            'HighPass_Retouch', {})])])

    add('usm_heavy', 'USM 重参数(mode1,r=5,iters=4)',
        [base_layer('L0', effects_params={
            **SHARPEN_FX, 'mode': 1, 'radius': 5.0, 'iterations': 4})])
    add('blur_soften_r30', '质感模糊 bilateral(r=30)',
        [base_layer('L0', effects_params=blur_fx(0, 30))])

    # ---- 动态场景: 每帧修改参数(触发 matte/shader 重建路径) ----
    add('qualifier_reframe', '限定器逐帧重抠(matte 每帧重建)',
        [base_layer('L0', qualifier_params=dict(QUALIFIER_HUE))], dynamic='qualifier')
    add('mask_reframe', 'Window 逐帧重建 mask',
        [base_layer('L0', mask_shapes=[circle_mask()], has_mask=True)], dynamic='mask')
    add('primary_drag', '一级调色拖动(每帧改参数)',
        [base_layer('L0', grading_params=dict(PRIMARY_GP))], dynamic='grading')

    # ---- 组合 ----
    add('combo_typical', '典型工作流:一级+曲线+LUT',
        [base_layer('L0', grading_params=dict(PRIMARY_GP),
                    curves_lut=curves, active_lut_path=str(LUT_PATH))],
        aux=[{'type': 'curves', 'layer_id': 'L0', 'data': curves.tobytes()},
             {'type': 'lut3d', 'layer_id': 'L0', 'path': str(LUT_PATH)}])

    full_layer = base_layer(
        'L0',
        grading_params={**PRIMARY_GP, 'mid_detail': 0.5, 'sharpness': 0.4},
        curves_lut=curves, hsl_curve_lut=hsl, mixer_params=dict(MIXER),
        warper_params=wgrid, active_lut_path=str(LUT_PATH),
        soft_clip_params=dict(SOFT_CLIP), effects_params=blur_fx(0, 30),
        mask_shapes=[circle_mask()], has_mask=True,
        qualifier_params=dict(QUALIFIER_HUE),
        plugins=[plugin_instance('bilateral_skin_smooth',
                                 {'strength': 0.8, 'radius': 0.0,
                                  'threshold': 0.1, 'recover': 0.4})])
    full_aux = [
        {'type': 'curves', 'layer_id': 'L0', 'data': curves.tobytes()},
        {'type': 'hsl_curve', 'layer_id': 'L0', 'data': hsl.tobytes()},
        {'type': 'warper', 'layer_id': 'L0', 'size': (8, 8), 'data': wgrid.tobytes()},
        {'type': 'lut3d', 'layer_id': 'L0', 'path': str(LUT_PATH)},
    ]
    add('combo_full', '单层全开(所有工具)', [full_layer], aux=full_aux, grain=dict(GRAIN))

    # ---- 多图层 ----
    l2 = [
        base_layer('L0', grading_params=dict(PRIMARY_GP), effects_params=blur_fx(3, 8)),
        base_layer('L1', grading_params=dict(PRIMARY_GP), curves_lut=curves,
                   mask_shapes=[circle_mask()], has_mask=True),
    ]
    add('layers_2', '双层:L0一级+快糊 / L1曲线+Window', l2,
        aux=[{'type': 'curves', 'layer_id': 'L1', 'data': curves.tobytes()}])

    l3 = [
        base_layer('L0', grading_params=dict(PRIMARY_GP)),
        base_layer('L1', grading_params=dict(PRIMARY_GP), curves_lut=curves,
                   mask_shapes=[circle_mask()], has_mask=True),
        base_layer('L2', hsl_curve_lut=hsl, qualifier_params=dict(QUALIFIER_HUE),
                   blend_mode='Soft Light', opacity=0.7),
    ]
    add('layers_3', '三层:一级 / 曲线+Window / HSL+限定器', l3,
        aux=[{'type': 'curves', 'layer_id': 'L1', 'data': curves.tobytes()},
             {'type': 'hsl_curve', 'layer_id': 'L2', 'data': hsl.tobytes()}])
    return S


SCENARIOS = build_scenarios()


# ---------------------------------------------------------------------------
# 基准执行
# ---------------------------------------------------------------------------

def wait_frame(renderer, idx: int, timeout: float = 30.0) -> bool:
    deadline = time.perf_counter() + timeout
    while renderer.debug_last_rendered_video_frame_index != idx:
        if time.perf_counter() > deadline:
            return False
        time.sleep(0.0005)
    return True


def run_scenario(app, renderer, frames, layers, aux, grain, warmup, measure, dynamic=None):
    w, h = frames[0][0], frames[0][1]
    render_params = {
        'viewport_size': (w, h),
        'visible_rect': (0.0, 0.0, float(w), float(h)),
        'scene_visible_rect': (0.0, 0.0, float(w), float(h)),
        'grain_visible_rect': (0.0, 0.0, float(w), float(h)),
        'current_lod': 0,
        'layers': layers,
        'show_mask': False,
        'active_layer_index': len(layers) - 1,
        'film_grain': grain or {'enabled': False},
        'render_reason': 'playback',
    }
    for payload in aux:
        renderer.submit(RenderCommand.UPDATE_AUX_TEX, dict(payload))

    n = len(frames)
    last = renderer.debug_last_rendered_video_frame_index
    idx = (last + 1) if isinstance(last, int) else 0

    def tweak_for_frame(i: int):
        if dynamic == 'qualifier' and layers[0].get('qualifier_params'):
            layers[0]['qualifier_params']['hue_center'] = 0.95 + (i % 32) * 1e-6
        elif dynamic == 'mask' and layers[0].get('mask_shapes'):
            layers[0]['mask_shapes'][0]['feather'] = 50.0 + (i % 32) * 1e-6
        elif dynamic == 'grading' and layers[0].get('grading_params'):
            layers[0]['grading_params']['exposure'] = 0.35 + (i % 32) * 0.003

    def submit_one(i: int):
        tweak_for_frame(i)
        _, _, y, u, v = frames[i % n]
        df = DecodedFrame(frame_index=i, pts=i * 1000, time_seconds=i / 25.0,
                          data=None, planes=(y, u, v), pixel_format='yuv420p',
                          width=w, height=h)
        renderer.submit(RenderCommand.RENDER_VIDEO_FRAME, {
            'decoded_frame': df, 'frame_index': i, 'render_params': render_params,
            'generation_id': 0, 'drop_if_stale': False,
            'quality': 'proxy', 'reason': 'playback',
            'yuv_matrix': 0, 'color_range': 0,
        })

    # warmup: shader 编译 / 纹理创建 / LUT 解析都不计入
    for _ in range(warmup):
        idx += 1
        submit_one(idx)
        if not wait_frame(renderer, idx):
            return None

    per_frame = []
    for _ in range(measure):
        idx += 1
        t0 = time.perf_counter()
        submit_one(idx)
        if not wait_frame(renderer, idx):
            return None
        per_frame.append((time.perf_counter() - t0) * 1000.0)
        app.processEvents()

    per_frame_np = np.asarray(per_frame)
    return {
        'fps': 1000.0 / float(per_frame_np.mean()),
        'ms_mean': float(per_frame_np.mean()),
        'ms_p50': float(np.percentile(per_frame_np, 50)),
        'ms_p95': float(np.percentile(per_frame_np, 95)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--video', nargs='*', default=['2K', '4K'], help='2K/4K 子集')
    ap.add_argument('--only', nargs='*', default=None, help='场景名过滤')
    ap.add_argument('--frames', type=int, default=96, help='预解码帧数')
    ap.add_argument('--measure', type=int, default=96, help='每场景测量帧数')
    ap.add_argument('--warmup', type=int, default=10)
    ap.add_argument('--json-out', type=str, default=None)
    ap.add_argument('--cooldown', type=float, default=0.0,
                    help='seconds to sleep between scenarios (thermal stability)')
    args = ap.parse_args()

    names = [n for n in SCENARIOS if not args.only or n in args.only]
    if not names:
        print(f'no scenario matches filter: {args.only}')
        return 2

    app = QApplication.instance() or QApplication(sys.argv)
    fmt = QSurfaceFormat()
    fmt.setVersion(4, 1)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    QSurfaceFormat.setDefaultFormat(fmt)
    surface = QOffscreenSurface()
    surface.create()
    main_ctx = QOpenGLContext()
    if not main_ctx.create():
        print('FAILED to create GL context')
        return 1
    main_ctx.makeCurrent(surface)

    renderer = AsyncRenderer(main_ctx)
    renderer.video_render_sync_mode = 'finish'
    renderer.video_playback_active = True
    renderer.start()
    t0 = time.time()
    while renderer.ctx is None and time.time() - t0 < 20:
        app.processEvents()
        time.sleep(0.02)
    if renderer.ctx is None:
        print('renderer ctx not ready')
        return 1

    results = {}
    for vkey in args.video:
        path = VIDEOS[vkey]
        if not path.exists():
            print(f'[SKIP] {path} not found')
            continue
        frames, decode_fps = decode_frames(path, args.frames)
        w, h = frames[0][0], frames[0][1]
        print(f'\n=== {vkey}: {path.name} {w}x{h} | decode-only {decode_fps:.0f} fps '
              f'| {len(frames)} frames buffered ===')
        hdr = f"{'scenario':<18} {'fps':>7} {'ms/f':>7} {'p50':>7} {'p95':>7}  vs_base"
        print(hdr)
        base_fps = None
        results[vkey] = {'decode_fps': decode_fps, 'resolution': f'{w}x{h}', 'rows': {}}
        for name in names:
            desc, layers, aux, grain, dynamic = SCENARIOS[name]
            r = run_scenario(app, renderer, frames, layers, aux, grain,
                             args.warmup, args.measure, dynamic)
            if r is None:
                print(f'{name:<18} TIMEOUT/FAIL')
                continue
            if name == 'baseline':
                base_fps = r['fps']
            delta = '' if base_fps is None or name == 'baseline' else (
                f'x{r["fps"] / base_fps:.2f}')
            budget = 'OK ' if r['fps'] >= 24 else 'LOW'
            print(f'{name:<18} {r["fps"]:7.1f} {r["ms_mean"]:7.2f} '
                  f'{r["ms_p50"]:7.2f} {r["ms_p95"]:7.2f}  {delta:>6} {budget}')
            results[vkey]['rows'][name] = r
            if args.cooldown > 0:
                time.sleep(args.cooldown)

            # 播放降级版本紧跟 full 场景
            if name == 'combo_full':
                dl, dg = apply_playback_degrade(layers, grain)
                r2 = run_scenario(app, renderer, frames, dl, aux, dg,
                                  args.warmup, args.measure)
                if r2:
                    print(f'{"combo_full_pb":<18} {r2["fps"]:7.1f} {r2["ms_mean"]:7.2f} '
                          f'{r2["ms_p50"]:7.2f} {r2["ms_p95"]:7.2f}  '
                          f'x{r2["fps"] / base_fps:.2f} {"OK " if r2["fps"] >= 24 else "LOW"}'
                          f'  (播放降级)')
                    results[vkey]['rows']['combo_full_pb'] = r2

    renderer.video_playback_active = False
    renderer.stop()

    if args.json_out:
        with open(args.json_out, 'w') as f:
            json.dump(results, f, indent=1)
        print(f'\n[json] {args.json_out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
