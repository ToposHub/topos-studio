import sys, math, json, importlib.util
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import av
import numpy as np
from PIL import Image, ImageDraw
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QPointF
from src.features.color.ui.color_wheel import GradingPanel
from src.features.color.core.the_decompiler import GradingMath
from src.features.edit.core.timeline_gpu_export_renderer import TimelineGpuExportRenderer, grade_params_to_uniforms
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import DARK_THEME

out=Path('.testrun/primary_wheel_redesign');out.mkdir(exist_ok=True)
av.logging.restore_default_callback()
app=QApplication([]); app.setStyleSheet(DARK_THEME); I18N.load_locale('zh_CN')
panel=GradingPanel();panel.resize(760,392);panel.show();app.processEvents()
panel.grab().save(str(out/'panel.png'))
video='tests/test_videos/2K_h264.mp4'
frames=[]
with av.open(video) as container:
 stream=container.streams.video[0]
 for index,frame in enumerate(container.decode(stream)):
  if index in (0,30,60):frames.append((index,frame.to_ndarray(format='bgr24')))
  if index>=60:break
cases=[]
for mode in ('lift','gamma','gain'):
 wheel=getattr(panel,mode+'_wheel')
 for hue in range(0,360,60):
  panel.reset_all()
  center,radius=wheel._wheel_canvas._geometry()
  angle=math.radians(hue+120.0)
  wheel._wheel_canvas._update_knob(center+QPointF(math.cos(angle),-math.sin(angle))*radius*0.12)
  wheel._on_value_interaction_ended()
  params=panel.get_current_params()
  assert abs((params[mode][0]-hue+180)%360-180)<1e-5
  cases.append((mode,hue,params))
panel.reset_all()

def render_cases():
 renderer=TimelineGpuExportRenderer()
 results=[]; previews={}
 try:
  for index,frame in frames:
   for mode,hue,params in cases:
    actual=renderer.render_packed_bgr(frame,grade_params_to_uniforms(params),frame.shape[1],frame.shape[0],1.0)
    if actual is None:raise RuntimeError('Production OpenGL renderer unavailable')
    rgb=frame[...,::-1].astype(np.float32)/255.0
    expected=GradingMath.apply_grading(rgb.reshape(-1,3),params).reshape(rgb.shape)
    expected=np.rint(np.clip(expected[...,::-1],0,1)*255.0).astype(np.uint8)
    error=np.abs(actual.astype(np.int16)-expected.astype(np.int16))
    max_error=int(error.max())
    assert max_error<=1,(index,mode,hue,max_error)
    diff=(actual.astype(np.float32)-frame.astype(np.float32))[...,::-1].mean(axis=(0,1))
    # Test relative channel changes on this real, non-neutral image.
    from src.features.color.core.composition_uniforms import params_to_rgb
    factors=np.array(params_to_rgb(hue,.12,0.0))
    assert np.all(diff[factors>1]>0) and np.all(diff[factors<1]<0),(index,mode,hue,diff)
    results.append({'frame':index,'wheel':mode,'hue':hue,'size':[frame.shape[1],frame.shape[0]],'gpu_cpu_max_u8_error':max_error,'mean_rgb_delta':diff.tolist()})
    if index==0 and hue in (0,120,240):previews[(mode,hue)]=actual
   print('validated frame',index,frame.shape,flush=True)
  return results,previews
 finally:renderer.release()

with ThreadPoolExecutor(max_workers=1,thread_name_prefix='primary-grade-validation') as pool:
 results,previews=pool.submit(render_cases).result()
report={'video':video,'source_frames':[i for i,_ in frames],'cases':len(results),'gpu_cpu_max_u8_error':max(r['gpu_cpu_max_u8_error'] for r in results),'results':results}
(out/'video_validation.json').write_text(json.dumps(report,indent=2))
base=frames[0][1]
contact=Image.new('RGB',(4*360,3*228),'#252529');d=ImageDraw.Draw(contact)
for row,mode in enumerate(('lift','gamma','gain')):
 for col,hue in enumerate((None,0,120,240)):
  frame=base if hue is None else previews[(mode,hue)]
  im=Image.fromarray(frame[...,::-1]).resize((360,202))
  contact.paste(im,(col*360,row*228+24))
  d.text((col*360+8,row*228+6),mode+' / '+('original' if hue is None else {0:'red',120:'green',240:'blue'}[hue]),fill='#eeeeef')
contact.save(out/'real_video_directions.jpg',quality=92)
print(json.dumps({k:v for k,v in report.items() if k!='results'}),flush=True)
panel.close()
