from PyQt6.QtWidgets import QFileDialog
QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs,True)
project_file=SESSION/'audit-review.topos'
save_dialog={}
def choose_save_path():
 d=next((w for w in app.topLevelWidgets() if isinstance(w,QFileDialog) and w.isVisible()),None)
 if d is None:save_dialog['error']='not found';return
 d.selectFile(str(project_file));save_dialog['selected']=d.selectedFiles();capture('031_save_dialog',d)
 box=d.findChild(QDialogButtonBox);button=box.button(QDialogButtonBox.StandardButton.Save)
 if button is not None:QTest.mouseClick(button,Qt.MouseButton.LeftButton)
 else:d.accept()
QTimer.singleShot(400,choose_save_path)
mw._save_current_project_as();QTest.qWait(350)
result={'dialog':save_dialog,'exists':project_file.exists(),'bytes':project_file.stat().st_size if project_file.exists() else 0,'mw_filepath':mw._current_edit_project_filepath}
if project_file.exists():
 from src.features.edit.core.edit_project_io import EditProjectIO
 io=EditProjectIO();loaded=io.load_project(project_file)
 result['reload']={'success':loaded is not None,'assets':len(loaded[0].assets) if loaded else None,'sequences':{k:tl_info(v) for k,v in loaded[2].items()} if loaded else None}
 io.cleanup()
