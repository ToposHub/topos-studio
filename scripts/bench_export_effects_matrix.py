"""真实素材导出效果矩阵基准（2026-09-16 第三轮验证）。

目的：验证 §11.6 两项优化在真实素材上的端到端收益——

1. HEVC 8-bit 平面直通（hevc/yuv420p 白名单 + scalar_delivery 泛化）；
2. CPU/GPU 自适应选路（主机可见单层轻量帧跳过 host↔GPU 往返）。

协议（沿用 bench_timeline_export_windows §4.1 精神，规模收窄）：

- 每个 (source, codec_tier, scenario, group) 子进程隔离执行：
  30 帧预热导出（丢弃）→ 240 帧计时导出（wall FPS + 帧时 p50/p95）；
- 输出经 PyAV probe（codec/profile/pix_fmt/帧数）钉死编码档位正确性；
- GPU trail fallback_reason 逐次记录（自适应跳过 / 硬编回退证据）。

场景（真实效果程序化构建，非模拟）：
  identity      单层原速无效果（直通形状）
  resize        输出 1280x720（直通 resize_delivery 形状）
  opacity_fade  36 帧淡入 + 36 帧淡出（标量交付形状）
  grade         ColorStack 调色（exposure/contrast/saturation/gain 非默认）
  blur          gaussian_blur intensity=40 radius=8
  two_layer     双轨同源，顶层 opacity 0.7
  transition    相邻双 clip + cross_dissolve 24 帧（centered）

编码档：h264（yuv420p）/ hevc8（HEVC yuv420p，本轮新增）/
hevc10（HEVC yuv420p10le，历史默认）。

GPU 组（子进程环境隔离）：
  adaptive     生产默认（本轮新增的自适应策略生效）
  forced_gpu   TOPOS_EXPORT_GPU_RENDER=1（用户强制 GPU，自适应让位）
  cpu_render   TOPOS_EXPORT_GPU_RENDER=0（纯 CPU 合成口径）

A/B 对比：同一脚本在基线 worktree（c0ccc208，本轮改动之前）复跑，
差异即本轮净收益；h264 直通为两侧共有的控制组。

用法::

    python scripts/bench_export_effects_matrix.py --side dev
    python scripts/bench_export_effects_matrix.py --side baseline \
        --only h264/hevc8/identity/adaptive

结果落盘：``tests/media/results/export_effects_matrix_<side>_<date>.{json,md}``
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
RESULTS_DIR = ROOT / "tests" / "media" / "results"
TEST_VIDEOS_DIR = ROOT / "tests" / "test_videos"

KIND = "export_effects_matrix_bench"
SCHEMA_VERSION = 1

FRAMES_DEFAULT = 240
WARMUP_DEFAULT = 30
BITRATE_BPS = 20_000_000  # 2K VBR 基准码率（§11.5 口径）

SOURCES: Dict[str, str] = {
    "h264": "2K_h264.mp4",                        # h264 yuv420p 8bit 1080p25
    "hevc8": ".cache/2K_hevc8_gen.mp4",           # hevc yuv420p 8bit（本轮生成）
    "hevc10": "2K_H265.mov",                      # hevc yuv420p10le Main10（负例）
}

GROUP_ENV: Dict[str, Dict[str, str]] = {
    "adaptive": {},
    "forced_gpu": {"TOPOS_EXPORT_GPU_RENDER": "1"},
    "cpu_render": {"TOPOS_EXPORT_GPU_RENDER": "0"},
}


def run_matrix() -> List[Dict[str, str]]:
    """(source, codec_tier, scenario, group) 全矩阵。"""
    runs: List[Dict[str, str]] = []
    # A. 直通/标量交付主张（生产默认组）
    for codec in ("h264", "hevc8", "hevc10"):
        for scenario in ("identity", "resize", "opacity_fade"):
            runs.append({"source": "h264", "codec": codec,
                         "scenario": scenario, "group": "adaptive"})
    for codec in ("h264", "hevc8"):
        for scenario in ("identity", "resize"):
            runs.append({"source": "hevc8", "codec": codec,
                         "scenario": scenario, "group": "adaptive"})
    # 10-bit 源 → 8-bit 输出档：位深门应拒绝直通（诚实负例）
    runs.append({"source": "hevc10", "codec": "hevc8",
                 "scenario": "identity", "group": "adaptive"})
    # B. CPU/GPU 自适应主张（h264 源，效果/多层/转场 × 三组）
    for scenario in ("resize", "grade", "blur", "two_layer", "transition"):
        for group in ("adaptive", "forced_gpu", "cpu_render"):
            runs.append({"source": "h264", "codec": "h264",
                         "scenario": scenario, "group": group})
    # C. 2026-09-17 pass2.5 效果白名单扩展（sharpen/glow，两组对照）
    for scenario in ("sharpen", "glow"):
        for group in ("adaptive", "cpu_render"):
            runs.append({"source": "h264", "codec": "h264",
                         "scenario": scenario, "group": group})
    return runs


# ======================================================================
# 子进程：单配置构建 + 预热 + 计时导出
# ======================================================================

def _source_path(key: str) -> Path:
    rel = SOURCES[key]
    path = TEST_VIDEOS_DIR / rel
    if not path.exists():
        raise FileNotFoundError(f"测试素材缺失: {path}")
    return path


def _probe_source_meta(path: Path) -> Dict[str, Any]:
    import av

    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        context = stream.codec_context
        return {
            "width": int(context.width),
            "height": int(context.height),
            "fps": float(stream.average_rate or 25.0),
            "total_frames": int(stream.frames or 0),
            "codec": str(context.name),
            "pix_fmt": str(context.pix_fmt),
        }


def _probe_output(path: Path) -> Dict[str, Any]:
    import av

    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        context = stream.codec_context
        decoded = 0
        for _frame in container.decode(stream):
            decoded += 1
        return {
            "codec": str(context.name),
            "profile": str(context.profile),
            "pix_fmt": str(context.pix_fmt),
            "frames": decoded,
            "width": int(context.width),
            "height": int(context.height),
        }


def build_scenario(
    scenario: str,
    source_key: str,
    codec_tier: str,
    frames: int,
    bitrate_bps: int,
    output_path: Path,
    source_meta: Dict[str, Any],
) -> Tuple[Any, Any, Any]:
    """程序化构建单场景时间线，返回 (project, sequence, export_config)。"""
    from src.features.edit.core.effect_instance import EffectInstance
    from src.features.edit.core.project import EditProject, MediaAsset
    from src.features.edit.core.timeline_export import (
        ExportCodec,
        ExportSource,
        TimelineExportConfig,
    )
    from src.features.edit.core.timeline_v2 import (
        ClipInstance,
        EditTrack,
        Rational,
        TimelineSequence,
        TransitionInstance,
    )
    from src.shared.project.color_stack import ColorStackData

    src = _source_path(source_key)
    fps = source_meta.get("fps") or 25.0
    fps_rational = Rational(max(1, int(round(fps))), 1)
    width = int(source_meta.get("width") or 1920)
    height = int(source_meta.get("height") or 1080)

    asset = MediaAsset(
        asset_id="bench_src", path=str(src), media_type="video",
        display_name=src.stem,
        metadata={"fps": fps, "total_frames": source_meta.get("total_frames", 0)},
    )
    project = EditProject.create(name="bench-effects-matrix")
    project = project.with_media_asset_added(asset)

    def _clip(clip_id: str, start: int, duration: int, **overrides: Any) -> ClipInstance:
        fields: Dict[str, Any] = dict(
            clip_id=clip_id, asset_id="bench_src", track_id="v1",
            timeline_start=start, timeline_duration=duration,
            source_start=0, source_duration=duration,
        )
        fields.update(overrides)
        return ClipInstance(**fields)

    transitions: Tuple[Any, ...] = ()
    if scenario in ("identity", "resize", "opacity_fade", "grade", "blur",
                    "sharpen", "glow"):
        overrides: Dict[str, Any] = {}
        if scenario == "opacity_fade":
            overrides["fade_in_duration"] = 36
            overrides["fade_out_duration"] = 36
        elif scenario == "grade":
            overrides["color_stack_id"] = "bench_stack"
            project.color_stacks["bench_stack"] = ColorStackData(
                stack_id="bench_stack", asset_id="bench_src",
                grading_params={
                    "exposure": 0.25, "contrast": 0.15, "saturation": 0.2,
                    "gain": (0.05, 0.0, -0.05),
                },
            )
        elif scenario == "blur":
            overrides["effects"] = (EffectInstance(
                effect_type="blur",
                params={"blur_type": "gaussian", "radius": 12.0},
                order=0,
            ),)
        elif scenario == "sharpen":
            overrides["effects"] = (EffectInstance(
                effect_type="sharpen",
                params={"amount": 1.2, "radius": 2.0},
                order=0,
            ),)
        elif scenario == "glow":
            overrides["effects"] = (EffectInstance(
                effect_type="glow",
                params={"radius": 15.0, "intensity": 1.5},
                order=0,
            ),)
        clips = [_clip("bench_c1", 0, frames, **overrides)]
        track_specs = [("v1", clips)]
    elif scenario == "two_layer":
        base = _clip("bench_c1", 0, frames)
        top = _clip("bench_c2", 0, frames, track_id="v2", opacity=0.7)
        track_specs = [("v1", [base]), ("v2", [top])]
    elif scenario == "transition":
        half = frames // 2
        left = _clip("bench_c1", 0, half)
        right = _clip("bench_c2", half, frames - half)
        track_specs = [("v1", [left, right])]
        transitions = (TransitionInstance(
            transition_type="cross_dissolve",
            track_id="v1",
            left_clip_id="bench_c1",
            right_clip_id="bench_c2",
            center_frame=half,
            duration=24,
        ),)
    else:
        raise ValueError(f"未知场景: {scenario}")

    tracks = tuple(
        EditTrack(track_id=track_id, name=track_id.upper(),
                  track_type="video", index=index,
                  clips=tuple(track_clips),
                  transitions=(transitions if track_id == "v1" else ()))
        for index, (track_id, track_clips) in enumerate(track_specs)
    )
    sequence = TimelineSequence(
        timeline_id="bench", name="BenchEffects",
        fps=fps_rational, width=width, height=height,
        video_tracks=tracks,
    )

    codec = {"h264": ExportCodec.H264,
             "hevc8": ExportCodec.HEVC,
             "hevc10": ExportCodec.HEVC}[codec_tier]
    pixel_format = {"h264": "",
                    "hevc8": "yuv420p",
                    "hevc10": "yuv420p10le"}[codec_tier]
    resolution = (1280, 720) if scenario == "resize" else None
    config = TimelineExportConfig(
        source=ExportSource.TIMELINE,
        timeline_id="bench",
        output_path=str(output_path),
        codec=codec,
        pixel_format=pixel_format,
        resolution=resolution,
        quality=bitrate_bps,
        quality_mode="vbr",
        include_audio=False,
    )
    return project, sequence, config


def run_config_main(
    source_key: str,
    codec_tier: str,
    scenario: str,
    group: str,
    frames: int,
    warmup_frames: int,
    emit_json: Path,
    runs: int = 2,
) -> int:
    """子进程入口：预热 + 计时导出 + 输出 probe + trail 证据。"""
    from src.features.edit.core.timeline_export import (
        create_timeline_exporter,
    )

    os.environ["TOPOS_CONFIG_PATH"] = str(
        emit_json.with_name(f"bench_cfg_{scenario}_{codec_tier}_{group}.json"))
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication  # noqa: F401  # 与生产导出同环境

    _qapp = QApplication.instance() or QApplication(sys.argv)

    source_meta = _probe_source_meta(_source_path(source_key))
    available = source_meta.get("total_frames") or 0
    if available and available < frames:
        print(json.dumps({
            "status": "error",
            "error": f"素材帧数不足: 需要 {frames}，实际 {available}",
        }))
        return 1

    workdir = emit_json.parent
    payloads: Dict[str, Any] = {}

    def _export_once(export_frames: int, out_path: Path) -> Dict[str, Any]:
        project, sequence, config = build_scenario(
            scenario, source_key, codec_tier, export_frames,
            BITRATE_BPS, out_path, source_meta)
        samples: List[float] = []

        def _on_progress(_c: int, _t: int, _p: float) -> None:
            samples.append(time.perf_counter())

        exporter = create_timeline_exporter(
            project, sequence, progress_callback=_on_progress)
        start = time.perf_counter()
        outcome = exporter.export_timeline(config)
        wall_s = time.perf_counter() - start
        record: Dict[str, Any] = {
            "frames_requested": export_frames,
            "wall_s": round(wall_s, 4),
            "status": ("ok" if bool(outcome.success) and out_path.exists()
                       else "failed"),
        }
        if record["status"] != "ok":
            record["error_code"] = str(getattr(outcome, "error_code", ""))
            record["cause"] = str(getattr(outcome, "cause", ""))[:500]
            return record
        record["fps"] = round(export_frames / wall_s, 3) if wall_s > 0 else None
        if len(samples) >= 3:
            deltas = [(samples[i] - samples[i - 1]) * 1000.0
                      for i in range(1, len(samples))]
            deltas.sort()
            record["frame_ms_p50"] = round(
                deltas[len(deltas) // 2], 3)
            record["frame_ms_p95"] = round(
                deltas[int(len(deltas) * 0.95)], 3)
        record["hw_fallback_reason"] = str(
            getattr(outcome, "hw_fallback_reason", ""))
        trail = getattr(exporter, "gpu_trail_snapshot", None)
        record["trail_fallback_reason"] = str(
            getattr(trail, "fallback_reason", "") or "")
        return record

    # 预热（丢弃）
    warmup_path = workdir / f"warmup_{scenario}_{codec_tier}_{group}.mp4"
    try:
        warmup = _export_once(warmup_frames, warmup_path)
        payloads["warmup"] = warmup.get("status")
    except Exception as exc:  # noqa: BLE001 — 预热失败不阻断测量
        payloads["warmup"] = f"failed: {type(exc).__name__}: {exc}"
    finally:
        warmup_path.unlink(missing_ok=True)

    # 正式测量（runs 次取中位——本机单次测量噪声可达 4×）
    run_records: List[Dict[str, Any]] = []
    record: Dict[str, Any] = {"status": "error", "error": "not run"}
    record_out: Dict[str, Any] = {}
    for run_index in range(1, runs + 1):
        out_path = workdir / (
            f"measured_{scenario}_{codec_tier}_{group}_r{run_index}.mp4")
        record = {"status": "error", "error": "not run"}
        record_out = {}
        try:
            record = _export_once(frames, out_path)
        finally:
            if out_path.exists():
                try:
                    record_out = _probe_output(out_path)
                except Exception as exc:  # noqa: BLE001
                    record_out = {"probe_error": f"{type(exc).__name__}: {exc}"}
                out_path.unlink(missing_ok=True)
        record["output_probe"] = record_out
        run_records.append(record)
        if record.get("status") != "ok":
            break
    ok_fps = sorted(float(r["fps"]) for r in run_records
                    if r.get("status") == "ok" and r.get("fps") is not None)
    payloads.update(run_records[-1])
    payloads["runs"] = run_records
    payloads["median_fps"] = ok_fps[len(ok_fps) // 2] if ok_fps else None
    payloads["config"] = {
        "source": source_key, "codec_tier": codec_tier,
        "scenario": scenario, "group": group,
        "frames": frames, "warmup": warmup_frames,
        "bitrate_bps": BITRATE_BPS,
    }
    payloads["env"] = {
        key: os.environ.get(key, "")
        for key in ("TOPOS_EXPORT_GPU_RENDER",
                    "TOPOS_DISABLE_WIN_HW_DECODE",
                    "TOPOS_DISABLE_WIN_HW_ENCODE")
    }
    with emit_json.open("w", encoding="utf-8") as handle:
        json.dump(payloads, handle, ensure_ascii=False, indent=1)
    return 0 if payloads.get("status") == "ok" else 1


# ======================================================================
# 父进程：矩阵编排 + 汇总
# ======================================================================

def _run_child(spec: Dict[str, str], frames: int, warmup: int, runs: int,
               workdir: Path, timeout_s: float) -> Dict[str, Any]:
    tag = f"{spec['source']}_{spec['codec']}_{spec['scenario']}_{spec['group']}"
    result_path = workdir / f"child_{tag}.json"
    cmd = [
        sys.executable, str(Path(__file__).resolve()),
        "--run-config", spec["source"], spec["codec"],
        spec["scenario"], spec["group"],
        "--frames", str(frames), "--warmup", str(warmup),
        "--runs", str(runs),
        "--emit-json", str(result_path),
    ]
    env = dict(os.environ)
    env.update(GROUP_ENV.get(spec["group"], {}))
    try:
        subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True,
                       text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": f"timeout (>{timeout_s}s)"}
    if not result_path.exists():
        return {"status": "error", "error": "child produced no result"}
    with result_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def render_markdown(side: str, results: Dict[str, Any]) -> str:
    lines = [
        f"# 导出效果矩阵基准（{side}）",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 协议：每配置 30 帧预热 + 240 帧计时；子进程隔离；20 Mbps VBR",
        "",
        "| source | codec | scenario | group | FPS | 帧时p50(ms) | "
        "输出 probe | trail/回退 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for record in results["runs"]:
        probe = record.get("output_probe") or {}
        probe_text = (
            f"{probe.get('codec')}/{probe.get('profile')}/"
            f"{probe.get('pix_fmt')}/{probe.get('frames')}f"
            if probe and not probe.get("probe_error") else
            str(probe.get("probe_error", "-")))
        lines.append(
            f"| {record['config']['source']} | {record['config']['codec']} "
            f"| {record['config']['scenario']} | {record['config']['group']} "
            f"| {record.get('median_fps') or record.get('fps') or '-'} "
            f"| {record.get('frame_ms_p50', '-')} "
            f"| {probe_text} | {record.get('trail_fallback_reason') or '-'} |")
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--side", default="dev",
                        help="标签（dev/baseline），用于结果文件名")
    parser.add_argument("--frames", type=int, default=FRAMES_DEFAULT)
    parser.add_argument("--warmup", type=int, default=WARMUP_DEFAULT)
    parser.add_argument("--runs", type=int, default=2,
                        help="每配置正式测量次数（取中位 FPS）")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--only", default="",
                        help="只跑匹配子串的配置（如 h264/hevc8/identity）")
    parser.add_argument("--run-config", nargs=4, default=None, metavar=(
        "SOURCE", "CODEC", "SCENARIO", "GROUP"), help="子进程入口")
    parser.add_argument("--emit-json", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.run_config is not None:
        assert args.emit_json is not None
        return run_config_main(
            args.run_config[0], args.run_config[1], args.run_config[2],
            args.run_config[3], args.frames, args.warmup, args.emit_json,
            runs=args.runs)

    workdir = RESULTS_DIR
    workdir.mkdir(parents=True, exist_ok=True)
    specs = [
        spec for spec in run_matrix()
        if not args.only or args.only in (
            f"{spec['source']}/{spec['codec']}/{spec['scenario']}"
            f"/{spec['group']}")
    ]
    records: List[Dict[str, Any]] = []
    for index, spec in enumerate(specs, 1):
        tag = (f"{spec['source']}/{spec['codec']}"
               f"/{spec['scenario']}/{spec['group']}")
        print(f"[{index}/{len(specs)}] {tag} ...", flush=True)
        record = _run_child(spec, args.frames, args.warmup, args.runs,
                            workdir, args.timeout)
        record["config"] = spec
        records.append(record)
        print(f"    -> {record.get('status')} fps={record.get('fps')}",
              flush=True)

    results = {
        "kind": KIND,
        "schema_version": SCHEMA_VERSION,
        "side": args.side,
        "git_rev": subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT),
            capture_output=True, text=True).stdout.strip(),
        "frames": args.frames,
        "warmup": args.warmup,
        "runs": records,
    }
    json_path = workdir / (
        f"export_effects_matrix_{args.side}_"
        f"{time.strftime('%Y-%m-%d')}.json")
    md_path = json_path.with_suffix(".md")
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(results, handle, ensure_ascii=False, indent=1)
    md_path.write_text(render_markdown(args.side, results), encoding="utf-8")
    print(f"结果: {json_path}\n      {md_path}")
    failed = [r for r in records if r.get("status") != "ok"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
