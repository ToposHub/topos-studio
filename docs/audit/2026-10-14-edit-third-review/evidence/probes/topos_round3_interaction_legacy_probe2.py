from pathlib import Path
import os,sys,json
os.environ['QT_QPA_PLATFORM']='offscreen'; sys.path.insert(0, '/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01')
from dataclasses import replace
from unittest.mock import patch
from PyQt6.QtCore import QPointF,Qt,QEvent
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication,QMenu
from src.features.edit.ui.timeline_view import TimelineView
from src.features.edit.ui.timeline_items import EditTool
from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel
from src.features.edit.ui.timeline_shortcuts import register_timeline_shortcuts,matches_key_event
from src.shared.ui.shortcut_manager import ShortcutManager
from src.features.edit.core.timeline_v2 import ClipInstance,EditTrack,TimelineSequence
from src.features.edit.core.selection import SelectionItem,SelectionType,TimelineSelection
from src.shared.media.metadata import Rational
app=QApplication([]); result={}
def model(locked=True,enabled=True):
 c=ClipInstance(clip_id='c',asset_id='missing',track_id='v',timeline_start=100,timeline_duration=100,source_start=100,source_duration=100,is_enabled=enabled)
 return TimelineSequence(timeline_id='probe',name='probe',fps=Rational(24,1),video_tracks=(EditTrack(track_id='v',name='V1',track_type='video',index=0,is_locked=locked,clips=(c,)),))
def walk(menu):
 for a in menu.actions():
  yield a
  if a.menu(): yield from walk(a.menu())
for label in ['Disable Clip','200%']:
 v=TimelineView(); v.load_timeline(model()); v.show(); app.processEvents(); item=v._items_map['c']; records=[]
 def exec_stub(menu,*args):
  a=next((a for a in walk(menu) if a.text()==label),None)
  records.append({'text':a.text() if a else None,'enabled':a.isEnabled() if a else None})
  return a
 with patch.object(QMenu,'exec',exec_stub): item._show_clip_context_menu(QPointF(10,10),QPointF(60,30))
 c=v._timeline.get_clip_by_id('c')
 result['locked_menu_'+label]={'selection':records,'enabled':c.is_enabled,'duration':c.timeline_duration,'speed':c.effective_speed(),'undo':v.can_undo()}
 v.close()
v=TimelineView();v.load_timeline(model(False,False));v.show();app.processEvents();item=v._items_map['c'];records=[]
def inspect(menu,*args):
 records.extend([a.text() for a in menu.actions() if 'nable Clip' in a.text() or 'isable Clip' in a.text()]);return None
with patch.object(QMenu,'exec',inspect):item._show_clip_context_menu(QPointF(10,10),QPointF(60,30))
result['disabled_menu_label']=records;v.close()
# manager defaults only; don't write user shortcuts
m=ShortcutManager();register_timeline_shortcuts(m);m._custom_shortcuts['edit.timeline.tool.blade']=''
with patch.object(ShortcutManager,'instance',classmethod(lambda cls:m)):
 e=QKeyEvent(QEvent.Type.KeyPress,Qt.Key.Key_B,Qt.KeyboardModifier.NoModifier)
 result['cleared_blade']={'registered_binding':m.get_shortcut('edit.timeline.tool.blade'),'matches_with_default':matches_key_event(e,'edit.timeline.tool.blade','B')}
 v=TimelineView(); v.load_timeline(model(False)); v.set_tool(EditTool.SELECT); v.keyPressEvent(e);result['cleared_blade']['tool']=v._active_tool.name;v.close()
p=EditPropertiesPanel();c1=model(False).get_clip_by_id('c');c2=replace(c1,clip_id='d');p.set_clips([c1,c2]);result['properties_multiselect']={'requested':['c','d'],'stored':[c.clip_id for c in p._current_clips]};p.close()
print(json.dumps(result,ensure_ascii=False,indent=2));open(str(Path(__file__).with_suffix(".json")),'w').write(json.dumps(result,ensure_ascii=False,indent=2))
