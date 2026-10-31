from PyQt6.QtWidgets import QDialogButtonBox
from src.features.edit.ui.new_timeline_dialog import NewTimelineDialog
parent_id=tv._timeline.timeline_id
created_dialog={}
def fill_new_sequence():
 d=next((w for w in app.topLevelWidgets() if isinstance(w,NewTimelineDialog) and w.isVisible()),None)
 if d is None:created_dialog['error']='dialog absent';return
 d.name_edit.selectAll();QTest.keyClicks(d.name_edit,'Audit advanced 25fps')
 d.fps_combo.setCurrentIndex(2)
 created_dialog['settings']=d.get_settings()
 capture('018_new_sequence_dialog',d)
 box=d.findChild(QDialogButtonBox);QTest.mouseClick(box.button(QDialogButtonBox.StandardButton.Ok),Qt.MouseButton.LeftButton)
QTimer.singleShot(350,fill_new_sequence)
mw._on_timeline_create_requested();QTest.qWait(300)
advanced_id=tv._timeline.timeline_id
sv.open_asset(asset);sv.set_in_point(100);sv.set_out_point(199);QTest.qWait(150)
for i in range(3):click(tp.btn_append)
click(tp.btn_zoom_fit)
result={'dialog':created_dialog,'old':parent_id,'new':advanced_id,'timeline':summary()}
capture('018_new_sequence')
