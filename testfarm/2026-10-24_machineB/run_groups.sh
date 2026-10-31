#!/bin/bash
# C0.1 测试农场首跑（计划表 B §C0.1，机器 B · 2026-10-24）
# 分组原则：G1=CI 逻辑套件对齐(无 GPU 前提)；G2-G5=按域分组(本机 Qt/GPU 全可用)；
# G6=应用/集成/杂域；G7=tests 根下散置用例。每组记 console log + junitxml + 退出码。
set -u
cd "$(dirname "$0")/../.." || exit 9
PY=.venv/Scripts/python.exe
DIR=testfarm/2026-10-24_machineB

run() {
  name=$1; shift
  echo "=== GROUP $name START $(date -Iseconds) ==="
  "$PY" -m pytest -q -o junit_family=xunit2 --junitxml="$DIR/$name.xml" \
    "$@" > "$DIR/$name.log" 2>&1
  rc=$?
  tail -1 "$DIR/$name.log" > "$DIR/$name.rc"
  echo "GROUP $name rc=$rc :: $(cat "$DIR/$name.rc")"
}

run G1_logic_ci \
  tests/media/test_windows_hw_fallback_reasons.py \
  tests/media/test_windows_hw_d3d11_contract.py \
  tests/media/test_windows_hw_gpu_capability.py \
  tests/media/test_windows_hw_zero_copy.py \
  tests/media/test_windows_hw_planar_source.py \
  tests/media/test_windows_hw_native_surface_contract.py \
  tests/media/test_windows_hw_pipeline_policy.py \
  tests/media/test_bench_timeline_export_windows.py \
  tests/media/test_decode_policy.py \
  tests/shared/gpu/test_platform_capability_kinds.py \
  tests/shared/gpu/test_backend_types.py \
  tests/export/test_hwaccel_encoder_map.py \
  tests/export/test_windows_render_cache.py

run G2_media tests/media
run G3_shared_export tests/shared tests/export
run G4_edit_color tests/edit tests/color
run G5_comp_render_plugins tests/composite tests/render tests/plugins
run G6_app_rest tests/app tests/integration tests/architecture \
  tests/feature_availability tests/file_explorer tests/project \
  tests/scripting tests/native tests/motion tests/ai tests/core \
  tests/teaching_animation tests/benchmark tests/deliver
run G7_root tests/test_ai_gen_job.py tests/test_batch_c_quality_perf.py \
  tests/test_topos_abi_smoke.py tests/test_topos_benchmark_protocol.py \
  tests/test_topos_oiio_contract.py tests/test_topos_render_cli.py

echo "ALL GROUPS DONE $(date -Iseconds)"
