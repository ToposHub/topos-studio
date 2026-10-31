timer.stop();rescue_timer.stop()
from PyQt6.QtWidgets import QMessageBox
close_timer=QTimer()
def discard_audit_dialogs():
    for d in QApplication.topLevelWidgets():
        if isinstance(d,QMessageBox) and d.isVisible():
            b=d.button(QMessageBox.StandardButton.Discard) or d.button(QMessageBox.StandardButton.No)
            if b:QTest.mouseClick(b,Qt.MouseButton.LeftButton)
        elif isinstance(d,QDialog) and d.isVisible():d.reject()
close_timer.timeout.connect(discard_audit_dialogs);close_timer.start(250)
mw.close();close_timer.stop();record('normal_audit_close',window_visible=mw.isVisible())
src.main._orderly_shutdown(app,0)
