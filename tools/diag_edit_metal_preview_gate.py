#!/usr/bin/env python3
"""Which gate keeps the Edit preview off the Metal path?

``EditGpuPreviewRenderer._try_metal_preview`` decides per frame whether an
eligible single-layer VideoToolbox frame is converted by Metal.  When no frame
ever reaches it, the only visible symptom is a playback trace that reports
``opengl`` for every frame.  This diagnostic wraps the gate for one benchmark
playback session, evaluates its conditions in the same order and counts the
first condition each frame failed (or that it passed), then writes the tally.

It changes no behaviour: the original gate still runs and decides.

Run from the repository root against an A/B directory built by
``bench_metal_ab_topos.py``::

    TOPOS_BACKEND_GRAPHICS=metal PYTHONPATH=. .venv/bin/python \\
        tools/diag_edit_metal_preview_gate.py --ab-dir bench_out/metal-ab.<date> \\
        --output docs/architecture/macos_metal_migration/evidence/metal-preview-gate.json
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Counter, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.bench_metal_ab_topos import ROOT  # noqa: E402
from tools.bench_metal_ab_playback import (  # noqa: E402
    clean_stale_session_files, rotate_previous_records,
)

logger = logging.getLogger(__name__)

# Order matters: the tally names the first condition a frame failed.
GATES: List[str] = [
    "mode_off",
    "effect_hdr",
    "background_task",
    "layer_count",
    "adjustment_or_transition",
    "frame_type",
    "no_pixel_buffer",
    "pixel_format_or_colour",
    "source_dimensions",
    "opacity",
    "effects_transform_crop",
    "grade_not_lut_only",
    "lut_mode_without_grade",
]
REACHED_METAL = "rendered_by_metal"
FELL_BACK = "metal_error_fallback"


def evaluate_gates(task: Any, mode: str) -> Optional[str]:
    """Mirror of ``_try_metal_preview``'s conditions; first failure or ``None``."""
    from src.features.edit.core.crop_cpu import crop_is_identity
    from src.features.edit.core.edit_gpu_preview_renderer import (
        _transform_tuple_is_identity,
    )

    if mode == "off":
        return "mode_off"
    if len(task.layers) != 1:
        return "layer_count"
    if task.adjustment_layers or task.transition_jobs:
        return "adjustment_or_transition"
    layer = task.layers[0]
    if layer.get("frame_type") != "vt_pixel_buffer":
        return "frame_type"
    source = layer.get("vt_pixel_buffer")
    if source is None or getattr(source, "pixel_buffer", None) is None:
        return "no_pixel_buffer"
    if (source.pixel_format != "nv12" or source.bit_depth != 8
            or source.color_matrix != "bt709" or source.color_range != "limited"
            or source.color_primaries not in (None, "bt709")
            or source.color_transfer not in (None, "bt709")):
        return "pixel_format_or_colour"
    if source.width <= 0 or source.height <= 0:
        return "source_dimensions"
    info = layer.get("track_info", {})
    transform = info.get("transform")
    if info.get("opacity", 1.0) != 1.0:
        return "opacity"
    if (info.get("clip_effects")
            or (transform and not _transform_tuple_is_identity(transform))
            or not crop_is_identity(info.get("crop"))):
        return "effects_transform_crop"
    grade = info.get("preview_grade")
    if grade:
        from src.features.color.core.color_graph_texture import serial_lut_only
        from src.shared.project.color_graph import ColorGraphData

        graph = grade.get("color_graph")
        if not isinstance(graph, ColorGraphData) or grade.get("chain_extra_params"):
            return "grade_not_lut_only"
        if serial_lut_only(graph) is None:
            return "grade_not_lut_only"
    elif mode == "lut":
        return "lut_mode_without_grade"
    return None


def _probe_key(*parts: str) -> str:
    """A JSON-safe key for a probe tally."""
    return " | ".join(parts)


def _note_diagnostic_error(details: Dict[str, Any], error: Exception) -> None:
    """Remember a failure of the probe itself; rendering is unaffected."""
    errors = details.setdefault("diagnostic_errors", collections.Counter())
    errors[f"{type(error).__name__}: {error}"] += 1


def install(tally: Counter, details: Dict[str, Any]) -> None:
    """Wrap the gate for the next renderer instance without changing it."""
    from src.features.edit.core import edit_gpu_preview_renderer as module

    original = module.EditGpuPreviewRenderer._try_metal_preview
    seen_types: Counter = collections.Counter()
    counter = [0]

    def wrapped(self: Any, task: Any, width: int, height: int) -> bool:
        # Nothing in this wrapper may escape into the renderer: only the real
        # gate is allowed to raise, exactly as it would without the probe.
        mode = getattr(self, "_metal_preview_mode", "off")
        details.setdefault("mode", mode)
        gate: Optional[str] = None
        try:
            for layer in task.layers:
                seen_types[str(layer.get("frame_type"))] += 1
            if getattr(self, "_effect_hdr", False):
                gate = "effect_hdr"
            elif task.background:
                gate = "background_task"
            else:
                gate = evaluate_gates(task, mode)
            if gate is not None:
                tally[gate] += 1
        except Exception as error:
            _note_diagnostic_error(details, error)
        used_metal = original(self, task, width, height)
        try:
            counter[0] += 1
            if used_metal:
                tally[REACHED_METAL] += 1
            elif gate is None:
                # Every gate passed but Metal did not render: the renderer fell
                # back after an error.
                tally[FELL_BACK] += 1
            if counter[0] % 100 == 0:
                # Does the playback trace agree with what the gate just did?
                from src.shared.video.playback_frame_trace import get_trace_collector

                summary = get_trace_collector().get_summary()
                details.setdefault("trace_probes", []).append({
                    "calls": counter[0],
                    "backend_counts": dict(summary.get("backend_counts") or {}),
                    "annotate": dict(summary.get("annotate") or {}),
                })
        except Exception as error:
            _note_diagnostic_error(details, error)
        return used_metal

    module.EditGpuPreviewRenderer._try_metal_preview = wrapped  # type: ignore[method-assign]
    details["frame_types"] = seen_types
    _install_trace_probes(getattr(module, "TimelinePreviewRenderer", None), details)


def _install_trace_probes(renderer_class: Any, details: Dict[str, Any]) -> None:
    """Compare what the descriptor declares with what the frame trace ends up saying."""

    def safe(label: str, probe: Callable[[], None]) -> None:
        try:
            probe()
        except Exception as error:
            _note_diagnostic_error(details, error)

    if renderer_class is None:
        from src.features.edit.core.timeline_preview_renderer import (
            TimelinePreviewRenderer as renderer_class,
        )
    # The trace is recorded by the preview worker class of the same module.
    import inspect

    module = inspect.getmodule(renderer_class)
    worker_class = next(
        (cls for cls in vars(module).values()
         if inspect.isclass(cls) and "_record_frame_trace" in vars(cls)),
        None,
    )
    if worker_class is None:
        raise RuntimeError("no preview worker class defines _record_frame_trace")
    original_ready = renderer_class._on_present_frame_ready
    original_record = worker_class._record_frame_trace

    def ready(self: Any, present_frame: object) -> None:
        # Observe only; the real handler decides everything.
        original_ready(self, present_frame)
        frame = int(getattr(present_frame, "frame", -1))

        def probe() -> None:
            from src.shared.video.playback_frame_trace import get_trace_collector

            declared = renderer_class._present_render_backend(present_frame)
            trace_id = self._lookup_frame_trace_id(frame)
            collector = get_trace_collector()
            matches = [t for t in collector.get_traces() if t.trace_id == trace_id]
            key = _probe_key(
                "declared=" + str(declared),
                "trace_id=" + ("0" if not trace_id else "set"),
                "traces_with_id=" + str(len(matches)),
                "trace_backend=" + ("/".join(sorted({t.backend for t in matches})) or "-"),
            )
            details.setdefault("present_probe", collections.Counter())[key] += 1

        safe("present_ready", probe)

    def record(self: Any, trace: Any) -> None:
        def probe() -> None:
            from src.shared.video.playback_frame_trace import get_trace_collector

            trace_id = int(getattr(trace, "trace_id", 0) or 0)
            already = any(
                t.trace_id == trace_id for t in get_trace_collector().get_traces()
            ) if trace_id else False
            key = _probe_key(
                "trace_id=" + ("0" if not trace_id else "set"),
                "already_recorded=" + str(already),
                "trace_backend=" + str(getattr(trace, "backend", "")),
            )
            details.setdefault("record_probe", collections.Counter())[key] += 1

        safe("record", probe)
        original_record(self, trace)

    renderer_class._on_present_frame_ready = ready  # type: ignore[method-assign]
    worker_class._record_frame_trace = record  # type: ignore[method-assign]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ab-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    ab_dir = (ROOT / args.ab_dir) if not args.ab_dir.is_absolute() else args.ab_dir
    rotate_previous_records(ab_dir)
    clean_stale_session_files(ab_dir)
    sys.path.insert(0, str(ab_dir))
    os.environ.setdefault("TOPOS_CONFIG_PATH", str(ab_dir / "topos_runtime" / "config.json"))
    os.environ.setdefault("TOPOS_BACKEND_GRAPHICS", "metal")

    tally: Counter = collections.Counter()
    details: Dict[str, Any] = {}
    install(tally, details)

    import topos_bench  # the A/B copy of the benchmark driver

    def write_report() -> None:
        details["tally"] = dict(tally)
        details["graphics_env"] = os.environ.get("TOPOS_BACKEND_GRAPHICS", "")
        frame_types = details.get("frame_types")
        details["frame_type_counts"] = dict(frame_types or {})
        details["diagnostic_errors"] = dict(details.get("diagnostic_errors", {}))
        details["gate_calls"] = sum(tally.values())
        for name in ("present_probe", "record_probe"):
            details[name] = dict(details.get(name, {}))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(details, indent=2, default=str) + "\n"
        args.output.write_text(text, encoding="utf-8")
        print("METAL_PREVIEW_GATE_DIAG " + text, flush=True)

    # The application leaves through os._exit(): no ``finally`` and no atexit
    # hooks run, so the report is flushed right before the process goes away.
    real_exit = os._exit

    def flushing_exit(code: int) -> None:
        try:
            write_report()
        except Exception:  # reporting must never hold the application open
            logger.exception("metal preview gate diagnostic: report failed")
        real_exit(code)

    os._exit = flushing_exit  # type: ignore[assignment]
    status = 0
    try:
        topos_bench.playback(args.repeats)
    except SystemExit as exit_request:  # QApplication.quit() unwinds main()
        status = int(exit_request.code or 0)
    write_report()
    return status


if __name__ == "__main__":
    sys.exit(main())
