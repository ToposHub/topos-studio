from pathlib import Path
import os,sys,json
os.environ['QT_QPA_PLATFORM']='offscreen';sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from PyQt6.QtCore import QPoint,QPointF,Qt
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest
from src.features.edit.ui.timeline_view import TimelineView
from src.features.edit.ui.timeline_items import EditTool
from src.features.edit.core.timeline_v2 import ClipInstance,EditTrack,TimelineSequence
from src.shared.media.metadata import Rational
app=QApplication([]);out={}
def new(locked=False):
 v=TimelineView();v.resize(1200,550);v.show();seq=TimelineSequence(timeline_id='p',name='p',fps=Rational(24,1),video_tracks=(EditTrack(track_id='v',name='V1',track_type='video',index=0,is_locked=locked,clips=(ClipInstance(clip_id='c',asset_id='missing',track_id='v',timeline_start=100,timeline_duration=100,source_start=100,source_duration=100),)),));v.load_timeline(seq);v.set_zoom(3);v.set_snap_enabled(False);app.processEvents();return v
for kind in ['empty','clip','middle']:
 v=new();v.set_tool(EditTool.HAND if kind!='middle' else EditTool.SELECT);v.horizontalScrollBar().setValue(50);app.processEvents();item=v._items_map['c']
 p=v.mapFromScene(item.mapToScene(QPointF(90,30))) if kind=='clip' else v.mapFromScene(QPointF(1000,item.y()+30))
 before=v.horizontalScrollBar().value();btn=Qt.MouseButton.MiddleButton if kind=='middle' else Qt.MouseButton.LeftButton
 QTest.mousePress(v.viewport(),btn,Qt.KeyboardModifier.NoModifier,p);QTest.mouseMove(v.viewport(),p-QPoint(90,0),10);app.processEvents()
 active=v._interaction.state.name;QTest.mouseRelease(v.viewport(),btn,Qt.KeyboardModifier.NoModifier,p-QPoint(90,0));app.processEvents()
 out['hand_'+kind]={'scroll_before':before,'scroll_after':v.horizontalScrollBar().value(),'during_state':active,'clip_start':v._timeline.get_clip_by_id('c').timeline_start};v.close()
for locked,btn in [(False,Qt.MouseButton.RightButton),(True,Qt.MouseButton.LeftButton)]:
 v=new(locked);v.set_tool(EditTool.BLADE);item=v._items_map['c'];p=v.mapFromScene(item.mapToScene(QPointF(150,30)))
 QTest.mouseClick(v.viewport(),btn,Qt.KeyboardModifier.NoModifier,p);app.processEvents();out['blade_'+('locked' if locked else 'rightclick')]={'clip_count':len(v._timeline.video_tracks[0].clips),'durations':[c.timeline_duration for c in v._timeline.video_tracks[0].clips],'undo':v.can_undo()};v.close()
print(json.dumps(out,ensure_ascii=False,indent=2));open(str(Path(__file__).with_suffix(".json")),'w').write(json.dumps(out,ensure_ascii=False,indent=2))
