import sys,pathlib,json
import numpy as np
sys.path.insert(0,'/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02')
from src.features.composite.render.composite_render_output import _convert_for_image_ext
from src.shared.color.color_space_descriptor import HDR10_VIDEO
values=np.array([[[-2.,.5,3.]]],np.float32)
result={}
for ext in ('png','tiff','exr'):
 out=_convert_for_image_ext(values.copy(),ext,output_descriptor=HDR10_VIDEO,render_pass='motion_vector')
 result[ext]={'values':out.tolist(),'dtype':str(out.dtype)}
print(json.dumps(result,indent=2));pathlib.Path('/tmp/topos_composite_aov_repro_20261014.json').write_text(json.dumps(result,indent=2))
