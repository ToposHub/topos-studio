from pathlib import Path
from dataclasses import replace
from unittest.mock import Mock
import numpy as np
from PyQt6.QtCore import QCoreApplication
from src.features.edit.core.edit_gpu_decode_manager import EditGpuDecodeManager
from src.features.edit.core.timeline_preview_renderer import AssetResolver, PreviewRenderWorker
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.core.timeline_render_plan import TimelineRenderPlan
from src.features.edit.core.project import EditProject
from src.shared.media.metadata import Rational
from src.shared.media.topos_source import ToposMediaSource

app = QCoreApplication([])
root = Path.cwd()
path = str(root/'tests/test_videos/TOPOS 422Proxy/北海公园 - TOPOS 422Proxy - 4K 细节优化.mov')
assert Path(path).is_file() and Path(path).stat().st_size > 1000000
project = EditProject.create('UHD Program verification').import_asset(
    path=path, media_type='video',
    metadata={'width':3840, 'height':2160, 'fps':25.0, 'total_frames':1730})
asset_id = next(iter(project.assets))
clip = ClipInstance('clip',asset_id,'v1',0,1730,0,1730)
track = EditTrack(track_id='v1', name='V1', track_type='video', index=0, clips=(clip,))
seq = TimelineSequence(timeline_id='tl', name='UHD', width=3840, height=2160,
                       fps=Rational(25,1), video_tracks=(track,))
manager = EditGpuDecodeManager()
manager.set_playback_active(True)
gpu = Mock()
gpu.submit.return_value = True
worker = PreviewRenderWorker(frame_provider=Mock(), asset_resolver=AssetResolver(project),
                             video_renderer=None, timeline_sequence=seq,
                             viewport_size=(960,540), gpu_decode_manager=manager, gpu_renderer=gpu)
native_file = ToposMediaSource(path)
native_file.open()

def render(f: int, quality: float, timeline: TimelineSequence,
           fbo_expected: tuple[int, int], input_expected: tuple[int, int]) -> None:
    worker._timeline_sequence = timeline
    worker.set_preview_quality(quality)
    layer = TimelineRenderPlan(timeline).get_frame_plan(f).layers[0]
    ok, trace = worker._try_gpu_render(frame=f,layers=(layer,),transitions=(),
                                       generation_id=1)
    assert ok, trace
    task = gpu.submit.call_args.args[0]
    frame = task.layers[0]['decoded_frame']
    assert task.output_fbo_size == fbo_expected, task.output_fbo_size
    assert task.decoded_sizes == (input_expected,), task.decoded_sizes
    assert (frame.width,frame.height) == input_expected
    if quality == 1:
        master = native_file.read_frame(f)
        assert all(np.array_equal(a,b) for a,b in zip(frame.planes,master.planes,strict=True))
        result = 'YUV planes identical to independent native decode'
    else:
        result = 'reduced source verified'
    print(f'frame={f} quality={quality} viewport=960x540 native_input={task.decoded_sizes} composite={task.output_fbo_size}: {result}',flush=True)

try:
    assert worker._preview_quality == 1.0
    render(301,1.0,seq,(3840,2160),(3840,2160))
    render(1474,0.5,seq,(1920,1080),(1920,1080))
    render(1604,0.25,seq,(960,540),(960,540))
    render(1604,1.0,seq,(3840,2160),(3840,2160))
    preview_1080 = replace(seq,width=1920,height=1080)
    render(1604,1.0,preview_1080,(1920,1080),(3840,2160))
finally:
    native_file.close()
    worker.stop()
    manager.close_all()
