#!/usr/bin/env python3
"""Drive the real Color app to reproduce the param-adjustment flash.

Scenario: open a video on the Color page, play it, then adjust grading
params while playing (exactly what the user does with a slider).  The run
writes a JSONL event trace via TOPOS_FLASH_DIAG and analyses it:

  * every front-texture publish during playback must come from the video
    playback lane with a monotonically advancing frame index
  * a publish with a foreign reason (grading/full/viewport_refresh), a
    pyramid source, or a backward frame jump inside the ±400 ms window of
    a params_invalidate event is reported as a flash suspect

Usage:
    .venv/bin/python tools/diag_grade_flash_driver.py \
        [--file tests/test_videos/2K_h264.mp4] [--out .cache/flash_diag.jsonl]
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import os
import sys
from pathlib import Path

faulthandler.enable()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
os.environ.setdefault("TOPOS_CONFIG_PATH", str(PROJECT_ROOT / ".topos" / "config.json"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default=str(PROJECT_ROOT / "tests/test_videos/2K_h264.mp4"))
    parser.add_argument("--out", default=str(PROJECT_ROOT / ".cache/flash_diag.jsonl"))
    parser.add_argument("--mode", choices=("single", "multiclip", "firstdrag"), default="multiclip",
                        help="single = standalone Color video; multiclip = Edit project "
                             "with the same source cut into 3 timeline clips (user scenario); "
                             "firstdrag = multiclip setup, then paused first/second drags only "
                             "(no playback) to reproduce the first-drag flash")
    parser.add_argument("--play-ms", type=int, default=2000, help="play before grading")
    parser.add_argument("--changes", type=int, default=6, help="param changes while playing")
    parser.add_argument("--interval-ms", type=int, default=250)
    parser.add_argument("--tail-ms", type=int, default=1500, help="keep playing after changes")
    parser.add_argument("--noncontig", action="store_true",
                        help="cut the clips with gaps between their source ranges so "
                             "cross-clip playback uses the wrap-advance seek path "
                             "(stop -> non-blocking seek -> restart) instead of the "
                             "seamless contiguous rebind")
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    os.environ["TOPOS_FLASH_DIAG"] = str(out_path)

    # Mirror src/main.py GL configuration before QApplication exists.
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QSurfaceFormat
    from PyQt6.QtWidgets import QApplication

    fmt = QSurfaceFormat()
    fmt.setVersion(4, 5)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    fmt.setRedBufferSize(8)
    fmt.setGreenBufferSize(8)
    fmt.setBlueBufferSize(8)
    fmt.setAlphaBufferSize(8)
    fmt.setDepthBufferSize(24)
    fmt.setStencilBufferSize(8)
    if sys.platform != "win32":
        fmt.setSamples(4)
    fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
    QSurfaceFormat.setDefaultFormat(fmt)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)

    app = QApplication(sys.argv)

    # Mirror src/main.py module registration: without the registry the Color
    # canvas dock is never built and the GL viewport never initialises.
    from src.app.main_window import MainWindow
    from src.shared.i18n.i18n import I18N
    from src.shared.config.config_manager import ConfigManager
    from src.shared.feature_flags import load_feature_flags
    from src.app.feature_registry import FeatureRegistry
    from src.app.app_context import AppContext

    config_manager = ConfigManager()
    feature_flags = load_feature_flags(config_manager)
    I18N.load_locale(I18N.get_system_locale())
    registry = FeatureRegistry()
    import logging as _logging
    _driver_logger = _logging.getLogger("flash_diag_driver")
    app_context = AppContext(
        config_manager=config_manager,
        feature_flags=feature_flags,
        i18n=I18N,
        logger=_driver_logger,
    )
    registry.initialize(app_context)
    from src.features.color.module import ColorModule
    registry.register_module(ColorModule())
    try:
        from src.features.edit.module import EditModule
        registry.register_module(EditModule())
    except Exception as exc:
        print(f"[driver] Edit module unavailable: {exc}")

    window = MainWindow(feature_flags=feature_flags, feature_registry=registry)
    app_context.main_window = window
    app_context.save_router = window.save_router
    registry.register_all_save_handlers()
    window.resize(1600, 900)
    window.show()
    window.raise_()
    window.activateWindow()
    if args.mode == "single":
        try:
            window.switch_to_page("Color", force=True)
        except Exception as exc:
            print(f"[driver] switch_to_page(Color) failed: {exc}")

    state = {"phase": "load", "change_index": 0}

    def wait_renderer_ready(step: int = 0):
        engine = getattr(window, "engine", None)
        renderer = getattr(engine, "renderer", None) if engine else None
        if renderer is not None:
            print(f"[driver] async renderer ready (ctx={renderer.ctx is not None})")
            if args.mode == "firstdrag":
                # Clip 2 was opened directly from Edit: no previous clip
                # frame exists in the renderer.  Real users grade clip 1
                # first, so reopen clip 1, let it settle, then switch to
                # clip 2 — the pyramid then holds clip 1's frame, which is
                # exactly what flashes on the first drag.
                try:
                    timeline_id = window._current_edit_project.active_timeline_id
                    timeline = window._current_timeline_sequences.get(timeline_id)
                    clips = []
                    for track in timeline.video_tracks:
                        clips.extend(track.clips)
                    print(f"[driver] firstdrag: settling clip 1 {clips[0].clip_id} first")
                    window.open_timeline_clip_in_color(timeline_id, clips[0].clip_id)
                except Exception as exc:
                    print(f"[driver] firstdrag: clip-1 settle failed: {exc}")
                # Let the static preview / hydration settle exactly like a
                # real user staring at the freshly selected clip.
                QTimer.singleShot(2500, switch_to_second_clip)
            else:
                QTimer.singleShot(200, start_playback)
            return
        if step == 5:
            canvas = getattr(window, "canvas", None)
            glv = getattr(canvas, "gl_viewport", None) if canvas else None
            print(
                f"[driver] debug: page={getattr(window, 'current_page', '?')} "
                f"engine={engine is not None} "
                f"canvas={canvas is not None} gl_viewport={glv is not None} "
                f"canvas_visible={canvas.isVisible() if canvas else None} "
                f"glv_visible={glv.isVisible() if glv else None} "
                f"window_visible={window.isVisible()}"
            )
        if step > 50:  # 10 s
            print("[driver] renderer never initialised; aborting")
            shutdown()
            return
        QTimer.singleShot(200, lambda: wait_renderer_ready(step + 1))

    def start_playback():
        vc = window.video_controller
        if vc is None or not vc.is_video_loaded():
            print("[driver] video not loaded yet, retrying")
            QTimer.singleShot(200, start_playback)
            return
        print(f"[driver] video loaded; playing {args.file}")
        vc.start_playback()
        state["phase"] = "playing"
        QTimer.singleShot(args.play_ms, do_drag if args.mode == "multiclip" else do_grade_change)

    def nudge_params(step_index: int) -> None:
        panel = window.mixer_panel
        params = {c: list(v) for c, v in panel.params.items()}
        colors = list(params.keys())
        color = colors[step_index % len(colors)]
        delta = 0.02 * (step_index % 7 - 3)  # wobble like a real drag
        params[color][1] = max(-1.0, min(1.0, float(params[color][1]) + delta))
        window.on_mixer_changed(params)
        panel.params = params

    drag_state = {"drag": 0, "tick": 0}

    def do_drag():
        """Continuous drag: interaction_started, ~60Hz mixer events, then end."""
        vc = window.video_controller
        if vc is None or not bool(getattr(vc, "is_playing", False)):
            print("[driver] playback stopped unexpectedly; finishing")
            finish()
            return
        if drag_state["drag"] >= args.changes:
            # Previous runs only graded inside one clip: playback never
            # crossed the clip boundary.  The user's timeline plays through
            # several clips, so exercise a drag that straddles the boundary
            # before the paused-adjust phase.
            QTimer.singleShot(200, do_boundary_drag)
            return
        drag_state["drag"] += 1
        drag_state["tick"] = 0
        print(f"[driver] drag #{drag_state['drag']} start while playing "
              f"(frame={getattr(vc, 'current_frame', '?')})")
        window.on_interaction_started()

        def drag_tick():
            if drag_state["tick"] >= 30:  # ~500 ms of 60 Hz events
                window.on_interaction_ended()
                print(f"[driver] drag #{drag_state['drag']} ended")
                QTimer.singleShot(args.interval_ms, do_drag)
                return
            drag_state["tick"] += 1
            nudge_params(drag_state["drag"] * 100 + drag_state["tick"])
            QTimer.singleShot(16, drag_tick)

        drag_tick()

    boundary_state = {"wait_ms": 0, "started": False}

    def _color_clip_out_source_frame():
        try:
            ctx = window._active_color_timeline_clip_playback_context()
            clip = ctx.get("clip") if ctx else None
            if clip is None:
                return None
            start = int(getattr(clip, "effective_source_start", 0)
                        or getattr(clip, "source_start", 0) or 0)
            dur = max(1, int(getattr(clip, "source_duration", 0) or 0))
            return start + dur - 1
        except Exception:
            return None

    def do_boundary_drag():
        """Wait until playback nears the clip out-frame, then drag across it."""
        vc = window.video_controller
        if vc is None or not bool(getattr(vc, "is_playing", False)):
            print("[driver] playback stopped before boundary; skipping")
            QTimer.singleShot(200, do_paused_adjust)
            return
        out_frame = _color_clip_out_source_frame()
        cur = getattr(vc, "current_frame", None)
        if out_frame is None:
            print("[driver] no clip context; skipping boundary drag")
            QTimer.singleShot(200, do_paused_adjust)
            return
        if boundary_state["wait_ms"] > 45000:
            print(f"[driver] boundary never reached (cur={cur} out={out_frame}); skipping")
            QTimer.singleShot(200, do_paused_adjust)
            return
        if cur is None or int(cur) < out_frame - 12:
            boundary_state["wait_ms"] += 100
            QTimer.singleShot(100, do_boundary_drag)
            return
        boundary_state["started"] = True
        print(f"[driver] boundary drag: dragging across clip out-frame {out_frame} "
              f"(cur={cur})")
        window.on_interaction_started()

        def cross_tick(i=0):
            if i >= 90:  # ~1.5 s of 60 Hz events — well past the boundary
                window.on_interaction_ended()
                vc2 = window.video_controller
                print(f"[driver] boundary drag ended (frame={getattr(vc2, 'current_frame', '?')})")
                QTimer.singleShot(400, do_paused_adjust)
                return
            nudge_params(5000 + i)
            QTimer.singleShot(16, lambda: cross_tick(i + 1))

        cross_tick()

    pause_state = {"n": 0}

    def do_paused_adjust():
        """Pause, then drag while paused — the classic grading posture."""
        vc = window.video_controller
        if pause_state["n"] == 0:
            print(f"[driver] pausing at frame {getattr(vc, 'current_frame', '?')}")
            vc.stop_playback()
        if pause_state["n"] >= 3:
            QTimer.singleShot(args.tail_ms, finish)
            return
        pause_state["n"] += 1

        def paused_tick(i=0):
            if i >= 20:
                window.on_interaction_ended()
                print(f"[driver] paused adjust #{pause_state['n']} ended")
                QTimer.singleShot(700, do_paused_adjust)
                return
            if i == 0:
                window.on_interaction_started()
            nudge_params(1000 + pause_state["n"] * 100 + i)
            QTimer.singleShot(25, lambda: paused_tick(i + 1))

        paused_tick()

    firstdrag_state = {"drag": 0}

    def switch_to_second_clip():
        """Clip 1 settled; now select clip 2 like the user and drag soon."""
        try:
            timeline_id = window._current_edit_project.active_timeline_id
            timeline = window._current_timeline_sequences.get(timeline_id)
            clips = []
            for track in timeline.video_tracks:
                clips.extend(track.clips)
            print(f"[driver] firstdrag: switching to clip 2 {clips[1].clip_id}")
            window.open_timeline_clip_in_color(timeline_id, clips[1].clip_id)
        except Exception as exc:
            print(f"[driver] firstdrag: clip-2 switch failed: {exc}")
        # Drag shortly after the switch — inside the async seek window where
        # the new clip frame may not be uploaded yet (user's first drag).
        QTimer.singleShot(250, do_first_drag)

    def do_first_drag():
        """Paused drag right after selecting a clip — user's exact posture."""
        vc = window.video_controller
        if vc is None or not vc.is_video_loaded():
            print("[driver] video not loaded yet, retrying")
            QTimer.singleShot(200, do_first_drag)
            return
        if bool(getattr(vc, "is_playing", False)):
            vc.stop_playback()
        if firstdrag_state["drag"] >= 3:
            QTimer.singleShot(args.tail_ms, finish)
            return
        firstdrag_state["drag"] += 1
        print(f"[driver] firstdrag #{firstdrag_state['drag']} start paused "
              f"(frame={getattr(vc, 'current_frame', '?')})")
        window.on_interaction_started()

        def tick(i=0):
            if i >= 20:
                window.on_interaction_ended()
                print(f"[driver] firstdrag #{firstdrag_state['drag']} ended")
                QTimer.singleShot(1200, do_first_drag)
                return
            nudge_params(9000 + firstdrag_state["drag"] * 100 + i)
            QTimer.singleShot(25, lambda: tick(i + 1))

        tick()

    def do_grade_change():
        vc = window.video_controller
        if vc is None or not bool(getattr(vc, "is_playing", False)):
            print("[driver] playback stopped unexpectedly; finishing")
            finish()
            return
        if state["change_index"] >= args.changes:
            QTimer.singleShot(args.tail_ms, finish)
            return
        state["change_index"] += 1
        print(f"[driver] mixer change #{state['change_index']} while playing "
              f"(frame={getattr(vc, 'current_frame', '?')})")
        window.on_interaction_started()
        nudge_params(state["change_index"])
        window.on_interaction_ended()
        QTimer.singleShot(args.interval_ms, do_grade_change)

    def finish():
        print("[driver] stopping playback and analysing trace")
        vc = window.video_controller
        if vc is not None and bool(getattr(vc, "is_playing", False)):
            vc.stop_playback()
        QTimer.singleShot(500, shutdown)

    def shutdown():
        # Skip window.close(): a modal welcome dialog or renderer teardown
        # can block it; the diagnosis output is complete at this point.
        analyse(out_path)
        # Skip Qt teardown: the GL render thread can bus-error during
        # interpreter shutdown; the diagnosis output is complete by now.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

    def setup_multiclip_project() -> bool:
        """Build the user's scenario: one source video cut into 3 clips."""
        try:
            # Edit's Program preview renderer decodes in a worker thread and
            # races the harness's rapid clip appends (segfault); it is not
            # part of the Color grading path under diagnosis, so disable it.
            window._init_timeline_preview_renderer = lambda: None
            window._auto_create_edit_project("Diag MultiClip")
            tpr = getattr(window, "timeline_preview_renderer", None)
            if tpr is not None:
                try:
                    tpr.shutdown()
                except Exception:
                    pass
                window.timeline_preview_renderer = None
            from src.shared.media.factory import MediaSourceFactory
            meta = MediaSourceFactory.probe_video_metadata(args.file)
            if meta is None:
                print("[driver] failed to probe video metadata")
                return False
            pixel_format = getattr(meta, "pixel_format", None) or getattr(meta, "pix_fmt", "") or ""
            project = window._current_edit_project
            new_project = project.import_asset(
                path=args.file,
                media_type="video",
                metadata={
                    "width": meta.width,
                    "height": meta.height,
                    "fps": float(meta.fps),
                    "duration_seconds": meta.duration_seconds,
                    "total_frames": meta.total_frames,
                    "codec_name": meta.codec_name or "",
                    "codec": meta.codec_name or "",
                    "pixel_format": pixel_format,
                },
                display_name=os.path.basename(args.file),
            )
            window._apply_project_change(new_project)
            asset = new_project.get_asset_by_path(args.file)
            if asset is None:
                print("[driver] asset import failed")
                return False
            total = int(meta.total_frames or 0)
            if args.noncontig:
                # Gaps between source ranges force the wrap-advance seek path.
                seg = max(30, total // 6)
                ranges = [(i * seg * 2, min(total - 1, i * seg * 2 + seg - 1))
                          for i in range(3)]
                if ranges[2][1] >= total:
                    print(f"[driver] video too short for noncontig clips: {total}")
                    return False
            else:
                seg = max(30, total // 3)
                ranges = [(i * seg, min(total - 1, (i + 1) * seg - 1)) for i in range(3)]
            for i, (in_f, out_f) in enumerate(ranges):
                ok = window.timeline_panel.append_clip(
                    asset_id=asset.asset_id, in_frame=in_f, out_frame=out_f
                )
                print(f"[driver] append clip #{i + 1} src[{in_f}..{out_f}] ok={ok}")
            timeline_id = window._current_edit_project.active_timeline_id
            timeline = window._current_timeline_sequences.get(timeline_id)
            if timeline is None:
                print("[driver] timeline not found")
                return False
            clips = []
            for track in timeline.video_tracks:
                clips.extend(track.clips)
            if len(clips) < 2:
                print(f"[driver] expected >=2 clips, got {len(clips)}")
                return False
            # Grade the middle clip like the user would.
            target = clips[1]
            print(f"[driver] opening clip {target.clip_id} "
                  f"(src {target.source_start}..{target.source_start + target.source_duration - 1}) in Color")
            window.open_timeline_clip_in_color(timeline_id, target.clip_id)
            return True
        except Exception as exc:
            import traceback
            traceback.print_exc()
            print(f"[driver] multiclip setup failed: {exc}")
            return False

    def begin():
        if args.mode in ("multiclip", "firstdrag"):
            print("[driver] building multi-clip Edit project")
            if not setup_multiclip_project():
                shutdown()
                return
        else:
            print(f"[driver] loading {args.file}")
            # Color rejects standalone videos; the timeline-clip entry flag routes
            # through the exact same graded video_controller path the user sees.
            window.load_video(args.file, for_timeline_clip=True)
        QTimer.singleShot(800, wait_renderer_ready)

    QTimer.singleShot(300, begin)
    app.exec()
    return 0


def analyse(out_path: Path) -> None:
    if not out_path.exists():
        print("[diag] no trace written")
        return
    events = []
    with open(out_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    print(f"[diag] {len(events)} events captured")

    invalidates = [e for e in events if e.get("k") == "params_invalidate"]
    publishes = [e for e in events if e.get("k") == "publish"]
    drops = [e for e in events if e.get("k") == "drop"]
    video_cmds = [e for e in events if e.get("k") == "video_cmd"]
    clip_opens = [e for e in events if e.get("k") in ("open_clip_in_color", "load_video")]
    print(f"[diag] invalidates={len(invalidates)} publishes={len(publishes)} "
          f"drops={len(drops)} video_cmds={len(video_cmds)} clip_opens={len(clip_opens)}")

    suspects = []
    window_ms = 400.0
    for inv in invalidates:
        t0, t1 = inv["t"] - window_ms, inv["t"] + window_ms
        win_pub = [p for p in publishes if t0 <= p["t"] <= t1]
        for p in win_pub:
            if p.get("playback") and p.get("reason") not in ("playback", "static_preview", "full"):
                suspects.append(("foreign_reason_during_playback", inv, p))
            if p.get("src") == "pyramid" and p.get("playback"):
                suspects.append(("pyramid_during_playback", inv, p))
        # a clip reopen / video reload inside the param-change window is the
        # classic "other clip's picture flashes" mechanism
        for co in clip_opens:
            if t0 <= co["t"] <= t1:
                suspects.append(("clip_reopen_near_param_change", inv, co))
        # frame-index continuity of video commands inside the window
        win_cmds = [c for c in video_cmds if t0 <= c["t"] <= t1]
        prev_idx = None
        for c in win_cmds:
            idx = c.get("frame_index")
            if prev_idx is not None and idx is not None and idx < prev_idx - 2:
                suspects.append(("frame_jump_backward", inv, {"prev": prev_idx, "cur": idx, "t": c["t"]}))
            if idx is not None:
                prev_idx = idx

    if not invalidates:
        print("[diag] WARNING: no params_invalidate events — chain not reached")

    # Presentation check: every canvas blit must show the texture the last
    # publish produced.  A blit of any other texture is exactly the visual
    # "flash of another clip's frame".
    blits = [e for e in events if e.get("k") == "blit"]
    published = set(p.get("front_tex") for p in publishes if p.get("front_tex"))
    last_pub_tex = None
    last_pub_t = None
    pub_sorted = sorted(publishes, key=lambda p: p["t"])
    pub_idx = 0
    blit_anoms = []
    for b in blits:
        while pub_idx < len(pub_sorted) and pub_sorted[pub_idx]["t"] <= b["t"]:
            last_pub_tex = pub_sorted[pub_idx].get("front_tex")
            last_pub_t = pub_sorted[pub_idx]["t"]
            pub_idx += 1
        tex = b.get("tex")
        if tex is not None and last_pub_tex is not None and tex != last_pub_tex:
            blit_anoms.append((b["t"], tex, last_pub_tex, last_pub_t))
    if blit_anoms:
        print(f"[diag] *** {len(blit_anoms)} BLIT ANOMALY(S) — foreign texture presented ***")
        for t, tex, want, pt in blit_anoms[:20]:
            print(f"  - t={t:.1f}ms blitted tex={tex} but last publish was tex={want} @ {pt:.1f}ms")
    elif blits:
        print(f"[diag] blit/publish consistency OK across {len(blits)} blit changes")


    # Content-fingerprint outlier check: a published frame whose centre
    # pixels differ a lot from BOTH neighbours is a re-published stale frame
    # (the visual "flash of another clip").
    fp_seq = [(p["t"], p.get("fp"), p.get("reason")) for p in publishes if p.get("fp")]
    outliers = []
    for i in range(1, len(fp_seq) - 1):
        t_prev, fp_prev, _ = fp_seq[i - 1]
        t_cur, fp_cur, reason = fp_seq[i]
        t_next, fp_next, _ = fp_seq[i + 1]

        def dist(a, b):
            return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5

        d_prev, d_next = dist(fp_prev, fp_cur), dist(fp_cur, fp_next)
        if d_prev > 40.0 and d_next > 40.0:
            outliers.append((t_cur, reason, round(d_prev, 1), round(d_next, 1), fp_cur))
    if outliers:
        print(f"[diag] *** {len(outliers)} FINGERPRINT OUTLIER(S) — stale frame published ***")
        for t_cur, reason, d_prev, d_next, fp in outliers[:20]:
            print(f"  - t={t_cur:.1f}ms reason={reason} d_prev={d_prev} d_next={d_next} fp={fp}")
    elif fp_seq:
        print(f"[diag] fingerprint continuity OK across {len(fp_seq)} publishes")

    if suspects:
        print(f"[diag] *** {len(suspects)} FLASH SUSPECT(S) ***")
        for kind, inv, detail in suspects[:20]:
            print(f"  - {kind} around invalidate t={inv['t']:.1f}ms -> {detail}")
    else:
        print("[diag] no publish anomaly found around param changes; "
              "the flash may be a GPU timing issue (same-frame stale blit)")

    # Show the exact event sequence around each invalidate for manual review
    for i, inv in enumerate(invalidates[:3]):
        t0, t1 = inv["t"] - 200.0, inv["t"] + 400.0
        print(f"[diag] sequence around invalidate #{i + 1} (t={inv['t']:.1f}ms):")
        for e in events:
            if t0 <= e.get("t", -1) <= t1 and e.get("k") in (
                "publish", "drop", "video_cmd", "submit", "tick_submit", "params_invalidate",
                "open_clip_in_color", "load_video", "clip_playback_context",
            ):
                print(f"    {e['t']:>10.1f} {e.get('k'):<16} "
                      + " ".join(f"{key}={val}" for key, val in e.items() if key not in ("t", "k")))


if __name__ == "__main__":
    raise SystemExit(main())
