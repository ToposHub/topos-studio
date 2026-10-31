if tp.btn_marker.isChecked():click(tp.btn_marker)
mw._save_current_project();QTest.qWait(350)
expected={'assets':len(mw._current_edit_project.assets),'timelines':len(mw._current_timeline_sequences),'duration':tv._timeline.duration_frames,'active':tv._timeline.timeline_id}
loaded=mw._open_project_path(str(project_file));QTest.qWait(1200)
result={'open_return':loaded,'expected':expected,'actual':{'assets':len(mw._current_edit_project.assets),'timelines':len(mw._current_timeline_sequences),'duration':tv._timeline.duration_frames,'active':tv._timeline.timeline_id},'page':mw.current_page}
capture('043_reopened_project')
