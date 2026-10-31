timer.stop()
# Exact primary points: exclude ClipItem painting margins and inspect Qt hit testing.
select_tab('Program');click(panel.btn_trim)
right=tv._timeline.video_tracks[0].clips[-1];item=tv._items_map[right.clip_id]
p=clip_point(right.clip_id,item._width-4,min(30,item._track_height/2))
local=item.mapFromScene(tv.mapToScene(p));hit=tv.itemAt(p)
record('hit_diagnosis',point=[p.x(),p.y()],local=[local.x(),local.y()],shape_contains=item.shape().contains(local),item_at=type(hit).__name__,item_at_id=getattr(hit,'_clip_id',None),locked=item._is_track_locked,tool=str(item._active_tool),map_scene=[item.pos().x(),item.pos().y()])
before=timeline_state();drag(p,frame_point(400));record('precise_right_trim',before=before,after=timeline_state(),screenshot=shot('14-precise-trim'))
click(panel.btn_select)
# Two-clip selection is fixture setup through the existing selection model/service.
sel=TimelineSelection()
for i,c in enumerate(tv._timeline.video_tracks[0].clips[:2]):sel=sel.with_clip_selected(c.clip_id,c.track_id,tv._timeline.timeline_id,replace=(i==0))
tv.set_selection(sel);wait(250);select_tab('Properties');wait(150)
props=mw.edit_properties_panel
record('multi_selected_fixture',selected=list(tv._selection.selected_clip_ids),context=[c.clip_id for c in props._current_clips])
sec=next(s for k,s in props._sections.items() if k.endswith(':transform'))
w=sec._param_widgets['translate_x']['widget']
before=[(c.clip_id,c.transform.translate_x) for c in tv._timeline.video_tracks[0].clips]
QTest.mouseDClick(w,Qt.MouseButton.LeftButton);wait(50);ed=QApplication.focusWidget();ed.selectAll();QTest.keyClicks(ed,'55');QTest.keyClick(ed,Qt.Key.Key_Return);wait(300)
record('multi_edit_real_control',context=[c.clip_id for c in props._current_clips],before=before,after=[(c.clip_id,c.transform.translate_x) for c in tv._timeline.video_tracks[0].clips],screenshot=shot('15-two-selected-one-edit'))
# Lock current video track using the same command as its header. Numeric edit still must reject.
tv._on_track_lock_changed(tv._timeline.video_tracks[0].track_id,True);wait(150)
props.set_clips([tv._timeline.video_tracks[0].clips[0]])
sec=next(s for k,s in props._sections.items() if k.endswith(':transform'));w=sec._param_widgets['translate_x']['widget']
before=[(c.clip_id,c.transform.translate_x) for c in tv._timeline.video_tracks[0].clips]
QTest.mouseDClick(w,Qt.MouseButton.LeftButton);wait(50);ed=QApplication.focusWidget();ed.selectAll();QTest.keyClicks(ed,'77');QTest.keyClick(ed,Qt.Key.Key_Return);wait(250)
record('locked_property_real_control',locked=tv._timeline.video_tracks[0].is_locked,before=before,after=[(c.clip_id,c.transform.translate_x) for c in tv._timeline.video_tracks[0].clips],screenshot=shot('16-locked-property'))
tv._on_track_lock_changed(tv._timeline.video_tracks[0].track_id,False);wait(100)
timer.start(500)
