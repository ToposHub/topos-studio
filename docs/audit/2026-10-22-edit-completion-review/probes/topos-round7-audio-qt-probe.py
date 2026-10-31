from pathlib import Path
import sys, time, json
sys.path.insert(0,str(Path.cwd()))
import numpy as np
from PyQt6.QtCore import QThread, QTimer, QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication
from src.features.edit.ui.timeline_view import _NormalizeGainWorker
from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState
from src.features.edit.core.audio_dsp import EffectChain
app=QApplication([])
out={}
worker=_NormalizeGainWorker('/unused.wav')
thread=QThread(); worker.moveToThread(thread)
class Emitter(QObject):
 cancel=pyqtSignal()
emitter=Emitter();emitter.cancel.connect(worker.cancel)
start=time.monotonic(); states=[]
def analyze(cancel_check=None):
 for _ in range(100):
  if cancel_check():
   states.append(('worker_saw_cancel',time.monotonic()-start));break
  time.sleep(.01)
 return True,'done',None
worker.analyze_sync=analyze
thread.started.connect(worker.run)
worker.finished.connect(thread.quit)
thread.finished.connect(app.quit)
def cancel():
 emitter.cancel.emit()
 states.append(('after_cancel_emit',time.monotonic()-start,worker._cancelled))
QTimer.singleShot(100,cancel)
thread.start();app.exec();thread.wait()
out['worker_cancel_signal']=states
out['worker_cancel_final']=worker._cancelled
class DC:
 _clip_id='c'
 _gain_db=0
 source_layout=None
 def decode_samples_for_frame(self,*args):return np.full((2,args[-1]),.1,dtype=np.float32)
 def close(self):pass
m=AudioMixer();m.add_track('t',DC(),AudioTrackState('t'))
sampled=[]
def sampler(frame):
 sampled.append(frame)
 return [('saturation',{'enabled':True,'drive':100},True)]
m.set_clip_effects('c',[('saturation',{'enabled':True,'drive':0},True)],track_id='t',animated_sampler=sampler)
audio,_=m.mix_samples(10,25,1920)
direct=EffectChain(48000);direct.set_params(sampler(10))
expected=direct.process(np.full((2,1920),.1,dtype=np.float32))
out['inactive_fx_gate']={'sampler_calls_during_mix':len(sampled)-1,'actual_peak':float(audio.max()),'expected_peak':float(expected.max())}
print(json.dumps(out,indent=2))
Path('/tmp/topos-round7-audio-qt-results.json').write_text(json.dumps(out,indent=2))
