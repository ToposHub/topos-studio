actual_path=SESSION/'Round7Audit20261022.topos'
opened=mw._open_project_path(str(actual_path));QTest.qWait(600)
mw.switch_to_page('Edit');QTest.qWait(200)
result={'actual_path':str(actual_path),'exists':actual_path.exists(),'open_return':opened,'assets':len(mw._current_edit_project.assets),'timelines':len(mw._current_timeline_sequences),'active':mw._current_edit_project.active_timeline_id,'duration':mw.timeline_panel.timeline_view._timeline.duration_frames,'keyframes':{c.clip_id:{k:v.to_dict() for k,v in c.animated_params.items()} for t in mw._current_timeline_sequences.values() for track in t.video_tracks for c in track.clips if c.animated_params}}
capture('014_reopened_actual_saved')
