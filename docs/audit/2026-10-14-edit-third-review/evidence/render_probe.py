from __future__ import annotations
import sys,json,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[4]
sys.path.insert(0,str(ROOT))
import numpy as np
from types import SimpleNamespace
from src.features.edit.core.timeline_export import TimelineExporter
from src.features.color.core.the_decompiler import GradingMath
result={'numpy':np.__version__}
for label,params in [('identity',{}),('gain',{'gain':[49.360623248919055,0.375193068518081,0]})]:
    ex=TimelineExporter.__new__(TimelineExporter);ex._color_grade_lut_cache={}
    calls=[]
    for i in range(3):
        try:
            lut=ex._get_independent_grade_lut(params)
            calls.append({'call':i+1,'lut':lut is not None,'cache_entries':len(ex._color_grade_lut_cache)})
        except Exception as e:calls.append({'call':i+1,'error':type(e).__name__,'message':str(e)})
    result[label]=calls
print(json.dumps(result,indent=2))
# 真实_apply_color_grade连续帧路径，不只直接调用缓存helper。
params={'gain':[49.360623248919055,0.375193068518081,0]}
def real_ex(params):
    stack=SimpleNamespace(grading_params=params,curves_data={},hsl_curve_data={},mixer_params={},lut_path=None,soft_clip_params={'enabled':False},layers=[],active_layer_index=None)
    ex=TimelineExporter.__new__(TimelineExporter);ex._project=SimpleNamespace(color_stacks={'stack':stack});ex._color_grade_lut_cache={}
    layer=SimpleNamespace(clip=SimpleNamespace(color_stack_id='stack'),clip_id='clip',source_frame=0)
    return ex,layer
ex,layer=real_ex(params);calls=[]
for i in range(3):
    try:calls.append({'call':i+1,'actual':ex._apply_color_grade(np.array([[[59,140,112]]],dtype=np.uint8),layer).tolist()})
    except Exception as e:calls.append({'call':i+1,'error':type(e).__name__,'message':str(e)})
safety=json.loads((Path(__file__).parent/'lut_safety_probe.json').read_text())['bad'][0]
ex,layer=real_ex(safety['params']);bgr=np.array([[safety['rgb_input'][::-1]]],dtype=np.uint8)
actual=ex._apply_color_grade(bgr,layer);expected=np.rint(np.clip(GradingMath.apply_grading(bgr[:,:,::-1].astype(np.float32).reshape(-1,3)/255,safety['params']),0,1)*255).astype(np.uint8)
(Path(__file__).parent/'render_real_path.json').write_text(json.dumps({'continuous_frames':calls,'safety_actual_rgb':actual[0,0,::-1].tolist(),'safety_reference_rgb':expected[0].tolist(),'safety_params':safety['params']},indent=2))
