#!/usr/bin/env python3
"""Drive the REAL app Edit timeline: N videos → N video tracks, measure playback.

Builds the timeline the way a user would (media pool import → drop on track),
starts transport playback and measures the real end-to-end frame rate:

  - presented frames (timeline_preview_renderer.frame_ready emissions)
  - playhead progression rate (effective timeline fps)
  - dropped frames (playback controller)
  - realtime-engine telemetry breakdown (decode/composite/total ms)

Usage:
    .venv/bin/python tests/benchmark/drive_real_app_edit_tracks.py \\
        tests/test_videos/2K_h264.mp4 [video2 ...] [seconds]

1 video → single track (V1); 2+ videos → one track each (V1, V2, ...).
A real app window appears during the run.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
import statistics
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

if not os.environ.get("TOPOS_CONFIG_PATH"):
    os.environ["TOPOS_CONFIG_PATH"] = str(REPO_ROOT / ".topos" / "config.json")

os.environ.setdefault("TOPOS_PRODUCT_MODE", "studio")
os.environ["TOPOS_TEST_MODE"] = "1"
os.environ["TOPOS_FEATURE_EDIT"] = "1"


def log(msg: str) -> None:
    print(f"[DRIVER] {msg}", flush=True)


_paint_stats: dict[str, list[float]] = {}


def install_paint_trace() -> None:
    """Time paintGL/paintEvent of the heavy widgets to locate UI-thread stalls."""
    def wrap(cls, method_name: str, label: str) -> None:
        orig = getattr(cls, method_name, None)
        if orig is None:
            return

        def traced(self, *a, **kw):
            t0 = time.perf_counter()
            try:
                return orig(self, *a, **kw)
            finally:
                _paint_stats.setdefault(label, []).append(
                    (time.perf_counter() - t0) * 1000)
        setattr(cls, method_name, traced)

    from PyQt6.QtWidgets import QWidget
    from src.features.edit.ui.timeline_view import TimelineView
    from src.features.edit.ui.program_viewer import ProgramViewer

    def wrap_tv_paint(self, event):
        t0 = time.perf_counter()
        try:
            return TimelineView._orig_paintEvent(self, event)
        finally:
            _paint_stats.setdefault("TimelineView.paintEvent", []).append(
                (time.perf_counter() - t0) * 1000)
            r = event.rect()
            _paint_stats.setdefault("TimelineView.paint_rect_w", []).append(r.width())
            _paint_stats.setdefault("TimelineView.paint_rect_h", []).append(r.height())
            _paint_stats.setdefault("TimelineView.paint_rect_x", []).append(r.x())

    if not hasattr(TimelineView, "_orig_paintEvent"):
        TimelineView._orig_paintEvent = TimelineView.paintEvent
        TimelineView.paintEvent = wrap_tv_paint

    # Deep breakdown of the two heavy widgets' internals（A/V 隔离专题：
    # 定位 paintEvent 内部具体耗时段）。
    for name in (
        "drawBackground", "_configure_layout_engine",
        "_draw_vertical_time_markers", "_paint_fixed_header_column",
        "drawForeground",
    ):
        wrap(TimelineView, name, f"TV.{name}")
    for name in (
        "_copy_shared_texture_to_display", "_render_texture",
        "_draw_guides_overlay", "_paint_subtitle_overlay",
    ):
        wrap(ProgramViewer, name, f"PV.{name}")

    def wrap_item(cls, label: str) -> None:
        orig = cls.__dict__.get("paint")
        if orig is None or getattr(cls, "_driver_traced", False):
            return
        cls._driver_traced = True

        def counting(self, painter, option, widget=None):
            t0 = time.perf_counter()
            try:
                return orig(self, painter, option, widget)
            finally:
                _paint_stats.setdefault(f"item.{label}", []).append(
                    (time.perf_counter() - t0) * 1000)
        cls.paint = counting

    from src.features.edit.ui.timeline_items import (
        ClipItem, PlayheadItem, TimecodeRulerItem)
    from src.features.edit.ui.timeline_items import TrackHeaderItem
    wrap_item(ClipItem, "ClipItem")
    wrap_item(PlayheadItem, "PlayheadItem")
    wrap_item(TimecodeRulerItem, "RulerItem")
    wrap_item(TrackHeaderItem, "TrackHeaderItem")

    if os.environ.get("TOPOS_ITEM_TRACE"):
        import traceback
        skip = int(os.environ.get("TOPOS_ITEM_TRACE_SKIP", "0"))
        stacks_printed = {"update": 0}

        orig_upd = TrackHeaderItem.update

        def traced_update(self, *a, **kw):
            stacks_printed["update"] += 1
            if skip < stacks_printed["update"] <= skip + 3:
                print(f"[ITEM-TRACE] TrackHeaderItem.update #{stacks_printed['update']}\n"
                      + "".join(traceback.format_stack(limit=8)[:-1]), flush=True)
            return orig_upd(self, *a, **kw)
        TrackHeaderItem.update = traced_update
    wrap(QWidget, "paintEvent", "QWidget.paintEvent(other)")
    wrap(ProgramViewer, "paintGL", "ProgramViewer.paintGL")
    wrap(ProgramViewer, "paintEvent", "ProgramViewer.paintEvent")


def parse_args() -> tuple[list[Path], float]:
    args = sys.argv[1:]
    videos: list[Path] = []
    seconds = 25.0
    for a in args:
        if a.replace(".", "", 1).isdigit():
            seconds = float(a)
        else:
            videos.append(Path(a).resolve())
    if not videos:
        videos = [REPO_ROOT / "tests/test_videos/2K_h264.mp4"]
    return videos, seconds


def main() -> int:
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QSurfaceFormat
    from PyQt6.QtWidgets import QApplication, QDialog

    VIDEOS, SECONDS = parse_args()
    STEADY_SKIP_S = 3.0  # skip initial rebuffer window for steady-state fps

    os.environ["QSG_RHI_BACKEND"] = "opengl"
    fmt = QSurfaceFormat()
    fmt.setVersion(4, 5)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    fmt.setRedBufferSize(8); fmt.setGreenBufferSize(8); fmt.setBlueBufferSize(8); fmt.setAlphaBufferSize(8)
    fmt.setDepthBufferSize(24); fmt.setStencilBufferSize(8)
    if sys.platform != "win32":
        fmt.setSamples(4)
    fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
    QSurfaceFormat.setDefaultFormat(fmt)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)

    if os.environ.get("TOPOS_PAINT_TRACE"):
        install_paint_trace()

    def _auto_dismiss_dialogs():
        for w in QApplication.topLevelWidgets():
            if isinstance(w, QDialog) and w.isVisible():
                log(f"auto-dismiss dialog: {type(w).__name__}")
                w.reject()

    _dialog_killer = QTimer()
    _dialog_killer.timeout.connect(_auto_dismiss_dialogs)
    _dialog_killer.start(200)

    from src.shared.config.config_manager import ConfigManager
    from src.shared.feature_flags import load_feature_flags
    from src.app.feature_registry import FeatureRegistry
    from src.app.app_context import AppContext
    from src.shared.i18n.i18n import I18N
    from src.app.main_window import MainWindow

    config_manager = ConfigManager()
    feature_flags = load_feature_flags(config_manager)
    I18N.load_locale("zh_CN")
    registry = FeatureRegistry()
    registry.initialize(AppContext(config_manager=config_manager, feature_flags=feature_flags, i18n=I18N, logger=None))
    from src.features.color.module import ColorModule
    registry.register_module(ColorModule())
    if feature_flags.is_enabled("composite"):
        from src.features.composite.module import CompositeModule
        m = CompositeModule(); registry.register_module(m); m.discover_and_register_plugins()
    if feature_flags.is_enabled("edit"):
        from src.features.edit.module import EditModule
        registry.register_module(EditModule())

    mw = MainWindow(feature_flags=feature_flags, feature_registry=registry, test_mode=False)
    mw.show()
    for _ in range(90):
        app.processEvents()
    settle_end = time.time() + 4.0
    while time.time() < settle_end:
        app.processEvents()
        time.sleep(0.02)
    log(f"booted page={mw.current_page} videos={[v.name for v in VIDEOS]} seconds={SECONDS}")

    # ── import all media (creates the project) ──
    mw.media_pool_panel._import_files([str(v) for v in VIDEOS])
    deadline = time.time() + 90
    project = None
    while time.time() < deadline:
        app.processEvents()
        project = getattr(mw, "_current_edit_project", None)
        if project is not None and all(
            project.get_asset_by_path(str(v)) is not None for v in VIDEOS
        ):
            break
        time.sleep(0.02)
    assert project is not None, "project not created"
    assets = [project.get_asset_by_path(str(v)) for v in VIDEOS]
    assert all(assets), "some assets missing after import"
    for v, a in zip(VIDEOS, assets):
        log(f"asset {v.name}: fps={a.metadata.get('fps')} frames={a.metadata.get('total_frames')}")

    # ── build the timeline ──
    mw.switch_to_page("Edit")
    for _ in range(60):
        app.processEvents()

    tv = mw.timeline_panel.timeline_view
    first_asset = assets[0]
    tv._ensure_first_asset_timeline_settings(first_asset)
    for _ in range(20):
        app.processEvents()
    timeline = mw.edit_page._current_timeline or tv._create_default_timeline(first_asset)

    # clip 1 → V1 (existing UI path)
    ok = tv._add_asset_as_clip(first_asset, timeline.video_tracks[0], 0)
    deadline = time.time() + 20
    while time.time() < deadline:
        app.processEvents()
        if mw.timeline_preview_renderer is not None:
            break
        time.sleep(0.02)
    src = getattr(tv, "_timeline", None) or timeline
    timeline = src

    # clips 2..N → V2.. (one extra video track per clip)
    from src.features.edit.core.timeline_v2 import EditTrack
    for track_idx, asset in enumerate(assets[1:], start=1):
        tl_with_track = timeline.with_track_added(EditTrack(
            track_id=str(uuid.uuid4()),
            name=f"V{track_idx + 1}",
            track_type="video",
            index=track_idx,
            clips=(),
            transitions=(),
            is_enabled=True,
            is_locked=False,
            is_muted=False,
            is_solo=False,
        ))
        timeline = tl_with_track
        try:
            mw.edit_page._current_timeline = timeline
        except Exception:
            pass
        mw._sync_timeline_to_views(timeline)
        if getattr(tv, "_timeline", None) is not None:
            tv._timeline = timeline
        for _ in range(10):
            app.processEvents()
        track_obj = timeline.video_tracks[track_idx] if track_idx < len(timeline.video_tracks) else None
        if track_obj is None:
            # 轨道顺序可能重排，按名称兜底查找
            track_obj = next((t for t in timeline.video_tracks if t.name == f"V{track_idx + 1}"), None)
        assert track_obj is not None, f"track V{track_idx + 1} not created"
        ok2 = tv._add_asset_as_clip(asset, track_obj, 0)
        # _add_asset_as_clip 之后 tv._timeline 又被替换为新的不可变时间线
        timeline = getattr(tv, "_timeline", None) or timeline
        log(f"track V{track_idx + 1}: clip added ok={ok2}")
        for _ in range(10):
            app.processEvents()

    try:
        mw.edit_page._current_timeline = timeline
    except Exception:
        pass
    mw._sync_timeline_to_views(timeline)
    for _ in range(30):
        app.processEvents()

    n_tracks = len([t for t in timeline.video_tracks if t.clips])
    n_clips = sum(len(t.clips) for t in timeline.video_tracks if t.clips)
    tl_fps = float(timeline.fps) if hasattr(timeline.fps, "num") else float(timeline.fps)
    log(f"timeline ready: fps={tl_fps} video_tracks={len(timeline.video_tracks)} "
        f"tracks_with_clips={n_tracks} total_clips={n_clips} first_clip_ok={ok}")
    assert n_clips >= 1, "no clips on timeline"

    # ── measure ──
    tl_renderer = mw.timeline_preview_renderer
    assert tl_renderer is not None, "timeline preview renderer missing"
    t0 = time.perf_counter()
    emissions: list[tuple[float, int]] = []
    tl_renderer.frame_ready.connect(
        lambda frame, result: emissions.append((time.perf_counter() - t0, int(frame))))

    controller = mw.timeline_panel.get_playback_controller()
    t0 = time.perf_counter()

    # Watchdog: dump the MainThread stack when a processEvents call blocks.
    import faulthandler
    import threading as _threading_mod
    _pe_active = {"at": 0.0, "fired": 0}

    def _watchdog() -> None:
        while _pe_active["at"] >= 0:
            started = _pe_active["at"]
            if started > 0 and time.perf_counter() - started > 0.08 \
                    and _pe_active["fired"] < 3:
                _pe_active["fired"] += 1
                print(f"\n[BLOCK-STACK #{_pe_active['fired']}] "
                      f"processEvents running for "
                      f"{(time.perf_counter() - started) * 1000:.0f}ms:",
                      flush=True)
                faulthandler.dump_traceback(file=sys.stdout)
            time.sleep(0.2)

    if os.environ.get("TOPOS_BLOCK_STACKS"):
        _watchdog_thread = _threading_mod.Thread(target=_watchdog, daemon=True)
        _watchdog_thread.start()

    controller.play()
    samples: list[tuple[float, int, bool]] = []
    blocking_peaks: list[tuple[float, float]] = []
    while time.perf_counter() - t0 < SECONDS:
        pe0 = time.perf_counter()
        _pe_active["at"] = pe0
        app.processEvents()
        _pe_active["at"] = 0.0
        pe_ms = (time.perf_counter() - pe0) * 1000
        if pe_ms > 25:
            blocking_peaks.append((time.perf_counter() - t0, pe_ms))
        try:
            head = int(controller.current_frame)
        except Exception:
            head = 0
        samples.append((time.perf_counter() - t0, head, bool(controller.is_playing)))
        time.sleep(0.004)
    still_playing = bool(controller.is_playing)
    try:
        controller.pause()
    except Exception:
        pass
    total_s = time.perf_counter() - t0

    # ── analysis ──
    print("\n===== RESULTS =====", flush=True)
    if blocking_peaks:
        print(f"BLOCKING processEvents calls (>25ms): n={len(blocking_peaks)}", flush=True)
        for t, ms in blocking_peaks[:15]:
            print(f"  +{t:.2f}s {ms:.1f}ms", flush=True)
    if _paint_stats:
        print("UI paint costs (ms):", flush=True)
        for label, costs in sorted(_paint_stats.items()):
            if label.startswith("TimelineView.paint_rect"):
                vs = sorted(costs)
                print(f"  {label:<28} n={len(vs):>5} p50={vs[len(vs)//2]:>6} "
                      f"p95={vs[int(len(vs)*0.95)]:>6} max={vs[-1]:>6}", flush=True)
                continue
            cs = sorted(costs)
            print(f"  {label:<28} n={len(cs):>5} p50={cs[len(cs)//2]:>6.2f} "
                  f"p95={cs[int(len(cs)*0.95)]:>7.2f} max={cs[-1]:>8.2f} "
                  f"sum={sum(cs)/1000:.2f}s", flush=True)
    print(f"videos={[v.name for v in VIDEOS]}", flush=True)
    print(f"timeline_fps={tl_fps} duration={total_s:.1f}s still_playing_at_end={still_playing}", flush=True)

    frames = [f for _, f, _ in samples]
    expected = tl_fps * total_s
    advanced = (frames[-1] - frames[0]) if frames else 0
    print(f"playhead: start={frames[0]} end={frames[-1]} advanced={advanced} "
          f"expected≈{int(expected)}", flush=True)
    print(f"EFFECTIVE_FPS={advanced / max(total_s, 1e-6):.2f} "
          f"(timeline {tl_fps:.0f}fps target, {advanced / max(expected, 1e-6) * 100:.0f}% realtime)", flush=True)

    distinct = len({f for _, f in emissions})
    print(f"presented: {len(emissions)} emissions, distinct={distinct}, "
          f"rate={len(emissions) / max(total_s, 1e-6):.1f}/s", flush=True)
    print(f"DROPPED_FRAMES={max(0, advanced - distinct)} "
          f"(playhead advanced {advanced}, presented distinct {distinct})", flush=True)
    try:
        log(f"controller.dropped_frames={controller.dropped_frames}")
    except Exception:
        pass

    # steady-state fps (skip initial rebuffer)
    if samples:
        ss = [(t, f) for t, f, _ in samples if t >= STEADY_SKIP_S]
        if len(ss) > 10 and ss[-1][1] > ss[0][1]:
            ss_s = ss[-1][0] - ss[0][0]
            print(f"STEADY_FPS={(ss[-1][1] - ss[0][1]) / max(ss_s, 1e-6):.2f} "
                  f"(after {STEADY_SKIP_S:.0f}s warmup, {ss_s:.1f}s window)", flush=True)
        ss_em = [t for t, _ in emissions if t >= STEADY_SKIP_S]
        if len(ss_em) > 10:
            ss_s2 = min(ss_em[-1], total_s) - STEADY_SKIP_S
            print(f"STEADY_PRESENTED_RATE={len(ss_em) / max(ss_s2, 1e-6):.1f}/s", flush=True)
        # playhead stall events (frozen ≥0.5s while playing)
        stall_n = 0
        last_t, last_f = ss[0] if ss else (0, frames[0])
        for t, f in (ss or []):
            if f == last_f and t - last_t >= 0.5:
                stall_n += 1
                last_t = t  # count one stall per frozen window
            elif f != last_f:
                last_t, last_f = t, f
        print(f"STALL_EVENTS(>=0.5s)={stall_n}", flush=True)

    # frame interval jitter of presented frames
    em_times = [t for t, _ in emissions]
    if len(em_times) > 10:
        gaps = [(em_times[i] - em_times[i - 1]) * 1000 for i in range(1, len(em_times))]
        gaps_sorted = sorted(gaps)
        print(f"presented interval ms: p50={gaps_sorted[len(gaps)//2]:.1f} "
              f"p95={gaps_sorted[int(len(gaps_sorted)*0.95)]:.1f} "
              f"max={gaps_sorted[-1]:.1f} (budget {1000/tl_fps:.1f})", flush=True)

    # realtime engine telemetry
    try:
        engine = getattr(tl_renderer, "_realtime_engine", None) or getattr(tl_renderer, "realtime_engine", None)
        tel = getattr(engine, "telemetry", None)
        if tel is not None:
            print(f"TELEMETRY={tel.summary()}", flush=True)
        stats = getattr(engine, "stats", None)
        if callable(stats):
            print(f"ENGINE_STATS={stats()}", flush=True)
        from src.shared.video.playback_frame_trace import get_trace_collector
        print(f"TRACE_COLLECTOR_SUMMARY={get_trace_collector().get_summary(last_n=120)}", flush=True)
    except Exception as e:
        print(f"[telemetry error: {e}]", flush=True)

    # decode manager stats (stream count / hit rates)
    try:
        worker = getattr(tl_renderer, "_worker", None)
        mgr = getattr(worker, "_gpu_decode_manager", None) if worker else None
        if mgr is not None:
            info = {}
            for attr in ("_streams", "_sources", "_stream_keys"):
                val = getattr(mgr, attr, None)
                if isinstance(val, dict):
                    info[attr] = list(val.keys())[:8]
            print(f"DECODE_MANAGER={info}", flush=True)
    except Exception as e:
        print(f"[decode-manager introspect error: {e}]", flush=True)

    print("===== END =====", flush=True)

    # 走正常关闭路径（closeEvent 停止后台线程），避免解释器直接拆除
    # 运行中的 QThread 导致退出段错误。
    try:
        controller.pause()
    except Exception:
        pass
    for _ in range(10):
        app.processEvents()
    if os.environ.get("TOPOS_EXIT_TRACE"):
        orig_shutdown = mw._shutdown_playback_pipeline

        def traced_shutdown():
            import traceback
            for name, fn in [
                ("pause", lambda: None),
            ]:
                pass
            print("[EXIT] shutdown start", flush=True)
            orig_shutdown()
            print("[EXIT] shutdown done", flush=True)
        mw._shutdown_playback_pipeline = traced_shutdown
    mw.close()
    print("[EXIT] mw.close() returned", flush=True)
    for _ in range(10):
        app.processEvents()
    print("[EXIT] driver done", flush=True)
    # 所有结果已落盘、线程已按序停止。解释器 finalization 阶段仍存在
    # 原生拆除崩溃（无 Python 栈，需 lldb 定位），此处直接退出避免
    # 基准被 exit 139 污染。
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
