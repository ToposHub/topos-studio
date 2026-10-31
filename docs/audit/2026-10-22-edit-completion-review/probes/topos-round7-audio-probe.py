from pathlib import Path
import runpy, json, sys
sys.path.insert(0, str(Path.cwd()))
import numpy as np
from unittest.mock import Mock
h=runpy.run_path('tests/edit/test_fix_round6_phase1b_audio.py')
from src.features.edit.core.audio_playback import AudioDecoder, AudioMixer, AudioTrackState
from src.features.edit.core.audio_retime import ClipRetimeReader
from src.features.edit.core.clip_params import SpeedParams, SpeedPoint
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.core.project import MediaAsset, EditProject
from src.shared.media.metadata import Rational
from src.features.edit.core.stem_export import create_clip_decode_fn, render_stem_pcm, StemSpec, StemType
tmp=Path('/tmp/topos-round7-audio-data');tmp.mkdir(exist_ok=True)
SR=48000
out={}
pcm=np.tile(np.linspace(.01,.4,SR*10,dtype=np.float32),(2,1))
wav=h['_write_wav'](tmp/'ramp.wav',pcm)
p=EditProject('p','P',assets={'a':MediaAsset('a',str(wav),'audio','Ramp',{})})
def render(clip, fps=Rational(25,1)):
 seq=TimelineSequence(timeline_id='s',name='S',width=64,height=48,fps=fps,audio_tracks=(EditTrack('t','A1','audio',0,clips=(clip,),volume=1),))
 exp=h['_exporter'](p,seq)
 return h['_render_master'](exp,clip.timeline_duration,h['_config'](tmp)),seq
for start in [0,25,100]:
 c=ClipInstance('c','a','t',0,50,start,100,speed=SpeedParams(speed=2.0))
 rendered,_=render(c)
 out[f'nonzero_start_{start}']={'first':float(rendered[0,0]),'last':float(rendered[0,-1]),'std_tail_1s':float(np.std(rendered[0,-SR:])),'expected_first':float(pcm[0,start*1920]),'expected_last':float(pcm[0,(start+100)*1920-2]),'peak':float(np.max(np.abs(rendered)))}
# Full reverse should return the first source audio block reversed at the end.
c=ClipInstance('c','a','t',0,100,0,100,speed=SpeedParams(speed=1.0,reverse=True))
rev,_=render(c)
out['reverse_tail']={'last_frame_std':float(np.std(rev[0,-1920:])), 'last_frame_first':float(rev[0,-1920]),'last_frame_last':float(rev[0,-1]),'previous_frame_std':float(np.std(rev[0,-3840:-1920]))}
# Explicit source 50fps / timeline 25fps, no intended speed change.
c=ClipInstance('c','a','t',0,50,0,100,_source_fps_num=50,_source_fps_den=1,_timeline_fps_num=25,_timeline_fps_den=1)
mix,seq=render(c)
out['mixedfps']={'first_frame_pair':c.timeline_to_source_frame_pair(0),'frame25_source':c.timeline_to_source_frame(25),'at_1_second':float(mix[0,SR]),'expected_1s':float(pcm[0,SR]),'last':float(mix[0,-1]),'expected_2s':float(pcm[0,2*SR-1])}
# Preserve pitch parity with actual sine; video master invokes WSOLA, stem currently resamples.
swav=h['_write_wav'](tmp/'sine.wav',h['_sine'](440,8))
ps=EditProject('p','P',assets={'a':MediaAsset('a',str(swav),'audio','Sine',{})})
c=ClipInstance('c','a','t',0,50,0,100,speed=SpeedParams(speed=2.0,preserve_pitch=True))
seq=TimelineSequence(timeline_id='s',name='S',width=64,height=48,fps=Rational(25,1),audio_tracks=(EditTrack('t','A1','audio',0,clips=(c,),volume=1),))
master=h['_render_master'](h['_exporter'](ps,seq),50,h['_config'](tmp))
decode,close=create_clip_decode_fn(ps,25,SR)
try: stem=render_stem_pcm(seq,StemSpec(StemType.MASTER,'Master',include_master=True),sample_rate=SR,channels=2,decode_clip_fn=decode)
finally: close()
out['preserve_pitch_parity']={'master_hz':h['_dominant_freq'](master),'stem_hz':h['_dominant_freq'](stem),'max_diff':float(np.max(np.abs(master-stem)))}
print(json.dumps(out,indent=2))
(tmp/'results.json').write_text(json.dumps(out,indent=2))
