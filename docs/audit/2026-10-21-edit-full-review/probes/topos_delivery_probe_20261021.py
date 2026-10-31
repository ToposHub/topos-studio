import json
import numpy as np
from unittest.mock import patch
from src.features.edit.core.project import EditProject,MediaAsset
from src.features.edit.core.timeline_v2 import ClipInstance,EditTrack,TimelineSequence,Rational
from src.features.edit.core.clip_params import SpeedParams,SpeedPoint
from src.features.edit.core.timeline_export import create_timeline_exporter,TimelineExportConfig
records=[]
class RecorderDecoder:
    def __init__(self): self.source_layout=None
    def open(self,*a,**k): return True
    def decode_samples_for_frame(self,frame,fps,num_samples):
        records.append(frame)
        return np.ones((2,num_samples),np.float32)*0.05
    def close(self): pass
p=EditProject.create(name='audit')
p=p.with_media_asset_added(MediaAsset(asset_id='src',path='/tmp/audit_source.wav',media_type='audio',display_name='source',metadata={'fps':24.,'total_frames':500}))
for label,speed,source_duration,timeline_duration in [('reverse',SpeedParams(reverse=True),12,12),('speedpoint',SpeedParams(speed=1.,speed_points=(SpeedPoint(source_offset=6,speed=2.),)),18,12),('constant2',SpeedParams(speed=2.),24,12)]:
    c=ClipInstance(clip_id='c',asset_id='src',track_id='a1',timeline_start=0,timeline_duration=timeline_duration,source_start=50,source_duration=source_duration,speed=speed)
    t=EditTrack(track_id='a1',track_type='audio',index=0,clips=(c,))
    seq=TimelineSequence(timeline_id='s',fps=Rational(24,1),audio_tracks=(t,))
    e=create_timeline_exporter(p,seq)
    records.clear()
    with patch('src.features.edit.core.audio_playback.AudioDecoder', RecorderDecoder):
        pcm=e._render_audio_master(0,timeline_duration,TimelineExportConfig(output_path='/tmp/unused.mp4'),encoder=object())
    print(json.dumps({'case':label,'actual_decode_source_frames':records,'canonical_clip_mapping':[c.timeline_to_source_frame(i) for i in range(timeline_duration)],'shape':None if pcm is None else list(pcm.shape)}))
