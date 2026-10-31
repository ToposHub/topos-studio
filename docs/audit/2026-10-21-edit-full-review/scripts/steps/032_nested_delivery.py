mw.switch_to_page('Edit');QTest.qWait(300)
clip=tv._timeline.video_tracks[0].clips[0]
ids=[clip.clip_id]+([clip.linked_clip_id] if clip.linked_clip_id else [])
mw._on_create_nested_from_selection(ids);QTest.qWait(300)
nested_asset=next((a for a in mw._current_edit_project.assets.values() if a.is_nested_sequence),None)
result={'child':nested_asset.nested_timeline_id if nested_asset else None,'parent_duration':tv._timeline.duration_frames}
mw.switch_to_page('Deliver');QTest.qWait(400)
ws=mw._deliver_workspace();dp=ws.settings_panel
result['resolver_types']={k:type(v).__name__ for k,v in dp._timeline_sequences.items()}
result['has_workspace_set_timeline_sequences']=hasattr(ws,'set_timeline_sequences')
dp._filename_edit.setText('audit_gui_nested');dp._range_combo.setCurrentIndex(0)
QTest.mouseClick(dp._add_and_start_btn,Qt.MouseButton.LeftButton);QTest.qWait(800)
result['jobs']=[{'name':j.name,'status':j.status,'error':j.error,'path':j.output_path} for j in ws._controller.queue.jobs]
capture('032_nested_delivery')
