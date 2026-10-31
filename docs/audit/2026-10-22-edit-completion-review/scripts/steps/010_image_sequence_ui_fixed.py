result={'containers':[(dp._container_combo.itemText(i),dp._container_combo.itemData(i)) for i in range(dp._container_combo.count())]}
result['codecs']=[(dp._codec_combo.itemText(i),str(getattr(dp._codec_combo.itemData(i),'value',dp._codec_combo.itemData(i)))) for i in range(dp._codec_combo.count())]
idx=dp._codec_combo.findData('png_sequence');result['png_index']=idx
if idx>=0:
    dp._codec_combo.setCurrentIndex(idx);dp._filename_edit.setText('round7_gui_png')
    dp._resolution_combo.setCurrentIndex(dp._resolution_combo.findData('custom'));dp._width_spin.setValue(320);dp._height_spin.setValue(180)
    dp._range_combo.setCurrentIndex(2);dp._start_frame_spin.setValue(0);dp._end_frame_spin.setValue(3)
    jobs,pf=dp.build_jobs(preflight=True);result['preflight']=str(pf);result['built']=[j.to_dict() for j in jobs]
    if jobs:QTest.mouseClick(dp._add_and_start_btn,Qt.MouseButton.LeftButton);QTest.qWait(200)
result['queue']=[{'status':str(j.status),'error':j.error,'path':j.output_path} for j in ws._controller.queue.jobs]
capture('010_png_ui')
