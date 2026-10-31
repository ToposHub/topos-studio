"""Round3 real model probes, no Qt window/GPU/production file changes."""
from dataclasses import replace
import json
from src.features.edit.core.timeline_v2 import ClipInstance,EditTrack,TimelineSequence,TimelineMarker,TransitionInstance
from src.features.edit.core.clip_params import SpeedParams,SpeedPoint
from src.features.edit.core.commands import *
from src.features.edit.core.timeline_ops import validate_timeline,timeline_duration_for_source,trim_clip_left

def clip(id='c',start=0,dur=100,track='v',source_start=100,source_duration=None,**kw):
 return ClipInstance(clip_id=id,asset_id='asset_'+id,track_id=track,timeline_start=start,timeline_duration=dur,source_start=source_start,source_duration=dur if source_duration is None else source_duration,**kw)
def seq(*clips,markers=(),transitions=(),audio=(),locked=False):
 return TimelineSequence(timeline_id='tl',video_tracks=(EditTrack(track_id='v',clips=tuple(clips),transitions=tuple(transitions),is_locked=locked),),audio_tracks=tuple(audio),markers=tuple(markers))
def apply(cmd,tl):
 r=cmd.apply(tl);return r if isinstance(r,tuple) else(r,cmd)
def case(name,fn):
 print('\nCASE:',name)
 try:fn()
 except Exception as e:print(type(e).__name__+':',e)
def snapshots(tl):return [(c.clip_id,c.track_id,c.timeline_start,c.timeline_end,c.linked_clip_id) for t in tl.all_tracks for c in t.clips]
def seam():
 a=clip('a',0);b=clip('b',100);t=TransitionInstance(transition_id='seam',track_id='v',left_clip_id='a',right_clip_id='b',center_frame=100,duration=20)
 return seq(a,b,transitions=(t,))
def macro_transition_delete():
 tl=seam()
 for command in [RemoveTransitionCommand.create(tl,'seam','v'),DeleteClipCommand.create('v','a')]:
  changed,cmd=apply(MacroCommand.create('intentional delete',(command,)),tl)
  print(type(command).__name__,'clips:',[c.clip_id for c in changed.video_tracks[0].clips],'trans:',[(t.transition_id,t.left_clip_id,t.right_clip_id,t.center_frame) for t in changed.video_tracks[0].transitions],'validation:',validate_timeline(changed))
case('Macro resurrects explicitly removed transition or deleted owner',macro_transition_delete)

def insert_markers():
 c=clip('c',100);a=clip('ac',100,track='a')
 tl=seq(c,audio=(EditTrack(track_id='a',track_type='audio',clips=(a,)),),markers=(TimelineMarker(marker_id='vm',frame=110,clip_id='c',track_id='v'),TimelineMarker(marker_id='am',frame=110,clip_id='ac',track_id='a')))
 changed,cmd=apply(InsertClipCommand.create('new',0,19,0,'v'),tl);undone=cmd.undo(changed);redone,cmd2=apply(cmd,undone)
 for label,t in [('before',tl),('apply',changed),('undo',undone),('redo',redone)]:print(label,'clip starts:',t.get_clip_by_id('c').timeline_start,t.get_clip_by_id('ac').timeline_start,'marker frames:',[(m.marker_id,m.frame) for m in t.markers],validate_timeline(t))
 loaded=InsertClipCommand.from_dict(json.loads(json.dumps(cmd.to_dict())));restored=loaded.undo(changed)
 print('JSON undo markers:',[(m.marker_id,m.frame) for m in restored.markers], 'equal:',tl.to_dict()==restored.to_dict())
case('Insert marker full transaction and JSON',insert_markers)

def linked_insert():
 v=clip('v0',0,linked_clip_id='a0');a=clip('a0',0,track='a',linked_clip_id='v0')
 tl=seq(v,audio=(EditTrack(track_id='a',track_type='audio',clips=(a,)),),markers=(TimelineMarker(marker_id='vm',frame=75,clip_id='v0',track_id='v'),TimelineMarker(marker_id='am',frame=75,clip_id='a0',track_id='a')))
 changed,cmd=apply(InsertLinkedAssetRippleCommand.create('new',0,9,40,'v','a'),tl)
 print('apply clips:',snapshots(changed));print('validation:',validate_timeline(changed));print('markers:',[(m.frame,m.clip_id) for m in changed.markers])
 undone=cmd.undo(changed);print('live undo equal:',tl.to_dict()==undone.to_dict(),'markers:',[(m.frame,m.clip_id) for m in undone.markers],validate_timeline(undone))
 loaded=InsertLinkedAssetRippleCommand.from_dict(json.loads(json.dumps(cmd.to_dict())));restored=loaded.undo(changed)
 print('JSON undo equal:',tl.to_dict()==restored.to_dict(),'clip count:',sum(len(t.clips) for t in restored.all_tracks))
 redone,cmd2=apply(cmd,undone);print('redo same IDs:',set(c.clip_id for t in changed.all_tracks for c in t.clips)==set(c.clip_id for t in redone.all_tracks for c in t.clips))
case('Linked ripple insert existing partner slices, markers, JSON and stable IDs',linked_insert)

def insert_lock_redo():
 tl=seq(clip());changed,cmd=apply(InsertClipCommand.create('new',0,9,40,'v'),tl);undone=cmd.undo(changed);locked=undone.with_track_updated(undone.video_tracks[0]._rebuild(is_locked=True));redone,_=apply(cmd,locked)
 print('locked redo accepted new clips:',len(redone.video_tracks[0].clips),'before:',len(locked.video_tracks[0].clips))
case('Insert snapshot redo bypasses new lock guard',insert_lock_redo)

def lock_extended():
 tl=seq(clip('a',0),clip('b',100),clip('c',200),locked=True)
 cmds=[SlipClipCommand.create('v','b',5),SlideClipCommand.create(tl,'v','b',5),RollTrimCommand.create('v','a','b',5),RippleDeleteCommand.create(tl,'v','b'),BatchDeleteCommand.create(tl,['a','b']),AppendClipCommand.create('new',0,9,'v'),FreezeFrameAtPlayheadCommand.create('b',150)]
 for cmd in cmds:
  try:
   changed,_=apply(cmd,tl);print(type(cmd).__name__,'accepted content changed:',changed.to_dict()!=tl.to_dict())
  except Exception as e:print(type(cmd).__name__,'rejected:',type(e).__name__,e)
case('Lock content commands beyond newly listed seven guards',lock_extended)

def ripple_domain():
 for side in ['left','right']:
  for name,sp,sn,tn,dur,sd in [('reverse',SpeedParams(reverse=True),None,None,100,100),('mixed',SpeedParams(),30,24,80,100),('step',SpeedParams(speed_points=(SpeedPoint(50,2),)),None,None,75,100)]:
   c=clip(dur=dur,source_duration=sd,speed=sp,_source_fps_num=sn,_timeline_fps_num=tn);tl=seq(c)
   cmd=RippleTrimCommand.create_left_trim('v','c',20) if side=='left' else RippleTrimCommand.create_right_trim('v','c',dur-20)
   changed,_=apply(cmd,tl);new=changed.get_clip_by_id('c')
   fs=list(range(new.timeline_duration));expected=[c.timeline_to_source_frame(i+20 if side=='left' else i) for i in fs];got=[new.timeline_to_source_frame(i) for i in fs];mism=[(i,b,a) for i,(b,a) in enumerate(zip(expected,got)) if b!=a]
   print(side,name,'source start/dur:',new.source_start,new.source_duration,'mismatch/max:',len(mism),max([abs(b-a) for _,b,a in mism] or [0]),'examples:',mism[:4])
case('RippleTrim target source still base formula',ripple_domain)

def slice_smooth():
 for fps in [(None,None),(24,24),(30,24)]:
  for reverse in [False,True]:
   sp=SpeedParams(speed_points=(SpeedPoint(40,2,'smooth'),),reverse=reverse);dur=timeline_duration_for_source(120,sp,fps[0],1,fps[1],1);c=clip(dur=dur,source_duration=120,speed=sp,_source_fps_num=fps[0],_timeline_fps_num=fps[1]);cut=49 if dur>49 else dur//2
   changed,cmd=apply(SplitClipCommand.create('v','c',cut),seq(c));newclips=changed.video_tracks[0].clips
   before=[c.timeline_to_source_frame(i) for i in range(dur)];after=[next(k for k in newclips if k.timeline_start<=i<k.timeline_end).timeline_to_source_frame(i) for i in range(dur)];mm=[(i,b,a) for i,(b,a) in enumerate(zip(before,after)) if b!=a]
   print(fps,reverse,'cut:',cut,'mismatches/max:',len(mm),max([abs(b-a) for _,b,a in mm] or[0]),'first:',mm[:4])
 # constant integer, no fractional timeline-source phase, stepped trim:
 sp=SpeedParams(speed_points=(SpeedPoint(50,2),));c=clip(dur=75,source_duration=100,speed=sp);tl=seq(c);ns,nd=c.with_left_edge_source_values(20)
 changed,_=apply(TrimClipCommand.create_left_trim('v','c',20,ns,new_source_duration=nd),tl);legacy=trim_clip_left(tl,'c',20)
 print('step left trim original/cmd/helper frames50,60,70:',[c.timeline_to_source_frame(i) for i in [50,60,70]],[changed.get_clip_by_id('c').timeline_to_source_frame(i) for i in [50,60,70]],[legacy.get_clip_by_id('c').timeline_to_source_frame(i) for i in [50,60,70]])
case('Smooth slice non-fractional phase and step trim rebase',slice_smooth)

def overwrite_payload():
 c=clip();t=TransitionInstance(transition_id='head',track_id='v',right_clip_id='c',center_frame=0,duration=20,alignment='start_on_cut');tl=seq(c,transitions=(t,),markers=(TimelineMarker(marker_id='m',frame=75,clip_id='c',track_id='v'),))
 changed,cmd=apply(OverwriteClipCommand.create('new',0,9,40,'v'),tl);undone=cmd.undo(changed)
 print('after markers/validation:',[(m.frame,m.clip_id) for m in changed.markers],validate_timeline(changed));print('transition before/after/undo:',1,len(changed.video_tracks[0].transitions),len(undone.video_tracks[0].transitions),'undo equal:',tl.to_dict()==undone.to_dict())
 # new FPS fields not serialized in Overwrite/Append public contracts
 for command in [OverwriteClipCommand.create('new',0,29,0,'v',source_fps_num=30,source_fps_den=1,timeline_fps_num=24,timeline_fps_den=1),AppendClipCommand.create('new',0,29,'v',source_fps_num=30,source_fps_den=1,timeline_fps_num=24,timeline_fps_den=1)]:
  original,_=apply(command,seq());loaded=type(command).from_dict(json.loads(json.dumps(command.to_dict())));after,_=apply(loaded,seq())
  print(type(command).__name__,'original/JSON duration:',original.video_tracks[0].clips[0].timeline_duration,after.video_tracks[0].clips[0].timeline_duration)
case('Overwrite marker/transition payload and new source FPS JSON',overwrite_payload)

print('\nDONE')
