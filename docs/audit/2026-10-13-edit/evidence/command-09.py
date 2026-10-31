timer.stop()
from src.features.edit.core.edit_project_io import EditProjectIO
io=EditProjectIO()
seqs=dict(mw._current_timeline_sequences)
path=OUT/'audit-scratch.topos'
before={tid:t.to_dict() for tid,t in seqs.items()}
saved=io.save_project(path,mw._current_edit_project,timeline_sequences=seqs,use_ai_callback=False,create_backup=False)
loaded=io.load_project(path)
if loaded:
    loaded_project,loaded_ui,loaded_sequences=loaded
    after={tid:t.to_dict() for tid,t in (loaded_sequences or {}).items()}
    record('save_reload',saved=saved,timeline_count=len(seqs),loaded_count=len(loaded_sequences or {}),timeline_payload_equal=before==after,asset_count=len(loaded_project.assets),path=str(path))
else: record('save_reload',saved=saved,loaded=False)
(OUT/'timeline_snapshot.json').write_text(json.dumps(before,ensure_ascii=False,indent=2),encoding='utf-8')
record('transport_snapshot',controller=vars(panel.get_playback_controller()),range_in=getattr(tv._timeline,'range_in',None),range_out=getattr(tv._timeline,'range_out',None))
for b in mw.findChildren(QTabBar):
    for i in range(b.count()):
        if 'Program' in b.tabText(i): QTest.mouseClick(b,Qt.MouseButton.LeftButton,pos=b.tabRect(i).center())
wait(120)
QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,pos=QPoint(frame_point(10).x(),14));wait(200)
if panel.get_playback_controller().is_playing: click(ep._program_viewer._transport_bar.btn_play)
start=tv.get_playhead_frame();shown=[]
def on_ready(frame,result):
    if result is not None: shown.append(frame)
mw.timeline_preview_renderer.frame_ready.connect(on_ready)
click(ep._program_viewer._transport_bar.btn_play);wait(2200)
record('program_button_playback',start=start,end=tv.get_playhead_frame(),playing=panel.get_playback_controller().is_playing,ready_frames=len(shown),screenshot=shot('17-program-playback'))
if panel.get_playback_controller().is_playing: click(ep._program_viewer._transport_bar.btn_play)
mw.timeline_preview_renderer.frame_ready.disconnect(on_ready)
record('final_snapshot',state=timeline_state(),ui_state=[mw.width(),mw.height()],timeline_revision=tv._timeline.revision,project_revision=mw._current_edit_project.revision)
io.cleanup()
timer.start(500)
