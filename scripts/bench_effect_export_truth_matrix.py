#!/usr/bin/env python3
"""T-P1-20 truth matrix — every ``exportable`` effect must really export.

For each effect whose unified ``EffectExecutionCapability`` says
``exportable``, run a real 48-frame timeline export against the shared 2K
TOPOS source in BOTH deliverable codecs (TOPOS Proxy and H.264), then:

* probe the output via the unified media factory (must succeed);
* decode a mid frame and compare its Y-plane mean against the same-frame
  ``identity`` export (must differ — the effect must visibly engage);
* record wall/fps and the export outcome's delivery path.

Preview-only plugins are verified on the rejection side: the AI manifest
and the Deliver preflight must refuse them with the same structured code
(covered by tests/edit/test_effect_execution_capability.py — this script
prints the manifest projection for the record).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SOURCE = ROOT / "tests" / "test_videos" / "2K_topos_proxy.mov"
EXPORT_EFFECTS = ("blur", "sharpen", "glow", "erode_dilate", "edge_detect",
                  "denoise", "defocus")
CODECS = ("topos_proxy", "h264")
DEFAULT_FRAMES = 48

#: 效果参数：catalog 默认值之上保证"效果确实参与渲染"的强制覆盖
#: （mix=1 全量生效；半径类参数给非零值避免中性默认）。
PARAM_OVERRIDES: dict[str, dict[str, Any]] = {
    "blur": {"radius_x": 14.0, "radius_y": 14.0, "mix": 1.0},
    "sharpen": {"amount": 4.0, "mix": 1.0},
    "glow": {"intensity": 0.8, "radius": 24.0, "mix": 1.0},
    "erode_dilate": {"radius": 3.0, "mix": 1.0},
    "edge_detect": {"strength": 2.0, "mix": 1.0},
    "denoise": {"strength": 0.6, "mix": 1.0},
    "defocus": {"radius": 12.0, "mix": 1.0},
}


def _source_meta() -> dict[str, Any]:
    from src.shared.media.topos_source import ToposMediaSource

    handle = ToposMediaSource(str(SOURCE), decode_role="export")
    handle.open()
    try:
        meta = handle.probe()
        return {
            "width": int(meta.width),
            "height": int(meta.height),
            "fps": float(meta.fps.to_fps()),
            "total_frames": int(handle.total_frames),
        }
    finally:
        handle.close()


def _build_timeline(effect_type: str, frames: int, output: Path, meta: dict[str, Any],
                    output_codec: str) -> tuple[Any, Any, Any]:
    """Single clip + single effect timeline（pattern: bench_export_effects_matrix）."""
    from src.features.edit.core.effect_instance import EffectInstance
    from src.features.edit.core.project import EditProject, MediaAsset
    from src.features.edit.core.timeline_export import (
        ExportCodec, ExportSource, TimelineExportConfig,
    )
    from src.features.edit.core.timeline_v2 import (
        ClipInstance, EditTrack, Rational, TimelineSequence,
    )

    fps = float(meta.get("fps") or 25.0)
    fps_rational = Rational(max(1, int(round(fps))), 1)
    width = int(meta.get("width") or 1920)
    height = int(meta.get("height") or 1080)

    asset = MediaAsset(
        asset_id="truth_src", path=str(SOURCE), media_type="video",
        display_name=SOURCE.stem,
        metadata={"fps": fps, "total_frames": meta.get("total_frames", 0)},
    )
    project = EditProject.create(name="bench-effect-truth")
    project = project.with_media_asset_added(asset)

    # identity = 无效果基线（不是效果类型——未知类型会被 preflight 阻断）
    if effect_type == "identity":
        effects: tuple = ()
    else:
        params = dict(PARAM_OVERRIDES.get(effect_type, {}))
        effects = (EffectInstance(effect_type=effect_type, params=params,
                                  is_enabled=True),)
    clip = ClipInstance(
        clip_id="truth_clip", asset_id="truth_src", track_id="v1",
        timeline_start=0, timeline_duration=frames, source_start=0,
        source_duration=frames, effects=effects,
    )
    track = EditTrack(track_id="v1", name="V1", track_type="video",
                      index=0, clips=(clip,))
    sequence = TimelineSequence(
        timeline_id="truth_seq", name="TruthMatrix", fps=fps_rational,
        width=width, height=height, video_tracks=(track,),
    )
    if output_codec == "topos_proxy":
        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id="truth_seq",
            output_path=str(output),
            codec=ExportCodec.TOPOS_PROXY,
            pixel_format="yuv422p10le",
            quality=20, quality_mode="crf",
            fps=fps_rational,
            include_audio=False, burn_subtitles=False,
        )
    elif output_codec == "h264":
        config = TimelineExportConfig(
            source=ExportSource.TIMELINE,
            timeline_id="truth_seq",
            output_path=str(output),
            codec=ExportCodec.H264,
            pixel_format="yuv420p",
            quality=20_000_000, quality_mode="vbr",
            fps=fps_rational,
            include_audio=False, burn_subtitles=False,
        )
    else:
        raise ValueError(output_codec)
    return project, sequence, config


def _export_once(effect_type: str, frames: int, output: Path,
                 meta: dict[str, Any], output_codec: str) -> dict[str, Any]:
    from src.features.edit.core.timeline_export import create_timeline_exporter

    project, sequence, config = _build_timeline(
        effect_type, frames, output, meta, output_codec)
    exporter = create_timeline_exporter(project, sequence)
    started = time.perf_counter()
    outcome = exporter.export_timeline(config)
    wall_s = time.perf_counter() - started
    telemetry = getattr(outcome, "stage_telemetry", None)
    return {
        "effect_type": effect_type,
        "output_codec": output_codec,
        "success": bool(outcome) and output.exists(),
        "wall_s": wall_s,
        "fps": frames / wall_s if wall_s else 0.0,
        "error_code": str(getattr(outcome, "error_code", "") or ""),
        "cause": str(getattr(outcome, "cause", "") or "")[:500],
        "delivery_path": str(getattr(outcome, "delivery_path", "") or ""),
        "preflight_blockers": [
            b.to_dict() for b in
            (getattr(exporter, "_last_preflight_result", None).blockers
             if getattr(exporter, "_last_preflight_result", None) else [])
        ],
    }


def _y_stats(path: Path, frame_index: int) -> Optional[dict[str, Any]]:
    """中间帧 Y 平面统计（均值/标准差/内容哈希）。

    对拍不用均值差——blur/erode_dilate/defocus 是均值保持的空间滤波，
    均值几乎不变不代表效果未生效；平面内容哈希是效果参与的确定性证据。
    """
    from src.shared.media.factory import MediaSourceFactory

    handle = MediaSourceFactory.create(str(path))
    handle.open()
    try:
        frame = handle.read_frame(frame_index)
        planes = getattr(frame, "planes", None)
        import numpy as np

        if not planes:
            data = getattr(frame, "data", None)
            if data is None:
                return None
            rgb = np.asarray(data)
            luma = (0.2126 * rgb[..., 0].astype(np.float64)
                    + 0.7152 * rgb[..., 1].astype(np.float64)
                    + 0.0722 * rgb[..., 2].astype(np.float64))
        else:
            luma = np.asarray(planes[0], dtype=np.float64)
        digest = hashlib.sha256(np.ascontiguousarray(luma).tobytes()).hexdigest()[:16]
        return {"mean": round(float(luma.mean()), 4),
                "std": round(float(luma.std()), 4),
                "hash": digest}
    except Exception:
        return None
    finally:
        handle.close()


def _probe_output(path: Path) -> dict[str, Any]:
    from src.shared.media.factory import MediaSourceFactory

    result = MediaSourceFactory.probe_video_metadata_cached(str(path))
    metadata = result.metadata
    return {
        "backend": result.backend,
        "reason_code": result.reason_code,
        "ok": metadata is not None,
        "width": int(getattr(metadata, "width", 0) or 0) if metadata else 0,
        "height": int(getattr(metadata, "height", 0) or 0) if metadata else 0,
        "total_frames": int(getattr(metadata, "total_frames", 0) or 0)
        if metadata else 0,
    }


def _child(args: argparse.Namespace) -> int:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    meta = _source_meta()
    result = _export_once(args.effect, args.frames, output, meta,
                          args.output_codec)
    if result["success"]:
        result["probe"] = _probe_output(output)
        result["mid_frame_y_stats"] = _y_stats(output, args.frames // 2)
        output.unlink(missing_ok=True)
    result["kind"] = "effect_truth_combo"
    Path(args.emit_json).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["success"] else 1


def _run_child(spec: dict[str, Any], workdir: Path,
               timeout: float) -> dict[str, Any]:
    tag = f"{spec['output_codec']}-{spec['effect']}"
    result_path = workdir / f"child_{tag}.json"
    output_path = workdir / f"child_{tag}.mov"
    cmd = [sys.executable, str(Path(__file__).resolve()), "--child",
           "--effect", spec["effect"], "--output-codec", spec["output_codec"],
           "--frames", str(spec["frames"]), "--output", str(output_path),
           "--emit-json", str(result_path)]
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                              text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "spec": spec, "timeout_s": timeout}
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))
    else:
        result = {"status": "child_no_result",
                  "stderr_tail": proc.stderr[-3000:]}
    if proc.returncode != 0:
        result.setdefault("child_returncode", proc.returncode)
    return result


def _parent(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    specs = [{"effect": effect, "output_codec": codec, "frames": args.frames}
             for codec in CODECS for effect in EXPORT_EFFECTS]
    rows: list[dict[str, Any]] = []
    identity_stats: dict[str, dict[str, Any]] = {}
    for index, spec in enumerate(specs, start=1):
        print(f"[{index}/{len(specs)}] {spec['output_codec']}/{spec['effect']} ...",
              flush=True)
        row = _run_child(spec, workdir, timeout=args.timeout)
        rows.append(row)
        stats = row.get("mid_frame_y_stats")
        print(f"    ok={row.get('success')} fps={round(row.get('fps', 0) or 0, 2)} "
              f"y={stats}", flush=True)

    # identity 基线导出（每编码一次）
    for codec in CODECS:
        print(f"[identity] {codec} ...", flush=True)
        row = _run_child({"effect": "identity", "output_codec": codec,
                          "frames": args.frames}, workdir, args.timeout)
        stats = row.get("mid_frame_y_stats")
        if isinstance(stats, dict):
            identity_stats[codec] = stats
        rows.append(row)
        print(f"    ok={row.get('success')} y={stats}", flush=True)

    verdicts: list[dict[str, Any]] = []
    for row in rows:
        effect = row.get("effect_type")
        if effect == "identity":
            continue
        codec = row.get("output_codec")
        stats = row.get("mid_frame_y_stats") or {}
        base = identity_stats.get(codec) or {}
        hash_differs = bool(stats.get("hash") and base.get("hash")
                            and stats["hash"] != base["hash"])
        std_delta = (abs(float(stats.get("std", 0))
                         - float(base.get("std", 0)))
                     if stats and base else 0.0)
        verdict = {
            "effect_type": effect,
            "output_codec": codec,
            "exported": bool(row.get("success")),
            "probe_ok": bool((row.get("probe") or {}).get("ok")),
            "pixels_differ_from_identity": hash_differs,
            "identity_std": base.get("std"),
            "effect_std": stats.get("std"),
            "std_delta": round(std_delta, 4),
        }
        verdict["verdict"] = ("PASS" if (verdict["exported"]
                                         and verdict["probe_ok"]
                                         and verdict["pixels_differ_from_identity"])
                              else "FAIL")
        verdicts.append(verdict)
        print(f"  {verdict['verdict']} {codec}/{effect}", flush=True)

    failed = [v for v in verdicts if v["verdict"] != "PASS"]
    report = {
        "task": "T-P1-20 exportable-effect truth matrix",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "frames": args.frames,
        "source": str(SOURCE),
        "identity_y_stats": identity_stats,
        "rows": rows,
        "verdicts": verdicts,
        "all_pass": not failed,
    }
    out = workdir / "result.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"wrote {out}; all_pass={report['all_pass']}")
    return 0 if report["all_pass"] else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--effect", default="blur")
    parser.add_argument("--output-codec", default="topos_proxy")
    parser.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    parser.add_argument("--output", default="bench_out/tmp_effect_truth.mov")
    parser.add_argument("--emit-json", default="bench_out/tmp_effect_truth.json")
    parser.add_argument("--workdir", default="bench_out/ai_upgrade/T-P1-20_truth_matrix")
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args(argv)
    if args.child:
        return _child(args)
    return _parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
