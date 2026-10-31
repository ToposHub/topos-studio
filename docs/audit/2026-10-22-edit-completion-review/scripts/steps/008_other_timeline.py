from PyQt6.QtWidgets import QDialogButtonBox
from src.features.edit.ui.new_timeline_dialog import NewTimelineDialog
def fill_dialog():
    dialog=next((w for w in app.topLevelWidgets() if isinstance(w,NewTimelineDialog) and w.isVisible()),None)
    if dialog is None:return
    dialog.name_edit.selectAll();QTest.keyClicks(dialog.name_edit,'Round7 second timeline')
    dialog.fps_combo.setCurrentIndex(2)
    QTest.mouseClick(dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok),Qt.MouseButton.LeftButton)
QTimer.singleShot(250,fill_dialog)
mw._on_timeline_create_requested();QTest.qWait(250)
second_id=tv._timeline.timeline_id
sv.open_asset(asset);sv.set_in_point(100);sv.set_out_point(199);QTest.qWait(100)
click(tp.btn_append);click(tp.btn_append)
result={'program_before':seq_info(tv._timeline)}
mw.switch_to_page('Deliver');QTest.qWait(250)
dp._timeline_combo.setCurrentIndex(dp._timeline_combo.findData(base_id));QTest.qWait(100)
dp._filename_edit.setText('round7_nonactive');dp._range_combo.setCurrentIndex(0)
result['selected']={'combo':dp._timeline_combo.currentData(),'draft':seq_info(dp._sequence),'program':seq_info(tv._timeline)}
QTest.mouseClick(dp._add_and_start_btn,Qt.MouseButton.LeftButton);QTest.qWait(300)
result['queue']=[{'status':str(j.status),'error':j.error,'path':j.output_path} for j in ws._controller.queue.jobs]
capture('008_nonactive_export')
