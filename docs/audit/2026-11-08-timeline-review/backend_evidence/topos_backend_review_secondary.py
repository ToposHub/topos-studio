from __future__ import annotations
import sys,json,runpy
from dataclasses import replace
sys.path.insert(0,'/Users/heng/Documents/vscode/topos-studio')
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt,QEvent
from PyQt6.QtGui import QKeyEvent
from src.features.edit.core.commands import MoveClipCommand
from src.features.edit.ai_editing.edit_project_service import EditProjectService
from src.features.edit.ui.timeline_panel import TimelinePanel
from src.features.edit.ui.timeline_view import TimelineView
from src.features.edit.core.selection import TimelineSelection
app=QApplication([]);result={}
t=runpy.run_path('tests/edit/test_position_lock_policy.py');state=t['_state']()
for policy in (None,{'c1'}):
 cmd=MoveClipCommand.create('v1','missing',20,0)
 try:
  r=EditProjectService.commit_command(state,'lock',cmd,base_revision=999,position_locked_clip_ids=policy)
  result['service_'+str(policy)]={'applied':r.applied,'error':r.error}
 except Exception as exc:result['service_'+str(policy)]={'exception':repr(exc)}
v=TimelineView();v.load_timeline(t['_timeline']());v.set_position_lock_enabled(True);v.set_selection(TimelineSelection().with_clip_selected('c1','v1','lock'))
try:v.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress,Qt.Key.Key_Delete,Qt.KeyboardModifier.NoModifier))
except Exception as exc:result['delete_keypress']={'exception':repr(exc),'history':len(v._command_stack._undo_stack),'clip_remaining':v.get_timeline().get_clip_by_id('c1') is not None}
v.close()
p=TimelinePanel();p.load_timeline(t['_timeline']());payload={'thumbnail_mode':'none','future_field':{'keep':True},'version':2,'source_destination_ui':{'lock':{'video':'v2','future_stream':'something'}}};p.apply_presentation_state(payload);result['presentation_export']=p.export_presentation_state()
try:p.apply_presentation_state({'custom_pixels_per_frame':2,'custom_scroll':'garbage'})
except Exception as exc:result['invalid_presentation']={'exception':repr(exc)}
p.timeline_view._on_source_patch_requested('v2',False);seq=p.timeline_view.get_timeline();p.clear();p.load_timeline(seq);result['new_project_same_timeline_route']={'disabled_after_clear':p.timeline_view.source_destination_stream_disabled('video')};p.close()
print(json.dumps(result,indent=2));open('/tmp/topos_backend_review_secondary_results.json','w').write(json.dumps(result,indent=2))
