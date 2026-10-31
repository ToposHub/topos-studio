mw.raise_();mw.activateWindow();wait(100)
select_tab('Properties');click(panel.btn_select)
from src.features.edit.core.selection import TimelineSelection
vt=tv._timeline.video_tracks[0]
if vt.is_locked:tv._on_track_lock_changed(vt.track_id,False)
clips=tv._timeline.video_tracks[0].clips
selection=TimelineSelection()
for i,c in enumerate((clips[0],clips[-1])):
    selection=selection.with_clip_selected(c.clip_id,c.track_id,tv._timeline.timeline_id,replace=(i==0))
tv.set_selection(selection);wait(150)
props=mw.edit_properties_panel
sec=next(s for k,s in props._sections.items() if k.endswith(':transform'))
def real_numeric(name,value):
    w=sec._param_widgets[name]['widget']
    QTest.mouseDClick(w,Qt.MouseButton.LeftButton);wait(70)
    ed=w._edit
    assert ed.isVisible(),(name,'editor did not open')
    ed.selectAll();QTest.keyClicks(ed,str(value));QTest.keyClick(ed,Qt.Key.Key_Return);wait(140)
def selected_transforms():
    return [(c.clip_id,c.transform.translate_x,c.transform.translate_y) for c in tv._timeline.video_tracks[0].clips if c.clip_id in tv._selection.selected_clip_ids]
real_numeric('translate_x',55);real_numeric('translate_y',66)
record('multi_xy_explicit_editor',state=selected_transforms(),screenshot=shot('29-multi-xy-confirmed'))
assert all(x==55 and y==66 for cid,x,y in selected_transforms())
key(Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier)
real_numeric('translate_y',88)
record('multi_undo_then_field_confirmed',state=selected_transforms(),screenshot=shot('30-undo-field-confirmed'))
assert all(x==55 and y==88 for cid,x,y in selected_transforms())
