import json
import numpy as np
from unittest.mock import patch
from src.features.edit.core.project import EditProject,MediaAsset
from src.features.edit.core.timeline_v2 import ClipInstance,EditTrack,TimelineSequence,Rational
from src.features.edit.core.timeline_export import create_timeline_exporter,TimelineExportConfig
opened=[]
class Decoder:
    def __init__(self): self.source_layout=None; self.closed=False; opened.append(self)
    def open(self,*a,**k): return True
    def decode_samples_for_frame(self,frame,fps,num_samples): return np.ones((2,num_samples),np.float32)*0.05
    def close(self): self.closed=True
p=EditProject.create(name='audit').with_media_asset_added(MediaAsset(asset_id='src',path='/tmp/source.wav',media_type='audio',display_name='src',metadata={})) 
c=ClipInstance(clip_id='c',asset_id='src',track_id='a1',timeline_start=0,timeline_duration=300,source_start=0,source_duration=300)
t=EditTrack(track_id='a1',track_type='audio',index=0,clips=(c,))
for fps in [Rational(25,1),Rational(30000,1001),Rational(60000,1001)]:
 seq=TimelineSequence(timeline_id='s',fps=fps,audio_tracks=(t,))
 e=create_timeline_exporter(p,seq)
 with patch('src.features.edit.core.audio_playback.AudioDecoder',Decoder):
  pcm=e._render_audio_master(0,300,TimelineExportConfig(output_path='/tmp/unused.mp4'),encoder=object())
 zero=np.where(pcm[0]==0)[0]
 print(json.dumps({'fps':str(fps),'samples':pcm.shape[-1],'artificial_silent_samples_per_channel':len(zero),'first_10_silent_sample_indices':zero[:10].tolist()}))
seq=TimelineSequence(timeline_id='s',fps=Rational(25,1),audio_tracks=(t,))
e=create_timeline_exporter(p,seq)
class CancelDecoder(Decoder):
 def decode_samples_for_frame(self,frame,fps,num_samples):
  e._cancel_requested=True
  return super().decode_samples_for_frame(frame,fps,num_samples)
with patch('src.features.edit.core.audio_playback.AudioDecoder',CancelDecoder):
 pcm=e._render_audio_master(0,300,TimelineExportConfig(output_path='/tmp/unused.mp4'),encoder=object())
print(json.dumps({'cancelled_pcm_is_None':pcm is None,'opened_decoder_closed':opened[-1].closed}))
