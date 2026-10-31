result={'project_path':getattr(mw,'_current_edit_project_filepath',None),'close_return':mw.close()}
QTimer.singleShot(100,QApplication.instance().quit)
