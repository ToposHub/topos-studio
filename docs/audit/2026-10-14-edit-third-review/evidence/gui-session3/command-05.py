timer.stop()
from src.features.edit.core.selection import TimelineSelection
select_tab('Properties');click(panel.btn_select)
clips=tv._timeline.video_tracks[0].clips
sel=TimelineSelection().with_clip_selected(clips[0].clip_id,clips[0].track_id,tv._timeline.timeline_id,replace=True)
tv.set_selection(sel);wait(180);props=mw.edit_properties_panel
props.set_clips([tv._timeline.get_clip_by_id(clips[0].clip_id)]);wait(100)
tv._on_track_lock_changed(clips[0].track_id,True);wait(100)
sec=next(s for k,s in props._sections.items() if k.endswith(':transform'));w=sec._param_widgets['translate_x']['widget']
before=timeline_state();oldlog=(OUT/'uncaught-qt-exceptions.log').read_text() if (OUT/'uncaught-qt-exceptions.log').exists() else ''
QTest.mouseDClick(w,Qt.MouseButton.LeftButton);wait(70);ed=QApplication.focusWidget()
if isinstance(ed,QLineEdit):ed.selectAll();QTest.keyClicks(ed,'77');QTest.keyClick(ed,Qt.Key.Key_Return);wait(200)
record('locked_property_real',control_enabled=w.isEnabled(),edit_type=type(ed).__name__,before=before,after=timeline_state(),exception=(OUT/'uncaught-qt-exceptions.log').read_text()[len(oldlog):] if (OUT/'uncaught-qt-exceptions.log').exists() else '',screenshot=shot('25-lock-property-error'))
tv._on_track_lock_changed(clips[0].track_id,False);wait(100)
clips=tv._timeline.video_tracks[0].clips
sel=TimelineSelection()
for i,c in enumerate((clips[0],clips[-1])):sel=sel.with_clip_selected(c.clip_id,c.track_id,tv._timeline.timeline_id,replace=(i==0))
tv.set_selection(sel);wait(180)
sec=next(s for k,s in props._sections.items() if k.endswith(':transform'))
def numeric_field(name,value):
    w=sec._param_widgets[name]['widget'];QTest.mouseDClick(w,Qt.MouseButton.LeftButton);wait(60);ed=QApplication.focusWidget()
    if isinstance(ed,QLineEdit):ed.selectAll();QTest.keyClicks(ed,str(value));QTest.keyClick(ed,Qt.Key.Key_Return);wait(180)
def transforms():return [(c.clip_id,c.transform.translate_x,c.transform.translate_y) for c in tv._timeline.video_tracks[0].clips]
before=transforms();numeric_field('translate_x',55);after_x=transforms();numeric_field('translate_y',66);after_y=transforms()
record('multi_xy_real',before=before,after_x=after_x,after_y=after_y,context=[c.clip_id for c in props._current_clips],selected=list(tv._selection.selected_clip_ids),screenshot=shot('26-multi-xy-overwrite'))
key(Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier);record('multi_xy_undo_once',state=transforms())
timer.start(500)
