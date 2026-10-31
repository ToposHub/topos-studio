"""合成页面全链路 GPU 覆盖逐节点基准（真实素材）。

对 Composite 页面的每个效果节点，用真实素材帧（4K/1080p 相机素材）
构建 image_input → <效果节点> → output 最小图，经生产入口
CompositeFrameRenderer.render_comp_clip_frame（与 Edit 时间线 comp_clip
预览/导出同一条链路）测量：

  - cold_ms   ：冷加载一帧（GL 上下文 + shader 编译 + 解码上传 + 求值 + 读回）
  - preview_ms：稳态预览帧（quality="preview"，srgb_u8 GPU 读回路径）
  - export_ms ：稳态导出帧（quality="export"，linear_f32 全精度读回）
  - baseline  ：无效果直连（image_input → output）作为对照

同时记录输出校验（非空/非黑/分辨率），把静默失败暴露出来。

用法：
    .venv/Scripts/python.exe scripts/bench_composite_nodes_gpu_coverage.py \
        [--res 4k|hd|both] [--preview-frames N] [--export-frames N] \
        [--node-filter blur,grade] [--json out.json]

素材：$TMP/topos_comp_bench/ 下的真实素材帧（由运行前 ffmpeg 提取）。
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

BENCH_DIR = Path(os.environ.get("TEMP", str(REPO_ROOT / ".cache"))) / "topos_comp_bench"

# ----------------------------------------------------------------------------
# 静态 CPU/GPU 分类（来自架构调研；基准数值用于验证该分类）
# gpu_path: full-GPU fragment/compute shader，无全帧 CPU 读回
# cpu_readback: 求值中含 GPU→CPU 全帧读回 + numpy 处理 + 重新上传
# cpu_raster: CPU 栅格化/模拟后上传
# ----------------------------------------------------------------------------
NODE_CLASSES = {
    # Color
    "grade": "gpu", "color_correct": "gpu", "cs_transform": "gpu",
    "huecorrect": "gpu", "lut": "gpu", "topos_color": "gpu",
    # Filter
    "blur": "gpu", "glow": "gpu", "denoise": "gpu", "sharpen": "gpu",
    "erode_dilate": "gpu", "defocus": "gpu", "edge_detect": "gpu",
    "relight": "gpu", "fog": "gpu", "godrays": "gpu",
    "chromatic_aberration": "gpu", "vignette": "gpu", "lens_flare": "gpu",
    "lens_distortion": "gpu", "deflicker": "cpu_readback",
    # Keyer
    "keyer": "gpu", "chroma_keyer": "gpu", "luma_keyer": "gpu",
    "difference_keyer": "gpu", "light_wrap": "gpu",
    # Channel / Utility
    "shuffle": "gpu", "copy_channel": "gpu", "set_alpha": "gpu",
    "remove_channels": "gpu", "invert": "gpu", "clamp": "gpu",
    "normalize": "gpu", "channel_merge": "gpu", "compare": "gpu",
    # Transform
    "transform": "gpu", "reformat": "gpu", "crop": "gpu", "cornerpin": "gpu",
    # Merge
    "blend": "gpu", "premult": "gpu", "unpremult": "gpu", "matte": "gpu",
    "mask_merge": "gpu", "dissolve": "gpu", "deep_merge": "gpu",
    "switch": "gpu",
    # Mask
    "mask": "gpu", "roto_shape": "gpu", "depth_matte": "gpu",
    # CPU 家族
    "text": "cpu_raster", "paint": "cpu_readback", "particle": "cpu_raster",
    "frame_interpolate": "cpu_readback",
    "ai_remove": "cpu_readback", "ai_generate": "cpu_readback",
    "ai_inpaint": "cpu_readback", "ai_expand": "cpu_readback",
    "ai_gen_expand": "cpu_readback", "ai_denoise": "cpu_readback",
    # 生成器（源节点）
    "noise": "gpu", "ramp": "gpu", "constant": "gpu",
    # CP 批次新增（2026-10-14）
    "keymix": "gpu", "grain": "gpu", "degrain": "gpu",
    "vectorblur": "gpu", "colorlookup": "gpu", "cdl": "gpu",
    "matte_cleanup": "gpu",
}

# 非默认参数覆盖（保证效果实际生效/命中大半径 compute 路径）
PARAM_OVERRIDES: dict[str, dict] = {
    "blur": {"radius": 24.0},
    "erode_dilate": {"size": 20},
    "defocus": {"size": 24.0},
    "transform": {"translate_x": 64.0, "translate_y": -32.0},
    "text": {"text": "Topos 合成性能基准 2026", "font_size": 160},
    "crop": {"x": 200, "y": 150, "width": 3000, "height": 1600},
    "reformat": {"width": 1920, "height": 1080},
    "glow": {"intensity": 1.2},
    "lens_flare": {"intensity": 0.8},
    "godrays": {"intensity": 0.9},
    "particle": {"count": 500},
    "dissolve": {"opacity": 0.5},
    # T-P1-60：定向 fixture——constant 用非默认颜色（平图不再被笼统计过）。
    # keyer 家族的定向源 fixture 见 NODE_FIXTURES（真实帧上没有幕布色，
    # 参数覆盖解决不了源的问题）。
    "constant": {"color_r": 0.8, "color_g": 0.3, "color_b": 0.1},
    # CP 批次新增节点：保证效果实际生效
    "grain": {"intensity": 0.4, "grain_type": "fine", "animate": False},
    "degrain": {"grain_threshold": 0.03, "amount": 1.0},
    "vectorblur": {"shutter": 1.0},
    "cdl": {"slope_r": 1.2, "offset_g": 0.02, "saturation": 1.1},
    "colorlookup": {
        "red_curve": "[[0.0,0.02],[0.5,0.55],[1.0,0.98]]",
        "master_curve": "[[0.0,0.0],[0.3,0.32],[0.7,0.68],[1.0,1.0]]",
    },
    "keymix": {"mask_channel": "luma", "mix": 1.0},
}

# ── T-P1-60 批次 B（W05）：节点定向源 fixture ─────────────────────────────
# 通用真实帧不适用、必须换源的节点在此登记：返回 (替代源列表, 附加节点参数)。
# 替代源可以是 Path 或 (Path, 源节点参数)（如 image_input 的 color_space）。


def _greenscreen_fixture() -> Path:
    """绿幕 fixture：纯绿底 (0,255,0) + 中央白色主体。"""
    path = BENCH_DIR / "fixture_greenscreen.png"
    if not path.exists():
        import numpy as np
        from PIL import Image
        w, h = 1920, 1080
        img = np.zeros((h, w, 3), dtype=np.uint8)
        img[..., 1] = 255
        img[h // 4:h * 3 // 4, w // 4:w * 3 // 4] = 255
        Image.fromarray(img).save(path)
    return path


def _cryptomatte_fixture() -> Path:
    """rgba24 ID fixture：左半 id=17 (r=17)，右半 id=523 (r=11,g=2)。"""
    path = BENCH_DIR / "fixture_cryptomatte_rgba24.png"
    if not path.exists():
        import numpy as np
        from PIL import Image
        w, h = 1920, 1080
        img = np.zeros((h, w, 4), dtype=np.uint8)
        img[..., 3] = 255
        img[:, :w // 2, 0] = 17
        img[:, w // 2:, 0] = 523 % 256
        img[:, w // 2:, 1] = (523 // 256) % 256
        Image.fromarray(img).save(path)
    return path


NODE_FIXTURES: dict = {
    "chroma_keyer": lambda: (
        [_greenscreen_fixture()],
        # 白主体距纯绿的加权距离 ≈ 0.285 → tolerance 0.15/softness 0.05
        # 保证主体 matte=1、幕布 matte=0（方向语义由
        # tests/composite/test_chroma_keyer_gpu.py 钉住）。
        {"screen_color_r": 0.0, "screen_color_g": 1.0, "screen_color_b": 0.0,
         "tolerance": 0.15, "softness": 0.05, "despill": True},
    ),
    "cryptomatte_select": lambda: (
        [(_cryptomatte_fixture(), {"color_space": "linear"})],
        # 选中左半 id=17 → matte 白左半黑右半；color_space=linear 防止
        # srgb→linear 转换破坏 8-bit ID 编码。
        {"id_encoding": "rgba24", "output_mode": "matte",
         "selected_ids": "[17.0]"},
    ),
    # 2026-10-14：mask_merge 是 matte 布尔节点（matte_value=max(alpha,luma)），
    # 不透明照片输入在数学上就是全覆盖 → 全白被判 flat。改喂软 matte fixture。
    "mask_merge": lambda: (
        [_soft_matte_fixture("a"), _soft_matte_fixture("b")],
        {"operation": "multiply"},
    ),
}


def _soft_matte_fixture(tag: str) -> Path:
    """软 matte fixture：左半 alpha 渐变 / 右半 alpha=0，RGB=灰。"""
    path = BENCH_DIR / f"fixture_softmatte_{tag}.png"
    if not path.exists():
        import numpy as np
        from PIL import Image
        w, h = 1920, 1080
        rgba = np.zeros((h, w, 4), dtype=np.uint8)
        rgba[..., :3] = 128
        rgba[:, : w // 2, 3] = np.linspace(0, 255, w // 2)[None, :].astype(np.uint8)
        rgba[:, w // 2:, 3] = (60 if tag == "a" else 200)
        Image.fromarray(rgba).save(path)
    return path


def resolve_node_fixtures(node_type: str, base_sources: list,
                          base_params: dict) -> tuple:
    """有定向 fixture 的节点返回 (fixture 源, 合并参数)，否则原样返回。"""
    factory = NODE_FIXTURES.get(node_type)
    if factory is None:
        return base_sources, base_params
    fx_sources, fx_params = factory()
    merged = dict(base_params)
    merged.update(fx_params)
    return fx_sources, merged

VIDEO_CASES = [
    ("video_frame_4k_h264", "2620吉利调色1.mp4", 8, 16),
    ("video_frame_hd_h264", "260616五菱调色2.mov", 8, 16),
    ("video_frame_4k_prores4444", "1.mov", 4, 10),
]


def _sources(res: str) -> list[Path]:
    if res == "4k":
        paths = sorted(BENCH_DIR.glob("real4k_f*.png"))
    else:
        paths = sorted(BENCH_DIR.glob("realhd_f*.png"))
    if not paths:
        raise SystemExit(f"missing bench material in {BENCH_DIR} (run ffmpeg extraction first)")
    return paths


def _lut_file() -> Path:
    """生成 33^3 恒等 .cube，供 lut 节点使用。"""
    path = BENCH_DIR / "identity_33.cube"
    if not path.exists():
        lines = ['TITLE "bench-identity"', f"LUT_3D_SIZE 33", ""]
        n = 33
        for b in range(n):
            for g in range(n):
                for r in range(n):
                    lines.append(f"{r/(n-1):.6f} {g/(n-1):.6f} {b/(n-1):.6f}")
        path.write_text("\n".join(lines), encoding="ascii")
    return path


def _png_size(path: Path) -> tuple[int, int]:
    """读 PNG IHDR 拿宽高（不解码整图）。"""
    with open(path, "rb") as f:
        head = f.read(26)
    if head[12:16] == b"IHDR":
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    return 1920, 1080


def build_graph(node_types_with_params: list[tuple[str, dict]], sources: list[Path],
                out_size: tuple[int, int] | None = None) -> dict:
    """构建 {sources...} → 效果节点 → output 的 CompositeGraphData dict。

    node_types_with_params: [(node_type, params)]，多个效果节点按序串联；
    输入端口自动布线：每个 image 输入口接到一个独立真实帧源。
    out_size: 输出分辨率（必须显式指定——Output 节点 custom 模式缺省
    时实际输出会退化成 1×1，读回路径就测不到了）。
    """
    from src.features.composite.core.node_engine import NODE_REGISTRY

    nodes: list[dict] = []
    connections: list[dict] = []
    src_ids: list[str] = []
    for i, src in enumerate(sources):
        src_params: dict = {}
        if isinstance(src, tuple):
            src, src_params = src  # (path, 源节点参数)
        nid = f"src{i}"
        src_ids.append(nid)
        nodes.append({
            "id": nid, "type": "image_input", "position": [-400, i * 120],
            "parameters": {"file_path": str(src), **src_params},
        })

    prev_output = None  # (node_id, port)
    for idx, (ntype, params) in enumerate(node_types_with_params):
        cls = NODE_REGISTRY.get(ntype)
        if cls is None:
            raise ValueError(f"node type not registered: {ntype}")
        nid = f"fx{idx}"
        nodes.append({
            "id": nid, "type": ntype, "position": [0, idx * 120],
            "parameters": dict(params),
        })
        probe = cls(node_id="probe")
        # CP-044 后所有效果节点带通用 mask 口；跳过它保持基准语义与
        # 历史数字可比（mask 后处理成本由专项用例覆盖）。
        image_inputs = [p.name for p in probe.inputs
                        if p.data_type == "image" and p.name != "mask"]
        if not image_inputs:
            image_inputs = ["image"]
        out_port = probe.outputs[0].name if probe.outputs else "result"
        if prev_output is None:
            for pi, port in enumerate(image_inputs):
                connections.append({
                    "src_node_id": src_ids[pi % len(src_ids)],
                    "src_port_name": "image",
                    "dst_node_id": nid, "dst_port_name": port,
                })
        else:
            connections.append({
                "src_node_id": prev_output[0], "src_port_name": prev_output[1],
                "dst_node_id": nid, "dst_port_name": image_inputs[0],
            })
            # 多余的 image 输入口接其它真实源（如 blend 的 B / mask）
            for pi, port in enumerate(image_inputs[1:]):
                connections.append({
                    "src_node_id": src_ids[(pi + 1) % len(src_ids)],
                    "src_port_name": "image",
                    "dst_node_id": nid, "dst_port_name": port,
                })
        prev_output = (nid, out_port)

    w, h = out_size or (1920, 1080)
    nodes.append({"id": "out", "type": "output", "position": [400, 0],
                  "parameters": {"format_mode": "custom",
                                 "width": int(w), "height": int(h)}})
    if prev_output is None:
        prev_output = (src_ids[0], "image")
    connections.append({
        "src_node_id": prev_output[0], "src_port_name": prev_output[1],
        "dst_node_id": "out", "dst_port_name": "image",
    })
    return {
        "composite_id": "bench-graph",
        "fps": 25.0,
        "width": int(w),
        "height": int(h),
        "nodes": nodes,
        "connections": connections,
    }


def render_frames(graph_data: dict, quality: str, frames: list[int],
                  warmup: int = 0, fresh: bool = False):
    """渲染一组帧，返回 (per_frame_ms 列表, 校验信息, 错误)。"""
    import numpy as np
    from src.features.composite.render.composite_frame_renderer import (
        CompositeFrameRenderer,
    )

    renderer = CompositeFrameRenderer()
    try:
        timings: list[float] = []
        info: dict = {}
        seq = list(range(-warmup, len(frames)))
        for k in seq:
            # warmup 帧不得为负：deflicker 等时域节点对负帧号会内部报错
            frame = frames[k] if k >= 0 else max(0, frames[0] + k)
            t0 = time.perf_counter()
            img = renderer.render_comp_clip_frame(
                graph_data=graph_data,
                timeline_frame=frame,
                source_frame=frame,
                quality=quality,
            )
            dt = (time.perf_counter() - t0) * 1000.0
            if k < 0:
                continue
            timings.append(dt)
            if img is None:
                # "render returned None" 是 classify 的 render_none 契约
                # 标记，必须保留；结构化诊断附在其上。
                info = {"ok": False, "error": "render returned None"}
                diag = list(getattr(renderer, "last_diagnostics", []) or [])
                if diag:
                    info["diagnostics"] = diag
                return timings, info, None
            arr = np.asarray(img)
            info = {
                "ok": True,
                "shape": list(arr.shape),
                "dtype": str(arr.dtype),
                "mean": round(float(arr.astype("float64").mean()), 4),
                "std": round(float(arr.astype("float64").std()), 4),
            }
            diag = list(getattr(renderer, "last_diagnostics", []) or [])
            if diag:
                info["diagnostics"] = diag
            # T-P1-60 步骤 6：节点级 performance attribution（frame trace）。
            ft = getattr(renderer, "last_frame_trace", None)
            if isinstance(ft, dict):
                info["attribution"] = {
                    k: ft.get(k) for k in (
                        "eval_ms", "readback_ms", "total_ms",
                        "cpu_upload_bytes", "cpu_download_bytes",
                        "cache_hit", "strategy", "backend",
                        "node_count", "readback_format")}
            if info["std"] < 1e-6:
                info["flat"] = True
        return timings, info, None
    except Exception as exc:  # noqa: BLE001
        import traceback
        return [], {"ok": False}, f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=4)}"
    finally:
        try:
            renderer.cleanup()
        except Exception:
            pass
        gc.collect()


def bench_case(name: str, graph_data: dict, preview_frames: int, export_frames: int):
    case = {"name": name}
    # 冷加载：全新渲染器渲染 1 帧（上下文 + shader 编译 + 解码上传 + 读回）
    t0 = time.perf_counter()
    timings, info, err = render_frames(graph_data, "preview", frames=[0])
    case["cold_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
    if err:
        case["error"] = err.splitlines()[0]
        return case
    case["output"] = info
    if not info.get("ok"):
        case["error"] = info.get("error", "unknown")
        return case

    # 稳态预览
    timings, info, err = render_frames(
        graph_data, "preview",
        frames=list(range(1, preview_frames + 1)), warmup=2)
    if err:
        case["error"] = err.splitlines()[0]
        return case
    case["preview_ms"] = round(statistics.median(timings), 1) if timings else None
    case["preview_p90_ms"] = round(sorted(timings)[-1], 1) if timings else None
    # T-P1-60 步骤 6：warm preview attribution（最后一帧的 frame trace）
    if info.get("attribution"):
        case["attribution_preview"] = info["attribution"]

    # 稳态导出
    timings, info, err = render_frames(
        graph_data, "export",
        frames=list(range(1, export_frames + 1)), warmup=1)
    if err:
        case["error"] = err.splitlines()[0]
        return case
    case["export_ms"] = round(statistics.median(timings), 1) if timings else None
    if info.get("attribution"):
        case["attribution_export"] = info["attribution"]
    return case


def bench_video(name: str, video_path: Path, start: int, count: int):
    """video_frame 节点：真实视频逐帧解码 + 求值 + 读回（预览路径）。"""
    from src.features.composite.core.node_engine import NODE_REGISTRY
    assert "video_frame" in NODE_REGISTRY
    sizes = {"2620吉利调色1.mp4": (3840, 2160), "260616五菱调色2.mov": (1920, 1080),
             "1.mov": (3840, 2160)}
    w, h = sizes.get(video_path.name, (1920, 1080))
    graph = {
        "composite_id": "bench-video",
        "fps": 25.0,
        "width": w, "height": h,
        "nodes": [
            {"id": "v", "type": "video_frame", "position": [0, 0],
             "parameters": {"file_path": str(video_path), "cache_frames": 1,
                            "video_width": w, "video_height": h}},
            {"id": "out", "type": "output", "position": [400, 0],
             "parameters": {"format_mode": "custom", "width": w, "height": h}},
        ],
        "connections": [
            {"src_node_id": "v", "src_port_name": "image",
             "dst_node_id": "out", "dst_port_name": "image"},
        ],
    }
    timings, info, err = render_frames(
        graph, "preview", frames=list(range(start, start + count)), warmup=2)
    case = {"name": name, "material": video_path.name}
    if err:
        case["error"] = err.splitlines()[0]
        return case
    case["output"] = info
    case["preview_ms"] = round(statistics.median(timings), 1) if timings else None
    case["preview_max_ms"] = round(max(timings), 1) if timings else None
    return case


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--res", choices=["4k", "hd", "both"], default="both")
    ap.add_argument("--preview-frames", type=int, default=5)
    ap.add_argument("--export-frames", type=int, default=3)
    ap.add_argument("--node-filter", default="")
    ap.add_argument("--skip-video", action="store_true")
    ap.add_argument("--gl-require", type=int, default=0,
                    help="0 = 应用现状（默认 3.3 上下文）；>=330 显式 require，"
                         "用于量化 compute shader 差距")
    ap.add_argument("--json", default=str(BENCH_DIR / "composite_nodes_bench.json"))
    args = ap.parse_args()

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # CompositeExportRenderer 的无头路径：standalone GL 上下文
    # （与 scripts/topos_render.py 相同的开关）
    os.environ.setdefault("TOPOS_HEADLESS_GL", "1")
    if args.gl_require:
        import moderngl
        _orig = moderngl.create_standalone_context
        _req = args.gl_require

        def _ctx_with_require(*a, **k):
            k.setdefault("require", _req)
            return _orig(*a, **k)

        moderngl.create_standalone_context = _ctx_with_require
    import moderngl as _mgl
    try:
        _probe = _mgl.create_standalone_context(require=args.gl_require or None)
        _gl_info = {"version": _probe.info["GL_VERSION"],
                    "renderer": _probe.info["GL_RENDERER"]}
        _probe.release()
    except Exception as exc:  # noqa: BLE001
        _gl_info = {"error": str(exc)}
    print(f"GL probe: {_gl_info}", flush=True)
    from src.features.composite.core.node_engine import NODE_REGISTRY

    lut = _lut_file()
    overrides = dict(PARAM_OVERRIDES)
    overrides.setdefault("lut", {"file_path": str(lut)})

    filter_set = {s.strip() for s in args.node_filter.split(",") if s.strip()} if args.node_filter else None

    resolutions = ["4k", "hd"] if args.res == "both" else [args.res]
    results: dict = {"meta": {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "gl": _gl_info,
        "gl_require": args.gl_require,
        "preview_frames": args.preview_frames,
        "export_frames": args.export_frames,
    }, "cases": [], "video": [], "baseline": {}}

    for res in resolutions:
        sources = _sources(res)
        out_size = _png_size(sources[0])
        print(f"\n=== [{res}] 素材: {sources[0].name} x{len(sources)} 输出 {out_size[0]}x{out_size[1]} ===", flush=True)

        # 基线：无效果直连
        base_graph = build_graph([], sources, out_size)
        base = bench_case(f"baseline_{res}", base_graph,
                          args.preview_frames, args.export_frames)
        results["baseline"][res] = base
        print(f"baseline: cold={base.get('cold_ms')}ms "
              f"preview={base.get('preview_ms')}ms export={base.get('export_ms')}ms "
              f"{'ERR:' + base.get('error', '') if base.get('error') else ''}", flush=True)

        for ntype, cls_kind in NODE_CLASSES.items():
            if ntype not in NODE_REGISTRY:
                results["cases"].append({"name": ntype, "res": res, "error": "not-registered"})
                continue
            if filter_set and ntype not in filter_set:
                continue
            params = overrides.get(ntype, {})
            fx_sources, params = resolve_node_fixtures(ntype, sources, params)
            label = f"{ntype}@{res}"
            t0 = time.perf_counter()
            try:
                graph = build_graph([(ntype, params)], fx_sources, out_size)
                case = bench_case(label, graph, args.preview_frames, args.export_frames)
            except Exception as exc:  # noqa: BLE001
                case = {"name": label, "error": f"build: {exc}"}
            case.update({"res": res, "node_type": ntype, "exec_class": cls_kind})
            case["total_wall_s"] = round(time.perf_counter() - t0, 2)
            results["cases"].append(case)
            flag = " !ERR " + case.get("error", "")[:60] if case.get("error") else ""
            flat = " !FLAT" if case.get("output", {}).get("flat") else ""
            print(f"{label:34s} cold={case.get('cold_ms', -1):>8} "
                  f"preview={case.get('preview_ms', -1):>7} export={case.get('export_ms', -1):>7}"
                  f" [{cls_kind}]{flag}{flat}", flush=True)

    if not args.skip_video:
        # QA-03：视频素材目录不再指向某台 Windows 桌面——环境变量
        # TC_BENCH_VIDEO_DIR 或本 checkout 的 tests/test_videos。
        import os as _os
        _video_dir = _os.environ.get("TC_BENCH_VIDEO_DIR")
        src_dir = (Path(_video_dir) if _video_dir else Path(
            _os.path.abspath(_os.path.join(
                _os.path.dirname(__file__), "..", "tests", "test_videos"))))
        print("\n=== video_frame 真实视频解码（Windows 软解路径）===", flush=True)
        for name, fname, start, count in VIDEO_CASES:
            vp = src_dir / fname
            if not vp.exists():
                results["video"].append({"name": name, "error": "material missing"})
                continue
            case = bench_video(name, vp, start, count)
            results["video"].append(case)
            print(f"{name:28s} preview={case.get('preview_ms', -1):>8}ms "
                  f"max={case.get('preview_max_ms', -1):>8}ms "
                  f"{'ERR:' + case.get('error', '') if case.get('error') else ''}", flush=True)

    out = Path(args.json)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nresults -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def classify_node_result(node_type: str, info: dict, error: str | None) -> str:
    """T-P1-60 步骤 3：六态可解释状态码。

    passed_nonflat / passed_flat_by_contract / blocked_missing_resource /
    blocked_missing_model / unsupported_backend / failed。
    ``None`` 与全平不再笼统记 ok。
    """
    from src.features.composite.core.node_capabilities import (
        get_node_contract_class,
    )

    contract = get_node_contract_class(node_type)
    # T-P1-60：evaluator 级结构化诊断优先（如 MODEL_UNAVAILABLE），
    # 不再依赖 None/报错文本启发式。
    for entry in (info or {}).get("diagnostics") or []:
        code = str((entry or {}).get("code", "")).upper()
        if code == "MODEL_UNAVAILABLE":
            return "blocked_missing_model"
    render_none = bool(error) and error.strip() == "render returned None"
    if error:
        if render_none:
            # 单节点 harness 的"无输出"：按契约解释（COMP-P1-033~036）
            if contract == "ai_model":
                return "blocked_missing_model"   # 缺模型包（无解释 None 的根因）
            if contract in ("3d_file_output", "source", "generator"):
                return "blocked_missing_resource"  # 缺 3D 场景/媒体 fixture 接线
        lowered = error.lower()
        if contract == "ai_model" and (
                "model" in lowered or "pack" in lowered):
            return "blocked_missing_model"
        if any(marker in lowered for marker in (
                "file", "not found", "missing", "no such",
                "not registered")):
            # 含 DEFERRED_NODE_TYPES 延迟注册节点（standalone harness 未
            # 加载 feature 模块）——缺资源而非节点缺陷
            return "blocked_missing_resource"
        return "failed"
    if not info or not info.get("ok"):
        if contract == "ai_model":
            # render None：占位 AI 节点缺模型包（COMP-P1-034）
            return "blocked_missing_model"
        return "failed"
    if info.get("flat"):
        if contract in ("source", "generator"):
            return "passed_flat_by_contract"
        if contract == "3d_file_output":
            return "passed_flat_by_contract"
        return "failed"
    return "passed_nonflat"
