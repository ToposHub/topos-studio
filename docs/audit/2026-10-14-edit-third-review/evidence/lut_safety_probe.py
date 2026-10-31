from __future__ import annotations
import sys,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[4];sys.path.insert(0,str(ROOT))
import numpy as np
from src.features.edit.core.timeline_export import TimelineExporter
from src.features.color.core.the_decompiler import GradingMath
rng=np.random.default_rng(7713)
frame=rng.integers(0,256,size=(2048,3),dtype=np.uint8)
results={'trials':0,'accepted':0,'bad':[]}
for i in range(700):
    params={k:[float(rng.uniform(0,360)),float(rng.uniform(0,1)),float(rng.uniform(-1,1))] for k in ('lift','gamma','gain')}
    ex=TimelineExporter.__new__(TimelineExporter);ex._color_grade_lut_cache={}
    lut=ex._get_independent_grade_lut(params);results['trials']+=1
    if lut is None:continue
    results['accepted']+=1
    fast=np.empty_like(frame)
    for ch in range(3):fast[:,ch]=lut[:,0,2-ch][frame[:,ch]]
    graded=GradingMath.apply_grading(frame.astype(np.float32)/255,params)
    ref=np.rint(np.clip(graded,0,1)*255).astype(np.uint8)
    err=np.abs(fast.astype(np.int32)-ref.astype(np.int32))
    if err.max()>1:
        idx=np.unravel_index(err.argmax(),err.shape)[0]
        results['bad'].append({'params':params,'max_error':int(err.max()),'rgb_input':frame[idx].tolist(),'fast':fast[idx].tolist(),'reference':ref[idx].tolist()})
        if len(results['bad'])>=3:break
print(json.dumps(results,indent=2))
