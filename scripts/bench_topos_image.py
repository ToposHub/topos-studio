#!/usr/bin/env python3
"""Run the reproducible Topos Image benchmark protocol.

The runner deliberately keeps the stages separate.  Native ``toos`` is used
for file probing, validation and codec decode; conversion, resize and WebP
encoding are measured in this process so their cost is not hidden in the
codec number.  The output is JSON and is validated against
``docs/image/benchmark_schema.json`` without requiring a third-party schema
package.

Example::

    python scripts/bench_topos_image.py tests/conformance/golden_image_v1_gbr10.bin \
        --toos native/topos_codec/build/release/toos --iters 5 \
        --cache-state hot --output /tmp/topos-image-benchmark.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    import numpy as np
except ImportError as exc:  # pragma: no cover - exercised by CLI diagnostics
    raise SystemExit("bench_topos_image.py requires numpy") from exc

try:
    from PIL import Image
except ImportError as exc:  # pragma: no cover - exercised by CLI diagnostics
    raise SystemExit("bench_topos_image.py requires Pillow") from exc

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is optional on SDK hosts
    psutil = None

REPO = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO / "docs/image/benchmark_schema.json"


def _find_toos(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).resolve()
        if not path.is_file():
            raise SystemExit(f"toos CLI does not exist: {path}")
        return path
    for config in ("release", "debug", "intra-release"):
        path = REPO / f"native/topos_codec/build/{config}/toos"
        if path.is_file():
            return path
    raise SystemExit("could not find toos; pass --toos or build native/topos_codec")


def _percentile(values: Iterable[float], fraction: float) -> float:
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(fraction * (len(ordered) - 1)))
    return ordered[index]


def _stats(samples_ms: list[float]) -> dict[str, float]:
    if not samples_ms:
        raise ValueError("benchmark stage produced no samples")
    return {
        "p50": _percentile(samples_ms, 0.50),
        "p95": _percentile(samples_ms, 0.95),
        "p99": _percentile(samples_ms, 0.99),
        "max": max(samples_ms),
        "mean": statistics.fmean(samples_ms),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _rss_tree_mb(process: Any) -> float:
    if psutil is None:
        return 0.0
    try:
        total = process.memory_info().rss
        total += sum(child.memory_info().rss for child in process.children(recursive=True))
        return total / (1024.0 * 1024.0)
    except (psutil.Error, OSError):
        return 0.0


def _run_timed(
    command: list[str], extra_env: dict[str, str] | None = None,
) -> tuple[float, float, str, str]:
    started = time.perf_counter()
    environment = os.environ.copy()
    if extra_env:
        environment.update(extra_env)
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env=environment)
    process = psutil.Process(child.pid) if psutil is not None else None
    peak_mb = 0.0
    while child.poll() is None:
        if process is not None:
            peak_mb = max(peak_mb, _rss_tree_mb(process))
        time.sleep(0.002)
    stdout, stderr = child.communicate()
    if process is not None:
        peak_mb = max(peak_mb, _rss_tree_mb(process))
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if child.returncode != 0:
        detail = (stdout + stderr).strip()
        raise RuntimeError(f"command failed ({child.returncode}): {' '.join(command)}\n{detail}")
    return elapsed_ms, peak_mb, stdout, stderr


def _parse_probe(stdout: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw_line in stdout.splitlines():
        if ":" not in raw_line:
            continue
        key, value = (part.strip() for part in raw_line.split(":", 1))
        if key == "size":
            result["width"], result["height"] = (int(part.strip()) for part in value.split("x"))
        elif key in {"file_size", "bit_depth", "image_profile"}:
            result[key] = int(value.split()[0])
        elif key == "codec":
            fields = dict(part.split("=", 1) for part in value.split() if "=" in part)
            result["codec_profile"] = int(fields.get("profile", 0))
            result["pixel_format"] = int(fields.get("pixel_format", 0))
        elif key == "planes":
            result["plane_count"] = int(value.split()[0])
        elif key == "alpha":
            fields = dict(part.split("=", 1) for part in value.split() if "=" in part)
            result["alpha_mode"] = int(fields.get("mode", 0))
            result["alpha_bit_depth"] = int(fields.get("depth", 0))
        elif key == "color":
            fields = dict(part.split("=", 1) for part in value.split() if "=" in part)
            result.update({name: int(fields.get(name, 0)) for name in
                           ("range", "primaries", "transfer", "matrix")})
        elif key == "pixl":
            fields = dict(part.split("=", 1) for part in value.split() if "=" in part)
            result["pixl_offset"] = int(fields.get("offset", 0))
            result["pixl_size"] = int(fields.get("size", 0))
    required = ("width", "height", "file_size", "bit_depth", "pixel_format", "plane_count")
    missing = [key for key in required if key not in result]
    if missing:
        raise RuntimeError(f"could not parse toos probe output; missing {missing}: {stdout}")
    return result


def _stage(
    command: list[str], samples: int, extra_env: dict[str, str] | None = None,
) -> tuple[dict[str, float], float]:
    timings: list[float] = []
    peak = 0.0
    for _ in range(samples):
        elapsed, rss, _, _ = _run_timed(command, extra_env=extra_env)
        timings.append(elapsed)
        peak = max(peak, rss)
    return _stats(timings), peak


def _throughput_fps(latency: dict[str, float]) -> float:
    return 1000.0 / latency["p50"] if latency["p50"] > 0.0 else 0.0


def _thread_env(requested: int) -> dict[str, str] | None:
    """Apply the benchmark's requested native slice-thread budget.

    The previous O0 runner only recorded ``--threads``.  O4 needs the matrix
    to actually exercise the native scheduler, while ``0`` keeps the library
    default untouched.
    """
    return {"TOPOS_SLICE_THREADS": str(requested)} if requested > 0 else None


def _read_sidecar(raw_path: Path) -> dict[str, Any]:
    sidecar = Path(f"{raw_path}.json")
    return json.loads(sidecar.read_text(encoding="utf-8"))


def _load_planes(raw_path: Path, sidecar: dict[str, Any]) -> list[np.ndarray]:
    values = np.fromfile(raw_path, dtype="<u2")
    width, height = int(sidecar["width"]), int(sidecar["height"])
    pixel_format = int(sidecar["pixel_format"])
    chroma_width = (width + 1) // 2 if pixel_format == 0 else width
    plane_shapes = [(height, width), (height, chroma_width), (height, chroma_width)] \
        if pixel_format == 0 else [(height, width)] * 3
    if ",A" in str(sidecar.get("channels", "")):
        plane_shapes.append((height, width))
    planes: list[np.ndarray] = []
    offset = 0
    for shape in plane_shapes:
        size = shape[0] * shape[1]
        end = offset + size
        if end > values.size:
            raise RuntimeError(f"decode sidecar exceeds raw payload: {raw_path}")
        planes.append(values[offset:end].reshape(shape))
        offset = end
    if offset != values.size:
        raise RuntimeError(f"decode sidecar leaves {values.size - offset} samples: {raw_path}")
    return planes


def _to_rgb8(planes: list[np.ndarray], sidecar: dict[str, Any]) -> np.ndarray:
    bit_depth = int(sidecar["bit_depth"])
    pixel_format = int(sidecar["pixel_format"])
    if pixel_format == 2:
        g, b, r = planes[:3]
        rgb = np.stack([r, g, b], axis=-1)
    else:
        from src.shared.color.types import ColorMatrix, ColorRange
        from src.shared.media.color_decode import ColorConverter

        if planes[1].shape != planes[0].shape:
            u = np.repeat(planes[1], 2, axis=1)[:, :planes[0].shape[1]]
            v = np.repeat(planes[2], 2, axis=1)[:, :planes[0].shape[1]]
        else:
            u, v = planes[1], planes[2]
        matrix_map = {0: ColorMatrix.GBR, 1: ColorMatrix.BT709,
                      5: ColorMatrix.BT601_PAL, 6: ColorMatrix.BT601,
                      9: ColorMatrix.BT2020_NC}
        range_map = {0: ColorRange.LIMITED, 1: ColorRange.FULL}
        matrix = matrix_map.get(int(sidecar.get("color_matrix", 1)), ColorMatrix.BT709)
        if matrix is ColorMatrix.GBR:
            raise RuntimeError("YUV file declares GBR matrix")
        rgb = ColorConverter().yuv_to_rgb(
            planes[0], u, v, matrix, range_map.get(int(sidecar.get("color_range", 1)),
                                                   ColorRange.FULL), bit_depth)
        rgb = np.clip(rgb, 0.0, 1.0) * 255.0
    scale = 255.0 / float((1 << bit_depth) - 1)
    if pixel_format == 2:
        rgb = np.clip(rgb.astype(np.float32) * scale, 0.0, 255.0)
    output = np.rint(rgb).astype(np.uint8)
    if len(planes) > 3:
        alpha = np.clip(planes[3].astype(np.float32) * (255.0 / 65535.0), 0.0, 255.0)
        output = np.concatenate([output, np.rint(alpha)[..., None].astype(np.uint8)], axis=-1)
    return output


def _resize(rgb: np.ndarray, target: int) -> np.ndarray:
    height, width = rgb.shape[:2]
    if max(width, height) <= target:
        return rgb
    ratio = target / float(max(width, height))
    size = (max(1, round(width * ratio)), max(1, round(height * ratio)))
    mode = "RGBA" if rgb.shape[-1] == 4 else "RGB"
    return np.asarray(Image.fromarray(rgb, mode=mode).resize(size, Image.Resampling.LANCZOS))


def _webp_encode(rgb: np.ndarray, quality: int) -> bytes:
    mode = "RGBA" if rgb.shape[-1] == 4 else "RGB"
    image = Image.fromarray(rgb, mode=mode)
    from io import BytesIO
    out = BytesIO()
    image.save(out, format="WEBP", quality=quality, method=4)
    return out.getvalue()


def _environment() -> dict[str, Any]:
    cpu_count = os.cpu_count() or 1
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                                check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"
    return {
        "os": platform.platform(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "processor": platform.processor() or platform.machine(),
        "logical_cpu_count": cpu_count,
        "physical_cpu_count": psutil.cpu_count(logical=False) if psutil else None,
        "memory_total_mb": round(psutil.virtual_memory().total / (1024 * 1024), 2)
        if psutil else None,
        "compiler": "native toos (external CLI)",
        "build_type": "unknown",
        "git_commit": commit or "unknown",
    }


def _validate_report(report: dict[str, Any]) -> None:
    required = {"schema_version", "environment", "source", "profile", "threads",
                "cache_state", "stages", "quality"}
    missing = sorted(required - report.keys())
    if missing:
        raise ValueError(f"benchmark report missing required fields: {missing}")
    for stage in report["stages"]:
        for key in ("latency_ms", "throughput_fps", "memory_peak_mb"):
            if key not in stage:
                raise ValueError(f"stage {stage.get('name')} missing {key}")
        for key in ("p50", "p95", "p99", "max", "mean"):
            if key not in stage["latency_ms"]:
                raise ValueError(f"stage {stage.get('name')} missing latency {key}")


def run(args: argparse.Namespace) -> dict[str, Any]:
    source = Path(args.input).resolve()
    if not source.is_file():
        raise SystemExit(f"input does not exist: {source}")
    toos = _find_toos(args.toos)
    native_env = _thread_env(args.threads)
    with tempfile.TemporaryDirectory(prefix="topos-image-bench-") as temp_dir:
        temp = Path(temp_dir)
        probe_out, probe_rss, probe_stdout, _ = _run_timed(
            [str(toos), "probe", str(source)], extra_env=native_env,
        )
        info = _parse_probe(probe_stdout)
        probe_stats = _stats([probe_out])
        stages: list[dict[str, Any]] = [{
            "name": "probe", "latency_ms": probe_stats,
            "throughput_fps": _throughput_fps(probe_stats),
            "memory_peak_mb": probe_rss,
        }]
        validate, validate_rss = _stage(
            [str(toos), "verify", str(source), "--deep"], args.iters,
            extra_env=native_env,
        )
        stages.append({"name": "validate", "latency_ms": validate,
                       "throughput_fps": _throughput_fps(validate), "memory_peak_mb": validate_rss})

        read_samples: list[float] = []
        payload = b""
        for _ in range(args.iters):
            started = time.perf_counter()
            with source.open("rb") as handle:
                payload = handle.read()
            read_samples.append((time.perf_counter() - started) * 1000.0)
        read_stats = _stats(read_samples)
        stages.append({"name": "read_payload", "latency_ms": read_stats,
                       "throughput_fps": _throughput_fps(read_stats), "memory_peak_mb": 0.0,
                       "output_bytes": len(payload)})

        raw = temp / "decoded.raw"
        decode, decode_rss = _stage(
            [str(toos), "decode", str(source), "-o", str(raw)], args.iters,
            extra_env=native_env,
        )
        sidecar = _read_sidecar(raw)
        stages.append({"name": "codec_decode", "latency_ms": decode,
                       "throughput_fps": _throughput_fps(decode), "memory_peak_mb": decode_rss})
        planes = _load_planes(raw, sidecar)
        convert_samples: list[float] = []
        rgb = np.empty((1, 1, 3), dtype=np.uint8)
        for _ in range(args.iters):
            started = time.perf_counter()
            rgb = _to_rgb8(planes, sidecar)
            convert_samples.append((time.perf_counter() - started) * 1000.0)
        convert_stats = _stats(convert_samples)
        stages.append({"name": "color_convert", "latency_ms": convert_stats,
                       "throughput_fps": _throughput_fps(convert_stats), "memory_peak_mb": 0.0})
        resize_samples: list[float] = []
        resized = rgb
        for _ in range(args.iters):
            started = time.perf_counter()
            resized = _resize(rgb, args.target_size)
            resize_samples.append((time.perf_counter() - started) * 1000.0)
        resize_stats = _stats(resize_samples)
        stages.append({"name": "resize", "latency_ms": resize_stats,
                       "throughput_fps": _throughput_fps(resize_stats), "memory_peak_mb": 0.0})
        webp_samples: list[float] = []
        encoded = b""
        for _ in range(args.iters):
            started = time.perf_counter()
            encoded = _webp_encode(resized, args.webp_quality)
            webp_samples.append((time.perf_counter() - started) * 1000.0)
        webp_stats = _stats(webp_samples)
        stages.append({"name": "webp_encode", "latency_ms": webp_stats,
                       "throughput_fps": _throughput_fps(webp_stats), "memory_peak_mb": 0.0,
                       "output_bytes": len(encoded)})

    profile_name = {0: "preview", 1: "hq", 2: "xq"}.get(info.get("image_profile"), "unknown")
    report = {
        "schema_version": 1,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": _environment(),
        "source": {"path": str(source), "sha256": _sha256(source), "bytes": source.stat().st_size,
                   "width": info["width"], "height": info["height"]},
        "profile": {"image_profile": info.get("image_profile", -1), "name": profile_name,
                    "pixel_format": info["pixel_format"], "bit_depth": info["bit_depth"],
                    "plane_count": info["plane_count"], "alpha_mode": info.get("alpha_mode", 0),
                    "alpha_bit_depth": info.get("alpha_bit_depth", 0),
                    "color_range": info.get("range", -1), "color_primaries": info.get("primaries", -1),
                    "color_transfer": info.get("transfer", -1), "color_matrix": info.get("matrix", -1)},
        "threads": {
            "requested": args.threads,
            "env": (native_env or {}).get("TOPOS_SLICE_THREADS",
                     os.environ.get("TOPOS_SLICE_THREADS")),
            "applied_to_native_subprocess": native_env is not None,
        },
        "cache_state": args.cache_state,
        "target_size": args.target_size,
        "webp_quality": args.webp_quality,
        "iterations": args.iters,
        "stages": stages,
        "quality": {"status": "not_measured", "reference": None,
                    "note": "O0 timing runner does not claim PSNR/SSIM/DeltaE."},
    }
    _validate_report(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help=".toos input")
    parser.add_argument("--toos", help="path to native toos CLI")
    parser.add_argument("--iters", type=int, default=5, help="number of timed iterations")
    parser.add_argument("--threads", type=int, default=0,
                        help="recorded thread budget; does not mutate the parent process")
    parser.add_argument("--build-type", choices=("debug", "release", "unknown"), default="unknown")
    parser.add_argument("--cache-state", choices=("cold", "hot", "unknown"), default="hot")
    parser.add_argument("--target-size", type=int, default=300)
    parser.add_argument("--webp-quality", type=int, default=85)
    parser.add_argument("--output", type=Path, help="write JSON report; stdout remains JSON too")
    args = parser.parse_args()
    if args.iters < 1 or args.target_size < 1 or not 0 <= args.webp_quality <= 100:
        parser.error("--iters and --target-size must be positive; WebP quality must be 0..100")
    report = run(args)
    report["environment"]["build_type"] = args.build_type
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
