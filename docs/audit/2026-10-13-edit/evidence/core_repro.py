from dataclasses import replace
from src.features.edit.core.timeline_v2 import TimelineSequence, EditTrack, ClipInstance, TransitionInstance, TimelineMarker
from src.features.edit.core.clip_params import SpeedParams, SpeedPoint
from src.features.edit.core.commands import CommandStack, SplitClipCommand, TrimClipCommand, RollTrimCommand, SlideClipCommand, ChangeClipSpeedCommand, MoveClipCommand
from src.features.edit.core.timeline_ops import validate_timeline

def clip(id, start, duration=100, source_start=100, **kw):
    return ClipInstance(clip_id=id,asset_id='asset',track_id='v',timeline_start=start,timeline_duration=duration,source_start=source_start,source_duration=duration,**kw)
def seq(*clips, **kw): return TimelineSequence(timeline_id='tl',video_tracks=(EditTrack(track_id='v',name='V1',clips=tuple(clips)),),**kw)
def maps(c): return [c.timeline_to_source_frame(i) for i in range(c.timeline_start,c.timeline_end)]
def case(name, fn):
    print('\n'+name)
    try: fn()
    except Exception as e: print(type(e).__name__+': '+str(e))

def roll_undo():
    tl=seq(clip('a',0),clip('b',100))
    stack=CommandStack(); changed=stack.execute(RollTrimCommand.create('v','a','b',-10),tl)
    print('after:',[(c.clip_id,c.timeline_start,c.timeline_duration) for c in changed.video_tracks[0].clips]); stack.undo(changed)
case('ROLL NEGATIVE UNDO',roll_undo)

def reverse_mapper():
    c=clip('a',0,speed=SpeedParams(speed=1,reverse=True))
    withfps=replace(c,_source_fps_num=24,_timeline_fps_num=24)
    print('without fps:',maps(c)[:3]); print('with fps:',maps(withfps)[:3])
case('REVERSE FPS',reverse_mapper)

def reverse_split():
    c=clip('a',0,speed=SpeedParams(speed=1,reverse=True)); tl=seq(c)
    new=CommandStack().execute(SplitClipCommand.create('v','a',40),tl)
    after=[new.get_clip_at_frame(i)[0].timeline_to_source_frame(i) for i in range(100)] if hasattr(new,'get_clip_at_frame') else []
    after=[next(k for k in new.video_tracks[0].clips if k.timeline_start<=i<k.timeline_end).timeline_to_source_frame(i) for i in range(100)]
    print('before head/seam/tail:',maps(c)[:3],maps(c)[38:43],maps(c)[-3:]); print('after:',after[:3],after[38:43],after[-3:]); print('ranges',[(k.source_start,k.source_duration) for k in new.video_tracks[0].clips])
case('REVERSE SPLIT',reverse_split)

def split_fade():
    c=clip('a',0,fade_out_duration=40); CommandStack().execute(SplitClipCommand.create('v','a',20),seq(c))
case('SPLIT FADE OUT',split_fade)

def trim_fade():
    c=clip('a',0,fade_out_duration=40); CommandStack().execute(TrimClipCommand.create_right_trim('v','a',20,20),seq(c))
case('TRIM FADE OUT',trim_fade)

def slide_gap():
    tl=seq(clip('a',0,90),clip('b',100,90),clip('c',200,100))
    s=CommandStack(); changed=s.execute(SlideClipCommand.create(tl,'v','b',5),tl); restored=s.undo(changed)
    print('before:',[(c.timeline_start,c.timeline_duration) for c in tl.video_tracks[0].clips]); print('after:',[(c.timeline_start,c.timeline_duration) for c in changed.video_tracks[0].clips]); print('undo:',[(c.timeline_start,c.timeline_duration) for c in restored.video_tracks[0].clips])
case('SLIDE GAPS UNDO',slide_gap)

def speed_fps():
    c=clip('a',0,duration=120,_source_fps_num=30,_timeline_fps_num=24); c=replace(c,timeline_duration=96)
    changed=CommandStack().execute(ChangeClipSpeedCommand.create('a','v',2.0),seq(c)).get_clip_by_id('a')
    print('before timeline/source duration',c.timeline_duration,c.source_duration,'after',changed.timeline_duration,changed.source_duration,'expected',48)
case('SPEED MIXED FPS',speed_fps)

from src.features.edit.core.commands import InsertClipCommand, AddTransitionCommand, RippleTrimCommand, RippleDeleteCommand

def insert_undo():
    tl=seq(clip('a',0)); s=CommandStack(); command=InsertClipCommand.create('new_asset',0,9,40,'v'); changed=s.execute(command,tl); restored=s.undo(changed)
    print('after',[(c.clip_id,c.timeline_start,c.timeline_duration) for c in changed.video_tracks[0].clips]); print('undo',[(c.clip_id,c.timeline_start,c.timeline_duration) for c in restored.video_tracks[0].clips])
case('INSERT MIDCLIP UNDO',insert_undo)

def variable_split():
    c=clip('a',0,speed=SpeedParams(speed=1,speed_points=(SpeedPoint(50,2),))); c=replace(c,timeline_duration=75)
    changed=CommandStack().execute(SplitClipCommand.create('v','a',60),seq(c)); after=[next(k for k in changed.video_tracks[0].clips if k.timeline_start<=i<k.timeline_end).timeline_to_source_frame(i) for i in range(75)]
    print('before at seam/tail',maps(c)[58:65],maps(c)[-3:]); print('after',after[58:65],after[-3:]); print('point offsets',[k.speed.speed_points for k in changed.video_tracks[0].clips])
case('VARIABLE SPEED SPLIT',variable_split)

def transition_split_undo():
    c=clip('a',0); t=TransitionInstance(transition_id='fade',track_id='v',right_clip_id='a',center_frame=0,duration=10,alignment='start_on_cut')
    tl=seq(c); tl=tl.with_track_updated(tl.video_tracks[0].with_transition_added(t)); s=CommandStack(); changed=s.execute(SplitClipCommand.create('v','a',50),tl); restored=s.undo(changed)
    print('before/after/undo transition count',len(tl.video_tracks[0].transitions),len(changed.video_tracks[0].transitions),len(restored.video_tracks[0].transitions))
case('SPLIT TRANSITION UNDO',transition_split_undo)

def transition_move():
    c=clip('a',0); t=TransitionInstance(transition_id='fade',track_id='v',right_clip_id='a',center_frame=0,duration=10,alignment='start_on_cut')
    tl=seq(c); tl=tl.with_track_updated(tl.video_tracks[0].with_transition_added(t)); changed=CommandStack().execute(MoveClipCommand.create('v','a',200),tl)
    print('clip/trans start',changed.get_clip_by_id('a').timeline_start,changed.video_tracks[0].transitions[0].start_frame); print('validation',validate_timeline(changed))
case('MOVE TRANSITION ANCHOR',transition_move)

def clip_animation_move():
    c=clip('a',0).with_keyframe_added('opacity',0,0).with_keyframe_added('opacity',99,1)
    changed=CommandStack().execute(MoveClipCommand.create('v','a',200),seq(c)).get_clip_by_id('a'); print('before/after local frame0 opacity',c.get_param_at_frame('opacity',0),changed.get_param_at_frame('opacity',200))
case('MOVE ANIMATION ANCHOR',clip_animation_move)

def linked_timeline(audio_locked=False):
    v=clip('v0',0,linked_clip_id='a0'); a=replace(v,clip_id='a0',track_id='a',linked_clip_id='v0')
    return TimelineSequence(timeline_id='tl',video_tracks=(EditTrack(track_id='v',clips=(v,)),),audio_tracks=(EditTrack(track_id='a',track_type='audio',is_locked=audio_locked,clips=(a,)),))

def linked_ripple_core():
    changed=CommandStack().execute(RippleTrimCommand.create_right_trim('v','v0',80),linked_timeline()); print('video/audio duration',changed.get_clip_by_id('v0').timeline_duration,changed.get_clip_by_id('a0').timeline_duration)
case('LINKED RIPPLE CORE',linked_ripple_core)

def locked_speed():
    tl=linked_timeline(True); changed=CommandStack().execute(ChangeClipSpeedCommand.create('v0','v',2),tl); print('locked audio duration before/after',tl.get_clip_by_id('a0').timeline_duration,changed.get_clip_by_id('a0').timeline_duration)
case('LOCKED PARTNER SPEED',locked_speed)

# Exercise UI release handler without constructing any QWidget / QApplication.
from src.features.edit.ui.timeline_view import TimelineView
class FakeView:
    def __init__(self,tl):
        self._timeline=tl; self._updating=False; self._snap_enabled=False; self._items_map={}; self._command_stack=CommandStack()
    def _execute_command(self,cmd): self._timeline=self._command_stack.execute(cmd,self._timeline)
def linked_ripple_ui():
    fake=FakeView(linked_timeline()); TimelineView._on_ripple_trim_right_finished(fake,'v0','v',100,80)
    print('UI video/audio duration',fake._timeline.get_clip_by_id('v0').timeline_duration,fake._timeline.get_clip_by_id('a0').timeline_duration)
case('LINKED RIPPLE UI RELEASE HANDLER',linked_ripple_ui)
from src.features.edit.core.commands import BatchDeleteCommand, DeleteClipCommand

def batch_sync_undo():
    v=(clip('v0',0,10),clip('v1',20,10)); a=replace(clip('audio',50,10),track_id='a')
    tl=TimelineSequence(timeline_id='tl',video_tracks=(EditTrack(track_id='v',clips=v),),audio_tracks=(EditTrack(track_id='a',track_type='audio',clips=(a,)),))
    s=CommandStack(); changed=s.execute(BatchDeleteCommand.create(tl,['v0','v1'],ripple=True),tl); restored=s.undo(changed)
    print('audio start before/after/undo',a.timeline_start,changed.get_clip_by_id('audio').timeline_start,restored.get_clip_by_id('audio').timeline_start)
case('BATCH RIPPLE CROSS TRACK UNDO',batch_sync_undo)

def batch_sync_threshold():
    v=(clip('v0',0,10),clip('v1',20,10)); a=replace(clip('audio',30,10),track_id='a')
    tl=TimelineSequence(timeline_id='tl',video_tracks=(EditTrack(track_id='v',clips=v),),audio_tracks=(EditTrack(track_id='a',track_type='audio',clips=(a,)),))
    changed=CommandStack().execute(BatchDeleteCommand.create(tl,['v0','v1'],ripple=True),tl)
    print('audio start before/after/expected',a.timeline_start,changed.get_clip_by_id('audio').timeline_start,10)
case('BATCH RIPPLE ORIGINAL THRESHOLD',batch_sync_threshold)

def delete_transition_undo():
    c=clip('a',0); t=TransitionInstance(transition_id='fade',track_id='v',right_clip_id='a',center_frame=0,duration=10,alignment='start_on_cut')
    tl=seq(c); tl=tl.with_track_updated(tl.video_tracks[0].with_transition_added(t)); s=CommandStack(); changed=s.execute(DeleteClipCommand.create('v','a'),tl); restored=s.undo(changed)
    print('before/after/undo transition count',len(tl.video_tracks[0].transitions),len(changed.video_tracks[0].transitions),len(restored.video_tracks[0].transitions))
case('DELETE TRANSITION UNDO',delete_transition_undo)

def trim_speed_ui():
    c=replace(clip('a',0,speed=2),source_duration=200); f=FakeView(seq(c)); f._snap_enabled=True; f._snap_indicator=None; f._linked_selection_enabled=False
    f._get_snap_points=lambda **kw: []; f._snap_frame=lambda n,*a,**kw:(n,False); f._validate_trim_right=lambda *a:True; f._get_reciprocal_linked_clip=lambda *a:None
    TimelineView._on_trim_right_finished(f,'a','v',100,200,80,160)
    result=f._timeline.get_clip_by_id('a'); print('actual timeline/source duration',result.timeline_duration,result.source_duration,'expected',80,160)
case('NORMAL TRIM SPEED UI RELEASE',trim_speed_ui)

def slide_fade_undo():
    tl=seq(clip('a',0,100,fade_out_duration=80),clip('b',100,100),clip('c',200,100)); s=CommandStack(); changed=s.execute(SlideClipCommand.create(tl,'v','b',-50),tl); restored=s.undo(changed)
    print('fade out before/after/undo',tl.get_clip_by_id('a').fade_out_duration,changed.get_clip_by_id('a').fade_out_duration,restored.get_clip_by_id('a').fade_out_duration)
case('SLIDE FADE UNDO',slide_fade_undo)

def split_marker():
    tl=seq(clip('a',0),markers=(TimelineMarker(marker_id='m',frame=75,clip_id='a',track_id='v'),)); s=CommandStack(); changed=s.execute(SplitClipCommand.create('v','a',50),tl); m=changed.markers[0]
    print('marker frame/owner clip end',m.frame,changed.get_clip_by_id(m.clip_id).timeline_end)
case('SPLIT SCOPED MARKER',split_marker)

def sync_marker():
    v=clip('v0',0,10); a=replace(clip('audio',50,10),track_id='a'); marker=TimelineMarker(marker_id='m',frame=55,clip_id='audio',track_id='a')
    tl=TimelineSequence(timeline_id='tl',video_tracks=(EditTrack(track_id='v',clips=(v,)),),audio_tracks=(EditTrack(track_id='a',track_type='audio',clips=(a,)),),markers=(marker,))
    s=CommandStack(); changed=s.execute(RippleDeleteCommand.create(tl,'v','v0'),tl); print('audio start/marker frame before',50,55,'after',changed.get_clip_by_id('audio').timeline_start,changed.markers[0].frame)
case('SYNC LOCK SCOPED MARKER',sync_marker)
