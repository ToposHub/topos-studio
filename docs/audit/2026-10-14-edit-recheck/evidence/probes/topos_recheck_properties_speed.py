from pathlib import Path
import os,sys,json
os.environ['QT_QPA_PLATFORM']='offscreen';sys.path.insert(0,'/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01')
from types import SimpleNamespace,MethodType
from dataclasses import replace
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest
from src.features.edit.ui.timeline_view import TimelineView
from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel
from src.features.edit.core.timeline_v2 import ClipInstance,EditTrack,TimelineSequence
from src.features.edit.core.clip_params import TransformParams
from src.app.controllers.edit_controller import EditControllerMixin
app=QApplication([]);out={}
for kind in ['locked_transform','linked_mixed_fps_speed']:
 c=ClipInstance(clip_id='c',asset_id='missing',track_id='v',timeline_start=100,timeline_duration=96,source_start=100,source_duration=120,_source_fps_num=30,_source_fps_den=1,_timeline_fps_num=24,_timeline_fps_den=1,transform=TransformParams(translate_x=10));a=replace(c,clip_id='a',track_id='a1',linked_clip_id='c');c=replace(c,linked_clip_id='a');seq=TimelineSequence(timeline_id='p',name='p',fps=24,video_tracks=(EditTrack(track_id='v',name='V1',track_type='video',index=0,is_locked=kind=='locked_transform',clips=(c,)),),audio_tracks=(EditTrack(track_id='a1',name='A1',track_type='audio',index=0,clips=(a,)),));v=TimelineView();v.load_timeline(seq);p=EditPropertiesPanel();p.resize(500,900);p.set_clip(c,media_type='video');p.show();app.processEvents();page=SimpleNamespace(_current_timeline=seq);host=SimpleNamespace(edit_page=page,timeline_panel=SimpleNamespace(get_timeline_view=lambda:v),edit_properties_panel=p,timeline_preview_renderer=None);host._execute_timeline_edit_command=MethodType(EditControllerMixin._execute_timeline_edit_command,host);host._request_program_viewer_update=lambda:None;v.timeline_changed.connect(lambda t:setattr(page,'_current_timeline',t));p.clip_param_changed.connect(lambda *a:EditControllerMixin._on_edit_clip_param_changed(host,*a));group='transform' if kind=='locked_transform' else 'speed';section=p._sections['clip:c:'+group]
 if section._is_collapsed:QTest.mouseClick(section._collapse_btn,Qt.MouseButton.LeftButton)
 widget=section._param_widgets['translate_x' if group=='transform' else 'speed']['widget'];QTest.mouseDClick(widget,Qt.MouseButton.LeftButton);app.processEvents();QTest.keyClick(widget._edit,Qt.Key.Key_A,Qt.KeyboardModifier.ControlModifier);QTest.keyClicks(widget._edit,'99' if group=='transform' else '2');QTest.keyClick(widget._edit,Qt.Key.Key_Return);app.processEvents();out[kind]=dict(model={c.clip_id:dict(duration=c.timeline_duration,source_duration=c.source_duration,x=c.transform.translate_x,speed=c.effective_speed(),linked_id=c.linked_clip_id) for t in v._timeline.all_tracks for c in t.clips},locked=v._timeline.get_track_by_id('v').is_locked,undo=v.can_undo(),revision=v._timeline.revision,expected_2x_mixed_fps_duration=48 if group=='speed' else None);p.close();v.close();app.processEvents()
Path('/tmp/topos_recheck_properties_speed.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps(out,ensure_ascii=False,indent=2))
