#!/usr/bin/env python3
"""T-P1-22 卡步骤 4 证据 — H265/ProRes 20 循环导出 + 音画时长 probe + 资源曲线.

对真实素材（2K_H265.mov：HEVC 视频 + pcm_s24le 立体声，19.48s）构建
视频+音频轨时间线（音频挂 reverb DSP，保持真实 DSP→mux 路径全程参与），
循环 20 次导出（H264 / HEVC / PRORES_422 轮转），每次验证：

* 导出 outcome 成功；
* 输出容器音/视频流时长对齐（|Δ| < 0.25s，time_base 换算）；
* 即时 RSS（psutil，非 ru_maxrss 峰值——T-P1-71 教训）。

循环后：首 5 / 末 5 循环 RSS 中位数对比（持续增长即标红，关联 TP-71-MEM-001）。
末尾附加一次中途取消循环：必须返回 cancelled=True（不得伪成功）。

用法：
  .venv/bin/python scripts/bench_tp122_export_loop.py [--cycles 20]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SOURCE = ROOT / "tests" / "test_videos" / "2K_H265.mov"
FRAMES = 48  # 25fps → 1.92s
SAMPLE_RATE = 48000
DURATION_TOLERANCE_S = 0.25


def _build_timeline():
    from src.features.edit.core.effect_instance import EffectInstance
    from src.features.edit.core.project import EditProject, MediaAsset
    from src.features.edit.core.timeline_v2 import (
        ClipInstance, EditTrack, Rational, TimelineSequence,
    )

    asset = MediaAsset(
        asset_id="src", path=str(SOURCE), media_type="video",
        display_name="src", metadata={"fps": 25.0, "total_frames": 487},
    )
    project = EditProject.create(name="tp122-loop")
    project = project.with_media_asset_added(asset)

    audio_effects = (EffectInstance(
        effect_type="reverb", params={}, is_enabled=True),)
    video_clip = ClipInstance(
        clip_id="vid", asset_id="src", track_id="v1",
        timeline_start=0, timeline_duration=FRAMES,
        source_start=0, source_duration=FRAMES,
    )
    audio_clip = ClipInstance(
        clip_id="aud", asset_id="src", track_id="a1",
        timeline_start=0, timeline_duration=FRAMES,
        source_start=0, source_duration=FRAMES,
        effects=audio_effects,
    )
    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                   clips=(video_clip,))
    a1 = EditTrack(track_id="a1", name="A1", track_type="audio", index=0,
                   clips=(audio_clip,))
    seq = TimelineSequence(
        timeline_id="s", name="TP122Loop", fps=Rational(25, 1),
        width=1920, height=1080, sample_rate=SAMPLE_RATE,
        video_tracks=(v1,), audio_tracks=(a1,))
    return project, seq


def _export(project, seq, codec: str, pixel_format: str, out: Path):
    from src.features.edit.core.timeline_export import (
        ExportCodec, ExportSource, TimelineExportConfig,
        create_timeline_exporter,
    )

    base = TimelineExportConfig(
        source=ExportSource.TIMELINE, timeline_id="s", output_path=str(out),
        codec=ExportCodec(codec), pixel_format=pixel_format,
        quality=6_000_000, quality_mode="vbr", fps=__import__(
            "src.features.edit.core.timeline_v2", fromlist=["Rational"]
        ).Rational(25, 1),
        include_audio=True, burn_subtitles=False,
    )
    exporter = create_timeline_exporter(project, seq)
    outcome = exporter.export_timeline(base)
    return outcome


def _probe_durations(path: Path) -> dict:
    import av

    with av.open(str(path)) as container:
        video = [s for s in container.streams if s.type == "video"]
        audio = [s for s in container.streams if s.type == "audio"]
        result = {"has_video": bool(video), "has_audio": bool(audio)}
        if video and video[0].duration and video[0].time_base:
            result["video_s"] = float(video[0].duration * video[0].time_base)
        elif container.duration:
            result["video_s"] = container.duration / 1e6
        if audio and audio[0].duration and audio[0].time_base:
            result["audio_s"] = float(audio[0].duration * audio[0].time_base)
        if video:
            result["video_codec"] = video[0].codec_context.name
        if audio:
            result["audio_codec"] = audio[0].codec_context.name
        return result


def _rss_mb() -> float:
    import psutil

    return psutil.Process().memory_info().rss / (1024 * 1024)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    if not SOURCE.is_file():
        print(f"missing source: {SOURCE}")
        return 2

    import tempfile

    project, seq = _build_timeline()
    codec_rotation = (
        ("h264", "yuv420p"),
        ("hevc", "yuv420p10le"),
        ("prores_422", "yuv422p10le"),
    )
    expected_s = FRAMES / 25.0

    rows: list[dict] = []
    tmp = Path(tempfile.mkdtemp(prefix="tp122_loop_"))
    cancel_report: dict = {}

    for cycle in range(1, args.cycles + 1):
        codec, pix_fmt = codec_rotation[(cycle - 1) % len(codec_rotation)]
        out = tmp / f"cycle_{cycle:02d}_{codec}.mov"
        rss_before = _rss_mb()
        outcome = _export(project, seq, codec, pix_fmt, out)
        rss_after = _rss_mb()
        row = {
            "cycle": cycle, "codec": codec,
            "success": bool(outcome),
            "cause": getattr(outcome, "cause", "") if not bool(outcome) else "",
            "rss_before_mb": round(rss_before, 1),
            "rss_after_mb": round(rss_after, 1),
        }
        if bool(outcome):
            probe = _probe_durations(out)
            row.update(probe)
            delta = abs(probe.get("audio_s", 0.0) - probe.get("video_s", 0.0))
            row["av_delta_s"] = round(delta, 3)
            row["av_aligned"] = delta < DURATION_TOLERANCE_S
            out.unlink(missing_ok=True)
        rows.append(row)
        flag = "OK" if row.get("success") and row.get("av_aligned", False) else "FAIL"
        print(f"  cycle {cycle:02d} [{codec:11s}] {flag} "
              f"av_delta={row.get('av_delta_s', 'n/a')}s "
              f"rss={row['rss_after_mb']:.0f}MB")

    # ── cancel cycle: mid-export cancel must report user_cancelled ──
    cancel_out = tmp / "cancel_probe.mov"
    from src.features.edit.core.timeline_export import (
        ExportCodec, ExportSource, TimelineExportConfig,
        create_timeline_exporter,
    )
    from src.features.edit.core.timeline_v2 import Rational

    cfg = TimelineExportConfig(
        source=ExportSource.TIMELINE, timeline_id="s",
        output_path=str(cancel_out), codec=ExportCodec.H264,
        pixel_format="yuv420p", quality=6_000_000, quality_mode="vbr",
        fps=Rational(25, 1), include_audio=True, burn_subtitles=False,
    )
    exporter = create_timeline_exporter(project, seq)

    def cancel_early(current, total, pct):
        if current >= 5:
            exporter.cancel_export()

    exporter._progress_callback = cancel_early
    cancel_outcome = exporter.export_timeline(cfg)
    cancel_report = {
        "success": bool(cancel_outcome),
        "cancelled": bool(getattr(cancel_outcome, "cancelled", False)),
    }
    print(f"  cancel probe: success={cancel_report['success']} "
          f"cancelled={cancel_report['cancelled']}")

    # ── resource curve ──
    first5 = statistics.median([r["rss_after_mb"] for r in rows[:5]])
    last5 = statistics.median([r["rss_after_mb"] for r in rows[-5:]])
    drift_mb = round(last5 - first5, 1)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = (Path(args.out) if args.out else
               ROOT / "bench_out" / "ai_upgrade" / f"T-P1-22_loop_{stamp}")
    out_dir.mkdir(parents=True, exist_ok=True)

    all_export_ok = all(r["success"] and r.get("av_aligned") for r in rows)
    payload = {
        "task": "T-P1-22 步骤 4 — 20 循环导出 + 音画时长 probe + 资源曲线",
        "date": stamp, "source": SOURCE.name, "frames": FRAMES,
        "expected_duration_s": expected_s,
        "tolerance_s": DURATION_TOLERANCE_S,
        "cycles": rows, "cancel_probe": cancel_report,
        "rss_first5_median_mb": first5, "rss_last5_median_mb": last5,
        "rss_drift_mb": drift_mb,
        "all_cycles_ok": all_export_ok,
        "cancel_contract_ok": (not cancel_report["success"]
                               and cancel_report["cancelled"]),
        "known_issue": "TP-71-MEM-001（H265 导出 native 层内存缓升，独立跟踪）",
    }
    (out_dir / "loop_report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# T-P1-22 步骤 4 — 20 循环导出证据",
        "",
        f"- 素材: {SOURCE.name}（HEVC + pcm_s24le，19.48s）；每循环 48 帧（1.92s），音频挂 reverb DSP",
        f"- 编解码轮转: h264 / hevc / prores_422；音画时长容差 ±{DURATION_TOLERANCE_S}s",
        f"- 结果: {sum(1 for r in rows if r['success'] and r.get('av_aligned'))}/{len(rows)} 循环 OK",
        f"- 取消契约: success={cancel_report['success']} cancelled={cancel_report['cancelled']} "
        f"（期望 False/True）",
        f"- RSS 首5循环中位数 {first5:.0f}MB → 末5循环 {last5:.0f}MB（drift {drift_mb:+.0f}MB；"
        f"TP-71-MEM-001 native 缓升独立跟踪）",
        "",
        "| 循环 | codec | 成功 | 音频流 | av Δ(s) | RSS(MB) |",
        "|---:|---|---|---|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['cycle']} | {r['codec']} | {'✓' if r['success'] else '✗'} "
            f"| {r.get('audio_s', '—')} | {r.get('av_delta_s', '—')} "
            f"| {r['rss_after_mb']} |")
    (out_dir / "result.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"evidence → {out_dir}")
    return 0 if (all_export_ok and cancel_report["cancelled"]) else 1


if __name__ == "__main__":
    sys.exit(main())
