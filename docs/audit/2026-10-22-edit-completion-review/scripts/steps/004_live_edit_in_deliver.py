from src.features.edit.core.selection import TimelineSelection,SelectionItem,SelectionType
result={'before':{'live':seq_info(tv._timeline),'draft':seq_info(dp._sequence),'preflight':seq_info(dp._sequence_for_combo_selection()),'controller':seq_info(ws._controller._sequences_by_id.get(base_id))},'queue_before':[{'status':str(j.status),'error':j.error,'path':j.output_path} for j in ws._controller.queue.jobs]}
tv.set_playhead(50);QTest.qWait(100)
c=tv._timeline.video_tracks[0].clips[0]
item=SelectionItem(item_type=SelectionType.CLIP,item_id=c.clip_id,track_id=c.track_id,timeline_id=base_id)
tv.set_selection(TimelineSelection(items=(item,),primary_item=item))
tv.setFocus();QTest.keyClick(tv,Qt.Key.Key_B,Qt.KeyboardModifier.ControlModifier);QTest.qWait(200)
result['after_shortcut']=seq_info(tv._timeline)
# Use the production split action when shortcut preset has no binding.
if len(tv._timeline.video_tracks[0].clips)==1:
    mw.on_timeline_split();QTest.qWait(200)
result['split']=seq_info(tv._timeline)
if len(tv._timeline.video_tracks[0].clips)>1:
    c=tv._timeline.video_tracks[0].clips[-1]
    ids=[c.clip_id]+([c.linked_clip_id] if c.linked_clip_id else [])
    items=tuple(SelectionItem(item_type=SelectionType.CLIP,item_id=i,track_id=tv._timeline.get_clip_by_id(i).track_id,timeline_id=base_id) for i in ids)
    tv.set_selection(TimelineSelection(items=items,primary_item=items[0]));tv.setFocus()
    QTest.keyClick(tv,Qt.Key.Key_Delete);QTest.qWait(300)
result['after']={'live':seq_info(tv._timeline),'draft':seq_info(dp._sequence),'preflight':seq_info(dp._sequence_for_combo_selection()),'controller':seq_info(ws._controller._sequences_by_id.get(base_id))}
dp._range_combo.setCurrentIndex(0);dp._filename_edit.setText('round7_after_edit')
QTest.mouseClick(dp._add_and_start_btn,Qt.MouseButton.LeftButton);QTest.qWait(200)
result['jobs']=[{'status':str(j.status),'error':j.error,'path':j.output_path} for j in ws._controller.queue.jobs]
capture('004_live_deliver_edit')
