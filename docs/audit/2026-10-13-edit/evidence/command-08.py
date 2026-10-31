timer.stop()
# Use the actual New Timeline dialog; no synthetic mutation of a live sequence.
def configure_new_timeline_dialog():
    for d in QApplication.topLevelWidgets():
        if type(d).__name__=='NewTimelineDialog' and d.isVisible():
            d.name_edit.selectAll();QTest.keyClicks(d.name_edit,'Audit Source Edits')
            buttons=[b for b in d.findChildren(QPushButton) if any(t in b.text().lower() for t in ['create','ok','创建','确定'])]
            if buttons: click(buttons[0])
            else: QTest.keyClick(d,Qt.Key.Key_Return)
QTimer.singleShot(250,configure_new_timeline_dialog)
mw._on_timeline_create_requested()
wait(600)
record('new_timeline',state=timeline_state(),screenshot=shot('13-new-timeline'))
# Source setup followed by real append buttons creates three 50-frame clips.
sv.seek(0);click(sv.toolbar.btn_mark_in);sv.seek(49);click(sv.toolbar.btn_mark_out)
click(panel.btn_append)
record('source_append_av',state=timeline_state())
sv.seek(50);click(sv.toolbar.btn_mark_in);sv.seek(99);click(sv.toolbar.btn_mark_out);click(panel.btn_append)
sv.seek(100);click(sv.toolbar.btn_mark_in);sv.seek(149);click(sv.toolbar.btn_mark_out);click(panel.btn_append)
click(panel.btn_zoom_fit);record('source_append_three',state=timeline_state(),screenshot=shot('14-source-append'))
# Source insert into an existing clip then undo: observe whether the split recovers.
sv.seek(150);click(sv.toolbar.btn_mark_in);sv.seek(159);click(sv.toolbar.btn_mark_out)
QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,pos=QPoint(frame_point(20).x(),14));wait(200)
before=timeline_state();click(panel.btn_insert)
record('source_insert_mid',before=before,after=timeline_state())
key(Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier)
record('source_insert_mid_undo',state=timeline_state(),screenshot=shot('15-insert-undo-hole'))
# Source overwrite real button at playhead 70.
QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,pos=QPoint(frame_point(70).x(),14));wait(150)
before=timeline_state();click(panel.btn_overwrite)
record('source_overwrite',before=before,after=timeline_state())
key(Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier)
record('source_overwrite_undo',state=timeline_state())
# Actual slip gesture on the last clip: sufficient head and tail handles.
click(panel.btn_slip);target=tv._timeline.video_tracks[0].clips[-1]
before=timeline_state();p=clip_point(target.clip_id);drag(p,p+QPoint(35,0))
record('slip_tool',before=before,after=timeline_state())
tv.undo();wait(100)
# RATE_STRETCH right-edge gesture, then direct undo for fixture restoration.
click(panel.btn_rate_stretch);target=tv._timeline.video_tracks[0].clips[-1]
before=timeline_state();p=clip_point(target.clip_id,tv._items_map[target.clip_id].boundingRect().right()-2);drag(p,p+QPoint(70,0))
record('rate_stretch_tool',before=before,after=timeline_state(),screenshot=shot('16-rate-stretch'))
tv.undo();wait(100)
# Zero-motion ripple edge click. Uncaught slot failure is retained by audit hook.
click(panel.btn_ripple);target=tv._timeline.video_tracks[0].clips[-1]
p=clip_point(target.clip_id,tv._items_map[target.clip_id].boundingRect().right()-2)
before=timeline_state();QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,pos=p);wait(200)
record('ripple_zero_click',before=before,after=timeline_state())
click(panel.btn_select)
timer.start(500)
