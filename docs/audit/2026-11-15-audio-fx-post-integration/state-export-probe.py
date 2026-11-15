"""Independent probes for deferred post-integration state/export findings.

Run with the repository Python environment. Only temporary synthetic WAV files
are created. Real TimelineExporter/renderer/Stem/Mixer are used; encoder output
is mocked. Historical v2 comparison requires local Git object 302b88b6.
This prints measurements and does not declare known bugs to be passing tests.
"""
import sys,json,tempfile,wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import numpy as np
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
from src.features.edit.core.timeline_v2 import *
from src.features.edit.core.project import EditProject,MediaAsset
from src.features.edit.core.timeline_export import TimelineExportConfig,TimelineExporter
from src.features.edit.core.stem_export import StemSpec,StemType,render_stem_pcm
from src.features.edit.core.audio_playback import AudioMixer,AudioTrackState
from src.features.edit.core.fx_versioning import collect_chain_diagnostics
from src.shared.media.metadata import Rational
from src.shared.keyframe import Keyframe,KeyframeTrack,InterpolationType
SR=48000;FPS=25;N=1920

def chain(iid: str = 'D', ms: float = 200.0, wet: float = 100.0) -> EffectChainParams:return EffectChainParams(effects=(TrackEffectInstance('delay',{'enabled':True,'left_delay':ms,'right_delay':ms,'left_feedback':0.,'right_feedback':0.,'dry_wet':wet,'level_db':0.,'low_cut':20.,'high_cut':20000.,'high_ratio':0.,'stereo_blend':0.},instance_id=iid),))
def wav(path: Path, sig: np.ndarray) -> None:
 with wave.open(str(path),'wb') as f:
  f.setnchannels(2);f.setsampwidth(2);f.setframerate(SR);f.writeframes(np.rint(sig.T*32767).astype(np.int16).tobytes())
temporary = tempfile.TemporaryDirectory(prefix='topos-post-state-')
root = Path(temporary.name)
sig=np.zeros((2,SR));sig[:,0]=.5;wav(root/'i.wav',sig)
p=EditProject('p','p',assets={'a':MediaAsset('a',str(root/'i.wav'),'audio','I',{})})
t=EditTrack('T','T','audio',0,clips=(ClipInstance('c','a','T',0,2,0,2),))
res={}
for location in ('track','master','bus-master'):
 kw={};tracks=(t,)
 if location=='track':tracks=(t.with_effect_chain(chain()),)
 elif location=='master':kw['master_effect_chain']=chain()
 else:
  tracks=(t.with_output_bus('B'),EditTrack('B','B','audio',1,is_bus=True,effect_chain=chain('B')));kw['master_effect_chain']=chain('M')
 seq=TimelineSequence(fps=Rational(FPS,1),audio_tracks=tracks,**kw)
 out=TimelineExporter(p,seq,Mock())._render_audio_master(0,4,TimelineExportConfig(output_path=str(root/'o.mov'),include_fx_tail=True),encoder=Mock())
 idx=np.flatnonzero(abs(out[0])>1e-4)
 res['tail_'+location]={'length_ms':out.shape[-1]*1000/SR,'first_echo_ms':idx[0]*1000/SR if idx.size else None,'peak':float(abs(out).max())}
# clip tail parity
from src.features.edit.core.effect_instance import EffectInstance
fx=EffectInstance(effect_type='delay',params=chain().effects[0].params,effect_id='CF')
c=ClipInstance('c','a','T',0,2,0,2,effects=(fx,));seq=TimelineSequence(fps=Rational(FPS,1),audio_tracks=(EditTrack('T','T','audio',0,clips=(c,)),))
out=TimelineExporter(p,seq,Mock())._render_audio_master(0,10,TimelineExportConfig(output_path=str(root/'cf.mov')),encoder=Mock())
def dec(t: object, c: object, f: int, fp: float, n: int) -> np.ndarray:
 y=np.zeros((2,n),np.float32)
 if f==0:y[:,0]=.5
 return y
stem=render_stem_pcm(seq,StemSpec(StemType.MASTER,'M',()),decode_clip_fn=dec)
class Dec:
 _channels=2;_clip_id='c'
 def decode_samples_for_frame(self, f: int, fps: float, num_samples: int) -> np.ndarray | None:return dec(None,None,f,fps,num_samples) if f<2 else None
 def close(self) -> None:pass
m=AudioMixer();m.add_track('T',Dec(),AudioTrackState(track_id='T'));m.set_clip_effects('c',chain().to_dsp_entries())
live=np.concatenate([m.mix_samples(f,FPS,N)[0] for f in range(10)],1)
res['clip_tail_peaks']={'export':float(abs(out).max()),'stem':float(abs(stem).max()),'live':float(abs(live).max())}
# Master/Bus FX automation whole output
const=np.full((2,SR),.2);wav(root/'c.wav',const)
p2=EditProject('p','p',assets={'a':MediaAsset('a',str(root/'c.wav'),'audio','C',{})})
auto=KeyframeTrack(param_name='fx/D/level_db',min_val=-24,max_val=6)
auto.add_keyframe(Keyframe(frame=0,value=0.,interpolation=InterpolationType.HOLD));auto.add_keyframe(Keyframe(frame=10,value=-12.,interpolation=InterpolationType.HOLD))
t2=EditTrack('T','T','audio',0,clips=(ClipInstance('c','a','T',0,25,0,25),))
for location in ('track','bus','master'):
 kw={};tracks=(t2,)
 if location=='master':kw={'master_effect_chain':chain(wet=0),'master_automation':{'fx/D/level_db':auto},'master_automation_mode':'read'}
 elif location=='track':tracks=(EditTrack('T','T','audio',0,clips=t2.clips,effect_chain=chain(wet=0),automation={'fx/D/level_db':auto},automation_mode='read'),)
 else:tracks=(t2.with_output_bus('B'),EditTrack('B','B','audio',1,is_bus=True,effect_chain=chain(wet=0),automation={'fx/D/level_db':auto},automation_mode='read'))
 seq=TimelineSequence(fps=Rational(FPS,1),audio_tracks=tracks,**kw)
 out=TimelineExporter(p2,seq,Mock())._render_audio_master(0,25,TimelineExportConfig(output_path=str(root/'a.mov')),encoder=Mock())
 stem=render_stem_pcm(seq,StemSpec(StemType.MASTER,'M',()),decode_clip_fn=lambda t,c,f,fp,n:np.full((2,n),.2,np.float32))
 res['automation_'+location]={'export_f0':float(out[0,100]),'export_f20':float(out[0,20*N+100]),'stem_f20':float(stem[0,20*N+100]),'expected_f20':.2*10**(-12/20)}
res['future_diagnostic']=collect_chain_diagnostics(EffectChainParams(effects=(TrackEffectInstance('delay',{},algorithm_version=777),)))
# Nonzero selected range must retain the Delay tail originating before In.
seq = TimelineSequence(fps=Rational(FPS, 1), audio_tracks=(t.with_effect_chain(chain()),))
full = TimelineExporter(p, seq, Mock())._render_audio_master(
    0, 10, TimelineExportConfig(output_path=str(root / 'full.mov')), encoder=Mock())
selected = TimelineExporter(p, seq, Mock())._render_audio_master(
    4, 10, TimelineExportConfig(output_path=str(root / 'selected.mov')), encoder=Mock())
res['nonzero_selection'] = {
    'start_frame': 4, 'end_frame': 10,
    'expected_peak_from_full_crop': float(np.max(np.abs(full[:, 4*N:10*N]))),
    'selected_peak': float(np.max(np.abs(selected))),
}
# Compare the actually released v2 source, not a hand-written legacy oracle.
import subprocess, types
from src.features.edit.core import audio_dsp as current_dsp
params = StripEQParams(enabled=True, bands=({
    'enabled': True, 'solo': False, 'index': 1, 'filter_type': 'HPF',
    'freq': 1000.0, 'gain': 0.0, 'q': 0.7071, 'slope': 12,
},))
source = subprocess.check_output(
    ['git', 'show', '302b88b6:src/features/edit/core/audio_dsp.py'], cwd=REPO_ROOT, text=True)
legacy = types.ModuleType('topos_post_review_actual_v2')
sys.modules[legacy.__name__] = legacy
exec(compile(source, 'git:302b88b6:audio_dsp.py', 'exec'), legacy.__dict__)
old_eq = legacy.StripEQFilter(SR)
old_eq.set_params(params)
new_eq = current_dsp.StripEQFilter(SR)
new_eq.set_params(params, algorithm_version=2)
tone = np.stack([np.sin(2*np.pi*250*np.arange(SR)/SR)]*2)
def steady_rms_db(samples: np.ndarray) -> float:
    return float(20*np.log10(np.sqrt(np.mean(samples[:, SR//2:]**2))))
res['v2_compatibility'] = {
    'historical_source_commit': '302b88b6',
    'historical_algorithm_version': legacy.ALGORITHM_VERSION,
    'historical_rms_dbfs': steady_rms_db(old_eq.process(tone.copy())),
    'current_version2_rms_dbfs': steady_rms_db(new_eq.process(tone.copy())),
}
res['probe_git_head'] = subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO_ROOT,text=True).strip()
print(json.dumps(res, indent=2))
temporary.cleanup()
