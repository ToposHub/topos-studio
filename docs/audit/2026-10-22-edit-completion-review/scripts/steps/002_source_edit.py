sv=mw.source_viewer_widget
mw.dock_source_viewer.show();mw.dock_source_viewer.raise_()
asset=mw._current_edit_project.get_asset_by_path(paths[0])
sv.open_asset(asset);QTest.qWait(600)
def click(w):
    QTest.mouseClick(w,Qt.MouseButton.LeftButton);QTest.qWait(150)
sv.seek(50);QTest.qWait(200);click(sv.toolbar.btn_mark_in)
sv.seek(149);QTest.qWait(200);click(sv.toolbar.btn_mark_out)
click(tp.btn_append);QTest.qWait(500)
def seq_info(t):
    return None if t is None else {'id':t.timeline_id,'name':t.name,'rev':t.revision,'frames':t.duration_frames,'tracks':[{'id':x.track_id,'clips':[{'id':c.clip_id,'start':c.timeline_start,'end':c.timeline_end} for c in x.clips]} for x in t.all_tracks]}
base_id=tv._timeline.timeline_id
result={'source':sv.current_asset.display_name,'in':sv.in_point,'out':sv.out_point,'sequence':seq_info(tv._timeline),'cached':[seq_info(t) for t in mw._current_timeline_sequences.values()]}
capture('002_source_append')
