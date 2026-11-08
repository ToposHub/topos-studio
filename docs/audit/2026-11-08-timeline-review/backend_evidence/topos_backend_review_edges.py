from __future__ import annotations
import sys,json
from dataclasses import replace
sys.path.insert(0,'/Users/heng/Documents/vscode/topos-studio')
from PyQt6.QtWidgets import QApplication
from src.features.edit.core.timeline_v2 import ClipInstance,EditTrack,TimelineSequence,Rational
from src.features.edit.core.clip_params import SpeedParams,SpeedPoint
from src.features.edit.core.project import EditProject,MediaAsset
from src.features.edit.core.commands import SetClipFlagCommand
from src.features.edit.ui.timeline_view import TimelineView
from src.features.edit.core.selection import TimelineSelection
app=QApplication([]);result={}
base=ClipInstance('c','old','v1',0,48,0,48)
seq=TimelineSequence(timeline_id='t',name='t',fps=Rational(24,1),video_tracks=(EditTrack('v1','V1','video',0,clips=(base,)),EditTrack('v2','V2','video',1)))
new=MediaAsset('new','/tmp/new.mp4','video','new',{'fps':24.,'total_frames':1000})
def view(clip=base,asset=new):
 v=TimelineView();v.load_timeline(seq.with_clip_updated(clip));v.set_project(EditProject('p','P',assets={'new':asset}));v.set_selection(TimelineSelection().with_clip_selected('c','v1','t'));return v
v=view();v._on_target_track_changed('v2');result['blue_default']={'painted_source':v.source_patch_track_id('video'),'effective_source':v.resolve_source_destination_track_id('video'),'headers':{k:h._source_patch_enabled for k,h in v._track_headers.items()}};v.close()
frozen=replace(base,source_start=0,source_duration=48,freeze_source_frame=5);v=view(frozen);ok=v.replace_clip_with_source('new',200,247);c=v.get_timeline().get_clip_by_id('c');result['replace_freeze']={'ok':ok,'source_start':c.source_start,'source_end':c.source_end,'freeze':c.freeze_source_frame,'mapped_first':c.timeline_to_source_frame(0)};v.close()
ramp=replace(base,source_duration=80,speed=SpeedParams(speed=1.,speed_points=(SpeedPoint(16,2.),)));v=view(ramp);before=[ramp.timeline_to_source_frame(f) for f in (0,16,24,32,40,47)];ok=v.replace_clip_with_source('new',0,47);c=v.get_timeline().get_clip_by_id('c');result['replace_ramp']={'ok':ok,'before':before,'after':[c.timeline_to_source_frame(f) for f in (0,16,24,32,40,47)],'source_duration_before':ramp.source_duration,'source_duration_after':c.source_duration};v.close()
audio=MediaAsset('new','/tmp/new.wav','audio','new',{'fps':24.,'total_frames':1000,'audio_info':{'channels':2}});v=view(asset=audio);ok=v.replace_clip_with_source('new',0,47);c=v.get_timeline().get_clip_by_id('c');result['replace_audio_into_video']={'ok':ok,'track_type':v.get_timeline().get_track_by_id(c.track_id).track_type,'asset_type':audio.media_type,'asset_id':c.asset_id};v.close()
cmd=SetClipFlagCommand.create((('v1','c'),),'#zzzzzz');after=cmd.apply(seq);result['flag_nonhex_command']={'accepted':after.get_clip_by_id('c').flag_color}
print(json.dumps(result,indent=2));open('/tmp/topos_backend_review_edges_results.json','w').write(json.dumps(result,indent=2))
