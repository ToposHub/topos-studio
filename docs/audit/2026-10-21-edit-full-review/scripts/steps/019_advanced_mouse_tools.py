result=[]
click(tp.btn_zoom_fit)
if tp.btn_snap.isChecked():click(tp.btn_snap)
def do_drag(p1,p2):
 QTest.mousePress(tv.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,p1)
 for j in range(1,11): QTest.mouseMove(tv.viewport(),QPoint(p1.x()+(p2.x()-p1.x())*j//10,p1.y()+(p2.y()-p1.y())*j//10),35)
 QTest.mouseRelease(tv.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,p2);QTest.qWait(300)
for tool,part,delta in [('btn_roll','cut',16),('btn_slip','body',18),('btn_slide','body',18),('btn_ripple','right',-18),('btn_rate_stretch','last',18)]:
 click(getattr(tp,tool));cs=tv._timeline.video_tracks[0].clips
 c=cs[-1] if part=='last' else cs[1] if part in ('body','right') else cs[0]
 it=get_item(c.clip_id); r=it.sceneBoundingRect()
 sc=QPointF(r.right()-2,r.center().y()) if part in ('cut','right','last') else r.center()
 p1=tv.mapFromScene(sc);p2=p1+QPoint(delta,0)
 before=summary();do_drag(p1,p2);after=summary()
 result.append({'tool':tool,'pixels':delta,'before':before,'after':after,'speed_after':[x.speed.to_dict() for x in tv._timeline.video_tracks[0].clips]})
 save_record('019_'+tool,result[-1])
 if before!=after:tv.undo();QTest.qWait(200)
click(tp.btn_select);capture('019_advanced_tools')
