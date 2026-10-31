mw.switch_to_page('Edit');QTest.qWait(200)
tv.undo();QTest.qWait(250)
pp=mw.edit_properties_panel
clip=tv._timeline.video_tracks[0].clips[0];cid=clip.clip_id
mw._on_timeline_clip_selected(cid);QTest.qWait(200)
from PyQt6.QtWidgets import QDockWidget
for dock in mw.findChildren(QDockWidget):
    if dock.widget() is pp:dock.show();dock.raise_()
tp.get_playback_controller().seek(0);QTest.qWait(150)
result={'before':seq_info(tv._timeline),'keys':list(pp._keyframe_buttons),'current_frame':pp._current_frame}
click(pp._keyframe_buttons['transform.translate_x']);QTest.qWait(150)
tp.get_playback_controller().seek(25);QTest.qWait(200)
sec=next(v for k,v in pp._sections.items() if k.endswith(':transform'))
drag=sec._param_widgets['translate_x']['widget']
QTest.mouseDClick(drag,Qt.MouseButton.LeftButton);QTest.qWait(80)
QTest.keyClick(drag._edit,Qt.Key.Key_A,Qt.KeyboardModifier.ControlModifier)
QTest.keyClicks(drag._edit,'100');QTest.keyClick(drag._edit,Qt.Key.Key_Return);QTest.qWait(250)
c=tv._timeline.get_clip_by_id(cid)
result['after_edit']={'frame':pp._current_frame,'field':drag.value(),'static':c.transform.translate_x,'sample25':c.get_param_at_frame('transform.translate_x',25)}
click(pp._keyframe_buttons['transform.translate_x']);QTest.qWait(150)
c=tv._timeline.get_clip_by_id(cid)
result['after_second_key']={'static':c.transform.translate_x,'sample25':c.get_param_at_frame('transform.translate_x',25),'keys':c.animated_params['transform.translate_x'].to_dict()}
capture('006_animation')
