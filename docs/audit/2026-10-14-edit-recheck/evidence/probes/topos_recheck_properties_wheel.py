from pathlib import Path
import os,sys,json
os.environ['QT_QPA_PLATFORM']='offscreen';sys.path.insert(0,'/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01')
from dataclasses import replace
from types import SimpleNamespace,MethodType
from PyQt6.QtCore import QPoint,QPointF,Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest
from src.features.edit.ui.timeline_view import TimelineView
from src.features.edit.ui.timeline_items import EditTool
from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel
from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.core.timeline_v2 import ClipInstance,EditTrack,TimelineSequence
from src.features.edit.core.clip_params import TransformParams
from src.features.edit.core.selection import TimelineSelection
app=QApplication([]);out={}
def model():
 a=ClipInstance(clip_id='c',asset_id='missing',track_id='v',timeline_start=100,timeline_duration=100,source_start=100,source_duration=100,transform=TransformParams(translate_x=10),opacity=.8)
 b=replace(a,clip_id='d',timeline_start=300,transform=TransformParams(translate_x=20),opacity=.3)
 return TimelineSequence(timeline_id='p',name='p',fps=24,video_tracks=(EditTrack(track_id='v',name='V1',track_type='video',index=0,clips=(a,b)),))
for kind in ['transform','opacity','transform_reset','opacity_reset']:
 seq=model();v=TimelineView();v.load_timeline(seq);p=EditPropertiesPanel();p.resize(500,900);p.set_clips(list(seq.video_tracks[0].clips),media_type='video');p.show();app.processEvents()
 page=SimpleNamespace(_current_timeline=seq);host=SimpleNamespace(edit_page=page,timeline_panel=SimpleNamespace(get_timeline_view=lambda:v),edit_properties_panel=p,timeline_preview_renderer=None)
 host._execute_timeline_edit_command=MethodType(EditControllerMixin._execute_timeline_edit_command,host);host._request_program_viewer_update=lambda:None;host._show_clip_in_properties=lambda clip:None
 v.timeline_changed.connect(lambda t:setattr(page,'_current_timeline',t))
 rec=[];p.clip_param_changed.connect(lambda *a:rec.append((a[0],a[1],a[2])));p.clip_param_changed.connect(lambda *a:EditControllerMixin._on_edit_clip_param_changed(host,*a));p.reset_transform_requested.connect(lambda ids:EditControllerMixin._on_edit_reset_transform(host,ids))
 section=p._sections['clip:c:'+kind.split('_')[0]]
 initial=dict(title=p._title_label.text(),context=[c.clip_id for c in p._current_clips],section_parameters=section.parameters)
 if kind.endswith('_reset'):QTest.mouseClick(section._reset_btn,Qt.MouseButton.LeftButton)
 else:
  w=section._param_widgets['translate_x' if kind=='transform' else 'value']['widget'];QTest.mouseDClick(w,Qt.MouseButton.LeftButton);app.processEvents();QTest.keyClick(w._edit,Qt.Key.Key_A,Qt.KeyboardModifier.ControlModifier);QTest.keyClicks(w._edit,'99' if kind=='transform' else '50');QTest.keyClick(w._edit,Qt.Key.Key_Return)
 app.processEvents();after={c.clip_id:dict(x=c.transform.translate_x,opacity=c.opacity) for c in v._timeline.video_tracks[0].clips}; undo_before=v.can_undo();v.undo();after_undo={c.clip_id:dict(x=c.transform.translate_x,opacity=c.opacity) for c in v._timeline.video_tracks[0].clips}
 out[kind]=dict(initial=initial,emissions=rec,after=after,undo_before=undo_before,after_one_undo=after_undo,undo_remaining=v.can_undo());p.close();v.close();app.processEvents()
# low/high zoom pointer anchor. Long sequence keeps scrollbar away from boundaries.
seq=model();a=replace(seq.video_tracks[0].clips[0],timeline_duration=100000,source_duration=100000);seq=replace(seq,video_tracks=(replace(seq.video_tracks[0],clips=(a,)),))
for ppf in [.1,.2,.5,1,3,10]:
 v=TimelineView();v.resize(1200,550);v.show();v.load_timeline(seq);v.set_zoom(ppf);app.processEvents();v.horizontalScrollBar().setValue(800);app.processEvents();pos=QPoint(813,150);errs=[]
 for rep in range(5):
  old=(v.mapToScene(pos).x()-220)/v._pixels_per_frame
  ev=QWheelEvent(QPointF(pos),QPointF(pos),QPoint(0,40),QPoint(),Qt.MouseButton.NoButton,Qt.KeyboardModifier.ControlModifier,Qt.ScrollPhase.ScrollUpdate,False);QApplication.sendEvent(v.viewport(),ev)
  new=(v.mapToScene(pos).x()-220)/v._pixels_per_frame;errs.append(dict(before=old,after=new,error=new-old,ppf=v._pixels_per_frame,scroll=v.horizontalScrollBar().value()))
 out['anchor_'+str(ppf)]=errs;v.close();app.processEvents()
Path('/tmp/topos_recheck_properties_wheel.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps(out,ensure_ascii=False,indent=2))
