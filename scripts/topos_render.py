#!/usr/bin/env python3
"""topos-render — headless composite rendering (A7).

Usage:
    python scripts/topos_render.py <project.topos | graph.json> [options]

Renders a composite graph to an image sequence using the exact production
export path (CompositeRenderOutput → CompositeFrameRenderer), so CLI output
is pixel-identical to a GUI export of the same graph and settings.

Options:
    --frames STR      Frame spec: "5", "1-100", "1,3,7-9" (default: 1)
    --out PATH        Output directory (or file path; its extension wins)
    --format STR      exr | png | jpg (default: from --out ext, else exr)
    --base-name STR   Sequence base name (default: input file stem)
    --aov STR         Comma-separated render passes (default: beauty)
    --quality STR     draft | preview | final (recorded; the export path
                      always renders final quality)
    --no-cache        Bypass the node disk-cache layer (force recompute)
    --dry-run         Validate the graph only, produce no output
    --json            Machine-readable result on stdout (for CI)

Exit codes: 0 success · 1 usage error · 2 graph validation failure
            3 render error · 4 encode error

Headless GL (A7 spike note): the renderer creates a ModernGL context on the
main thread of this process.  On macOS hosts whose window-server-backed
context creation is unhealthy that path dies with SIGSEGV inside native
code (uncatchable); the CLI therefore sets TOPOS_HEADLESS_GL=1 so the
export renderer uses a standalone (offscreen) context, on which the
identical pipeline runs reliably.  `--dry-run` never touches GL and is
always safe for CI validation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_BAD_GRAPH = 2
EXIT_RENDER_ERROR = 3
EXIT_ENCODE_ERROR = 4


class _UsageErrorParser(argparse.ArgumentParser):
    """argparse exits with code 2 by default, which collides with the
    documented BAD_GRAPH code — route usage errors to 1 instead."""

    def error(self, message):  # pragma: no cover - argparse plumbing
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def parse_frame_spec(spec: str) -> list:
    """Parse "5", "1-100", "1,3,7-9" into a frame list."""
    frames = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            lo_i, hi_i = int(lo), int(hi)
            if hi_i < lo_i:
                raise ValueError(f"frame range inverted: {part}")
            frames.extend(range(lo_i, hi_i + 1))
        else:
            frames.append(int(part))
    if not frames:
        raise ValueError("empty frame spec")
    return frames


def load_graph_data(input_path: str):
    """Load graph data from a .topos project or a graph JSON file."""
    path = Path(input_path)
    if not path.is_file():
        raise FileNotFoundError(f"input not found: {input_path}")

    if path.suffix.lower() == ".topos":
        from src.shared.project.topos_project_read_service import (
            ToposProjectReadService,
        )
        bundle = ToposProjectReadService().load(str(path))
        graph_data = getattr(bundle, "composite_graph", None) if bundle \
            else None
        if not graph_data:
            raise ValueError("project contains no composite graph")
        return graph_data

    from src.features.composite.utils.serialization_formats import (
        deserialize,
    )
    data = deserialize(path.read_bytes())
    if isinstance(data, dict) and isinstance(
            data.get("composite_graph"), dict):
        data = data["composite_graph"]
    if not isinstance(data, dict):
        raise ValueError("input is not a graph document")
    return data


def validate_graph(graph_data: dict):
    """Deserialize + migrate the graph; raises on structural failure."""
    from src.features.composite.core.node_engine import NodeGraph
    graph = NodeGraph.from_dict(graph_data)
    if not graph.nodes:
        raise ValueError("graph has no nodes")
    return graph


def main(argv=None) -> int:
    parser = _UsageErrorParser(
        prog="topos-render", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help=".topos project or graph JSON")
    parser.add_argument("--frames", default="1",
                        help="frame spec: 5 | 1-100 | 1,3,7-9")
    parser.add_argument("--out", default="",
                        help="output directory or file path")
    parser.add_argument("--format", default="",
                        help="exr | png | jpg")
    parser.add_argument("--base-name", default="",
                        help="sequence base name")
    parser.add_argument("--aov", default="beauty",
                        help="comma-separated render passes")
    parser.add_argument("--quality", default="final",
                        choices=["draft", "preview", "final"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-cache", action="store_true",
                        help="bypass the node disk-cache layer")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable result on stdout")
    args = parser.parse_args(argv)

    if args.no_cache:
        # Read lazily by the disk-cache store at first use; must be set
        # before the render imports touch the store singleton.
        os.environ["TOPOS_DISABLE_DISK_CACHE"] = "1"

    # Offscreen context: window-server-backed context creation can die with
    # SIGSEGV on unhealthy macOS states (uncatchable); a standalone context
    # runs the identical pipeline reliably (see export renderer note).
    os.environ.setdefault("TOPOS_HEADLESS_GL", "1")

    result = {
        "ok": False,
        "input": args.input,
        "frames": [],
        "passes": [],
        "outputs": [],
        "quality": args.quality,
        "duration_s": 0.0,
        "error": "",
    }
    started = time.perf_counter()

    def emit(code: int) -> int:
        result["duration_s"] = round(time.perf_counter() - started, 3)
        result["ok"] = code == EXIT_OK
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=1))
        elif result["error"]:
            print(f"error: {result['error']}", file=sys.stderr)
        return code

    # ── Load ───────────────────────────────────────────────────────────
    try:
        graph_data = load_graph_data(args.input)
    except Exception as exc:
        result["error"] = f"load failed: {exc}"
        return emit(EXIT_BAD_GRAPH)

    # ── Validate (also runs the schema migration chain) ────────────────
    try:
        validate_graph(graph_data)
    except Exception as exc:
        result["error"] = f"graph validation failed: {exc}"
        return emit(EXIT_BAD_GRAPH)

    # Parse the frame spec BEFORE the dry-run return so usage errors are
    # still caught by validation-only CI runs.
    try:
        frames = parse_frame_spec(args.frames)
    except ValueError as exc:
        result["error"] = f"bad --frames: {exc}"
        return emit(EXIT_USAGE)

    if args.dry_run:
        return emit(EXIT_OK)

    # ── Resolve output parameters ──────────────────────────────────────
    out_ext = (args.format or
               (Path(args.out).suffix.lstrip(".") if args.out else "")
               or "exr").lower()
    if out_ext not in ("exr", "png", "jpg", "jpeg"):
        result["error"] = f"unsupported format: {out_ext}"
        return emit(EXIT_USAGE)
    if out_ext == "jpeg":
        out_ext = "jpg"

    out_dir = args.out or str(Path.cwd() / "render_out")
    # Only a known image extension means "file path was given" — a suffix
    # like "v2.0" on a directory name must not strip its last component.
    if Path(out_dir).suffix.lower() in (".exr", ".png", ".jpg", ".jpeg",
                                        ".tif", ".tiff"):
        out_dir = str(Path(out_dir).parent)
    base_name = args.base_name or Path(args.input).stem or "render"
    passes = [p.strip() for p in args.aov.split(",") if p.strip()] \
        or ["beauty"]

    # ── Render ────────────────────────────────────────────────────────
    # Main-thread headless loop: the GL context is created on the thread
    # that runs this script (context affinity). The GUI export facade
    # spawns worker threads and per-worker contexts, which is both
    # unstable headless and unnecessary here — farm scaling happens by
    # running multiple CLI processes.
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.render.composite_export_renderer import (
        CompositeExportRenderer,
    )
    from src.shared.io.file_io import FileIO

    try:
        node_graph = NodeGraph.from_dict(graph_data)
        renderer = CompositeExportRenderer()
        renderer.set_persistent_mode(True)
    except Exception as exc:
        result["error"] = f"renderer init failed: {exc}"
        return emit(EXIT_RENDER_ERROR)

    fps = float(graph_data.get("fps", 24.0) or 24.0)
    written = []
    for render_pass in passes:
        pass_dir = (str(Path(out_dir) / render_pass)
                    if len(passes) > 1 else out_dir)
        os.makedirs(pass_dir, exist_ok=True)
        result["passes"].append(render_pass)
        for frame in sorted(set(frames)):
            out_path = str(Path(pass_dir)
                           / f"{base_name}.{str(frame).zfill(4)}.{out_ext}")
            try:
                snapshot = node_graph.snapshot(
                    color_texture_id=0, color_width=0, color_height=0,
                    color_revision=0, frame_number=int(frame), fps=fps)
                outcome = renderer.render_and_readback(
                    snapshot, readback_output="linear_f32",
                    render_pass=render_pass)
            except Exception as exc:
                result["error"] = f"render failed: {exc}"
                return emit(EXIT_RENDER_ERROR)
            image = getattr(outcome, "image", None)
            error = getattr(outcome, "error", None)
            if error is not None or image is None:
                result["error"] = (f"render failed at frame {frame}: "
                                   f"{error or 'no image'}")
                return emit(EXIT_RENDER_ERROR)
            options = ({"exr_type": 1, "exr_compression": 3}
                        if out_ext == "exr" else {"quality": 95})
            if not FileIO.save_image(out_path, image, options):
                result["error"] = f"encode failed: {out_path}"
                return emit(EXIT_ENCODE_ERROR)
            written.append(out_path)

    result["frames"] = sorted(set(frames))
    result["outputs"] = written
    return emit(EXIT_OK)


if __name__ == "__main__":
    sys.exit(main())
