"""Isolated reproductions: only writes synthetic files in a temporary directory."""
from __future__ import annotations
import os
import sys
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['TOPOS_TEST_MODE'] = '1'
from PyQt6.QtWidgets import QApplication
from src.features.edit.core.project import EditProject
from src.features.edit.core.project_archive import collect_project
from src.features.edit.core.edit_project_io import EditProjectIO
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, Rational, TimelineSequence
from src.features.edit.ui.media_pool_panel import MediaPoolPanel
from src.features.edit.core.media_management_worker import KIND_CONSOLIDATE
from tempfile import TemporaryDirectory
temporary = TemporaryDirectory(prefix='topos-media-pool-review-')
work = Path(temporary.name)
app = QApplication.instance() or QApplication([])
source = work / 'source.mov'
source.write_bytes(b'NEW_SOURCE_CONTENT')
project = EditProject.create('Audit original').import_asset(str(source), 'video', {'fps':25.,'total_frames':100})
project = project.create_timeline('Audit timeline')
aid = project.get_asset_by_path(str(source)).asset_id
tid = next(iter(project.timelines))
clip = ClipInstance('c',aid,'v',0,20,10,20,_source_fps_num=25,_source_fps_den=1,_timeline_fps_num=25,_timeline_fps_den=1)
sequence = TimelineSequence(timeline_id=tid,name='Audit timeline',fps=Rational(25,1),video_tracks=(EditTrack('v','V','video',0,clips=(clip,)),))
output={}
# Core collection can overwrite a pre-existing destination with another asset's bytes.
target=work/'existing_target'
(target/'media').mkdir(parents=True,exist_ok=True)
existing=target/'media'/'source.mov'
existing.write_bytes(b'PREEXISTING_ARCHIVE_KEEP_ME')
with patch('src.features.edit.core.project_archive.build_identity_manifest', return_value={}):
    archived,report=collect_project(project,target_dir=str(target),sequences=[sequence])
output['existing_target_overwritten'] = existing.read_bytes()==source.read_bytes()
# Match the exact UI save call: timeline refs alone do not persist editable sequences.
io=EditProjectIO()
saved=io.save_project(str(work/'collected.topos'),archived)
loaded=io.load_project(str(work/'collected.topos'))
output['archive_save_returns_success']=saved
output['saved_timeline_refs']=list(loaded[0].timelines) if loaded else None
output['saved_timeline_sequences']=list((loaded[2] or {}).keys()) if loaded else None
panel=MediaPoolPanel(); panel._offline_refresh_timer.stop();panel.set_project(project)
# A queued operation holds self._project rather than the initiating snapshot.
queued=[]
panel._collect_timeline_sequences=lambda:[sequence]
def queue(kind: str, work_fn: Any, finish_fn: Any, **kwargs: Any) -> None: queued.append((kind,work_fn,finish_fn))
panel._run_media_management=queue
with patch('src.features.edit.ui.media_pool_panel.QFileDialog.getExistingDirectory',return_value=str(work/'later')):
    panel._on_collect_project()
replacement=EditProject.create('Changed project')
panel.set_project(replacement)
seen=[]
def fake_collect(actual: Any, **kwargs: Any) -> tuple[Any, None]:
    seen.append({'project_id':actual.project_id,'sequence_id':kwargs['sequences'][0].timeline_id})
    return actual, None
with patch('src.features.edit.ui.media_pool_panel.collect_project',side_effect=fake_collect):
    queued[0][1](lambda *_:None,lambda:False)
output['queued_collect_uses_replacement_project']=seen[0]['project_id']==replacement.project_id
output['queued_collect_keeps_old_sequence']=seen[0]['sequence_id']==tid
panel.clear_project();panel.close();panel.deleteLater();app.processEvents()
print(json.dumps(output,indent=2,ensure_ascii=False))

temporary.cleanup()
