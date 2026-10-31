import json,numpy as np
from unittest.mock import patch
from src.features.edit.core.project import EditProject,MediaAsset
from src.features.edit.core.timeline_v2 import ClipInstance,EditTrack,TimelineSequence,Rational
from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.timeline_export import create_timeline_exporter,TimelineExportConfig
from src.features.edit.core.stem_export import render_stem_pcm,StemSpec,StemType
class D:
 source_layout=None
 def open(self,*a,**k): return True
 def decode_samples_for_frame(self,frame,fps,n): return np.ones((2,n),np.float32)*.5
 def close(self): pass
p=EditProject.create(name='audit').with_media_asset_added(MediaAsset(asset_id='s',path='/tmp/source.wav',media_type='audio',display_name='s',metadata={}))
for label,extra in [('limiter',{'effects':(EffectInstance(effect_type='limiter',params={'threshold_db':-20.,'release_ms':100.,'enabled':True}),)}),('freeze',{'freeze_source_frame':0})]:
 c=ClipInstance(clip_id='c',asset_id='s',track_id='a1',timeline_start=0,timeline_duration=25,source_start=0,source_duration=25,**extra)
 seq=TimelineSequence(timeline_id='s',fps=Rational(25,1),audio_tracks=(EditTrack(track_id='a1',track_type='audio',index=0,clips=(c,)),))
 e=create_timeline_exporter(p,seq)
 with patch('src.features.edit.core.audio_playback.AudioDecoder',D):
  master=e._render_audio_master(0,25,TimelineExportConfig(output_path='/tmp/unused.mp4'),encoder=object())
 stem=render_stem_pcm(seq,StemSpec(StemType.MASTER,'Master'),decode_clip_fn=lambda t,c,f,fps,n:np.ones((2,n),np.float32)*.5)
 print(json.dumps({'case':label,'timeline_master_peak':float(np.max(abs(master))),'master_stem_peak':float(np.max(abs(stem))),'equal':bool(np.array_equal(master,stem))}))
