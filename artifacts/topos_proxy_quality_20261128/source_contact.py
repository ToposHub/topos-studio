#!/usr/bin/env python3
"""Preview real source content before selecting a richly colored test window."""
from __future__ import annotations

import math
from pathlib import Path

import av
from PIL import Image, ImageDraw, ImageFont

from measure import OUT, ROOT, SOURCES, source_path

media = ROOT / 'tests/test_videos/达芬奇转码_测速_2026-11-27'
alt = media / 'H264_H265_全长/4K/H265'
sources = [source_path(c) for c in SOURCES]
sources.extend(sorted(path for path in alt.glob('*.mov') if path.name not in
                      {p.name for p in sources}))
typeface = '/System/Library/Fonts/PingFang.ttc'
font_path = '/System/Library/Fonts/Supplemental/Arial.ttf'
labels = ('Beihai DNx', 'Yuanmingyuan 02 DNx',
          'Wulan 00001', 'Wulan 00006', 'Wulan 00009', 'Wulan 00011',
          'Yuanmingyuan 01', 'Yuanmingyuan 03')
assert len(sources) == len(labels)
fractions = (.04, .15, .27, .39, .51, .63, .76, .89, .96)
w,h=210,118
sheet=Image.new('RGB',(len(fractions)*(w+4)+4,len(sources)*(h+40)+5),'#16191e')
d=ImageDraw.Draw(sheet)
font=ImageFont.truetype(font_path,15)
for i, (path,label) in enumerate(zip(sources,labels)):
    for j,fraction in enumerate(fractions):
        try:
            with av.open(str(path)) as c:
                st=c.streams.video[0]
                st.thread_count=4
                fps=st.average_rate
                count=st.frames or round(st.duration*st.time_base*fps)
                wanted=round(fraction*(count-1))
                frame_time=wanted/fps
                first=st.start_time or 0
                c.seek(first+math.ceil(frame_time/st.time_base),stream=st,backward=True)
                for f in c.decode(st):
                    if f.pts is not None and (f.pts-first)*st.time_base >= frame_time:
                        x=4+j*(w+4)
                        y=5+i*(h+40)
                        thumb=f.reformat(width=w,height=h,format='rgb24',
                                         src_colorspace='ITU709',dst_colorspace='ITU709').to_image()
                        sheet.paste(thumb,(x,y))
                        d.text((x,y+h+4),f'{label} | f{wanted}',font=font,fill='#edf2f8')
                        break
        except av.error.FFmpegError as e:
            print(path,e)
    print(label,path.name,flush=True)
sheet.save(OUT/'source_contact.png')
