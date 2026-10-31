from __future__ import annotations
import json, os, sys, time, traceback
from pathlib import Path
ROOT=Path("/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01")
SESSION=Path("/tmp/topos-edit-audit-1020/session5"); OUT=Path("/tmp/topos-edit-audit-1020/out5")
SESSION.mkdir(parents=True,exist_ok=True); OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(ROOT))
os.environ["TOPOS_CONFIG_PATH"]=str(SESSION/"config.json"); os.environ["TOPOS_PRODUCT_MODE"]="studio"
os.environ["TOPOS_TEST_MODE"]="1"; os.environ["TOPOS_FEATURE_EDIT"]="1"; os.environ["QSG_RHI_BACKEND"]="opengl"
(SESSION/"config.json").write_text(json.dumps({"locale":"zh_CN","current_page":"Edit","work_mode":"video","auto_load_last_project":False,"check_updates_on_startup":False,"show_welcome_on_startup":False,"features":{"color":True,"edit":True,"composite":False,"deliver":True,"teaching_animation":False,"ai_assistant":False}}),encoding="utf-8")
import src.main  # noqa
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QSurfaceFormat
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest
fmt=QSurfaceFormat(); fmt.setVersion(4,5); fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile); fmt.setSamples(4)
QSurfaceFormat.setDefaultFormat(fmt); QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts,True)
app=QApplication([sys.argv[0]])
from src.shared.config.config_manager import ConfigManager
from src.shared.feature_flags import load_feature_flags
from src.app.feature_registry import FeatureRegistry
from src.app.app_context import AppContext
from src.shared.i18n.i18n import I18N
from src.features.edit.module import EditModule
from src.features.color.module import ColorModule
from src.features.deliver.module import DeliverModule
from src.app.main_window import MainWindow
recs=[]
def rec(n,**d):
    v={"name":n,**d}; recs.append(v); (OUT/"records.json").write_text(json.dumps(recs,ensure_ascii=False,indent=2,default=str),encoding="utf-8"); print("UNDO "+json.dumps(v,ensure_ascii=False,default=str),flush=True)
def wait(c,t=90):
    e=time.monotonic()+t
    while time.monotonic()<e:
        QTest.qWait(200)
        if c(): return True
    return False
def counts(tl): return {t.track_type:len(t.clips) for t in tl.all_tracks}
cfg=ConfigManager(); flags=load_feature_flags(cfg); I18N.load_locale("zh_CN")
reg=FeatureRegistry(); reg.initialize(AppContext(config_manager=cfg,feature_flags=flags,i18n=I18N,logger=None))
for m in (ColorModule(),EditModule(),DeliverModule()): reg.register_module(m)
mw=MainWindow(feature_flags=flags,feature_registry=reg,test_mode=False,startup_no_dialog=True)
mw.resize(1440,1000); mw.show(); QTest.qWait(1200); mw.switch_to_page("Edit"); QTest.qWait(800)
mw._auto_create_edit_project("Undo1020"); mp=mw.media_pool_panel
mp._import_files([str(ROOT/"tests/test_videos/2K_h264.mp4")]); wait(lambda: bool(getattr(mw._current_edit_project,"assets",None)))
asset=next(iter(mw._current_edit_project.assets.values()))
mw.timeline_panel.append_clip(asset_id=asset.asset_id,in_frame=0,out_frame=300); wait(lambda: mw.timeline_panel.timeline_view._timeline is not None,30); QTest.qWait(700)
tv=mw.timeline_panel.timeline_view; tl=tv._timeline
rec("initial",counts=counts(tl))
tv.set_playhead(100); QTest.qWait(200)
QTest.keyClick(tv,Qt.Key.Key_B,Qt.KeyboardModifier.ControlModifier); QTest.qWait(400)
tl=tv._timeline; rec("after_split",counts=counts(tl))
# Menu state
rec("menu_undo_enabled", on=mw.undo_action_menu.isEnabled(), redo=mw.redo_action_menu.isEnabled(),
    undo_text=mw.undo_action_menu.text(), redo_text=mw.redo_action_menu.text(),
    undo_shortcut=mw.undo_action_menu.shortcut().toString())
# Real macOS Cmd+Z
tv.setFocus(); QTest.qWait(100)
QTest.keyClick(tv,Qt.Key.Key_Z,Qt.KeyboardModifier.MetaModifier); QTest.qWait(500)
tl=tv._timeline; rec("cmd_z",counts=counts(tl), menu_undo_enabled=mw.undo_action_menu.isEnabled())
# If still split, try Control+Z (hardcoded handler)
if counts(tl).get("video")==2:
    QTest.keyClick(tv,Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier); QTest.qWait(500)
    tl=tv._timeline; rec("ctrl_z",counts=counts(tl))
# Redo with Cmd+Shift+Z after any successful undo
QTest.keyClick(tv,Qt.Key.Key_Z,Qt.KeyboardModifier.MetaModifier|Qt.KeyboardModifier.ShiftModifier); QTest.qWait(500)
tl=tv._timeline; rec("cmd_shift_z",counts=counts(tl))
OUT.joinpath("records.json").write_text(json.dumps(recs,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
sys.stdout.flush(); os._exit(0)
