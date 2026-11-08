from dataclasses import replace
import json
import pytest
from PyQt6.QtCore import QPoint,QPointF,Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
from src.features.edit.core.timeline_v2 import ClipInstance,EditTrack,TimelineSequence
from src.features.edit.ui.timeline_view import TimelineView
from src.features.edit.ui.timeline_items import EditTool,ClipItem

def make(qtbot, blocker=False):
 v=ClipInstance('v','src','v1',100,100,100,100,linked_clip_id='a')
 a=ClipInstance('a','src','a1',100,100,100,100,linked_clip_id='v')
 ac=(a,)
 if blocker: ac+=(ClipInstance('block','other','a1',200,100,0,100),)
 tl=TimelineSequence('audit','audit',video_tracks=(EditTrack('v1','v1','video',0,clips=(v,)),EditTrack('v2','v2','video',1)),audio_tracks=(EditTrack('a1','a1','audio',0,clips=ac),EditTrack('a2','a2','audio',1)))
 view=TimelineView();qtbot.addWidget(view);view.resize(1400,700);view.show();view.load_timeline(tl);view.set_zoom(3);view.set_snap_enabled(False);view.set_tool(EditTool.SELECT)
 view._on_track_height_changed('v2',140);view._on_track_height_changed('a2',110)
 QTest.qWait(5)
 return view

@pytest.mark.parametrize('case',['move','cross','copy','copy_overlap','copy_cross','shift_cross','copy_audio_blocked'])
def test_drag(qtbot,monkeypatch,case):
 view=make(qtbot,case=='copy_audio_blocked');item=view._items_map['v'];local=QPointF(100,35)
 start=view.mapFromScene(item.mapToScene(local));cross=case in ('cross','copy_cross','shift_cross');dx=360 if case in ('copy','copy_audio_blocked') else 150 if case=='copy_overlap' else 60
 end=start+QPoint(dx,0)
 if cross:end.setY(view.mapFromScene(QPointF(0,view._layout_engine.track_top('v2')+35)).y())
 mods=Qt.KeyboardModifier.AltModifier if 'copy' in case else Qt.KeyboardModifier.ShiftModifier if case=='shift_cross' else Qt.KeyboardModifier.NoModifier
 monkeypatch.setattr(QApplication,"keyboardModifiers",staticmethod(lambda:mods))
 QTest.mousePress(view.viewport(),Qt.MouseButton.LeftButton,mods,pos=start);QTest.mouseMove(view.viewport(),end,10)
 session=view._current_drag_session
 preview=None if session is None else {'targets':session.current_track_ids.copy(),'start':view._items_map['v']._start_frame,'height':view._items_map['v']._track_height,'target_height':view._layout_engine.track_rect('v2').height(),'ghosts':{k:(g._start_frame,g._track_height)for k,g in view._alt_copy_ghosts.items()}}
 QTest.mouseRelease(view.viewport(),Qt.MouseButton.LeftButton,mods,pos=end)
 tl=view.get_timeline();state=[(c.clip_id,c.track_id,c.timeline_start,c.linked_clip_id)for t in tl.all_tracks for c in t.clips]
 print('AUDIT',case,json.dumps({'preview':preview,'result':state,'undo':len(view._command_stack._undo_stack)}))

@pytest.mark.parametrize('zone',['trim_left','trim_right','ripple_left','ripple_right','fade_in','fade_out'])
@pytest.mark.parametrize('finish',['return','escape'])
def test_trim_fade_return_cancel(qtbot,zone,finish):
 from PyQt6.QtCore import QRectF
 view=make(qtbot);item=view._items_map['v'];before=view.get_timeline().to_dict()
 for entry in view._items_map.values():
  if isinstance(entry,ClipItem):entry._max_source_frames=1000
 if zone.startswith('ripple'):view.set_tool(EditTool.TRIM)
 if zone.startswith('fade'):
  local=QPointF(*item._fade_handle_position(QRectF(0,0,item._width,item._track_height),'in' if zone=='fade_in' else 'out'))
 else:local=QPointF(5 if zone.endswith('left') else item._width-5,35)
 start=view.mapFromScene(item.mapToScene(local));end=start+QPoint(-30 if zone=='fade_out' else 30,0)
 QTest.mousePress(view.viewport(),Qt.MouseButton.LeftButton,pos=start);QTest.mouseMove(view.viewport(),end,10)
 mode=item._drag_mode
 if finish=='return':QTest.mouseMove(view.viewport(),start,10);end=start
 else:QTest.keyClick(view,Qt.Key.Key_Escape)
 QTest.mouseRelease(view.viewport(),Qt.MouseButton.LeftButton,pos=end)
 print('AUDIT_CANCEL',zone,finish,mode,'same_model',view.get_timeline().to_dict()==before,'undo',len(view._command_stack._undo_stack),'idle',view._interaction.is_idle)
 assert view.get_timeline().to_dict()==before
 assert not view._command_stack._undo_stack
