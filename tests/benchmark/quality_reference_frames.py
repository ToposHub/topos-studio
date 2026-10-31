#!/usr/bin/env python3
"""P0-05: image-quality golden reference frames for the YUV GPU pipeline.

Renders deterministic synthetic YUV(A) sources through the real
``YUVGPUUploader`` upload + conversion path into an RGBA16F framebuffer,
and either records golden frames (``--update-golden``) or compares against
previously recorded goldens (backend-migration correctness gate, plan §6.4).

Coverage: SDR/WideGamut matrices (BT.709/BT.601/BT.2020), limited/full
range, 8/10/12-bit, 4:2:0/4:2:2/4:4:4 and the alpha plane path.

Usage:
    # record goldens (run once per baseline)
    .venv/bin/python tests/benchmark/quality_reference_frames.py --update-golden

    # correctness gate (CI / before-after backend change)
    .venv/bin/python tests/benchmark/quality_reference_frames.py

Artifacts (tests/media/results/quality_reference/):
    golden/<case_id>.npy   RGBA16F top-left origin golden frames
    reference_meta.json    environment + tolerances for the golden set
    quality_reference_check.json / .md   last gate result (paired)
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

REPO = Path(__file__).resolve().parent.parent.parent
OUT_DIR = REPO / 'tests' / 'media' / 'results' / 'quality_reference'
GOLDEN_DIR = OUT_DIR / 'golden'

WIDTH, HEIGHT = 512, 288
BLOCK = 64  # chroma pattern block size: keeps bilinear-vs-nearest divergence local

DEFAULT_TOLERANCE = {
    # 444 chroma is full-resolution: shader must match CPU math almost exactly.
    'max_abs_444': 2.0 / 255.0,
    'mean_abs': 1.0 / 255.0,
    'rmse': 1.0 / 255.0,
    # 420/422 bilinear chroma upsampling diverges at block edges only.
    'max_abs_subsampled': 6.0 / 255.0,
}


def _git_sha() -> str:
    try:
        return subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=REPO,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return 'unknown'


# ----------------------------------------------------------------------------
# deterministic source pattern
# ----------------------------------------------------------------------------

def build_test_planes(
    bit_depth: int,
    subsampling: str,
    with_alpha: bool = False,
) -> Dict[str, np.ndarray]:
    """Deterministic YUV(A) source covering ramps, patches and legal extremes."""
    max_code = (1 << bit_depth) - 1
    w, h = WIDTH, HEIGHT

    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    y = 0.55 * xx / max(w - 1, 1) + 0.35 * yy / max(h - 1, 1)
    y += 0.10 * ((xx // BLOCK + yy // BLOCK) % 2)
    y_plane = np.clip(y, 0.0, 1.0)

    def chroma_field(phase: float) -> np.ndarray:
        cw, ch = (w, h) if subsampling == '444' else (
            (w // 2, h // 2) if subsampling == '420' else (w // 2, h)
        )
        cyy, cxx = np.mgrid[0:ch, 0:cw].astype(np.float64)
        c = 0.5 + 0.45 * np.sin(2.0 * np.pi * (cxx / max(cw - 1, 1)) + phase)
        c += 0.2 * ((cxx // (BLOCK // 2) + cyy // (BLOCK // 2)) % 2) - 0.1
        return np.clip(c, 0.0, 1.0)

    planes = {
        'y': np.ascontiguousarray(
            np.rint(y_plane * max_code).astype(np.uint16 if bit_depth > 8 else np.uint8)),
        'u': np.ascontiguousarray(
            np.rint(chroma_field(0.0) * max_code).astype(np.uint16 if bit_depth > 8 else np.uint8)),
        'v': np.ascontiguousarray(
            np.rint(chroma_field(np.pi / 2) * max_code).astype(np.uint16 if bit_depth > 8 else np.uint8)),
    }
    if with_alpha:
        step = 8 if bit_depth > 8 else 8
        a = (np.add.outer(np.arange(h), np.arange(w)) // step * step) % (step * 16)
        a = (a % (max_code + 1)).astype(np.uint16 if bit_depth > 8 else np.uint8)
        planes['a'] = np.ascontiguousarray(a)
    return planes


# ----------------------------------------------------------------------------
# CPU reference conversion (same math family as the GLSL shader)
# ----------------------------------------------------------------------------

_MATRIX_COEFFS = {
    # Row form used by the shader (yuv_gpu_upload.py GLSL): R=y+a·v, G=y+b·u+c·v, B=y+d·u
    # bt601 是数字 YCbCr 标准系数（Kr=0.299/Kb=0.114 推导，±0.5 色度约定），
    # 与 AUD-39800 修正后的 yuv_gpu_upload.py GPU 矩阵一致
    # （AUD-R2-010 订正；旧 PAL 模拟系数 1.140/2.032/-0.3945/-0.581 已废弃）。
    'bt709': (1.5748, -0.1873, -0.4681, 1.8556),
    'bt601': (1.4020, -0.3441, -0.7141, 1.7720),
    'bt2020': (1.4746, -0.1646, -0.5714, 1.8814),
}


def _upsample_chroma(c: np.ndarray, h: int, w: int, bilinear: bool) -> np.ndarray:
    """Upsample chroma to the luma grid.

    bilinear=True reproduces LINEAR-filtered texture sampling at luma pixel
    centers; bilinear=False is NEAREST sampling (integer-texture path).
    """
    ch, cw = c.shape
    if (ch, cw) == (h, w):
        return c
    if not bilinear:
        axis0 = np.repeat(np.arange(ch), 2 if ch != h else 1)[:h]
        axis1 = np.repeat(np.arange(cw), 2 if cw != w else 1)[:w]
        return c[np.ix_(axis0, axis1)]

    r_idx = (np.arange(h) + 0.5) / h * ch - 0.5
    c_idx = (np.arange(w) + 0.5) / w * cw - 0.5
    r0 = np.clip(np.floor(r_idx).astype(np.int64), 0, ch - 1)
    c0 = np.clip(np.floor(c_idx).astype(np.int64), 0, cw - 1)
    r1 = np.clip(r0 + 1, 0, ch - 1)
    c1 = np.clip(c0 + 1, 0, cw - 1)
    fr = np.clip(r_idx - np.floor(r_idx), 0.0, 1.0)[..., None]
    fc = np.clip(c_idx - np.floor(c_idx), 0.0, 1.0)[None, ...]
    top = c[r0][:, c0] * (1.0 - fc) + c[r0][:, c1] * fc
    bot = c[r1][:, c0] * (1.0 - fc) + c[r1][:, c1] * fc
    return top * (1.0 - fr) + bot * fr


def cpu_reference(
    planes: Dict[str, np.ndarray],
    bit_depth: int,
    subsampling: str,
    matrix: str,
    limited: bool,
    alpha_bit_depth: Optional[int] = None,
    chroma_bilinear: bool = False,
) -> np.ndarray:
    """Convert planes to RGBA float32 (top-left origin, unclamped RGB).

    ``chroma_bilinear`` must match the texture filter of the upload path:
    LINEAR for r8/float16 pools, NEAREST for the r16ui integer path.
    """
    y = planes['y'].astype(np.float64)
    u = planes['u'].astype(np.float64)
    v = planes['v'].astype(np.float64)
    h, w = y.shape
    max_code = float((1 << bit_depth) - 1)

    if limited:
        shift = float(1 << (bit_depth - 8))
        y_min, y_max = 16.0 * shift, 235.0 * shift
        c_neutral, c_range = 128.0 * shift, 224.0 * shift
        yn = (y - y_min) / (y_max - y_min)
        # shader maps chroma to ±0.5 (range 224), not ±1.0
        un = (u - c_neutral) / c_range
        vn = (v - c_neutral) / c_range
    else:
        yn = y / max_code
        un = u / max_code - 0.5
        vn = v / max_code - 0.5

    if subsampling in ('420', '422'):
        un = _upsample_chroma(un, h, w, chroma_bilinear)
        vn = _upsample_chroma(vn, h, w, chroma_bilinear)

    kr_u, kg_u, kg_v, kb_v = _MATRIX_COEFFS[matrix]
    r = yn + kr_u * vn
    g = yn + kg_u * un + kg_v * vn
    b = yn + kb_v * un

    rgba = np.stack(
        [r, g, b, np.ones_like(r)], axis=-1, dtype=np.float64)

    if 'a' in planes:
        a_bits = alpha_bit_depth or bit_depth
        rgba[..., 3] = planes['a'].astype(np.float64) / float((1 << a_bits) - 1)

    # NOTE: no RGB clamp — the pipeline keeps out-of-gamut values (HDR rule),
    # and the shader writes unclampled results into the RGBA16F target.
    return rgba.astype(np.float32)


# ----------------------------------------------------------------------------
# GPU render
# ----------------------------------------------------------------------------

_CASES: List[Dict[str, object]] = [
    {'case_id': 'sdr_8bit_709_limited_420', 'bit_depth': 8, 'subsampling': '420',
     'matrix': 'bt709', 'limited': True},
    {'case_id': 'sdr_8bit_601_full_444', 'bit_depth': 8, 'subsampling': '444',
     'matrix': 'bt601', 'limited': False},
    {'case_id': 'sdr_10bit_709_limited_422', 'bit_depth': 10, 'subsampling': '422',
     'matrix': 'bt709', 'limited': True},
    {'case_id': 'sdr_12bit_709_limited_444', 'bit_depth': 12, 'subsampling': '444',
     'matrix': 'bt709', 'limited': True},
    {'case_id': 'wide_10bit_2020_limited_420', 'bit_depth': 10, 'subsampling': '420',
     'matrix': 'bt2020', 'limited': True},
    {'case_id': 'alpha_10bit_709_limited_444', 'bit_depth': 10, 'subsampling': '444',
     'matrix': 'bt709', 'limited': True, 'with_alpha': True},
]


def render_case(ctx, uploader, case: Dict[str, object]) -> Tuple[np.ndarray, str]:
    """Render one case through the real upload+conversion path, RGBA16F out."""
    import moderngl

    bit_depth = int(case['bit_depth'])  # type: ignore[arg-type]
    subsampling = str(case['subsampling'])
    with_alpha = bool(case.get('with_alpha', False))

    planes = build_test_planes(bit_depth, subsampling, with_alpha)
    upload = getattr(
        uploader,
        {'420': 'upload_yuv420', '422': 'upload_yuv422', '444': 'upload_yuv444'}[subsampling])
    result = upload(
        planes['y'], planes['u'], planes['v'], 0, bit_depth,
        **({'a': planes['a'], 'alpha_bit_depth': bit_depth} if with_alpha else {}))
    y_tex, u_tex, v_tex = result[0], result[1], result[2]
    a_tex = result[3] if len(result) > 3 else None

    mode = 'r16ui' if uploader.is_r16_available() and bit_depth > 8 else (
        'r8' if bit_depth <= 8 else 'float16')

    tex = ctx.texture((WIDTH, HEIGHT), components=4, dtype='f2')
    fbo = ctx.framebuffer(color_attachments=[tex])
    uploader.set_yuv_matrix(
        {'bt709': uploader.MATRIX_BT709,
         'bt601': uploader.MATRIX_BT601,
         'bt2020': uploader.MATRIX_BT2020}[str(case['matrix'])])  # type: ignore[arg-type]
    uploader.set_color_range(
        uploader.RANGE_LIMITED if case['limited'] else uploader.RANGE_FULL)  # type: ignore[arg-type]
    try:
        uploader.render_yuv_to_rgb(
            y_tex, u_tex, v_tex, fbo,
            bit_depth=bit_depth,
            a_tex=a_tex,
            alpha_bit_depth=bit_depth if with_alpha else 10,
            alpha_premultiplied=False,
        )
        raw = fbo.read(components=4, dtype='f2')
    finally:
        fbo.release()
        tex.release()

    frame = np.frombuffer(raw, dtype=np.float16).reshape(HEIGHT, WIDTH, 4).astype(np.float32)
    # moderngl offscreen FBO read() is already top-row-first; no flip needed.
    return frame.copy(), mode


def metrics(gpu: np.ndarray, ref: np.ndarray) -> Dict[str, float]:
    diff = np.abs(gpu[..., :3].astype(np.float64) - ref[..., :3].astype(np.float64))
    return {
        'max_abs': float(diff.max()),
        'mean_abs': float(diff.mean()),
        'rmse': float(np.sqrt((diff ** 2).mean())),
        'p99_abs': float(np.percentile(diff, 99.0)),
        'alpha_max_abs': float(np.abs(gpu[..., 3].astype(np.float64)
                                      - ref[..., 3].astype(np.float64)).max()),
    }


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--update-golden', action='store_true',
                        help='record golden frames instead of comparing')
    parser.add_argument('--cases', type=str, default='',
                        help='comma-separated case_id filter')
    parser.add_argument('--tolerance-scale', type=float, default=1.0,
                        help='scale all tolerances (regression budgets only)')
    args = parser.parse_args()

    import moderngl
    from src.shared.video.yuv_gpu_upload import YUVGPUUploader

    tol = {k: v * args.tolerance_scale for k, v in DEFAULT_TOLERANCE.items()}
    selected = [c for c in _CASES
                if not args.cases or str(c['case_id']) in args.cases.split(',')]

    ctx = moderngl.create_standalone_context()
    uploader = YUVGPUUploader(ctx, pool_size=5)
    env = {
        'git_sha': _git_sha(),
        'timestamp': datetime.now().isoformat(),
        'os': platform.platform(),
        'gl_renderer': str(ctx.info.get('GL_RENDERER', '')),
        'gl_version': str(ctx.info.get('GL_VERSION', '')),
        'r16ui_available': bool(uploader.is_r16_available()),
        'tolerances': tol,
    }
    print(f"GL: {env['gl_renderer']} / {env['gl_version']}  "
          f"r16ui={env['r16ui_available']}  git={env['git_sha'][:12]}")

    results: List[Dict[str, object]] = []
    failed = False
    for case in selected:
        case_id = str(case['case_id'])
        subsampled = str(case['subsampling']) in ('420', '422')
        ref = cpu_reference(
            build_test_planes(int(case['bit_depth']), str(case['subsampling']),
                              bool(case.get('with_alpha', False))),
            int(case['bit_depth']), str(case['subsampling']),
            str(case['matrix']), bool(case['limited']),
            int(case['bit_depth']) if case.get('with_alpha') else None,
            chroma_bilinear=int(case['bit_depth']) <= 8)

        gpu, mode = render_case(ctx, uploader, case)
        m = metrics(gpu, ref)
        entry: Dict[str, object] = {'case_id': case_id, 'mode': mode, **m}

        golden_path = GOLDEN_DIR / f'{case_id}.npy'
        if args.update_golden:
            GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
            np.save(golden_path, gpu.astype(np.float16))
            entry['golden'] = 'written'
        elif golden_path.exists():
            golden = np.load(golden_path).astype(np.float32)
            gm = metrics(gpu, golden)
            entry['golden_metrics'] = gm
        else:
            entry['golden_metrics'] = None
            entry['note'] = 'golden missing (run --update-golden)'

        limit_max = tol['max_abs_subsampled'] if subsampled else tol['max_abs_444']
        passed = (m['mean_abs'] <= tol['mean_abs'] and m['rmse'] <= tol['rmse']
                  and m['max_abs'] <= limit_max
                  and m['alpha_max_abs'] <= tol['max_abs_444'])
        entry['passed'] = passed
        failed = failed or not passed
        results.append(entry)
        print(f"[{'PASS' if passed else 'FAIL'}] {case_id} ({mode})  "
              f"max={m['max_abs'] * 255:.2f}/255 mean={m['mean_abs'] * 255:.3f}/255 "
              f"rmse={m['rmse'] * 255:.3f}/255 alpha_max={m['alpha_max_abs'] * 255:.2f}/255")

    uploader.release()
    ctx.release()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    meta = {'schema': 'topos.quality_reference.v1', 'environment': env, 'cases': results}
    (OUT_DIR / 'quality_reference_check.json').write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + '\n')
    md = ['# 画质参考帧检查（P0-05）', '',
          f"> {env['timestamp']}  git `{env['git_sha'][:12]}`  "
          f"GL `{env['gl_renderer']}`", '',
          '| 用例 | 上传模式 | max/255 | mean/255 | rmse/255 | 结果 |', '|---|---|---:|---:|---:|---|']
    md += [f"| {r['case_id']} | {r['mode']} | {r['max_abs'] * 255:.2f} | "
           f"{r['mean_abs'] * 255:.3f} | {r['rmse'] * 255:.3f} | "
           f"{'PASS' if r['passed'] else 'FAIL'} |" for r in results]
    md.append('')
    (OUT_DIR / 'quality_reference_check.md').write_text('\n'.join(md))

    if args.update_golden:
        meta['golden_set'] = {str(c['case_id']): f"golden/{c['case_id']}.npy" for c in results}
        (OUT_DIR / 'reference_meta.json').write_text(
            json.dumps(meta, indent=2, ensure_ascii=False) + '\n')
        print(f"golden updated in {GOLDEN_DIR}")
        return 0
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
