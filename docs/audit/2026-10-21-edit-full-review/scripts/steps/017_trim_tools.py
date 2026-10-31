result={}
mw.dock_program_viewer.raise_();click(tp.btn_zoom_fit)
# A selected clip from an actual viewport mouse click.
def get_item(cid):return next(x for x in tv.scene().items() if isinstance(x,ClipItem) and x.clip_id==cid)
def drag_points(start,end):
 QTest.mousePress(tv.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,start)
 for i in range(1,9):QTest.mouseMove(tv.viewport(),start+(end-start)*i//8,35)
 QTest.mouseRelease(tv.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,end)
 QTest.qWait(350)

cid=tv._timeline.video_tracks[0].clips[0].clip_id
before=summary();c=tv._timeline.get_clip_by_id(cid);it=get_item(cid)
click(tp.btn_trim);start=tv.mapFromScene(it.sceneBoundingRect().topRight()+QPointF(-2,it.sceneBoundingRect().height()/2))
end=start-QPoint(20,0)
# QPoint arithmetic uses integer coordinates to match an ordinary mouse drag.
QTest.mousePress(tv.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,start)
for i in range(1,9):QTest.mouseMove(tv.viewport(),QPoint(start.x()+(end.x()-start.x())*i//8,start.y()),35)
QTest.mouseRelease(tv.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,end);QTest.qWait(350)
result['trim_mouse']={'before':before,'after':summary()}
if tv._timeline.get_clip_by_id(cid).timeline_duration!=c.timeline_duration:tv.undo();QTest.qWait(200)
# Check toolbar selection covers all supported editing modes.
result['tools']=[]
for name in ('btn_select','btn_trim','btn_blade','btn_ripple','btn_rate_stretch','btn_roll','btn_slip','btn_slide','btn_hand','btn_zoom'):
 b=getattr(tp,name);click(b); result['tools'].append({'button':name,'checked':b.isChecked(),'tool':str(tv._current_tool) if hasattr(tv,'_current_tool') else None})
click(tp.btn_select)
# Split at a position known to be strictly inside a clip.
c=next(x for x in tv._timeline.video_tracks[0].clips if x.timeline_duration>=40)
f=c.timeline_start+c.timeline_duration//2;tp.set_playhead_frame(f);tv.setFocus();QApplication.setActiveWindow(mw)
b=summary();QTest.keyClick(tv,Qt.Key.Key_B,Qt.KeyboardModifier.ControlModifier);QTest.qWait(250)
result['split_interior']={'frame':f,'before':b,'after':summary()}
tv.undo();QTest.qWait(200)
capture('017_trim_tools')
