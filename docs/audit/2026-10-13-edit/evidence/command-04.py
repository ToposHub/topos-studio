from src.features.edit.ui.timeline_items import EditTool

def clip_point(clip_id, local_x=None, local_y=None):
    item=tv._items_map[clip_id]; r=item.boundingRect()
    return tv.mapFromScene(item.mapToScene(QPointF(r.center().x() if local_x is None else local_x,r.center().y() if local_y is None else local_y)))

def frame_point(frame, track_id=None):
    tv._configure_layout_engine()
    track_id=track_id or tv._timeline.video_tracks[0].track_id
    r=tv._layout_engine.track_rect(track_id)
    return tv.mapFromScene(QPointF(tv._layout_engine.frame_to_x(frame),r.center().y()))

def key(key, modifiers=Qt.KeyboardModifier.NoModifier):
    tv.setFocus(); QTest.keyClick(tv,key,modifiers); wait(200)

# Double-click the real media-pool item to open the source monitor.
row=mw.media_pool_panel.asset_model.row_for_asset(asset.asset_id)
idx=mw.media_pool_panel.asset_model.index(row,0)
QTest.mouseDClick(mw.media_pool_panel.grid_view.viewport(),Qt.MouseButton.LeftButton,pos=mw.media_pool_panel.grid_view.visualRect(idx).center())
wait(1200)
click(sv.toolbar.btn_mark_in)
# Source seek is setup; I/O and Clear are actual button interactions.
sv.slider.setValue(100); wait(400); click(sv.toolbar.btn_mark_out)
record('source_io', in_point=sv.in_point,out_point=sv.out_point, enabled=panel.btn_insert.isEnabled(),screenshot=shot('04-source-io'))
click(sv.toolbar.btn_clear)
record('source_clear_io',in_point=sv.in_point,out_point=sv.out_point,enabled=panel.btn_insert.isEnabled())
click(sv.toolbar.btn_mark_in);click(sv.toolbar.btn_mark_out)
record('source_single_frame',in_point=sv.in_point,out_point=sv.out_point,enabled=panel.btn_insert.isEnabled())
# Switch to Program by clicking its visible tab (coordinates relative to main window).
QTest.mouseClick(mw,Qt.MouseButton.LeftButton,pos=QPoint(690,50));wait(200)
click(panel.btn_select)
# Trim the full linked clip to a shorter editing sequence.
vc=tv._timeline.video_tracks[0].clips[0]
before=timeline_state()
start=clip_point(vc.clip_id,tv._items_map[vc.clip_id].boundingRect().right()-2)
drag(start,frame_point(300))
record('ordinary_right_trim',before=before,after=timeline_state(),screenshot=shot('05-normal-trim'))
key(Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier)
record('ordinary_trim_undo',state=timeline_state())
key(Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier|Qt.KeyboardModifier.ShiftModifier)
record('ordinary_trim_redo',state=timeline_state())
click(panel.btn_zoom_fit)
click(panel.btn_blade)
QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,pos=frame_point(150));wait(350)
record('blade_split',state=timeline_state(),screenshot=shot('06-blade-split'))
click(panel.btn_select)
# Ruler seek and actual transport keyboard.
QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,pos=QPoint(frame_point(50).x(),14));wait(500)
record('ruler_seek',state=timeline_state())
key(Qt.Key.Key_Space);wait(1200)
record('playback',frame=tv.get_playhead_frame(),playing=panel.get_playback_controller().is_playing)
key(Qt.Key.Key_Space)
# Marker opens a real dialog: press Return to accept via Qt timer.
def accept_dialog():
    for d in QApplication.topLevelWidgets():
        if isinstance(d,QDialog) and d.isVisible():
            QTest.keyClick(d,Qt.Key.Key_Return)
QTimer.singleShot(300,accept_dialog)
key(Qt.Key.Key_M)
record('marker',count=len(tv._timeline.markers),state=timeline_state())
