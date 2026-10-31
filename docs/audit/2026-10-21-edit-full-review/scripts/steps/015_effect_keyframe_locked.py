result={}
cid=clipitem.clip_id; clip=tv._timeline.get_clip_by_id(cid)
result['ordinary_transform_animation_buttons']=list(k for k in pp._keyframe_buttons if not k.startswith('effect:'))
keys=list(pp._keyframe_buttons)
if keys:
 key=keys[0]; btn=pp._keyframe_buttons[key]
 tp.set_playhead_frame(10);QTest.qWait(150);click(btn)
 clip=tv._timeline.get_clip_by_id(cid)
 result['keyframe_added']=[e.to_dict() for e in clip.effects]
 # Lock via existing track header signal handler; then click the actual effect ◆.
 tv._on_track_lock_changed(clip.track_id,True);QTest.qWait(200)
 before_revision=tv._timeline.revision
 tp.set_playhead_frame(20);QTest.qWait(150)
 btn=pp._keyframe_buttons[key]
 result['locked_button_enabled']=btn.isEnabled();click(btn)
 result['locked_effect_animation']={'revision_before':before_revision,'revision_after':tv._timeline.revision,'effects':[e.to_dict() for e in tv._timeline.get_clip_by_id(cid).effects]}
 tv._on_track_lock_changed(clip.track_id,False);QTest.qWait(200)
capture('015_effect_keyframe_locked')
