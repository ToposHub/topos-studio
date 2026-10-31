from PyQt6.QtWidgets import QDialogButtonBox
create_timer=QTimer()
def setup_short_fixture():
    for d in QApplication.topLevelWidgets():
        if type(d).__name__=='NewTimelineDialog' and d.isVisible():
            create_timer.stop()
            d.name_edit.selectAll();QTest.keyClicks(d.name_edit,'Round4 Timeline')
            d.fps_combo.setCurrentText('25')
            click(d._btn_box.button(QDialogButtonBox.StandardButton.Ok))
create_timer.timeout.connect(setup_short_fixture);create_timer.start(200)
mw._on_timeline_create_requested();create_timer.stop();wait(400)
short_id=tv._timeline.timeline_id
select_tab('Source')
for first,last in [(0,49),(50,99),(100,149)]:
    sv.seek(first);click(sv.toolbar.btn_mark_in);sv.seek(last);click(sv.toolbar.btn_mark_out);click(panel.btn_append)
select_tab('Program');click(panel.btn_zoom_fit)
assert len(tv._timeline.video_tracks[0].clips)==3

def assert_av_aligned():
    vs=[(c.timeline_start,c.timeline_end,c.source_start,c.source_duration) for c in tv._timeline.video_tracks[0].clips]
    aus=[(c.timeline_start,c.timeline_end,c.source_start,c.source_duration) for c in tv._timeline.audio_tracks[0].clips]
    assert vs==aus,(vs,aus)
    return vs

def content_json():
    d=tv._timeline.to_dict();d.pop('revision',None);return d

click(panel.btn_roll)
prior=content_json();p=frame_point(50);drag(p,p+QPoint(24,0))
assert content_json()!=prior,'Roll did not commit'
record('linked_roll_real_drag',aligned=assert_av_aligned(),state=timeline_state(),screenshot=shot('31-roll-linked'))
key(Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier)
assert content_json()==prior,'Roll Undo not exact'
record('linked_roll_one_undo',exact=True)
# Slip and Slide are actual toolbar clicks followed by real scene mouse events.
for tool,button in [('slip',panel.btn_slip),('slide',panel.btn_slide)]:
    click(button);prior=content_json();middle=tv._timeline.video_tracks[0].clips[1]
    p=clip_point(middle.clip_id);drag(p,p+QPoint(32,0))
    assert content_json()!=prior,tool+' did not commit'
    record('linked_'+tool+'_real_drag',aligned=assert_av_aligned(),state=timeline_state(),screenshot=shot('32-'+tool+'-linked'))
    key(Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier)
    assert content_json()==prior,tool+' Undo not exact'
    record('linked_'+tool+'_one_undo',exact=True)
# Cancel a live linked drag then send the late release.
click(panel.btn_slip);prior=content_json();middle=tv._timeline.video_tracks[0].clips[1]
p=clip_point(middle.clip_id);end=p+QPoint(25,0);drag(p,end,release=False)
key(Qt.Key.Key_Escape);QTest.mouseRelease(tv.viewport(),Qt.MouseButton.LeftButton,pos=end);wait(200)
assert content_json()==prior
record('linked_slip_escape_late_release',exact=True)
