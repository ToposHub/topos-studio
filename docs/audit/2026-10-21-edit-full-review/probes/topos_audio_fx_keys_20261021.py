import json,numpy as np
from unittest.mock import patch
from src.features.edit.core.project import EditProject,MediaAsset
from src.features.edit.core.timeline_v2 import ClipInstance,EditTrack,TimelineSequence,Rational
from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.timeline_export import create_timeline_exporter,TimelineExportConfig
class D:
 source_layout=None
 def open(self,*a,**k): return True
 def decode_samples_for_frame(self,frame,fps,n): return np.ones((2,n),np.float32)*.5
 def close(self): pass
p=EditProject.create(name='audit').with_media_asset_added(MediaAsset(asset_id='s',path='/tmp/source.wav',media_type='audio',display_name='s',metadata={}))
base=EffectInstance(effect_type='limiter',params={'threshold_db':0.,'release_ms':100.,'enabled':True})
keyed=base.with_keyframe_added('threshold_db',0,-20.,min_value=-20,max_value=0.)
static=EffectInstance(effect_type='limiter',params={'threshold_db':-20.,'release_ms':100.,'enabled':True})
outs={}
for label,effect in [('base',base),('animated',keyed),('static',static)]:
 c=ClipInstance(clip_id='c',asset_id='s',track_id='a1',timeline_start=0,timeline_duration=25,source_start=0,source_duration=25,effects=(effect,))
 seq=TimelineSequence(timeline_id='s',fps=Rational(25,1),audio_tracks=(EditTrack(track_id='a1',track_type='audio',index=0,clips=(c,)),))
 e=create_timeline_exporter(p,seq)
 with patch('src.features.edit.core.audio_playback.AudioDecoder',D):
  pcm=e._render_audio_master(0,25,TimelineExportConfig(output_path='/tmp/unused.mp4'),encoder=object())
 outs[label]=pcm
 print(json.dumps({'case':label,'effect_threshold_at_0':effect.get_param_at_frame('threshold_db',0),'tail_pcm_level':float(pcm[0,-1]),'peak':float(max(abs(pcm[0])))}))
print('animated_equals_unanimated_base',np.array_equal(outs['base'],outs['animated']))
