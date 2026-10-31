import json,numpy as np
from pathlib import Path
from src.features.edit.core.project import EditProject,MediaAsset
from src.features.edit.core.timeline_v2 import ClipInstance,EditTrack,TimelineSequence,Rational
from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.timeline_export import create_timeline_exporter,TimelineExportConfig
from src.features.edit.core.stem_export import render_stem_pcm,StemSpec,StemType,create_clip_decode_fn
p=EditProject.create(name='audit').with_media_asset_added(MediaAsset(asset_id='s',path=str(Path.cwd()/'tests/test_videos/2K_h264.mp4'),media_type='video',display_name='2K_h264',metadata={'fps':25.}))
for label,extra in [('limiter',{'effects':(EffectInstance(effect_type='limiter',params={'threshold_db':-30.,'release_ms':100.,'enabled':True}),)}),('freeze',{'freeze_source_frame':100})]:
 c=ClipInstance(clip_id='c',asset_id='s',track_id='a1',timeline_start=0,timeline_duration=50,source_start=100,source_duration=50,**extra)
 seq=TimelineSequence(timeline_id='s',fps=Rational(25,1),audio_tracks=(EditTrack(track_id='a1',track_type='audio',index=0,clips=(c,)),))
 e=create_timeline_exporter(p,seq)
 master=e._render_audio_master(0,50,TimelineExportConfig(output_path='/tmp/unused.mp4'),encoder=object())
 decode,close=create_clip_decode_fn(p,25.,48000)
 try:
  stem=render_stem_pcm(seq,StemSpec(StemType.MASTER,'Master'),decode_clip_fn=decode)
 finally:close()
 print(json.dumps({'case':label,'timeline_master_peak':float(np.max(abs(master))),'master_stem_peak':float(np.max(abs(stem))),'max_abs_difference':float(np.max(abs(master-stem))),'equal':bool(np.array_equal(master,stem))}))
