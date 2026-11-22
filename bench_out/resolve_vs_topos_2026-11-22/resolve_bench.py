from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import DaVinciResolveScript as api

OUT = Path(__file__).resolve().parent
MEDIA = OUT.parents[1] / 'tests' / 'test_videos'
FILES = {'h264': '4K_H264.mov', 'dnxhr': '4K_DNxHR HQX 10bit.mov'}
RANGES = [(25, 225), (275, 475)]  # [in,out)
LUT = OUT / 'Topos_Resolve_AB_33.cube'
PROJECT = 'Topos vs Resolve UHD25 2026-11-22'
SETTINGS = {
    'timelineResolutionWidth': '3840', 'timelineResolutionHeight': '2160',
    'timelineFrameRate': '25', 'timelinePlaybackFrameRate': '25',
    'colorScienceMode': 'davinciYRGB', 'perfRenderCacheMode': 'none',
    'perfAutoRenderCacheEnable': '0', 'perfOptimisedMediaOn': '0',
    'perfProxyMediaMode': '0', 'perfProxyResolutionRatio': 'original',
}

def check(result, operation):
    if not result:
        raise RuntimeError(f'{operation} failed: {result}')
    return result

def make_cube():
    with LUT.open('w', encoding='ascii') as f:
        f.write('TITLE "UHD25 - Warm Contrast"\nLUT_3D_SIZE 33\nDOMAIN_MIN 0.0 0.0 0.0\nDOMAIN_MAX 1.0 1.0 1.0\n')
        for b in range(33):
            for g in range(33):
                for r in range(33):
                    rgb = [r / 32, g / 32, b / 32]
                    luma = sum(v * w for v, w in zip(rgb, (0.2126, 0.7152, 0.0722)))
                    warm = (0.028, 0.008, -0.022)
                    result = [min(1, max(0, (luma + 1.14 * (v - luma) - 0.5) * 1.12 + 0.5 + bias)) for v, bias in zip(rgb, warm)]
                    f.write(' '.join(f'{v:.7f}' for v in result) + '\n')

def timeline_of(project, src, graded):
    name = f'{src.upper()} - {"LUT" if graded else "Original"}'
    for i in range(1, project.GetTimelineCount() + 1):
        tl = project.GetTimelineByIndex(i)
        if tl.GetName() == name:
            return tl
    raise RuntimeError(f'{name} not found')

def append(pool, source, timeline, exclusive):
    start = timeline.GetStartFrame()
    for index, (in_frame, out_frame) in enumerate(RANGES):
        clip = check(pool.AppendToTimeline([{
            'mediaPoolItem': source, 'startFrame': in_frame,
            'endFrame': out_frame if exclusive else out_frame - 1,
            'recordFrame': start + index * 200, 'mediaType': 1, 'trackIndex': 1
        }]), 'AppendToTimeline')[0]
        if clip.GetDuration() != 200:
            check(timeline.DeleteClips([clip], False), 'delete unexpected benchmark test clip')
            return append(pool, source, timeline, not exclusive)
        print('clip', clip.GetName(), clip.GetStart(), clip.GetEnd(), clip.GetDuration(),clip.GetSourceStartFrame(),clip.GetSourceEndFrame(),flush=True)

def setup(resolve, pm):
    make_cube()
    cur = pm.GetCurrentProject()
    previous = cur.GetName() if cur else None
    if previous != PROJECT:
        if cur:
            check(pm.SaveProject(), 'save active project before switching')
        (OUT / 'original_project.txt').write_text(previous or '', encoding='utf-8')
    p = cur if previous == PROJECT else check(pm.CreateProject(PROJECT), 'create isolated Resolve project')
    print('Resolve',resolve.GetProductName(),resolve.GetVersionString(),flush=True)
    for key, val in SETTINGS.items():
        print(key, p.SetSetting(key, val), p.GetSetting(key), flush=True)
    pool = p.GetMediaPool()
    sources = {}
    for src, name in FILES.items():
        sources[src] = check(pool.ImportMedia([str(MEDIA / name)]), f'import {name}')[0]
    lut_home = Path('/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT')
    placed_lut = lut_home / LUT.name
    if not placed_lut.exists():
        shutil.copyfile(LUT, placed_lut)
    if placed_lut.read_bytes() != LUT.read_bytes():
        raise RuntimeError('Existing LUT contents conflict')
    p.RefreshLUTList()
    for src, source in sources.items():
        try:
            tl = timeline_of(p, src, False)
        except RuntimeError:
            tl = check(pool.CreateEmptyTimeline(f'{src.upper()} - Original'), 'create original timeline')
        check(p.SetCurrentTimeline(tl), 'set current timeline')
        if not tl.GetItemListInTrack('video', 1):
            append(pool, source, tl, False)
        try:
            graded = timeline_of(p, src, True)
        except RuntimeError:
            graded = check(tl.DuplicateTimeline(f'{src.upper()} - LUT'), 'duplicate graded timeline')
        check(p.SetCurrentTimeline(graded),'set graded timeline')
        check(resolve.OpenPage('color'), 'open Color page')
        for clip in graded.GetItemListInTrack('video', 1):
            graph = check(clip.GetNodeGraph(),'get clip grade graph')
            print('nodes', graph.GetNumNodes(), flush=True)
            check(graph.SetLUT(1, LUT.name), 'apply cube')
            print('LUT',graph.GetLUT(1), flush=True)
            graph.SetNodeLabel(1,'Shared 33 cube grade')
    check(pm.SaveProject(),'save Resolve test project')
    check(p.SetCurrentTimeline(timeline_of(p,'h264',False)), 'select first timeline')
    check(resolve.OpenPage('edit'),'open Edit')

def render(p, src, graded, codec, repeat):
    tl = timeline_of(p, src, graded)
    check(p.SetCurrentTimeline(tl),'select render timeline')
    mode = 'LUT' if graded else 'Original'
    name = f'Resolve_{src}_{mode}_to_{codec}_{repeat}'
    fmt, enc = ('mp4','H264') if codec == 'h264' else ('mov','DNxHRHQX_10')
    check(p.SetCurrentRenderMode(1), 'single clip render')
    check(p.SetCurrentRenderFormatAndCodec(fmt, enc),f'set render format {fmt}/{enc}')
    s = dict(SelectAllFrames=True, TargetDir=str(OUT), CustomName=name,
             ExportVideo=True, ExportAudio=False, FormatWidth=3840, FormatHeight=2160,
             FrameRate=25.0, ColorSpaceTag='Rec.709', GammaTag='Rec.709')
    if codec == 'h264':
        s.update(VideoQuality=50_000, EncodingProfile='High', NetworkOptimization=False, MultiPassEncode=False)
    if not p.SetRenderSettings(s):
        for key, val in s.items():
            print('render setting', key, repr(val), p.SetRenderSettings({key:val}), flush=True)
        check(p.SetRenderSettings({k:s[k] for k in ('TargetDir','CustomName','SelectAllFrames','ExportVideo','ExportAudio')}),'set essential render parameters')
    job = check(p.AddRenderJob(),'add job')
    print('START',name,job,flush=True)
    start = time.monotonic()
    check(p.StartRendering([job]),'start render')
    statuses=[]
    while p.IsRenderingInProgress():
        status = p.GetRenderJobStatus(job)
        statuses.append({'elapsed_s':time.monotonic()-start, **status})
        time.sleep(0.15)
    wall = time.monotonic()-start
    status = p.GetRenderJobStatus(job)
    record = dict(app='Resolve', name=name, source=src, codec=codec, graded=graded, repeat=repeat,
                  wall_s=wall, status=status, statuses=statuses, file=f'{name}.{fmt}', config=s,
                  job=next((j for j in p.GetRenderJobList() if j.get('JobId')==job), {}))
    with (OUT/'resolve_exports.jsonl').open('a') as f:
        f.write(json.dumps(record, default=str)+'\n')
    print('END', name, round(wall,3),status, flush=True)
    check(status.get('JobStatus')=='Complete','render completion')

def main():
    cli=argparse.ArgumentParser()
    cli.add_argument('action',choices=['setup','select','render','probe','archive'])
    cli.add_argument('--source',choices=FILES,default='h264')
    cli.add_argument('--graded',action='store_true')
    cli.add_argument('--codec',choices=['h264','dnxhr'],default='h264')
    cli.add_argument('--repeat',type=int,default=1)
    cli.add_argument('--page',default='edit')
    args=cli.parse_args()
    r=check(api.scriptapp('Resolve'),'resolve connection')
    pm=check(r.GetProjectManager(),'project manager')
    if args.action=='setup':
        setup(r,pm)
        return
    p=check(pm.GetCurrentProject(),'active project')
    if p.GetName()!=PROJECT:
        p=check(pm.LoadProject(PROJECT),'load isolated Resolve project')
    if args.action=='select':
        tl=timeline_of(p,args.source,args.graded)
        check(p.SetCurrentTimeline(tl),'set timeline')
        check(r.OpenPage(args.page),'open selected page')
        check(tl.SetCurrentTimecode(tl.GetStartTimecode()),'seek to the start')
        print(tl.GetName(), tl.GetStartTimecode(), tl.GetStartFrame(), tl.GetEndFrame())
    elif args.action=='render':
        check(r.OpenPage('deliver'),'open Deliver')
        render(p,args.source,args.graded,args.codec,args.repeat)
    elif args.action=='probe':
        tl=p.GetCurrentTimeline()
        print('project', p.GetName(),'product',r.GetProductName(),'page',r.GetCurrentPage())
        print('timeline',tl.GetName(), tl.GetCurrentTimecode())
        print('settings',{k:p.GetSetting(k) for k in SETTINGS})
        print('video clips',[(c.GetStart(), c.GetEnd(), c.GetDuration(),c.GetSourceStartFrame(), c.GetSourceEndFrame(), c.GetNodeGraph().GetLUT(1)) for c in tl.GetItemListInTrack('video',1)])
    elif args.action=='archive':
        check(pm.SaveProject(),'save test project')
        check(pm.ExportProject(PROJECT,str(OUT/'Resolve_UHD25_AB.drp'),True),'export test project')
        print('exported Resolve_UHD25_AB.drp')

if __name__=='__main__':
    main()
