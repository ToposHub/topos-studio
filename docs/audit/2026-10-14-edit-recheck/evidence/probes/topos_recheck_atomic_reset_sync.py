from pathlib import Path
import os,sys,json
os.environ['QT_QPA_PLATFORM']='offscreen';sys.path.insert(0,'/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01')
from types import SimpleNamespace,MethodType
from dataclasses import replace
from unittest.mock import patch
from PyQt6.QtCore import Qt,QPointF
from PyQt6.QtWidgets import QApplication,QMenu
from PyQt6.QtTest import QTest
from src.features.edit.ui.timeline_view import TimelineView
from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel
from src.features.edit.core.timeline_v2 import ClipInstance,EditTrack,TimelineSequence
from src.features.edit.core.clip_params import TransformParams
from src.app.controllers.edit_controller import EditControllerMixin
app=QApplication([]);out={};c=ClipInstance(clip_id='c',asset_id='missing',track_id='v',timeline_start=100,timeline_duration=100,source_start=100,source_duration=100,transform=TransformParams(translate_x=10));d=replace(c,clip_id='d',track_id='v2',transform=TransformParams(translate_x=20));seq=TimelineSequence(timeline_id='p',name='p',fps=24,video_tracks=(EditTrack(track_id='v',name='V1',track_type='video',index=0,clips=(c,)),EditTrack(track_id='v2',name='V2',track_type='video',index=1,is_locked=True,clips=(d,))))
v=TimelineView();v.load_timeline(seq);p=EditPropertiesPanel();p.resize(500,900);p.set_clips([c,d],media_type='video');p.show();app.processEvents();page=SimpleNamespace(_current_timeline=seq);host=SimpleNamespace(edit_page=page,timeline_panel=SimpleNamespace(get_timeline_view=lambda:v),edit_properties_panel=p,timeline_preview_renderer=None);host._execute_timeline_edit_command=MethodType(EditControllerMixin._execute_timeline_edit_command,host);host._request_program_viewer_update=lambda:None;host._show_clip_in_properties=lambda clip:None;v.timeline_changed.connect(lambda t:setattr(page,'_current_timeline',t));p.reset_transform_requested.connect(lambda ids:EditControllerMixin._on_edit_reset_transform(host,ids));QTest.mouseClick(p._sections['clip:c:transform']._reset_btn,Qt.MouseButton.LeftButton);app.processEvents();out['reset_one_locked']=dict(x={c.clip_id:c.transform.translate_x for t in v._timeline.video_tracks for c in t.clips},undo=v.can_undo(),revision=v._timeline.revision);p.close();v.close()
v=TimelineView();v.resize(1200,550);v.show();v.load_timeline(seq);app.processEvents();header=v._track_headers['v'];events=[];header.sync_lock_changed.connect(lambda *a:events.append(a));p=v.mapFromScene(header.mapToScene(QPointF(70,30)))
def choose(menu,*args):
 a=next(a for a in menu.actions() if a.text() in ['Sync Lock','同步锁']);a.setChecked(not a.isChecked());return a
with patch.object(QMenu,'exec',choose):QTest.mouseClick(v.viewport(),Qt.MouseButton.RightButton,Qt.KeyboardModifier.NoModifier,p)
app.processEvents();out['sync_lock_integrated_after']=dict(model=v._timeline.get_track_by_id('v').is_sync_locked,header=header._is_sync_locked,events=events,undo=v.can_undo());v.undo();app.processEvents();out['sync_lock_integrated_undo']=dict(model=v._timeline.get_track_by_id('v').is_sync_locked,header=v._track_headers['v']._is_sync_locked);v.close();Path('/tmp/topos_recheck_atomic_reset_sync.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps(out,ensure_ascii=False,indent=2))
