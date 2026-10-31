timer.stop()
record('transport_diagnosis',tv_id=tv._timeline.timeline_id,ep_id=ep._current_timeline.timeline_id,tv_duration=tv._timeline.duration_frames,panel_total=panel._total_frames,controller_duration=panel.get_playback_controller()._timeline_duration,project_active=mw._current_edit_project.active_timeline_id,project_refs=[(k,v.name) for k,v in mw._current_edit_project.timelines.items()],cache=[(k,t.name,t.duration_frames,[len(q.clips) for q in t.video_tracks]) for k,t in mw._current_timeline_sequences.items()])
# Direct sequence selection is fixture setup to distinguish tile navigation from codec playback.
original=next(t for t in mw._current_timeline_sequences.values() if max((c.timeline_end for q in t.video_tracks for c in q.clips),default=0)>1000)
mw._on_timeline_switched(original.timeline_id);wait(500)
for label,at in [('prores',325),('topos',817)]:
    QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,pos=QPoint(frame_point(at).x(),14));wait(250)
    shown=[]
    def on_ready(frame,result):
        if result is not None:shown.append(frame)
    renderer=mw.timeline_preview_renderer;renderer.frame_ready.connect(on_ready)
    start=tv.get_playhead_frame();click(ep._program_viewer._transport_bar.btn_play);wait(1600)
    record('direct_selected_codec_'+label,start=start,end=tv.get_playhead_frame(),total=panel._total_frames,playing=panel.get_playback_controller().is_playing,ready_frames=len(shown),screenshot=shot('20-direct-'+label))
    if panel.get_playback_controller().is_playing:click(ep._program_viewer._transport_bar.btn_play)
    renderer.frame_ready.disconnect(on_ready)
timer.start(500)
