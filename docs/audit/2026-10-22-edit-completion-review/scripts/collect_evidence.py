"""Collect this audit's generated evidence and verify the artifact bundle."""
from __future__ import annotations
import ast
import hashlib
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

EVIDENCE = Path(__file__).resolve().parents[1]
ROOT = EVIDENCE.parents[2]
for folder in ('scripts/steps', 'records/export_manifests'):
    (EVIDENCE / folder).mkdir(parents=True, exist_ok=True)
for step in Path('/tmp/topos-round7-gui/commands').glob('*.done'):
    shutil.copy2(step, EVIDENCE / 'scripts/steps' / step.with_suffix('.py').name)
for manifest in Path('/tmp/topos-round7-gui/exports').glob('*.manifest.json'):
    shutil.copy2(manifest, EVIDENCE / 'records/export_manifests' / manifest.name)
for frame in Path('/tmp/topos-round7-gui/exports').glob('round7_gui_png_*.png'):
    shutil.copy2(frame, EVIDENCE / 'screenshots' / frame.name)
environment = {
    'client_date': '2026-10-22', 'client_timezone': 'Asia/Shanghai',
    'python': sys.version, 'platform': platform.platform(),
    'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
    'branch': subprocess.check_output(['git', 'branch', '--show-current'], cwd=ROOT, text=True).strip(),
    'application_sessions': [{'exec_session': 32720, 'exit_code': 0},
                             {'exec_session': 44162, 'exit_code': 0}],
    'actual_project': '/tmp/topos-round7-gui/Round7Audit20261022.topos',
    'source_media_read_only': True, 'product_code_modified': False,
}
(EVIDENCE / 'records/environment.json').write_text(json.dumps(environment, indent=2))
for script in EVIDENCE.rglob('*.py'):
    ast.parse(script.read_text(), filename=str(script))
main = ROOT / 'docs/edit_page_seventh_completion_review_optimization_plan_2026-10-22.md'
for markdown in (main, EVIDENCE / 'README.md'):
    for target in re.findall(r'\]\(([^)]+)\)', markdown.read_text()):
        if target.startswith(('http:', 'https:', '#')):
            continue
        path = target.split('#', 1)[0].strip('<>')
        if not (markdown.parent / path).exists():
            raise RuntimeError(f'Broken link in {markdown.name}: {target}')
items = []
for file in sorted(EVIDENCE.rglob('*')):
    if not file.is_file() or file.name == 'evidence_manifest.json':
        continue
    content = file.read_bytes()
    items.append({'path': file.relative_to(EVIDENCE).as_posix(), 'bytes': len(content),
                  'sha256': hashlib.sha256(content).hexdigest()})
(EVIDENCE / 'evidence_manifest.json').write_text(json.dumps(items, indent=2))
print(json.dumps({'files': len(items), 'bytes': sum(i['bytes'] for i in items),
                  'python_syntax': 'passed', 'local_links': 'passed'}))
