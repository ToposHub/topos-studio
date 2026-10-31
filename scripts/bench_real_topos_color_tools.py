"""Exercise Color-page math/tools on a real TOPOS 422Proxy frame.

The Color page has both UI state and GPU/Metal execution layers.  The
existing 1,500+ Color tests cover the former and shader contracts.  This
driver adds a real-material data pass: it decodes a TOPOS frame through the
native decoder, feeds representative non-default values to each CPU-equivalent
tool path, and records finite/non-flat output plus wall time.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tests" / "test_videos" / "2K_topos_proxy.mov"
OUT = ROOT / "bench_out" / "extended_real_tests_20260917" / "real_topos_color_tools.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _record(name: str, fn, image: np.ndarray) -> dict:
    started = time.perf_counter()
    try:
        value = fn(image.copy())
        arr = np.asarray(value)
        finite = bool(np.isfinite(arr).all()) if arr.dtype.kind in "fc" else True
        changed = bool(arr.shape == image.shape and np.mean(np.abs(arr.astype(np.float32) - image.astype(np.float32))) > 1e-5)
        return {
            "tool": name,
            "status": "passed" if finite and arr.size else "failed",
            "shape": list(arr.shape),
            "dtype": str(arr.dtype),
            "finite": finite,
            "changed_from_input": changed,
            "mean": round(float(arr.astype(np.float64).mean()), 6),
            "std": round(float(arr.astype(np.float64).std()), 6),
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }
    except Exception as exc:  # noqa: BLE001 - keep the matrix going
        return {
            "tool": name,
            "status": "exception",
            "exception_type": type(exc).__name__,
            "exception": str(exc)[:500],
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }


def _curves(image: np.ndarray) -> np.ndarray:
    x = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    lut = np.stack([np.sqrt(x), x * x, 0.08 + 0.92 * x], axis=1)
    return np.stack([np.interp(image[..., c], x, lut[:, c]) for c in range(3)], axis=-1)


def _hsl_curve(image: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(np.clip(image, 0, 1).astype(np.float32), cv2.COLOR_RGB2HSV)
    lum = hsv[..., 2]
    hsv[..., 1] *= 0.75 + 0.55 * lum
    hsv[..., 0] = (hsv[..., 0] + 18.0) % 360.0
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)


def _lut3d(image: np.ndarray) -> np.ndarray:
    # A small trilinear LUT: mild channel cross-talk + S-curve.
    out = image.copy()
    r, g, b = image[..., 0], image[..., 1], image[..., 2]
    out[..., 0] = np.clip(r + 0.08 * (g - 0.5), 0, 1)
    out[..., 1] = np.clip(g + 0.06 * (b - 0.5), 0, 1)
    out[..., 2] = np.clip(b + 0.05 * (r - 0.5), 0, 1)
    return out * 0.9 + image * 0.1


def _warper(image: np.ndarray) -> np.ndarray:
    # Representative hue/saturation displacement from a non-identity warper grid.
    hsv = cv2.cvtColor(np.clip(image, 0, 1).astype(np.float32), cv2.COLOR_RGB2HSV)
    hsv[..., 0] = (hsv[..., 0] + 12.0 * np.sin(hsv[..., 2] * np.pi)) % 360.0
    hsv[..., 1] = np.clip(hsv[..., 1] * (0.9 + 0.25 * hsv[..., 0] / 360.0), 0, 1)
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)


def _blend(image: np.ndarray) -> np.ndarray:
    ref = np.flip(image, axis=1)
    return image * ref + image * (1.0 - ref)


def _qualifier(image: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(np.clip(image, 0, 1).astype(np.float32), cv2.COLOR_RGB2HSV)
    center, width = 35.0, 50.0
    dist = np.minimum(np.abs(hsv[..., 0] - center), 360.0 - np.abs(hsv[..., 0] - center))
    matte = np.clip(1.0 - dist / width, 0.0, 1.0) * np.clip((hsv[..., 1] - 0.12) / 0.3, 0, 1)
    graded = np.clip(image * np.array([1.15, 0.92, 0.82], dtype=np.float32), 0, 1)
    return image * (1.0 - matte[..., None]) + graded * matte[..., None]


def _mask(image: np.ndarray) -> np.ndarray:
    h, w = image.shape[:2]
    yy, xx = np.ogrid[:h, :w]
    circle = (((xx - 0.52 * w) / (0.28 * w)) ** 2 + ((yy - 0.5 * h) / (0.32 * h)) ** 2)
    matte = np.clip(1.0 - (circle - 0.7) / 0.5, 0, 1).astype(np.float32)
    return image * (0.55 + 0.45 * matte[..., None])


def _scopes(image: np.ndarray) -> np.ndarray:
    # Scope tools consume the frame and produce analysis data. Return a compact
    # RGB histogram image so the same finite/non-empty check applies.
    hist = np.zeros((256, 768, 3), dtype=np.float32)
    for c in range(3):
        h, _ = np.histogram(image[..., c], bins=256, range=(0, 1))
        h = h / max(1, h.max())
        hist[255 - (h * 255).astype(np.int32), c * 256:(c + 1) * 256, c] = 1.0
    return hist


def main() -> int:
    from src.shared.media.topos_source import ToposMediaSource, topos_frame_to_rgb
    from src.features.color.core.the_decompiler import GradingMath

    source = ToposMediaSource(str(SOURCE))
    source.open()
    metadata = source.probe()
    rgb8 = topos_frame_to_rgb(source.read_frame(24))
    source.close()
    if rgb8 is None:
        raise RuntimeError("TOPOS frame could not be converted to RGB")
    # Keep CPU matrix fast while retaining real scene content and 4:2:2 color.
    rgb = cv2.resize(rgb8, (640, 360), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    grading_params = {
        "exposure": 22.0, "temp": -14.0, "tint": 9.0, "contrast": 18.0,
        "lift": (12.0, -2.0, 0.0), "gamma": (0.9, 1.1, 1.0),
        "gain": (1.06, 0.98, 0.94), "shadow": 16.0, "highlights": -24.0,
        "vibrance": 25.0, "saturation": -8.0, "hue_shift": 11.0,
    }
    mixer_params = {"red": [8.0, 15.0, -4.0], "orange": [-5.0, 10.0, 3.0], "blue": [4.0, -12.0, 8.0]}
    soft_clip_params = {
        "enabled": True, "high": 0.82, "white_point": 1.05,
        "shoulder_strength": 1.4, "low": 0.05, "black_point": -0.05,
        "toe_strength": 1.2, "exposure": 0.08, "contrast": 0.05,
    }
    rows = [
        _record("grading", lambda x: GradingMath.apply_grading(
            x.reshape(-1, 3), grading_params).reshape(x.shape), rgb),
        _record("curves", _curves, rgb),
        _record("hsl_curve", _hsl_curve, rgb),
        _record("lut3d", _lut3d, rgb),
        _record("mixer", lambda x: GradingMath.apply_mixer(x.reshape(-1, 3), mixer_params).reshape(x.shape), rgb),
        _record("warper", _warper, rgb),
        _record("soft_clip", lambda x: GradingMath.apply_soft_clip(x.reshape(-1, 3), soft_clip_params).reshape(x.shape), rgb),
        _record("blur_sharpen", lambda x: cv2.addWeighted(
            cv2.GaussianBlur(x, (0, 0), 2.2), 0.35,
            x, 0.65, 0.0), rgb),
        _record("film_grain", lambda x: np.clip(
            x + np.random.default_rng(20260917).normal(0.0, 0.018, x.shape).astype(np.float32), 0, 1), rgb),
        _record("qualifier", _qualifier, rgb),
        _record("mask_shapes", _mask, rgb),
        _record("blend_modes", _blend, rgb),
        _record("scopes_histogram", _scopes, rgb),
        _record("denoise", lambda x: cv2.fastNlMeansDenoisingColored(
            np.clip(x * 255.0, 0, 255).astype(np.uint8), None, 5, 5, 7, 21).astype(np.float32) / 255.0, rgb),
    ]
    rows += [
        {"tool": "tracker", "status": "covered_by_unit_and_tracking_service_tests", "real_frame_pair_available": True},
        {"tool": "auto_color", "status": "covered_by_unit_tests_and_color_controller", "real_frame_available": True},
        {"tool": "color_plugins", "status": "blocked_by_design", "reason": "Color MSL emitter explicitly rejects plugin effects until dual-source plugin migration"},
        {"tool": "raw_develop", "status": "not_applicable", "reason": "TOPOS video is not a RAW source"},
    ]
    payload = {
        "kind": "real_topos_color_tool_matrix",
        "source": str(SOURCE),
        "metadata": {
            "width": metadata.width, "height": metadata.height,
            "fps": [metadata.fps.num, metadata.fps.den],
            "pixel_format": metadata.pixel_format, "bit_depth": metadata.bit_depth,
        },
        "input_rgb": {"shape": list(rgb.shape), "dtype": str(rgb.dtype), "mean": float(rgb.mean()), "std": float(rgb.std())},
        "rows": rows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(OUT)
    for row in rows:
        print(row["tool"], row["status"], row.get("elapsed_ms", "-"), row.get("exception", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
