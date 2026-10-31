from pathlib import Path
import os, sys, json
os.environ['QT_QPA_PLATFORM']='offscreen'
sys.path.insert(0, '/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01')
from PyQt6.QtCore import QPointF, QPoint, Qt, QEvent
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import QApplication, QGraphicsScene, QGraphicsView
from PyQt6.QtTest import QTest
from src.features.edit.ui.timeline_items import ClipItem, EditTool
from src.features.edit.ui.timeline_view import TimelineView
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.media.metadata import Rational
app=QApplication([])
result={}
for edge in ['left','right']:
 scene=QGraphicsScene(); view=QGraphicsView(scene); view.resize(1000,300); view.setSceneRect(0,0,900,200)
 item=ClipItem('c','v','probe',100,100,2,0,source_start=100,source_duration=100,max_source_frames=1000,media_type='video')
 scene.addItem(item); item.setPos(100,60); item.set_active_tool(EditTool.RIPPLE)
 records=[]
 item.ripple_trim_left_finished.connect(lambda *args:records.append(('left',args)))
 item.ripple_trim_right_finished.connect(lambda *args:records.append(('right',args)))
 view.show(); app.processEvents()
 pos=view.mapFromScene(item.mapToScene(QPointF(2 if edge=='left' else item._width-2,30)))
 QTest.mouseClick(view.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,pos)
 result['ripple_click_'+edge]=records
 view.close()
seq=TimelineSequence(timeline_id='probe',name='probe',fps=Rational(24,1),video_tracks=(EditTrack(track_id='v',name='V1',track_type='video',index=0,clips=(ClipInstance(clip_id='c',asset_id='missing',track_id='v',timeline_start=100,timeline_duration=100,source_start=100,source_duration=100),)),))
v=TimelineView(); v.resize(1200,550); v.show(); v.load_timeline(seq); v.set_zoom(3); v.set_snap_enabled(False); app.processEvents()
item=v._items_map['c']; pos=v.mapFromScene(item.mapToScene(QPointF(90,30)))
QTest.mousePress(v.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,pos)
QTest.mouseMove(v.viewport(),pos+QPoint(90,0),10); app.processEvents()
result['before_escape']={'model':v._timeline.get_clip_by_id('c').timeline_start,'preview':item._start_frame,'interaction':v._interaction.state.name,'drag':item._is_dragging}
QTest.keyClick(v,Qt.Key.Key_Escape); app.processEvents()
result['after_escape']={'model':v._timeline.get_clip_by_id('c').timeline_start,'preview':item._start_frame,'interaction':v._interaction.state.name,'drag':item._is_dragging}
QTest.mouseRelease(v.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,pos+QPoint(90,0)); app.processEvents()
result['after_release']={'model':v._timeline.get_clip_by_id('c').timeline_start,'preview':item._start_frame,'undo':v.can_undo()}
# horizontal-only trackpad wheel versus vertical wheel
v.horizontalScrollBar().setValue(80); before=v.horizontalScrollBar().value()
e=QWheelEvent(QPointF(500,150),QPointF(500,150),QPoint(40,0),QPoint(120,0),Qt.MouseButton.NoButton,Qt.KeyboardModifier.NoModifier,Qt.ScrollPhase.ScrollUpdate,False)
QApplication.sendEvent(v.viewport(),e)
result['horizontal_wheel']={'before':before,'after':v.horizontalScrollBar().value()}
beforezoom=v._pixels_per_frame
e=QWheelEvent(QPointF(500,150),QPointF(500,150),QPoint(40,0),QPoint(120,0),Qt.MouseButton.NoButton,Qt.KeyboardModifier.ControlModifier,Qt.ScrollPhase.ScrollUpdate,False)
QApplication.sendEvent(v.viewport(),e)
result['ctrl_horizontal_wheel']={'before':beforezoom,'after':v._pixels_per_frame}
v.close(); print(json.dumps(result,ensure_ascii=False,indent=2)); open(str(Path(__file__).with_suffix(".json")),'w').write(json.dumps(result,ensure_ascii=False,indent=2))
