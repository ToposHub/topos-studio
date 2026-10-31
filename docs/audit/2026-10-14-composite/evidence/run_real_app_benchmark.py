"""Run the existing real-app driver with an isolated preference/project directory."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[4]
OUT=Path(__file__).resolve().parent

def main() -> None:
    temp=Path('/tmp/topos_composite_gui_audit_20261014')
    temp.mkdir(exist_ok=True)
    config={'show_welcome_on_startup':False,'auto_load_last_project':False,'check_updates_on_startup':False,'project_library_directory':str(temp/'projects'),'cache_directory':str(temp/'cache'),'current_page':'Color','locale':'zh_CN','work_mode':'video'}
    (temp/'config.json').write_text(json.dumps(config))
    env=dict(os.environ,TOPOS_CONFIG_PATH=str(temp/'config.json'),TOPOS_PRODUCT_MODE='studio',TOPOS_TEST_MODE='1',TOPOS_FEATURE_EDIT='1',TOPOS_PLAYBACK_MODE='page',OPENCV_IO_ENABLE_OPENEXR='1',OPENBLAS_NUM_THREADS='1',QT_QPA_PLATFORM='cocoa',PYTHONFAULTHANDLER='1')
    env.pop('TOPOS_HEADLESS_GL',None)
    rows=[]
    for media in ['2K_H265.mov','4K_H265.mov','4K_topos_422_Proxy.mov']:
        (temp/'config.json').write_text(json.dumps(config))
        command=[sys.executable,str(OUT/'real_app_driver_audit_copy.py'),str(ROOT/'tests/test_videos'/media),'15']
        try:
            run=subprocess.run(command,cwd=ROOT,env=env,capture_output=True,timeout=150)
            output=run.stdout.decode('utf8','replace')+run.stderr.decode('utf8','replace')
            row={'media':media,'returncode':run.returncode,'log':media+'.real_app.log'}
        except subprocess.TimeoutExpired as exc:
            output=(exc.stdout or b'').decode('utf8','replace')+(exc.stderr or b'').decode('utf8','replace')
            row={'media':media,'returncode':'timeout','log':media+'.real_app.log'}
        (OUT/row['log']).write_text(output)
        rows.append(row)
        (OUT/'real_app_runs.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
        print(row,flush=True)
        if row['returncode'] != 0:
            print('Real app driver unavailable; remaining real app cases need manual validation.',flush=True)
            break

if __name__=='__main__':main()
