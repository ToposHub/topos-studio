from __future__ import annotations

import json
import statistics
import subprocess
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]

def lines(name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (OUT/name).read_text().splitlines() if line]

def tc_frames(tc: str) -> int:
    h,m,s,f = map(int,tc.split(':'))
    return ((h*60+m)*60+s)*25+f

def present_report(item: dict[str, Any]) -> dict[str, Any]:
    frames={f:t for t,f in reversed(item['frames']) if 0 <= f < 400}
    times=sorted(frames.values())
    gaps=[b-a for a,b in zip(times,times[1:])]
    return {'source':item['source'],'graded':item['graded'],'repeat':item['repeat'],
        'wall_s':round(item['wall_s'],3),'timeline_frames':400,'unique_presented':len(frames),
        'unobserved_timeline_frames':400-len(frames),
        'viewer_present_fps':round(len(frames)/item['wall_s'],3),
        'present_gap_p95_ms':round(statistics.quantiles(gaps,n=20)[-1]*1000,3),
        'decode_paths':item['trace'].get('decode_path_counts',{}),
        'render_paths':item['trace'].get('render_path_counts',{})}

def top_export(item: dict[str, Any]) -> dict[str, Any]:
    telem=item['stage_telemetry'] or {}
    stages={s['name']:{k:s[k] for k in ('backend','sample_count','wall_p50_ms','wall_p95_ms')}
            for s in telem.get('stages',[]) if s['sample_count']}
    return {k:item[k] for k in ('source','graded','codec','repeat','wall_s','file','success')} | {
        'fps':400/item['wall_s'],'stages':stages,'fallback_root_cause':telem.get('fallback_root_cause')}

def main() -> None:
    observations=json.loads((OUT/'resolve_playback_observations.json').read_text())
    start=tc_frames(observations['timed_transport_start'])
    for trial in observations['runs']:
        advance=tc_frames(trial['end_tc'])-start
        trial.update(playhead_advanced_frames=advance, playhead_frames_per_wall_s=advance/trial['duration_s'])
    git_head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    metadata={'date_local':'2026-11-22','timezone':'Asia/Shanghai','git_head':git_head,
        'main_worktree_has_preexisting_changes':True,'resolve_version':'Studio 21.2.0.6',
        'macOS':'15.3.1','CPU':'Intel Core i9-9900K','GPU':'AMD Radeon RX 6900 XT 16 GB','RAM_GiB':32,
        'geometry':[3840,2160],'timeline_fps':25,'timeline_frames':400,'audio':False,
        'source_ranges_end_exclusive':[[25,225],[275,475]],'lut':'Topos_Resolve_AB_33.cube',
        'sources':{'h264':'4K_H264.mov','dnxhr':'4K_DNxHR HQX 10bit.mov'},
        'resolve_render_cache':'none','resolve_optimized_media':False,'resolve_proxy':False,
        'topos_render_cache':'off','topos_viewer_budget_actual_decode_target':[1792,1008],
        'topos_viewer_quality_requested':'Full','topos_adaptive_quality':False,
        'topos_export_graphics':'opengl','h264_target_bps':50_000_000}
    top=[top_export(item) for item in lines('topos_exports.jsonl')]
    resolve=[]
    for item in lines('resolve_exports.jsonl'):
        resolve.append({k:item[k] for k in ('source','graded','codec','repeat','wall_s','file','status')} | {
            'fps':400/item['wall_s']})
    verified=json.loads((OUT/'export_verification.json').read_text())
    data={'metadata':metadata,'resolve_playback':observations,
          'topos_playback':[present_report(item) for item in lines('topos_playback.jsonl')],
          'resolve_exports':resolve,'topos_exports':top,
          'export_verification':verified,
          'excluded_trials':{
             'resolve_playback_initial':'No valid presentation count and initial attempts did not have clean start/stop.',
             'topos_h264_LUT_to_h264_1':'Pilot overlapping Resolve UI activity. The output is available and verified.'}}
    for supplemental in ('dnxhr_packet_comparison','cpu_decode_reference'):
        path=OUT/(supplemental+'.json')
        if path.exists():
            data[supplemental]=json.loads(path.read_text())
    assert all(item['format_ok'] for item in verified)
    (OUT/'results.json').write_text(json.dumps(data,indent=2)+'\n')
    for item in top:
        t=item['wall_s']
        peer=[r['wall_s'] for r in resolve if (r['source'],r['graded'],r['codec']) ==
              (item['source'],item['graded'],item['codec'])]
        r=statistics.mean(peer)
        print(item['file'], f'Topos {t:.3f}s ({400/t:.2f} fps) | Resolve {r:.3f}s ({400/r:.2f} fps) | {t/r:.2f}x', item['stages'])

if __name__ == '__main__':
    main()
