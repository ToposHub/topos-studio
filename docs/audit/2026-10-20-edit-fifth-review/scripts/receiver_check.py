from __future__ import annotations
import json, os, sys
from pathlib import Path
ROOT=Path("/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01")
S=Path("/tmp/topos-edit-audit-1020/session6"); S.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(ROOT)); os.environ["TOPOS_CONFIG_PATH"]=str(S/"config.json"); os.environ["TOPOS_PRODUCT_MODE"]="studio"
os.environ["TOPOS_TEST_MODE"]="1"; os.environ["TOPOS_FEATURE_EDIT"]="1"; os.environ["QSG_RHI_BACKEND"]="opengl"
(S/"config.json").write_text(json.dumps({"locale":"zh_CN","current_page":"Edit","work_mode":"video","auto_load_last_project":False,"check_updates_on_startup":False,"show_welcome_on_startup":False,"features":{"color":True,"edit":True,"composite":False,"deliver":True,"teaching_animation":False,"ai_assistant":False}}))
import src.main
from PyQt6.QtWidgets import QApplication, QMenu
from PyQt6.QtGui import QSurfaceFormat
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
fmt=QSurfaceFormat(); fmt.setVersion(4,5); fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile); fmt.setSamples(4); QSurfaceFormat.setDefaultFormat(fmt)
QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts,True); app=QApplication([sys.argv[0]])
from src.shared.config.config_manager import ConfigManager
from src.shared.feature_flags import load_feature_flags
from src.app.feature_registry import FeatureRegistry
from src.app.app_context import AppContext
from src.shared.i18n.i18n import I18N
from src.features.edit.module import EditModule
from src.features.color.module import ColorModule
from src.features.deliver.module import DeliverModule
from src.app.main_window import MainWindow
cfg=ConfigManager(); flags=load_feature_flags(cfg); I18N.load_locale("zh_CN")
reg=FeatureRegistry(); reg.initialize(AppContext(config_manager=cfg,feature_flags=flags,i18n=I18N,logger=None))
for m in (ColorModule(),EditModule(),DeliverModule()): reg.register_module(m)
mw=MainWindow(feature_flags=flags,feature_registry=reg,test_mode=False,startup_no_dialog=True)
mw.resize(1440,1000); mw.show(); QTest.qWait(1200); mw.switch_to_page("Edit"); QTest.qWait(600)
p=mw.edit_properties_panel
print("RECV clip_keyframe", p.receivers(p.keyframe_toggled), "effect_keyframe", p.receivers(p.effect_keyframe_toggled))
def walk(menu,path=""):
    out=[]
    for a in menu.actions():
        t=a.text().replace("&","")
        if a.menu(): out+=walk(a.menu(),path+"/"+t)
        else: out.append(path+"/"+t)
    return out
for act in mw.menuBar().actions():
    if act.menu():
        acts=walk(act.menu(),act.text().replace("&",""))
        if act.text().replace("&","").startswith("交付") or act.text().replace("&","").startswith("Deliver"):
            print("DELIVER_MENU", json.dumps(acts,ensure_ascii=False))
sys.stdout.flush(); os._exit(0)
