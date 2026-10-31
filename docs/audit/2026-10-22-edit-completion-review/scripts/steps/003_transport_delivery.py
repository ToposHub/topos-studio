mw.dock_program_viewer.raise_();bar=mw.program_viewer_widget.get_transport_bar()
click(tp.btn_zoom_fit)
p=tv.mapFromScene(QPointF(tv._layout_engine.frame_to_x(25),12))
QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,p);QTest.qWait(250)
def frames():
    return {'view':tv._playhead_frame,'controller':tp.get_current_frame(),'page':ep._current_frame}
result={'ruler':frames()}
click(bar.btn_next_frame);QTest.qWait(250);result['next']=frames()
click(bar.btn_prev_frame);QTest.qWait(250);result['prev']=frames()
click(bar.btn_jump_start);QTest.qWait(250);result['home']=frames()
capture('003_transport')
mw.switch_to_page('Deliver');QTest.qWait(350)
ws=mw._deliver_workspace();dp=ws.settings_panel
export_dir=SESSION/'exports';export_dir.mkdir(exist_ok=True)
dp._output_dir_edit.setText(str(export_dir));dp._filename_edit.setText('round7_custom_h264')
dp._range_combo.setCurrentIndex(2);dp._start_frame_spin.setValue(0);dp._end_frame_spin.setValue(49)
for i in range(dp._codec_combo.count()):
    if str(getattr(dp._codec_combo.itemData(i),'value',dp._codec_combo.itemData(i))).lower()=='h264':
        dp._codec_combo.setCurrentIndex(i);break
dp._resolution_combo.setCurrentIndex(dp._resolution_combo.findData('custom'))
dp._width_spin.setValue(640);dp._height_spin.setValue(360);dp._audio_check.setChecked(True)
jobs,preflight=dp.build_jobs(preflight=True)
result['custom']={'jobs':[j.to_dict() for j in jobs],'preflight':str(preflight),'sequence':seq_info(dp._sequence),'map':{k:seq_info(v) for k,v in dp._timeline_sequences.items()}}
QTest.mouseClick(dp._add_and_start_btn,Qt.MouseButton.LeftButton);QTest.qWait(500)
result['queue']=[{'id':j.job_id,'status':str(j.status),'error':j.error,'path':j.output_path} for j in ws._controller.queue.jobs]
capture('003_delivery_started')
