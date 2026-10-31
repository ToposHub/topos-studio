import time
heartbeat_times=[]
heartbeat=QTimer(mw);heartbeat.setInterval(20);heartbeat.timeout.connect(lambda:heartbeat_times.append(time.perf_counter()));heartbeat.start()
QTest.qWait(120);pre=len(heartbeat_times);t0=time.perf_counter()
normalization=tv._analyze_clip_normalize_gain(paths[0],0.0,60.0)
t1=time.perf_counter();during=len(heartbeat_times)-pre;QTest.qWait(120);heartbeat.stop()
result={'media':Path(paths[0]).name,'audio_duration_seconds':60,'analysis_elapsed_seconds':t1-t0,'heartbeat_during_analysis':during,'largest_heartbeat_gap_seconds':max((b-a for a,b in zip(heartbeat_times,heartbeat_times[1:])),default=0),'analysis':normalization,'method':'production normalize method on actual GUI thread; no decoder mock'}
