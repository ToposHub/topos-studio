cp=mw.caption_workspace_panel
mw.dock_caption_workspace.raise_();QTest.qWait(150)
result={}
QTest.mouseClick(cp._find_edit,Qt.MouseButton.LeftButton);QTest.keyClicks(cp._find_edit,'Hello');click(cp._find_btn)
result['find']={'selected':list(cp._selected_clip_ids()),'status':cp._status_label.text(),'playhead':tv._playhead_frame}
QTest.mouseClick(cp._replace_edit,Qt.MouseButton.LeftButton);QTest.keyClicks(cp._replace_edit,'Hi');click(cp._replace_btn)
result['replace']=[c.text for t in tv._timeline.subtitle_tracks for c in t.subtitle_clips]
rows=cp._model.rowCount();click(cp._split_btn)
result['split']={'before':rows,'after':cp._model.rowCount(),'status':cp._status_label.text()}
tv.undo();QTest.qWait(200);result['undo_split']=cp._model.rowCount()
# Navigate using actual timeline ruler clicks, compare view/controller.
viewframes=[]
for f in (25,120,250):
 p=tv.mapFromScene(QPointF(tv._layout_engine.frame_to_x(f),12))
 QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,p);QTest.qWait(250)
 viewframes.append({'requested':f,'view':tv._playhead_frame,'controller':tp.get_current_frame(),'page':ep._current_frame})
result['ruler_seek']=viewframes
capture('022_caption_actions')
