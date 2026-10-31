# Exercise the real Settings dialog, using a delayed Qt input callback.
from PyQt6.QtWidgets import QDialogButtonBox
settings_timer=QTimer()
def change_settings():
    for d in QApplication.topLevelWidgets():
        if type(d).__name__=='NewTimelineDialog' and d.isVisible():
            settings_timer.stop()
            d.fps_combo.setCurrentIndex(d.fps_combo.findData(50.0))
            shot('40-settings-dialog',d)
            QTest.mouseClick(d._btn_box.button(QDialogButtonBox.StandardButton.Ok),Qt.MouseButton.LeftButton)
settings_timer.timeout.connect(change_settings);settings_timer.start(1200)
settings_before=content_json();mw._on_timeline_settings_requested(short_id);settings_timer.stop();wait(300)
assert tv._timeline.fps.to_float()==50
assert [(c.timeline_start,c.timeline_end) for c in tv._timeline.video_tracks[0].clips]==[(0,100),(100,200),(200,300)]
select_tab('Properties');real_numeric('translate_x',99)
key(Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier)
assert tv._timeline.fps.to_float()==50
key(Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier)
assert content_json()==settings_before
record('settings_edit_undo_order',fps_after_undo=str(tv._timeline.fps),exact=True,screenshot=shot('41-settings-undo'))
# Switch via the same production controller used by the timeline selector.
other_id=next(tid for tid in mw._current_timeline_sequences if tid!=short_id)
short_snapshot=content_json();short_full_snapshot=tv._timeline.to_dict();other_snapshot=mw._current_timeline_sequences[other_id].to_dict()
mw._on_timeline_switched(other_id);wait(200)
key(Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier)
assert mw._current_timeline_sequences[short_id].to_dict()==short_full_snapshot
mw._on_timeline_switched(short_id);wait(200)
assert content_json()==short_snapshot
record('timeline_undo_isolation',exact=True)
# Selection is fixture setup; execute the actual context action handler.
from src.features.edit.core.selection import TimelineSelection
click(panel.btn_select);select_tab('Program');click(panel.btn_zoom_fit)
video=tv._timeline.video_tracks[0].clips[0]
audio=tv._timeline.get_clip_by_id(video.linked_clip_id)
sel=TimelineSelection()
for i,c in enumerate((video,audio)):
    sel=sel.with_clip_selected(c.clip_id,c.track_id,tv._timeline.timeline_id,replace=i==0)
tv.set_selection(sel);wait(150)
counts_before=(len(mw._current_edit_project.assets),len(mw._current_edit_project.timelines),len(mw._current_timeline_sequences))
prior=content_json();tv._on_create_nested_menu_requested();wait(500)
counts_after=(len(mw._current_edit_project.assets),len(mw._current_edit_project.timelines),len(mw._current_timeline_sequences))
assert all(y==x+1 for x,y in zip(counts_before,counts_after)),(counts_before,counts_after)
after=content_json();child_ids=set(mw._current_timeline_sequences)-{other_id,short_id}
key(Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier)
assert content_json()==prior
assert (len(mw._current_edit_project.assets),len(mw._current_edit_project.timelines),len(mw._current_timeline_sequences))==counts_before
key(Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier|Qt.KeyboardModifier.ShiftModifier)
assert content_json()==after
assert child_ids.issubset(mw._current_timeline_sequences)
record('nest_context_action_atomic_history',before=counts_before,after=counts_after,exact=True,screenshot=shot('42-nest-redo'))
from src.features.edit.core.timeline_ops import validate_timeline
assert all(not validate_timeline(seq) for seq in mw._current_timeline_sequences.values())
from src.features.edit.core.edit_project_io import EditProjectIO
io=EditProjectIO();seqs=dict(mw._current_timeline_sequences);payload={k:v.to_dict() for k,v in seqs.items()};path=OUT/'audit-scratch.topos'
saved=io.save_project(path,mw._current_edit_project,timeline_sequences=seqs,use_ai_callback=False,create_backup=False);assert saved
loaded=io.load_project(path);assert loaded
new_payload={k:v.to_dict() for k,v in loaded[2].items()}
assert payload==new_payload
(OUT/'timeline_snapshot.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2))
(OUT/'timeline_after_reload.json').write_text(json.dumps(new_payload,ensure_ascii=False,indent=2))
record('save_reload',saved=saved,sequence_count=len(seqs),payload_equal=True,project_revision=[mw._current_edit_project.revision,loaded[0].revision])
io.cleanup()
