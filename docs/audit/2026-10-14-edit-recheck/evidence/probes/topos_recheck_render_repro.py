"""Pure-CPU probes; no GPU calls/QApplication and no production changes."""
import ast, inspect, textwrap
from typing import Dict, Any
from types import SimpleNamespace
from pathlib import Path
import numpy as np
import cv2
from src.features.color.core.the_decompiler import GradingMath
from src.features.edit.core.timeline_export import TimelineExporter
from src.features.edit.core.timeline_gpu_export_renderer import grade_params_to_uniforms
from src.features.edit.core.timeline_metal_export_renderer import TimelineMetalExportRenderer

root=Path('/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01')
# Load only the test fixture helpers, not module-level GL availability probes.
source=(root/'tests/edit/test_timeline_gpu_export_renderer.py').read_text()
tree=ast.parse(source)
selected=[]
for node in tree.body:
    if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='GRADE_TYPICAL' for t in node.targets): selected.append(node)
    if isinstance(node,ast.FunctionDef) and node.name in ('_make_planes','_make_exporter','_cpu_stack_reference'): selected.append(node)
ns={'np':np,'SimpleNamespace':SimpleNamespace,'TimelineExporter':TimelineExporter}
exec(compile(ast.Module(body=selected,type_ignores=[]),'fixture_helpers','exec'),ns)
# Reconstruct only a diagnostic CPU model of the current export GLSL,
# which omits the post-gamma dark protection and clips hue input.
src=textwrap.dedent(inspect.getsource(GradingMath.apply_grading))
src=src.replace('@staticmethod\n','').replace('def apply_grading(', 'def legacy_grade(')
a=src.index('    # === 暗部保护');b=src.index('    # 5. Shadows',a)
src=src[:a]+src[b:]
src=src.replace('hsv = GradingMath.rgb2hsv(res)', 'hsv = GradingMath.rgb2hsv(np.clip(res,0,1))')
probe_ns={'np':np,'GradingMath':GradingMath,'Dict':Dict,'Any':Any}
exec(src,probe_ns)
legacy=probe_ns['legacy_grade']

def exporter(params):
    stack=SimpleNamespace(grading_params=params,curves_data={},hsl_curve_data={},mixer_params={},lut_path=None,soft_clip_params={'enabled':False},layers=[],active_layer_index=None)
    ex=TimelineExporter.__new__(TimelineExporter);ex._project=SimpleNamespace(color_stacks={'s':stack});ex._color_grade_lut_cache={}
    layer=SimpleNamespace(clip=SimpleNamespace(color_stack_id='s'),clip_id='c',source_frame=0)
    return ex,layer

def ref(frame,p):
    rgb=frame[...,::-1].astype(np.float32)/255
    out=GradingMath.apply_grading(rgb.reshape(-1,3),p).reshape(rgb.shape)
    return np.rint(np.clip(out[...,::-1],0,1)*255).astype(np.uint8)

project_grades=[{'lift':[250.67947667772825,0.7041904101271046,0]}, {'gain':[49.360623248919055,0.375193068518081,0]}]
all_codes=np.arange(256,dtype=np.uint8);ramp=np.stack((all_codes,all_codes[::-1],all_codes),axis=-1)[None]
random=np.random.default_rng(42).integers(0,256,size=(32,256,3),dtype=np.uint8);frame=np.concatenate((ramp,random),axis=0)
for p in project_grades:
    ex,layer=exporter(p);actual=ex._apply_color_grade(frame,layer);expected=ref(frame,p);diff=np.abs(actual.astype(np.int16)-expected.astype(np.int16))
    print('LUT',p,'mismatch/max:',np.count_nonzero(diff),diff.size,int(diff.max()))
    # Literal RGBA shape forces the real export float fallback, same RGB data.
    alpha=np.full(frame.shape[:2]+(1,),255,dtype=np.uint8);fallback=ex._apply_color_grade(np.concatenate((frame,alpha),axis=-1),layer)[...,:3]
    print('  real float fallback equal:',np.array_equal(fallback,expected))
    rgb=frame[...,::-1].astype(np.float32)/255
    old=legacy(rgb.reshape(-1,3),p).reshape(rgb.shape)
    oldu8=np.rint(np.clip(old[...,::-1],0,1)*255).astype(np.uint8)
    d=np.abs(oldu8.astype(np.int16)-expected.astype(np.int16))
    print('  export GLSL no-dark diagnostic mismatch/max:',np.count_nonzero(d),int(d.max()))
    # Independent LUT table cannot represent same-channel values that vary
    # when other input channels change.
    samples=np.array([[0,0,0],[.1,.1,0]],dtype=np.float32)
    print('  same B input0, different R/G, authoritative B outputs:',GradingMath.apply_grading(samples,p)[:,2].tolist())

planes=ns['_make_planes']('yuv422p10le');meta=SimpleNamespace(color_range='limited',color_matrix='bt709');ex=ns['_make_exporter']()
f=np.rint(np.clip(ex._planes_to_bgr_float(planes,'yuv422p10le',meta,None,False),0,1)*255).astype(np.uint8)
rgb=f[...,::-1].astype(np.float32)/255;p=ns['GRADE_TYPICAL']
new=GradingMath.apply_grading(rgb.reshape(-1,3),p).reshape(rgb.shape)
old=legacy(rgb.reshape(-1,3),p).reshape(rgb.shape)
cpu=cv2.resize(np.rint(np.clip(new[...,::-1],0,1)*255).astype(np.uint8),(192,128))
oldcpu=cv2.resize(np.rint(np.clip(old[...,::-1],0,1)*255).astype(np.uint8),(192,128))
diff=np.abs(cpu.astype(np.int16)-oldcpu.astype(np.int16))
print('GPU fixture no-dark diagnostic u8 max/channels:',int(diff.max()),diff.max(axis=(0,1)).tolist(),'first cpu/diagnostic:',cpu[0,0].tolist(),oldcpu[0,0].tolist())
uniforms=grade_params_to_uniforms(p)
print('fixture wheel uniforms:',{k:v for k,v in uniforms.items() if k in ('u_lift','u_gamma','u_gain')})
print('wheel uniform parser == GradingMath:',all(np.allclose(uniforms['u_'+n],GradingMath.params_to_rgb(*p[n])) for n in ('lift','gamma','gain')))
# Source includes representative normalized wheel bounds, demonstrating this
# is not exclusively an out-of-UI-range legacy fixture issue.
valid={'lift':[60,1,0], 'gamma':[0,1,0], 'gain':[0,0,0]}
a=GradingMath.apply_grading(rgb.reshape(-1,3),valid).reshape(rgb.shape);b=legacy(rgb.reshape(-1,3),valid).reshape(rgb.shape)
d=np.abs(np.rint(np.clip(a,0,1)*255).astype(np.int16)-np.rint(np.clip(b,0,1)*255).astype(np.int16))
print('valid wheels no-dark diagnostic u8 mismatch/max:',int(np.count_nonzero(d)),int(d.max()))
# No GPU activation: route policy and cache allocation code are pure/static.
effect={'kind':2.,'half_x':8.,'half_y':8.,'strength':1.}
print('Metal radius8 single-effect materialize:',TimelineMetalExportRenderer._should_materialize_effect_chain((effect,),False))

black=np.zeros((1,3),dtype=np.float32)
print('valid edge wheels black RGB cpu/GL diagnostic u8:',np.rint(np.clip(GradingMath.apply_grading(black,valid),0,1)*255).astype(np.uint8).tolist(),np.rint(np.clip(legacy(black,valid),0,1)*255).astype(np.uint8).tolist())
p=dict(valid,saturation=50.)
hdr=np.array([[1.,1.04,0.]],dtype=np.float32)
solo=GradingMath.apply_grading(hdr,p)[0]
batched=GradingMath.apply_grading(np.concatenate((hdr,black),axis=0),p)[0]
print('CPU dark protection batch dependence HDR samepixel alone/with black:',solo.tolist(),batched.tolist(),'maxfloatdiff:',float(np.max(np.abs(solo-batched))))
print('  final samepixel u8 alone/with black:',np.rint(np.clip(solo,0,1)*255).astype(np.uint8).tolist(),np.rint(np.clip(batched,0,1)*255).astype(np.uint8).tolist())
f16=ex._convert_yuv_planes_to_bgr16(planes,'yuv422p10le',meta);rgb16=f16[...,::-1].astype(np.float32)/65535.
a16=GradingMath.apply_grading(rgb16.reshape(-1,3),ns['GRADE_TYPICAL']).reshape(rgb16.shape)
b16=legacy(rgb16.reshape(-1,3),ns['GRADE_TYPICAL']).reshape(rgb16.shape)
c16=cv2.resize(np.rint(np.clip(a16[...,::-1],0,1)*65535).astype(np.uint16),(192,128));o16=cv2.resize(np.rint(np.clip(b16[...,::-1],0,1)*65535).astype(np.uint16),(192,128))
print('GPU fixture no-dark diagnostic u16 max:',int(np.abs(c16.astype(np.int32)-o16.astype(np.int32)).max()))
img=np.rint(np.clip(ex._planes_to_bgr_float(ns['_make_planes']('yuv420p',96,64),'yuv420p',meta,None,False),0,1)*255).astype(np.uint8)
img=cv2.resize(img,(192,128)).astype(np.float32)/255
cs=np.rint(np.clip(cpu.astype(np.float32)/255*.35+img*.65,0,1)*255).astype(np.uint8)
os=np.rint(np.clip(oldcpu.astype(np.float32)/255*.35+img*.65,0,1)*255).astype(np.uint8)
print('GPU fixture no-dark diagnostic stack max:',int(np.abs(cs.astype(np.int16)-os.astype(np.int16)).max()))
