# Select a real graphics item using a viewport mouse click.
click(tp.btn_select); click(tp.btn_zoom_fit)
clipitem=next(x for x in tv.scene().items() if isinstance(x,ClipItem) and x.clip_id==tv._timeline.video_tracks[0].clips[0].clip_id)
pos=tv.mapFromScene(clipitem.sceneBoundingRect().center())
QTest.mouseClick(tv.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,pos); QTest.qWait(300)
pp=mw.edit_properties_panel
# Bring the Properties tab forward using the existing dock API.
for dock in mw.findChildren(__import__('PyQt6.QtWidgets',fromlist=['QDockWidget']).QDockWidget):
 if dock.widget() is pp: dock.show();dock.raise_()
QTest.qWait(300)
result={'selection':str(tv._selection),'sections':{k:list(v._param_widgets) for k,v in pp._sections.items()},'keyframes':list(pp._keyframe_buttons),'clip_id':clipitem.clip_id,'properties_visible':pp.isVisible(),'program_children':[(k,type(v).__name__) for k,v in vars(mw.program_viewer_widget).items() if 'viewer' in k]}
capture('013_properties')
