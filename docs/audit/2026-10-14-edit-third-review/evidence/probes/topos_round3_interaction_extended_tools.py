from pathlib import Path
import os,sys,json,traceback
os.environ['QT_QPA_PLATFORM']='offscreen';sys.path.insert(0,'/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01')
from dataclasses import replace
from PyQt6.QtCore import QPoint,QPointF,Qt
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest
from src.features.edit.ui.timeline_view import TimelineView,TRACK_HEADER_WIDTH
from src.features.edit.ui.timeline_items import EditTool,ClipItem
from src.features.edit.core.timeline_v2 import ClipInstance,EditTrack,TimelineSequence
from src.features.edit.core.clip_params import SpeedParams
app=QApplication([]);out={};errs=[]
sys.excepthook=lambda *a:errs.append(dict(message=str(a[1]),traceback=''.join(traceback.format_exception(*a))))
def state(v):
 return dict(model={c.clip_id:dict(start=c.timeline_start,duration=c.timeline_duration,source_start=c.source_start,source_duration=c.source_duration,speed=c.effective_speed()) for t in v._timeline.all_tracks for c in t.clips},items={cid:dict(start=i._start_frame,duration=i._duration_frames,source_start=i._source_start,source_duration=i._source_duration,trim_preview=i._trim_preview_active) for cid,i in v._items_map.items() if isinstance(i,ClipItem)},undo=len(v._command_stack._undo_stack),redo=len(v._command_stack._redo_stack),revision=v._timeline.revision,interaction=v._interaction.state.name,affected=list(v._preview_affected_item_ids))
def setup(seq):
 v=TimelineView();v.resize(1400,650);v.show();v.load_timeline(seq);v.set_zoom(3);v.set_snap_enabled(False);app.processEvents()
 for i in v._items_map.values():
  if isinstance(i,ClipItem):i._max_source_frames=1000
 return v
for edge in ['left','right']:
 for end in ['release','esc_tool_late_release']:
  errs.clear();c=ClipInstance(clip_id='c',asset_id='missing',track_id='v',timeline_start=100,timeline_duration=100,source_start=100,source_duration=200,speed=SpeedParams(speed=2),linked_clip_id='a');a=replace(c,clip_id='a',track_id='a1',linked_clip_id='c',source_duration=100,speed=SpeedParams(speed=1));seq=TimelineSequence(timeline_id='p',name='p',fps=24,video_tracks=(EditTrack(track_id='v',name='V1',track_type='video',index=0,clips=(c,)),),audio_tracks=(EditTrack(track_id='a1',name='A1',track_type='audio',index=0,is_locked=True,clips=(a,)),));v=setup(seq);i=v._items_map['c'];p=v.mapFromScene(i.mapToScene(QPointF(2 if edge=='left' else i._width-2,30)));q=p+QPoint(30 if edge=='left' else -30,0);before=state(v);QTest.mousePress(v.viewport(),Qt.MouseButton.LeftButton,pos=p);QTest.mouseMove(v.viewport(),q,10);app.processEvents();during=state(v)
  if end!='release':QTest.keyClick(v,Qt.Key.Key_Escape);QTest.keyClick(v,Qt.Key.Key_R)
  QTest.mouseRelease(v.viewport(),Qt.MouseButton.LeftButton,pos=q);app.processEvents();out['linked_locked_trim_'+edge+'_'+end]=dict(before=before,during=during,after=state(v),errors=list(errs));v.close()
for speed in [.5,2]:
 errs.clear();l=ClipInstance(clip_id='l',asset_id='missing',track_id='v',timeline_start=0,timeline_duration=100,source_start=100,source_duration=int(100*speed),speed=SpeedParams(speed=speed));r=replace(l,clip_id='r',timeline_start=100,source_start=400);seq=TimelineSequence(timeline_id='p',name='p',fps=24,video_tracks=(EditTrack(track_id='v',name='V1',track_type='video',index=0,clips=(l,r)),));v=setup(seq);v.set_tool(EditTool.ROLL);h=next(iter(v._roll_handles.values()));p=v.mapFromScene(h.mapToScene(h.boundingRect().center()));q=p+QPoint(30,0);QTest.mousePress(v.viewport(),Qt.MouseButton.LeftButton,pos=p);QTest.mouseMove(v.viewport(),q,10);app.processEvents();during=state(v);expected={'l':list(l.with_right_edge_source_values(110)),'r':list(r.with_left_edge_source_values(110))};QTest.mouseRelease(v.viewport(),Qt.MouseButton.LeftButton,pos=q);app.processEvents();out['roll_speed_'+str(speed)]=dict(during=during,expected_source=expected,after=state(v),errors=list(errs));v.close()
# repeated wheel at low zoom; true Qt wheel events, away from scrollbar endpoints.
from PyQt6.QtGui import QWheelEvent
for ppf in [.1,.2,.5,1,3,10]:
 l=replace(l,timeline_duration=100000,source_duration=100000,speed=SpeedParams());seq=replace(seq,video_tracks=(replace(seq.video_tracks[0],clips=(l,)),));v=setup(seq);v.set_zoom(ppf);v.horizontalScrollBar().setValue(800);app.processEvents();pos=QPoint(813,150)
 def af():return (v.mapToScene(pos).x()-TRACK_HEADER_WIDTH)/v._pixels_per_frame
 ref=af();samples=[]
 for dy in [40]*20+[-40]*20:
  event=QWheelEvent(QPointF(pos),QPointF(pos),QPoint(0,dy),QPoint(),Qt.MouseButton.NoButton,Qt.KeyboardModifier.ControlModifier,Qt.ScrollPhase.ScrollUpdate,False);QApplication.sendEvent(v.viewport(),event);a=af();samples.append(dict(ppf=v._pixels_per_frame,drift_frames=a-ref,drift_pixels=(a-ref)*v._pixels_per_frame,scroll=v.horizontalScrollBar().value()))
 out['wheel_20plus20_'+str(ppf)]=dict(samples=samples,worst_px=max(abs(s['drift_pixels']) for s in samples),final=samples[-1]);v.close()
Path('/tmp/topos_round3_interaction_extended_tools.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps({k:v if 'samples' not in v else dict(worst_px=v['worst_px'],final=v['final']) for k,v in out.items()},ensure_ascii=False,indent=2))
