"""CP 导出速度基准（真实素材，2026-10-14）。

用 tests/test_videos 提取的真实帧序列，经生产导出入口
CompositeRenderOutput.render_image_sequence / render_video 测量：

  A. baseline   ：image_input → output（解码+求值+读回+编码 的底噪）
  B. heavy_chain：blur(24) → grade → glow → degrain → colorlookup → cdl
                  （六连效果链，覆盖 CP 新节点 + 大半径模糊）
  C. EXR(zip)   ：baseline 与 heavy 的 EXR zip 序列
  D. video      ：H.264 CRF23 视频导出
  E. multipart  ：file_output(exr_multipart) 多层写出 vs 单层 EXR

输出 JSON 到 bench_out/cp_audit_20261014/export_speed.json。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

BENCH_DIR = REPO_ROOT / ".cache" / "topos_comp_bench"
OUT_DIR = REPO_ROOT / "bench_out" / "cp_audit_20261014"
W, H = 1920, 1080
FRAMES = list(range(1, 6))  # 5 帧（与提取的 realhd_f1..f5 对应）


def build_graph(chain: list[tuple[str, dict]], out_w=W, out_h=H,
                file_output: dict | None = None) -> dict:
    nodes, connections = [], []
    sources = sorted(BENCH_DIR.glob("realhd_f*.png"))
    src_ids = []
    for i, src in enumerate(sources):
        nid = f"src{i}"
        src_ids.append(nid)
        nodes.append({"id": nid, "type": "image_input",
                      "position": [-500, i * 120],
                      "parameters": {"file_path": str(src)}})
    prev = (src_ids[0], "image")
    for idx, (ntype, params) in enumerate(chain):
        nid = f"fx{idx}"
        nodes.append({"id": nid, "type": ntype, "position": [0, idx * 120],
                      "parameters": dict(params)})
        connections.append({"src_node_id": prev[0], "src_port_name": prev[1],
                            "dst_node_id": nid, "dst_port_name": "image"})
        prev = (nid, "result")
    nodes.append({"id": "out", "type": "output", "position": [500, 0],
                  "parameters": {"format_mode": "custom",
                                 "width": out_w, "height": out_h}})
    connections.append({"src_node_id": prev[0], "src_port_name": prev[1],
                        "dst_node_id": "out", "dst_port_name": "image"})
    if file_output:
        nodes.append({"id": "fo", "type": "file_output",
                      "position": [500, 160],
                      "parameters": dict(file_output)})
        connections.append({"src_node_id": prev[0], "src_port_name": prev[1],
                            "dst_node_id": "fo", "dst_port_name": "image"})
    return {"composite_id": "cp-export-bench", "fps": 24.0,
            "width": out_w, "height": out_h,
            "nodes": nodes, "connections": connections}


HEAVY_CHAIN = [
    ("blur", {"radius": 24.0}),
    ("grade", {"gamma_r": 1.05, "gain_g": 1.1}),
    ("glow", {"intensity": 1.2}),
    ("degrain", {"grain_threshold": 0.03}),
    ("colorlookup", {"red_curve": "[[0.0,0.02],[0.5,0.55],[1.0,0.98]]"}),
    ("cdl", {"slope_r": 1.2, "saturation": 1.1}),
]


def run_image_sequence(renderer, graph, ext, options, out_sub, label, rows):
    out = OUT_DIR / out_sub
    if out.exists():
        shutil.rmtree(out)
    started = time.perf_counter()
    result = renderer.render_image_sequence(
        graph_data=graph, output_dir=str(out), base_name="f",
        ext=ext, start_frame=1, end_frame=len(FRAMES),
        image_options=options or {})
    wall = time.perf_counter() - started
    ok = bool(getattr(result, "success", False))
    n = len(list(out.glob("*.png") if ext == "png" else out.glob("*.exr")))
    rows.append({"case": label, "ok": ok, "frames_written": n,
                 "wall_s": round(wall, 3),
                 "fps": round(len(FRAMES) / wall, 2) if wall > 0 else None,
                 "ms_per_frame": round(wall * 1000 / len(FRAMES), 1),
                 "error": getattr(result, "error", None) if not ok else None})
    print(f"{label:34s} ok={ok} frames={n} {wall:.2f}s "
          f"({len(FRAMES)/wall:.1f} fps)", flush=True)


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("TOPOS_HEADLESS_GL", "1")
    from src.features.composite.render.composite_render_output import (
        CompositeRenderOutput,
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    renderer = CompositeRenderOutput()
    rows: list[dict] = []

    # 热身：吸收 GL 上下文/shader 编码/首帧解码的冷成本，消除用例顺序偏差
    warm = build_graph([])
    renderer.render_image_sequence(
        graph_data=warm, output_dir=str(OUT_DIR / "_warm"),
        base_name="w", ext="png", start_frame=1, end_frame=2,
        image_options={})
    shutil.rmtree(OUT_DIR / "_warm", ignore_errors=True)

    baseline = build_graph([])
    heavy = build_graph(HEAVY_CHAIN)

    # A/B: PNG
    run_image_sequence(renderer, baseline, "png", {}, "png_baseline",
                       "A baseline png", rows)
    run_image_sequence(renderer, heavy, "png", {}, "png_heavy",
                       "B heavy_chain(6fx) png", rows)

    # C: EXR zip
    run_image_sequence(renderer, baseline, "exr", {}, "exr_baseline",
                       "C baseline exr", rows)
    run_image_sequence(renderer, heavy, "exr", {}, "exr_heavy",
                       "C heavy_chain(6fx) exr", rows)

    # D: video h264（24fps CRF23）——10 帧序列
    video_path = OUT_DIR / "bench_h264.mp4"
    started = time.perf_counter()
    result = renderer.render_video(
        graph_data=heavy, output_path=str(video_path), width=W, height=H,
        fps=24.0, start_frame=1, end_frame=10, codec="h264",
        pix_fmt="yuv420p", crf=23)
    wall = time.perf_counter() - started
    ok = bool(getattr(result, "success", False))
    rows.append({"case": "D heavy_chain video h264 crf23",
                 "ok": ok, "frames_written": 10 if ok else 0,
                 "wall_s": round(wall, 3),
                 "fps": round(10 / wall, 2) if wall > 0 else None,
                 "ms_per_frame": round(wall * 100, 1),
                 "error": getattr(result, "error", None) if not ok else None})
    print(f"D heavy_chain video h264        ok={ok} {wall:.2f}s "
          f"({10/wall:.1f} fps)", flush=True)

    # E: 写出器直测 —— render_image_sequence 只求值选定的命名输出
    # （file_output 不在其依赖集内，"一次导一路"为已知行为，CP-075 挂账），
    # 故直接对 heavy 链渲染出的真实帧计时两种 EXR 写出器。
    import numpy as np
    import cv2
    seq_dir = OUT_DIR / "exr_heavy"
    frame = cv2.imread(str(sorted(seq_dir.glob("*.exr"))[0]),
                       cv2.IMREAD_UNCHANGED)
    if frame is None:
        # PNG 版兜底（exr 读回需要 opencv EXR 支持）
        frame = cv2.imread(str(sorted((OUT_DIR / "png_heavy").glob("*.png"))[0]),
                           cv2.IMREAD_UNCHANGED).astype(np.float32) / 255.0
    rgba = frame[:, :, ::-1]  # BGR→RGB
    from src.features.composite.core.exr_multipart_writer import (
        write_exr_multipart,
    )
    for label, fn in (
            ("E writer: multipart(1 part, PIZ)",
             lambda p: write_exr_multipart(p, rgba, {}, compression="PIZ")),
            ("E writer: multipart(beauty+3 AOV, PIZ)",
             lambda p: write_exr_multipart(
                 p, rgba,
                 {"depth": np.full(rgba.shape[:2], 5.0, np.float32),
                  "normal": np.zeros(rgba.shape[:2] + (3,), np.float32),
                  "object_id": np.full(rgba.shape[:2], 17.0, np.float32)},
                 compression="PIZ")),
            ("E writer: cv2 single-part EXR(pxr24)",
             lambda p: cv2.imwrite(
                 p, frame.astype(np.float32),
                 [cv2.IMWRITE_EXR_TYPE, cv2.IMWRITE_EXR_TYPE_FLOAT,
                  cv2.IMWRITE_EXR_COMPRESSION, 5]))):
        out_file = OUT_DIR / ("w_" + label.split(":")[1].strip().split("(")[0]
                              .replace(" ", "_") + ".exr")
        started = time.perf_counter()
        ok = bool(fn(str(out_file)))
        wall = time.perf_counter() - started
        rows.append({"case": label, "ok": ok, "frames_written": int(ok),
                     "wall_s": round(wall, 3),
                     "ms_per_frame": round(wall * 1000, 1)})
        print(f"{label:38s} ok={ok} {wall*1000:.1f} ms/frame", flush=True)

    out_json = OUT_DIR / "export_speed.json"
    out_json.write_text(json.dumps(
        {"kind": "cp_export_speed_real_media", "rows": rows,
         "frames": len(FRAMES), "resolution": f"{W}x{H}"},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwritten {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
