timer.stop()
from src.features.edit.core.timeline_ops import validate_timeline
record('source_validation',issues=validate_timeline(tv._timeline))
# 新建真实小时间线，便于正常Roll/取消与监视器抢焦测试。
create_timer=QTimer()
def setup_roll_fixture():
    for d in QApplication.topLevelWidgets():
        if type(d).__name__=='NewTimelineDialog' and d.isVisible():
            create_timer.stop();d.name_edit.selectAll();QTest.keyClicks(d.name_edit,'Round3 Roll')
            click(d._btn_box.button(__import__('PyQt6.QtWidgets',fromlist=['QDialogButtonBox']).QDialogButtonBox.StandardButton.Ok))
create_timer.timeout.connect(setup_roll_fixture);create_timer.start(300);mw._on_timeline_create_requested();create_timer.stop();wait(400)
select_tab('Source');sv.seek(0);click(sv.toolbar.btn_mark_in);sv.seek(49);click(sv.toolbar.btn_mark_out);click(panel.btn_append)
sv.seek(50);click(sv.toolbar.btn_mark_in);sv.seek(99);click(sv.toolbar.btn_mark_out);click(panel.btn_append)
select_tab('Program');click(panel.btn_zoom_fit);click(panel.btn_roll)
# 隐藏旧monitor保证测试首次show的focus影响。所有动作仍发真实事件。
monitor=getattr(tv,'_trim_monitor_dialog',None)
if monitor:monitor.hide()
tv.setFocus();wait(100)
before=timeline_state();p=frame_point(50);QTest.mousePress(tv.viewport(),Qt.MouseButton.LeftButton,pos=p);wait(100)
record('roll_normal_press',state=str(tv._interaction.state),preview=getattr(tv,'_roll_preview_state',None) is not None,focus=type(app.focusWidget()).__name__ if app.focusWidget() else None,monitor_visible=getattr(tv,'_trim_monitor_dialog',None).isVisible() if getattr(tv,'_trim_monitor_dialog',None) else None,screenshot=shot('23-roll-press'))
for i in range(1,9):
    end=p+QPoint(i*3,0)
    QApplication.sendEvent(tv.viewport(),QMouseEvent(__import__('PyQt6.QtCore',fromlist=['QEvent']).QEvent.Type.MouseMove,QPointF(end),QPointF(tv.viewport().mapToGlobal(end)),Qt.MouseButton.NoButton,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier));wait(30)
record('roll_normal_move',state=str(tv._interaction.state),preview=getattr(tv,'_roll_preview_state',None) is not None)
QTest.mouseRelease(tv.viewport(),Qt.MouseButton.LeftButton,pos=end);wait(250)
record('roll_normal_release',before=before,after=timeline_state(),screenshot=shot('24-roll-normal-release'))
if before!=timeline_state():key(Qt.Key.Key_Z,Qt.KeyboardModifier.ControlModifier)
timer.start(500)
