from __future__ import annotations
import json, os, sys, time, traceback
from pathlib import Path

ROOT = Path("/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01")
SESSION = Path("/tmp/topos-edit-audit-1020/session3")
OUT = Path("/tmp/topos-edit-audit-1020/out3")
SESSION.mkdir(parents=True, exist_ok=True); OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))
os.environ["TOPOS_CONFIG_PATH"] = str(SESSION / "config.json")
os.environ["TOPOS_PRODUCT_MODE"] = "studio"
os.environ["TOPOS_TEST_MODE"] = "1"
os.environ["TOPOS_FEATURE_EDIT"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["QSG_RHI_BACKEND"] = "opengl"

(SESSION / "config.json").write_text(json.dumps({
    "locale": "zh_CN", "current_page": "Edit", "work_mode": "video",
    "auto_load_last_project": False, "check_updates_on_startup": False,
    "show_welcome_on_startup": False,
    "scene_cut_sensitivity": 0.91,
    "scene_cut_use_proxy": False,
    "scene_cut_min_scene_frames": 2,
    "features": {"color": True, "edit": True, "composite": False,
                 "deliver": True, "teaching_animation": False, "ai_assistant": False},
}), encoding="utf-8")

import src.main  # noqa
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QSurfaceFormat
from PyQt6.QtWidgets import QApplication, QMenu
from PyQt6.QtTest import QTest

fmt = QSurfaceFormat(); fmt.setVersion(4, 5)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
fmt.setSamples(4); fmt.setDepthBufferSize(24); fmt.setStencilBufferSize(8)
QSurfaceFormat.setDefaultFormat(fmt)
QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
app = QApplication([sys.argv[0]])

from src.shared.config.config_manager import ConfigManager
from src.shared.feature_flags import load_feature_flags
from src.app.feature_registry import FeatureRegistry
from src.app.app_context import AppContext
from src.shared.i18n.i18n import I18N
from src.features.edit.module import EditModule
from src.features.color.module import ColorModule
from src.features.deliver.module import DeliverModule
from src.app.main_window import MainWindow

records: list[dict] = []
def record(name: str, **data):
    v = {"name": name, **data}
    records.append(v)
    (OUT / "records.json").write_text(json.dumps(records, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("AUDIT " + json.dumps(v, ensure_ascii=False, default=str), flush=True)

def wait_until(cond, timeout=60.0, step=0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QTest.qWait(int(step * 1000))
        if cond():
            return True
    return False

def rcv(obj, sig):
    try: return obj.receivers(sig)
    except Exception as exc: return f"ERR:{type(exc).__name__}"

config = ConfigManager(); flags = load_feature_flags(config)
I18N.load_locale("zh_CN")
registry = FeatureRegistry()
registry.initialize(AppContext(config_manager=config, feature_flags=flags, i18n=I18N, logger=None))
for m in (ColorModule(), EditModule(), DeliverModule()):
    registry.register_module(m)
mw = MainWindow(feature_flags=flags, feature_registry=registry, test_mode=False, startup_no_dialog=True)
mw.resize(1440, 1000); mw.show()
QTest.qWait(1200); mw.switch_to_page("Edit"); QTest.qWait(800)

video = str(ROOT / "tests/test_videos/2K_h264.mp4")
mw._auto_create_edit_project("Audit1020")
mp = mw.media_pool_panel
mp._import_files([video])
ok = wait_until(lambda: bool(getattr(mw._current_edit_project, "assets", None)), timeout=90)
record("import", ok=ok, assets=len(getattr(mw._current_edit_project, "assets", {}) or {}))
asset = next(iter(mw._current_edit_project.assets.values()))
record("asset", id=asset.asset_id, type=asset.media_type, fps=asset.fps, frames=asset.total_frames,
       duration=asset.duration_seconds, audio_channels=asset.audio_channels)

# Media Pool right-click "Add to Timeline" is a real UI entry; call its handler.
try:
    mw._on_add_asset_to_timeline(asset.asset_id)
    QTest.qWait(1200)
    tl0 = mw.timeline_panel.timeline_view._timeline
    record("media_pool_add_to_timeline", ok=True,
           clips=[(t.track_id, t.track_type, len(t.clips)) for t in (tl0.all_tracks if tl0 else [])])
except BaseException as exc:
    record("media_pool_add_to_timeline", ok=False, error=f"{type(exc).__name__}: {exc}",
           traceback=traceback.format_exc()[-1200:])

tl = mw.timeline_panel.timeline_view._timeline
if tl is None or not any(t.clips for t in tl.all_tracks):
    mw.timeline_panel.append_clip(asset_id=asset.asset_id, in_frame=0, out_frame=asset.total_frames - 1)
    QTest.qWait(1200)
    tl = mw.timeline_panel.timeline_view._timeline

def tl_summary():
    if tl is None: return None
    return {"revision": tl.revision, "fps": str(tl.fps), "tracks": [
        {"id": t.track_id, "type": t.track_type, "clips": [
            {"id": c.clip_id, "start": c.timeline_start, "end": c.timeline_end,
             "src": c.source_start, "dur": c.source_duration, "link": c.linked_clip_id,
             "asset": c.asset_id, "effects": len(c.effects or ())} for c in t.clips]}
        for t in tl.all_tracks]}
record("timeline_ready", summary=tl_summary(),
       project_timelines=list((mw._current_edit_project.timelines or {}).keys()),
       current_sequences=list((mw._current_timeline_sequences or {}).keys()))

# Menu audit.
def walk_menu(menu: QMenu, path=""):
    out = []
    for act in menu.actions():
        label = act.text().replace("&", "")
        sub = act.menu()
        if sub is not None: out.extend(walk_menu(sub, path + "/" + label))
        else: out.append({"path": path + "/" + label, "enabled": act.isEnabled()})
    return out
menu_actions = []
for act in mw.menuBar().actions():
    if act.menu() is not None: menu_actions.extend(walk_menu(act.menu(), act.text().replace("&", "")))
timeline_menu = [a for a in menu_actions if "时间线" in a["path"]]
record("menu_actions", timeline=timeline_menu,
       edit_top=[a for a in menu_actions if a["path"].startswith("编辑")])

tv = mw.timeline_panel.timeline_view
# E22: mp.asset_removed 信号已删除（死信号清理），不再纳入接收者检查。
record("signal_receivers",
       status_message_requested=rcv(tv, tv.status_message_requested),
       playback_state_changed=rcv(mw.timeline_panel, mw.timeline_panel.playback_state_changed),
       open_asset_grade_requested=rcv(mp, mp.open_asset_grade_requested),
       asset_removed="removed_by_E22")

# Effect + keyframe toggle.
from src.features.edit.core.commands import AddEffectCommand
clip = next(c for t in tl.video_tracks for c in t.clips)
tv.execute_command(AddEffectCommand.create(track_id=clip.track_id, clip_id=clip.clip_id, effect_type="saturation"))
QTest.qWait(300)
tl = tv._timeline; clip = tl.get_clip_by_id(clip.clip_id); eff = clip.effects[0]
param = next(iter(dict(getattr(eff, "params", {}) or {"saturation": 0.0})))
record("effect_added", effect_id=eff.effect_id, type=eff.effect_type, param=param, params=dict(eff.params))
try:
    mw._on_edit_effect_keyframe_toggled(clip.clip_id, eff.effect_id, param, 5, 0.5)
    record("keyframe_toggle", ok=True)
except BaseException as exc:
    record("keyframe_toggle", ok=False, error=f"{type(exc).__name__}: {exc}")

# Hardcoded shortcut: Ctrl+Shift+X should Split, but X is Cut.
from src.features.edit.core.selection import TimelineSelection, SelectionItem, SelectionType
cut_calls, split_calls = [], []
orig_cut, orig_split = tv._cut_selected_clips, tv._split_all_at_playhead
tv._cut_selected_clips = lambda *a, **k: (cut_calls.append(1), orig_cut(*a, **k))[1]
tv._split_all_at_playhead = lambda *a, **k: (split_calls.append(1), orig_split(*a, **k))[1]
item = SelectionItem(item_type=SelectionType.CLIP, item_id=clip.clip_id, track_id=clip.track_id, timeline_id=tl.timeline_id)
tv.set_selection(TimelineSelection(items=(item,), primary_item=item))
# E03: Split 的 Ctrl+Shift+X 由菜单 QAction 的 WindowShortcut 快捷键分发，
# 要求 mw 是 QApplication 的 active window。脚本从后台 shell 启动时 macOS
# 不一定前置窗口（此时 split_calls 会假阴性 0），显式设置使分发可复现；
# Cut 的 Ctrl+X 分支走 TimelineView 本地按键处理，不受激活状态影响。
from PyQt6.QtWidgets import QApplication
QApplication.setActiveWindow(mw)
QTest.keyClick(tv, Qt.Key.Key_X, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
QTest.qWait(300)
record("ctrl_shift_x", cut_calls=len(cut_calls), split_calls=len(split_calls))
tv._cut_selected_clips, tv._split_all_at_playhead = orig_cut, orig_split

# Automation recording guard.
track = tl.get_track_by_id(clip.track_id)
record("automation", edit_page_has_is_playing=hasattr(mw.edit_page, "_is_playing"),
       edit_page_is_playing=getattr(mw.edit_page, "_is_playing", None))
try:
    # E05 修复后：未播放时不采集（recorders=0 是正确行为）。
    before = dict(track.automation or {})
    mw._record_fader_sample(clip.track_id, "volume", 0.5)
    after = dict(track.automation or {})
    record("automation_record_sample", ok=True, changed=before != after,
           recorders=len(getattr(mw, "_automation_recorders", {})))
    # E05 主验证：播放中推子采样必须被采集（此前恒 0）。
    mw._automation_mode = "Write"
    panel = mw.timeline_panel
    controller = panel.get_playback_controller()
    controller.play()
    QTest.qWait(400)
    mw._record_fader_sample(clip.track_id, "volume", 0.7)
    recorder_count_playing = len(getattr(mw, "_automation_recorders", {}))
    controller.stop()
    QTest.qWait(200)
    record("automation_playing_sample", ok=True,
           recorders=recorder_count_playing,
           committed=len(mw.timeline_panel.timeline_view._command_stack._undo_stack))
except BaseException as exc:
    record("automation_record_sample", ok=False, error=f"{type(exc).__name__}: {exc}")

# Playback state signal.
pb_states = []
mw.timeline_panel.playback_state_changed.connect(lambda *a: pb_states.append(a))
mw.timeline_panel.set_playhead_frame(0)
mw.timeline_panel.start_playback()
QTest.qWait(2500)
frame = mw.timeline_panel.get_current_frame()
mw.timeline_panel.pause_playback()
QTest.qWait(300)
record("playback", advanced=frame > 0, frame=frame, playback_state_emissions=len(pb_states))

# Scene cut preference consumption.
sc = mw.edit_page._ensure_scene_cut_controller()
settings = sc._get_default_settings() if sc else None
record("scene_cut_prefs", controller=sc is not None,
       configured={"sensitivity": 0.91, "use_proxy": False, "min_scene_frames": 2},
       actual=({"sensitivity": settings.sensitivity, "use_proxy": settings.use_proxy,
                "min_scene_frames": settings.min_scene_frames} if settings else None))

# Save/load roundtrip using production IO.
try:
    from src.features.edit.core.edit_project_io import EditProjectIO
    io = EditProjectIO()
    proj = mw._current_edit_project
    seq_map = dict(mw._current_timeline_sequences or {})
    if not seq_map and tl is not None:
        seq_map = {tl.timeline_id: tl}
    path = OUT / "audit-project.topos"
    saved = io.save_project(path, proj, timeline_sequences=seq_map, create_backup=False)
    loaded = io.load_project(path) if saved else None
    if loaded:
        p2, _, s2 = loaded
        record("save_load", ok=True, saved=saved, size=path.stat().st_size,
               assets=len(p2.assets), timelines=len(p2.timelines), sequences=len(s2 or {}),
               clip_counts=[len(t.clips) for t in tl.all_tracks])
    else:
        record("save_load", ok=False, saved=saved)
    io.cleanup()
except BaseException as exc:
    record("save_load", ok=False, error=f"{type(exc).__name__}: {exc}")

(OUT / "records.json").write_text(json.dumps(records, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
sys.stdout.flush(); sys.stderr.flush()
os._exit(0)
