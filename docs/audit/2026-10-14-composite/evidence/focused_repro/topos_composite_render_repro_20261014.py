"""Focused audit reproductions. No product edits; GPU work uses its own worker thread."""
import sys, json, tempfile, pathlib, threading
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
ROOT=pathlib.Path('/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02')
sys.path.insert(0,str(ROOT))
from src.features.composite.render.composite_render_output import CompositeRenderOutput, _convert_for_image_ext
from src.shared.io.file_io import FileIO
from src.shared.color.color_space_descriptor import LINEAR_SCENE, DISPLAY_P3_SRGB
from src.shared.color.color_transform_provider import BuiltinColorProvider
class Config:
    def get(self,key,default=None): return 'builtin' if key=='composite.color_engine' else default
cfg=Config(); evidence={}
bgr=np.array([[[0.0,0.0,1.0]]],np.float32)
actual=_convert_for_image_ext(bgr,'tif',output_descriptor=DISPLAY_P3_SRGB,config_manager=cfg)
correct=(np.clip(BuiltinColorProvider().transform(bgr[...,::-1].copy(),LINEAR_SCENE,DISPLAY_P3_SRGB)[...,::-1],0,1)*65535).astype(np.uint16)
evidence['image_sequence_p3_channel_order']={'actual_bgr16':actual.tolist(),'correct_bgr16':correct.tolist(),'max_error':int(np.abs(actual.astype(int)-correct.astype(int)).max())}
frame=np.full((2,2,3),0.25,np.float32)
class FakeRenderer:
    def __init__(self): self.cleaned=False
    def cleanup(self): self.cleaned=True
with tempfile.TemporaryDirectory(prefix='topos-render-audit-') as tempdir:
    def save(path,img,options=None): pathlib.Path(path).write_bytes(b'audit'); return True
    with patch.object(FileIO,'save_image',side_effect=save):
        out=CompositeRenderOutput(cfg); fake=FakeRenderer(); out._frame_renderer=fake
        out._render_one_frame=lambda **kwargs:frame.copy()
        state={'cancel':False}
        res=out.render_image_sequence({'nodes':[]},str(pathlib.Path(tempdir)/'cancel'),'x','png',0,4,parallel_frames=1,is_cancelled=lambda:state['cancel'],progress_cb=lambda c,t:state.__setitem__('cancel',c>=1))
        evidence['sequence_mid_cancel']={**vars(res),'files':sorted(p.name for p in pathlib.Path(res.output_path).glob('*')),'cleanup_called':fake.cleaned,'renderer_retained':out._frame_renderer is fake}
        out=CompositeRenderOutput(cfg)
        out._render_one_frame=lambda **kwargs:None if kwargs['timeline_frame']==1 else frame.copy()
        res=out.render_image_sequence({'nodes':[]},str(pathlib.Path(tempdir)/'gap'),'x','png',0,2,parallel_frames=1)
        evidence['sequence_missing_frame']={**vars(res),'files':sorted(p.name for p in pathlib.Path(res.output_path).glob('*'))}
        out=CompositeRenderOutput(cfg); calls=[]
        out._render_one_frame=lambda **kwargs:(calls.append(kwargs['timeline_frame']) or frame.copy())
        def factory(*args,**kwargs): raise RuntimeError('GL context unavailable (audit injection)')
        out._make_parallel_render_fn=factory
        res=out.render_image_sequence({'nodes':[]},str(pathlib.Path(tempdir)/'fallback'),'x','png',0,2,parallel_frames=2)
        evidence['sequence_factory_fallback_missing']={**vars(res),'serial_calls':calls}
from src.features.composite.render.composite_export_renderer import CompositeExportRenderer
from src.features.composite.core.graph_snapshot import GraphSnapshot, NodeSnapshot
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.vectorblur_node import VectorBlurNode
from src.features.composite.core.eval_context import CompositeEvalContext
r=CompositeExportRenderer(context=SimpleNamespace(info={}))
r._persistent=True; r.fb_manager=object(); r._resource_pool=object(); r._cache_manager=object()
r._eval_ctx=CompositeEvalContext(gl=r.ctx); r._scheduler=SimpleNamespace(plan=lambda *a,**k:SimpleNamespace(output_dimensions=(1,1),needs_tiling=False))
r._sync_render_nodes=lambda snapshot:None
capture=[]
def evaluate(**kwargs): capture.append((r._eval_ctx.motion_blur_shutter,VectorBlurNode().effective_shutter(r._eval_ctx)));return TextureResult(texture_id=1,width=1,height=1)
r._evaluate_snapshot=evaluate; r._readback=lambda result:np.ones((1,1,3),np.float32)
ns=NodeSnapshot('out','output',{'motion_blur_shutter':0.9},False,[],[])
snapshot=GraphSnapshot(generation=1,nodes={'out':ns})
result=r.render_and_readback(snapshot)
evidence['export_project_shutter']={'requested':0.9,'observed_ctx_and_effective_shutter':capture,'error':result.error}
# Real low-load 8x8 production GPU pool and paint upload, on a dedicated GL worker.
def gpu_checks():
    import moderngl
    from src.shared.gpu.gl_backend import FramebufferManager
    from src.features.composite.render.gpu_resource_pool import GPUResourcePool
    from src.features.composite.evaluators.filter_evaluator import FilterEvaluatorMixin
    from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin
    try:
        ctx=moderngl.create_standalone_context()
        fb=FramebufferManager(ctx); pool=GPUResourcePool(fb,max_memory_mb=16)
        class Harness(FilterEvaluatorMixin, BaseEvaluatorMixin): pass
        h=Harness(); h.ctx=ctx;h.fb_manager=fb;h._resource_pool=pool
        src=np.ones((8,8,4),np.float32); src[...,:3]=0.2;src[...,3]=0.5
        tex=fb.get_temp_texture(8,8,dtype='f4');tex.write(src.tobytes())
        tr=TextureResult(texture_id=tex.glo,width=8,height=8,alpha_premultiplied=False,color_space='linear')
        h._get_texture_by_id=lambda tid:tex if tid==tex.glo else None
        h._rasterize_stroke_mask=lambda stroke,w,hh:np.zeros((hh,w),np.float32)
        params={'output_width':8,'output_height':8,'strokes':[{'type':'solid','opacity':1,'color':[1,0,0,1]}]}
        got=h._apply_paint(tr,params)
        evidence['paint_production_pool']={'passthrough_instead_of_paint':got['result'].texture_id==tr.texture_id,'result_same_object':got['result'] is tr,'pool_stats':pool.get_stats() if hasattr(pool,'get_stats') else {},'source_alpha':0.5}
        ai=h._upload_ai_result_texture(np.ones((8,8,3),np.float32),tr)
        evidence['ai_production_pool_upload']={'returned_none':ai is None}
        # Isolate the alpha math bug using explicit f4 upload targets.
        class F4Pool:
            def acquire_texture(self,w,hh,components=4,dtype='f4'): return fb.get_temp_texture(w,hh,components,dtype='f4')
        h._resource_pool=F4Pool()
        got=h._apply_paint(tr,params)
        outtex=next(t for t in fb._active_textures if t.glo==got['result'].texture_id)
        pixels=np.frombuffer(outtex.read(),np.float32).reshape(8,8,4)
        evidence['paint_straight_alpha_math']={'expected_unpainted_rgba':[0.1,0.1,0.1,0.5],'actual_unpainted_rgba':pixels[0,0].tolist()}
        pool.cleanup();ctx.release()
    except Exception as exc:
        import traceback
        evidence['gpu_exception']={'error':str(exc),'traceback':traceback.format_exc()}
t=threading.Thread(target=gpu_checks,name='audit-tiny-owning-gl-worker');t.start();t.join(30)
if t.is_alive(): evidence['gpu_timeout']=True
print(json.dumps(evidence,indent=2,ensure_ascii=False,default=str))
pathlib.Path('/tmp/topos_composite_render_repro_20261014.json').write_text(json.dumps(evidence,indent=2,ensure_ascii=False,default=str))
