from pathlib import Path
import subprocess,sys,json,os,time
files=sorted(str(p) for p in Path('tests/edit').glob('test_*.py') if any(x in p.name for x in ('audio','stem','normalize','pitch','nested')))
started=time.monotonic()
print('SELECTED',len(files),flush=True)
print('\n'.join(files),flush=True)
env=dict(os.environ,QT_QPA_PLATFORM='offscreen',OPENBLAS_NUM_THREADS='1')
try:
 result=subprocess.run([sys.executable,'-m','pytest','-q',*files,'--junitxml=/tmp/topos-round7-audio-suite.xml'],env=env,timeout=120)
 print('RESULT',json.dumps({'returncode':result.returncode,'seconds':time.monotonic()-started,'files':files}),flush=True)
except subprocess.TimeoutExpired:
 print('TIMEOUT',json.dumps({'seconds':time.monotonic()-started,'files':files}),flush=True)
 sys.exit(124)
