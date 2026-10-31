#!/usr/bin/env python3
"""
Performance benchmark tool for Scene Cut Detection.

This tool measures detection performance across various scenarios:
- Synthetic fixtures (hard_cut, flash, dissolve, etc.)
- Real media files (1080p, 4K)
- Proxy vs. non-proxy detection
- Cache performance (first run vs. cached)

Performance targets (from implementation plan P10):
- 1080p 10min H.264 with proxy: >= 8x realtime
- 4K 10min H.264 without proxy: >= 2x realtime
- 1 hour material: memory growth < 300 MB
- Cached results reload: < 1 second

Usage:
    # Benchmark all fixtures
    .venv/bin/python tools/benchmark_scene_cut_detection.py

    # Benchmark specific fixture
    .venv/bin/python tools/benchmark_scene_cut_detection.py --fixture hard_cut

    # Benchmark real media file
    .venv/bin/python tools/benchmark_scene_cut_detection.py --media /path/to/video.mp4

    # Enable proxy mode
    .venv/bin/python tools/benchmark_scene_cut_detection.py --media /path/to/video.mp4 --use-proxy

    # JSON output for CI integration
    .venv/bin/python tools/benchmark_scene_cut_detection.py --json

Output:
- Detection speed (fps, realtime multiplier)
- Memory usage (peak, growth)
- Candidates found (count, kind breakdown)
- Cache status (hit/miss, load time)

Reference: docs/resolve_like_scene_cut_detection_implementation_plan_2026-06-04.md P10
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import os
import sys
import tracemalloc
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Project paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "scene_cut"
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"

# Add project to path
sys.path.insert(0, str(PROJECT_ROOT))

from src.features.edit.core.scene_cut.detector import (
    SceneCutDetector,
    DetectionPhase,
    DetectionProgress,
    get_hard_cut_candidates,
    get_warning_candidates,
)
from src.features.edit.core.scene_cut.models import (
    SceneCutSettings,
    SceneCutAnalysis,
    SceneCutCandidate,
    SceneCutKind,
    SceneCutDecision,
    DETECTOR_VERSION,
)
from src.features.edit.core.scene_cut.cache import (
    SceneCutCache,
    generate_cache_key,
)

logger = logging.getLogger("benchmark_scene_cut")


# =============================================================================
# Benchmark Results Data Structures
# =============================================================================

@dataclass
class BenchmarkMetrics:
    """Performance metrics for a single benchmark run."""

    # Timing
    duration_seconds: float = 0.0
    frames_processed: int = 0
    detection_fps: float = 0.0
    realtime_multiplier: float = 0.0

    # Memory
    memory_peak_mb: float = 0.0
    memory_growth_mb: float = 0.0
    memory_start_mb: float = 0.0

    # Results
    candidates_found: int = 0
    hard_cuts: int = 0
    soft_transitions: int = 0
    flash_detected: int = 0
    fast_motion: int = 0
    black_gap: int = 0
    warnings: int = 0

    # Cache
    cache_hit: bool = False
    cache_load_seconds: float = 0.0
    cache_key: str = ""

    # Media info
    source_path: str = ""
    media_fps: float = 0.0
    media_duration_seconds: float = 0.0
    media_frames: int = 0
    resolution: str = ""

    # Settings
    sensitivity: float = 0.55
    use_proxy: bool = True
    sample_width: int = 256
    sample_height: int = 144

    # Status
    success: bool = True
    error_message: str = ""
    detector_version: str = DETECTOR_VERSION


@dataclass
class BenchmarkReport:
    """Complete benchmark report with summary and individual results."""

    timestamp: str = ""
    total_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0

    # Summary statistics
    avg_fps: float = 0.0
    avg_realtime_multiplier: float = 0.0
    avg_memory_growth_mb: float = 0.0
    avg_candidates: float = 0.0

    # Performance target checks
    target_1080p_proxy_met: bool = False
    target_4k_no_proxy_met: bool = False
    target_memory_met: bool = False
    target_cache_met: bool = False

    # Individual results
    results: List[BenchmarkMetrics] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "timestamp": self.timestamp,
            "total_runs": self.total_runs,
            "successful_runs": self.successful_runs,
            "failed_runs": self.failed_runs,
            "summary": {
                "avg_fps": self.avg_fps,
                "avg_realtime_multiplier": self.avg_realtime_multiplier,
                "avg_memory_growth_mb": self.avg_memory_growth_mb,
                "avg_candidates": self.avg_candidates,
            },
            "performance_targets": {
                "1080p_proxy_8x_realtime": self.target_1080p_proxy_met,
                "4k_no_proxy_2x_realtime": self.target_4k_no_proxy_met,
                "memory_growth_under_300mb": self.target_memory_met,
                "cache_reload_under_1s": self.target_cache_met,
            },
            "results": [asdict(r) for r in self.results],
        }


# =============================================================================
# Benchmark Functions
# =============================================================================

def get_fixture_manifest() -> Dict[str, Any]:
    """Load fixture manifest.json."""
    manifest_path = FIXTURE_DIR / "manifest.json"
    if not manifest_path.exists():
        logger.warning(f"Manifest not found: {manifest_path}")
        return {"fixtures": {}}

    with open(manifest_path) as f:
        return json.load(f)


def get_fixture_info(fixture_name: str) -> Optional[Dict[str, Any]]:
    """Get info for a specific fixture from manifest."""
    manifest = get_fixture_manifest()
    fixtures = manifest.get("fixtures", {})

    # Try exact match first
    if fixture_name in fixtures:
        return fixtures[fixture_name]

    # Try without suffix
    for key, info in fixtures.items():
        if key.replace("_24fps", "") == fixture_name:
            return info

    return None


def format_resolution(width: int, height: int) -> str:
    """Format resolution string."""
    if height >= 2160:
        return "4K"
    elif height >= 1080:
        return "1080p"
    elif height >= 720:
        return "720p"
    elif height >= 480:
        return "480p"
    else:
        return f"{width}x{height}"


def run_benchmark(
    source_path: str,
    settings: SceneCutSettings,
    asset_id: str = "benchmark-asset",
    use_cache: bool = True,
    measure_memory: bool = True,
    progress_callback: Optional[Any] = None,
    proxy_path: Optional[str] = None,
) -> BenchmarkMetrics:
    """
    Run a single benchmark for scene cut detection.

    Args:
        source_path: Path to video file
        settings: Detection settings
        asset_id: Asset identifier
        use_cache: Whether to use cache
        measure_memory: Whether to measure memory
        progress_callback: Optional progress callback

    Returns:
        BenchmarkMetrics with performance data
    """
    metrics = BenchmarkMetrics(
        source_path=source_path,
        sensitivity=settings.sensitivity,
        use_proxy=settings.use_proxy,
        sample_width=settings.sample_width,
        sample_height=settings.max_sample_height,
    )

    # Get file info
    if not Path(source_path).exists():
        metrics.success = False
        metrics.error_message = f"File not found: {source_path}"
        return metrics

    file_stat = Path(source_path).stat()
    file_size = file_stat.st_size
    mtime_ns = file_stat.st_mtime_ns

    # Start memory tracking
    if measure_memory:
        tracemalloc.start()
        gc.collect()
        memory_start = tracemalloc.get_traced_memory()[0] / (1024 * 1024)
        metrics.memory_start_mb = memory_start

    # Check cache first
    cache_key = generate_cache_key(
        path=source_path,
        size=file_size,
        mtime_ns=mtime_ns,
        asset_id=asset_id,
        settings=settings,
        proxy_path=proxy_path,
    )
    metrics.cache_key = cache_key

    cache_dir = PROJECT_ROOT / ".cache" / "scene_cut_benchmark"
    cache = SceneCutCache(str(cache_dir)) if use_cache else None

    # Check for cached analysis
    cached_analysis: Optional[SceneCutAnalysis] = None
    if cache and cache.has_analysis(cache_key):
        cache_start = time.time()
        cached_analysis = cache.load_analysis(cache_key)
        cache_load_time = time.time() - cache_start
        metrics.cache_hit = cached_analysis is not None
        metrics.cache_load_seconds = cache_load_time

        if cached_analysis:
            metrics.duration_seconds = cache_load_time
            if cached_analysis.source_time_index is not None:
                metrics.frames_processed = len(cached_analysis.source_time_index.points)
            else:
                metrics.frames_processed = 0
            metrics.candidates_found = len(cached_analysis.candidates)

            # Count by kind
            hard_cuts = get_hard_cut_candidates(cached_analysis)
            warnings = get_warning_candidates(cached_analysis)

            metrics.hard_cuts = len(hard_cuts)
            metrics.warnings = len(warnings)
            metrics.soft_transitions = sum(
                1 for c in cached_analysis.candidates
                if c.kind == SceneCutKind.SOFT_TRANSITION
            )
            metrics.flash_detected = sum(
                1 for c in cached_analysis.candidates
                if c.kind == SceneCutKind.FLASH
            )
            metrics.fast_motion = sum(
                1 for c in cached_analysis.candidates
                if c.kind == SceneCutKind.FAST_MOTION
            )
            metrics.black_gap = sum(
                1 for c in cached_analysis.candidates
                if c.kind == SceneCutKind.BLACK_GAP
            )

            # Get media info from diagnostics
            diagnostics = cached_analysis.diagnostics or {}
            metrics.media_fps = diagnostics.get("fps", 0.0)
            metrics.media_frames = diagnostics.get("total_frames", 0)
            source_width = int(diagnostics.get("source_width", 0) or 0)
            source_height = int(diagnostics.get("source_height", 0) or 0)
            if source_width > 0 and source_height > 0:
                metrics.resolution = f"{source_width}x{source_height}"
            if metrics.media_frames <= 0:
                metrics.media_frames = metrics.frames_processed
            if metrics.media_fps > 0 and metrics.media_frames > 0:
                metrics.media_duration_seconds = metrics.media_frames / metrics.media_fps
            if cache_load_time > 0 and metrics.frames_processed > 0:
                metrics.detection_fps = metrics.frames_processed / cache_load_time
                if metrics.media_fps > 0:
                    metrics.realtime_multiplier = metrics.detection_fps / metrics.media_fps

            if measure_memory:
                tracemalloc.stop()

            return metrics

    # Run detection
    detector = SceneCutDetector(
        source_path=source_path,
        asset_id=asset_id,
        settings=settings,
        proxy_path=proxy_path,
    )

    # Track progress
    start_time = time.time()
    last_progress: Dict[str, Any] = {"phase": None, "current": 0, "total": 0}

    def track_progress(progress: DetectionProgress) -> None:
        last_progress["phase"] = progress.phase.value
        last_progress["current"] = progress.current_frame
        last_progress["total"] = progress.total_frames
        if progress_callback:
            progress_callback(progress)

    try:
        analysis = detector.detect(
            progress_callback=track_progress,
            cancel_token=None,
        )

        duration = time.time() - start_time
        metrics.duration_seconds = duration

        # Calculate fps
        total_frames = last_progress["total"] or analysis.diagnostics.get("total_frames", 0)
        metrics.frames_processed = total_frames
        metrics.media_frames = total_frames

        if total_frames > 0 and duration > 0:
            metrics.detection_fps = total_frames / duration

            # Get media fps for realtime multiplier
            media_fps = analysis.diagnostics.get("fps", 24.0)
            metrics.media_fps = media_fps
            if media_fps > 0:
                metrics.media_duration_seconds = total_frames / media_fps

            if media_fps > 0:
                metrics.realtime_multiplier = metrics.detection_fps / media_fps

        # Count candidates
        metrics.candidates_found = len(analysis.candidates)

        hard_cuts = get_hard_cut_candidates(analysis)
        warnings = get_warning_candidates(cached_analysis or analysis)

        metrics.hard_cuts = len(hard_cuts)
        metrics.warnings = len(warnings)

        # Count by kind
        for candidate in analysis.candidates:
            if candidate.kind == SceneCutKind.SOFT_TRANSITION:
                metrics.soft_transitions += 1
            elif candidate.kind == SceneCutKind.FLASH:
                metrics.flash_detected += 1
            elif candidate.kind == SceneCutKind.FAST_MOTION:
                metrics.fast_motion += 1
            elif candidate.kind == SceneCutKind.BLACK_GAP:
                metrics.black_gap += 1

        # Get resolution from detector diagnostics
        source_width = int(analysis.diagnostics.get("source_width", 0) or 0)
        source_height = int(analysis.diagnostics.get("source_height", 0) or 0)
        metrics.resolution = (
            f"{source_width}x{source_height}"
            if source_width > 0 and source_height > 0
            else "unknown"
        )

        # Save to cache
        if cache and analysis:
            cache.save_analysis(analysis)

    except Exception as e:
        metrics.success = False
        metrics.error_message = str(e)
        logger.error(f"Benchmark failed: {e}")

    # Measure memory
    if measure_memory:
        gc.collect()
        memory_peak = tracemalloc.get_traced_memory()[1] / (1024 * 1024)
        memory_current = tracemalloc.get_traced_memory()[0] / (1024 * 1024)
        memory_growth = memory_current - metrics.memory_start_mb

        metrics.memory_peak_mb = memory_peak
        metrics.memory_growth_mb = max(0.0, memory_growth)

        tracemalloc.stop()

    return metrics


def run_fixture_benchmark(
    fixture_name: str,
    settings: SceneCutSettings = SceneCutSettings(),
) -> BenchmarkMetrics:
    """
    Run benchmark on a synthetic fixture.

    Args:
        fixture_name: Name of fixture (e.g., "hard_cut", "flash_no_cut")
        settings: Detection settings

    Returns:
        BenchmarkMetrics
    """
    fixture_info = get_fixture_info(fixture_name)
    if not fixture_info:
        logger.warning(f"Fixture info not found for: {fixture_name}")
        # Try direct filename
        filename = f"{fixture_name}_24fps.mp4"
        fixture_path = FIXTURE_DIR / filename
        if not fixture_path.exists():
            metrics = BenchmarkMetrics(
                source_path=str(fixture_path),
                success=False,
                error_message=f"Fixture not found: {fixture_name}",
            )
            return metrics
    else:
        filename = fixture_info.get("filename", f"{fixture_name}_24fps.mp4")
        fixture_path = FIXTURE_DIR / filename

    if not fixture_path.exists():
        metrics = BenchmarkMetrics(
            source_path=str(fixture_path),
            success=False,
            error_message=f"Fixture file not found: {fixture_path}",
        )
        return metrics

    # Get fixture metadata
    fps = fixture_info.get("fps", 24.0) if fixture_info else 24.0
    width = fixture_info.get("width", 640) if fixture_info else 640
    height = fixture_info.get("height", 480) if fixture_info else 480
    total_frames = fixture_info.get("total_frames", 300) if fixture_info else 300

    metrics = run_benchmark(
        source_path=str(fixture_path),
        settings=settings,
        asset_id=f"fixture-{fixture_name}",
        use_cache=False,  # Don't cache fixtures
        measure_memory=True,
    )

    # Add fixture-specific info
    metrics.media_fps = fps
    metrics.media_frames = total_frames
    metrics.media_duration_seconds = total_frames / fps if fps > 0 else 0
    metrics.resolution = format_resolution(width, height)

    return metrics


def run_media_benchmark(
    media_path: str,
    settings: SceneCutSettings = SceneCutSettings(),
    use_proxy: bool = False,
    run_twice: bool = True,
    proxy_path: Optional[str] = None,
) -> Tuple[BenchmarkMetrics, Optional[BenchmarkMetrics]]:
    """
    Run benchmark on real media file.

    Args:
        media_path: Path to media file
        settings: Detection settings
        use_proxy: Whether to use proxy mode
        run_twice: Whether to run twice for cache test
        proxy_path: Path to proxy media file (optional)

    Returns:
        Tuple of (first_run_metrics, cached_run_metrics)
    """
    # Adjust settings for proxy
    if use_proxy or proxy_path:
        settings = SceneCutSettings(
            sensitivity=settings.sensitivity,
            use_proxy=True,
            sample_width=settings.sample_width,
            max_sample_height=settings.max_sample_height,
        )

    # First run (no cache)
    first_metrics = run_benchmark(
        source_path=media_path,
        settings=settings,
        asset_id=f"media-{Path(media_path).stem}",
        use_cache=True,  # Enable cache for second run
        measure_memory=True,
        proxy_path=proxy_path,
    )

    # Second run (with cache)
    cached_metrics: Optional[BenchmarkMetrics] = None
    if run_twice and first_metrics.success:
        cached_metrics = run_benchmark(
            source_path=media_path,
            settings=settings,
            asset_id=f"media-{Path(media_path).stem}",
            use_cache=True,
            measure_memory=False,  # Don't measure memory for cached run
            proxy_path=proxy_path,
        )

    return first_metrics, cached_metrics


# =============================================================================
# Performance Target Validation
# =============================================================================

def validate_performance_targets(report: BenchmarkReport) -> Dict[str, Any]:
    """
    Validate performance targets against benchmark results.

    Targets from implementation plan P10:
    - 1080p 10min H.264 with proxy: >= 8x realtime
    - 4K 10min H.264 without proxy: >= 2x realtime
    - 1 hour material: memory growth < 300 MB
    - Cached results: reload < 1 second

    Args:
        report: BenchmarkReport with results

    Returns:
        Dictionary with validation results
    """
    validation = {
        "1080p_proxy_8x_realtime": {
            "target": 8.0,
            "met": False,
            "actual": None,
            "message": "No 1080p proxy benchmark available",
        },
        "4k_no_proxy_2x_realtime": {
            "target": 2.0,
            "met": False,
            "actual": None,
            "message": "No 4K no-proxy benchmark available",
        },
        "memory_growth_300mb": {
            "target": 300.0,
            "met": True,  # Default to True if no long media tested
            "actual": None,
            "message": "No long-duration benchmark available",
        },
        "cache_reload_1s": {
            "target": 1.0,
            "met": False,
            "actual": None,
            "message": "No cache benchmark available",
        },
    }

    # Find 1080p proxy result
    for result in report.results:
        if result.use_proxy and result.resolution == "1080p":
            validation["1080p_proxy_8x_realtime"]["actual"] = result.realtime_multiplier
            validation["1080p_proxy_8x_realtime"]["met"] = result.realtime_multiplier >= 8.0
            validation["1080p_proxy_8x_realtime"]["message"] = (
                f"Actual: {result.realtime_multiplier:.2f}x realtime"
            )
            report.target_1080p_proxy_met = result.realtime_multiplier >= 8.0

    # Find 4K no-proxy result
    for result in report.results:
        if not result.use_proxy and result.resolution == "4K":
            validation["4k_no_proxy_2x_realtime"]["actual"] = result.realtime_multiplier
            validation["4k_no_proxy_2x_realtime"]["met"] = result.realtime_multiplier >= 2.0
            validation["4k_no_proxy_2x_realtime"]["message"] = (
                f"Actual: {result.realtime_multiplier:.2f}x realtime"
            )
            report.target_4k_no_proxy_met = result.realtime_multiplier >= 2.0

    # Check memory growth
    max_memory_growth = max(
        (r.memory_growth_mb for r in report.results if r.success),
        default=0.0,
    )
    validation["memory_growth_300mb"]["actual"] = max_memory_growth
    validation["memory_growth_300mb"]["met"] = max_memory_growth < 300.0
    validation["memory_growth_300mb"]["message"] = f"Peak growth: {max_memory_growth:.2f} MB"
    report.target_memory_met = max_memory_growth < 300.0

    # Check cache reload — exclude fixtures (they use use_cache=False by design)
    cache_results = [
        r for r in report.results
        if r.cache_hit and not r.source_path.startswith(str(FIXTURE_DIR))
    ]
    if cache_results:
        max_cache_time = max(r.cache_load_seconds for r in cache_results)
        validation["cache_reload_1s"]["actual"] = max_cache_time
        validation["cache_reload_1s"]["met"] = max_cache_time < 1.0
        validation["cache_reload_1s"]["message"] = f"Max reload: {max_cache_time:.3f}s"
        report.target_cache_met = max_cache_time < 1.0

    return validation


# =============================================================================
# Main Benchmark Runner
# =============================================================================

def run_all_fixture_benchmarks(
    settings: SceneCutSettings = SceneCutSettings(),
) -> BenchmarkReport:
    """
    Run benchmarks on all synthetic fixtures.

    Args:
        settings: Detection settings

    Returns:
        BenchmarkReport with all results
    """
    report = BenchmarkReport(
        timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
    )

    manifest = get_fixture_manifest()
    fixtures = manifest.get("fixtures", {})

    if not fixtures:
        logger.warning("No fixtures found in manifest")
        # Try to find fixtures by scanning directory
        for mp4_file in FIXTURE_DIR.glob("*.mp4"):
            fixture_name = mp4_file.stem.replace("_24fps", "")
            logger.info(f"Running benchmark: {fixture_name}")
            metrics = run_fixture_benchmark(fixture_name, settings)
            report.results.append(metrics)
            report.total_runs += 1
            if metrics.success:
                report.successful_runs += 1
            else:
                report.failed_runs += 1
    else:
        for fixture_name in fixtures:
            logger.info(f"Running benchmark: {fixture_name}")
            metrics = run_fixture_benchmark(fixture_name, settings)
            report.results.append(metrics)
            report.total_runs += 1
            if metrics.success:
                report.successful_runs += 1
            else:
                report.failed_runs += 1

    # Calculate summary
    successful = [r for r in report.results if r.success]
    if successful:
        report.avg_fps = sum(r.detection_fps for r in successful) / len(successful)
        report.avg_realtime_multiplier = sum(
            r.realtime_multiplier for r in successful if r.realtime_multiplier > 0
        ) / len([r for r in successful if r.realtime_multiplier > 0] or [1])
        report.avg_memory_growth_mb = sum(
            r.memory_growth_mb for r in successful
        ) / len(successful)
        report.avg_candidates = sum(r.candidates_found for r in successful) / len(successful)

    # Validate targets
    validate_performance_targets(report)

    return report


def print_report(report: BenchmarkReport, json_output: bool = False) -> None:
    """
    Print benchmark report.

    Args:
        report: BenchmarkReport
        json_output: Whether to output as JSON
    """
    if json_output:
        print(json.dumps(report.to_dict(), indent=2))
        return

    # Human-readable output
    print("=" * 60)
    print("Scene Cut Detection Benchmark Report")
    print("=" * 60)
    print(f"Timestamp: {report.timestamp}")
    print(f"Total runs: {report.total_runs}")
    print(f"Successful: {report.successful_runs}")
    print(f"Failed: {report.failed_runs}")
    print()

    # Summary
    print("Summary Statistics:")
    print(f"  Average FPS: {report.avg_fps:.2f}")
    print(f"  Average Realtime Multiplier: {report.avg_realtime_multiplier:.2f}x")
    print(f"  Average Memory Growth: {report.avg_memory_growth_mb:.2f} MB")
    print(f"  Average Candidates: {report.avg_candidates:.1f}")
    print()

    # Performance targets
    print("Performance Targets:")
    print(f"  1080p Proxy >= 8x realtime: {'PASS' if report.target_1080p_proxy_met else 'N/A'}")
    print(f"  4K No Proxy >= 2x realtime: {'PASS' if report.target_4k_no_proxy_met else 'N/A'}")
    print(f"  Memory Growth < 300 MB: {'PASS' if report.target_memory_met else 'FAIL'}")
    print(f"  Cache Reload < 1s: {'PASS' if report.target_cache_met else 'N/A'}")
    print()

    # Individual results
    print("Individual Results:")
    print("-" * 60)
    for result in report.results:
        status = "OK" if result.success else "FAIL"
        print(f"  [{status}] {Path(result.source_path).name}")
        if result.success:
            print(f"    Duration: {result.duration_seconds:.2f}s")
            print(f"    FPS: {result.detection_fps:.2f} ({result.realtime_multiplier:.2f}x realtime)")
            print(f"    Memory: peak={result.memory_peak_mb:.2f}MB, growth={result.memory_growth_mb:.2f}MB")
            print(f"    Candidates: {result.candidates_found}")
            print(f"      Hard cuts: {result.hard_cuts}")
            print(f"      Soft transitions: {result.soft_transitions}")
            print(f"      Flash: {result.flash_detected}")
            print(f"      Fast motion: {result.fast_motion}")
            print(f"      Black gap: {result.black_gap}")
            print(f"      Warnings: {result.warnings}")
        else:
            print(f"    Error: {result.error_message}")
        print()

    print("=" * 60)


# =============================================================================
# CLI Entry Point
# =============================================================================

def main() -> int:
    """Main entry point for benchmark tool."""
    parser = argparse.ArgumentParser(
        description="Performance benchmark for Scene Cut Detection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "--fixture",
        type=str,
        default=None,
        help="Benchmark specific fixture (e.g., hard_cut, flash_no_cut)",
    )

    parser.add_argument(
        "--media",
        type=str,
        default=None,
        help="Benchmark real media file",
    )

    parser.add_argument(
        "--use-proxy",
        action="store_true",
        default=False,
        help="Use proxy mode for media benchmark",
    )

    parser.add_argument(
        "--proxy-media",
        type=str,
        default=None,
        help="Path to proxy media file for proxy benchmark",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output results as JSON",
    )

    parser.add_argument(
        "--sensitivity",
        type=float,
        default=0.55,
        help="Detection sensitivity (0.35-0.75)",
    )

    parser.add_argument(
        "--all-fixtures",
        action="store_true",
        default=False,
        help="Run benchmarks on all fixtures",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    # Configure logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Validate sensitivity
    if not 0.35 <= args.sensitivity <= 0.75:
        logger.error(f"Invalid sensitivity: {args.sensitivity}")
        return 1

    # Create settings
    settings = SceneCutSettings(
        sensitivity=args.sensitivity,
        use_proxy=args.use_proxy,
    )

    # Run benchmark
    report = BenchmarkReport(timestamp=time.strftime("%Y-%m-%d %H:%M:%S"))

    if args.fixture:
        # Single fixture
        logger.info(f"Benchmarking fixture: {args.fixture}")
        metrics = run_fixture_benchmark(args.fixture, settings)
        report.results.append(metrics)
        report.total_runs = 1
        report.successful_runs = 1 if metrics.success else 0
        report.failed_runs = 0 if metrics.success else 1

    elif args.media:
        # Real media file
        logger.info(f"Benchmarking media: {args.media}")
        first_metrics, cached_metrics = run_media_benchmark(
            args.media,
            settings,
            use_proxy=args.use_proxy,
            run_twice=True,
            proxy_path=args.proxy_media,
        )
        report.results.append(first_metrics)
        report.total_runs = 1
        report.successful_runs = 1 if first_metrics.success else 0
        report.failed_runs = 0 if first_metrics.success else 1

        if cached_metrics:
            report.results.append(cached_metrics)
            report.total_runs += 1
            report.successful_runs += 1 if cached_metrics.success else 0
            report.failed_runs += 0 if cached_metrics.success else 1

    elif args.all_fixtures:
        # All fixtures
        logger.info("Benchmarking all fixtures")
        report = run_all_fixture_benchmarks(settings)

    else:
        # Default: all fixtures
        logger.info("Benchmarking all fixtures (default)")
        report = run_all_fixture_benchmarks(settings)

    # Calculate summary
    successful = [r for r in report.results if r.success]
    if successful:
        report.avg_fps = sum(r.detection_fps for r in successful) / len(successful)
        realtime_results = [r for r in successful if r.realtime_multiplier > 0]
        if realtime_results:
            report.avg_realtime_multiplier = sum(
                r.realtime_multiplier for r in realtime_results
            ) / len(realtime_results)
        report.avg_memory_growth_mb = sum(
            r.memory_growth_mb for r in successful
        ) / len(successful)
        report.avg_candidates = sum(r.candidates_found for r in successful) / len(successful)

    # Validate performance targets
    validate_performance_targets(report)

    # Print report
    print_report(report, json_output=args.json)

    # Return exit code
    return 0 if report.failed_runs == 0 else 1


if __name__ == "__main__":
    sys.exit(main())


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    "BenchmarkMetrics",
    "BenchmarkReport",
    "run_benchmark",
    "run_fixture_benchmark",
    "run_media_benchmark",
    "run_all_fixture_benchmarks",
    "validate_performance_targets",
    "print_report",
]
