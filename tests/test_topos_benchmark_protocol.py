"""O0/V0 benchmark contracts that do not require a native rebuild."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
IMAGE_SCHEMA = REPO / "docs/image/benchmark_schema.json"
VIDEO_SCHEMA = REPO / "docs/video/benchmark_schema.json"
REDUCED_SCHEMA = REPO / "docs/codec/reduced_decode_benchmark_schema.json"
VALIDATOR = REPO / "scripts/validate_benchmark_report.py"
CORPUS = REPO / "tests/fixtures/topos_image/manifest.json"


def test_benchmark_schemas_are_valid_json() -> None:
    for path in (IMAGE_SCHEMA, VIDEO_SCHEMA, REDUCED_SCHEMA):
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert schema["$schema"].endswith("draft/2020-12/schema")
        assert schema["type"] == "object"
        assert schema["required"]


def test_reduced_decode_schema_freezes_comparison_modes() -> None:
    schema = json.loads(REDUCED_SCHEMA.read_text(encoding="utf-8"))
    assert schema["properties"]["benchmark"]["const"] == "topos_reduced_decode"
    assert set(schema["$defs"]["mode"]["properties"]["name"]["enum"]) == {
        "full", "post_scale", "legacy_reduced", "v7_band", "v7_base"
    }
    assert {"packet_bytes_read", "entropy_symbols", "idct_samples", "upload_bytes"} <= set(
        schema["$defs"]["telemetry"]["required"]
    )


def test_image_schema_covers_frozen_protocol_fields() -> None:
    schema = json.loads(IMAGE_SCHEMA.read_text(encoding="utf-8"))
    assert {"environment", "source", "profile", "threads", "cache_state", "stages", "quality"} <= set(schema["required"])
    assert {"p50", "p95", "p99", "max", "mean"} <= set(schema["$defs"]["percentiles"]["required"])


def test_corpus_manifest_is_reproducible_and_authorized() -> None:
    manifest = json.loads(CORPUS.read_text(encoding="utf-8"))
    assert manifest["authorization"].startswith("synthetic")
    assert set(manifest["categories"]) >= {"gradient", "noise", "hdr_highlight", "transparent_edges", "animation"}
    assert manifest["dimensions"]["4K"] == [3840, 2160]
    assert manifest["dimensions"]["6K"] == [6144, 3240]
    assert manifest["dimensions"]["8K_DCI"] == [8192, 4320]
    assert manifest["dimensions"]["12K"] == [12288, 6480]
    assert manifest["dimensions"]["16K"] == [16384, 8640]
    assert manifest["generated_files"] == []


def test_corpus_generator_smoke(tmp_path: Path) -> None:
    generator = REPO / "tests/fixtures/topos_image/generate_corpus.py"
    proc = subprocess.run([sys.executable, str(generator), "--output-dir", str(tmp_path),
                           "--sizes", "smoke", "--frames", "2", "--write-manifest"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["files"] == 11
    assert all(len(record["sha256"]) == 64 for record in payload["records"])
    run_manifest = tmp_path / "manifest.generated.json"
    assert run_manifest.exists()
    recorded = json.loads(run_manifest.read_text(encoding="utf-8"))
    assert recorded["generated_with"] == {"sizes": "smoke", "frames": 2}
    assert len(recorded["generated_files"]) == 11


def test_corpus_generator_exposes_reduced_resolution_matrix() -> None:
    import importlib.util

    generator = REPO / "tests/fixtures/topos_image/generate_corpus.py"
    spec = importlib.util.spec_from_file_location("topos_reduced_corpus", generator)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.REDUCED_SIZES["4K_DCI"] == (4096, 2160)
    assert module.REDUCED_SIZES["6K"] == (6144, 3240)
    assert module.REDUCED_SIZES["8K_DCI"] == (8192, 4320)
    assert module.REDUCED_SIZES["12K"] == (12288, 6480)
    assert module.REDUCED_SIZES["16K"] == (16384, 8640)


def test_benchmark_report_validator_accepts_minimal_image_report(tmp_path: Path) -> None:
    report = {
        "schema_version": 1,
        "environment": {"os": "test", "python": "3", "machine": "test", "logical_cpu_count": 1, "build_type": "debug", "git_commit": "test"},
        "source": {"path": "fixture.toos", "sha256": "0" * 64, "bytes": 1, "width": 1, "height": 1},
        "profile": {"image_profile": 1, "name": "hq", "pixel_format": 2, "bit_depth": 10, "plane_count": 3, "alpha_mode": 0, "alpha_bit_depth": 0, "color_range": 1, "color_primaries": 1, "color_transfer": 1, "color_matrix": 0},
        "threads": {"requested": 1}, "cache_state": "hot", "quality": {"status": "not_measured"},
        "stages": [{"name": f"stage-{i}", "memory_peak_mb": 0, "throughput_fps": 1, "latency_ms": {k: 1 for k in ("p50", "p95", "p99", "max", "mean")}} for i in range(7)],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(VALIDATOR), str(path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "valid image" in proc.stdout


def test_benchmark_report_validator_accepts_minimal_reduced_report(tmp_path: Path) -> None:
    run = {
        "status": "ok", "cache_state": "cold", "iterations": 1,
        "latency_ms": {key: 1 for key in ("p50", "p95", "p99", "max", "mean")},
        "output_bytes": 4,
        "telemetry": {
            "packet_bytes_read": 4, "segments_parsed": 1,
            "segments_skipped": 0, "entropy_symbols": 2,
            "coefficients_skipped": 0, "idct_samples": 1,
            "upload_bytes": 4, "frames": 1, "wall_ns": 1,
            "p50_ns": 1, "p95_ns": 1, "p99_ns": 1,
        },
    }
    report = {
        "schema_version": 1, "benchmark": "topos_reduced_decode",
        "generated_at_utc": "2026-01-01T00:00:00+00:00",
        "environment": {"os": "test", "python": "3", "machine": "test",
                        "logical_cpu_count": 1, "build_type": "debug", "git_commit": "test"},
        "source": {"path": "frame.toos", "width": 2, "height": 2,
                   "plane_count": 3, "pixel_format": 0, "bit_depth": 10,
                   "packet_sha256": "0" * 64, "packet_bytes": 4},
        "request": {"scale_divisor": 2, "scale": 1, "target_width": 1, "target_height": 1},
        "threads": {"requested": 1, "effective": 1},
        "cache_policy": {"cold": "test", "hot": "test"},
        "modes": [{"name": "legacy_reduced", "cold": run, "hot": dict(run, cache_state="hot")}],
    }
    path = tmp_path / "reduced.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(VALIDATOR), str(path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "valid reduced_decode" in proc.stdout


def test_reduced_benchmark_runner_exposes_all_comparison_modes() -> None:
    import importlib.util

    path = REPO / "scripts" / "bench_topos_reduced_decode.py"
    spec = importlib.util.spec_from_file_location("bench_topos_reduced_decode", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.MODE_NAMES == ("full", "post_scale", "legacy_reduced", "v7_band", "v7_base")
    assert module.SCALE_BY_DIVISOR == {2: 1, 3: 2, 4: 3, 8: 4}
    assert module._percentiles([1_000_000, 2_000_000])["p50"] == 1.0


def test_benchmark_threads_are_applied_to_native_subprocess() -> None:
    import importlib.util

    path = REPO / "scripts" / "bench_topos_image.py"
    spec = importlib.util.spec_from_file_location("bench_topos_image", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._thread_env(4) == {"TOPOS_SLICE_THREADS": "4"}
    assert module._thread_env(0) is None
