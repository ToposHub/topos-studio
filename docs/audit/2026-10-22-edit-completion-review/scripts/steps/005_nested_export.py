mw.switch_to_page('Edit');QTest.qWait(200)
clip=tv._timeline.video_tracks[0].clips[0]
ids=[clip.clip_id]+([clip.linked_clip_id] if clip.linked_clip_id else [])
mw._on_create_nested_from_selection(ids);QTest.qWait(300)
result={'parent':seq_info(tv._timeline),'children':[seq_info(t) for k,t in mw._current_timeline_sequences.items() if k!=base_id]}
mw.switch_to_page('Deliver');QTest.qWait(250)
dp._filename_edit.setText('round7_nested');dp._range_combo.setCurrentIndex(0)
jobs,pf=dp.build_jobs(preflight=True)
result['preflight']=str(pf);result['jobs_count']=len(jobs)
QTest.mouseClick(dp._add_and_start_btn,Qt.MouseButton.LeftButton);QTest.qWait(300)
result['queue']=[{'status':str(j.status),'error':j.error,'path':j.output_path} for j in ws._controller.queue.jobs]
capture('005_nested_export')
