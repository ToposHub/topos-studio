sc=ep._ensure_scene_cut_controller();sa=mw._current_edit_project.get_asset_by_path(paths[2]);scene_results=[];scene_errors=[]
sc.analysis_available.connect(lambda a:scene_results.append(a)) if hasattr(sc,'analysis_available') else None
sc.detection_error.connect(lambda *a:scene_errors.append(str(a)))
sc.start_detection_for_asset(sa.asset_id);QTest.qWait(300)
sd=sc._active_dialog
buttons=[{'text':b.text(),'enabled':b.isEnabled()} for b in sd.findChildren(QAbstractButton)]
b=next((b for b in sd.findChildren(QAbstractButton) if b.text() in ('自动检测','Auto Detect')),None)
if b:QTest.mouseClick(b,Qt.MouseButton.LeftButton)
QTest.qWait(600)
result={'source':sa.display_name,'dialog':type(sd).__name__,'buttons':buttons,'started':b is not None,'active_dialog':sd.isVisible(),'errors':scene_errors}
capture('039_scene_detection',sd)
