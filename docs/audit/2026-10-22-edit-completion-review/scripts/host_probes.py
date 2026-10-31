"""Reproduce queue restoration ordering with production models and widgets."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import tempfile
from dataclasses import replace

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PyQt6.QtWidgets import QApplication
from src.features.edit.core.project import EditProject, TimelineSequenceRef
from src.features.edit.core.timeline_v2 import TimelineSequence, EditTrack, ClipInstance
from src.shared.media.metadata import Rational
from src.features.deliver.ui.deliver_workspace import DeliverWorkspace
from src.features.deliver.core.video_export_job import VideoExportJob, VideoConfig
from src.features.deliver.core.export_queue import save_export_queue

app = QApplication([])
with tempfile.TemporaryDirectory(prefix='topos-round7-restore-') as temporary:
    os.environ['TOPOS_CONFIG_PATH'] = str(Path(temporary) / 'config.json')
    clip = ClipInstance('c', 'a', 'v', 0, 100, 0, 100)
    seq = TimelineSequence('s', 'S', Rational(25, 1), 640, 360,
                           video_tracks=(EditTrack('v', 'V1', 'video', 0, clips=(clip,)),))
    project = replace(EditProject.create('Queue audit'),
                      timelines={'s': TimelineSequenceRef('s', 'S', fps=25.0)},
                      active_timeline_id='s')
    job = VideoExportJob(project_id=project.project_id, timeline_id='s',
                         timeline_name='S', output_path=str(Path(temporary) / 'output.mp4'),
                         video_config=VideoConfig(codec='h264', container='mp4'))
    save_export_queue([job.to_legacy_job()])
    workspace = DeliverWorkspace()
    workspace.set_project(project)
    after_project = [{'status': str(j.status), 'error': j.error} for j in workspace._controller.queue.jobs]
    workspace.set_sequence(seq)
    workspace.set_timeline_sequences({'s': seq})
    output = {'initial_status': str(job.status), 'after_set_project': after_project,
              'after_promote': [{'status': str(j.status), 'error': j.error} for j in workspace._controller.queue.jobs],
              'context_resolves': workspace._controller._resolve_context(job).is_resolvable}
    workspace.close()
    print(json.dumps(output, indent=2))
