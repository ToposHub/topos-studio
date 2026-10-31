from pathlib import Path
import os,sys,json
os.environ['QT_QPA_PLATFORM']='offscreen';sys.path.insert(0,'/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01')
from dataclasses import replace
from unittest.mock import patch
from PyQt6.QtCore import QPoint,QPointF,Qt
from PyQt6.QtWidgets import QApplication,QMenu
from PyQt6.QtTest import QTest
from src.features.edit.ui.timeline_view import TimelineView
from src.features.edit.core.timeline_v2 import ClipInstance,EditTrack,TimelineSequence
from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.selection import TimelineSelection
app=QApplication([]);out={}
for edge in ['left','right']:
 for snapping in [False,True]:
  c=ClipInstance(clip_id='c',asset_id='missing',track_id='v',timeline_start=100,timeline_duration=100,source_start=100,source_duration=200,speed=SpeedParams(speed=2.0));seq=TimelineSequence(timeline_id='p',name='p',fps=24,video_tracks=(EditTrack(track_id='v',name='V1',track_type='video',index=0,clips=(c,)),));v=TimelineView();v.resize(1200,550);v.show();v.load_timeline(seq);v.set_zoom(3);v.set_snap_enabled(snapping);app.processEvents();item=v._items_map['c'];item._max_source_frames=1000;p=v.mapFromScene(item.mapToScene(QPointF(2 if edge=='left' else item._width-2,30)));q=p+QPoint(30 if edge=='left' else -30,0)
  QTest.mousePress(v.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,p);QTest.mouseMove(v.viewport(),q,10);app.processEvents();preview=dict(start=item._start_frame,duration=item._duration_frames,source_start=item._source_start,source_duration=item._source_duration);expected=c.with_left_edge_source_values(item._start_frame) if edge=='left' else c.with_right_edge_source_values(item._duration_frames)
  QTest.mouseRelease(v.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,q);app.processEvents();commit=v._timeline.get_clip_by_id('c');out[edge+'_snap'+str(snapping)]=dict(preview=preview,expected_preview_source=list(expected),commit=dict(start=commit.timeline_start,duration=commit.timeline_duration,source_start=commit.source_start,source_duration=commit.source_duration));v.close()
# menu through real QTest right click with exec stub (records constructed action).
for disabled,locked in [(True,False),(False,True)]:
 c=ClipInstance(clip_id='c',asset_id='missing',track_id='v',timeline_start=100,timeline_duration=100,source_start=100,source_duration=100,is_enabled=not disabled);seq=TimelineSequence(timeline_id='p',name='p',fps=24,video_tracks=(EditTrack(track_id='v',name='V1',track_type='video',index=0,is_locked=locked,clips=(c,)),));v=TimelineView();v.resize(1200,550);v.show();v.load_timeline(seq);v.set_zoom(3);app.processEvents();item=v._items_map['c'];labels=[]
 def select(menu,*_):
  a=next((a for a in menu.actions() if 'isable Clip' in a.text() or 'nable Clip' in a.text()),None);labels.append(dict(text=a.text(),enabled=a.isEnabled(),item_state=item._is_enabled));return a
 p=v.mapFromScene(item.mapToScene(QPointF(90,30)))
 with patch.object(QMenu,'exec',select):QTest.mouseClick(v.viewport(),Qt.MouseButton.RightButton,Qt.KeyboardModifier.NoModifier,p)
 app.processEvents();out['disabled_menu' if disabled else 'locked_menu']=dict(before_enabled=c.is_enabled,menu=labels,after_enabled=v._timeline.get_clip_by_id('c').is_enabled,undo=v.can_undo())
 if locked:
  v.set_selection(TimelineSelection().with_clip_selected('c','v','p'));QTest.keyClick(v,Qt.Key.Key_D);app.processEvents();out['locked_keyboard_disable']=dict(enabled=v._timeline.get_clip_by_id('c').is_enabled,undo=v.can_undo())
 v.close()
Path('/tmp/topos_round3_interaction_trim_preview_menu.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps(out,ensure_ascii=False,indent=2))
