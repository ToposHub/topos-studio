from PyQt6.QtWidgets import QMessageBox
export_events=[]
ws._controller.job_finished.connect(lambda jid,ok:export_events.append({'id':jid,'success':ok}))
# The production button runs preflight, enqueue and worker start.
QTest.mouseClick(dp._add_and_start_btn,Qt.MouseButton.LeftButton);QTest.qWait(500)
result={'jobs':[j.to_dict() for j in ws._controller.get_jobs()] if hasattr(ws._controller,'get_jobs') else [str(j) for j in ws._controller.queue.jobs], 'events':export_events,'dialogs':[{'type':type(w).__name__,'title':w.windowTitle(),'text':w.text() if isinstance(w,QMessageBox) else ''} for w in app.topLevelWidgets() if isinstance(w,QDialog) and w.isVisible()]}
capture('030_export_running')
