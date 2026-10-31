#!/usr/bin/env python
"""R7: real-file bitrate acceptance for the MG cache quality tiers.

Renders actual encoded videos with the exact per-tier bitrate arguments the
realtime cache recorder passes to FFmpeg, then reads the delivered files back
with ffprobe and prints a target-range vs measured report.

Acceptance policy (the product contract, not just "some bitrate happened"):

  * noise samples (incompressible) are the gate: the measured average must
    land inside [60% of target, upper] where upper is 125% of target for
    release-grade runs, widened by the VBV physics ceiling for short clips
    (bufsize is 2x target, so a clip shorter than 8s may legally average
    above maxrate while the initial buffer drains);
  * moving-detail and flat samples are reported with explicit verdicts of
    their own -- "content-complexity limited" and "static floor policy" --
    and never presented as "target reached".

Product tier targets at 1920x1080 are proxy 8 / balanced 20 / high_quality
40 Mbps (motion_graphic_bitrate_kbps scales by pixel area above that).

Exit code 0 when every gated sample meets its policy, 1 otherwise.
--report-only prints the same report but always exits 0.  Requires FFmpeg
+ ffprobe on PATH (skips with a message when absent).

Usage:
  scripts/verify_mg_cache_bitrate.py                 # gate at 720p (default)
  scripts/verify_mg_cache_bitrate.py --width 1920 --height 1080 \
      --seconds 10 --fps 24                          # release-grade run
  scripts/verify_mg_cache_bitrate.py --report-only   # exploratory, exit 0
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from src.features.edit.core.render_cache import (  # noqa: E402
    MOTION_GRAPHIC_QUALITY_PRESETS,
    motion_graphic_bitrate_kbps,
    motion_graphic_min_bitrate_kbps,
)
from src.features.teaching_animation.render.bitrate_probe import (  # noqa: E402
    VideoMetrics,
    probe_video_metrics,
)
from src.features.teaching_animation.render.html_realtime_recorder import (  # noqa: E402
    _ensure_minimum_webm_bitrate,
)

# Gate bounds for the incompressible sample, as fractions of the tier target.
_GATE_LOWER = 0.60
_GATE_UPPER = 1.25
# The recorder encodes with bufsize = 2x target, so over a T-second clip the
# VBV-bounded average may legally reach target * (1 + 2/T).  Below this
# duration the widened ceiling is flagged as a short-sample transient.
_RELEASE_SECONDS = 8


def vbv_ceiling_ratio(seconds: int) -> float:
    """Upper-bound ratio the VBV allows for a clip of this length."""
    return max(_GATE_UPPER, 1.0 + 2.0 / max(1, seconds))


def _encode(
    ffmpeg: str, out: Path, *, width: int, height: int, fps: int, seconds: int,
    bitrate_kbps: int, sample: str,
) -> None:
    """Encode one real sample with the recorder's CBR-style arguments.

    Samples: noise (incompressible random frames -- the acceptance gate),
    moving (testsrc2 -- moderate detail, reported as complexity-limited)
    and flat (static color on WebM -- exercises the minimum-bitrate
    Void-padding policy).  Noise/moving use the H.264 delivery the recorder
    uses for mp4 caches; flat uses the VP8/WebM delivery.
    """
    if sample == "noise":
        source = f"nullsrc=s={width}x{height}:r={fps}:d={seconds},geq=random(1)*255:128:128"
    elif sample == "moving":
        source = f"testsrc2=s={width}x{height}:r={fps}:d={seconds}"
    else:
        source = f"color=c=0x123456:size={width}x{height}:rate={fps}:duration={seconds}"
    command = [
        ffmpeg, "-y", "-f", "lavfi", "-i", source,
        "-vf", f"scale={width}:{height},format=yuv420p",
        "-r", str(fps),
        "-b:v", f"{bitrate_kbps}k",
        "-minrate", f"{bitrate_kbps}k",
        "-maxrate", f"{bitrate_kbps}k",
        "-bufsize", f"{bitrate_kbps * 2}k",
        "-c:v", "libvpx" if out.suffix == ".webm" else "libx264",
        "-pix_fmt", "yuv420p",
        str(out),
    ]
    if out.suffix == ".webm":
        command[-3:-3] = ["-deadline", "good", "-cpu-used", "4"]
    subprocess.run(command, check=True, capture_output=True)


def _tier_rows(args, ffmpeg: str, outdir: Path) -> list[dict]:
    rows: list[dict] = []
    for preset, _reference in MOTION_GRAPHIC_QUALITY_PRESETS.items():
        # Both helpers already return kbps (e.g. 8000 = 8 Mbps).
        target_kbps = motion_graphic_bitrate_kbps(args.width, args.height, preset=preset)
        minimum_kbps = motion_graphic_min_bitrate_kbps(args.width, args.height, preset=preset)
        for sample, ext in (("noise", ".mp4"), ("moving", ".mp4"), ("flat", ".webm")):
            path = outdir / f"{preset}_{sample}{ext}"
            _encode(ffmpeg, path, width=args.width, height=args.height,
                    fps=args.fps, seconds=args.seconds,
                    bitrate_kbps=target_kbps, sample=sample)
            metrics: VideoMetrics | None = probe_video_metrics(path)
            if sample == "flat":
                # The recorder applies Void padding only after its high-quality
                # encode; mirror that policy so the floor is measurable.
                _ensure_minimum_webm_bitrate(
                    path, metrics.duration_s if metrics else float(args.seconds), minimum_kbps)
                metrics = probe_video_metrics(path)
            rows.append({
                "preset": preset, "sample": sample, "path": path,
                "target_kbps": target_kbps, "minimum_kbps": minimum_kbps,
                "metrics": metrics,
            })
    return rows


def _verdict(row: dict, seconds: int) -> tuple[str, str, bool]:
    """Classify one sample: (verdict line, note, gated_failure)."""
    metrics = row["metrics"]
    target = row["target_kbps"]
    sample = row["sample"]
    if metrics is None:
        return "PROBE FAILED", "ffprobe could not read the file back", True
    measured = metrics.avg_bitrate_kbps
    ratio = metrics.bitrate_ratio(target)
    ceiling = vbv_ceiling_ratio(seconds)
    if sample == "noise":
        ok = _GATE_LOWER * target <= measured <= ceiling * target
        note = (f"gate: measured must stay within "
                f"[{_GATE_LOWER:.0%}, {ceiling:.0%}] of target")
        return ("PASS" if ok else "FAIL"), note, not ok
    if sample == "moving":
        # Moderately complex content legitimately undershoots a CBR target;
        # it proves the encode chain, not that the tier's quality was reached.
        ok = measured > 0
        note = "content-complexity limited: encode-chain check, not a quality verdict"
        return ("INFO" if ok else "FAIL"), note, not ok
    # flat: static content must satisfy the minimum-bitrate floor policy.
    ok = measured >= row["minimum_kbps"] * 0.98
    note = "static floor policy: minimum-bitrate padding works, not a quality verdict"
    return ("PASS" if ok else "FAIL"), note, not ok


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--seconds", type=int, default=2)
    parser.add_argument("--outdir", type=Path, default=None)
    parser.add_argument("--report-only", action="store_true",
                        help="print the report but always exit 0 (exploration)")
    parser.add_argument("--strict", action="store_true",
                        help="kept for compatibility: gating is now the default")
    parser.add_argument("--legacy-lenient", action="store_true",
                        help="old behaviour: pass when anything was encoded at all")
    args = parser.parse_args()

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        print("SKIP: FFmpeg/ffprobe not on PATH")
        return 0

    outdir = args.outdir or Path(tempfile.mkdtemp(prefix="mg_bitrate_"))
    outdir.mkdir(parents=True, exist_ok=True)
    rows = _tier_rows(args, ffmpeg, outdir)

    print(f"\nMG cache bitrate acceptance ({args.width}x{args.height}, "
          f"{args.seconds}s @ {args.fps}fps) — real encodes, ffprobe read-back")
    print(f"gate: noise within "
          f"[{_GATE_LOWER:.0%}, {vbv_ceiling_ratio(args.seconds):.0%}] of target"
          f" (VBV ceiling for {args.seconds}s clips; release runs use "
          f"--seconds >= {_RELEASE_SECONDS})\n")
    print(f"{'tier':<13}{'sample':<8}{'target':>9}{'range (Mbps)':>17}{'measured':>11}"
          f"{'ratio':>8}  verdict")
    failures = 0
    for row in rows:
        metrics = row["metrics"]
        target_m = row["target_kbps"] / 1000
        if metrics is None:
            print(f"{row['preset']:<13}{row['sample']:<8}{target_m:>8.1f}M"
                  f"{'?':>17}{'?':>11}{'?':>8}  PROBE FAILED")
            failures += 1
            continue
        verdict, note, failed = _verdict(row, args.seconds)
        if failed and not args.legacy_lenient:
            failures += 1
        measured_m = metrics.avg_bitrate_kbps / 1000
        ceiling_m = row["target_kbps"] * vbv_ceiling_ratio(args.seconds) / 1000
        print(f"{row['preset']:<13}{row['sample']:<8}{target_m:>8.1f}M"
              f"{_GATE_LOWER * target_m:>8.1f}–{ceiling_m:<8.1f}"
              f"{measured_m:>10.1f}M{metrics.bitrate_ratio(row['target_kbps']):>8.3f}"
              f"  {verdict}")
        print(f"    {note}")
        print(f"    {row['path']}  ({metrics.codec}, {metrics.nb_frames} frames, "
              f"{metrics.duration_s:.2f}s, {metrics.pixel_format})")
    gated = sum(1 for row in rows if row["sample"] in ("noise", "flat"))
    if args.legacy_lenient:
        print("\nLEGACY LENIENT MODE: any successful encode passes; do not use for gating")
    print(f"\n{len(rows) - failures}/{len(rows)} samples passed policy "
          f"({gated} gated); files kept in {outdir}")
    if args.report_only:
        print("--report-only: exit code forced to 0")
        return 0
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
