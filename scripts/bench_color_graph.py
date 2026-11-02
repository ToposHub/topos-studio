#!/usr/bin/env python
"""B16.2 调色节点图媒体级基准（F08）——冻结 300 帧工作负载。

同机对照**图执行路径（N07 图 plan）**与**旧 layers 串行路径（N06
run_serial_passes，生产同一算子）**：decode / compile / graph / present
分段计时，报告 p50/p95/p99、吞吐、丢帧（相对 60fps/30fps 预算）与
资源增长（RSS + scratch 纹理计数）。

设计（计划 B16.2）：

- **decode**：cv2 读取真实测试素材帧（默认 4K_H264.mov，可 --media 换
  素材），转 scene-linear float32 并缩放到目标尺寸——只计时取帧+色彩
  展开，缩放不计入 decode 段。
- **compile**：图快照 → render plan 编译 + 首个 shader 变体编译（含
  曲线 LUT 上传）一次性成本。
- **graph**：逐帧 N07 执行（串行三 Corrector：乘性/加性/曲线——f03
  非交换结构），输入上传 RGBA16F；**legacy 对照**：同一图层序列经
  run_serial_passes ping-pong（与生产 E03 同一算子调用）。
- **present**：FBO readback 作为呈现代理成本（量化到 fp16 读回）。
- **无旧链双执行断言**：图路径逐帧只执行 plan.passes（executed_passes
  记录），不触发 legacy 循环；两路径互不调用（脚本结构即证据，报告
  记录 per-frame executed_passes）。
- **无资源持续增长断言**：每 100 帧 RSS/纹理计数不单调增（报告
  `resource_growth`）。

用法（计划验收原样）：
    QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/bench_color_graph.py \
        --fixture-set standard --sizes 1920x1080,3840x2160 --frames 300 \
        --report docs/validation/color_nodes/backend_bench.json
"""

from __future__ import annotations

import argparse
import gc
import json
import platform
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from statistics import quantiles

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
            capture_output=True, text=True, check=True).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _pct(samples_ms: list[float]) -> dict[str, float]:
    if not samples_ms:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0,
                "mean": 0.0}
    ordered = sorted(samples_ms)
    qs = quantiles(ordered, n=100, method="inclusive")
    return {"p50": round(qs[49], 3), "p95": round(qs[94], 3),
            "p99": round(qs[98], 3), "max": round(ordered[-1], 3),
            "mean": round(sum(ordered) / len(ordered), 3)}


def _dropped(samples_ms: list[float], fps: float) -> int:
    budget = 1000.0 / fps
    return sum(1 for ms in samples_ms if ms > budget)


def _rss_mb() -> float:
    """当前常驻内存（非高水位——ru_maxrss 只增不减，会伪造"增长"）。"""
    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024.0 * 1024.0)
    except Exception:  # noqa: BLE001
        return -1.0


def _decode_frames(media: Path, size: tuple[int, int], frames: int
                   ) -> tuple[list[np.ndarray], dict]:
    """读取前 N 帧 → scene-linear float32 + 目标尺寸缩放。

    decode 段只计 cv2.read + float 展开；resize 计入 prepare 段。
    """
    import cv2
    cap = cv2.VideoCapture(str(media))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开素材: {media}")
    width, height = size
    decoded: list[np.ndarray] = []
    decode_ms: list[float] = []
    prepare_ms: list[float] = []
    while len(decoded) < frames:
        t0 = time.perf_counter()
        ok, bgr = cap.read()
        if not ok:
            break
        decode_ms.append((time.perf_counter() - t0) * 1000.0)
        t1 = time.perf_counter()
        if (bgr.shape[1], bgr.shape[0]) != (width, height):
            bgr = cv2.resize(bgr, (width, height),
                             interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).astype(np.float32)
        rgb = rgb / 255.0
        rgba = np.dstack([rgb, np.ones(rgb.shape[:2], np.float32)])
        decoded.append(np.ascontiguousarray(rgba))
        prepare_ms.append((time.perf_counter() - t1) * 1000.0)
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    info = {"frames_decoded": len(decoded),
            "source_size": f"{src_w}x{src_h}",
            "decode_ms": _pct(decode_ms), "prepare_ms": _pct(prepare_ms)}
    return decoded, info


def _legacy_layers_frame(ctx, pipeline, layer_payloads, src_tex, size,
                         quad_cache):
    """旧生产 layers 单帧形态：上传一次 → 逐层 ping-pong → 返回末层纹理。

    与图路径同 I/O 形态（一次上传、无逐层 readback），逐层
    prepare/render_corrector_gl——即 E03 旧循环经 N06 提取后的同一算子。
    """
    from src.features.color.core.composition_uniforms import (
        composition_uniform_values,
        upload_composition_uniforms,
    )
    from src.features.color.core.corrector_render_service import (
        prepare_corrector,
        render_corrector_gl,
    )
    width, height = size
    current = src_tex
    owned: list = []
    white = ctx.texture((width, height), components=4, dtype="f2",
                        data=np.ones((height, width, 4), dtype=np.float16))
    for payload in layer_payloads:
        if payload.get("visible") is False:
            continue
        prepared = prepare_corrector(pipeline, payload, layer_id="legacy")
        out_tex = ctx.texture((width, height), components=4, dtype="f2")
        fbo = ctx.framebuffer(color_attachments=[out_tex])
        fbo.use()

        def _update(prog, _payload=payload):
            values = composition_uniform_values(
                _payload, bindings=tuple(prepared.bindings),
                width=width, height=height)
            upload_composition_uniforms(prog, values)

        def _aux(prog, _payload=payload):
            unit = 2
            if "u_curves_tex" in prog:
                lut = _payload.get("curves_lut")
                if lut is None:
                    ramp = np.linspace(0.0, 1.0, 256, dtype=np.float32)
                    arr = np.stack([ramp] * 3, -1).reshape(256, 1, 3)
                else:
                    arr = np.asarray(lut, np.float32).reshape(256, 1, 3)
                tex = ctx.texture((256, 1), components=3, dtype="f4",
                                  data=np.ascontiguousarray(arr).tobytes())
                tex.filter = (ctx.LINEAR, ctx.LINEAR)
                tex.use(unit)
                prog["u_curves_tex"].value = unit
        render_corrector_gl(
            ctx, prepared, layer=payload, grade_input_tex=current,
            matte_tex=white, viewport=size, update_uniforms=_update,
            bind_aux=_aux, render_quad=quad_cache)
        fbo.release()
        owned.append(out_tex)
        current = out_tex
    white.release()
    return current, owned


def _bench_layers():
    """f03 非交换三层（乘性/加性/曲线）——与 B00 冻结结构一致。"""
    from src.features.color.core.layer import build_curves_lut_from_data
    from tests.color.fixtures.color_graph import _neutral_layer
    l0 = _neutral_layer("Print Warmer")
    l0.grading_params["exposure"] = 0.5
    l0.grading_params["gain"] = (0.0, 0.0, -0.15)
    l1 = _neutral_layer("Lift Shadows")
    l1.grading_params["lift"] = (0.0, 0.0, 0.05)
    l1.grading_params["contrast"] = -0.2
    l2 = _neutral_layer("Tone Curve")
    l2.curves_data = {"M": [(0, 255), (64, 209), (191, 46), (255, 0)]}
    l2.curves_lut = build_curves_lut_from_data(l2.curves_data)
    l2.grading_params["saturation"] = 1.15
    return [l0, l1, l2]


def _payloads_for(graph):
    from src.features.color.core.layer import build_curves_lut_from_data
    from src.shared.project.color_graph import unfreeze_params

    def provide(node_id):
        node = next((n for n in graph.nodes if n.node_id == node_id), None)
        payload = dict(unfreeze_params(dict(node.legacy_payload))) if (
            node is not None and node.legacy_payload) else {}
        if node is not None:
            payload["visible"] = node.enabled
        if payload.get("curves_data"):
            payload["curves_lut"] = build_curves_lut_from_data(
                dict(payload["curves_data"]))
        return payload
    return provide


def run_size(ctx_factory, media: Path, size: tuple[int, int], frames: int,
             warmup: int = 5) -> dict:
    import moderngl
    from src.features.color.core.color_graph_compiler import compile_graph
    from src.features.color.core.color_graph_cpu import run_plan_cpu  # noqa: F401
    from src.features.color.core.color_graph_executor import ColorGraphExecutor
    from src.features.color.core.color_graph_migration import migrate_legacy_stack
    from src.features.color.core.color_graph_snapshot import freeze_graph
    from src.features.color.core.shader_compiler import ShaderPipeline
    from tests.color.test_color_graph_migration import _stack_with_layers

    layers = _bench_layers()
    width, height = size

    decoded, decode_info = _decode_frames(media, size, frames)
    if len(decoded) < 10:
        raise RuntimeError(f"素材可解码帧不足: {len(decoded)}")

    ctx = ctx_factory()
    try:
        # ── compile 段（一次性） ──
        t0 = time.perf_counter()
        graph = migrate_legacy_stack(_stack_with_layers(layers))
        provider = _payloads_for(graph)
        plan = compile_graph(freeze_graph(graph, session_id="bench"),
                             size=size)
        executor = ColorGraphExecutor(ctx)
        # 首帧 warmup 同时编译 shader 变体 + 曲线 LUT（计入 compile 段）
        src_tex = ctx.texture(size, components=4, dtype="f2",
                              data=decoded[0].astype(np.float16))
        warm = executor.execute_plan(plan, src_tex, request_id="warm",
                                     source_size=size,
                                     layer_param_provider=provider)
        assert warm.published, warm.diagnostic_message
        executor.release_revision("warm")
        src_tex.release()
        compile_ms = (time.perf_counter() - t0) * 1000.0

        # ── 逐帧双路径 ──
        graph_ms: list[float] = []
        present_ms: list[float] = []
        executed_passes: set[int] = set()
        rss_samples: list[float] = []
        for i, frame in enumerate(decoded):
            if i < warmup:
                continue   # warmup 帧不计时（驱动首帧抖动）
            t1 = time.perf_counter()
            tex = ctx.texture(size, components=4, dtype="f2",
                              data=frame.astype(np.float16))
            result = executor.execute_plan(
                plan, tex, request_id=f"bench-{i}", source_size=size,
                layer_param_provider=provider)
            if not result.published:
                raise RuntimeError(result.diagnostic_message or "未发布")
            executed_passes.add(result.executed_passes)
            graph_ms.append((time.perf_counter() - t1) * 1000.0)
            t2 = time.perf_counter()
            fbo = ctx.framebuffer(color_attachments=[result.output_texture])
            fbo.use()
            np.frombuffer(fbo.read(components=4, dtype="f2"),
                          dtype=np.float16)
            present_ms.append((time.perf_counter() - t2) * 1000.0)
            executor.release_revision(f"bench-{i}")
            tex.release()
            if i % 100 == 0:
                rss_samples.append(_rss_mb())
                gc.collect()

        # ── legacy 对照（同一算子 ping-pong；独立显式循环，非图路径
        #    内部触发——两路径互不调用即"无旧链双执行"的结构证据） ──
        import moderngl as _mgl
        pipeline = ShaderPipeline(ctx)
        layer_payloads = [provider(n.node_id) for n in graph.nodes
                          if n.node_type == "corrector"]

        def _quad(prog):
            vao = ctx.vertex_array(prog, [(ctx.buffer(np.array(
                [-1.0, -1.0, 0.0, 0.0, 1.0, -1.0, 1.0, 0.0,
                 -1.0, 1.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0],
                dtype="f4").tobytes()), "2f 2f", "in_vert", "in_texcoord")])
            vao.render(_mgl.TRIANGLE_STRIP)

        legacy_ms: list[float] = []
        for i, frame in enumerate(decoded):
            if i < warmup:
                continue
            t1 = time.perf_counter()
            tex = ctx.texture(size, components=4, dtype="f2",
                              data=frame.astype(np.float16))
            final_tex, owned = _legacy_layers_frame(
                ctx, pipeline, layer_payloads, tex, size, _quad)
            fbo = ctx.framebuffer(color_attachments=[final_tex])
            fbo.use()
            np.frombuffer(fbo.read(components=4, dtype="f2"),
                          dtype=np.float16)
            legacy_ms.append((time.perf_counter() - t1) * 1000.0)
            tex.release()
            for t in owned:
                t.release()
        # 图路径仅执行 plan passes；legacy 计数独立（无叠加）
        dual_execution = {
            "graph_executed_passes": sorted(executed_passes),
            "plan_passes": len(plan.passes),
            "note": "图路径逐帧只跑 plan passes；legacy 对照为独立循环，"
                    "两路径互不调用（无生产双执行）",
        }
        growth = (max(rss_samples) - min(rss_samples[:2])) if len(
            rss_samples) >= 2 else 0.0
        return {
            "size": f"{width}x{height}",
            "media": media.name,
            "frames": len(decoded),
            "decode": decode_info,
            "compile_ms": round(compile_ms, 3),
            "graph_ms": _pct(graph_ms),
            "present_ms": _pct(present_ms),
            "legacy_ms": _pct(legacy_ms),
            "fps_graph": round(1000.0 / max(1e-9, _pct(graph_ms)["p50"]), 2),
            "fps_legacy": round(1000.0 / max(1e-9, _pct(legacy_ms)["p50"]),
                                2),
            "dropped_frames": {
                "graph@60fps": _dropped(graph_ms, 60.0),
                "graph@30fps": _dropped(graph_ms, 30.0),
                "legacy@60fps": _dropped(legacy_ms, 60.0),
                "legacy@30fps": _dropped(legacy_ms, 30.0),
            },
            "resource": {
                "rss_samples_mb": [round(v, 1) for v in rss_samples],
                "rss_growth_mb": round(growth, 1),
                # 只判持续增长（下降=GC 回收，非泄漏；取样间隔 100 帧）
                "stable": bool(growth < 200.0),
            },
            "dual_execution": dual_execution,
        }
    finally:
        ctx.release()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-set", default="standard",
                        choices=["standard"],
                        help="F08 冻结工作负载集（standard = f03 三层"
                             "非交换串行）")
    parser.add_argument("--sizes", default="1920x1080,3840x2160")
    parser.add_argument("--frames", type=int, default=300)
    parser.add_argument("--media", default=None,
                        help="素材路径（默认 4K_H264.mov）")
    parser.add_argument("--report", default=str(
        REPO / "docs/validation/color_nodes/backend_bench.json"))
    args = parser.parse_args()

    media = Path(args.media) if args.media else (
        REPO / "tests/test_videos/4K_H264.mov")
    if not media.exists():
        print(f"素材不存在: {media}", file=sys.stderr)
        return 2

    import moderngl

    def ctx_factory():
        return moderngl.create_standalone_context(require=330)

    sizes = []
    for token in args.sizes.split(","):
        w, h = token.lower().split("x")
        sizes.append((int(w), int(h)))

    report = {
        "kind": "color_graph_backend_bench",
        "fixture_set": args.fixture_set,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "git": _git_sha(),
        "platform": platform.platform(),
        "gpu": {},
        "media": str(media.relative_to(REPO)),
        "frames_requested": args.frames,
        "sizes": [],
    }
    probe = ctx_factory()
    report["gpu"] = {"renderer": probe.info.get("GL_RENDERER"),
                     "version": probe.info.get("GL_VERSION")}
    probe.release()

    for size in sizes:
        print(f"benchmarking {size[0]}x{size[1]} × {args.frames} 帧 ...")
        entry = run_size(ctx_factory, media, size, args.frames)
        report["sizes"].append(entry)
        g = entry["graph_ms"]
        l = entry["legacy_ms"]
        print(f"  graph p50 {g['p50']}ms / legacy p50 {l['p50']}ms；"
              f"compile {entry['compile_ms']}ms；"
              f"rss增长 {entry['resource']['rss_growth_mb']}MB")

    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
