export_dir=SESSION/'exports';export_dir.mkdir(exist_ok=True)
dp.set_active_timeline(advanced_id)
dp._output_dir_edit.setText(str(export_dir));dp._filename_edit.setText('audit_gui_h264')
dp._range_combo.setCurrentIndex(2);dp._start_frame_spin.setValue(0);dp._end_frame_spin.setValue(50)
# Select codec/resolution using the same controls used in the panel.
for i in range(dp._codec_combo.count()):
 if str(getattr(dp._codec_combo.itemData(i),'value',dp._codec_combo.itemData(i))).lower()=='h264':dp._codec_combo.setCurrentIndex(i);break
dp._resolution_combo.setCurrentIndex(dp._resolution_combo.findData('custom'))
dp._width_spin.setValue(640);dp._height_spin.setValue(360)
dp._audio_check.setChecked(True)
for i in range(dp._subtitle_combo.count()):
 if str(getattr(dp._subtitle_combo.itemData(i),'value',dp._subtitle_combo.itemData(i))).lower() in ('none','off'):dp._subtitle_combo.setCurrentIndex(i);break
QTest.qWait(250)
jobs,preflight=dp.build_jobs(preflight=True)
result={'codec':str(dp._codec_combo.currentData()),'resolution':dp._collect_resolution() if hasattr(dp,'_collect_resolution') else [dp._width_spin.value(),dp._height_spin.value()],'jobs':[j.to_dict() for j in jobs],'preflight':str(preflight),'enabled':dp._add_and_start_btn.isEnabled(),'controller_type':type(ws._controller).__name__}
capture('027_export_prepared')
