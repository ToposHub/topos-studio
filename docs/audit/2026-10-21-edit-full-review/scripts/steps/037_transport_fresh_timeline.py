ep.switch_timeline(parent_id);QTest.qWait(400);click(tp.btn_select);click(tp.btn_zoom_fit)
mw.dock_program_viewer.raise_();bar=mw.program_viewer_widget.get_transport_bar()
p=tv.mapFromScene(QPointF(tv._layout_engine.frame_to_x(25),12));QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,p);QTest.qWait(200)
result={'timeline':tl_info(tv._timeline),'ruler':{'view':tv._playhead_frame,'controller':tp.get_current_frame(),'page':ep._current_frame}}
click(bar.btn_next_frame);QTest.qWait(450);result['next']={'view':tv._playhead_frame,'controller':tp.get_current_frame(),'page':ep._current_frame}
click(bar.btn_prev_frame);QTest.qWait(350);result['prev']={'view':tv._playhead_frame,'controller':tp.get_current_frame(),'page':ep._current_frame}
click(bar.btn_jump_start);QTest.qWait(350);result['home']={'view':tv._playhead_frame,'controller':tp.get_current_frame(),'page':ep._current_frame}
# Marker panel is a QDockWidget created lazily; observe both request and visibility.
if tp.btn_marker.isChecked():click(tp.btn_marker)
click(tp.btn_marker);QTest.qWait(400)
result['marker_panel']={'checked':tp.btn_marker.isChecked(),'visible':tp._marker_list_dock.isVisible(),'hidden':tp._marker_list_dock.isHidden(),'floating':tp._marker_list_dock.isFloating(),'parent':type(tp._marker_list_dock.parent()).__name__}
capture('037_transport_and_marker')
