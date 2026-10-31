timer.stop()
select_tab('Program');click(panel.btn_roll);tv.setFocus();wait(100)
trace=[]
def traced_start(*args):trace.append({'event':'roll_started','args':list(args),'state':str(tv._interaction.state),'focus':type(app.focusWidget()).__name__ if app.focusWidget() else None})
def traced_finish(*args):trace.append({'event':'roll_finished','args':list(args),'state':str(tv._interaction.state)})
for h in tv._roll_handles.values():
    h.roll_trim_started.connect(traced_start);h.roll_trim_finished.connect(traced_finish)
p=frame_point(50);hit=tv.itemAt(p)
record('roll_hit',hit=type(hit).__name__,point=[p.x(),p.y()],handles=len(tv._roll_handles))
before=timeline_state();drag(p,p+QPoint(36,0))
record('roll_signal_trace',trace=trace,before=before,after=timeline_state(),state=str(tv._interaction.state),screenshot=shot('28-roll-signal-trace'))
for h in tv._roll_handles.values():
    h.roll_trim_started.disconnect(traced_start);h.roll_trim_finished.disconnect(traced_finish)
click(panel.btn_select)
# 保存真实GUI构成的两个时间线，逐字段核对载荷。
from src.features.edit.core.edit_project_io import EditProjectIO
io=EditProjectIO();seqs=dict(mw._current_timeline_sequences);before_payload={k:v.to_dict() for k,v in seqs.items()};path=OUT/'audit-scratch.topos'
saved=io.save_project(path,mw._current_edit_project,timeline_sequences=seqs,use_ai_callback=False,create_backup=False);loaded=io.load_project(path)
(OUT/'timeline_snapshot.json').write_text(json.dumps(before_payload,ensure_ascii=False,indent=2))
if loaded:
    new_payload={k:v.to_dict() for k,v in loaded[2].items()}
    (OUT/'timeline_after_reload.json').write_text(json.dumps(new_payload,ensure_ascii=False,indent=2))
    record('save_reload',saved=saved,sequence_count=len(seqs),payload_equal=before_payload==new_payload,project_revision=[mw._current_edit_project.revision,loaded[0].revision])
else:record('save_reload',saved=saved,loaded=False)
io.cleanup();timer.start(500)
