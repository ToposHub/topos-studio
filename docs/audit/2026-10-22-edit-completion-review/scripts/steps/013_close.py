mw._save_current_project();QTest.qWait(150)
result={'saved_project':str(project_file),'close_return':mw.close()}
QTimer.singleShot(150,app.quit)
