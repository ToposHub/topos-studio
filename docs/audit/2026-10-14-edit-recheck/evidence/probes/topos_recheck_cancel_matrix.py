from pathlib import Path
import os,sys,json,traceback
os.environ['QT_QPA_PLATFORM']='offscreen';sys.path.insert(0, '/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01')
from dataclasses import replace
from contextlib import nullcontext
from unittest.mock import patch
from PyQt6.QtCore import QPoint,QPointF,Qt,QEvent
from PyQt6.QtGui import QFocusEvent
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest
from src.features.edit.ui.timeline_view import TimelineView
from src.features.edit.ui.timeline_items import EditTool,ClipItem
from src.features.edit.core.timeline_v2 import ClipInstance,EditTrack,TimelineSequence
from src.features.edit.core.selection import TimelineSelection
from src.shared.media.metadata import Rational
app=QApplication([]);out={}; errors=[]
sys.excepthook=lambda *args: errors.append(''.join(traceback.format_exception(*args)))
def new(kind):
 cs=[ClipInstance(clip_id='c',asset_id='missing',track_id='v',timeline_start=100,timeline_duration=100,source_start=100,source_duration=100,fade_in_duration=10,fade_out_duration=10)]
 ac=[]
 if kind.startswith('linked'):
  cs[0]=replace(cs[0],linked_clip_id='a');ac=[replace(cs[0],clip_id='a',track_id='a1',linked_clip_id='c')]
 if kind in ['slide','roll_seam']:
  cs=[replace(cs[0],timeline_start=0,clip_id='l'),cs[0],replace(cs[0],timeline_start=200,clip_id='r')]
 if kind in ['multi_move','alt_multi']:
  cs.append(replace(cs[0],clip_id='d',timeline_start=300))
 seq=TimelineSequence(timeline_id='p',name='p',fps=Rational(24,1),video_tracks=(EditTrack(track_id='v',name='V1',track_type='video',index=0,clips=tuple(cs)),),audio_tracks=((EditTrack(track_id='a1',name='A1',track_type='audio',index=0,clips=tuple(ac)),) if ac else ()))
 v=TimelineView();v.resize(1500,650);v.show();v.load_timeline(seq);v.set_zoom(3);v.set_snap_enabled(False);app.processEvents();v.setFocus()
 for item in v._items_map.values():
  if isinstance(item,ClipItem):item._max_source_frames=1000
 if kind in ['multi_move','alt_multi']:
  v.set_selection(TimelineSelection().with_clip_selected('c','v','p').with_clip_selected('d','v','p',replace=False))
 return v
def snap(v):
 return {cid:dict(start=i._start_frame,duration=i._duration_frames,source_start=i._source_start,source_duration=i._source_duration,fade_in=i._fade_in_duration,fade_out=i._fade_out_duration,preview_active=i._trim_preview_active,preview_start=i._trim_preview_source_start,preview_duration=i._trim_preview_source_duration,x=round(i.x(),3),y=round(i.y(),3)) for cid,i in v._items_map.items() if isinstance(i,ClipItem)}
def details(v):
 return dict(state=v._interaction.state.name,undo=v.can_undo(),roll_preview=v._roll_preview_state is not None,drag_session=v._current_drag_session is not None,ghosts=len(getattr(v,'_alt_copy_ghosts',{})),items=snap(v))
for kind in ['move','alt_move','multi_move','alt_multi','linked_move','trim_left','trim_right','linked_trim_left','linked_trim_right','ripple_left','ripple_right','slip','slide','fade_in','fade_out','rate_left','rate_right','roll_seam']:
 for cancel in ['esc','tool','focus']:
  v=new(kind);item=v._items_map['c'];before=snap(v);payload=v._timeline.to_dict();tool=EditTool.SELECT;x=90;y=30;dx=30
  if 'left' in kind:x=2
  if 'right' in kind:x=item._width-2;dx=-30
  if kind.startswith('ripple'):tool=EditTool.RIPPLE
  if kind.startswith('rate'):tool=EditTool.RATE_STRETCH
  if kind=='slip':tool=EditTool.SLIP
  if kind=='slide':tool=EditTool.SLIDE
  if kind=='fade_in':x=30;y=5
  if kind=='fade_out':x=item._width-30;y=5;dx=-30
  if kind=='roll_seam':
   tool=EditTool.ROLL;item=next(h for h in v._roll_handles.values() if h.left_clip_id=='l' and h.right_clip_id=='c');x=0;y=item.boundingRect().center().y()
  v.set_tool(tool);app.processEvents();p=v.mapFromScene(item.mapToScene(QPointF(x,y)));q=p+QPoint(dx,0);mods=Qt.KeyboardModifier.AltModifier if kind.startswith('alt') else Qt.KeyboardModifier.NoModifier
  QTest.mousePress(v.viewport(),Qt.MouseButton.LeftButton,mods,p)
  # QTest.mouseMove has no modifiers argument and resets global modifiers on offscreen.
  # Supply the held-Alt keyboard snapshot only during its dispatch, to exercise real copy ghosts.
  with patch.object(QApplication,'keyboardModifiers',return_value=Qt.KeyboardModifier.AltModifier) if kind.startswith('alt') else nullcontext():
   QTest.mouseMove(v.viewport(),q,10)
  app.processEvents();during=details(v)
  if cancel=='esc':QTest.keyClick(v,Qt.Key.Key_Escape)
  if cancel=='tool':QTest.keyClick(v,Qt.Key.Key_A)
  if cancel=='focus':QApplication.sendEvent(v,QFocusEvent(QEvent.Type.FocusOut,Qt.FocusReason.ActiveWindowFocusReason))
  app.processEvents();after_cancel=details(v)
  QTest.mouseRelease(v.viewport(),Qt.MouseButton.LeftButton,mods,q);app.processEvents();after_release=details(v)
  # compare visible actual geometry/source/fade + active flag; inactive preview ranges are historical cache metadata
  visible_keys=['start','duration','source_start','source_duration','fade_in','fade_out','preview_active','x','y']
  mismatch={cid:{k:[before[cid][k],i[k]] for k in visible_keys if before[cid][k]!=i[k]} for cid,i in snap(v).items() if cid in before}
  mismatch={k:d for k,d in mismatch.items() if d}
  out[kind+'_'+cancel]=dict(model_unchanged=v._timeline.to_dict()==payload,visual_mismatch=mismatch,during=during,after_cancel=after_cancel,after_release=after_release)
  v.close();app.processEvents()
out['uncaught']=errors
Path('/tmp/topos_recheck_cancel_matrix.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
print(json.dumps({k:dict(model_unchanged=d['model_unchanged'],visual_mismatch=d['visual_mismatch'],state_during=d['during']['state'],state_after=d['after_release']['state'],undo=d['after_release']['undo'],roll_preview=d['after_release']['roll_preview']) for k,d in out.items() if k!='uncaught'},indent=2));print('UNCAUGHT',len(errors))
