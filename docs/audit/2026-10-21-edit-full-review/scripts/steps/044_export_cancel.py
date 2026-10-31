mw.switch_to_page('Deliver');QTest.qWait(350);ws=mw._deliver_workspace();dp=ws.settings_panel
dp._range_combo.setCurrentIndex(0);dp._filename_edit.setText('audit_gui_cancel');control_events=[]
def cancel_current_audit_job():
 control_events.append({'action':'cancel','before':[{'name':j.name,'status':j.status} for j in ws._controller.queue.jobs]})
 ws._controller.cancel_current()
QTimer.singleShot(150,cancel_current_audit_job)
QTest.mouseClick(dp._add_and_start_btn,Qt.MouseButton.LeftButton);QTest.qWait(300)
result={'events':control_events,'jobs':[{'name':j.name,'status':j.status,'error':j.error,'path':j.output_path} for j in ws._controller.queue.jobs],'success_manifest_exists':(export_dir/'audit_gui_cancel.mp4.manifest.json').exists()}
capture('044_export_cancel')
