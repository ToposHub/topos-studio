import json, sys
from dataclasses import replace
sys.path.insert(0, '/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01')
from src.features.edit.core.timeline_v2 import TimelineSequence, EditTrack, ClipInstance, TransitionInstance
from src.features.edit.core.clip_params import TransformParams
from src.features.edit.core.commands import AddKeyframeCommand, MoveClipToTrackCommand, MoveClipToExistingTrackCommand, CommandStack

results = {}
base_clip = ClipInstance(clip_id='c', asset_id='a', track_id='v1', timeline_start=100, timeline_duration=100, source_start=0, source_duration=100)
base = TimelineSequence(video_tracks=(EditTrack(track_id='v1', name='V1', track_type='video', index=0, clips=(base_clip,)), EditTrack(track_id='v2', name='V2', track_type='video', index=1)))
for param, value in [('transform.translate_x', -250.0), ('transform.translate_y', 500.0), ('transform.rotation', -45.0), ('transform.rotation', 180.0), ('transform.scale_x', 0.0)]:
    cmd = AddKeyframeCommand.create('v1', 'c', param, 100, value)
    seq = cmd.apply(base)
    clip = seq.get_clip_by_id('c')
    results[f'keyframe_range_{param}_{value}'] = {'requested': value, 'evaluated': clip.get_param_at_frame(param,100), 'range': [clip.animated_params[param].min_value, clip.animated_params[param].max_value]}
keyed = base_clip.with_keyframe_added('opacity',100,0.0).with_keyframe_added('opacity',199,1.0)
keybase = base.with_clip_updated(keyed)
for label, cmd in [('new_track', MoveClipToTrackCommand.create_for_new_track('v1','c','video','above',300,100)), ('existing_track_legacy', MoveClipToExistingTrackCommand.create('v1','v2','c',300,100))]:
    after = cmd.apply(keybase)
    moved = after.get_clip_by_id('c')
    results[f'keyframes_after_{label}'] = {'start':moved.timeline_start,'keyframes':[x.frame for x in moved.animated_params['opacity'].keyframes.keyframes], 'value_at_start':moved.get_param_at_frame('opacity',300), 'original_at_start':keyed.get_param_at_frame('opacity',100)}
trans = TransitionInstance(transition_id='fade', transition_type='dip_to_color', track_id='v1', left_clip_id='', right_clip_id='c', center_frame=100, duration=12, alignment='start_on_cut')
with_trans = base.with_track_updated(base.video_tracks[0]._rebuild(transitions=(trans,)))
for label, cmd in [('new_track', MoveClipToTrackCommand.create_for_new_track('v1','c','video','above',300,100)), ('existing_track_legacy', MoveClipToExistingTrackCommand.create('v1','v2','c',300,100))]:
    stack = CommandStack()
    after = stack.execute(cmd, with_trans)
    undone = stack.undo(after)
    results[f'transitions_after_{label}'] = {'before':sum(len(t.transitions) for t in with_trans.all_tracks),'after':sum(len(t.transitions) for t in after.all_tracks),'undo':sum(len(t.transitions) for t in undone.all_tracks)}
print(json.dumps(results,indent=2))
from src.features.edit.core.commands import ChangeClipSpeedCommand, FreezeFrameAtPlayheadCommand
from src.features.edit.core.timeline_v2 import TimelineMarker
from src.features.edit.core.timeline_ops import validate_timeline
from src.features.edit.core.audio_transition_catalog import AUDIO_TRANSITION_TYPE_OPTIONS
print('AUDIO TYPES', AUDIO_TRANSITION_TYPE_OPTIONS)
v = replace(base_clip, timeline_start=0, linked_clip_id='a_clip')
a = replace(v, clip_id='a_clip', track_id='a1', linked_clip_id='c')
atr = TransitionInstance(transition_id='a_fade', transition_type=AUDIO_TRANSITION_TYPE_OPTIONS[0], track_id='a1', left_clip_id='', right_clip_id='a_clip', center_frame=0,duration=80,alignment='start_on_cut')
linked = TimelineSequence(video_tracks=(EditTrack(track_id='v1', name='V1', track_type='video', index=0, clips=(v,)),),audio_tracks=(EditTrack(track_id='a1',name='A1',track_type='audio',index=0,clips=(a,),transitions=(atr,)),))
stack=CommandStack(); after=stack.execute(ChangeClipSpeedCommand.create('c','v1',2.0),linked); undone=stack.undo(after)
print('linked_speed_undo_transition', [(label,[(t.transition_id,t.duration) for tr in sq.audio_tracks for t in tr.transitions]) for label,sq in [('before',linked),('after',after),('undo',undone)]])
base0=TimelineSequence(video_tracks=(EditTrack(track_id='v1',name='V1',track_type='video',index=0,clips=(replace(base_clip,timeline_start=0),)),),markers=(TimelineMarker(marker_id='m',frame=75,clip_id='c',track_id='v1'),))
stack=CommandStack();after=stack.execute(FreezeFrameAtPlayheadCommand.create('c',50),base0)
print('freeze_scoped_marker', [{'clip':m.clip_id,'frame':m.frame} for m in after.markers],validate_timeline(after))
trtail=TransitionInstance(transition_id='tail',transition_type='dip_to_color',track_id='v1',left_clip_id='c',right_clip_id='',center_frame=100,duration=12,alignment='end_on_cut')
basetail=base0.with_track_updated(base0.video_tracks[0]._rebuild(transitions=(trtail,)))
stack=CommandStack();after=stack.execute(FreezeFrameAtPlayheadCommand.create('c',50),basetail); undone=stack.undo(after)
print('freeze_tail_transition',[(label,[t.to_dict() for tr in sq.video_tracks for t in tr.transitions]) for label,sq in [('before',basetail),('after',after),('undo',undone)]])
from src.features.edit.core.commands import RemoveKeyframeCommand
locked=base.with_track_updated(replace(base.video_tracks[0],is_locked=True))
added=AddKeyframeCommand.create('v1','c','opacity',100,0.5).apply(locked)
removed=RemoveKeyframeCommand.create('v1','c','opacity',100).apply(added)
print('locked_keyframe_writes', {'track_locked':added.video_tracks[0].is_locked,'added':added.get_clip_by_id('c').has_keyframe_at('opacity',100),'removed':not removed.get_clip_by_id('c').has_keyframe_at('opacity',100)})
