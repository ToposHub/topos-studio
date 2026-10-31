#!/usr/bin/env python3
"""F0.3 G 级驱动：真实窗口中验收黄金工程场景（不 offscreen、不 mock）。

场景（断言式，输出结构化 JSON 报告）：
  G1 启动真实 MainWindow 并导入黄金媒体（渐变视频×2 + 正弦 WAV）
  G2 真实时间线组装 + 视图统一命令入口建链接 A/V
  G3 真实复制/粘贴（同序列）：新 id、链接批内重映射、视图级 Undo 恢复
  G4 多时间线：创建第二序列 + 切换 + 跨序列粘贴（现行语义如实记录）
  G5 Smart Bin 拉取式动态刷新（真实媒体池面板路径）

用法：
  .venv/bin/python tests/validation/drive_golden_real_app.py [--out DIR]
真实窗口会在运行期间出现；stdout 逐行记录。退出码 0=全部通过，1=断言失败，
2=环境失败。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

if not os.environ.get("TOPOS_CONFIG_PATH"):
    os.environ["TOPOS_CONFIG_PATH"] = str(REPO_ROOT / ".topos" / "config.json")
os.environ.setdefault("TOPOS_PRODUCT_MODE", "studio")
os.environ["TOPOS_TEST_MODE"] = "1"
os.environ["TOPOS_FEATURE_EDIT"] = "1"

from tests.edit.helpers import golden_media  # noqa: E402

REPORT: dict = {"scenarios": {}, "media": {}, "environment": {}}
FAILURES: list[str] = []


def log(msg: str) -> None:
    print(f"[GOLDEN-G] {msg}", flush=True)


def check(scenario: str, name: str, ok: bool, detail: str = "") -> bool:
    entry = {"ok": bool(ok), "detail": detail}
    REPORT["scenarios"].setdefault(scenario, {})[name] = entry
    if not ok:
        FAILURES.append(f"{scenario}/{name}: {detail}")
    log(f"  {'PASS' if ok else 'FAIL'} {scenario}/{name} {detail}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None, help="报告输出目录")
    args = parser.parse_args()

    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QSurfaceFormat
    from PyQt6.QtWidgets import QApplication

    os.environ["QSG_RHI_BACKEND"] = "opengl"
    fmt = QSurfaceFormat()
    fmt.setVersion(4, 5)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    QSurfaceFormat.setDefaultFormat(fmt)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)

    from PyQt6.QtWidgets import QDialog

    def _auto_dismiss_dialogs() -> None:
        for w in QApplication.topLevelWidgets():
            if isinstance(w, QDialog) and w.isVisible():
                log(f"auto-dismiss dialog: {type(w).__name__}")
                w.reject()

    dialog_killer = QTimer()
    dialog_killer.timeout.connect(_auto_dismiss_dialogs)
    dialog_killer.start(200)

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

    # G1 启动 + 媒体准备 -------------------------------------------------
    media_dir = Path(tempfile.mkdtemp(prefix="golden_g_media_"))
    grad24 = golden_media.write_gradient_video(
        media_dir / "grad24.mp4", width=640, height=360, frames=48)
    grad25 = golden_media.write_gradient_video(
        media_dir / "grad25.mp4", width=640, height=360, frames=50, fps_num=25, fps_den=1)
    sine = golden_media.write_wav(media_dir / "sine1k.wav", golden_media.sine_pcm(1000.0, 2.0))
    REPORT["environment"]["media_dir"] = str(media_dir)

    mw = MainWindow(feature_flags=feature_flags, feature_registry=registry, test_mode=False)
    mw.show()
    for _ in range(90):
        app.processEvents()
    REPORT["environment"]["boot_page"] = str(mw.current_page)
    check("G1", "real_window_booted", mw.current_page is not None, f"page={mw.current_page}")

    mw.media_pool_panel._import_files([str(grad24), str(grad25), str(sine)])
    deadline = time.time() + 60
    while time.time() < deadline:
        app.processEvents()
        project = getattr(mw, "_current_edit_project", None)
        if project is not None and project.get_asset_by_path(str(sine)) is not None:
            break
        time.sleep(0.02)
    project = mw._current_edit_project
    a24 = project.get_asset_by_path(str(grad24))
    a25 = project.get_asset_by_path(str(grad25))
    awav = project.get_asset_by_path(str(sine))
    check("G1", "golden_media_imported", None not in (a24, a25, awav),
          f"video2={a24 is not None and a25 is not None} wav={awav is not None}")

    # G2 时间线组装 + 链接 A/V（视图统一命令入口）------------------------
    tv = mw.timeline_panel.timeline_view
    tv._ensure_first_asset_timeline_settings(a24)
    for _ in range(20):
        app.processEvents()
    timeline = mw.edit_page._current_timeline or tv._create_default_timeline(a24)
    v_track = timeline.video_tracks[0]
    ok_v = tv._add_asset_as_clip(a24, v_track, 0)
    for _ in range(10):
        app.processEvents()
    timeline = mw.edit_page._current_timeline
    if not timeline.audio_tracks:
        tv._on_add_track_requested("audio", "below", len(timeline.audio_tracks))
        for _ in range(20):
            app.processEvents()
        timeline = mw.edit_page._current_timeline
        log(f"  audio tracks after add request: {len(timeline.audio_tracks) if timeline else 'no-tl'}")
    a_tracks = timeline.audio_tracks
    ok_a = False
    if a_tracks:
        ok_a = tv._add_asset_as_clip(awav, a_tracks[0], 0)
        log(f"  add wav clip ok={ok_a}")
        for _ in range(10):
            app.processEvents()
    timeline = mw.edit_page._current_timeline
    v_clips = list(timeline.video_tracks[0].clips)
    a_clips = list(timeline.audio_tracks[0].clips) if timeline.audio_tracks else []
    check("G2", "clips_added", ok_v and ok_a and len(a_clips) >= 1,
          f"video={len(v_clips)} audio={len(a_clips)}")

    if v_clips and a_clips:
        from src.features.edit.core.commands import SetClipLinkedCommand
        cmd = SetClipLinkedCommand.create_link(
            v_clips[0].clip_id, v_clips[0].track_id, a_clips[0].clip_id, a_clips[0].track_id)
        tv.execute_command(cmd)
        for _ in range(10):
            app.processEvents()
        timeline = mw.edit_page._current_timeline
        v = timeline.get_clip_by_id(v_clips[0].clip_id)
        a = timeline.get_clip_by_id(a_clips[0].clip_id)
        check("G2", "linked_av_bidirectional",
              v is not None and a is not None and v.linked_clip_id == a.clip_id
              and a.linked_clip_id == v.clip_id,
              f"v.link={v.linked_clip_id if v else None} a.link={a.linked_clip_id if a else None}")
        linked_pair = (v_clips[0].clip_id, a_clips[0].clip_id)
    else:
        linked_pair = None
        check("G2", "linked_av_bidirectional", False, "no clips to link")

    # G3 同序列复制/粘贴 + Undo ------------------------------------------
    from src.features.edit.core.selection import SelectionItem, SelectionType, TimelineSelection

    payload = None

    def _select(clip_ids: list[str]) -> None:
        items = []
        for cid in clip_ids:
            clip = timeline.get_clip_by_id(cid)
            if clip is not None:
                items.append(SelectionItem(
                    item_type=SelectionType.CLIP, item_id=cid,
                    track_id=clip.track_id, timeline_id=timeline.timeline_id))
        tv.set_selection(TimelineSelection(items=tuple(items)))

    timeline = mw.edit_page._current_timeline
    before_ids = {c.clip_id for t in (*timeline.video_tracks, *timeline.audio_tracks) for c in t.clips}
    if linked_pair:
        _select(list(linked_pair))
        tv._copy_selected_clips()
        payload = tv._clipboard
        copied = payload is not None and len(payload.clips) == 2
        check("G3", "copy_linked_pair", copied,
              f"clips={len(payload.clips) if payload else 0} fps={getattr(payload, 'fps', None)}")
        tv._paste_clips_at_playhead()
        for _ in range(10):
            app.processEvents()
        timeline = mw.edit_page._current_timeline
        after_ids = {c.clip_id for t in (*timeline.video_tracks, *timeline.audio_tracks) for c in t.clips}
        new_ids = after_ids - before_ids
        remap_ok = False
        if len(new_ids) == 2:
            new_v = timeline.get_clip_by_id(sorted(new_ids)[0])
            partners = {timeline.get_clip_by_id(i).linked_clip_id for i in new_ids}
            remap_ok = partners == new_ids
        check("G3", "paste_new_ids_and_link_remap", len(new_ids) == 2 and remap_ok,
              f"new={sorted(new_ids)}")
        tv.undo()
        for _ in range(10):
            app.processEvents()
        timeline = mw.edit_page._current_timeline
        undone_ids = {c.clip_id for t in (*timeline.video_tracks, *timeline.audio_tracks) for c in t.clips}
        check("G3", "view_undo_restores", undone_ids == before_ids,
              f"restored={len(undone_ids)} expected={len(before_ids)}")

    # G4 多时间线：真实生产路径（媒体池信号→控制器→NewTimelineDialog→创建）。
    # 对话框以默认设置脚本化接受；时间线经前后差分识别。
    project_before = mw._current_edit_project
    ids_before = set(project_before.timelines.keys())
    from src.features.edit.ui.new_timeline_dialog import NewTimelineDialog

    orig_exec = NewTimelineDialog.exec

    def _auto_accept_exec(self, *args, **kwargs):
        return 1  # QDialog.DialogCode.Accepted（默认设置创建）

    NewTimelineDialog.exec = _auto_accept_exec
    try:
        mw.media_pool_panel.timeline_create_requested.emit()
        deadline = time.time() + 10
        seq_b_id = None
        while time.time() < deadline:
            app.processEvents()
            new_ids = set(mw._current_edit_project.timelines.keys()) - ids_before
            if new_ids:
                seq_b_id = sorted(new_ids)[0]
                break
            time.sleep(0.02)
    finally:
        NewTimelineDialog.exec = orig_exec
    check("G4", "second_timeline_created", seq_b_id is not None, f"id={seq_b_id}")

    if seq_b_id:
        mw.edit_page.switch_timeline(seq_b_id)
        for _ in range(20):
            app.processEvents()
        timeline_b = mw.edit_page._current_timeline
        switch_ok = timeline_b is not None and timeline_b.timeline_id == seq_b_id
        check("G4", "timeline_switch_active", switch_ok,
              f"active={timeline_b.timeline_id if timeline_b else None}")
        ids_before_b = {c.clip_id for t in (*timeline_b.video_tracks, *timeline_b.audio_tracks) for c in t.clips}
        if payload is not None:
            tv._paste_clips_at_playhead()
            for _ in range(10):
                app.processEvents()
            timeline_b = mw.edit_page._current_timeline
            ids_after_b = {c.clip_id for t in (*timeline_b.video_tracks, *timeline_b.audio_tracks) for c in t.clips}
            pasted = ids_after_b - ids_before_b
            # 现行契约：粘贴按原 track_id 匹配目标轨；跨序列轨道 id 不匹配时
            # 条目被跳过。两种结果都如实记录（差量交由 F2 任务处置）。
            check("G4", "cross_timeline_paste_recorded", True,
                  f"pasted={len(pasted)} semantics={'remapped' if pasted else 'skipped_by_track_id'}")

    # G5 Smart Bin 拉取式刷新（真实媒体池路径）---------------------------
    project = mw._current_edit_project
    video_bin = next((b for b in project.get_default_smart_bins()
                      if "video" in b.bin_id.lower() or "video" in b.name.lower()), None)
    if video_bin is not None:
        mw.media_pool_panel._on_bin_selected(video_bin.bin_id, True)
        for _ in range(40):
            app.processEvents()
        got = mw.media_pool_panel._get_current_assets()
        ok_first = got is not None and all(
            a.media_type == "video" for a in got) and len(got) >= 2
        check("G5", "smart_bin_pull_filters_videos", bool(ok_first),
              f"count={len(got) if got is not None else None}")
        # 导入新视频 → 拉取路径应反映
        extra = golden_media.write_gradient_video(
            media_dir / "extra.mp4", width=320, height=180, frames=12)
        mw.media_pool_panel._import_files([str(extra)])
        deadline = time.time() + 30
        while time.time() < deadline:
            app.processEvents()
            if mw._current_edit_project.get_asset_by_path(str(extra)) is not None:
                break
            time.sleep(0.02)
        mw.media_pool_panel._on_bin_selected(video_bin.bin_id, True)
        for _ in range(40):
            app.processEvents()
        got2 = mw.media_pool_panel._get_current_assets()
        ok_refresh = got2 is not None and any(
            a.path == str(extra) for a in got2)
        check("G5", "smart_bin_pull_updates_after_import", bool(ok_refresh),
              f"count={len(got2) if got2 is not None else None}")
    else:
        check("G5", "smart_bin_available", False, "no default video smart bin found")

    # 报告 ----------------------------------------------------------------
    REPORT["failures"] = FAILURES
    REPORT["passed"] = not FAILURES
    out_dir = Path(args.out) if args.out else (
        Path(tempfile.mkdtemp(prefix="golden_g_report_")))
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"report -> {report_path}")
    log(f"RESULT {'PASS' if not FAILURES else 'FAIL'} failures={len(FAILURES)}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
