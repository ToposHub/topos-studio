from pathlib import Path
import os,sys,json
os.environ['QT_QPA_PLATFORM']='offscreen';sys.path.insert(0, '/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01')
from PyQt6.QtCore import QPoint,QPointF,Qt
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest
from src.features.edit.ui.timeline_view import TimelineView
from src.features.edit.core.timeline_v2 import ClipInstance,EditTrack,TimelineSequence
from src.features.edit.core.clip_params import SpeedParams
from src.shared.media.metadata import Rational
app=QApplication([]);out={}
for edge in ['left','right']:
 c=ClipInstance(clip_id='c',asset_id='missing',track_id='v',timeline_start=100,timeline_duration=100,source_start=100,source_duration=200,speed=SpeedParams(speed=2.0));seq=TimelineSequence(timeline_id='p',name='p',fps=Rational(24,1),video_tracks=(EditTrack(track_id='v',name='V1',track_type='video',index=0,clips=(c,)),));v=TimelineView();v.resize(1200,550);v.show();v.load_timeline(seq);v.set_zoom(3);v.set_snap_enabled(False);app.processEvents();item=v._items_map['c'];item._max_source_frames=1000
 p=v.mapFromScene(item.mapToScene(QPointF(2 if edge=='left' else item._width-2,30)));q=p+QPoint(30 if edge=='left' else -30,0)
 QTest.mousePress(v.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,p);QTest.mouseMove(v.viewport(),q,10);app.processEvents();QTest.mouseRelease(v.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,q);app.processEvents();new=v._timeline.get_clip_by_id('c');out['speed_2_trim_'+edge]={'timeline_start':new.timeline_start,'timeline_duration':new.timeline_duration,'source_start':new.source_start,'source_duration':new.source_duration,'speed':new.effective_speed(),'source_last_frame':new.timeline_to_source_frame(new.timeline_end-1)};v.close()
print(json.dumps(out,ensure_ascii=False,indent=2));open(str(Path(__file__).with_suffix(".json")),'w').write(json.dumps(out,ensure_ascii=False,indent=2))
