"""Per-effect Edit export matrix using the native TOPOS 422Proxy source.

This is intentionally a small, reproducible acceptance driver rather than a
unit test.  Each effect is run in a fresh child process so one native/GPU
failure cannot hide the result of the remaining effects.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tests" / "test_videos" / "2K_topos_proxy.mov"
RESULT_DIR = ROOT / "bench_out" / "extended_real_tests_20260917"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _non_default_params(definition: Any) -> dict[str, Any]:
    """Choose deterministic non-default values from the public schema."""
    params = {name: schema.default for name, schema in definition.params_schema.items()}
    overrides: dict[str, Any] = {
        "blur_type": "gaussian",
        "radius": 8.0,
        "radius_x": 8.0,
        "radius_y": 8.0,
        "amount": 1.2,
        "strength": 0.65,
        "threshold": 0.25,
        "threshold_high": 0.75,
        "iterations": 2,
        "size": 3,
        "max_radius": 12.0,
        "intensity": 1.25,
        "mix": 0.7,
        "smooth_radius": 5.0,
        "detail_preserve": 0.75,
        "mix_amount": 0.65,
        "blur_radius": 4.0,
        "enable_tint": True,
    }
    for name, value in overrides.items():
        if name in params:
            params[name] = value
    return params


def _run_one(effect_type: str, output: Path) -> dict[str, Any]:
    from scripts.bench_export_effects_matrix import build_scenario
    from src.features.edit.core.effect_registry import (
        NON_ATTACHABLE_VIDEO_EFFECT_TYPES,
        get_effect_registry,
    )
    from src.features.edit.core.timeline_export import create_timeline_exporter
    from src.features.shared.effects import get_effect_catalog
    from src.features.shared.effects.plugin_identity import build_plugin_identity

    started = time.perf_counter()
    registry = get_effect_registry()
    definition = next(
        (item for item in registry.get_all_definitions()
         if item.effect_type == effect_type), None
    )
    if definition is None:
        return {"effect_type": effect_type, "status": "not_registered"}

    result: dict[str, Any] = {
        "effect_type": effect_type,
        "category": definition.category,
        "params": _non_default_params(definition),
        "source": str(SOURCE),
        "source_codec": "TOPOS 422Proxy / yuv422p10le / 10-bit",
    }

    # These four legacy entries are deliberately not attachable video
    # filters.  Test their classification explicitly so the matrix does not
    # misreport a design exclusion as a runtime failure.
    if effect_type in NON_ATTACHABLE_VIDEO_EFFECT_TYPES:
        result.update(status="not_attachable_by_design", reason="clip-native or legacy ghost effect")
        return result

    # The matrix focuses on video entries. Audio and generator entries are
    # covered by their dedicated edit/audio test suites and are not silently
    # treated as video effects here.
    if definition.category in {"audio", "generator"}:
        result.update(status="separate_domain", reason=definition.category)
        return result

    meta = {"fps": 25.0, "width": 1920, "height": 1080, "total_frames": 96}
    project, sequence, config = build_scenario(
        "identity", "h264", "h264", 8, 20_000_000, output, meta
    )
    asset = project.assets["bench_src"].with_path(str(SOURCE))
    project = replace(project, assets={"bench_src": asset})
    clip = sequence.video_tracks[0].clips[0]
    effect = definition.create_instance(order=0)
    effect = replace(effect, params=result["params"])
    if effect_type.startswith("plugin:"):
        spec = get_effect_catalog().get_spec(effect_type)
        if spec is None:
            result.update(status="plugin_missing_from_catalog")
            return result
        effect = replace(effect, plugin_identity=build_plugin_identity(spec).to_dict())
        result["plugin_identity"] = "active_catalog_identity"
    clip = replace(clip, effects=(effect,))
    track = sequence.video_tracks[0]
    sequence = replace(sequence, video_tracks=(replace(track, clips=(clip,)),))

    try:
        exporter = create_timeline_exporter(project, sequence)
        outcome = exporter.export_timeline(config)
        result.update(
            status="passed" if bool(outcome) and output.exists() else "export_failed",
            success=bool(outcome),
            error_code=str(getattr(outcome, "error_code", "")),
            cause=str(getattr(outcome, "cause", ""))[:500],
            elapsed_s=round(time.perf_counter() - started, 3),
            output_bytes=output.stat().st_size if output.exists() else 0,
            hw_fallback_reason=str(getattr(outcome, "hw_fallback_reason", "")),
            trail_fallback_reason=str(
                getattr(getattr(exporter, "gpu_trail_snapshot", None), "fallback_reason", "") or ""
            ),
        )
    except Exception as exc:  # noqa: BLE001 - matrix must record the item
        result.update(
            status="exception",
            exception_type=type(exc).__name__,
            exception=str(exc)[:1000],
            elapsed_s=round(time.perf_counter() - started, 3),
        )
    finally:
        if output.exists():
            output.unlink()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--effect", default="")
    parser.add_argument("--child", action="store_true")
    args = parser.parse_args()
    RESULT_DIR.mkdir(parents=True, exist_ok=True)

    from src.features.edit.core.effect_registry import get_effect_registry

    effects = [d.effect_type for d in get_effect_registry().get_all_definitions()]
    if args.effect:
        effects = [args.effect]
    if args.child:
        output = RESULT_DIR / f"topos_edit_effect_{args.effect.replace(':', '_')}.mp4"
        print(json.dumps(_run_one(args.effect, output), ensure_ascii=False))
        return 0

    rows: list[dict[str, Any]] = []
    for effect in effects:
        cmd = [sys.executable, str(Path(__file__).resolve()), "--child", "--effect", effect]
        env = dict(os.environ)
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        env.setdefault("TOPOS_EXPORT_GPU_RENDER", "0")
        try:
            proc = subprocess.run(
                cmd, cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120
            )
            line = next((line for line in reversed(proc.stdout.splitlines()) if line.strip()), "")
            row = json.loads(line) if line else {
                "effect_type": effect, "status": "child_no_json",
                "stderr": proc.stderr[-2000:], "returncode": proc.returncode,
            }
            if proc.returncode != 0:
                row.setdefault("child_returncode", proc.returncode)
                row.setdefault("child_stderr", proc.stderr[-2000:])
        except subprocess.TimeoutExpired:
            row = {"effect_type": effect, "status": "timeout", "timeout_s": 120}
        rows.append(row)
        print(f"{effect}: {row.get('status')}", flush=True)
    payload = {
        "kind": "real_topos_edit_effect_matrix",
        "source": str(SOURCE),
        "native_codec": os.environ.get("TOPOS_CODEC_LIB", ""),
        "rows": rows,
    }
    path = RESULT_DIR / "real_topos_edit_effect_matrix.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(path)
    return 0 if all(row.get("status") in {"passed", "not_attachable_by_design", "separate_domain"} for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
