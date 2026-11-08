from __future__ import annotations
import sys, json
from types import SimpleNamespace
from dataclasses import replace
sys.path.insert(0,'/Users/heng/Documents/vscode/topos-studio')
from PyQt6.QtWidgets import QApplication
from src.features.edit.core.timeline_v2 import ClipInstance,EditTrack,TimelineSequence,Rational
from src.features.edit.core.project import EditProject,MediaAsset
from src.features.edit.core.commands import SetClipFlagCommand
from src.features.edit.ui.timeline_view import TimelineView
from src.features.edit.ui.edit_page import EditPage
from src.features.edit.core.selection import TimelineSelection
app=QApplication([]); result={}
c1=ClipInstance('c1','old','v1',0,48,0,48,flag_color='#4BA6DD')
c2=ClipInstance('c2','old','v1',48,48,48,48)
seq=TimelineSequence(timeline_id='t',name='t',fps=Rational(24,1),video_tracks=(EditTrack('v1','V1','video',0,clips=(c1,c2)),))
cmd=SetClipFlagCommand.create((('v1','c1'),('v1','c2')),'#4BA6DD')
after=cmd.apply(seq); undo=cmd.undo(after)
result['flag_mixed_undo']={'before':[c.flag_color for c in seq.video_tracks[0].clips],'after':[c.flag_color for c in after.video_tracks[0].clips],'undo':[c.flag_color for c in undo.video_tracks[0].clips]}
v=TimelineView();v.load_timeline(seq)
page=SimpleNamespace(_timeline_panel=SimpleNamespace(get_timeline_view=lambda:v),_main_window=SimpleNamespace(_start_recording=lambda tid:None,_recording_managers={}))
try: EditPage._on_timeline_voiceover_requested(page)
except Exception as exc: result['voiceover_without_audio']={'exception':repr(exc),'audio_tracks_after':[t.track_id for t in v.get_timeline().audio_tracks],'history':len(v._command_stack._undo_stack)}
v.close()
vc=ClipInstance('vc','old','v1',0,48,0,48,linked_clip_id='ac')
ac=ClipInstance('ac','old','a1',0,48,0,96,speed=2.0,linked_clip_id='vc')
seq2=TimelineSequence(timeline_id='r',name='r',fps=Rational(24,1),video_tracks=(EditTrack('v1','V1','video',0,clips=(vc,)),),audio_tracks=(EditTrack('a1','A1','audio',0,clips=(ac,)),))
new=MediaAsset('new','/tmp/new.mp4','video','new',{'fps':48.,'total_frames':1000,'audio_info':{'channels':2,'channel_layout':'stereo'}})
v=TimelineView();v.load_timeline(seq2);v.set_project(EditProject('p','P',assets={'new':new}));v.set_selection(TimelineSelection().with_clip_selected('vc','v1','r'))
success=v.replace_clip_with_source('new',0,10)
updated=v.get_timeline(); p=updated.get_clip_by_id('ac'); c=updated.get_clip_by_id('vc')
result['replace_partner_speed']={'success':success,'video_duration':c.source_duration,'audio_duration':p.source_duration,'audio_needed':int(p.timeline_duration*p.source_frames_per_timeline_frame()),'audio_source_last':p.timeline_to_source_frame(47),'speed':p.effective_speed()}
v.close()
print(json.dumps(result,indent=2));open('/tmp/topos_backend_review_results.json','w').write(json.dumps(result,indent=2))
