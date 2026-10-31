"""Read-only model recheck; fixtures synthetic, no QApplication/GPU/file mutation."""
from dataclasses import replace
import json
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence, TransitionInstance, TimelineMarker
from src.features.edit.core.clip_params import SpeedParams, SpeedPoint
from src.features.edit.core.commands import *
from src.features.edit.core.timeline_ops import validate_timeline, timeline_duration_for_source

def clip(id='c', start=0, dur=100, source_start=100, source_duration=None, track='v', **kw):
    return ClipInstance(clip_id=id,asset_id='asset_'+id,track_id=track,timeline_start=start,timeline_duration=dur,source_start=source_start,source_duration=dur if source_duration is None else source_duration,**kw)
def seq(*clips, markers=(), transitions=(), locked=False, audio=()):
    return TimelineSequence(timeline_id='tl',video_tracks=(EditTrack(track_id='v',clips=tuple(clips),transitions=tuple(transitions),is_locked=locked),),audio_tracks=tuple(audio),markers=tuple(markers))
def result(cmd, tl):
    r=cmd.apply(tl); return r if isinstance(r,tuple) else (r,cmd)
def maps(c): return [c.timeline_to_source_frame(i) for i in range(c.timeline_start,c.timeline_end)]
def mapped(tl, frames, track='v'):
    out=[]
    for i in frames:
        c=next(c for c in tl.get_track_by_id(track).clips if c.timeline_start<=i<c.timeline_end)
        out.append(c.timeline_to_source_frame(i))
    return out

def run(name, fn):
    print('\nCASE:',name)
    try: fn()
    except Exception as e: print(type(e).__name__+': '+str(e))

def fade_explicit():
    c=clip(fade_out_duration=40)
    cmd=TrimClipCommand.create_left_trim('v','c',80,180,new_source_duration=20)
    cmd.apply(seq(c))
run('explicit left trim fade40 to duration20',fade_explicit)

def reverse_right():
    c=clip(speed=SpeedParams(reverse=True)); expected=maps(c)[:90]
    ns,nd=c.with_right_edge_source_values(90)
    cmd=TrimClipCommand.create_right_trim('v','c',90,nd,new_source_start=ns)
    changed,cmd=result(cmd,seq(c)); after=changed.get_clip_by_id('c')
    print('helper start/duration:',ns,nd,'actual:',after.source_start,after.source_duration)
    print('before/after head:',expected[:3],maps(after)[:3], 'equal:',expected==maps(after))
    explicit=replace(cmd,new_timeline_start=0)
    changed,explicit=result(explicit,seq(c)); undone=explicit.undo(changed)
    print('direct explicit apply source:',changed.get_clip_by_id('c').source_start,'undo source:',undone.get_clip_by_id('c').source_start,'expected undo:',c.source_start)
run('reverse right factory and explicit undo',reverse_right)

def trim_undo_payload():
    c=clip(fade_out_duration=40); t=TransitionInstance(transition_id='head',track_id='v',right_clip_id='c',center_frame=0,duration=40,alignment='start_on_cut')
    tl=seq(c,transitions=(t,)); changed,cmd=result(TrimClipCommand.create_right_trim('v','c',20,20),tl); undone=cmd.undo(changed)
    print('fade before/after/undo:',c.fade_out_duration,changed.get_clip_by_id('c').fade_out_duration,undone.get_clip_by_id('c').fade_out_duration)
    print('trans duration before/after/undo:',40,changed.video_tracks[0].transitions[0].duration,undone.video_tracks[0].transitions[0].duration,'full undo equal:',tl.to_dict()==undone.to_dict())
run('trim undo fade and transition payload',trim_undo_payload)

def speed_undo_payload():
    c=clip(fade_out_duration=40); tl=seq(c)
    changed,cmd=result(ChangeClipSpeedCommand.create('c','v',5),tl); undone=cmd.undo(changed)
    print('duration/fade before/after/undo:',[(k.timeline_duration,k.fade_out_duration) for k in (c,changed.get_clip_by_id('c'),undone.get_clip_by_id('c'))])
    print('full undo equal:',tl.to_dict()==undone.to_dict())
run('speed undo fade payload',speed_undo_payload)

def marker_seam():
    tl=seq(clip(),markers=(TimelineMarker(marker_id='m',frame=50,clip_id='c',track_id='v'),))
    changed,cmd=result(SplitClipCommand.create('v','c',50),tl)
    print('marker owner/range:',changed.markers[0].clip_id,[(c.clip_id,c.timeline_start,c.timeline_end) for c in changed.video_tracks[0].clips])
    print('validation:',validate_timeline(changed))
run('split marker exactly seam',marker_seam)

def marker_roundtrip():
    tl=seq(clip(),markers=(TimelineMarker(marker_id='m',frame=75,clip_id='c',track_id='v'),))
    changed,cmd=result(SplitClipCommand.create('v','c',50),tl)
    loaded=SplitClipCommand.from_dict(json.loads(json.dumps(cmd.to_dict())))
    undone=loaded.undo(changed)
    print('owner before/after/deserialized undo:',tl.markers[0].clip_id,changed.markers[0].clip_id,undone.markers[0].clip_id)
    print('validation:',validate_timeline(undone),'full undo equal:',tl.to_dict()==undone.to_dict())
run('split marker command JSON roundtrip undo',marker_roundtrip)

def transition_roll_macro():
    a=clip('a',0); b=clip('b',100)
    t=TransitionInstance(transition_id='seam',track_id='v',left_clip_id='a',right_clip_id='b',center_frame=100,duration=20)
    tl=seq(a,b,transitions=(t,)); changed,cmd=result(RollTrimCommand.create('v','a','b',-10),tl); undone=cmd.undo(changed)
    print('roll transition before/after/undo:',len(tl.video_tracks[0].transitions),len(changed.video_tracks[0].transitions),len(undone.video_tracks[0].transitions),'full undo equal:',tl.to_dict()==undone.to_dict())
    macro=MacroCommand.create('Move pair',(MoveClipCommand.create('v','a',200),MoveClipCommand.create('v','b',300)))
    moved,macro=result(macro,tl)
    print('group clips:',[(c.clip_id,c.timeline_start,c.timeline_end) for c in moved.video_tracks[0].clips], 'transitions:',len(moved.video_tracks[0].transitions))
run('roll and group-move delete valid seam transition',transition_roll_macro)

def locked_commands():
    tl=seq(clip(),locked=True)
    commands=[MoveClipCommand.create('v','c',200),TrimClipCommand.create_right_trim('v','c',90,90),DeleteClipCommand.create('v','c'),InsertClipCommand.create('new',0,9,0,'v'),OverwriteClipCommand.create('new',0,9,0,'v'),RippleTrimCommand.create_right_trim('v','c',90)]
    for cmd in commands:
        try:
            changed,_=result(cmd,tl)
            print(type(cmd).__name__,'accepted changed:',tl.to_dict()!=changed.to_dict())
        except Exception as e: print(type(cmd).__name__,'rejected:',type(e).__name__,str(e))
run('locked-track command matrix',locked_commands)

def mixedfps_insert_sync():
    a=clip('follower',100); au=replace(a,clip_id='audio',track_id='a')
    tl=seq(a,audio=(EditTrack(track_id='a',track_type='audio',clips=(au,),is_sync_locked=True),))
    cmd=replace(InsertClipCommand.create('new',0,29,0,'v'),_source_fps_num=30,_source_fps_den=1,_timeline_fps_num=24,_timeline_fps_den=1)
    changed,cmd=result(cmd,tl)
    new=changed.get_clip_by_id(cmd._inserted_clip_id)
    print('inserted duration/ripple_offset:',new.timeline_duration,cmd._ripple_offset)
    print('video/audio follower starts:',changed.get_clip_by_id('follower').timeline_start,changed.get_clip_by_id('audio').timeline_start,'expected:',124,124)
run('mixedFPS insert target/sync tracks delta mismatch',mixedfps_insert_sync)

def insert_slice_reverse():
    c=clip(speed=SpeedParams(reverse=True)); tl=seq(c)
    changed,cmd=result(InsertClipCommand.create('new',0,9,40,'v'),tl)
    remapped=mapped(changed,list(range(40))+list(range(50,110)))
    print('before/after first seam/tail:',maps(c)[:3],remapped[:3],maps(c)[38:43],remapped[38:43],maps(c)[-3:],remapped[-3:],'equal:',maps(c)==remapped)
run('insert interior reverse slice playback changed',insert_slice_reverse)

def insert_target_followers():
    c=clip('follower',100).with_keyframe_added('opacity',100,0).with_keyframe_added('opacity',199,1)
    tl=seq(c,markers=(TimelineMarker(marker_id='m',frame=110,clip_id='follower',track_id='v'),))
    changed,cmd=result(InsertClipCommand.create('new',0,19,0,'v'),tl)
    follower=changed.get_clip_by_id('follower')
    print('clip start/marker/firstkey before:',100,110,100,'after:',follower.timeline_start,changed.markers[0].frame,[(k.frame,k.value) for k in follower.animated_params['opacity'].keyframes.keyframes])
    print('local opacity before/after:',c.get_param_at_frame('opacity',100),follower.get_param_at_frame('opacity',120),'validation:',validate_timeline(changed))
run('insert target followers marker/keys do not follow',insert_target_followers)

def ripple_target_followers():
    c=clip('follower',100).with_keyframe_added('opacity',100,0).with_keyframe_added('opacity',199,1)
    tl=seq(clip('deleted',0,20),c,markers=(TimelineMarker(marker_id='m',frame=110,clip_id='follower',track_id='v'),))
    changed,cmd=result(RippleDeleteCommand.create(tl,'v','deleted'),tl)
    follower=changed.get_clip_by_id('follower')
    print('follower start/marker/keys after:',follower.timeline_start,changed.markers[0].frame,[(k.frame,k.value) for k in follower.animated_params['opacity'].keyframes.keyframes])
    print('local frame10 opacity before/after:',c.get_param_at_frame('opacity',110),follower.get_param_at_frame('opacity',90))
run('ripple target followers marker/keys do not follow',ripple_target_followers)

def split_matrix():
    rows=[]
    for fps in [None,(30,24),(24,30)]:
        for reverse in [False,True]:
            for name,sp in [('constant.5',SpeedParams(speed=.5,reverse=reverse)),('constant1.25',SpeedParams(speed=1.25,reverse=reverse)),('step',SpeedParams(speed=1,reverse=reverse,speed_points=(SpeedPoint(40,2),))),('smooth',SpeedParams(speed=1,reverse=reverse,speed_points=(SpeedPoint(40,2,'smooth'),)))]:
                sn,tn=(fps if fps else (None,None)); dur=timeline_duration_for_source(120,sp,sn,1,tn,1)
                c=clip(dur=dur,source_duration=120,speed=sp,_source_fps_num=sn,_timeline_fps_num=tn)
                cut=max(1,dur//2+1)
                changed,_=result(SplitClipCommand.create('v','c',cut),seq(c))
                before=maps(c); after=mapped(changed,range(dur)); mismatches=[(i,b,a) for i,(b,a) in enumerate(zip(before,after)) if b!=a]
                rows.append((fps,reverse,name,dur,cut,len(mismatches),mismatches[:4]))
    for row in rows: print(row)
run('split preservation matrix (fps,reverse,speed,duration,cut,mismatchcount,examples)',split_matrix)

def ramp_fps_mapper():
    sp=SpeedParams(speed=1,speed_points=(SpeedPoint(60,2),)); dur=timeline_duration_for_source(120,sp,30,1,24,1)
    c=clip(dur=dur,source_duration=120,speed=sp,_source_fps_num=30,_timeline_fps_num=24)
    print('duration:',dur,'mapped last5:',maps(c)[-5:],'expected unrounded source offset at frame71:',60+(71/0.8-60)*2,'mapped:',c.timeline_to_source_frame(71)-100)
    smooth=SpeedParams(speed=1,speed_points=(SpeedPoint(60,2,'smooth'),))
    print('smooth helper at24/24 vs authoritative SpeedParams duration:',timeline_duration_for_source(120,smooth,24,1,24,1),smooth.timeline_duration_for_source_duration(120))
run('variable speed mixedFPS mapper and smooth duration integration',ramp_fps_mapper)

def roll_reverse():
    a=clip('a',0,speed=SpeedParams(reverse=True)); b=clip('b',100,speed=SpeedParams(reverse=True)); tl=seq(a,b)
    changed,cmd=result(RollTrimCommand.create('v','a','b',10),tl)
    aa=changed.get_clip_by_id('a'); bb=changed.get_clip_by_id('b')
    print('before/after retained A head:',maps(a)[:5],maps(aa)[:5], 'B at110:',b.timeline_to_source_frame(110),bb.timeline_to_source_frame(110))
run('reverse roll retains wrong content',roll_reverse)

def batch_locked_sync():
    a=clip('audio',100,track='a')
    tl=seq(clip('d1',0,10),clip('d2',20,10),audio=(EditTrack(track_id='a',track_type='audio',is_locked=True,is_sync_locked=True,clips=(a,)),))
    changed,cmd=result(BatchDeleteCommand.create(tl,['d1','d2'],ripple=True),tl)
    print('locked audio start before/after:',100,changed.get_clip_by_id('audio').timeline_start)
run('batch cumulative sync shifts locked track (new regression)',batch_locked_sync)

def command_payload_roundtrip():
    c=clip('a',0); t=TransitionInstance(transition_id='head',track_id='v',right_clip_id='a',center_frame=0,duration=10,alignment='start_on_cut')
    tl=seq(c,transitions=(t,))
    for command in [DeleteClipCommand.create('v','a'),RippleDeleteCommand.create(tl,'v','a'),BatchDeleteCommand.create(tl,['a'])]:
        changed,updated=result(command,tl)
        loaded=type(updated).from_dict(json.loads(json.dumps(updated.to_dict())))
        restored=loaded.undo(changed)
        print(type(command).__name__,'before/after/loadedundo trans:',1,len(changed.video_tracks[0].transitions),len(restored.video_tracks[0].transitions),'full undo equal:',tl.to_dict()==restored.to_dict())
    b=clip('b',100); t2=TransitionInstance(transition_id='seam',track_id='v',left_clip_id='a',right_clip_id='b',center_frame=100,duration=20)
    tl2=seq(c,b,transitions=(t2,)); changed,cmd=result(MoveClipCommand.create('v','a',200),tl2)
    restored=MoveClipCommand.from_dict(json.loads(json.dumps(cmd.to_dict()))).undo(changed)
    print('MoveClipCommand loadedundo trans:',len(restored.video_tracks[0].transitions))
    tl3=seq(clip('a',0,90),clip('b',100,90),clip('c',200,100))
    changed,cmd=result(SlideClipCommand.create(tl3,'v','b',5),tl3)
    restored=SlideClipCommand.from_dict(json.loads(json.dumps(cmd.to_dict()))).undo(changed)
    print('SlideClipCommand full serialized undo equal:',tl3.to_dict()==restored.to_dict(),'before/undo ranges:',[(c.timeline_start,c.timeline_duration) for c in tl3.video_tracks[0].clips],[(c.timeline_start,c.timeline_duration) for c in restored.video_tracks[0].clips])
run('new snapshots missing from command serialized payload',command_payload_roundtrip)

def trim_variable_left():
    sp=SpeedParams(speed=1,speed_points=(SpeedPoint(50,2),)); c=clip(dur=75,speed=sp,source_duration=100)
    ns,nd=c.with_left_edge_source_values(20)
    changed,cmd=result(TrimClipCommand.create_left_trim('v','c',20,ns,new_source_duration=nd),seq(c))
    new=changed.get_clip_by_id('c')
    print('source start/duration/speedpoints:',new.source_start,new.source_duration,new.speed.speed_points)
    print('before/after retained timeline[50,60,70]:',[c.timeline_to_source_frame(i) for i in [50,60,70]],[new.timeline_to_source_frame(i) for i in [50,60,70]])
run('variable-speed left trim does not rebase points',trim_variable_left)

def inverse_fps():
    c=clip(dur=80,source_duration=100,_source_fps_num=30,_timeline_fps_num=24)
    inv=c.source_to_timeline_frame(160)
    print('source160 inverse timeline:',inv,'expected:',48,'mapped back:',c.timeline_to_source_frame(inv))
run('mixedFPS source-to-timeline inverse ignores FPS',inverse_fps)

def batch_sync_collision():
    audio=EditTrack(track_id='a',track_type='audio',is_sync_locked=True,clips=(clip('fixed',0,25,track='a'),clip('follower',30,10,track='a')))
    tl=seq(clip('d1',0,10),clip('d2',20,10),audio=(audio,))
    changed,cmd=result(BatchDeleteCommand.create(tl,['d1','d2'],ripple=True),tl)
    print('audio ranges:',[(c.clip_id,c.timeline_start,c.timeline_end) for c in changed.audio_tracks[0].clips],'validation:',validate_timeline(changed))
run('batch cumulative sync creates overlap behind fixed clip',batch_sync_collision)

def stack_noop():
    tl=seq(clip()); stack=CommandStack()
    changed=stack.execute(MoveClipCommand.create('v','c',200),tl); undone=stack.undo(changed)
    print('redo before no-op:',stack.can_redo(),'history:',stack.get_history_size())
    unchanged=stack.execute(MoveClipCommand.create('v','c',0),undone)
    print('same timeline:',unchanged is undone,'redo after:',stack.can_redo(),'history:',stack.get_history_size())
run('no-op command clears redo and pushes history',stack_noop)

print('\\nDONE')
