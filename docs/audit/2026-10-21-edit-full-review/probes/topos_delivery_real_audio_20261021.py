import json
import numpy as np
from pathlib import Path
from src.features.edit.core.project import EditProject,MediaAsset
from src.features.edit.core.timeline_v2 import ClipInstance,EditTrack,TimelineSequence,Rational
from src.features.edit.core.clip_params import SpeedParams,SpeedPoint
from src.features.edit.core.timeline_export import create_timeline_exporter,TimelineExportConfig
root=Path.cwd()
p=EditProject.create(name='audio_audit')
p=p.with_media_asset_added(MediaAsset(asset_id='src',path=str(root/'tests/test_videos/2K_h264.mp4'),media_type='video',display_name='2K_h264',metadata={'fps':25.,'total_frames':1572,'audio_channels':2}))
audios={}
for label,speed,source_duration,timeline_duration in [('normal',SpeedParams(),50,50),('reverse',SpeedParams(reverse=True),50,50),('speedpoint',SpeedParams(speed=1.,speed_points=(SpeedPoint(source_offset=25,speed=2.),)),75,50)]:
 c=ClipInstance(clip_id='c',asset_id='src',track_id='a1',timeline_start=0,timeline_duration=timeline_duration,source_start=100,source_duration=source_duration,speed=speed)
 t=EditTrack(track_id='a1',track_type='audio',index=0,clips=(c,))
 seq=TimelineSequence(timeline_id='s',fps=Rational(25,1),audio_tracks=(t,))
 e=create_timeline_exporter(p,seq)
 pcm=e._render_audio_master(0,timeline_duration,TimelineExportConfig(output_path='/tmp/unused.mp4'),encoder=object())
 audios[label]=pcm
 print(json.dumps({'case':label,'shape':list(pcm.shape),'peak':float(np.max(abs(pcm))),'first_source':c.timeline_to_source_frame(0),'last_source':c.timeline_to_source_frame(49)}))
for label in ['reverse','speedpoint']:
 print(json.dumps({'compare':label,'identical_to_normal':bool(np.array_equal(audios[label],audios['normal'])),'max_abs_difference':float(np.max(abs(audios[label]-audios['normal'])))}))
