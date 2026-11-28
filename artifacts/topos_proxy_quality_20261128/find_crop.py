#!/usr/bin/env python3
"""Find cropped screenshot views within the local 4K DNxHR reference."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from measure import OUT, SOURCES, WIDTH, HEIGHT, av_frames, source_path

W, H = 960, 540
PREVIEW_FPS = 2.5
cv2.setNumThreads(2)
sift = cv2.SIFT_create(nfeatures=1300, contrastThreshold=0.015)
matcher = cv2.BFMatcher()


def gray_resize(reference: Path) -> np.ndarray:
    raw = np.asarray(Image.open(reference).convert('L'))
    scale = min(1.0, 780 / max(raw.shape))
    return cv2.resize(raw, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)


def compare(frame, templates):
    frame_kp, frame_desc = sift.detectAndCompute(frame, None)
    if frame_desc is None:
        return [None] * len(templates)
    rows = []
    for (shape, src_kp, src_desc) in templates:
        good = [m for m,n in matcher.knnMatch(src_desc, frame_desc,k=2)
                if m.distance < 0.76*n.distance]
        if len(good) < 10:
            rows.append(None)
            continue
        lhs = np.float32([src_kp[m.queryIdx].pt for m in good])
        rhs = np.float32([frame_kp[m.trainIdx].pt for m in good])
        transform, mask = cv2.estimateAffinePartial2D(
            lhs, rhs, method=cv2.RANSAC, ransacReprojThreshold=2.5)
        if transform is None:
            rows.append(None)
            continue
        h,w=shape
        area=np.float32([[0,0],[w,0],[w,h],[0,h]]).reshape(1,-1,2)
        corners=cv2.transform(area, transform)[0] * 4
        rows.append(dict(inliers=int(mask.sum()), corners_uhd=corners.tolist(),
                         scale=float(np.linalg.norm(transform[:,0]))))
    return rows


def previews():
    source = source_path('beihai')
    p=subprocess.Popen([
        "ffmpeg","-nostdin","-loglevel","error","-threads","5","-i",str(source),
        "-map","0:v:0","-an","-filter_threads","1",
        "-vf",f"fps={PREVIEW_FPS},scale={W}:{H}:flags=area,format=gray",
        "-c:v","rawvideo","-threads","1","-f","rawvideo","-"
    ], stdout=subprocess.PIPE)
    assert p.stdout
    idx=0
    while b:=p.stdout.read(W*H):
        assert len(b)==W*H
        yield idx, np.frombuffer(b,dtype=np.uint8).reshape(H,W)
        idx+=1
    assert p.wait()==0


def main():
    screens = [Path(p) for p in sys.argv[1:]]
    templates=[]
    for path in screens:
        img=gray_resize(path)
        templates.append((img.shape,*sift.detectAndCompute(img,None)))
    rough=[[] for _ in screens]
    for idx,img in previews():
        values=compare(img,templates)
        for table,r in zip(rough,values,strict=True):
            if r is not None:
                # FFmpeg fps sampling can choose a source frame around the output time.
                table.append(dict(r,frame_estimate=round(idx*25/PREVIEW_FPS)))
    report=[]
    for idx,(path,table) in enumerate(zip(screens,rough,strict=True)):
        best=sorted(table,key=lambda x:x['inliers'],reverse=True)[:10]
        if not best:
            report.append(dict(screen=path.name,rough=[]))
            continue
        print(path.name,[(q['inliers'],q['frame_estimate']) for q in best],flush=True)
        start=max(0,best[0]['frame_estimate']-16)
        length=min(34,SOURCES['beihai'][2]-start)
        fine=[]
        for f,planes in av_frames(source_path('beihai'),[(start,length)],SOURCES['beihai'][1]):
            l=np.uint8(np.clip((planes[0].astype(np.float32)-64)*(255/876),0,255))
            img=cv2.resize(l,(W,H),interpolation=cv2.INTER_AREA)
            r=compare(img,[templates[idx]])[0]
            if r:
                fine.append(dict(r,frame=f))
        fine.sort(key=lambda x:x['inliers'],reverse=True)
        print(path.name,'refined',fine[:2],flush=True)
        report.append(dict(screen=path.name,rough=best,fine=fine[:10]))
    (OUT/'cropped_screenshot_matches.json').write_text(
        json.dumps(report,indent=2,ensure_ascii=False)+"\n")


if __name__=='__main__':
    main()
