from __future__ import annotations
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path.cwd()))
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.core.commands import AddKeyframeCommand, UpdateClipParamCommand, MoveClipToExistingTrackCommand, FreezeFrameAtPlayheadCommand
from src.features.edit.core.clip_params import TransformParams, SpeedParams
from src.features.edit.core.captions.importer import parse_subtitle_text
from src.features.edit.core.timeline_ops import validate_timeline
from src.shared.media.metadata import Rational

def seq(c, tracks=()):
    return TimelineSequence(timeline_id='s',name='S',width=1920,height=1080,fps=Rational(25,1),video_tracks=(EditTrack('v1','V1','video',0,clips=(c,)),)+tracks)

out = {}
c=ClipInstance('c','a','v1',100,100,0,100)
s=seq(c,(EditTrack('v2','V2','video',1),))
cmd=MoveClipToExistingTrackCommand.create('v1','v2','c',300)
sm=cmd.apply(s); su=cmd.undo(sm)
out['legacy_move_undo']={'original':100,'moved':sm.get_clip_by_id('c').timeline_start,'restored':su.get_clip_by_id('c').timeline_start,'stored_old':cmd.old_timeline_start}

def ttml(attrs, body):
    return '<tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttp="http://www.w3.org/ns/ttml#parameter" '+attrs+'><body>'+body+'</body></tt>'
for key, attrs, body in [
    ('default_tick_rate','ttp:frameRate="30"','<p begin="30t" end="60t">X</p>'),
    ('parent_end','', '<div begin="1s" end="3s"><p begin="1s" end="9s">X</p></div>'),
    ('media_frame_clock','ttp:frameRate="30"','<p begin="00:00:01:00" end="00:00:02:00">X</p>'),
    ('media_frame_clock_mixed','ttp:frameRate="30"','<p begin="00:00:01:00" end="2s">X</p>'),
    ('invalid_tick','ttp:tickRate="0"','<p begin="10t" end="20t">X</p>'),
]:
    try:
        cues=parse_subtitle_text(ttml(attrs,body),'.ttml',24)
        out[key]=[{'start':q.start_frame,'duration':q.duration_frames} for q in cues]
    except Exception as e:
        out[key]={'error':str(e)}

c=ClipInstance('c','a','v1',0,100,0,100)
s=seq(c)
add=AddKeyframeCommand.create('v1','c','transform.translate_x',0,0)
s=add.apply(s)
cmd=UpdateClipParamCommand('u','X','v1','c','transform',TransformParams(translate_x=100))
s=cmd.apply(s)
c=s.get_clip_by_id('c')
out['animated_static_edit']={'static_x':c.transform.translate_x,'rendered_x_frame50':c.get_param_at_frame('transform.translate_x',50)}

legacy=ClipInstance('legacy','a','v1',0,100,0,100).with_keyframe_added('transform.translate_x',0,-250,min_value=0,max_value=100)
legacy=ClipInstance.from_dict(legacy.to_dict())
legacy_seq=AddKeyframeCommand.create('v1','legacy','transform.translate_x',50,-500).apply(seq(legacy))
legacy=legacy_seq.get_clip_by_id('legacy')
out['legacy_keyframe_range']={'raw_values':[k.value for k in legacy.animated_params['transform.translate_x'].keyframes.keyframes], 'range':[legacy.animated_params['transform.translate_x'].keyframes.min_val,legacy.animated_params['transform.translate_x'].keyframes.max_val], 'sample0':legacy.get_param_at_frame('transform.translate_x',0),'sample50':legacy.get_param_at_frame('transform.translate_x',50)}

v=ClipInstance('vc','a','v1',0,100,0,100,linked_clip_id='ac')
a=ClipInstance('ac','a','a1',0,100,0,100,linked_clip_id='vc')
linked=replace(seq(v),audio_tracks=(EditTrack('a1','A1','audio',0,clips=(a,)),))
freeze=FreezeFrameAtPlayheadCommand.create('vc',50)
fs=freeze.apply(linked)
out['freeze_linked']={'clips':[{'id':x.clip_id,'link':x.linked_clip_id,'duration':x.timeline_duration,'start':x.timeline_start} for tr in fs.all_tracks for x in tr.clips], 'validation':[str(e) for e in validate_timeline(fs)]}

from PyQt6.QtWidgets import QApplication
from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel
app=QApplication.instance() or QApplication([])
p=EditPropertiesPanel(); p.set_clip(c); p.set_current_frame(50)
events=[]
p.keyframe_toggled.connect(lambda *args:events.append(args))
p._keyframe_buttons['transform.translate_x'].click()
out['panel_second_keyframe_request']=events
for section in p._sections.values():
    if section.section_id.endswith(':transform'):
        out['panel_display_x']=section._param_widgets['translate_x']['widget'].value()
p.close()

animated=ClipInstance('display','a','v1',0,100,0,100)
for f,vv in [(0,0),(50,100)]:
    animated=AddKeyframeCommand.create('v1','display','transform.translate_x',f,vv).apply(seq(animated)).get_clip_by_id('display')
p=EditPropertiesPanel();p.set_clip(animated);p.set_current_frame(50)
for section in p._sections.values():
    if section.section_id.endswith(':transform'):
        out['seek_display']={'frame':50,'model_sample':animated.get_param_at_frame('transform.translate_x',50),'display':section._param_widgets['translate_x']['widget'].value(),'diamond_value':p._keyframe_buttons['transform.translate_x']._current_value}
p.close()

print(json.dumps(out,indent=2,ensure_ascii=False))
