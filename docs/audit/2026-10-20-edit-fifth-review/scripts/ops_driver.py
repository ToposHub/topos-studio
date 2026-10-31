from __future__ import annotations
import json, os, sys, time, traceback
from pathlib import Path
ROOT = Path("/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01")
SESSION = Path("/tmp/topos-edit-audit-1020/session4")
OUT = Path("/tmp/topos-edit-audit-1020/out4")
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
    "features": {"color": True, "edit": True, "composite": False, "deliver": True,
                 "teaching_animation": False, "ai_assistant": False},
}), encoding="utf-8")
import src.main  # noqa
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QSurfaceFormat
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest
fmt = QSurfaceFormat(); fmt.setVersion(4,5); fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
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
from src.features.edit.core.selection import TimelineSelection, SelectionItem, SelectionType
from src.features.edit.ui.timeline_items import ClipItem

records=[]
def record(name, **d):
    v={"name":name,**d}; records.append(v)
    (OUT/"records.json").write_text(json.dumps(records,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    print("OPS "+json.dumps(v,ensure_ascii=False,default=str),flush=True)
def wait_until(cond,timeout=60.0,step=0.2):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        QTest.qWait(int(step*1000))
        if cond(): return True
    return False
def counts(tl):
    return {t.track_type: len(t.clips) for t in tl.all_tracks}
def summary(tl):
    return {"rev":tl.revision,"counts":counts(tl),"markers":len(getattr(tl,"markers",())),
            "transitions":sum(len(getattr(t,"transitions",()) or ()) for t in tl.all_tracks)}

config=ConfigManager(); flags=load_feature_flags(config); I18N.load_locale("zh_CN")
registry=FeatureRegistry(); registry.initialize(AppContext(config_manager=config,feature_flags=flags,i18n=I18N,logger=None))
for m in (ColorModule(),EditModule(),DeliverModule()): registry.register_module(m)
mw=MainWindow(feature_flags=flags,feature_registry=registry,test_mode=False,startup_no_dialog=True)
mw.resize(1440,1000); mw.show(); QTest.qWait(1200); mw.switch_to_page("Edit"); QTest.qWait(800)
mw._auto_create_edit_project("Ops1020"); mp=mw.media_pool_panel
mp._import_files([str(ROOT/"tests/test_videos/2K_h264.mp4")])
wait_until(lambda: bool(getattr(mw._current_edit_project,"assets",None)),timeout=90)
asset=next(iter(mw._current_edit_project.assets.values()))
mw.timeline_panel.append_clip(asset_id=asset.asset_id,in_frame=0,out_frame=asset.total_frames-1)
wait_until(lambda: mw.timeline_panel.timeline_view._timeline is not None,timeout=30); QTest.qWait(1000)
tv=mw.timeline_panel.timeline_view; tl=tv._timeline
record("initial", summary=summary(tl))

# 1) Split at playhead via keyboard Ctrl+B.
tv.set_playhead(600); QTest.qWait(200)
QTest.keyClick(tv, Qt.Key.Key_B, Qt.KeyboardModifier.ControlModifier); QTest.qWait(500)
tl=tv._timeline
record("split_ctrl_b", summary=summary(tl),
       links_ok=all((c.linked_clip_id is None) or (tl.get_clip_by_id(c.linked_clip_id) is not None) for t in tl.all_tracks for c in t.clips))
after_split=summary(tl)

# 2) Undo / Redo via view.
tv.undo(); QTest.qWait(400); tl=tv._timeline; record("undo_split", summary=summary(tl), restored=summary(tl)==record_init if False else None)
tv.redo(); QTest.qWait(400); tl=tv._timeline; record("redo_split", summary=summary(tl))

# 3) Copy / paste selected video clip at playhead 200.
vclip=next(c for t in tl.video_tracks for c in t.clips)
item=SelectionItem(item_type=SelectionType.CLIP,item_id=vclip.clip_id,track_id=vclip.track_id,timeline_id=tl.timeline_id)
tv.set_selection(TimelineSelection(items=(item,),primary_item=item))
tv.set_playhead(200); QTest.qWait(200)
before=counts(tl)
tv._copy_selected_clips(); QTest.qWait(200)
tv._paste_clips_at_playhead(); QTest.qWait(500)
tl=tv._timeline
record("copy_paste", before=before, after=counts(tl))

# 4) Marker add.
tv.set_playhead(300); QTest.qWait(100)
try:
    tv.add_marker_at_playhead(); QTest.qWait(300)
    tl=tv._timeline; record("marker", ok=True, markers=len(getattr(tl,"markers",())))
except BaseException as e:
    record("marker", ok=False, error=f"{type(e).__name__}: {e}")

# 5) Transition at a real cut (after split/redo exists).
tl=tv._timeline
vtrack=tl.video_tracks[0]
clips=sorted(vtrack.clips,key=lambda c:c.timeline_start)
if len(clips)>=2:
    left,right=clips[0],clips[1]
    try:
        tv.add_transition_at_cut_point(vtrack.track_id,left.clip_id,right.clip_id,duration=10,transition_type="cross_dissolve")
        QTest.qWait(400); tl=tv._timeline
        trs=[tr for t in tl.all_tracks for tr in (getattr(t,"transitions",()) or ())]
        record("transition_add", ok=True, count=len(trs), types=[getattr(tr,"transition_type",None) for tr in trs])
        tv.undo(); QTest.qWait(300); tl=tv._timeline
        record("transition_undo", count=sum(len(getattr(t,"transitions",()) or ()) for t in tl.all_tracks))
    except BaseException as e:
        record("transition_add", ok=False, error=f"{type(e).__name__}: {e}")
else:
    record("transition_add", ok=False, error="no adjacent clips")

# 6) ClipItem double-click receiver audit.
receivers=[]
for it in tv.scene().items():
    if isinstance(it,ClipItem):
        try: receivers.append(it.receivers(it.clip_double_clicked))
        except Exception as e: receivers.append(f"ERR:{e}")
record("clip_item_double_click_receivers", receivers=receivers)

# 7) Properties panel edit of transform X (single selection).
try:
    from src.features.edit.core.commands import UpdateClipParamCommand
    from dataclasses import replace
    tl=tv._timeline; vclip=next(c for t in tl.video_tracks for c in t.clips)
    before=float(vclip.transform.translate_x)
    new_t=replace(vclip.transform,translate_x=55.0)
    cmd=UpdateClipParamCommand(command_id="audit-x",label="Set X",track_id=vclip.track_id,clip_id=vclip.clip_id,param_type="transform",new_value=new_t)
    tv.execute_command(cmd); QTest.qWait(300); tl=tv._timeline
    after=float(tl.get_clip_by_id(vclip.clip_id).transform.translate_x)
    tv.undo(); QTest.qWait(300); tl=tv._timeline
    undo=float(tl.get_clip_by_id(vclip.clip_id).transform.translate_x)
    record("property_transform", before=before, after=after, undo=undo)
except BaseException as e:
    record("property_transform", ok=False, error=f"{type(e).__name__}: {e}")

(OUT/"records.json").write_text(json.dumps(records,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
sys.stdout.flush(); sys.stderr.flush(); os._exit(0)
