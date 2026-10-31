from PyQt6.QtWidgets import QFileDialog,QDialogButtonBox
QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs,True)
project_file=SESSION/'round7-review.topos';dialog_result={}
def choose_save():
    dialog=next((w for w in app.topLevelWidgets() if isinstance(w,QFileDialog) and w.isVisible()),None)
    if dialog is None:dialog_result['error']='dialog absent';return
    dialog.selectFile(str(project_file));dialog_result['selected']=dialog.selectedFiles()
    button=dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save)
    QTest.mouseClick(button,Qt.MouseButton.LeftButton)
QTimer.singleShot(250,choose_save)
mw._save_current_project_as();QTest.qWait(200)
expected={'assets':len(mw._current_edit_project.assets),'timelines':len(mw._current_timeline_sequences),'active':tv._timeline.timeline_id,'duration':tv._timeline.duration_frames}
opened=mw._open_project_path(str(project_file));QTest.qWait(500)
result={'dialog':dialog_result,'file_exists':project_file.exists(),'open_return':opened,'expected':expected,'actual':{'assets':len(mw._current_edit_project.assets),'timelines':len(mw._current_timeline_sequences),'active':tv._timeline.timeline_id,'duration':tv._timeline.duration_frames}}
capture('012_reopened')
