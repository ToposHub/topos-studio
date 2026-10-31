dp._timeline_combo.setCurrentIndex(dp._timeline_combo.findData(parent_id));dp._filename_edit.setText('audit_gui_other_timeline');QTest.qWait(200)
result={'selected':dp._timeline_combo.currentData(),'draft_sequence':tl_info(dp._sequence),'controller_sequence_type':type(ws._controller._sequences_by_id.get(parent_id)).__name__}
QTest.mouseClick(dp._add_and_start_btn,Qt.MouseButton.LeftButton);QTest.qWait(700)
result['jobs']=[{'name':j.name,'status':j.status,'error':j.error,'path':j.output_path} for j in ws._controller.queue.jobs]
capture('033_other_timeline_export')
