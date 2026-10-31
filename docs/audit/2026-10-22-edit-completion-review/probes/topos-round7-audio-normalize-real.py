from pathlib import Path
import sys,runpy,json
sys.path.insert(0,str(Path.cwd()))
import numpy as np
h=runpy.run_path('tests/edit/test_fix_round6_phase1b_audio.py')
from src.features.edit.ui.timeline_view import _NormalizeGainWorker
tmp=Path('/tmp/topos-round7-audio-data');tmp.mkdir(exist_ok=True)
s=h['_sine'](440,4)
out={}
for name,data in [('inphase',s),('antiphase',s*np.array([[1],[-1]],dtype=np.float32)),('silence',np.zeros_like(s))]:
 p=h['_write_wav'](tmp/f'{name}.wav',data)
 out[name]=_NormalizeGainWorker(str(p),duration_frames=100,source_fps=25).analyze_sync()
print(json.dumps(out,ensure_ascii=False,indent=2))
Path('/tmp/topos-round7-audio-normalize-real.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
