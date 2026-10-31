#!/usr/bin/env python3
"""Diagnose comp-clip playback cost through the real headless preview path.

Replicates exactly what TimelinePreviewRenderer does for comp_clip assets:
CompositeFrameRenderer.render_comp_clip_frame per frame (new provider per
frame, CPU decode, graph eval, GPU->CPU readback).

Reports per-frame total + internal stage breakdown (context/provider build,
snapshot build, eval+readback).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

VIDEO = str(ROOT / "tests/test_videos/2K_h264.mp4")
N_FRAMES = 48


def _build_project():
    from src.features.edit.core.project import EditProject, MediaAsset
    from src.features.edit.core.timeline_v2 import (
        ClipInstance, EditTrack, TimelineSequence,
    )

    project = EditProject.create("diag-comp-clip")
    asset = MediaAsset(
        asset_id="asset-src",
        path=VIDEO,
        media_type="video",
        display_name="2K_h264",
        metadata={"width": 1920, "height": 1080, "fps": 25.0},
    )
    project.assets[asset.asset_id] = asset
    clip = ClipInstance(
        clip_id="clip-1", asset_id="asset-src", track_id="track-v1",
        timeline_start=0, timeline_duration=N_FRAMES + 8,
        source_start=0, source_duration=N_FRAMES + 8,
    )
    seq = TimelineSequence(
        timeline_id="tl-1", name="Timeline 1", fps=25.0,
        width=1920, height=1080,
        video_tracks=(EditTrack("track-v1", "V1", "video", 0, clips=(clip,)),),
    )
    return project, seq, clip


def _graph_data(with_blur: bool) -> dict:
    nodes = [
        {
            "id": "in-1", "type": "timeline_clip_input", "position": [0, 0],
            "parameters": {
                "timeline_id": "tl-1", "clip_id": "clip-1",
                "asset_id": "asset-src", "input_stage": "source_raw",
            },
            "disabled": False,
        },
    ]
    connections = []
    prev, prev_port = "in-1", "output"
    if with_blur:
        nodes.append({
            "id": "blur-1", "type": "blur", "position": [200, 0],
            "parameters": {"blur_type": "box", "radius_x": 5.0, "radius_y": 5.0},
            "disabled": False,
        })
        connections.append({
            "src_node_id": prev, "src_port_name": prev_port,
            "dst_node_id": "blur-1", "dst_port_name": "image",
        })
        prev, prev_port = "blur-1", "result"
    nodes.append({
        "id": "out-1", "type": "output", "position": [400, 0],
        "parameters": {"format_mode": "custom", "width": 1920, "height": 1080},
        "disabled": False,
    })
    connections.append({
        "src_node_id": prev, "src_port_name": prev_port,
        "dst_node_id": "out-1", "dst_port_name": "image",
    })
    return {
        "composite_id": "graph-diag",
        "source_clip_id": "clip-1",
        "source_asset_id": "asset-src",
        "source_asset_path": VIDEO,
        "timeline_id": "tl-1",
        "clip_id": "clip-1",
        "width": 1920, "height": 1080, "fps": 25.0,
        "duration_frames": N_FRAMES,
        "nodes": nodes,
        "connections": connections,
    }


def main() -> int:
    import os
    import faulthandler
    faulthandler.enable()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtGui import QGuiApplication, QSurfaceFormat
    # Mirror src/main.py: default GL format must be set before app creation.
    _fmt = QSurfaceFormat()
    _fmt.setVersion(4, 1)
    _fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    _fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    QSurfaceFormat.setDefaultFormat(_fmt)
    _app = QGuiApplication.instance() or QGuiApplication([])

    import src.features.composite.render.composite_frame_renderer as cfr_mod
    from src.features.composite.render.composite_frame_renderer import (
        CompositeFrameRenderer,
    )

    project, seq, clip = _build_project()

    # ── Stage instrumentation (monkeypatch timers) ──
    stats: dict[str, list[float]] = {
        "ctx_build": [], "snapshot": [], "eval_readback": [],
    }
    orig_ctx = CompositeFrameRenderer._build_timeline_source_context
    orig_snap = CompositeFrameRenderer._build_snapshot

    def timed_ctx(self, *a, **kw):
        t0 = time.perf_counter()
        r = orig_ctx(self, *a, **kw)
        stats["ctx_build"].append(time.perf_counter() - t0)
        return r

    def timed_snap(self, *a, **kw):
        t0 = time.perf_counter()
        r = orig_snap(self, *a, **kw)
        stats["snapshot"].append(time.perf_counter() - t0)
        return r

    CompositeFrameRenderer._build_timeline_source_context = timed_ctx
    CompositeFrameRenderer._build_snapshot = timed_snap

    def run_suite(label: str, with_blur: bool) -> None:
        for k in stats:
            stats[k].clear()
        # Headless substitute for the app's shared GL context: a standalone
        # moderngl context; render_and_readback wraps it via create_context().
        import moderngl
        gl_ctx = moderngl.create_standalone_context()
        orig_create = moderngl.create_context
        moderngl.create_context = lambda *a, **kw: gl_ctx
        renderer = CompositeFrameRenderer()
        graph = _graph_data(with_blur)
        totals: list[float] = []
        ok = 0
        for f in range(N_FRAMES):
            if f == 0 or f == N_FRAMES - 1:
                print(f"  ... frame {f}", flush=True)
            t0 = time.perf_counter()
            t_rb = time.perf_counter()
            img = renderer.render_comp_clip_frame(
                graph_data=graph,
                timeline_id="tl-1",
                clip_id="clip-1",
                asset_id="asset-src",
                edit_project=project,
                timeline_sequences={"tl-1": seq},
                timeline_frame=f,
                source_frame=f,
                quality="preview",
            )
            dt = time.perf_counter() - t0
            totals.append(dt)
            if img is not None:
                ok += 1
        # eval_readback ≈ total - ctx_build - snapshot
        for i, tot in enumerate(totals):
            other = (stats["ctx_build"][i] if i < len(stats["ctx_build"]) else 0.0) \
                + (stats["snapshot"][i] if i < len(stats["snapshot"]) else 0.0)
            stats["eval_readback"].append(max(0.0, tot - other))
        renderer.cleanup()
        moderngl.create_context = orig_create
        try:
            gl_ctx.release()
        except Exception:
            pass

        def ms(name: str) -> str:
            v = sorted(stats[name])
            n = len(v) or 1
            return (f"mean={1000 * sum(v) / n:.1f} "
                    f"p50={1000 * v[n // 2]:.1f} max={1000 * v[-1]:.1f}")

        tot = sorted(totals)
        n = len(tot)
        fps_eff = n / sum(tot) if sum(tot) > 0 else 0.0
        print(f"\n[{label}] ok={ok}/{n}")
        print(f"  total/frame     : mean={1000 * sum(tot) / n:.1f} "
              f"p50={1000 * tot[n // 2]:.1f} max={1000 * tot[-1]:.1f} ms "
              f"-> {fps_eff:.1f} fps (target 25)")
        print(f"  ctx+provider    : {ms('ctx_build')} ms")
        print(f"  snapshot build  : {ms('snapshot')} ms")
        print(f"  eval+readback   : {ms('eval_readback')} ms")

    run_suite("passthrough (no blur)", with_blur=False)
    run_suite("with blur (box r=5)", with_blur=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
