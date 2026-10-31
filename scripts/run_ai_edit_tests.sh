#!/usr/bin/env bash
# M0 baseline: AI / edit focused test runner.
#
# Runs the AI assistant + TAEP edit-planning focused test surface and reports
# pass count, duration and known failures — the baseline artifact required by
# docs/ai_assistant_agent_harness_product_plan_2026-08-01.md §11.2 M0 step 1.
#
# Usage:
#   scripts/run_ai_edit_tests.sh            # run focused AI/edit surface
#   scripts/run_ai_edit_tests.sh --mcp      # also include mcp-marked tests
#   scripts/run_ai_edit_tests.sh --privacy  # also include privacy-marked tests
#
# Exit code is pytest's exit code. A summary is always printed at the end.

set -euo pipefail

# Resolve project root regardless of where the script is invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

# Pick the python interpreter: prefer the project venv, fall back to PATH.
if [[ -x ".venv/bin/python" ]]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi

# Focused test paths (the AI assistant + TAEP edit-planning surface).
PATHS=(
  "tests/edit/ai_editing"
  "tests/app/test_ai_command_stack_commit.py"
  "tests/app/test_ai_e2e_deterministic.py"
  "tests/app/test_ai_edit_controller.py"
  "tests/ui/test_ai_edit_assistant_dock.py"
  "tests/ui/test_ai_job_decision_prefs.py"
  "tests/ui/test_ai_subpanels.py"
  "tests/ui/test_ai_timeline_diff.py"
  "tests/architecture/test_feature_flags_registry.py"
)

# Marker filter: by default run everything in the paths above. The feature-flag
# registry test sits under tests/architecture but is part of the M0 baseline, so
# it is included explicitly via the path list rather than the marker.
MARK_EXPR=""

EXTRA_FLAGS=()
for arg in "$@"; do
  case "$arg" in
    --mcp)
      MARK_EXPR="$( [ -n "$MARK_EXPR" ] && echo "$MARK_EXPR or " )mcp"
      ;;
    --privacy)
      MARK_EXPR="$( [ -n "$MARK_EXPR" ] && echo "$MARK_EXPR or " )privacy"
      ;;
    --generation)
      MARK_EXPR="$( [ -n "$MARK_EXPR" ] && echo "$MARK_EXPR or " )generation"
      ;;
    --analysis)
      MARK_EXPR="$( [ -n "$MARK_EXPR" ] && echo "$MARK_EXPR or " )ai_analysis"
      ;;
    -h|--help)
      sed -n '2,16p' "$0"
      exit 0
      ;;
    *)
      EXTRA_FLAGS+=("$arg")
      ;;
  esac
done

echo "============================================================"
echo " Topos AI / edit focused test suite (M0 baseline)"
echo " python:   ${PY}"
echo " paths:    ${PATHS[*]}"
if [[ -n "$MARK_EXPR" ]]; then
  echo " markers:  ${MARK_EXPR}"
fi
echo "============================================================"

START=$(date +%s)

# Run pytest. We do not use set -e for this command because we want to print a
# summary even when tests fail.
set +e
if [[ -n "$MARK_EXPR" ]]; then
  "$PY" -m pytest "${PATHS[@]}" -m "${MARK_EXPR}" \
    --tb=short -q ${EXTRA_FLAGS[@]+"${EXTRA_FLAGS[@]}"}
else
  "$PY" -m pytest "${PATHS[@]}" --tb=short -q ${EXTRA_FLAGS[@]+"${EXTRA_FLAGS[@]}"}
fi
RC=$?
set -e

END=$(date +%s)
DURATION=$((END - START))

echo "------------------------------------------------------------"
echo " AI/edit focused suite finished in ${DURATION}s with exit code ${RC}"
if [[ $RC -ne 0 ]]; then
  echo " ⚠  some tests failed — record them as the M0 known-failure baseline"
fi
echo "------------------------------------------------------------"

exit $RC
