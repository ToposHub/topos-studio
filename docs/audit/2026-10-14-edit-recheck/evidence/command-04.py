timer.stop()
from PyQt6.QtCore import qVersion,QT_VERSION_STR
record('runtime',python=sys.version,qt_runtime=qVersion(),qt_compiled=QT_VERSION_STR,platform=app.platformName())
# Restore a precise two-clip selection through the existing selection service as fixture.
# The actual numeric edit below still uses visible DragValue keyboard input.
from src.features.edit.core.selection import TimelineSelection
select_tab('Program');click(panel.btn_select)
clips=list(tv._timeline.video_tracks[0].clips[:2])
# Keep fixture setter explicit so it is not reported as a successful Shift-click test.
import inspect
record('selection_api',methods=[(n,str(inspect.signature(getattr(tv,n)))) for n in ('set_selection','_on_selection_changed','_update_selection','_select_clips') if hasattr(tv,n)],selected=list(tv._selection.selected_clip_ids))
# Clear the earlier edit only if it is the latest command label.
record('stack_label',label=tv._command_stack.get_undo_label() if hasattr(tv._command_stack,'get_undo_label') else None)
# Observe press/move/preview directly with a safely interior body point on the right clip.
click(panel.btn_trim);right=clips[-1];item=tv._items_map[right.clip_id]
p=clip_point(right.clip_id,item._width-4)
before=timeline_state();QTest.mouseMove(tv.viewport(),p);wait(80)
QTest.mousePress(tv.viewport(),Qt.MouseButton.LeftButton,pos=p);wait(50)
record('trim_pressed',mode=item._drag_mode,pending=item._drag_pending,is_dragging=item._is_dragging,interaction=str(tv._interaction.state),point=[p.x(),p.y()])
end=frame_point(400)
for i in range(1,9):
    pt=p+(end-p)*(i/8)
    e=QMouseEvent(QEvent.Type.MouseMove,QPointF(pt),QPointF(tv.viewport().mapToGlobal(pt)),Qt.MouseButton.NoButton,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(tv.viewport(),e);wait(25)
record('trim_preview_state',mode=item._drag_mode,duration=item._duration_frames,source=item._source_duration,interaction=str(tv._interaction.state))
QTest.mouseRelease(tv.viewport(),Qt.MouseButton.LeftButton,pos=end);wait(200)
record('trim_tool_release',before=before,after=timeline_state(),screenshot=shot('13-trim-tool'))
click(panel.btn_select)
timer.start(500)
