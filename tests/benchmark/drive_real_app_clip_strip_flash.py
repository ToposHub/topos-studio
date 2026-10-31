#!/usr/bin/env python3
"""Drive the REAL Color page and verify the Timeline Clips strip never blanks.

Scenario under test (user report): playing across a clip boundary and
switching clips used to flash the whole strip black, because every grading
commit eagerly rebuilt all cards and the recreated cards paint the dark
placeholder until async thumbnails return.

This driver reproduces the exact path — two same-source, non-contiguous
clips (scene-cut style) so playback crosses the wrap-advance boundary —
and asserts the DaVinci-style behaviour:
  1. No strip rebuild while playback is running.
  2. Card thumbnails stay present across the boundary and across a manual
     clip switch (no black flash).
  3. The deferred idle refresh does run after playback stops, and rebuilds
     with thumbnails re-primed from cache in the same pass.

Usage:
  .venv/bin/python tests/benchmark/drive_real_app_clip_strip_flash.py <video>
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("TOPOS_PRODUCT_MODE", "studio")
os.environ["TOPOS_TEST_MODE"] = "1"
os.environ["TOPOS_FEATURE_EDIT"] = "1"
if not os.environ.get("TOPOS_CONFIG_PATH"):
    os.environ["TOPOS_CONFIG_PATH"] = str(REPO_ROOT / ".topos" / "config.json")

VIDEO = Path(next(a for a in sys.argv[1:] if not a.startswith("--"))).resolve()

FAILURES: list[str] = []


def log(msg: str) -> None:
    print(f"[STRIPDIAG] {msg}", flush=True)


def check(name: str, ok: bool, detail: str = "") -> None:
    tag = "PASS" if ok else "FAIL"
    log(f"{tag} {name} {detail}")
    if not ok:
        FAILURES.append(name)


def main() -> int:
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QSurfaceFormat
    from PyQt6.QtWidgets import QApplication, QDialog

    fmt = QSurfaceFormat()
    fmt.setVersion(4, 5)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    QSurfaceFormat.setDefaultFormat(fmt)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)

    from src.shared.config.config_manager import ConfigManager
    from src.shared.feature_flags import load_feature_flags
    from src.app.feature_registry import FeatureRegistry
    from src.app.app_context import AppContext
    from src.shared.i18n.i18n import I18N
    from src.app.main_window import MainWindow

    cm = ConfigManager()
    ff = load_feature_flags(cm)
    I18N.load_locale("zh_CN")
    reg = FeatureRegistry()
    reg.initialize(AppContext(config_manager=cm, feature_flags=ff, i18n=I18N, logger=None))
    from src.features.color.module import ColorModule
    reg.register_module(ColorModule())
    from src.features.edit.module import EditModule
    reg.register_module(EditModule())

    def _auto_dismiss_dialogs():
        for w in QApplication.topLevelWidgets():
            if isinstance(w, QDialog) and w.isVisible():
                log(f"auto-dismiss dialog: {type(w).__name__}")
                w.reject()

    _dialog_killer = QTimer()
    _dialog_killer.timeout.connect(_auto_dismiss_dialogs)
    _dialog_killer.start(200)

    mw = MainWindow(feature_flags=ff, feature_registry=reg, test_mode=False)
    mw.show()
    for _ in range(60):
        app.processEvents()

    # ---- import + two same-source clips (non-contiguous ranges) ----
    mw.media_pool_panel._import_files([str(VIDEO)])
    t0 = time.time()
    asset = None
    while time.time() - t0 < 60:
        app.processEvents()
        time.sleep(0.02)
        p = getattr(mw, "_current_edit_project", None)
        if p is not None:
            asset = p.get_asset_by_path(str(VIDEO))
            if asset is not None:
                break
    assert asset, "import failed"
    tv = mw.timeline_panel.timeline_view
    tv._ensure_first_asset_timeline_settings(asset)
    for _ in range(20):
        app.processEvents()
    tl = mw.edit_page._current_timeline or tv._create_default_timeline(asset)
    t0 = time.time()
    while time.time() - t0 < 20:
        app.processEvents()
        if mw.timeline_preview_renderer:
            break
        time.sleep(0.02)

    # Build the user's timeline shape: two scene-cut sub-clips of the SAME
    # source file with a gap between their source ranges (non-contiguous),
    # i.e. exactly what scene detection produces and what takes the
    # wrap-advance path on playback.
    from src.features.edit.core.scene_cut.models import SceneCutKind, SceneSubclipSpec
    from src.features.edit.core.scene_cut.subclips import create_subclip_asset

    fps = float(tl.fps) if not hasattr(tl.fps, "num") else (
        tl.fps.num / tl.fps.den
    )

    def _spec(scene_index: int, start: int, dur: int) -> SceneSubclipSpec:
        return SceneSubclipSpec(
            parent_asset_id=asset.asset_id,
            source_path=str(VIDEO),
            scene_index=scene_index,
            source_start=start,
            source_duration=dur,
            start_seconds=start / fps,
            duration_seconds=dur / fps,
            display_name=f"scene {scene_index}",
            start_timecode=f"00:00:{start // int(fps):02d}:{start % int(fps):02d}",
            end_timecode=f"00:00:{(start + dur) // int(fps):02d}:{(start + dur) % int(fps):02d}",
            thumbnail_source_frame=start,
            detection_confidence=1.0,
            detection_kind=SceneCutKind.HARD_CUT,
        )

    short = 24
    sub_a = create_subclip_asset(_spec(1, 0, short), "driver-analysis", asset)
    sub_b = create_subclip_asset(_spec(2, short * 2, short), "driver-analysis", asset)
    project = mw._current_edit_project
    project.assets[sub_a.asset_id] = sub_a
    project.assets[sub_b.asset_id] = sub_b
    tv._add_asset_as_clip(sub_a, tl.video_tracks[0], 0)
    tv._add_asset_as_clip(sub_b, tl.video_tracks[0], short)
    t0 = time.time()
    while time.time() - t0 < 10:
        app.processEvents()
        time.sleep(0.02)
        tl = mw.edit_page._current_timeline or tl
        if len(tl.video_tracks[0].clips) >= 2:
            break

    clip_a, clip_b = tl.video_tracks[0].clips[0], tl.video_tracks[0].clips[1]
    log(f"timeline ready: A={clip_a.source_start}+{clip_a.source_duration} "
        f"B={clip_b.source_start}+{clip_b.source_duration} same_path="
        f"{project.assets[clip_a.asset_id].path == project.assets[clip_b.asset_id].path}")

    mw.open_timeline_clip_in_color(tl.timeline_id, clip_a.clip_id, 0)
    t0 = time.time()
    while time.time() - t0 < 30:
        app.processEvents()
        time.sleep(0.02)
        if getattr(mw, "current_page", "") == "Color" and getattr(mw, "video_controller", None):
            if mw.video_controller.is_video_loaded():
                break
    vc = mw.video_controller
    strip = mw.color_timeline_clip_strip
    log(f"color page ready loaded={vc.is_video_loaded()} cards={len(strip._cards)}")

    # ---- instrumentation ----
    stats = {
        "rebuilds": 0,
        "refreshes": 0,
        "rebuilds_during_playback": 0,
        "priming_violations": 0,
    }

    _orig_rebuild = strip._rebuild_cards

    def _counted_rebuild():
        had = {
            card.entry.clip_id: card._pixmap is not None
            for card in strip._cards
        }
        _orig_rebuild()
        stats["rebuilds"] += 1
        if stats["rebuilds"] <= 3:
            import traceback
            origin = "".join(
                traceback.format_stack(limit=7)[-4:-1]
            ).replace("\n", " | ")
            log(f"rebuild#{stats['rebuilds']} origin: {origin}")
        if bool(getattr(vc, "is_playing", False)):
            stats["rebuilds_during_playback"] += 1
        for card in strip._cards:
            if had.get(card.entry.clip_id) and card._pixmap is None:
                stats["priming_violations"] += 1
                from src.features.color.ui.timeline_clip_strip import (
                    _compute_preview_frame,
                )
                entry = card.entry
                frame = _compute_preview_frame(entry, card._preview_mode)
                probe = None
                if entry.media_type != "image":
                    from src.shared.media.thumbnail_provider import ThumbnailProvider
                    probe = ThumbnailProvider.instance().get_cached_frame_pixmap(
                        entry.asset_id, frame, 144, path=entry.asset_path,
                    )
                log(f"priming miss: clip={entry.clip_id} media={entry.media_type} "
                    f"frame={frame} probe={'hit' if probe is not None else 'MISS'} "
                    f"src={entry.source_start}+{entry.source_duration} "
                    f"mode={card._preview_mode}")

    strip._rebuild_cards = _counted_rebuild

    _orig_refresh = mw.refresh_color_timeline_clip_strip

    def _counted_refresh():
        stats["refreshes"] += 1
        return _orig_refresh()

    mw.refresh_color_timeline_clip_strip = _counted_refresh

    def all_cards_have_pixmaps() -> bool:
        return bool(strip._cards) and all(c._pixmap is not None for c in strip._cards)

    # Baseline: let the async thumbnails arrive for both cards.
    t0 = time.time()
    while time.time() - t0 < 15 and not all_cards_have_pixmaps():
        app.processEvents()
        time.sleep(0.02)
    check("baseline thumbnails present", all_cards_have_pixmaps(),
          f"cards={len(strip._cards)} wait={time.time()-t0:.1f}s")
    stats["rebuilds"] = 0
    stats["refreshes"] = 0

    # ---- Phase 1: playback across the boundary ----
    vc.start_playback()
    crossed = False
    t0 = time.time()
    while time.time() - t0 < 20:
        app.processEvents()
        time.sleep(0.01)
        if strip._active_clip_id == clip_b.clip_id:
            crossed = True
            break
    vc.stop_playback()
    for _ in range(30):
        app.processEvents()
    check("playback crossed into clip B", crossed,
          f"active={strip._active_clip_id}")
    check("no strip rebuild while playing", stats["rebuilds_during_playback"] == 0,
          f"rebuilds_during_playback={stats['rebuilds_during_playback']}")
    check("cards keep thumbnails across boundary", all_cards_have_pixmaps())

    # ---- Phase 2: deferred idle refresh after playback stops ----
    t0 = time.time()
    while time.time() - t0 < 3 and stats["refreshes"] == 0:
        app.processEvents()
        time.sleep(0.02)
    check("idle refresh ran after stop", stats["refreshes"] >= 1,
          f"refreshes={stats['refreshes']} rebuilds={stats['rebuilds']}")
    check("idle rebuild kept thumbnails (cache priming)",
          stats["priming_violations"] == 0,
          f"violations={stats['priming_violations']}")

    # ---- Phase 3: manual clip switch ----
    stats["rebuilds"] = 0
    stats["refreshes"] = 0
    stats["priming_violations"] = 0
    # Switch to clip A — a DIFFERENT clip than the active B, matching the
    # user's manual switch scenario (stack flush -> commit -> deferred).
    # The old behaviour rebuilt the strip synchronously INSIDE this call
    # (commit -> refresh -> rebuild), so compare before/after directly.
    before = stats["rebuilds"]
    mw.open_timeline_clip_in_color(tl.timeline_id, clip_a.clip_id, 0)
    check("no eager rebuild during switch call",
          stats["rebuilds"] == before, f"rebuilds={stats['rebuilds']}")
    t0 = time.time()
    while time.time() - t0 < 30:
        app.processEvents()
        time.sleep(0.02)
        if getattr(mw, "current_page", "") == "Color" and vc.is_video_loaded():
            if int(getattr(vc, "current_frame", -1) or -1) >= 0:
                break
    check("cards keep thumbnails after switch", all_cards_have_pixmaps(),
          f"active={strip._active_clip_id}")

    # And the deferred refresh still lands afterwards.
    t0 = time.time()
    while time.time() - t0 < 3 and stats["refreshes"] == 0:
        app.processEvents()
        time.sleep(0.02)
    check("deferred refresh after switch", stats["refreshes"] >= 1,
          f"refreshes={stats['refreshes']} violations={stats['priming_violations']}")

    log(f"final stats: {stats}")
    if FAILURES:
        log(f"RESULT: FAILED {FAILURES}")
        return 1
    log("RESULT: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
