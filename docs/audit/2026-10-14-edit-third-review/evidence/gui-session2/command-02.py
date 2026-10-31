timer.stop()
# NewTimelineDialog is real UI; controller entry only opens the dialog.
def configure_new():
    for d in QApplication.topLevelWidgets():
        if type(d).__name__=='NewTimelineDialog' and d.isVisible():
            d.name_edit.selectAll();QTest.keyClicks(d.name_edit,'Recheck Source AV')
            buttons=[b for b in d.findChildren(QPushButton) if any(t in b.text().lower() for t in ['create','ok','创建','确定'])]
            if buttons:click(buttons[0])
            else:QTest.keyClick(d,Qt.Key.Key_Return)
create_timer=QTimer();create_timer.timeout.connect(configure_new);create_timer.start(300);mw._on_timeline_create_requested();create_timer.stop();wait(450)
select_tab('Source')
sv.seek(0);click(sv.toolbar.btn_mark_in);sv.seek(49);click(sv.toolbar.btn_mark_out)
click(panel.btn_append);record('source_append_empty',state=timeline_state())
sv.seek(50);click(sv.toolbar.btn_mark_in);sv.seek(99);click(sv.toolbar.btn_mark_out);click(panel.btn_append)
record('source_append_second',state=timeline_state(),screenshot=shot('17-source-appended-av'))
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
