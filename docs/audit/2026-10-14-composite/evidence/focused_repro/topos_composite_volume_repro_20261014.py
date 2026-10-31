import sys, tempfile, pathlib, os, json
from types import SimpleNamespace
import numpy as np
sys.path.insert(0,'/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02')
from src.features.composite.nodes.volume3d_node import Volume3DNode
class FakeTexture:
 def __init__(self,glo,data):self.glo=glo;self.pixels=np.frombuffer(data,np.float32).copy()
 def release(self):pass
class FakeGL:
 def __init__(self):self.textures=[]
 def texture(self,size,components,data,dtype):
  tex=FakeTexture(len(self.textures)+1,data);self.textures.append(tex);return tex
with tempfile.TemporaryDirectory(prefix='topos-volume-audit-') as td:
 p=pathlib.Path(td)/'density.npz';np.savez(p,density=np.ones((1,1,1),np.float32))
 n=Volume3DNode();n.parameters.update(file_path=str(p),output_width=1,output_height=1)
 gl=FakeGL();ctx=SimpleNamespace(gl=gl,frame_number=0)
 signature_before=n.runtime_file_signature();a=n._do_evaluate({},ctx,None,render_mode='export');alpha_before=float(gl.textures[-1].pixels[-1])
 np.savez(p,density=np.full((1,1,1),2.,np.float32));st=p.stat();os.utime(p,(st.st_atime,st.st_mtime+2))
 signature_after=n.runtime_file_signature();b=n._do_evaluate({},ctx,None,render_mode='export');alpha_after=float(gl.textures[-1].pixels[-1])
 result={'runtime_file_signature_changed':signature_before!=signature_after,'upload_count_after_replacing_file':len(gl.textures),'before_alpha':alpha_before,'after_alpha':alpha_after,'expected_after_alpha':float(1-np.exp(-2.)),'same_glo':a['result'].texture_id==b['result'].texture_id}
 print(json.dumps(result,indent=2));pathlib.Path('/tmp/topos_composite_volume_repro_20261014.json').write_text(json.dumps(result,indent=2))
