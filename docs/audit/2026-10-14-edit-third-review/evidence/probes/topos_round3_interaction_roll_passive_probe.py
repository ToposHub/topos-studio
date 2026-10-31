from pathlib import Path
import os,sys,json,traceback
os.environ['QT_QPA_PLATFORM']='offscreen';sys.path.insert(0,'/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01')
from dataclasses import replace
from PyQt6.QtCore import QPoint,QPointF,Qt
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest
from src.features.edit.ui.timeline_view import TimelineView
from src.features.edit.ui.timeline_items import EditTool
from src.features.edit.core.timeline_v2 import ClipInstance,EditTrack,TimelineSequence
from src.features.edit.core.clip_params import SpeedParams
from PyQt6.QtCore import QObject,QEvent
class FocusTrace(QObject):
 def eventFilter(self,obj,event):
  if event.type()==QEvent.Type.FocusOut: obj._probe_focus_out.append({'reason':event.reason().name,'active_window':type(QApplication.activeWindow()).__name__,'interaction':obj._interaction.state.name})
  return False
app=QApplication([]);out={};errors=[];sys.excepthook=lambda *a:errors.append(str(a[1]))
for speed in [.5,2]:
 l=ClipInstance(clip_id='l',asset_id='missing',track_id='v',timeline_start=0,timeline_duration=100,source_start=100,source_duration=int(100*speed),speed=SpeedParams(speed=speed));r=replace(l,clip_id='r',timeline_start=100,source_start=400);seq=TimelineSequence(timeline_id='p',name='p',fps=24,video_tracks=(EditTrack(track_id='v',name='V1',track_type='video',index=0,clips=(l,r)),));v=TimelineView();v.resize(1400,650);v.show();v.load_timeline(seq);v.set_zoom(3);v.set_snap_enabled(False);v.set_tool(EditTool.ROLL);app.processEvents();v.setFocus();monitor=v._trim_monitor();monitor.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating,True);monitor.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus,True);v._probe_focus_out=[];trace=FocusTrace(v);v.installEventFilter(trace);h=next(iter(v._roll_handles.values()));events=[];h.roll_trim_started.connect(lambda *a:events.append(a));
 for i in v._items_map.values():i._max_source_frames=1000
 p=v.mapFromScene(h.mapToScene(h.boundingRect().center()));q=p+QPoint(30,0);info=dict(p=[p.x(),p.y()],hrect=[h.boundingRect().x(),h.boundingRect().y(),h.boundingRect().width(),h.boundingRect().height()],viewport=[v.viewport().width(),v.viewport().height()],htool=h._active_tool.name,hvisible=h.isVisible(),buttons=str(h.acceptedMouseButtons()),items=[type(i).__name__ for i in v.scene().items(v.mapToScene(p))],focus=v.hasFocus())
 QTest.mouseMove(v.viewport(),p,10);QTest.mousePress(v.viewport(),Qt.MouseButton.LeftButton,pos=p);info['events_press']=list(events);QTest.mouseMove(v.viewport(),q,10);app.processEvents();info['state_during']=v._interaction.state.name;info['preview']={cid:[i._source_start,i._source_duration,i._start_frame,i._duration_frames] for cid,i in v._items_map.items()};QTest.mouseRelease(v.viewport(),Qt.MouseButton.LeftButton,pos=q);app.processEvents();info['committed']={c.clip_id:[c.source_start,c.source_duration,c.timeline_start,c.timeline_duration] for t in v._timeline.all_tracks for c in t.clips};info['expected']={'l':list(l.with_right_edge_source_values(110)),'r':list(r.with_left_edge_source_values(110))};info['focus_out']=list(v._probe_focus_out);info['errors']=list(errors);out[str(speed)]=info;v.close();app.processEvents()
Path('/tmp/topos_round3_interaction_roll_passive_probe.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps(out,ensure_ascii=False,indent=2))
