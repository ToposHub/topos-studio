timer.stop()
from PyQt6.QtWidgets import QTabBar
for b in mw.findChildren(QTabBar):
    for i in range(b.count()):
        if 'Program' in b.tabText(i):
            QTest.mouseClick(b,Qt.MouseButton.LeftButton,pos=b.tabRect(i).center())
wait(200)
# Actual Escape during an in-progress move of the right, unlinked clip.
click(panel.btn_select)
right=tv._timeline.video_tracks[0].clips[-1]
before=timeline_state(); start=clip_point(right.clip_id); end=start+QPoint(80,0)
drag(start,end,release=False)
key(Qt.Key.Key_Escape)
preview=tv._items_map[right.clip_id]._start_frame
QTest.mouseRelease(tv.viewport(),Qt.MouseButton.LeftButton,pos=end);wait(300)
record('escape_move',before=before,preview_after_esc=preview,after=timeline_state(),screenshot=shot('07-escape-commits'))
tv.undo();wait(200)
# Hand tool on a clip body must pan; observe whether it changes content.
click(panel.btn_hand)
right=tv._timeline.video_tracks[0].clips[-1]
before=timeline_state();scroll=tv.horizontalScrollBar().value()
start=clip_point(right.clip_id);drag(start,start+QPoint(75,0))
record('hand_tool',before=before,after=timeline_state(),scroll_before=scroll,scroll_after=tv.horizontalScrollBar().value())
tv.undo();wait(200)
click(panel.btn_roll)
cut=tv._timeline.video_tracks[0].clips[0].timeline_end
before=timeline_state();p=frame_point(cut)
drag(p,p-QPoint(45,0))
record('roll_negative',before=before,after=timeline_state())
try:
    tv.undo();wait(200);record('roll_negative_undo',state=timeline_state())
except Exception as ex:
    record('roll_negative_undo',error=str(ex),state=timeline_state())
# Lock is fixture setup through the same command path as the header lock toggle.
track=tv._timeline.video_tracks[0]
tv._on_track_lock_changed(track.track_id,True);wait(150)
click(panel.btn_blade)
before=timeline_state()
QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,pos=frame_point(60));wait(250)
record('locked_track_blade',before=before,after=timeline_state(),screenshot=shot('08-locked-blade'))
# Genuine horizontal wheel/pixel scroll and Ctrl+horizontal should not zoom-out.
click(panel.btn_select)
bar=tv.horizontalScrollBar();bar.setValue(min(100,bar.maximum()))
before_scroll=bar.value();before_zoom=tv._pixels_per_frame
p=QPoint(650,80)
e=QWheelEvent(QPointF(p),QPointF(tv.viewport().mapToGlobal(p)),QPoint(40,0),QPoint(120,0),Qt.MouseButton.NoButton,Qt.KeyboardModifier.NoModifier,Qt.ScrollPhase.ScrollUpdate,False)
QApplication.sendEvent(tv.viewport(),e);wait(100)
record('horizontal_scroll',before=before_scroll,after=bar.value(),maximum=bar.maximum())
e=QWheelEvent(QPointF(p),QPointF(tv.viewport().mapToGlobal(p)),QPoint(40,0),QPoint(120,0),Qt.MouseButton.NoButton,Qt.KeyboardModifier.ControlModifier,Qt.ScrollPhase.ScrollUpdate,False)
QApplication.sendEvent(tv.viewport(),e);wait(100)
record('ctrl_horizontal_scroll',zoom_before=before_zoom,zoom_after=tv._pixels_per_frame)
record('end_of_batch',project_revision=mw._current_edit_project.revision,undo=tv.can_undo(),screenshot=shot('09-batch-end'))
timer.start(500)
