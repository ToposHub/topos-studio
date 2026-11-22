from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

OUT = Path(__file__).resolve().parent
MEDIA = OUT.parents[1] / 'tests' / 'test_videos'

def main() -> None:
    results=[]
    for name in ('4K_H264.mov','4K_DNxHR HQX 10bit.mov'):
        cmd=['ffmpeg','-hide_banner','-nostdin','-loglevel','error','-threads','8',
             '-hwaccel','none','-i',str(MEDIA/name),'-map','0:v:0','-an','-sn','-dn',
             '-frames:v','400','-progress','pipe:1','-f','null','-']
        start=time.monotonic()
        run=subprocess.run(cmd,check=True,capture_output=True,text=True)
        wall=time.monotonic()-start
        decoded=int([line.split('=')[1] for line in run.stdout.splitlines() if line.startswith('frame=')][-1])
        assert decoded==400
        results.append({'file':name,'frames':decoded,'wall_s':wall,'fps':decoded/wall,
                        'hardware_acceleration':False,'threads':8,'scope':'Sequential decode first 400 source frames to null'})
        print(results[-1],flush=True)
    (OUT/'cpu_decode_reference.json').write_text(json.dumps(results,indent=2)+'\n')

if __name__=='__main__':
    main()
