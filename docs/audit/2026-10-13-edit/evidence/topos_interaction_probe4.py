from pathlib import Path
import os,sys,json
os.environ['QT_QPA_PLATFORM']='offscreen';sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from unittest.mock import patch
from PyQt6.QtCore import QPoint,QPointF,Qt
from PyQt6.QtWidgets import QApplication,QMessageBox
from PyQt6.QtTest import QTest
from src.features.edit.ui.timeline_view import TimelineView
from src.features.edit.core.timeline_v2 import ClipInstance,EditTrack,TimelineSequence,SubtitleClip
from src.features.edit.core.selection import TimelineSelection
from src.shared.media.metadata import Rational
app=QApplication([]);out={}
v=TimelineView();v.resize(1200,550);v.show();c=ClipInstance(clip_id='c',asset_id='missing',track_id='v',timeline_start=100,timeline_duration=100,source_start=100,source_duration=100);seq=TimelineSequence(timeline_id='p',name='p',fps=Rational(24,1),video_tracks=(EditTrack(track_id='v',name='V1',track_type='video',index=0,clips=(c,)),));v.load_timeline(seq);v.set_zoom(3);app.processEvents();v.set_selection(TimelineSelection().with_clip_selected('c','v','p'));item=v._items_map['c']
p=v.mapFromScene(item.mapToScene(QPointF(-10,10)));q=v.mapFromScene(item.mapToScene(QPointF(item._width+10,item._track_height-10)))
QTest.mousePress(v.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.ShiftModifier,p);QTest.mouseMove(v.viewport(),q,10);QTest.mouseRelease(v.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.ShiftModifier,q);app.processEvents();out['shift_box_selection']={'selected_ids':list(v._selection.selected_clip_ids),'is_multi':v._selection.is_multi};v.close()
s=SubtitleClip(clip_id='s',track_id='s1',timeline_start=100,timeline_duration=100,text='probe');st=EditTrack(track_id='s1',name='S1',track_type='subtitle',index=0,subtitle_clips=(s,));seq=TimelineSequence(timeline_id='p',name='p',fps=Rational(24,1),subtitle_tracks=(st,));v=TimelineView();v.resize(1200,550);v.show();v.load_timeline(seq);v.set_zoom(3);app.processEvents();v.set_selection(TimelineSelection().with_clip_selected('s','s1','p'));before=v._pixels_per_frame;v.zoom_fit_selection();out['subtitle_fit_selection']={'before':before,'after':v._pixels_per_frame};v.set_playhead(50);v._jump_to_next_clip_boundary();out['subtitle_next_boundary']={'from':50,'to':v.get_playhead_frame(),'subtitle_start':100};records=[]
def question(*args):records.append(str(args[2]));return QMessageBox.StandardButton.No
with patch.object(QMessageBox,'question',question):v._on_track_delete_requested('s1')
out['subtitle_delete_track']={'confirmation_calls':records,'remaining_tracks':len(v._timeline.subtitle_tracks),'undo':v.can_undo()};v.close()
print(json.dumps(out,ensure_ascii=False,indent=2));open(str(Path(__file__).with_suffix(".json")),'w').write(json.dumps(out,ensure_ascii=False,indent=2))
