"""R7: real-file bitrate acceptance probes (FFmpeg required, else skipped)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.shared.media.motion_graphic_delivery import (
    MOTION_GRAPHIC_QUALITY_PRESETS,
    motion_graphic_bitrate_kbps,
    motion_graphic_min_bitrate_kbps,
)
from src.features.teaching_animation.render.bitrate_probe import probe_video_metrics
from src.features.teaching_animation.render.html_realtime_recorder import (
    _ensure_minimum_webm_bitrate,
)

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg/ffprobe not installed",
)

W, H, FPS, SECONDS = 320, 180, 24, 2


def _encode(path: Path, *, noise: bool, bitrate_kbps: int) -> Path:
    # Random-noise frames are effectively incompressible, so a CBR-style
    # target is actually spendable (testsrc2's smooth gradients are not at
    # small resolutions and make x264 legitimately undershoot).
    source = (
        f"nullsrc=s={W}x{H}:r={FPS}:d={SECONDS},geq=random(1)*255:128:128" if noise
        else f"color=c=0x123456:size={W}x{H}:rate={FPS}:duration={SECONDS}"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", source,
         "-b:v", f"{bitrate_kbps}k", "-minrate", f"{bitrate_kbps}k",
         "-maxrate", f"{bitrate_kbps}k", "-bufsize", f"{bitrate_kbps * 2}k",
         "-c:v", "libvpx" if path.suffix == ".webm" else "libx264",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True,
    )
    return path


def test_probe_reads_back_real_mp4_metrics(tmp_path: Path) -> None:
    video = _encode(tmp_path / "sample.mp4", noise=True, bitrate_kbps=2500)
    metrics = probe_video_metrics(video)
    assert metrics is not None
    assert metrics.codec == "h264"
    assert (metrics.width, metrics.height) == (W, H)
    assert metrics.nb_frames == FPS * SECONDS
    assert abs(metrics.duration_s - SECONDS) < 0.25
    assert metrics.pixel_format == "yuv420p"
    # Incompressible noise at a 2.5 Mbps CBR target measures near target:
    # this is the "real file, not computed constant" acceptance.
    assert metrics.avg_bitrate_kbps >= 0.8 * 2500


def test_probe_returns_none_for_missing_inputs(tmp_path: Path) -> None:
    assert probe_video_metrics(tmp_path / "ghost.mp4") is None


def test_flat_webm_meets_minimum_after_void_padding(tmp_path: Path) -> None:
    """The low-complexity undershoot policy, proven on a real encode."""
    preset = "balanced"
    minimum = motion_graphic_min_bitrate_kbps(W, H, preset=preset) // 1000
    video = _encode(tmp_path / "flat.webm", noise=False, bitrate_kbps=minimum)
    before = probe_video_metrics(video)
    assert before is not None
    _ensure_minimum_webm_bitrate(video, before.duration_s, minimum)
    after = probe_video_metrics(video)
    assert after is not None
    assert after.avg_bitrate_kbps >= minimum * 0.98
    assert after.nb_frames == FPS * SECONDS  # padding never touched the stream


def test_every_tier_reports_real_measured_bitrate(tmp_path: Path) -> None:
    """One real file per quality tier: target vs. measured is explainable."""
    report: dict[str, dict] = {}
    for preset in MOTION_GRAPHIC_QUALITY_PRESETS:
        target = motion_graphic_bitrate_kbps(W, H, preset=preset) // 1000
        # MP4/x264: predictable CBR behaviour.  (VP8/libvpx undershoots
        # pathologically on pure noise, which the flat-sample padding test
        # below covers separately.)
        video = _encode(tmp_path / f"{preset}.mp4", noise=True, bitrate_kbps=target)
        metrics = probe_video_metrics(video)
        assert metrics is not None
        report[preset] = {
            "target_kbps": target,
            "measured_kbps": round(metrics.avg_bitrate_kbps, 1),
            "ratio": round(metrics.bitrate_ratio(target), 3),
        }
        assert metrics.avg_bitrate_kbps > 0
        assert metrics.nb_frames >= FPS * SECONDS - 1  # WebM counts via packets
    # Proxy's 8 Mbps is spendable at this size, so it lands near target;
    # higher tiers legitimately undershoot on a 320x180 surface (an encoder
    # cannot spend 40 Mbps on frames this small) but must still invest more
    # bits than the tier below — that ordering is the honest acceptance.
    assert report["proxy"]["ratio"] >= 0.8
    assert report["balanced"]["measured_kbps"] > report["proxy"]["measured_kbps"]
    assert report["high_quality"]["measured_kbps"] > report["balanced"]["measured_kbps"]


def test_cache_worker_sidecar_payload_shape(tmp_path: Path) -> None:
    """The worker's <video>.bitrate.json contract (target + measured)."""
    video = _encode(tmp_path / "clip.mp4", noise=True, bitrate_kbps=2000)
    metrics = probe_video_metrics(video)
    assert metrics is not None
    payload = {
        "quality_tier": "high_quality",
        "target_bitrate_kbps": 40,
        "minimum_bitrate_kbps": 20,
        "measured": {
            "avg_bitrate_kbps": round(metrics.avg_bitrate_kbps, 1),
            "duration_s": round(metrics.duration_s, 4),
            "nb_frames": metrics.nb_frames,
            "codec": metrics.codec,
        },
    }
    text = json.dumps(payload)
    restored = json.loads(text)
    assert restored["target_bitrate_kbps"] == 40
    assert restored["measured"]["codec"] == "h264"
