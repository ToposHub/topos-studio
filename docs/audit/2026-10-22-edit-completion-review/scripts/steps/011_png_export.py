dp._container_combo.setCurrentIndex(dp._container_combo.findData(''));QTest.qWait(150)
result={'codecs':[(dp._codec_combo.itemText(i),dp._codec_combo.itemData(i)) for i in range(dp._codec_combo.count())]}
dp._codec_combo.setCurrentIndex(dp._codec_combo.findData('png_sequence'))
dp._filename_edit.setText('round7_gui_png');dp._range_combo.setCurrentIndex(2)
dp._start_frame_spin.setValue(0);dp._end_frame_spin.setValue(3)
dp._resolution_combo.setCurrentIndex(dp._resolution_combo.findData('custom'))
dp._width_spin.setValue(320);dp._height_spin.setValue(180)
jobs,pf=dp.build_jobs(preflight=True);result['preflight']=str(pf);result['built']=[j.to_dict() for j in jobs]
if jobs:QTest.mouseClick(dp._add_and_start_btn,Qt.MouseButton.LeftButton);QTest.qWait(250)
result['queue']=[{'status':str(j.status),'error':j.error,'path':j.output_path} for j in ws._controller.queue.jobs]
capture('011_png_export')
