mw._save_current_project();QTest.qWait(200)
result={'saved_path':mw._current_edit_project_filepath,'dirty':mw._has_unsaved_changes(),'requested_close':'MainWindow.close via Qt timer; normal production shutdown'}
save_record('045_close_requested',result)
QTimer.singleShot(500,mw.close)
