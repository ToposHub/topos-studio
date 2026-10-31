#!/usr/bin/env python3
"""
Playback trace dump — one-click export of recent edit-page playback traces.

P0.4 of the professional NLE realtime playback rebuild. Wraps the in-process
``FrameTraceCollector`` (``src/shared/video/playback_frame_trace.py``) and emits
either a JSON or a Markdown report of the most recent playback frames, so a
blue-frame / late-frame / audio-seek incident can be explained after the fact.

The collector is an in-process singleton, so the most useful invocation is
in-process (call ``dump_report()`` from a debug hook / test / REPL right after a
playback session). The CLI also accepts ``--from-file`` to reformat a JSON dump
that was persisted earlier, and ``--self-test`` to synthesise a few traces and
validate the tooling standalone.

Examples
--------
    # Markdown summary of the last 10 seconds of the live collector
    python tools/playback_trace_dump.py --last-seconds 10 --format markdown

    # Reformat a previously exported JSON file
    python tools/playback_trace_dump.py --from-file trace.json --format markdown

    # Standalone validation (no app needed)
    python tools/playback_trace_dump.py --self-test
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Make ``src`` importable when run as a plain script (tools/ is repo-root).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.shared.video.playback_frame_trace import (  # noqa: E402
    EditPlaybackFrameTrace,
    FrameTraceCollector,
    get_trace_collector,
)


def _filter_last_seconds(traces: List[EditPlaybackFrameTrace], seconds: float) -> List[EditPlaybackFrameTrace]:
    """Return only traces whose monotonic timestamp is within ``seconds`` of the latest."""
    if not traces or seconds <= 0:
        return list(traces)
    newest = max(t.timestamp for t in traces)
    cutoff = newest - seconds
    return [t for t in traces if t.timestamp >= cutoff]


def build_report(collector: FrameTraceCollector, last_seconds: Optional[float]) -> Dict[str, Any]:
    """Build a serialisable report dict from a collector."""
    traces = collector.get_traces()
    if last_seconds is not None:
        traces = _filter_last_seconds(traces, last_seconds)

    # Summarise exactly the (possibly time-windowed) slice we are reporting.
    temp = FrameTraceCollector()
    for t in traces:
        temp.record(t)
    summary = temp.get_summary()
    summary["reported_frames"] = len(traces)
    if traces:
        summary["window_seconds"] = round(max(t.timestamp for t in traces) - min(t.timestamp for t in traces), 3)
    else:
        summary["window_seconds"] = 0.0
    return {
        "summary": summary,
        "traces": [t.to_compact_dict() for t in traces],
    }


def format_markdown(report: Dict[str, Any], *, trace_limit: int = 25) -> str:
    """Render a report dict as a human-readable Markdown document."""
    s = report.get("summary", {})
    lines: List[str] = []
    lines.append("# Playback Trace Dump")
    lines.append("")
    lines.append(f"- reported_frames: `{s.get('reported_frames', 0)}`")
    lines.append(f"- window_seconds: `{s.get('window_seconds', 0.0)}`")
    lines.append(f"- total_frames (collector): `{s.get('total_frames', 0)}`")
    lines.append(f"- gpu_frames: `{s.get('gpu_frames', 0)}`")
    lines.append(f"- cpu_fallback_frames: `{s.get('cpu_fallback_frames', 0)}`")
    lines.append("")
    lines.append("## Problem counters (target = 0 during playback)")
    lines.append("")
    lines.append("| metric | count |")
    lines.append("|---|---:|")
    for key, label in (
        ("sync_seek_frames", "sync_seek_frames"),
        ("ui_thread_sync_decode_frames", "ui_thread_sync_decode_frames"),
        ("stale_frame_emitted", "stale_frame_emitted"),
        ("qimage_upload_frames", "qimage_upload_frames"),
        ("gl_tex_image_upload_frames", "gl_tex_image_upload_frames"),
        ("cpu_to_ndarray_frames", "cpu_to_ndarray_frames"),
        ("texture_slot_reuse_without_ack", "texture_slot_reuse_without_ack"),
    ):
        lines.append(f"| {label} | `{s.get(key, 0)}` |")
    lines.append("")
    lines.append("## Timing averages (ms)")
    lines.append("")
    lines.append("| stage | avg_ms |")
    lines.append("|---|---:|")
    for key, label in (("decode_avg_ms", "decode"), ("render_avg_ms", "render"), ("total_avg_ms", "total")):
        lines.append(f"| {label} | `{s.get(key, 0.0):.3f}` |")
    lines.append("")

    def _dist(title: str, key: str) -> None:
        dist = s.get(key) or {}
        lines.append(f"## {title}")
        lines.append("")
        if not dist:
            lines.append("_(none)_")
        else:
            lines.append("| value | count |")
            lines.append("|---|---:|")
            for value, count in sorted(dist.items(), key=lambda kv: kv[1], reverse=True):
                lines.append(f"| `{value}` | `{count}` |")
        lines.append("")

    _dist("Decode path distribution", "decode_path_counts")
    _dist("Render path distribution", "render_path_counts")
    _dist("Fallback reasons", "fallback_reasons")

    traces = report.get("traces", []) or []
    if traces:
        lines.append(f"## Last {min(trace_limit, len(traces))} traces (of {len(traces)})")
        lines.append("")
        lines.append("| frame | decode_path | render_path | fallback | sync_seek | decode_ms | render_ms |")
        lines.append("|---:|---|---|---|---:|---:|---:|")
        for t in traces[-trace_limit:]:
            lines.append(
                "| {frame} | `{dp}` | `{rp}` | `{fb}` | {ss} | {dm:.3f} | {rm:.3f} |".format(
                    frame=t.get("frame_index", -1),
                    dp=t.get("decode_path", ""),
                    rp=t.get("render_path", ""),
                    fb=t.get("fallback_reason", ""),
                    ss=t.get("sync_seek_count", 0),
                    dm=float(t.get("decode_ms", 0.0)),
                    rm=float(t.get("render_ms", 0.0)),
                )
            )
        lines.append("")
    return "\n".join(lines)


def dump_report(
    *,
    last_seconds: Optional[float] = None,
    fmt: str = "markdown",
    collector: Optional[FrameTraceCollector] = None,
    out: Optional[Path] = None,
) -> str:
    """Dump the live collector (or a provided one). Returns the rendered text."""
    collector = collector if collector is not None else get_trace_collector()
    report = build_report(collector, last_seconds)
    if fmt.lower() == "json":
        text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    else:
        text = format_markdown(report)
    if out is not None:
        Path(out).write_text(text, encoding="utf-8")
    return text


def _load_report_from_file(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    # Accept either a full report dict or a bare list of trace dicts.
    if isinstance(data, list):
        return {"summary": {}, "traces": data}
    return data


def _self_test() -> str:
    """Populate a throwaway collector with synthetic traces and dump Markdown."""
    c = FrameTraceCollector()
    c.start_session(page="self_test")
    good = EditPlaybackFrameTrace(
        frame_index=1, decode_path="macos_vt_iosurface_zero_copy", render_path="gpu_planar_upload",
        decode_ms=2.1, render_ms=1.4, total_ms=3.7,
    )
    bad = EditPlaybackFrameTrace(
        frame_index=2, decode_path="cpu_packed_rgb", render_path="cpu_qimage_upload",
        fallback_reason="vt_unavailable", sync_seek_count=1, qimage_copy=True,
        decode_ms=14.2, render_ms=9.8, total_ms=24.5,
    )
    c.record(good)
    c.record(bad)
    return dump_report(last_seconds=None, fmt="markdown", collector=c)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Dump recent playback trace as JSON or Markdown.")
    p.add_argument("--last-seconds", type=float, default=None, help="Only report frames within N seconds of the latest.")
    p.add_argument("--last-frames", type=int, default=None, help="Only report the last N frames.")
    p.add_argument("--format", choices=("json", "markdown"), default="markdown")
    p.add_argument("--out", type=Path, default=None, help="Write report to this path instead of stdout.")
    p.add_argument("--from-file", type=Path, default=None, help="Reformat a previously exported JSON report.")
    p.add_argument("--self-test", action="store_true", help="Synthesise traces and dump, no app required.")
    args = p.parse_args(argv)

    if args.self_test:
        text = _self_test()
    elif args.from_file is not None:
        report = _load_report_from_file(args.from_file)
        if args.last_frames is not None:
            report["traces"] = report.get("traces", [])[-args.last_frames:]
        if args.format == "json":
            text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
        else:
            text = format_markdown(report)
    else:
        collector = get_trace_collector()
        if not collector.trace_count:
            sys.stderr.write(
                "note: live FrameTraceCollector is empty. Record traces in-process (enable "
                "TOPOS_EDIT_PLAYBACK_TRACE) or pass --from-file / --self-test.\n"
            )
        # --last-frames maps to the collector's own slicing.
        last_seconds = args.last_seconds
        if args.last_frames is not None and last_seconds is None:
            # Emulate frame-limit by summarising the slice via a temp collector.
            tmp = FrameTraceCollector()
            for t in collector.get_traces(last_n=args.last_frames):
                tmp.record(t)
            text = dump_report(last_seconds=None, fmt=args.format, collector=tmp, out=args.out)
            return 0
        text = dump_report(last_seconds=last_seconds, fmt=args.format, out=args.out)

    if args.out is not None:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
