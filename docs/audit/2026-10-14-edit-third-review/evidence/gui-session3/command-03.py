timer.stop()
from src.features.edit.core.selection import TimelineSelection
# 真实控件输入；选区/锁状态由正常接口建立审计fixture。
clips=tv._timeline.video_tracks[0].clips
sel=TimelineSelection().with_clip_selected(clips[0].clip_id,clips[0].track_id,tv._timeline.timeline_id,replace=True)
tv.set_selection(sel);wait(200)
props=mw.edit_properties_panel
props.set_clips((tv._timeline.get_clip_by_id(clips[0].clip_id),));wait(150)
section=next(s for s in props._sectioned_panel.sections() if s.section_key.endswith(':transform'))
x=section.widget._inputs['translate_x'];before=timeline_state()
tv._on_track_lock_changed(clips[0].track_id,True);wait(150)
start_errors=(OUT/'uncaught-qt-exceptions.log').read_text() if (OUT/'uncaught-qt-exceptions.log').exists() else ''
QTest.mouseDClick(x,Qt.MouseButton.LeftButton);wait(100)
le=x.findChild(QLineEdit)
if le:
    le.selectAll();QTest.keyClicks(le,'77');QTest.keyClick(le,Qt.Key.Key_Return);wait(300)
record('locked_property_control',control_enabled=x.isEnabled(),value_before=0,value_display=x.value(),before=before,after=timeline_state(),new_exception=(OUT/'uncaught-qt-exceptions.log').read_text()[len(start_errors):] if (OUT/'uncaught-qt-exceptions.log').exists() else '',screenshot=shot('21-locked-property-rejection'))
tv._on_track_lock_changed(clips[0].track_id,False);wait(150)
# 整体重投影，避免把审计输入被拒后的控件值当作下一次初值。
props.set_clips((tv._timeline.get_clip_by_id(clips[0].clip_id),));wait(150)
# 多选真正输入一个公共字段，再Undo一次。
clips=tv._timeline.video_tracks[0].clips
sel=TimelineSelection().with_clip_selected(clips[0].clip_id,clips[0].track_id,tv._timeline.timeline_id,replace=True).with_clip_selected(clips[-1].clip_id,clips[-1].track_id,tv._timeline.timeline_id,replace=False)
tv.set_selection(sel);wait(180)
section=next(s for s in props._sectioned_panel.sections() if s.section_key.endswith(':transform'))
x=section.widget._inputs['translate_x']
before=[(c.clip_id,c.transform.translate_x) for c in tv._timeline.video_tracks[0].clips]
QTest.mouseDClick(x,Qt.MouseButton.LeftButton);wait(100);le=x.findChild(QLineEdit)
if le:le.selectAll();QTest.keyClicks(le,'55');QTest.keyClick(le,Qt.Key.Key_Return);wait(250)
after=[(c.clip_id,c.transform.translate_x) for c in tv._timeline.video_tracks[0].clips]
y=section.widget._inputs['translate_y'];QTest.mouseDClick(y,Qt.MouseButton.LeftButton);wait(100);le=y.findChild(QLineEdit)
if le:le.selectAll();QTest.keyClicks(le,'66');QTest.keyClick(le,Qt.Key.Key_Return);wait(250)
record('multi_continuous_xy',before=before,after_x=after,after_y=[(c.clip_id,c.transform.translate_x,c.transform.translate_y) for c in tv._timeline.video_tracks[0].clips],context=[c.clip_id for c in props._current_clips],screenshot=shot('22-multi-continuous-xy'))
key(Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier)
record('multi_edit_undo',before=before,after=after,undo=[(c.clip_id,c.transform.translate_x) for c in tv._timeline.video_tracks[0].clips],screenshot=shot('22-multi-edit-undo'))
timer.start(500)
