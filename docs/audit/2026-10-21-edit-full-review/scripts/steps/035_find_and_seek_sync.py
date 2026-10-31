mw.switch_to_page('Edit');QTest.qWait(300)
ep.switch_timeline(advanced_id);QTest.qWait(200)
cp=mw.caption_workspace_panel;mw.dock_caption_workspace.show();mw.dock_caption_workspace.raise_()
# Place at frame250 by the same ruler click a user performs.
p=tv.mapFromScene(QPointF(tv._layout_engine.frame_to_x(250),12));QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,p);QTest.qWait(200)
result={'before':{'view':tv._playhead_frame,'controller':tp.get_current_frame(),'page':ep._current_frame},'cues':[(c.clip_id,c.text,c.timeline_start) for t in tv._timeline.subtitle_tracks for c in t.subtitle_clips]}
QTest.mouseClick(cp._find_edit,Qt.MouseButton.LeftButton);QTest.keyClick(cp._find_edit,Qt.Key.Key_A,Qt.KeyboardModifier.ControlModifier);QTest.keyClicks(cp._find_edit,'Hi');click(cp._find_btn);QTest.qWait(700)
result['after_find']={'view':tv._playhead_frame,'controller':tp.get_current_frame(),'page':ep._current_frame,'selected':list(cp._selected_clip_ids()),'status':cp._status_label.text()}
# Transport Home/step are ordinary UI controls too.
mw.dock_program_viewer.raise_();bar=mw.program_viewer_widget.get_transport_bar()
click(bar.btn_jump_start);result['home']={'view':tv._playhead_frame,'controller':tp.get_current_frame(),'page':ep._current_frame}
click(bar.btn_next_frame);result['next']={'view':tv._playhead_frame,'controller':tp.get_current_frame(),'page':ep._current_frame}
capture('034_seek_disagreement')
