timer.stop()
# Slide and zoom are exercised in the visible app with actual events.
click(panel.btn_slide)
target=tv._timeline.video_tracks[0].clips[1]
before=timeline_state();p=clip_point(target.clip_id);drag(p,p+QPoint(18,0));record('slide_tool',before=before,after=timeline_state())
try:tv.undo();wait(100)
except Exception as ex:record('slide_undo_exception',error=str(ex))
click(panel.btn_zoom);zoom=tv._pixels_per_frame;QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,pos=frame_point(100));wait(150)
record('zoom_tool',before=zoom,after=tv._pixels_per_frame)
click(panel.btn_zoom_fit);record('zoom_fit',zoom=tv._pixels_per_frame,viewport_width=tv.viewport().width())
click(panel.btn_select)
# Copy/paste produced a true model change earlier. Test delete and its undo on an unlinked video fragment.
target=tv._timeline.video_tracks[0].clips[1]
QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,pos=clip_point(target.clip_id));wait(100)
before=timeline_state();key(Qt.Key.Key_Delete);record('delete_key',before=before,after=timeline_state())
key(Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier);record('delete_key_undo',state=timeline_state())
# Both snapshots were kept. Keep final screenshot before stopping the scratch app.
record('audit_completed',screenshot=shot('21-final-window'),state=timeline_state())
timer.start(500)
