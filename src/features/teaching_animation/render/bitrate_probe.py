"""ffprobe-based acceptance metrics for real encoded video files (R7).

The cache quality tiers (proxy 8 / balanced 20 / high 40 Mbps) were previously
verified only as computed constants and encoder arguments.  This probe reads
the *actual* delivered file back so reports, cache manifests and tests can
compare target vs. achieved bitrate, frame count and duration.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess

from src.shared.ffmpeg_locator import find_ffprobe

#: Upper bound for one ffprobe invocation.  A corrupted/truncated file can make
#: ffprobe hang far longer than any legitimate probe; the module contract is
#: "never raises, degrade to unknown", and a hang is strictly worse than an
#: exception because it stalls the whole acceptance/cache pipeline (AUD-29808).
_FFPROBE_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class VideoMetrics:
    """Measured facts about one encoded video file."""

    duration_s: float
    nb_frames: int
    width: int
    height: int
    fps: float
    codec: str
    pixel_format: str
    size_bytes: int
    format_bitrate_bps: int
    stream_bitrate_bps: int | None = None

    @property
    def avg_bitrate_bps(self) -> int:
        """Best available average bitrate (container header > byte math)."""
        if self.format_bitrate_bps > 0:
            return self.format_bitrate_bps
        if self.duration_s > 0:
            return int(self.size_bytes * 8.0 / self.duration_s)
        return 0

    @property
    def avg_bitrate_kbps(self) -> float:
        return self.avg_bitrate_bps / 1000.0

    def bitrate_ratio(self, target_kbps: int) -> float:
        """Achieved/target ratio; >1.0 means the file met or exceeded target."""
        if target_kbps <= 0:
            return 0.0
        return self.avg_bitrate_kbps / float(target_kbps)


def _ffprobe_binary() -> str | None:
    """ffprobe 定位：ffmpeg_locator（打包 bin/ → PATH）。"""
    return find_ffprobe()


def probe_video_metrics(path: str | Path) -> VideoMetrics | None:
    """Probe a real video file; ``None`` when ffprobe or the file is missing.

    Never raises: acceptance reporting must degrade to "unknown" rather than
    break the cache pipeline that produced the file.
    """
    video = Path(path)
    binary = _ffprobe_binary()
    if binary is None or not video.is_file():
        return None
    try:
        result = subprocess.run(
            [
                binary, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries",
                "stream=codec_name,pix_fmt,width,height,r_frame_rate,nb_frames,bit_rate:"
                "format=duration,bit_rate,size",
                "-of", "json", str(video),
            ],
            check=False, capture_output=True, text=True,
            timeout=_FFPROBE_TIMEOUT_S,
        )
        payload = json.loads(result.stdout or "{}")
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return None
    streams = payload.get("streams") or []
    fmt = payload.get("format") or {}
    if not streams:
        return None
    stream = streams[0]

    def _int(value: object) -> int:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    def _opt_int(value: object) -> int | None:
        try:
            number = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    def _float(value: object) -> float:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    fps = 0.0
    rate = str(stream.get("r_frame_rate", ""))
    if "/" in rate:
        num, _, den = rate.partition("/")
        try:
            fps = float(num) / max(1e-9, float(den))
        except ValueError:
            fps = 0.0
    nb_frames = _int(stream.get("nb_frames"))
    if nb_frames <= 0:
        # WebM/Matroska containers do not store a per-stream frame count;
        # count packets instead (cheap: demux only, no decode).
        try:
            counted = subprocess.run(
                [binary, "-v", "error", "-count_packets", "-select_streams", "v:0",
                 "-show_entries", "stream=nb_read_packets",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
                check=False, capture_output=True, text=True,
                timeout=_FFPROBE_TIMEOUT_S,
            )
            nb_frames = _int(counted.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            nb_frames = 0
    def _fallback_size() -> int:
        # AUD-29809: is_file() and stat() are two separate syscalls — a file
        # deleted between them raised OSError out of this function, breaking
        # the documented "never raises, degrade to unknown" contract. The
        # whole function body above is guarded; this late stat was the one
        # unguarded edge. Degrade to 0 like every other probe failure.
        try:
            return video.stat().st_size if video.is_file() else 0
        except OSError:
            return 0

    return VideoMetrics(
        duration_s=_float(fmt.get("duration")),
        nb_frames=nb_frames,
        width=_int(stream.get("width")),
        height=_int(stream.get("height")),
        fps=fps,
        codec=str(stream.get("codec_name", "")),
        pixel_format=str(stream.get("pix_fmt", "")),
        size_bytes=_int(fmt.get("size")) or _fallback_size(),
        format_bitrate_bps=_int(fmt.get("bit_rate")),
        stream_bitrate_bps=_opt_int(stream.get("bit_rate")),
    )


__all__ = ["VideoMetrics", "probe_video_metrics"]
