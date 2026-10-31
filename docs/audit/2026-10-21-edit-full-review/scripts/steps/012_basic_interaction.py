result={}
sv.set_in_point(50);sv.set_out_point(149)
# Insert the selected source range in the middle, then undo/redo using real keys.
tp.set_playhead_frame(50); click(tp.btn_insert); result['insert']=summary()
QApplication.setActiveWindow(mw); tv.setFocus(); QTest.keyClick(tv,Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier); QTest.qWait(300); result['undo_insert']=summary()
QTest.keyClick(tv,Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier|Qt.KeyboardModifier.ShiftModifier); QTest.qWait(300); result['redo_insert']=summary()
tp.set_playhead_frame(75); click(tp.btn_overwrite); result['overwrite']=summary()
click(tp.btn_append); result['append_second']=summary()
click(sv.toolbar.btn_clear); result['clear_source_buttons']=[tp.btn_insert.isEnabled(),tp.btn_overwrite.isEnabled(),tp.btn_append.isEnabled()]
click(tp.btn_zoom_fit); QTest.qWait(400)
mw.dock_program_viewer.show(); mw.dock_program_viewer.raise_()
# Split linked A/V through the timeline's keyboard route.
tp.set_playhead_frame(225); tv.setFocus(); QTest.keyClick(tv,Qt.Key.Key_B,Qt.KeyboardModifier.ControlModifier); QTest.qWait(400); result['split']=summary()
QTest.keyClick(tv,Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier); QTest.qWait(250); result['undo_split']=summary()
QTest.keyClick(tv,Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier|Qt.KeyboardModifier.ShiftModifier); QTest.qWait(250); result['redo_split']=summary()
# Actual playback control, with observation of real rendered frames.
frames=[]
def observe_frame(*a): frames.append((__import__('time').perf_counter(),str(a[0]),a[1] is not None if len(a)>1 else None))
if mw.timeline_preview_renderer is not None: mw.timeline_preview_renderer.frame_ready.connect(observe_frame)
tp.set_playhead_frame(0); click(mw.program_viewer_widget.get_transport_bar().btn_play); QTest.qWait(2200); f1=tp.get_current_frame(); click(mw.program_viewer_widget.get_transport_bar().btn_play)
result['playback']={'frame':f1,'render_emissions':len(frames),'non_none':sum(x[2] is True for x in frames),'events':frames[:3]+frames[-3:]}
capture('011_editing_program')
