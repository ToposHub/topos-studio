from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
import time

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QSurfaceFormat
from PyQt6.QtWidgets import QApplication

from src.app.controllers.file_controller import FileOpsMixin
from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager
from src.features.edit.core.edit_gpu_preview_renderer import EditGpuPreviewRenderer
from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.core.project import EditProject
from src.features.edit.ui.program_viewer import ProgramViewerWidget
from src.shared.media.metadata import Rational
from src.shared.video.frame_provider_service import VideoFrameProviderService

format = QSurfaceFormat()
format.setVersion(4, 1)
format.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(format)
app = QApplication([])
path = str(Path.cwd()/'tests/test_videos/TOPOS 422Proxy/北海公园 - TOPOS 422Proxy - 4K 细节优化.mov')
assert Path(path).stat().st_size > 1000000
project = EditProject.create('UHD Program verification').import_asset(
    path=path, media_type='video',
    metadata={'width':3840, 'height':2160, 'fps':25.0, 'total_frames':1730})
asset_id = next(iter(project.assets))
clip = ClipInstance('clip',asset_id,'v1',0,1730,0,1730)
track = EditTrack(track_id='v1', name='V1', track_type='video', index=0, clips=(clip,))
seq = TimelineSequence(timeline_id='tl', name='UHD', width=3840, height=2160,
                       fps=Rational(25,1), video_tracks=(track,))
provider = VideoFrameProviderService()
manager = EditGpuDecodeManager()
program = ProgramViewerWidget()
program.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
program.resize(960, 620)
program.show()
viewer = program.get_viewer()

def wait(predicate: Callable[[], bool], timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.003)
    assert predicate(), 'timed out waiting for Qt/GPU'

wait(lambda: viewer.isValid() and viewer.context() is not None)
print('Program Viewer context ready', flush=True)
gpu = EditGpuPreviewRenderer(viewer.context())
gpu.start()
timeline = TimelinePreviewRenderer(project, seq, provider, main_context=viewer.context(),
                                    viewport_size=(viewer.width(),viewer.height()))
try:
    timeline.set_gpu_renderer(manager, gpu)
    wait(lambda: gpu.is_initialized)
    controller = SimpleNamespace(
        program_viewer_widget=program, timeline_preview_renderer=timeline,
        _ensure_program_monitor_output=lambda: None,
        _submit_program_monitor_frame=lambda *args: None, _current_edit_project=None,
    )
    timeline.frame_ready.connect(
        lambda frame, result: FileOpsMixin._on_timeline_preview_frame_ready(controller, frame, result))
    program.preview_quality_changed.connect(timeline.set_preview_quality)
    presented = []
    viewer.frame_presented.connect(presented.append)
    errors = []
    timeline.render_error.connect(lambda frame, msg: errors.append((frame, msg)))
    label = program.get_transport_bar().resolution_label
    for frame, scale, expected, decoded in [
        (301,1.0,'3840×2160','3840×2160'),
        (1474,0.5,'1920×1080 / 3840×2160','1920×1080'),
        (1604,0.25,'960×540 / 3840×2160','960×540'),
        (1605,1.0,'3840×2160','3840×2160'),
    ]:
        action = next(action for action in program.get_toolbar().quality_menu.actions()
                      if action.data() == scale)
        if not action.isChecked():
            action.trigger()
        timeline.set_playhead(frame)
        wait(lambda: ((frame in presented and label.text() == expected) or errors))
        assert not errors, errors
        assert label.text() == expected
        assert decoded in label.toolTip()
        print('painted', frame, 'widget', viewer.width(),viewer.height(),
              'preview', label.text(), 'info', label.toolTip().replace('\n',' | '),
              flush=True)
    assert gpu._output_pool and len(gpu._output_pool) == 8
    timeline.set_sequence(replace(seq, width=1920, height=1080))
    frame = 1606
    timeline.set_playhead(frame)
    wait(lambda: frame in presented and label.text() == "1920×1080")
    assert viewer._tex_width == 1920 and viewer._tex_height == 1080
    assert label.toolTip().count("3840×2160") == 2
    print("1080p timeline with the same master", label.toolTip().replace("\n", " | "), flush=True)
finally:
    timeline.shutdown()
    gpu.stop()
    gpu.wait(4000)
    provider.shutdown()
    program.close()
    app.processEvents()
