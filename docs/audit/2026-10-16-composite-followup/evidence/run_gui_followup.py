"""Replay selected GUI media cases, each with independent temporary state."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent
DRIVER = ROOT / 'docs/audit/2026-10-14-composite/evidence/real_app_driver_audit_copy.py'

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--media', action='append', required=True)
    parser.add_argument('--seconds', type=float, default=15.0)
    args = parser.parse_args()
    rows: list[dict] = []
    for name in args.media:
        media = ROOT / 'tests/test_videos' / name
        if not media.is_file():
            raise FileNotFoundError(media)
        temporary = Path(tempfile.mkdtemp(prefix='topos-comp-followup-20261016-'))
        config = {'show_welcome_on_startup': False, 'auto_load_last_project': False,
                  'check_updates_on_startup': False,
                  'project_library_directory': str(temporary/'projects'),
                  'cache_directory': str(temporary/'cache'),
                  'current_page': 'Color', 'locale': 'zh_CN', 'work_mode': 'video'}
        config_path = temporary / 'config.json'
        config_path.write_text(json.dumps(config))
        env = dict(os.environ, TOPOS_CONFIG_PATH=str(config_path),
                   TOPOS_PRODUCT_MODE='studio', TOPOS_TEST_MODE='1',
                   TOPOS_FEATURE_EDIT='1', TOPOS_PLAYBACK_MODE='page',
                   OPENCV_IO_ENABLE_OPENEXR='1', OPENBLAS_NUM_THREADS='1',
                   QT_QPA_PLATFORM='cocoa', PYTHONFAULTHANDLER='1')
        env.pop('TOPOS_HEADLESS_GL', None)
        command = [sys.executable, str(DRIVER), str(media), str(args.seconds)]
        try:
            run = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, timeout=150)
            output = run.stdout.decode('utf8','replace') + run.stderr.decode('utf8','replace')
            code: int | str = run.returncode
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or b'').decode('utf8','replace') + (exc.stderr or b'').decode('utf8','replace')
            code = 'timeout'
        log = OUT / (name+'.real_app.log')
        log.write_text(output)
        row = {'media': name, 'returncode': code, 'log': str(log.relative_to(ROOT)),
               'config': str(config_path), 'seconds': args.seconds}
        rows.append(row)
        (OUT/'gui_results.json').write_text(json.dumps({
            'date': '2026-10-16', 'git_head': subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            'rows': rows}, ensure_ascii=False, indent=2))
        print(json.dumps(row,ensure_ascii=False),flush=True)
    return 0 if all(r['returncode']==0 for r in rows) else 1

if __name__ == '__main__':
    raise SystemExit(main())
