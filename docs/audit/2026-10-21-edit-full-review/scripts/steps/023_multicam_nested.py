result={}
# Create multicam through the Media Pool's production request signal.
tv.set_playhead(0);QTest.qWait(150)
a1=mw._current_edit_project.get_asset_by_path(paths[0]);a2=mw._current_edit_project.get_asset_by_path(paths[2])
before=summary();mp.create_multicam_requested.emit([a1.asset_id,a2.asset_id]);QTest.qWait(450)
result['multicam_create']={'before':before,'after':summary()}
tv.set_playhead(40);QTest.qWait(150);QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,tv.mapFromScene(QPointF(tv._layout_engine.frame_to_x(40),12)));tv.setFocus()
QTest.keyClick(tv,Qt.Key.Key_2);QTest.qWait(350)
result['multicam_angle']={'timeline':summary(),'assets':[(c.asset_id,c.source_start,c.timeline_start,c.timeline_end) for c in tv._timeline.video_tracks[0].clips]}
tv.undo();QTest.qWait(200);result['multicam_angle_undo']=summary()
tv.undo();QTest.qWait(250);result['multicam_create_undo']=summary()
# Select a linked pair with the normal selection representation and invoke the existing context-menu dispatcher.
clip=tv._timeline.video_tracks[0].clips[0]
ids=[clip.clip_id]+([clip.linked_clip_id] if clip.linked_clip_id else [])
items=tuple(SelectionItem(item_type=SelectionType.CLIP,item_id=i,track_id=tv._timeline.get_clip_by_id(i).track_id,timeline_id=tv._timeline.timeline_id) for i in ids)
tv.set_selection(TimelineSelection(items=items,primary_item=items[0]));QTest.qWait(100)
before_ids=set(mw._current_timeline_sequences)
tv._on_create_nested_menu_requested();QTest.qWait(400)
result['nested_create']={'child_ids':list(set(mw._current_timeline_sequences)-before_ids),'timeline':summary()}
nested=next((c for t in tv._timeline.video_tracks for c in t.clips if mw._current_edit_project.assets[c.asset_id].is_nested_sequence),None)
if nested:
 tv._on_enter_nested_clip_requested(nested.clip_id,nested.track_id);QTest.qWait(300)
 result['nested_enter']={'active':tv._timeline.timeline_id,'clips':summary()}
 ep.switch_timeline(advanced_id);QTest.qWait(250)
 tv.undo();QTest.qWait(250);result['nested_undo']={'timeline':summary(),'children':list(mw._current_timeline_sequences)}
capture('023_multicam_nested')
