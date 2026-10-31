timer.stop()
from PyQt6.QtWidgets import QMessageBox
# This is the audit's own scratch app/project, not the user's live process.
def close_audit_dialog():
    for d in QApplication.topLevelWidgets():
        if isinstance(d,QMessageBox) and d.isVisible():
            button=d.button(QMessageBox.StandardButton.Discard) or d.button(QMessageBox.StandardButton.No)
            if button:click(button)
        elif isinstance(d,QDialog) and d.isVisible():d.reject()
QTimer.singleShot(250,close_audit_dialog)
mw.close()
record('audit_shutdown',closed=True)
src.main._orderly_shutdown(app,0)
