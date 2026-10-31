def tl_info(v):return {'type':type(v).__name__,'id':getattr(v,'timeline_id',None),'duration':getattr(v,'duration_frames',None),'frame_count':getattr(v,'frame_count',None),'revision':getattr(v,'revision',None)}
result={'project_active':getattr(mw._current_edit_project,'active_timeline_id',None),'view':tl_info(tv._timeline),'ep':tl_info(ep._current_timeline),'sequences':{k:tl_info(v) for k,v in mw._current_timeline_sequences.items()},'dp_seq':tl_info(dp._sequence),'dp_sequences':{k:tl_info(v) for k,v in dp._timeline_sequences.items()},'selected':dp._timeline_combo.currentData()}
# Explicitly choosing the active sequence from the visible Deliver combo is a normal UI action.
idx=dp._timeline_combo.findData(advanced_id)
dp._timeline_combo.setCurrentIndex(0 if idx!=0 else min(1,dp._timeline_combo.count()-1));dp._timeline_combo.setCurrentIndex(idx);QTest.qWait(250)
jobs,preflight=dp.build_jobs(preflight=True)
result['after_reselect']={'seq':tl_info(dp._sequence),'jobs':len(jobs),'preflight':str(preflight),'label':dp._preflight_label.text()}
# Entire timeline uses a different range guard; test whether the queue is reachable.
dp._range_combo.setCurrentIndex(0);QTest.qWait(100)
jobs,preflight=dp.build_jobs(preflight=True)
result['entire']={'jobs':[j.to_dict() for j in jobs],'preflight':str(preflight),'label':dp._preflight_label.text()}
