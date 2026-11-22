from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import DaVinciResolveScript as api

OUT = Path(__file__).resolve().parent

def frame(tc: str) -> int:
    h,m,s,f = map(int,tc.split(':'))
    return ((h*60+m)*60+s)*25 + f

r=api.scriptapp('Resolve')
t=r.GetProjectManager().GetCurrentProject().GetCurrentTimeline()
assert t.SetCurrentTimecode(t.GetStartTimecode())
print('READY',t.GetName(),t.GetCurrentTimecode(),flush=True)
start=time.monotonic()
samples=[]
first=None
last_changed=None
last_frame=None
settle=None
while time.monotonic()-start < 48.0:
    t0=time.monotonic()
    tc=t.GetCurrentTimecode()
    t1=time.monotonic()
    if not tc:
        continue
    f=frame(tc)-t.GetStartFrame() if not samples else frame(tc)-samples[0]['base']
    if f!=last_frame:
        samples.append(dict(t=t1-start, frame=f, tc=tc, rpc_ms=(t1-t0)*1000, base=frame(tc)-f))
        if f>=1 and first is None:
            first=t1
            print('PLAYING',tc,flush=True)
        last_frame=f
        last_changed=t1
    elif first and t1-last_changed > 1.5:
        break
    time.sleep(0.025)
result={'timeline':t.GetName(),'tag':sys.argv[1],'samples':samples}
with (OUT/'resolve_playback.jsonl').open('a') as f:
    f.write(json.dumps(result)+'\n')
print('END',len(samples),samples[-1],flush=True)
