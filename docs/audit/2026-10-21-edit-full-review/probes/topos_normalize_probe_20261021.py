import json,wave
import numpy as np
from src.features.edit.ui.timeline_view import TimelineView
from src.features.edit.core.loudness_analyzer import analyze_loudness,compute_normalize_gain_db
sr=48000
left=.3*np.sin(2*np.pi*440*np.arange(sr*4)/sr)
for label,right in [('inphase',left),('antiphase',-left)]:
 samples=np.stack([left,right])
 path=f'/tmp/topos_{label}_audit.wav'
 with wave.open(path,'wb') as f:
  f.setnchannels(2);f.setsampwidth(2);f.setframerate(sr);f.writeframes((samples.T*32767).astype('<i2').tobytes())
 result=TimelineView._analyze_clip_normalize_gain(object(),path,0,4)
 ref=analyze_loudness(samples.astype(np.float32),sr)
 expected=compute_normalize_gain_db(ref.integrated_lufs,-23)
 print(json.dumps({'case':label,'ui_method_result':result,'actual_stereo_lufs':ref.integrated_lufs,'expected_gain_db':expected},ensure_ascii=False))
