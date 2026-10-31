#!/usr/bin/env python3
"""P0-GPU-1 导出 GPU 合成渲染器验证探针（一致性 + 吞吐 + 集成路径）。

三重验证：
1. 一致性：TimelineGpuExportRenderer 输出 vs **真实产品 CPU 链**
   （TimelineExporter._planes_to_bgr_float + GradingMath.apply_grading +
   cv2.resize + normalise/blend/quantize 的单层语义复刻），覆盖
   yuv422p10le/yuv420p、恒等/典型调色、opacity<1。
2. 集成路径：fake project/layer/DecodedFrame 走 TimelineExporter.
   _composite_layers，验证 GPU 钩子接线与 eligibility（GPU 结果应与
   直接调用渲染器一致；禁用开关应回落 CPU 判定）。
3. 吞吐：1080p→4K，GPU 渲染器 vs CPU 产品链，各 24 帧。

用法：
    python tools/dbg_gpu_export_renderer_check.py [--frames 24] [--quick]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

GRADE_TYPICAL = {
    "exposure": 8.0, "temp": 4.0, "tint": -2.0, "contrast": 6.0,
    "lift": (1.5, 0.0, -1.5), "gamma": (0.0, 2.0, 0.0),
    "gain": (2.0, 0.0, -2.0), "shadow": -8.0, "highlights": 6.0,
    "vibrance": 10.0, "saturation": 5.0,
}


def make_planes(fmt: str, w: int, h: int):
    """合成 planar YUV（梯度+正弦+噪声）。"""
    rng = np.random.default_rng(7)
    xs = np.linspace(0.0, 1.0, w, dtype=np.float32)
    ys = np.linspace(0.0, 1.0, h, dtype=np.float32)
    xg, yg = np.meshgrid(xs, ys)
    base = (0.5 + 0.25 * np.sin(12 * xg * 3.14159) * np.cos(9 * yg * 3.14159)
            + 0.08 * rng.standard_normal((h, w)).astype(np.float32))
    u_base = 0.5 + 0.2 * np.sin(7 * xg * 3.14159 + 1.0)
    v_base = 0.5 + 0.2 * np.cos(5 * yg * 3.14159 + 0.5)
    bits = 10 if "10le" in fmt else 8
    dtype = np.uint16 if bits == 10 else np.uint8
    cmax = float((1 << bits) - 1)
    if "420" in fmt:
        u_small = u_base[::2, ::2]
        v_small = v_base[::2, ::2]
    else:
        u_small = u_base[:, ::2] if "422" in fmt else u_base
        v_small = v_base[:, ::2] if "422" in fmt else v_base
    # limited-range 码值
    y = np.rint(np.clip(base, 0.0, 1.0) * (219.0 * (1 << max(0, bits - 8)))
                + 16.0 * (1 << max(0, bits - 8))).astype(dtype)
    u = np.rint(np.clip(u_small, 0.0, 1.0) * (224.0 * (1 << max(0, bits - 8)))
                + 128.0 * (1 << max(0, bits - 8))).astype(dtype)
    v = np.rint(np.clip(v_small, 0.0, 1.0) * (224.0 * (1 << max(0, bits - 8)))
                + 128.0 * (1 << max(0, bits - 8))).astype(dtype)
    return (np.ascontiguousarray(y), np.ascontiguousarray(u),
            np.ascontiguousarray(v))


def cpu_reference(ex, planes, fmt, meta, grade_params, out_w, out_h, opacity,
                  output_u16=False):
    """真实产品函数复刻 _composite_layers 单层语义（extract→grade→resize
    →normalise→blend→quantize）；output_u16 镜像高精度交付（u16 域链）。"""
    import cv2

    if output_u16:
        frame = ex._convert_yuv_planes_to_bgr16(planes, fmt, meta)
    else:
        bgr_f = ex._planes_to_bgr_float(planes, fmt, meta, None, False)
        if bgr_f is None:
            raise RuntimeError("CPU reference conversion failed")
        frame = np.rint(np.clip(bgr_f, 0.0, 1.0) * 255.0).astype(np.uint8)
    if frame is None:
        raise RuntimeError("CPU reference conversion failed")
    scale = float(np.iinfo(frame.dtype).max)
    if grade_params is not None:
        from src.features.color.core.the_decompiler import GradingMath

        rgb = frame[..., ::-1].astype(np.float32) / scale
        graded = GradingMath.apply_grading(
            rgb.reshape(-1, 3), grade_params).reshape(rgb.shape)
        frame = np.rint(np.clip(graded[..., ::-1], 0.0, 1.0)
                        * scale).astype(frame.dtype)
    resized = cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_LINEAR)
    layer_float = resized.astype(np.float32) / scale
    composite = layer_float * opacity
    out_max = 65535.0 if output_u16 else 255.0
    return np.rint(np.clip(composite, 0.0, 1.0) * out_max).astype(
        np.uint16 if output_u16 else np.uint8)


def make_exporter(project, provider):
    from src.features.edit.core.timeline_export import TimelineExporter

    ex = TimelineExporter.__new__(TimelineExporter)
    ex._project = project
    ex._sequence = None
    ex._frame_provider = provider
    ex._open_clips = {}
    ex._gpu_export_renderer = None
    ex._gpu_ineligible_clip_ids = set()
    ex._gpu_render_disabled = False
    ex._use_high_precision_export = False
    ex._export_has_alpha = False
    ex._export_color_metadata = None
    return ex


def diff_stats(a: np.ndarray, b: np.ndarray) -> str:
    d = np.abs(a.astype(np.int16) - b.astype(np.int16))
    return (f"maxdiff {d.max()} | 平均 |Δ| {d.mean():.4f} | "
            f">2 LSB {(d > 2).mean() * 100:.3f}%")


def cpu_reference_stack(ex, layer_inputs, meta, out_w, out_h,
                        output_u16=False):
    """多层 CPU 参照（镜像 _composite_layers 层循环）。

    layer_inputs = [(src, fmt, grade_params, opacity), ...]
    src = planes 元组（planar）或 BGR u8 ndarray（packed/图片），
    顺序为 track 自下而上。
    """
    import cv2

    composite = None
    for src, fmt, grade, opacity in layer_inputs:
        if isinstance(src, tuple):
            if output_u16:
                frame = ex._convert_yuv_planes_to_bgr16(src, fmt, meta)
            else:
                f = ex._planes_to_bgr_float(src, fmt, meta, None, False)
                frame = np.rint(np.clip(f, 0.0, 1.0) * 255.0).astype(np.uint8)
        else:
            frame = src
        if frame is None:
            raise RuntimeError("stack CPU reference conversion failed")
        scale = float(np.iinfo(frame.dtype).max)
        if grade is not None:
            from src.features.color.core.the_decompiler import GradingMath

            rgb = frame[..., ::-1].astype(np.float32) / scale
            graded = GradingMath.apply_grading(
                rgb.reshape(-1, 3), grade).reshape(rgb.shape)
            frame = np.rint(np.clip(graded[..., ::-1], 0.0, 1.0)
                            * scale).astype(frame.dtype)
        resized = cv2.resize(frame, (out_w, out_h),
                             interpolation=cv2.INTER_LINEAR)
        layer_float = resized.astype(np.float32) / scale
        if composite is None:
            composite = layer_float * opacity
        else:
            composite = composite * (1.0 - opacity) + layer_float * opacity
    out_max = 65535.0 if output_u16 else 255.0
    return np.rint(np.clip(composite, 0.0, 1.0) * out_max).astype(
        np.uint16 if output_u16 else np.uint8)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=24)
    ap.add_argument("--warmup", type=int, default=4)
    ap.add_argument("--quick", action="store_true", help="小尺寸快速自检")
    args = ap.parse_args()

    from src.features.edit.core.timeline_gpu_export_renderer import (
        GpuYuvSource, TimelineGpuExportRenderer, grade_params_to_uniforms,
    )
    from src.shared.media.frame import DecodedFrame

    meta = SimpleNamespace(color_range="limited", color_matrix="bt709")
    ex = make_exporter(None, None)

    src_w, src_h = (640, 360) if args.quick else (1920, 1080)
    out_w, out_h = (1280, 720) if args.quick else (3840, 2160)

    cases = [
        ("yuv422p10le→4K 恒等", "yuv422p10le", None, 1.0, False),
        ("yuv422p10le→4K 典型调色", "yuv422p10le", GRADE_TYPICAL, 1.0, False),
        ("yuv422p10le→4K opacity0.7", "yuv422p10le", None, 0.7, False),
        ("yuv420p→4K 恒等", "yuv420p", None, 1.0, False),
        # P0-GPU-2 高精度交付（u16）：>8-bit 源 RGB16UI 全链；8-bit 源 u8 链出口 u16
        ("yuv422p10le→4K u16 恒等", "yuv422p10le", None, 1.0, True),
        ("yuv422p10le→4K u16 典型调色", "yuv422p10le", GRADE_TYPICAL, 1.0, True),
        ("yuv420p→4K u16 恒等(8bit源)", "yuv420p", None, 1.0, True),
        ("yuv420p→4K u16 典型调色(8bit源)", "yuv420p", GRADE_TYPICAL, 1.0, True),
    ]

    renderer = TimelineGpuExportRenderer()
    gpu_identity_result = None
    try:
        print(f"== GPU 导出渲染器一致性（{src_w}x{src_h} → {out_w}x{out_h}）==")
        for label, fmt, grade, opacity, out16 in cases:
            planes = make_planes(fmt, src_w, src_h)
            src = GpuYuvSource(
                planes=planes, luma_bits=10 if "10le" in fmt else 8,
                chroma_x_div=2 if "422" in fmt or "420" in fmt else 1,
                chroma_y_div=2 if "420" in fmt else 1,
                full_range=False, kr=0.2126, kb=0.0722)
            uniforms = grade_params_to_uniforms(grade) if grade else None
            gpu = renderer.render_planar(src, uniforms, out_w, out_h, opacity,
                                         output_u16=out16)
            cpu = cpu_reference(ex, planes, fmt, meta, grade,
                                out_w, out_h, opacity, output_u16=out16)
            if gpu is None:
                print(f"[{label}] GPU 返回 None（渲染失败）")
                continue
            if gpu.shape != cpu.shape or gpu.dtype != cpu.dtype:
                print(f"[{label}] 形状/类型不匹配 GPU{gpu.dtype}{gpu.shape} "
                      f"CPU{cpu.dtype}{cpu.shape}")
                continue
            unit = "code16" if out16 else "LSB"
            src10 = "10le" in fmt
            if not out16:
                # u8 域链包络：cv2 定点双线性在 <0.1% 像素上 ±3 LSB
                #（420 两轴插值略宽于 422 的 2）
                bar = 3 if "420" in fmt else 2
            elif src10:
                bar = 8  # deep-u16 链：量化点逐点镜像，4K 实测包络 ≤7
            else:
                bar = 780  # 8-bit 源 u8 域链：±3 u8-LSB × 257
            d = np.abs(cpu.astype(np.int32) - gpu.astype(np.int32))
            status = "OK" if d.max() <= bar else "超出约定"
            print(f"[{label}] maxdiff {d.max()} {unit} | 平均 |Δ| {d.mean():.4f} "
                  f"| >{bar} 占比 {(d > bar).mean() * 100:.4f}% [{status}]")
            if grade is None and opacity == 1.0 and not out16:
                gpu_identity_result = (gpu, cpu)

        # ── P0-GPU-3 多层合成（须在独立 renderer 实例创建/释放之前完成：
        # 同线程其它 context 释放会使本 renderer 失去线程当前性）──
        print("\n== 多层合成（render_layer_stack / RGBA32F 累积）==")
        p422 = make_planes("yuv422p10le", src_w, src_h)
        p420 = make_planes("yuv420p", src_w, src_h)
        img8 = np.rint(np.clip(
            ex._planes_to_bgr_float(p420, "yuv420p", meta, None, False),
            0.0, 1.0) * 255.0).astype(np.uint8)
        src422 = GpuYuvSource(planes=p422, luma_bits=10, chroma_x_div=2,
                              chroma_y_div=1, full_range=False,
                              kr=0.2126, kb=0.0722)
        src420 = GpuYuvSource(planes=p420, luma_bits=8, chroma_x_div=2,
                              chroma_y_div=2, full_range=False,
                              kr=0.2126, kb=0.0722)
        u_grade = grade_params_to_uniforms(GRADE_TYPICAL)

        stack_cases = [
            ("2层 u8 (422调色+图片0.65)",
             [(p422, "yuv422p10le", GRADE_TYPICAL, 1.0),
              (img8, None, None, 0.65)],
             [(src422, u_grade, 1.0), (img8, None, 0.65)], False),
            ("3层 u8 混合",
             [(p420, "yuv420p", None, 1.0),
              (p422, "yuv422p10le", GRADE_TYPICAL, 0.8),
              (img8, None, None, 0.5)],
             [(src420, None, 1.0), (src422, u_grade, 0.8),
              (img8, None, 0.5)], False),
            ("2层 u16 (422调色+图片0.65)",
             [(p422, "yuv422p10le", GRADE_TYPICAL, 1.0),
              (img8, None, None, 0.65)],
             [(src422, u_grade, 1.0), (img8, None, 0.65)], True),
            ("3层 u16 混合",
             [(p420, "yuv420p", None, 1.0),
              (p422, "yuv422p10le", GRADE_TYPICAL, 0.8),
              (img8, None, None, 0.5)],
             [(src420, None, 1.0), (src422, u_grade, 0.8),
              (img8, None, 0.5)], True),
        ]
        for label, cpu_layers, gpu_layers, out16 in stack_cases:
            gpu = renderer.render_layer_stack(gpu_layers, out_w, out_h,
                                              output_u16=out16)
            cpu = cpu_reference_stack(ex, cpu_layers, meta, out_w, out_h,
                                      output_u16=out16)
            if gpu is None or gpu.shape != cpu.shape \
                    or gpu.dtype != cpu.dtype:
                print(f"[{label}] 失败/不匹配: "
                      f"gpu={None if gpu is None else (gpu.dtype, gpu.shape)}")
                continue
            bar = 200 if out16 else 4  # u16 栈：≈0.78 u8-LSB 等价（FS 口径）
            d = np.abs(cpu.astype(np.int32) - gpu.astype(np.int32))
            status = "OK" if d.max() <= bar else "超出约定"
            print(f"[{label}] maxdiff {d.max()} | 平均 |Δ| {d.mean():.4f} "
                  f"| >{bar} 占比 {(d > bar).mean() * 100:.4f}% [{status}]")

        # 多层集成放在吞吐之后（独立 context 的创建/释放会使本进程内
        # 先前 context 失去线程当前性——moderngl 不做按调用 make-current）

        # ── 吞吐 ──
        print(f"\n== 吞吐（{src_w}x{src_h} → {out_w}x{out_h}，"
              f"{args.frames} 帧）==")
        planes = make_planes("yuv422p10le", src_w, src_h)
        src = GpuYuvSource(
            planes=planes, luma_bits=10, chroma_x_div=2, chroma_y_div=1,
            full_range=False, kr=0.2126, kb=0.0722)
        uniforms = grade_params_to_uniforms(GRADE_TYPICAL)
        n = args.frames + args.warmup

        t0 = time.perf_counter()
        for i in range(n):
            assert renderer.render_planar(src, uniforms, out_w, out_h, 1.0) \
                is not None
        gpu_ms = (time.perf_counter() - t0) / args.frames * 1000
        t0 = time.perf_counter()
        for i in range(n):
            assert renderer.render_planar(
                src, uniforms, out_w, out_h, 1.0,
                output_u16=True) is not None
        gpu16_ms = (time.perf_counter() - t0) / args.frames * 1000
        stack3 = [(src, uniforms, 1.0), (img8, None, 0.65)]
        t0 = time.perf_counter()
        for i in range(n):
            assert renderer.render_layer_stack(
                stack3, out_w, out_h) is not None
        stack_ms = (time.perf_counter() - t0) / args.frames * 1000
        stack3_cpu = [(planes, "yuv422p10le", GRADE_TYPICAL, 1.0),
                      (img8, None, None, 0.65)]
        t0 = time.perf_counter()
        for i in range(n):
            cpu_reference_stack(ex, stack3_cpu, meta, out_w, out_h)
        cpu_ms = (time.perf_counter() - t0) / args.frames * 1000
        t0 = time.perf_counter()
        for i in range(n):
            cpu_reference(ex, planes, "yuv422p10le", meta, GRADE_TYPICAL,
                          out_w, out_h, 1.0)
        cpu1_ms = (time.perf_counter() - t0) / args.frames * 1000
        print(f"  GPU 单层u8(含调色) {gpu_ms:.1f} ms ({1000.0 / gpu_ms:.0f} fps)"
              f"  vs CPU {cpu1_ms:.0f} ms → {cpu1_ms / gpu_ms:.0f}×")
        print(f"  GPU 单层u16(含调色) {gpu16_ms:.1f} ms ({1000.0 / gpu16_ms:.0f} fps)")
        print(f"  GPU 2层栈(422调色+图片) {stack_ms:.1f} ms ({1000.0 / stack_ms:.0f} fps)"
              f"  vs CPU 2层 {cpu_ms:.0f} ms → {cpu_ms / stack_ms:.0f}×")

        # ── 集成路径（独立 renderer 实例；其后不再使用主 renderer）──
        print("\n== 集成路径（_composite_layers GPU 钩子）==")
        decoded = DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            planes=planes, pixel_format="yuv422p10le", metadata=meta)
        stack = SimpleNamespace(
            grading_params=dict(GRADE_TYPICAL), layers=[],
            active_layer_index=None)
        asset = SimpleNamespace(
            media_type="video", asset_id="a1", path="fake.mov",
            effective_path=None)
        project = SimpleNamespace(
            color_stacks={"sid": stack},
            get_asset_by_id=lambda _aid: asset)
        provider = SimpleNamespace(
            open_clip=lambda p, share_by_path=False: "clip-1",
            request_frame=lambda cid, f, quality="master": decoded)
        layer = SimpleNamespace(
            clip_id="c1", asset_id="a1", source_frame=0, opacity=1.0,
            crop=(), track_index=0,
            clip=SimpleNamespace(effects=(), color_stack_id="sid"))
        config = SimpleNamespace(resolution=(out_w, out_h))

        integ_ex = make_exporter(project, provider)
        frame = integ_ex._composite_layers([layer], (), config, 0)
        if frame is None:
            print("[单层集成] GPU 返回 None（失败）")
        else:
            print(f"[单层集成 GPU 帧 vs CPU 参照(典型调色)] "
                  f"{diff_stats(cpu_reference(ex, planes, 'yuv422p10le', meta, GRADE_TYPICAL, out_w, out_h, 1.0), frame)}")
        integ_ex._release_gpu_export_renderer()

        # 禁用开关短路
        integ_ex2 = make_exporter(project, provider)
        integ_ex2._gpu_render_disabled = True
        assert integ_ex2._gpu_render_composite([layer], (), out_w, out_h) is None
        print("[禁用开关] TOPOS_EXPORT_GPU_RENDER=0 → 短路返回 None ✓")

        # 高精度集成：_use_high_precision_export=True → u16 帧
        integ_hd = make_exporter(project, provider)
        integ_hd._use_high_precision_export = True
        frame16 = integ_hd._composite_layers([layer], (), config, 0)
        if frame16 is not None:
            print(f"[高精度集成] dtype={frame16.dtype} shape={frame16.shape} "
                  f"| vs CPU u16 参照 maxdiff "
                  f"{np.abs(cpu_reference(ex, planes, 'yuv422p10le', meta, GRADE_TYPICAL, out_w, out_h, 1.0, output_u16=True).astype(np.int32) - frame16.astype(np.int32)).max()}")
        else:
            print("[高精度集成] GPU 返回 None（失败）")

        # 多层集成：视频层 + 图片层
        asset2 = SimpleNamespace(
            media_type="image", asset_id="a2", path="fake_img.png",
            effective_path=None)
        project2 = SimpleNamespace(
            color_stacks={"sid": stack},
            get_asset_by_id=lambda aid: asset if aid == "a1" else asset2)
        layer2 = SimpleNamespace(
            clip_id="c2", asset_id="a2", source_frame=0, opacity=0.65,
            crop=(), track_index=1,
            clip=SimpleNamespace(effects=(), color_stack_id=None))
        integ2 = make_exporter(project2, provider)
        integ2._get_image_frame = lambda p: img8
        frame2 = integ2._composite_layers([layer, layer2], (), config, 0)
        if frame2 is None:
            print("[多层集成] GPU 返回 None（失败）")
        else:
            cpu2 = cpu_reference_stack(
                ex, [(planes, "yuv422p10le", GRADE_TYPICAL, 1.0),
                     (img8, None, None, 0.65)], meta, out_w, out_h)
            d2 = np.abs(cpu2.astype(np.int32) - frame2.astype(np.int32))
            print(f"[多层集成(视频+图片)] maxdiff {d2.max()} | "
                  f"平均 |Δ| {d2.mean():.4f}")
        for inst in (integ_ex, integ_hd, integ2):
            inst._release_gpu_export_renderer()

        # ── P0-GPU-4 转场：GPU vs 真实产品 CPU 路径对拍 ──
        print("\n== 转场（GPU vs 真实 _composite_layers CPU 路径）==")

        def _fake_trans(ttype, left_id, right_id, lo=1.0, ro=1.0,
                        factor=0.3, color=(1.0, 0.5, 0.0)):
            return SimpleNamespace(
                transition_type=ttype,
                transition_id=f"t-{ttype}",
                left_clip_id=left_id, right_clip_id=right_id,
                blend_factor=0.4,
                left_opacity=lo, right_opacity=ro,
                transition=SimpleNamespace(params={"direction": "right"}),
                get_color_factor=lambda: factor,
                blend_color=color)

        l1 = SimpleNamespace(
            clip_id="c1", asset_id="a1", source_frame=0, opacity=1.0,
            crop=(), track_index=0,
            clip=SimpleNamespace(effects=(), color_stack_id="sid"))
        l2 = SimpleNamespace(
            clip_id="c2", asset_id="a2", source_frame=0, opacity=1.0,
            crop=(), track_index=1,
            clip=SimpleNamespace(effects=(), color_stack_id=None))
        asset2_v = SimpleNamespace(
            media_type="video", asset_id="a2", path="fake2.mov",
            effective_path=None)
        proj_wipe = SimpleNamespace(
            color_stacks={"sid": stack},
            get_asset_by_id=lambda aid: (
                asset if aid == "a1" else asset2_v))
        seen = {"a2": 0}

        def _req2(clip_id, frame, quality="master"):
            # a2 返回不同内容（420 平面）以区分两侧
            seen["a2"] += 1
            return DecodedFrame(
                frame_index=0, pts=0, time_seconds=0.0,
                planes=p420, pixel_format="yuv420p", metadata=meta)

        provider2 = SimpleNamespace(
            open_clip=lambda p, share_by_path=False: "clip-2",
            request_frame=_req2)

        def _parity(label, layers, transitions):
            ex_g = make_exporter(proj_wipe, provider2)
            gpu = ex_g._composite_layers(layers, transitions, config, 0)
            ex_g._release_gpu_export_renderer()
            ex_c = make_exporter(proj_wipe, provider2)
            ex_c._gpu_render_disabled = True
            cpu = ex_c._composite_layers(layers, transitions, config, 0)
            if gpu is None or cpu is None:
                print(f"[{label}] 失败 gpu={gpu is not None} cpu={cpu is not None}")
                return
            if gpu.shape != cpu.shape or gpu.dtype != cpu.dtype:
                print(f"[{label}] 形状/类型不匹配")
                return
            d = np.abs(cpu.astype(np.int32) - gpu.astype(np.int32))
            bar = 4
            status = "OK" if d.max() <= bar else "超出约定"
            print(f"[{label}] maxdiff {d.max()} | 平均 |Δ| {d.mean():.4f} "
                  f"| >{bar} 占比 {(d > bar).mean() * 100:.4f}% [{status}]")

        _parity("dissolve(有效opacity 0.35/0.75)", [l1, l2],
                (_fake_trans("dissolve", "c1", "c2", 0.35, 0.75),))
        _parity("wipe(422调色|420, progress0.4)", [l1, l2],
                (_fake_trans("wipe", "c1", "c2"),))
        _parity("iris_circle", [l1, l2],
                (_fake_trans("iris_circle", "c1", "c2"),))
        _parity("dip_to_color(0.3)", [l1, l2],
                (_fake_trans("dip_to_color", "c1", "c2"),))
        l3 = SimpleNamespace(
            clip_id="c3", asset_id="a2", source_frame=0, opacity=0.5,
            crop=(), track_index=2,
            clip=SimpleNamespace(effects=(), color_stack_id=None))
        _parity("slide_left + 上层视频0.5", [l1, l2, l3],
                (_fake_trans("slide_left", "c1", "c2"),))
        # 高精度交付下的 wipe
        ex_g16 = make_exporter(proj_wipe, provider2)
        ex_g16._use_high_precision_export = True
        gpu16 = ex_g16._composite_layers(
            [l1, l2], (_fake_trans("wipe", "c1", "c2"),), config, 0)
        ex_g16._release_gpu_export_renderer()
        ex_c16 = make_exporter(proj_wipe, provider2)
        ex_c16._use_high_precision_export = True
        ex_c16._gpu_render_disabled = True
        cpu16 = ex_c16._composite_layers(
            [l1, l2], (_fake_trans("wipe", "c1", "c2"),), config, 0)
        if gpu16 is not None and cpu16 is not None:
            d = np.abs(cpu16.astype(np.int32) - gpu16.astype(np.int32))
            print(f"[wipe u16 交付] dtype={gpu16.dtype} maxdiff {d.max()} "
                  f"| 平均 |Δ| {d.mean():.4f} [u16 域包络 600]")
        else:
            print(f"[wipe u16 交付] 失败 gpu={gpu16 is not None} "
                  f"cpu={cpu16 is not None}")
    finally:
        renderer.release()


if __name__ == "__main__":
    main()
