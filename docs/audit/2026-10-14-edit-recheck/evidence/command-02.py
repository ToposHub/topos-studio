timer.stop()
# Geometry/focus diagnosis before testing Roll on visible seams.
click(panel.btn_select);click(panel.btn_zoom_fit)
record('geometry',viewport=[tv.viewport().width(),tv.viewport().height()],video_points=[(c.clip_id,[clip_point(c.clip_id).x(),clip_point(c.clip_id).y()]) for c in tv._timeline.video_tracks[0].clips],viewer_attrs=[(n,type(getattr(sv,n)).__name__) for n in ['viewer','_viewer','source_viewer'] if hasattr(sv,n)],scene_focus=type(tv.scene().focusItem()).__name__ if tv.scene().focusItem() else None)
# Use actual clipped point inside rightmost video to trim its end to frame350.
right=tv._timeline.video_tracks[0].clips[-1]
p=clip_point(right.clip_id,tv._items_map[right.clip_id].boundingRect().right()-3)
record('trim_coordinates',start=[p.x(),p.y()],start_visible=tv.viewport().rect().contains(p),target=[frame_point(350).x(),frame_point(350).y()])
before=timeline_state();drag(p,frame_point(350));record('right_trim_recheck',before=before,after=timeline_state(),interaction=str(tv._interaction.state),screenshot=shot('06-right-trim'))
click(panel.btn_zoom_fit)
# Roll seam, drag without release then Escape. Fixture cut is first video's end.
cut=tv._timeline.video_tracks[0].clips[0].timeline_end
click(panel.btn_roll);p=frame_point(cut)
before=timeline_state();drag(p,p+QPoint(30,0),release=False)
record('roll_preview',interaction=str(tv._interaction.state),roll_preview=bool(tv._roll_preview_state),point=[p.x(),p.y()],visible=tv.viewport().rect().contains(p))
key(Qt.Key.Key_Escape);QTest.mouseRelease(tv.viewport(),Qt.MouseButton.LeftButton,pos=p+QPoint(30,0));wait(250)
record('roll_escape_release',before=before,after=timeline_state(),roll_preview=bool(tv._roll_preview_state),screenshot=shot('07-roll-esc-release'))
# Undo if this changed the model; the original Roll Undo issue was fixed.
if timeline_state()['tracks']!=before['tracks']:tv.undo();wait(100);record('roll_undo',state=timeline_state())
click(panel.btn_select)
# Plain move Escape is now expected to cancel correctly.
right=tv._timeline.video_tracks[0].clips[-1];p=clip_point(right.clip_id)
before=timeline_state();drag(p,p+QPoint(35,0),release=False);key(Qt.Key.Key_Escape);QTest.mouseRelease(tv.viewport(),Qt.MouseButton.LeftButton,pos=p+QPoint(35,0));wait(150)
record('move_escape_release',before=before,after=timeline_state())
# Hand tool must pan only.
click(panel.btn_hand);right=tv._timeline.video_tracks[0].clips[-1];p=clip_point(right.clip_id)
before=timeline_state();scroll=tv.horizontalScrollBar().value();drag(p,p-QPoint(45,0));record('hand_tool',before=before,after=timeline_state(),scroll_before=scroll,scroll_after=tv.horizontalScrollBar().value())
click(panel.btn_select);click(panel.btn_zoom_fit)
# Actual multi-selection points on video fragments; open Properties tab.
left,right=tv._timeline.video_tracks[0].clips[:2]
click(panel.btn_linked_selection) if panel.btn_linked_selection.isChecked() else None
QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,pos=clip_point(left.clip_id));wait(100)
QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.ShiftModifier,pos=clip_point(right.clip_id));wait(200)
select_tab('Properties')
props=mw.edit_properties_panel
record('multi_properties_inventory',selected=list(tv._selection.selected_clip_ids),context=[c.clip_id for c in props._current_clips],controls=[(type(w).__name__,w.objectName(),getattr(w,'value',lambda:None)(),w.isVisible(),w.toolTip()) for w in props.findChildren(__import__('PyQt6.QtWidgets',fromlist=['QDoubleSpinBox']).QDoubleSpinBox)],screenshot=shot('08-multi-properties'))
timer.start(500)
