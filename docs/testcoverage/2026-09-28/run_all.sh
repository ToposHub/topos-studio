#!/bin/zsh
cd "/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01"
PY=.venv/bin/python
M="not gpu_renderer and not webengine and not html_renderer and not slow and not real_media and not visual and not unstable and not real_provider and not acceptance"
CFG=docs/testcoverage/2026-09-28/cov.rc
for shard in color core shared edit composite render media project export deliver app video video_denoise vault scripting architecture feature_availability file_explorer; do
  echo "=== shard $shard start $(date +%H:%M:%S) ==="
  $PY -m pytest tests/$shard -m "$M" -q -p no:cacheprovider \
    --cov --cov-append --cov-config=$CFG --cov-report= 2>&1 | tail -1
done
echo "=== ALL DONE $(date +%H:%M:%S) ==="
