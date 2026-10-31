#!/usr/bin/env python3
"""TOPOS 422Proxy end-to-end anchor matrix.

This benchmark is deliberately stricter than a feature smoke test.  Every
scenario uses the same real TOPOS 422Proxy source, frame range, output codec,
warmup policy, and isolated child process.  The report separates:

* native decode-only;
* native TOPOS encode-only (one real decoded frame repeated);
* direct decode -> TOPOS encode (the codec-only production anchor);
* TimelineExporter identity (the no-effect end-to-end anchor);
* each representative effect/composite scenario against that anchor.

The effect cost is reported as ``scenario_total - identity_total``.  A GPU
label is never treated as proof: the exporter trail and fallback reason are
recorded verbatim, so a forced-GPU run that falls back to CPU remains visible.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tests" / "test_videos" / "2K_topos_proxy.mov"
OUT_DIR = ROOT / "bench_out" / "topos_anchor_matrix_20260917"
LIB = ROOT / "native" / "topos_codec" / "build" / "release" / "topos_codec.0.1.0.dylib"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_FRAMES = 48
DEFAULT_WARMUP = 8
DEFAULT_RUNS = 1
BITRATE_BPS = 20_000_000
SCENARIOS = ("identity", "grade", "blur", "sharpen", "glow", "two_layer", "transition")
GROUP_ENV = {
    "cpu_render": {"TOPOS_EXPORT_GPU_RENDER": "0"},
    "forced_gpu": {"TOPOS_EXPORT_GPU_RENDER": "1"},
}


def _percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * p))))
    return float(ordered[index])


def _source_meta() -> dict[str, Any]:
    from src.shared.media.topos_source import ToposMediaSource

    source = ToposMediaSource(str(SOURCE), decode_role="export")
    source.open()
    try:
        meta = source.probe()
        return {
            "width": int(meta.width),
            "height": int(meta.height),
            "fps": float(meta.fps.to_fps()),
            "fps_rational": [int(meta.fps.num), int(meta.fps.den)],
            "total_frames": int(source.total_frames),
            "pixel_format": str(meta.pixel_format),
            "bit_depth": int(meta.bit_depth),
            "diagnostics": dict(source.diagnostics),
        }
    finally:
        source.close()


def _native_decode_stage_stats(codec: Any) -> dict[str, Any]:
    try:
        stats = codec.decode_stage_stats_get()
        names = (
            "scan_ns", "crc_ns", "entropy_ns", "dequant_idct_ns",
            "output_ns", "alloc_ns", "blocks", "nonzero_ac",
            "dc_only_blocks", "slices", "packet_bytes_read",
            "segments_parsed", "segments_skipped", "entropy_symbols",
            "coefficients_skipped", "idct_samples", "upload_bytes",
            "frames", "wall_ns", "p50_ns", "p95_ns", "p99_ns",
        )
        return {name: int(getattr(stats, name)) for name in names}
    except Exception as exc:  # old native libraries may not expose stats
        return {"unavailable": f"{type(exc).__name__}: {exc}"}


def _native_encode_stage_stats(codec: Any) -> dict[str, Any]:
    try:
        stats = codec.encode_stage_stats_get()
        names = (
            "pad_ns", "fill_ns", "entropy_ns", "crc_ns", "copy_ns",
            "asm_ns", "slices", "blocks", "dct_ns", "probe_fill_ns",
            "probe_wall_ns", "final_wall_ns", "check_wall_ns",
        )
        return {name: int(getattr(stats, name)) for name in names}
    except Exception as exc:
        return {"unavailable": f"{type(exc).__name__}: {exc}"}


def run_decode_only(frames: int, warmup: int) -> dict[str, Any]:
    """Measure the real native ToposMediaSource read path only."""
    from src.shared.media.topos_source import ToposMediaSource
    from src.shared.codec.topos_binding import ToposCodec

    codec = ToposCodec()
    codec.decode_stage_stats_reset()
    source = ToposMediaSource(str(SOURCE), decode_role="export")
    source.open()
    try:
        for index in range(min(warmup, source.total_frames)):
            source.read_frame(index)
        samples: list[float] = []
        started = time.perf_counter()
        for index in range(frames):
            tick = time.perf_counter()
            source.read_frame(index)
            samples.append((time.perf_counter() - tick) * 1000.0)
        wall_s = time.perf_counter() - started
        return {
            "status": "ok",
            "frames": frames,
            "wall_s": wall_s,
            "fps": frames / wall_s if wall_s else 0.0,
            "frame_ms_p50": statistics.median(samples),
            "frame_ms_p95": _percentile(samples, 0.95),
            "stage": _native_decode_stage_stats(codec),
            "diagnostics": dict(source.diagnostics),
        }
    finally:
        source.close()


def _topos_encoder_config(meta: dict[str, Any], profile: str = "proxy") -> Any:
    from src.shared.export.video_encoder import FFmpegEncoderConfig
    from src.shared.media.metadata import Rational

    return FFmpegEncoderConfig(
        codec="topos",
        width=int(meta["width"]),
        height=int(meta["height"]),
        fps=Rational(int(meta["fps_rational"][0]), int(meta["fps_rational"][1])),
        pix_fmt="yuv422p10le",
        profile=profile,
        crf=20,
        threads=0,
        pipelined_conversion=False,
        color_metadata=None,
    )


def run_encode_only(meta: dict[str, Any], frames: int, warmup: int,
                    output: Path) -> dict[str, Any]:
    """Measure Topos encoding with one real source frame reused.

    Reusing one decoded frame isolates encoder work without retaining 48 full
    1080p frames in memory.  The direct decode->encode anchor below remains
    the production-shaped reference for content-varying end-to-end cost.
    """
    from src.shared.codec.topos_binding import ToposCodec
    from src.shared.export.topos_encoder import ToposVideoEncoder
    from src.shared.media.topos_source import ToposMediaSource

    source = ToposMediaSource(str(SOURCE), decode_role="export")
    source.open()
    encoder = ToposVideoEncoder(
        str(output), _topos_encoder_config(meta),
        slice_threads=min(16, os.cpu_count() or 1),
    )
    try:
        frame = source.read_frame(0)
        encoder.open()
        for _ in range(warmup):
            encoder.encode_frame(frame)
        codec = ToposCodec()
        codec.encode_stage_stats_reset()
        samples: list[float] = []
        started = time.perf_counter()
        for _ in range(frames):
            tick = time.perf_counter()
            encoder.encode_frame(frame)
            samples.append((time.perf_counter() - tick) * 1000.0)
        encoder.close()
        wall_s = time.perf_counter() - started
        return {
            "status": "ok" if output.exists() else "failed",
            "frames": frames,
            "wall_s": wall_s,
            "fps": frames / wall_s if wall_s else 0.0,
            "frame_ms_p50": statistics.median(samples),
            "frame_ms_p95": _percentile(samples, 0.95),
            "stage": _native_encode_stage_stats(codec),
            "output_bytes": output.stat().st_size if output.exists() else 0,
        }
    finally:
        source.close()
        if encoder.is_open:
            try:
                encoder.abort()
            except Exception:
                pass
        output.unlink(missing_ok=True)


def run_decode_encode(meta: dict[str, Any], frames: int, warmup: int,
                      output: Path) -> dict[str, Any]:
    """Measure native decode -> Topos encode with per-stage wall samples."""
    from src.shared.codec.topos_binding import ToposCodec
    from src.shared.export.topos_encoder import ToposVideoEncoder
    from src.shared.media.topos_source import ToposMediaSource

    source = ToposMediaSource(str(SOURCE), decode_role="export")
    source.open()
    encoder = ToposVideoEncoder(
        str(output), _topos_encoder_config(meta),
        slice_threads=min(16, os.cpu_count() or 1),
    )
    try:
        encoder.open()
        for index in range(min(warmup, source.total_frames)):
            encoder.encode_frame(source.read_frame(index))
        codec = ToposCodec()
        codec.decode_stage_stats_reset()
        codec.encode_stage_stats_reset()
        decode_ms: list[float] = []
        encode_ms: list[float] = []
        started = time.perf_counter()
        for index in range(frames):
            tick = time.perf_counter()
            frame = source.read_frame(index)
            decode_ms.append((time.perf_counter() - tick) * 1000.0)
            tick = time.perf_counter()
            encoder.encode_frame(frame)
            encode_ms.append((time.perf_counter() - tick) * 1000.0)
        encoder.close()
        wall_s = time.perf_counter() - started
        return {
            "status": "ok" if output.exists() else "failed",
            "frames": frames,
            "wall_s": wall_s,
            "fps": frames / wall_s if wall_s else 0.0,
            "decode_ms_p50": statistics.median(decode_ms),
            "decode_ms_p95": _percentile(decode_ms, 0.95),
            "encode_ms_p50": statistics.median(encode_ms),
            "encode_ms_p95": _percentile(encode_ms, 0.95),
            "decode_stage": _native_decode_stage_stats(codec),
            "encode_stage": _native_encode_stage_stats(codec),
            "diagnostics": dict(source.diagnostics),
            "output_bytes": output.stat().st_size if output.exists() else 0,
        }
    finally:
        source.close()
        if encoder.is_open:
            try:
                encoder.abort()
            except Exception:
                pass
        output.unlink(missing_ok=True)


def _build_timeline(scenario: str, frames: int, output: Path, meta: dict[str, Any],
                    output_codec: str) -> tuple[Any, Any, Any]:
    """Reuse the production matrix scenario builder with a TOPOS source."""
    import scripts.bench_export_effects_matrix as matrix
    from src.features.edit.core.timeline_export import ExportCodec

    matrix.SOURCES["topos"] = SOURCE.name
    project, sequence, config = matrix.build_scenario(
        scenario, "topos", "h264", frames, BITRATE_BPS, output, meta
    )
    if output_codec == "topos_proxy":
        config = replace(
            config,
            codec=ExportCodec.TOPOS_PROXY,
            pixel_format="yuv422p10le",
            quality=20,
            quality_mode="crf",
        )
    elif output_codec == "h264":
        config = replace(config, codec=ExportCodec.H264, pixel_format="yuv420p")
    else:
        raise ValueError(f"unknown output codec {output_codec}")
    config = replace(config, include_audio=False, burn_subtitles=False)
    return project, sequence, config


def run_timeline(scenario: str, group: str, output_codec: str, frames: int,
                 warmup: int, runs: int, output: Path, meta: dict[str, Any]) -> dict[str, Any]:
    from src.features.edit.core.timeline_export import create_timeline_exporter
    from src.shared.codec.topos_binding import ToposCodec

    def once(count: int, measured: bool) -> dict[str, Any]:
        project, sequence, config = _build_timeline(scenario, count, output, meta, output_codec)
        codec = ToposCodec()
        codec.decode_stage_stats_reset()
        codec.encode_stage_stats_reset()
        progress_times: list[float] = []
        exporter = create_timeline_exporter(
            project, sequence,
            progress_callback=lambda *_args: progress_times.append(time.perf_counter()),
        )
        started = time.perf_counter()
        outcome = exporter.export_timeline(config)
        wall_s = time.perf_counter() - started
        trail = getattr(exporter, "gpu_trail_snapshot", None)
        record: dict[str, Any] = {
            "status": "ok" if bool(outcome) and output.exists() else "failed",
            "success": bool(outcome),
            "wall_s": wall_s,
            "fps": count / wall_s if wall_s else 0.0,
            "hw_fallback_reason": str(getattr(outcome, "hw_fallback_reason", "") or ""),
            "trail_fallback_reason": str(getattr(trail, "fallback_reason", "") or ""),
            "trail": trail.to_dict() if trail is not None else None,
            "decode_stage": _native_decode_stage_stats(codec),
            "encode_stage": _native_encode_stage_stats(codec),
            "progress_samples": len(progress_times),
        }
        if output.exists():
            record["output_bytes"] = output.stat().st_size
        output.unlink(missing_ok=True)
        if record["status"] != "ok":
            record["error_code"] = str(getattr(outcome, "error_code", ""))
            record["cause"] = str(getattr(outcome, "cause", ""))[:1000]
        return record

    warmup_record: dict[str, Any]
    try:
        warmup_record = once(warmup, measured=False)
    except Exception as exc:  # keep the matrix recordable
        warmup_record = {"status": "exception", "exception": f"{type(exc).__name__}: {exc}"}

    runs_out: list[dict[str, Any]] = []
    for _ in range(runs):
        try:
            record = once(frames, measured=True)
        except Exception as exc:
            record = {"status": "exception", "exception": f"{type(exc).__name__}: {exc}"}
        runs_out.append(record)
        if record.get("status") != "ok":
            break
    fps_values = [float(row["fps"]) for row in runs_out if row.get("status") == "ok"]
    result: dict[str, Any] = {
        "status": "ok" if fps_values else "failed",
        "scenario": scenario,
        "group": group,
        "output_codec": output_codec,
        "frames": frames,
        "warmup": warmup,
        "warmup_result": warmup_record,
        "runs": runs_out,
        "median_fps": statistics.median(fps_values) if fps_values else None,
    }
    if runs_out:
        result.update({key: runs_out[-1].get(key) for key in (
            "wall_s", "fps", "output_bytes", "hw_fallback_reason",
            "trail_fallback_reason", "trail", "decode_stage", "encode_stage",
            "error_code", "cause", "exception",
        ) if key in runs_out[-1]})
    return result


def _child(args: argparse.Namespace) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("TOPOS_CODEC_PROFILE", "1")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    meta = _source_meta()
    if args.kind == "decode_only":
        result = run_decode_only(args.frames, args.warmup)
    elif args.kind == "encode_only":
        result = run_encode_only(meta, args.frames, args.warmup, output)
    elif args.kind == "decode_encode":
        result = run_decode_encode(meta, args.frames, args.warmup, output)
    elif args.kind == "timeline":
        result = run_timeline(args.scenario, args.group, args.output_codec,
                              args.frames, args.warmup, args.runs, output, meta)
    else:
        raise ValueError(args.kind)
    result["kind"] = args.kind
    result["source"] = str(SOURCE)
    result["meta"] = meta
    Path(args.emit_json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "ok" else 1


def _run_child(spec: dict[str, Any], workdir: Path, timeout: float) -> dict[str, Any]:
    result_path = workdir / ("child_" + "_".join(str(spec.get(k, "")) for k in (
        "kind", "scenario", "group", "output_codec")) + ".json")
    output_path = workdir / (result_path.stem + ".mov")
    cmd = [sys.executable, str(Path(__file__).resolve()), "--child",
           "--kind", spec["kind"], "--frames", str(spec["frames"]),
           "--warmup", str(spec["warmup"]), "--runs", str(spec.get("runs", 1)),
           "--scenario", spec.get("scenario", "identity"),
           "--group", spec.get("group", "cpu_render"),
           "--output-codec", spec.get("output_codec", "topos_proxy"),
           "--output", str(output_path), "--emit-json", str(result_path)]
    env = dict(os.environ)
    env.update(GROUP_ENV.get(spec.get("group", ""), {}))
    env.setdefault("TOPOS_CODEC_LIB", str(LIB))
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True,
                              text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "spec": spec, "timeout_s": timeout}
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))
    else:
        result = {"status": "child_no_result", "stdout_tail": proc.stdout[-2000:],
                  "stderr_tail": proc.stderr[-4000:]}
    result["spec"] = spec
    if proc.returncode != 0:
        result.setdefault("child_returncode", proc.returncode)
        result.setdefault("child_stderr_tail", proc.stderr[-4000:])
    return result


def _timeline_specs(frames: int, warmup: int, output_codec: str) -> list[dict[str, Any]]:
    return [
        {"kind": "timeline", "scenario": scenario, "group": group,
         "output_codec": output_codec, "frames": frames, "warmup": warmup, "runs": DEFAULT_RUNS}
        for scenario in SCENARIOS for group in GROUP_ENV
    ]


def _delta(row: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    total = row.get("wall_s")
    base = baseline.get("wall_s")
    if not isinstance(total, (int, float)) or not isinstance(base, (int, float)) or base <= 0:
        return {"extra_ms": None, "overhead_ratio": None, "fps_ratio": None}
    return {
        "extra_ms": (float(total) - float(base)) * 1000.0,
        "overhead_ratio": (float(total) - float(base)) / float(base),
        "fps_ratio": (float(row.get("fps", 0.0)) / float(baseline.get("fps", 1.0))),
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# TOPOS 422Proxy 全链路速度锚点对比",
        "",
        "## 测试口径",
        "",
        "同一真实素材、同一分辨率、同一帧数、同一预热与进程隔离策略。",
        "效果额外成本 = 效果总耗时 − 同组 identity 无效果总耗时；",
        "不是把“强制 GPU”标签当成 GPU 证据，GPU 是否生效以 trail/fallback 为准。",
        "",
        "| 层级 | FPS | p50/总耗时 | 说明 |",
        "|---|---:|---:|---|",
    ]
    for key in ("decode_only", "encode_only", "decode_encode"):
        row = report["anchors"].get(key) or {}
        if key == "decode_encode":
            desc = "真实逐帧 TOPOS 解码 → TOPOS 编码"
            time_text = f"{row.get('wall_s', 0):.3f}s"
        else:
            desc = {"decode_only": "原生 TOPOS 解码", "encode_only": "原生 TOPOS 编码（重复一张真实帧隔离编码器）"}[key]
            time_text = f"{row.get('frame_ms_p50', 0):.2f}ms"
        lines.append(f"| {desc} | {row.get('fps', '-'):.2f} | {time_text} | {row.get('status')} |")
    lines.extend([
        "",
        "## TimelineExporter 锚点与效果",
        "",
        "| 输出 | 场景 | 选路 | FPS | identity 基线 | 额外耗时 ms | 总耗时倍率 | GPU trail/fallback | 状态 |",
        "|---|---|---|---:|---:|---:|---:|---|---|",
    ])
    for row in report["timeline"]:
        base = report["identity_baselines"].get(f"{row.get('output_codec')}:{row.get('group')}") or {}
        d = _delta(row, base)
        overhead_text = (
            f"{1 + float(d['overhead_ratio']):.3f}"
            if d.get("overhead_ratio") is not None else "-"
        )
        lines.append(
            f"| {row.get('output_codec')} | {row.get('scenario')} | {row.get('group')} "
            f"| {row.get('fps', '-') if row.get('fps') is not None else '-'} "
            f"| {base.get('fps', '-')} | {d.get('extra_ms', '-') if d.get('extra_ms') is not None else '-'} "
            f"| {overhead_text} "
            f"| {row.get('trail_fallback_reason') or 'full_gpu/unknown'} | {row.get('status')} |"
        )
    lines.extend([
        "",
        "## 解释与 GPU 优化门槛",
        "",
        "- `identity` 是生产型无效果完整导出锚点，不等同于单纯解码速度；它包含时间线调度、帧交付、合成、编码和封装。",
        "- `decode_encode` 是 TOPOS codec-only 锚点，用来区分素材解码/编码成本与编辑器效果链成本。",
        "- `trail_fallback_reason=cpu_encoder_surface_path` 表示当前链路仍有 CPU 可见编码面；即使 GPU renderer 被请求，也不能算全链路 GPU。",
        "- 下一轮优化必须优先让 identity 与无效果路径做到原生平面/Metal surface → GPU render → 硬件编码面，效果路径再逐项迁移。",
        "",
    ])
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--output-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--kind", choices=("decode_only", "encode_only", "decode_encode", "timeline"))
    parser.add_argument("--scenario", default="identity")
    parser.add_argument("--group", default="cpu_render")
    parser.add_argument("--output-codec", default="topos_proxy")
    parser.add_argument("--output", default="")
    parser.add_argument("--emit-json", default="")
    args = parser.parse_args(argv)
    if args.child:
        if not args.kind or not args.emit_json:
            parser.error("--child requires --kind and --emit-json")
        return _child(args)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.setdefault("TOPOS_CODEC_LIB", str(LIB))
    # Run anchors in their own children as well, so native profile counters are
    # not contaminated by the later TimelineExporter runs.
    anchor_specs = [
        {"kind": "decode_only", "frames": args.frames, "warmup": args.warmup},
        {"kind": "encode_only", "frames": args.frames, "warmup": args.warmup},
        {"kind": "decode_encode", "frames": args.frames, "warmup": args.warmup},
    ]
    anchors: dict[str, Any] = {}
    for spec in anchor_specs:
        spec["runs"] = args.runs
        result = _run_child(spec, args.output_dir, args.timeout)
        anchors[spec["kind"]] = result
        print(f"anchor {spec['kind']}: {result.get('status')} fps={result.get('fps')}", flush=True)

    timeline: list[dict[str, Any]] = []
    for index, spec in enumerate(_timeline_specs(args.frames, args.warmup, "topos_proxy"), 1):
        result = _run_child(spec, args.output_dir, args.timeout)
        timeline.append(result)
        print(
            f"[{index}/{len(_timeline_specs(args.frames, args.warmup, 'topos_proxy'))}] "
            f"{spec['scenario']}/{spec['group']}: {result.get('status')} fps={result.get('fps')}",
            flush=True,
        )

    identity_baselines = {
        f"{row.get('output_codec')}:{row.get('group')}": row
        for row in timeline if row.get("scenario") == "identity" and row.get("status") == "ok"
    }
    for row in timeline:
        base = identity_baselines.get(f"{row.get('output_codec')}:{row.get('group')}")
        if base is not None:
            row["baseline_delta"] = _delta(row, base)
    report: dict[str, Any] = {
        "kind": "topos_anchor_matrix",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": str(SOURCE),
        "native_codec_lib": str(LIB),
        "protocol": {
            "frames": args.frames,
            "warmup": args.warmup,
            "runs": args.runs,
            "output_codec": "topos_proxy",
            "scenarios": list(SCENARIOS),
            "groups": list(GROUP_ENV),
            "effect_extra_formula": "effect_total - identity_total",
            "effect_overhead_formula": "(effect_total - identity_total) / identity_total",
        },
        "anchors": anchors,
        "timeline": timeline,
        "identity_baselines": identity_baselines,
    }
    json_path = args.output_dir / "topos_anchor_matrix.json"
    md_path = args.output_dir / "topos_anchor_matrix.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(f"results: {json_path}\n         {md_path}")
    return 0 if all(row.get("status") == "ok" for row in timeline) else 1


if __name__ == "__main__":
    raise SystemExit(main())
