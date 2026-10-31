from pathlib import Path
import sys,json,runpy,logging
sys.path.insert(0,str(Path.cwd()))
import numpy as np
from unittest.mock import Mock
from dataclasses import replace
h=runpy.run_path('tests/edit/test_fix_round6_phase1b_audio.py')
from src.features.edit.core.timeline_v2 import ClipInstance,TimelineSequence,EditTrack
from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.project import EditProject,MediaAsset
from src.shared.media.metadata import Rational
from src.features.edit.core.audio_playback import AudioDecoder
from src.features.edit.core.timeline_export import TimelineExportConfig
root=Path.cwd(); wav=root/'tests/test_videos/2K_h264.mp4'
p=EditProject('p','P',assets={'a':MediaAsset('a',str(wav),'video','Real',{})})
out={}
for speed,sr,start,duration,source_dur in [(1,44100,50,50,100),(2,48000,100,50,100),(1,48000,100,100,100)]:
 c=ClipInstance('c','a','t',0,duration,start,source_dur,speed=SpeedParams(speed=speed))
 seq=TimelineSequence(timeline_id='s',name='S',width=64,height=48,fps=Rational(25,1),audio_tracks=(EditTrack('t','A1','audio',0,clips=(c,),volume=1),))
 exp=h['_exporter'](p,seq)
 cfg=TimelineExportConfig(output_path='/tmp/topos-round7-audio-data/real.mp4',audio_sample_rate=sr)
 pcm=exp._render_audio_master(0,duration,cfg,encoder=Mock())
 k=f'speed{speed}_sr{sr}_in{start}'
 out[k]={'shape':list(pcm.shape),'peak':float(np.max(np.abs(pcm))),'nonzero_samples':int(np.count_nonzero(pcm)),'tail_std':float(np.std(pcm[0,-sr:]))}
# Determine the production AAC decoder short-block behavior independently.
d=AudioDecoder();d.open(str(wav),timeline_fps=25,output_sample_rate=44100)
try:
 blocks=[]
 for f in range(50,100):
  a=d.decode_samples_for_frame(f,25,1764)
  blocks.append({'frame':f,'length':None if a is None else a.shape[-1]})
 out['decoder_blocks']=blocks
finally:d.close()
print(json.dumps(out,indent=2))
Path('/tmp/topos-round7-audio-real-results.json').write_text(json.dumps(out,indent=2))
