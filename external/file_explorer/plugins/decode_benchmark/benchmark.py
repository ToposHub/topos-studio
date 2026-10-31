"""Decode benchmark engine for the file-explorer plugin.

The runner deliberately goes through :class:`SourceManager`, the shared
lower-level media source used by the project's playback/fallback paths. It
therefore measures the project's real FFmpeg/PyAV source, proxy policy and
hardware-acceleration selection instead of shelling out to a second decoder
implementation. It is intentionally a source/decode benchmark, not a claim
about the Edit page's GPU compositor or present timing.

The module has no Qt dependency so it can be used by unit tests or a future
headless benchmark command.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Iterable, Literal, Optional, Sequence

logger = logging.getLogger(__name__)

# ``edit_realtime`` is handled by the Qt host service rather than this
# SourceManager worker.  Keeping it in the public mode type lets the plugin
# export one consistent report format while the low-level runner remains
# deliberately source-only.
DecodeMode = Literal["master", "proxy", "edit_realtime"]
SeekPattern = Literal["sequential", "random"]

SUPPORTED_CODECS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Common delivery, camera and editing codecs.  The aliases are the names
    # PyAV/FFmpeg reports in ``stream.codec.name``; the benchmark still probes
    # the real file and only uses this table to group its result in the UI.
    ("H.264", ("h264", "avc1", "avc")),
    ("H.265 / HEVC", ("hevc", "h265", "hev1")),
    ("ProRes", ("prores", "prores_ks", "prores_aw")),
    ("DNxHR / DNxHD", ("dnxhd", "dnxhr")),
    ("AV1", ("av1", "libaom-av1", "libdav1d", "svtav1")),
    ("VP9", ("vp9", "libvpx-vp9")),
    ("VP8", ("vp8", "libvpx")),
    ("MPEG-2", ("mpeg2video", "mpeg2")),
    ("MPEG-4", ("mpeg4", "mpeg4video", "mp4v")),
    ("MJPEG", ("mjpeg", "ljpeg")),
    ("FFV1", ("ffv1",)),
)

RESOLUTION_BUCKETS: tuple[tuple[str, int], ...] = (
    ("2K", 3200),
    ("4K", 5000),
    ("6K", 7000),
    ("8K", 100000),
)


def codec_family(codec_name: str) -> str:
    """Return a user-facing codec family for an FFmpeg codec name."""

    normalized = (codec_name or "").strip().lower()
    for label, aliases in SUPPORTED_CODECS:
        if normalized in aliases or any(alias in normalized for alias in aliases):
            return label
    return normalized.upper() if normalized else "Unknown"


def resolution_bucket(width: int, height: int) -> str:
    """Classify a source by its long edge.

    The bucket is intentionally descriptive rather than a hard validation
    gate: 4096x2160 and 3840x2160 both report as 4K, while unusual camera
    aspect ratios still land in the closest expected class.
    """

    long_edge = max(int(width), int(height))
    for label, upper_bound in RESOLUTION_BUCKETS:
        if long_edge <= upper_bound:
            return label
    return "8K+"


def percentile(values: Sequence[float], percent: float) -> float:
    """Compute a linear-interpolated percentile without NumPy."""

    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * (float(percent) / 100.0)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


@dataclass(frozen=True)
class BenchmarkConfig:
    """Configuration for one source benchmark."""

    frame_count: int = 120
    warmup_frames: int = 8
    mode: DecodeMode = "proxy"
    pattern: SeekPattern = "sequential"

    def normalized(self) -> "BenchmarkConfig":
        """Return a validated configuration suitable for a worker thread."""

        raw_mode = str(self.mode)
        mode: DecodeMode = raw_mode if raw_mode in ("master", "proxy", "edit_realtime") else "proxy"  # type: ignore[assignment]
        pattern: SeekPattern = "random" if self.pattern == "random" else "sequential"
        return BenchmarkConfig(
            frame_count=max(1, min(int(self.frame_count), 10000)),
            warmup_frames=max(0, min(int(self.warmup_frames), 240)),
            mode=mode,
            pattern=pattern,
        )


@dataclass
class BenchmarkResult:
    """Measured result for one source file."""

    path: str
    filename: str
    codec: str
    codec_family: str
    resolution: str
    width: int
    height: int
    fps: float
    bit_depth: int
    pixel_format: str
    mode: str
    pattern: str
    requested_frames: int
    warmup_frames: int = 0
    frames_decoded: int = 0
    failed_frames: int = 0
    elapsed_ms: float = 0.0
    decode_fps: float = 0.0
    avg_frame_ms: float = 0.0
    p95_frame_ms: float = 0.0
    min_frame_ms: float = 0.0
    max_frame_ms: float = 0.0
    decode_path: str = "unknown"
    decoder: str = "cpu"
    hardware_acceleration: bool = False
    proxy_size: str = "—"
    status: str = "pending"
    error: str = ""
    # Edit host-service measurements.  SourceManager results leave these at
    # their defaults; Edit realtime rows use them to distinguish presented
    # FPS from decode-only FPS and to preserve compositor/invariant details in
    # exported JSON/CSV reports.
    presented_fps: float = 0.0
    presented_frames: int = 0
    render_path: str = "unknown"
    composite_avg_ms: float = 0.0
    composite_p95_ms: float = 0.0
    dropped_frames: int = 0
    invariants_ok: bool = True
    frame_signal_source: str = "source_manager"
    # Detailed Edit frame-trace distributions (decode/render path and
    # fallback reasons) are kept as JSON-friendly dictionaries.
    trace_summary: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Serialize the result for JSON/API consumers."""

        return asdict(self)


ProgressCallback = Callable[[str, int, int], None]
ResultCallback = Callable[[BenchmarkResult], None]


def _ensure_project_import_path() -> None:
    """Make ``src.shared`` importable in standalone file-explorer launches."""

    project_root = Path(__file__).resolve().parents[4]
    if (project_root / "src").is_dir() and str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))


class BenchmarkCancelled(Exception):
    """Raised internally when a benchmark was cancelled."""


class DecodeBenchmarkRunner:
    """Run benchmarks through the Topos ``SourceManager`` pipeline."""

    def __init__(self, cancel_event: Optional[threading.Event] = None) -> None:
        self.cancel_event = cancel_event or threading.Event()

    def cancel(self) -> None:
        """Request cancellation at the next frame boundary."""

        self.cancel_event.set()

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise BenchmarkCancelled()

    @staticmethod
    def _probe(path: str) -> Any:
        # Keep imports lazy: loading the file explorer should not initialise
        # PyAV/OpenGL when the benchmark window is never opened.
        _ensure_project_import_path()
        from src.shared.media.factory import MediaSourceFactory

        metadata = MediaSourceFactory.probe_video_metadata(path)
        if metadata is None:
            raise RuntimeError("无法探测视频元数据（请确认 FFmpeg/PyAV 可用）")
        return metadata

    @staticmethod
    def _frame_indices(
        total_frames: Optional[int], config: BenchmarkConfig
    ) -> list[int]:
        """Build deterministic sequential or seek-heavy frame indices."""

        normalized = config.normalized()
        available = int(total_frames or 0)
        start = min(normalized.warmup_frames, max(available - 1, 0)) if available else normalized.warmup_frames
        count = normalized.frame_count
        if available:
            count = min(count, max(1, available - start))
        if normalized.pattern == "sequential" or not available:
            return [start + offset for offset in range(count)]

        # A fixed stride produces repeatable random-ish seeks while avoiding
        # duplicates for common GOP lengths. The prime is intentionally not a
        # random seed so exported reports can be compared across machines.
        span = available - start
        if span <= 0:
            return [max(0, available - 1)]
        stride = 9973 % span
        if stride == 0:
            stride = 1
        # Ensure the walk visits every index before repeating whenever the
        # source has a common factor with the preferred stride.
        while math.gcd(stride, span) != 1:
            stride = (stride + 2) % span or 1
        return [start + ((offset * stride) % span) for offset in range(min(count, span))]

    def run_one(
        self,
        path: str,
        config: BenchmarkConfig,
        progress: Optional[ProgressCallback] = None,
    ) -> BenchmarkResult:
        """Benchmark one source and always release its decoder resources."""

        normalized = config.normalized()
        absolute_path = os.path.abspath(os.path.expanduser(path))
        if normalized.mode == "edit_realtime":
            return BenchmarkResult(
                path=absolute_path,
                filename=os.path.basename(absolute_path),
                codec="mixed",
                codec_family="Edit Timeline",
                resolution="—",
                width=0,
                height=0,
                fps=0.0,
                bit_depth=0,
                pixel_format="timeline",
                mode="edit_realtime",
                pattern=normalized.pattern,
                requested_frames=normalized.frame_count,
                warmup_frames=normalized.warmup_frames,
                status="error",
                error="Edit 实时模式必须通过宿主 host service 运行",
            )
        if not os.path.isfile(absolute_path):
            return BenchmarkResult(
                path=absolute_path,
                filename=os.path.basename(absolute_path),
                codec="unknown",
                codec_family="Unknown",
                resolution="—",
                width=0,
                height=0,
                fps=0.0,
                bit_depth=0,
                pixel_format="—",
                mode=normalized.mode,
                pattern=normalized.pattern,
                requested_frames=normalized.frame_count,
                warmup_frames=normalized.warmup_frames,
                status="error",
                error="文件不存在",
            )

        metadata = None
        manager = None
        try:
            metadata = self._probe(absolute_path)
            result = BenchmarkResult(
                path=absolute_path,
                filename=os.path.basename(absolute_path),
                codec=str(metadata.codec_name or "unknown"),
                codec_family=codec_family(str(metadata.codec_name or "")),
                resolution=resolution_bucket(metadata.width, metadata.height),
                width=int(metadata.width),
                height=int(metadata.height),
                fps=float(metadata.fps.to_float()),
                bit_depth=int(metadata.bit_depth),
                pixel_format=str(metadata.pixel_format or "unknown"),
                mode=normalized.mode,
                pattern=normalized.pattern,
                requested_frames=normalized.frame_count,
                warmup_frames=normalized.warmup_frames,
                status="running",
            )
            self._check_cancelled()

            # Importing SourceManager here keeps this module safe in the file
            # explorer process until the user actually starts a run.
            _ensure_project_import_path()
            from src.shared.video.playback_clock import SourceManager

            # SourceManager proxy is an offline lower-level comparison at a
            # 1920×1080 target.  It is intentionally kept separate from the
            # Edit host-service mode below, which measures the active
            # TimelinePreviewRenderer viewport and GPU compositor.
            manager = SourceManager(
                absolute_path,
                viewport_size=(1920, 1080),
                enable_planar_proxy=True,
                enable_planar_master=True,
            )
            if not manager.open():
                raise RuntimeError("Topos 解码源打开失败")

            getter = manager.get_master_frame if normalized.mode == "master" else manager.get_proxy_frame

            # Warm up the same source and decoder context used for the formal
            # run. Warmup frames are intentionally excluded from all timings,
            # but they establish GOP references, pixel-format conversion and
            # hardware session state just as playback does.
            available = int(manager.total_frames or 0)
            warmup_count = (
                min(normalized.warmup_frames, max(available - 1, 0))
                if available
                else normalized.warmup_frames
            )
            for warmup_index in range(warmup_count):
                self._check_cancelled()
                getter(warmup_index)

            frame_indices = self._frame_indices(manager.total_frames, normalized)

            frame_times: list[float] = []
            wall_start = time.perf_counter()
            for completed, frame_index in enumerate(frame_indices, start=1):
                self._check_cancelled()
                start = time.perf_counter()
                frame = getter(frame_index)
                elapsed = (time.perf_counter() - start) * 1000.0
                frame_times.append(elapsed)
                if frame is None:
                    result.failed_frames += 1
                else:
                    result.frames_decoded += 1
                if progress is not None:
                    progress(absolute_path, completed, len(frame_indices))

            result.elapsed_ms = (time.perf_counter() - wall_start) * 1000.0
            if result.frames_decoded:
                result.decode_fps = result.frames_decoded / max(result.elapsed_ms / 1000.0, 1e-9)
            measured = frame_times
            result.avg_frame_ms = mean(measured) if measured else 0.0
            result.p95_frame_ms = percentile(measured, 95.0)
            result.min_frame_ms = min(measured) if measured else 0.0
            result.max_frame_ms = max(measured) if measured else 0.0

            stats = manager.get_stats()
            hw = stats.get("hwaccel", {}) if isinstance(stats, dict) else {}
            if normalized.mode == "master":
                result.decode_path = "master_cpu"
                result.decoder = "cpu"
                result.hardware_acceleration = False
            else:
                result.decode_path = str(stats.get("decode_path", "unknown"))
                result.decoder = str(hw.get("decoder_type") or "cpu")
                result.hardware_acceleration = bool(hw.get("hwaccel_enabled", False))
            proxy_size = stats.get("proxy_size") if isinstance(stats, dict) else None
            if (
                isinstance(proxy_size, (tuple, list))
                and len(proxy_size) == 2
                and all(isinstance(value, (int, float)) for value in proxy_size)
            ):
                result.proxy_size = f"{proxy_size[0]}×{proxy_size[1]}"
            result.status = "ok" if result.failed_frames == 0 else "partial"
            return result
        except BenchmarkCancelled:
            raise
        except Exception as exc:
            if metadata is None:
                return BenchmarkResult(
                    path=absolute_path,
                    filename=os.path.basename(absolute_path),
                    codec="unknown",
                    codec_family="Unknown",
                    resolution="—",
                    width=0,
                    height=0,
                    fps=0.0,
                    bit_depth=0,
                    pixel_format="—",
                    mode=normalized.mode,
                    pattern=normalized.pattern,
                    requested_frames=normalized.frame_count,
                    warmup_frames=normalized.warmup_frames,
                    status="error",
                    error=str(exc),
                )
            return BenchmarkResult(
                path=absolute_path,
                filename=os.path.basename(absolute_path),
                codec=str(metadata.codec_name or "unknown"),
                codec_family=codec_family(str(metadata.codec_name or "")),
                resolution=resolution_bucket(metadata.width, metadata.height),
                width=int(metadata.width),
                height=int(metadata.height),
                fps=float(metadata.fps.to_float()),
                bit_depth=int(metadata.bit_depth),
                pixel_format=str(metadata.pixel_format or "unknown"),
                mode=normalized.mode,
                pattern=normalized.pattern,
                requested_frames=normalized.frame_count,
                warmup_frames=normalized.warmup_frames,
                status="error",
                error=str(exc),
            )
        finally:
            if manager is not None:
                try:
                    manager.close()
                except Exception as exc:  # pragma: no cover - defensive native cleanup
                    logger.warning("关闭 benchmark 解码源失败: %s", exc)

    def run_many(
        self,
        paths: Iterable[str],
        config: BenchmarkConfig,
        progress: Optional[ProgressCallback] = None,
        result_callback: Optional[ResultCallback] = None,
    ) -> list[BenchmarkResult]:
        """Run all paths in order, stopping cleanly when cancelled."""

        results: list[BenchmarkResult] = []
        for path in paths:
            self._check_cancelled()
            result = self.run_one(path, config, progress)
            results.append(result)
            if result_callback is not None:
                result_callback(result)
        return results


def export_results_json(path: str | Path, results: Sequence[BenchmarkResult]) -> None:
    """Write benchmark results as UTF-8 JSON."""

    output = Path(path).expanduser()
    output.write_text(
        json.dumps([result.to_dict() for result in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def export_results_csv(path: str | Path, results: Sequence[BenchmarkResult]) -> None:
    """Write benchmark results as a portable UTF-8 CSV."""

    output = Path(path).expanduser()
    rows = [result.to_dict() for result in results]
    fieldnames = list(rows[0].keys()) if rows else ["path", "status"]
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


__all__ = [
    "BenchmarkCancelled",
    "BenchmarkConfig",
    "BenchmarkResult",
    "DecodeBenchmarkRunner",
    "RESOLUTION_BUCKETS",
    "SUPPORTED_CODECS",
    "codec_family",
    "export_results_csv",
    "export_results_json",
    "percentile",
    "resolution_bucket",
]
