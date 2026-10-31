"""Replay the read-only composite audit suite, isolating native Qt/GL crashes."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent

def main() -> None:
    files = set((ROOT / 'tests/composite').rglob('test_*.py'))
    for folder in ('ui', 'app', 'project', 'edit'):
        files.update((ROOT / f'tests/{folder}').glob('*composite*.py'))
    files.update((ROOT / 'tests/ui').glob('test_spline*.py'))
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', TOPOS_TEST_MODE='1', OPENBLAS_NUM_THREADS='1', TOPOS_HEADLESS_GL='1')
    rows = []
    logs = OUT / 'pytest_logs'
    logs.mkdir(exist_ok=True)
    for index, path in enumerate(sorted(files), 1):
        relative = str(path.relative_to(ROOT))
        stem = relative.replace('/', '__').removesuffix('.py')
        xml = logs / (stem + '.xml')
        command = [sys.executable, '-m', 'pytest', relative, '-o', 'addopts=', '-q', '--tb=short', '-p', 'no:cacheprovider', f'--junitxml={xml}']
        started = time.perf_counter()
        try:
            completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, timeout=120)
            code = completed.returncode
            output = completed.stdout.decode('utf-8', 'replace') + completed.stderr.decode('utf-8', 'replace')
        except subprocess.TimeoutExpired as exc:
            code = 'timeout'
            output = (exc.stdout or b'').decode('utf-8', 'replace') + (exc.stderr or b'').decode('utf-8', 'replace')
        (logs / (stem + '.log')).write_text(output, encoding='utf-8')
        row = dict(file=relative, returncode=code, wall_s=round(time.perf_counter()-started, 3), log=str((logs/(stem+'.log')).relative_to(ROOT)))
        if xml.exists():
            tree = ET.parse(xml)
            cases = list(tree.iter('testcase'))
            row.update(tests=len(cases), failures=sum(c.find('failure') is not None for c in cases), errors=sum(c.find('error') is not None for c in cases), skipped=sum(c.find('skipped') is not None for c in cases))
            row['passed'] = row['tests'] - row['failures'] - row['errors'] - row['skipped']
        rows.append(row)
        (OUT/'suite_results.json').write_text(json.dumps(dict(python=sys.executable, environment={k:env[k] for k in ('QT_QPA_PLATFORM','TOPOS_HEADLESS_GL','OPENBLAS_NUM_THREADS')}, total_files=len(files), completed_files=len(rows), rows=rows), ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"[{index}/{len(files)}] {relative}: {code} ({row['wall_s']}s)", flush=True)

if __name__ == '__main__':
    main()
