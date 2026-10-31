timer.stop()
# Switch back to Timeline 1 using the actual media-pool timeline tile.
model=mw.media_pool_panel.asset_model
i=next(i for i in range(model.rowCount()) if str(model.data(model.index(i,0),Qt.ItemDataRole.DisplayRole))=='Timeline 1')
idx=model.index(i,0);QTest.mouseDClick(mw.media_pool_panel.grid_view.viewport(),Qt.MouseButton.LeftButton,pos=mw.media_pool_panel.grid_view.visualRect(idx).center());wait(700)
for b in mw.findChildren(QTabBar):
    for i in range(b.count()):
        if 'Program' in b.tabText(i):QTest.mouseClick(b,Qt.MouseButton.LeftButton,pos=b.tabRect(i).center())
click(panel.btn_select);click(panel.btn_zoom_fit)
for label,at in [('prores',325),('topos',817)]:
    if sv._is_playing:sv.pause()
    QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,pos=QPoint(frame_point(at).x(),14));wait(350)
    shown=[]
    def on_ready(frame,result):
        if result is not None:shown.append(frame)
    renderer=mw.timeline_preview_renderer;renderer.frame_ready.connect(on_ready)
    start=tv.get_playhead_frame();click(ep._program_viewer._transport_bar.btn_play);wait(2000)
    record('real_codec_transport_'+label,start=start,end=tv.get_playhead_frame(),playing=panel.get_playback_controller().is_playing,ready_frames=len(shown),screenshot=shot('18-play-'+label))
    if panel.get_playback_controller().is_playing:click(ep._program_viewer._transport_bar.btn_play)
    renderer.frame_ready.disconnect(on_ready)
# Group move follows linked selection; deselect via Esc after release.
click(panel.btn_select);vc=tv._timeline.video_tracks[0].clips[-1]
before=timeline_state();p=clip_point(vc.clip_id);drag(p,p+QPoint(30,0));record('linked_move',before=before,after=timeline_state())
tv.undo();wait(100)
# Ctrl+C/V through real focused shortcuts at an empty tail.
click(panel.btn_linked_selection) if not panel.btn_linked_selection.isChecked() else None
vc=tv._timeline.video_tracks[0].clips[0];QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,pos=clip_point(vc.clip_id));wait(100)
key(Qt.Key.Key_C,Qt.KeyboardModifier.ControlModifier)
QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,pos=QPoint(frame_point(1330).x(),14));wait(150)
key(Qt.Key.Key_V,Qt.KeyboardModifier.ControlModifier)
record('copy_paste',state=timeline_state(),screenshot=shot('19-copy-paste'))
# Source-specific keyboard routing was not a valid performance test; retain actual codecs above.
record('final_codec_state',state=timeline_state())
timer.start(500)
