"""Read-only real media measurements through the production frame renderer.

Run after the isolated suite has finished to avoid benchmark contention.
This measures offscreen frame rendering/readback, not GUI displayed FPS.
"""
from __future__ import annotations
import json
import hashlib
import math
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

def percentile(values: list[float], p: float) -> float | None:
    return sorted(values)[max(0, math.ceil(len(values)*p)-1)] if values else None

def worker(media: Path, profile: str = 'baseline') -> None:
    os.environ.update(QT_QPA_PLATFORM='offscreen', TOPOS_HEADLESS_GL='1', TOPOS_TEST_MODE='1', OPENBLAS_NUM_THREADS='1', OPENCV_IO_ENABLE_OPENEXR='1')
    import numpy as np
    import psutil
    from src.features.composite.render.composite_frame_renderer import CompositeFrameRenderer
    probes=json.loads((OUT/'media_probe.json').read_text())
    probe=next(r for r in probes if Path(r['file']).name==media.name)
    meta=next(s for s in probe['probe']['streams'] if s.get('codec_type')=='video')
    w,h=meta['width'],meta['height']
    num,den=map(int,meta['avg_frame_rate'].split('/'))
    graph={'composite_id':'audit-'+media.stem,'fps':num/den,'width':w,'height':h,'duration_frames':int(meta['nb_frames']),
        'nodes':[{'id':'v','type':'video_frame','position':[0,0],'parameters':{'file_path':str(media),'cache_frames':1,'video_width':w,'video_height':h}}, {'id':'out','type':'output','position':[400,0],'parameters':{'format_mode':'custom','width':w,'height':h}}],
        'connections':[{'src_node_id':'v','src_port_name':'image','dst_node_id':'out','dst_port_name':'image'}]}
    renderer=CompositeFrameRenderer()
    if profile == 'grade_blur_merge':
        graph['composite_id'] += '-grade-blur-merge'
        graph['nodes'].extend([
            {'id':'g','type':'grade','position':[100,0],'parameters':{'multiply':1.1}},
            {'id':'b','type':'blur','position':[200,0],'parameters':{'blur_type':'gaussian','radius_x':24.0,'radius_y':24.0}},
            {'id':'c','type':'constant','position':[200,100],'parameters':{'width':w,'height':h,'color_r':0.1,'color_g':0.2,'color_b':0.3,'color_a':1.0}},
            {'id':'m','type':'merge','position':[300,0],'parameters':{'mix':0.85}},
        ])
        graph['connections']=[
            {'src_node_id':src,'src_port_name':port,'dst_node_id':dst,'dst_port_name':inp}
            for src,port,dst,inp in [('v','image','g','image'),('g','result','b','image'),('b','result','m','A'),('c','result','m','B'),('m','result','out','image')]]
    rows=[]
    started=time.perf_counter()
    process=psutil.Process()
    peak=process.memory_info().rss
    cleanup_error=None
    try:
        for quality,frames in [('preview',list(range(15))+[100,240,10,400]),('export',[3,4,5,6])]:
            for index,frame in enumerate(frames):
                t0=time.perf_counter()
                arr=renderer.render_comp_clip_frame(graph_data=graph,timeline_frame=frame,source_frame=frame,quality=quality)
                dt=(time.perf_counter()-t0)*1000
                row={'quality':quality,'frame':frame,'ms':dt,'phase':'cold' if quality=='preview' and index==0 else ('warmup' if quality=='preview' and index<3 else ('seek' if quality=='preview' and index>=15 else 'sequential')),'ok':arr is not None,'trace':renderer.last_frame_trace,'diagnostics':renderer.last_diagnostics}
                node=getattr(renderer._export_renderer, '_render_nodes', {}).get('v')
                row['decoded_frame']=getattr(node, '_current_frame', None)
                if arr is not None:
                    row.update(shape=list(arr.shape),dtype=str(arr.dtype),mean=float(arr.mean()),std=float(arr.std()),finite=bool(np.isfinite(arr).all()),hash=hashlib.blake2b(arr.tobytes(),digest_size=8).hexdigest())
                rows.append(row)
                peak=max(peak,process.memory_info().rss)
                if arr is None: break
    except Exception as exc:
        rows.append({'exception':f'{type(exc).__name__}: {exc}'})
    finally:
        try: renderer.cleanup()
        except Exception as exc: cleanup_error=f'{type(exc).__name__}: {exc}'
    summary={}
    for quality,phase in [('preview','sequential'),('preview','seek'),('export','sequential')]:
        samples=[r['ms'] for r in rows if r.get('quality')==quality and r.get('phase')==phase and r.get('ok')]
        if samples:
            import statistics
            summary[quality+'_'+phase]={'n':len(samples),'median_ms':statistics.median(samples),'p95_ms':percentile(samples,.95),'max_ms':max(samples),'render_throughput_fps':1000/statistics.mean(samples)}
    result={'file':str(media.relative_to(ROOT)),'profile':profile,'codec':meta.get('codec_name','TOPOS/custom'),'resolution':[w,h],'source_fps':num/den,'wall_s':time.perf_counter()-started,'peak_sampled_rss_mib':peak/1024**2,'cleanup_error':cleanup_error,'summary':summary,'rows':rows}
    suffix='' if profile=='baseline' else '.'+profile
    path=OUT/'media_bench'/(media.stem+suffix+'.json')
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str))
    print(media.name,json.dumps(summary),flush=True)

def main() -> None:
    if len(sys.argv)>1:
        worker(Path(sys.argv[1]).resolve(), sys.argv[2] if len(sys.argv)>2 else 'baseline'); return
    logs=OUT/'media_bench'
    logs.mkdir(exist_ok=True)
    results=[]
    cases=[(m,'baseline') for m in sorted((ROOT/'tests/test_videos').iterdir()) if m.suffix.lower() in ('.mov','.mp4')]
    cases.extend((ROOT/'tests/test_videos'/name,'grade_blur_merge') for name in ['2K_H265.mov','4K_H265.mov','4K_prores4444.mov','4K_topos_422_Proxy.mov'])
    for media,profile in cases:
        command=[sys.executable,str(Path(__file__).resolve()),str(media),profile]
        try:
            r=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,timeout=120)
            suffix='' if profile=='baseline' else '.'+profile
            (logs/(media.stem+suffix+'.log')).write_text(r.stdout+r.stderr)
            row={'file':media.name,'profile':profile,'returncode':r.returncode}
        except subprocess.TimeoutExpired:
            row={'file':media.name,'profile':profile,'returncode':'timeout'}
        results.append(row)
        (OUT/'media_benchmark_runs.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
        print(row,flush=True)

if __name__=='__main__': main()
