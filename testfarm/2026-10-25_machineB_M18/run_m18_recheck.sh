#!/bin/bash
# M18 机器 B 复验（计划表 M18 §M18.4b，2026-10-25 用户确认接入）
# 运行方式（机器 B，仓库根）：
#   bash testfarm/2026-10-25_machineB_M18/run_m18_recheck.sh [Windows打包目录]
# 产物：docs/validation/upgrade_2026-10-24/M18/machine_B/report/*.json
#       + 本目录 run.rc / run.log；完成后 git commit+push 回传。
set -u
cd "$(dirname "$0")/../.." || exit 9
PY=.venv/Scripts/python.exe
[ -x "$PY" ] || PY=python
DIST="${1:-}"
DIR=testfarm/2026-10-25_machineB_M18

ARGS=()
[ -n "$DIST" ] && ARGS=(--dist "$DIST")
"$PY" scripts/m18_machine_b_recheck.py "${ARGS[@]}" \
  > "$DIR/run.log" 2>&1
rc=$?
echo "rc=$rc" > "$DIR/run.rc"
tail -3 "$DIR/run.log"
exit $rc
