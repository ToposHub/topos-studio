from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import av
import numpy as np

OUT = Path(__file__).resolve().parent
MEDIA = OUT.parents[1] / 'tests' / 'test_videos'
SOURCES = {'h264':'4K_H264.mov','dnxhr':'4K_DNxHR HQX 10bit.mov'}
SAMPLES = (0, 60, 199, 200, 270, 399)

def original_index(index: int) -> int:
    return index + (25 if index < 200 else 75)

def sample_video(path: Path, samples: set[int], full_count: bool) -> tuple[int, dict[int,np.ndarray]]:
    found = {}
    count = 0
    with av.open(str(path)) as container:
        video = container.streams.video[0]
        video.thread_type = 'AUTO'
        for index, frame in enumerate(container.decode(video)):
            count += 1
            if index in samples:
                found[index]=frame.reformat(width=320,height=180,format='rgb24',
                                            src_colorspace='ITU709',dst_colorspace='ITU709').to_ndarray()
            if not full_count and index >= max(samples):
                break
    return count, found

def grade(image: np.ndarray) -> np.ndarray:
    rgb = image.astype(np.float32)/255.0
    luma = rgb @ np.asarray([0.2126,0.7152,0.0722],dtype=np.float32)
    warm = np.asarray([0.028,0.008,-0.022],dtype=np.float32)
    return np.clip((luma[...,None]+1.14*(rgb-luma[...,None])-0.5)*1.12+0.5+warm,0,1)*255

def distance(a: np.ndarray,b: np.ndarray) -> float:
    return round(float(np.mean(np.abs(a.astype(np.float32)-b.astype(np.float32)))),4)

def packet_digests(path: Path) -> list[bytes]:
    with av.open(str(path)) as container:
        return [hashlib.sha256(bytes(p)).digest()
                for p in container.demux(container.streams.video[0]) if p.size]


def compare_dnxhr_packets() -> None:
    source = packet_digests(MEDIA/SOURCES['dnxhr'])
    expected = source[25:225] + source[275:475]
    rendered = packet_digests(OUT/'Resolve_dnxhr_Original_to_dnxhr_1.mov')
    result = {'app':'Resolve','case':'DNxHR HQX original cut export',
              'compared_packets':len(rendered),
              'identical_to_trimmed_source_packets':sum(a == b for a,b in zip(expected,rendered)),
              'exact_trimmed_packet_copy':expected == rendered}
    (OUT/'dnxhr_packet_comparison.json').write_text(json.dumps(result,indent=2)+'\n')
    print(result,flush=True)


def main() -> None:
    cli=argparse.ArgumentParser()
    cli.add_argument('files',nargs='*',type=Path)
    args=cli.parse_args()
    originals={}
    record=[]
    exports=args.files or sorted(p for p in OUT.iterdir() if p.suffix in ('.mov','.mp4'))
    for path in exports:
        _,source,mode,_,codec,_=path.stem.split('_')
        if source not in originals:
            _,originals[source]=sample_video(MEDIA/SOURCES[source],{original_index(i) for i in SAMPLES},False)
        meta=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams',
                                                '-show_format','-of','json',str(path)]))
        srcframes=originals[source]
        count, frames=sample_video(path,set(SAMPLES),True)
        stream=next(s for s in meta['streams'] if s['codec_type']=='video')
        audio=[s for s in meta['streams'] if s['codec_type']=='audio']
        data={key:stream.get(key) for key in ('width','height','avg_frame_rate','duration','bit_rate','codec_name',
                                             'profile','pix_fmt','color_range','color_space','color_transfer','color_primaries')}
        data.update(path=path.name,decoded_frames=count,audio_streams=len(audio),size_bytes=path.stat().st_size,
                    encoder=stream.get('tags',{}).get('encoder'),
                    samples=[{'timeline_frame':i,'source_frame':original_index(i),
                              'mae_to_source':distance(frames[i],srcframes[original_index(i)]),
                              'mae_to_calculated_grade':distance(frames[i],grade(srcframes[original_index(i)]))}
                             for i in SAMPLES if i in frames])
        data['format_ok']=(count==400 and (data['width'],data['height'])==(3840,2160)
            and data['avg_frame_rate']=='25/1' and not audio and
            (data['codec_name'],data['pix_fmt'])==(('h264','yuv420p') if codec=='h264'
                                                else ('dnxhd','yuv422p10le')))
        record.append(data)
        print(path.name,'verified' if data['format_ok'] else 'CHECK',data['decoded_frames'],
              data['pix_fmt'],[(x['timeline_frame'],x['mae_to_source'],x['mae_to_calculated_grade']) for x in data['samples']],flush=True)
    (OUT/'export_verification.json').write_text(json.dumps(record,indent=2)+'\n')
    if not args.files:
        compare_dnxhr_packets()

if __name__=='__main__':
    main()
