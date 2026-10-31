"""Run all Edit tests in isolated batches, retaining native hang diagnostics."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
label = sys.argv[1] if len(sys.argv) > 1 else 'final-edit-batches'
if not label.replace('-', '').replace('_', '').isalnum():
    raise ValueError('Use a simple directory label for retained validation output')
OUT = Path(__file__).resolve().parent / 'validation' / label
OUT.mkdir(parents=True, exist_ok=True)
files = sorted(str(p.relative_to(ROOT)) for p in (ROOT/'tests/edit').rglob('test_*.py'))
files = [p for p in files if not p.endswith('/test_p2_09_session_pool.py')]
batches = [files[i:i+90] for i in range(0,len(files),90)]
(OUT/'batch-manifest.json').write_text(json.dumps(batches, indent=2))
results = []
for index, paths in enumerate(batches, 1):
    label = f'edit-batch-{index}'
    started = time.monotonic()
    args = [sys.executable, '-c', 'import src.main; import pytest,sys; raise SystemExit(pytest.main(sys.argv[1:]))',
            '-q', *paths, '--maxfail=25', '-o', 'faulthandler_timeout=90',
            f'--junitxml={OUT/label}.xml']
    with (OUT/f'{label}.log').open('w') as handle:
        config_path = OUT / '.run-configs' / label / 'config.json'
        config_path.parent.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(args, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT,
                                   env={**os.environ, 'QT_QPA_PLATFORM':'offscreen',
                                        'OPENBLAS_NUM_THREADS':'1',
                                        'TOPOS_CONFIG_PATH':str(config_path)},
                                   start_new_session=True)
        try:
            returncode = process.wait(timeout=360)
            timed_out = False
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            returncode, timed_out = process.returncode, True
    row = {'label':label, 'files':len(paths), 'returncode':returncode,
           'timed_out':timed_out, 'seconds':round(time.monotonic()-started,2)}
    results.append(row)
    (OUT/'batch-results.json').write_text(json.dumps(results,indent=2))
    print(json.dumps(row), flush=True)
