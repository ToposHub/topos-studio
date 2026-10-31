timer.stop()
select_tab('Source')
sv.seek(0);click(sv.toolbar.btn_mark_in);sv.seek(49);click(sv.toolbar.btn_mark_out)
click(panel.btn_append);record('source_append_existing',state=timeline_state())
sv.seek(50);click(sv.toolbar.btn_mark_in);sv.seek(99);click(sv.toolbar.btn_mark_out);click(panel.btn_append)
record('source_append_next',state=timeline_state(),screenshot=shot('17-source-appended-av'))
# Source overwrite onto occupied A/V content uses actual toolbar click.
select_tab('Program');click(panel.btn_zoom_fit);click(panel.btn_select)
QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,pos=QPoint(frame_point(20).x(),14));wait(150)
select_tab('Source');sv.seek(100);click(sv.toolbar.btn_mark_in);sv.seek(109);click(sv.toolbar.btn_mark_out)
before=timeline_state();click(panel.btn_overwrite);record('source_overwrite_occupied',before=before,after=timeline_state(),screenshot=shot('18-overwrite-failure'))
# Insert into occupied linked clips shows whether new A/V is atomic.
before=timeline_state();click(panel.btn_insert);record('source_insert_internal',before=before,after=timeline_state(),screenshot=shot('19-insert-audio-fallback'))

select_tab('Program');click(panel.btn_select);click(panel.btn_zoom_fit)
record('source_final',state=timeline_state(),project_revision=mw._current_edit_project.revision)
timer.start(500)
