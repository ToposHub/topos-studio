#!/usr/bin/env bash
# MotionGraphicClip validation entry point (post-implementation Batch 0).
#
# Usage:
#   scripts/run_motion_graphic_validation.sh --focused
#   scripts/run_motion_graphic_validation.sh --architecture
#   scripts/run_motion_graphic_validation.sh --waveform
#   scripts/run_motion_graphic_validation.sh --html
#   scripts/run_motion_graphic_validation.sh --gpu
#   scripts/run_motion_graphic_validation.sh --full
#
# The focused and waveform modes are safe local gates. Architecture is a
# manual baseline audit because the repository currently has unrelated legacy
# guardrail failures. HTML is a real backend gate and requires the pinned
# Playwright/Chromium extra. Full mode deliberately runs in its own process so
# a native Qt/audio crash cannot make the focused result look successful.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

VALIDATION_PYTHON="${VALIDATION_PYTHON:-python3}"
MODE="focused"

usage() {
  sed -n '2,15p' "$0"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --focused|--architecture|--waveform|--html|--gpu|--full)
      MODE="${1#--}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

run_pytest() {
  echo "[motion-graphic] python=${VALIDATION_PYTHON} mode=${MODE}"
  "$VALIDATION_PYTHON" -m pytest "$@" --tb=short -q
}

case "$MODE" in
  focused)
    run_pytest \
      tests/core/test_w5_audio_composition.py \
      tests/render/test_teaching_animation_phase2.py \
      tests/render/test_teaching_animation_phase6.py \
      tests/render/test_teaching_animation_phase7.py \
      tests/render/test_w0b_render_contracts.py \
      tests/render/test_w4_capability_consistency.py \
      tests/render/test_scene_renderer_router.py \
      tests/render/test_motion_graphic_benchmark.py \
      tests/render/test_release_manifest.py \
      tests/render/test_s0_s3_release_fixtures.py \
      tests/render/test_motion_graphic_reopen_gate.py \
      tests/render/test_hdr_gpu_contract.py \
      tests/render/test_render_worker_lifecycle_soak.py \
      tests/motion/test_w6_expression.py \
      tests/motion/test_w6_expression_runtime.py \
      tests/motion/test_expression_cross_process_golden.py \
      tests/ui/test_editor_html_preview.py \
      tests/ui/test_editor_multiselect.py \
      tests/ui/test_layer_tree_model.py \
      tests/ui/test_teaching_animation_phase3.py \
      tests/ai/test_motion_graphic_integration.py \
      tests/ai/test_motion_graphic_b5_advanced.py \
      tests/ai/test_teaching_animation_phase5.py \
      tests/composite/test_graph_evaluator_consistency.py \
      tests/shared/test_effect_spec.py
    ;;
  architecture)
    run_pytest tests/architecture/test_boundary_guardrails.py
    ;;
  waveform)
    run_pytest tests/edit/test_audio_waveform_lifecycle.py
    ;;
  html)
    run_pytest -m html_renderer
    ;;
  gpu)
    run_pytest -m gpu_renderer
    ;;
  full)
    run_pytest tests
    ;;
esac
