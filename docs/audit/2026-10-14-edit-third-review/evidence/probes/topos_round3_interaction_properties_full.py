from pathlib import Path
import os,sys,json,traceback
os.environ['QT_QPA_PLATFORM']='offscreen';sys.path.insert(0,'/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01')
from dataclasses import replace
from types import SimpleNamespace
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication,QCheckBox
from PyQt6.QtTest import QTest
from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.ui.timeline_view import TimelineView
from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel
from src.features.edit.ui.timeline_items import ClipItem
from src.features.edit.core.timeline_v2 import ClipInstance,EditTrack,TimelineSequence
from src.features.edit.core.clip_params import TransformParams,CropParams,SpeedParams
from src.features.edit.core.selection import TimelineSelection
app=QApplication([]);out={};errors=[]
sys.excepthook=lambda *a:errors.append(dict(type=a[0].__name__,message=str(a[1]),traceback=''.join(traceback.format_exception(*a))))
class Host(EditControllerMixin):
 def _request_program_viewer_update(self):pass
 def __init__(self,v,p,seq):
  self.edit_page=SimpleNamespace(_current_timeline=seq,_fps=24.0)
  self.timeline_panel=SimpleNamespace(get_timeline_view=lambda:v)
  self.edit_properties_panel=p;self.timeline_preview_renderer=None;self._current_edit_project=None;self.edit_info_panel=None

def make(locked=False,partner_locked=False,linked=False):
 c=ClipInstance(clip_id='c',asset_id='missing',track_id='v',timeline_start=100,timeline_duration=100,source_start=100,source_duration=100,transform=TransformParams(translate_x=10,translate_y=11),crop=CropParams(left=.1,right=.2,top=.05,bottom=.03,feather=.3),opacity=.8)
 d=replace(c,clip_id='d',track_id='v2',timeline_start=300,transform=TransformParams(translate_x=20,translate_y=21),crop=CropParams(left=.2,right=.1,top=.06,bottom=.02,feather=.4),opacity=.3)
 tracks=[EditTrack(track_id='v',name='V1',track_type='video',index=0,is_locked=locked,clips=(c,)),EditTrack(track_id='v2',name='V2',track_type='video',index=1,clips=(d,))]
 if linked:
  c=replace(c,linked_clip_id='a');tracks[0]=replace(tracks[0],clips=(c,));a=replace(c,clip_id='a',track_id='a1',linked_clip_id='c');ats=(EditTrack(track_id='a1',name='A1',track_type='audio',index=0,is_locked=partner_locked,clips=(a,)),)
 else:ats=()
 return TimelineSequence(timeline_id='p',name='p',fps=24,video_tracks=tuple(tracks),audio_tracks=ats)
def setup(seq,multi):
 v=TimelineView();v.load_timeline(seq);v.set_selection(TimelineSelection().with_clip_selected('c','v','p').with_clip_selected('d','v2','p',replace=False) if multi else TimelineSelection().with_clip_selected('c','v','p'))
 p=EditPropertiesPanel();p.resize(500,1000);p.set_clips([seq.get_clip_by_id(cid) for cid in (['c','d'] if multi else ['c'])],media_type='video');p.show();app.processEvents();h=Host(v,p,seq)
 v.timeline_changed.connect(lambda t:setattr(h.edit_page,'_current_timeline',t));p.clip_param_changed.connect(h._on_edit_clip_param_changed);p.clips_param_batch_changed.connect(h._on_edit_clips_param_batch_changed);p.reset_transform_requested.connect(h._on_edit_reset_transform)
 return v,p,h
def expand(p,group):
 section=next(s for sid,s in p._sections.items() if sid.endswith(':'+group))
 if section._is_collapsed:QTest.mouseClick(section._collapse_btn,Qt.MouseButton.LeftButton);app.processEvents()
 return section
def input_value(p,group,param,value):
 section=expand(p,group);w=section._param_widgets[param]['widget'];QTest.mouseDClick(w,Qt.MouseButton.LeftButton);app.processEvents();QTest.keyClick(w._edit,Qt.Key.Key_A,Qt.KeyboardModifier.ControlModifier);QTest.keyClicks(w._edit,str(value));QTest.keyClick(w._edit,Qt.Key.Key_Return);app.processEvents();return w.value()
def click_reset(p,group):
 section=expand(p,group);QTest.mouseClick(section._reset_btn,Qt.MouseButton.LeftButton);app.processEvents()
def state(v,p):
 def vals(c):return dict(x=c.transform.translate_x,y=c.transform.translate_y,scale_x=c.transform.scale_x,scale_y=c.transform.scale_y,crop=c.crop.to_dict(),opacity=c.opacity,speed=c.effective_speed(),reverse=c.speed.reverse if isinstance(c.speed,SpeedParams) else False,duration=c.timeline_duration,linked=c.linked_clip_id)
 widgets={sid:{k:info['widget'].value() for k,info in section._param_widgets.items() if 'widget' in info and callable(getattr(info['widget'],'value',None))} for sid,section in p._sections.items()}
 items={cid:dict(duration=i._duration_frames,source_start=i._source_start,source_duration=i._source_duration,speed=i._speed,opacity=i._opacity,locked=i._is_track_locked,enabled=i._is_enabled) for cid,i in v._items_map.items() if isinstance(i,ClipItem)}
 return dict(items=items,widgets=widgets,model={c.clip_id:vals(c) for t in v._timeline.all_tracks for c in t.clips},panel_snapshots={c.clip_id:vals(c) for c in p._current_clips},panel_title=p._title_label.text(),view_selection=list(v._selection.selected_clip_ids),undo=len(v._command_stack._undo_stack),redo=len(v._command_stack._redo_stack),revision=v._timeline.revision)
for group,param,value in [('transform','translate_x',99),('crop','left',5),('opacity','value',50),('speed','speed',2)]:
 errors.clear();v,p,h=setup(make(locked=True),False);before=state(v,p);widget=input_value(p,group,param,value);out['locked_single_'+group]=dict(before=before,after=state(v,p),widget_value=widget,errors=list(errors));p.close();v.close()
errors.clear();v,p,h=setup(make(partner_locked=True,linked=True),False);input_value(p,'speed','speed',2);out['locked_partner_single_speed']=dict(after=state(v,p),errors=list(errors));p.close();v.close()
for case in ['batch_xy','batch_crop_sides','batch_speed_undo','batch_crop_reset','batch_speed_reset','batch_transform_reset_context','batch_locked_target']:
 errors.clear();v,p,h=setup(make(locked=case=='batch_locked_target'),True);steps=[dict(stage='before',state=state(v,p))]
 if case=='batch_xy':
  input_value(p,'transform','translate_x',55);steps.append(dict(stage='X55',state=state(v,p)));input_value(p,'transform','translate_y',66);steps.append(dict(stage='Y66',state=state(v,p)))
 if case=='batch_crop_sides':
  input_value(p,'crop','left',5);steps.append(dict(stage='left5',state=state(v,p)));input_value(p,'crop','right',7);steps.append(dict(stage='right7',state=state(v,p)))
 if case=='batch_speed_undo':
  input_value(p,'speed','speed',2);steps.append(dict(stage='Speed2',state=state(v,p)));v.undo();steps.append(dict(stage='Undo',state=state(v,p)))
 if case in ['batch_crop_reset','batch_speed_reset']:
  click_reset(p,'crop' if case=='batch_crop_reset' else 'speed');steps.append(dict(stage='Reset',state=state(v,p)));v.undo();steps.append(dict(stage='OneUndo',state=state(v,p)))
 if case=='batch_transform_reset_context':
  click_reset(p,'transform');steps.append(dict(stage='Reset',state=state(v,p)));input_value(p,'transform','translate_y',88);steps.append(dict(stage='Y88',state=state(v,p)))
 if case=='batch_locked_target':
  widget=input_value(p,'transform','translate_x',99);steps.append(dict(stage='X99',widget_value=widget,state=state(v,p)))
 out[case]=dict(steps=steps,errors=list(errors));p.close();v.close();app.processEvents()
Path('/tmp/topos_round3_interaction_properties_full.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps({k:dict(errors=[e['message'] for e in d['errors']],after=(d.get('after') or d.get('steps',[])[-1]['state'])) for k,d in out.items()},ensure_ascii=False,indent=2))
