#!/usr/bin/env bash
# Composite validation runner with explicit test layers.
#
# Usage:
#   scripts/run_composite_tests.sh --fast
#   scripts/run_composite_tests.sh --smoke
#   scripts/run_composite_tests.sh --gpu
#   scripts/run_composite_tests.sh --media
#   scripts/run_composite_tests.sh --full
#   COMPOSITE_SOAK_ITERATIONS=5 scripts/run_composite_tests.sh --soak
#
# The default/full mode keeps the historical process-per-file isolation. This
# avoids turning a native Qt/ModernGL crash in one test file into a false
# result for the rest of the composite suite. Fast, smoke, GPU, and media
# modes use the same isolation and only change the selected files/markers.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PY="${REPO_ROOT}/.venv/bin/python"
if [[ ! -x "${PY}" ]]; then
    PY="$(command -v python3)"
fi

cd "${REPO_ROOT}"

# QA-01: 与产品启动（src/main.py、src/shared/io/file_io.py）共用同一环境
# 契约。必须在任何子进程导入 cv2 之前导出，否则 OpenEXR 用例（如
# test_ai_remove_wiring）会以 "OpenEXR disabled" 失败，被误读为产品 bug。
export OPENCV_IO_ENABLE_OPENEXR=1

# QA-01: 能力自检——写读一个 2x2 EXR 验证 OpenEXR 真的可用；构建缺
# OpenEXR 支持时给出可定位错误，而不是让 EXR 用例逐个失败。
if ! "${PY}" - <<'PYEOF'
import os
import sys
import tempfile

os.environ["OPENCV_IO_ENABLE_OPENEXR"] = "1"
import cv2
import numpy as np

img = np.full((2, 2, 3), 0.5, dtype=np.float32)
with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "probe.exr")
    ok_w = cv2.imwrite(path, img)
    back = cv2.imread(path, cv2.IMREAD_UNCHANGED) if ok_w else None
if back is None or back.shape != img.shape:
    sys.stderr.write(
        "OPENCV_IO_ENABLE_OPENEXR=1 is set but this OpenCV build still "
        "cannot round-trip EXR (probe.exr failed). EXR-dependent composite "
        "tests would report false failures; fix the runtime environment "
        "first.\n")
    sys.exit(1)
PYEOF
then
    echo "OpenEXR capability probe failed; aborting." >&2
    exit 2
fi

MODE="full"
SOAK_ITERATIONS="${COMPOSITE_SOAK_ITERATIONS:-3}"
pytest_args=()

usage() {
    sed -n '2,14p' "$0"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --fast|--smoke|--gpu|--media|--full|--soak)
            MODE="${1#--}"
            shift
            ;;
        --repeat)
            if [[ $# -lt 2 ]]; then
                echo "--repeat requires a positive integer" >&2
                exit 2
            fi
            SOAK_ITERATIONS="$2"
            shift 2
            ;;
        --repeat=*)
            SOAK_ITERATIONS="${1#--repeat=}"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            pytest_args+=("$@")
            break
            ;;
        *)
            pytest_args+=("$1")
            shift
            ;;
    esac
done

if [[ ! "${SOAK_ITERATIONS}" =~ ^[1-9][0-9]*$ ]]; then
    echo "Soak iterations must be a positive integer: ${SOAK_ITERATIONS}" >&2
    exit 2
fi

files=()
marker_args=()
case "${MODE}" in
    full)
        while IFS= read -r f; do
            files+=("${f}")
        done < <(find tests/composite -name 'test_*.py' | sort)
        ;;
    fast)
        while IFS= read -r f; do
            files+=("${f}")
        done < <(find tests/composite -name 'test_*.py' | sort)
        marker_args=(-m 'not gpu_renderer and not real_media and not slow')
        ;;
    smoke)
        files=(
            tests/composite/test_composite_gpu_lifecycle.py
            tests/composite/test_render_lifecycle_contract.py
            tests/composite/test_render_pass_contract.py
            tests/composite/test_scene_payload.py
            tests/composite/test_runtime_diagnostics.py
        )
        marker_args=(-m smoke)
        ;;
    gpu)
        files=(
            tests/composite/test_color_input_color_space.py
            tests/composite/test_nodes/test_output_nodes.py
            tests/composite/test_nodes/test_upgraded_nodes.py
            tests/composite/test_scene3d_motion_aov.py
            tests/composite/test_shader_uniform_contract.py
            tests/composite/test_video_frame_playback.py
        )
        marker_args=(-m gpu_renderer)
        ;;
    media)
        files=(tests/composite/test_video_frame_playback.py)
        marker_args=(-m real_media)
        ;;
    soak)
        files=(
            tests/composite/test_composite_gpu_lifecycle.py
            tests/composite/test_render_lifecycle_contract.py
        )
        ;;
    *)
        echo "Unknown mode: ${MODE}" >&2
        usage >&2
        exit 2
        ;;
esac

fail_files=()
fail_count=0
pass_count=0
no_test_count=0

run_file() {
    local iteration="$1"
    local total_iterations="$2"
    local f="$3"
    local out status summary
    local -a command=("${PY}" -m pytest "${f}" --no-header -q -p no:cacheprovider)

    if [[ ${#marker_args[@]} -gt 0 ]]; then
        command+=("${marker_args[@]}")
    fi
    if [[ ${#pytest_args[@]} -gt 0 ]]; then
        command+=("${pytest_args[@]}")
    fi

    out=$("${command[@]}" 2>&1)
    status=$?
    summary=$(echo "${out}" | tail -1)
    if [[ ${status} -eq 5 ]]; then
        no_test_count=$((no_test_count + 1))
        echo "[${iteration}/${total_iterations}] skip ${f}  (${summary})"
    elif [[ ${status} -ne 0 ]]; then
        echo "[${iteration}/${total_iterations}] FAIL ${f}"
        echo "    ${summary}"
        fail_files+=("${f} (iteration ${iteration})")
        fail_count=$((fail_count + 1))
    else
        pass_count=$((pass_count + 1))
        echo "[${iteration}/${total_iterations}] ok   ${f}  (${summary})"
    fi
}

total_iterations=1
if [[ "${MODE}" == "soak" ]]; then
    total_iterations="${SOAK_ITERATIONS}"
fi

echo "Composite validation: mode=${MODE} python=${PY} files=${#files[@]} iterations=${total_iterations}"
for ((iteration = 1; iteration <= total_iterations; iteration++)); do
    for f in "${files[@]}"; do
        run_file "${iteration}" "${total_iterations}" "${f}"
    done
done

echo "----------------------------------------"
echo "Files passed: ${pass_count}  no tests selected: ${no_test_count}  failed: ${fail_count}"
if [[ ${fail_count} -gt 0 ]]; then
    echo "Failing files:"
    printf '  %s\n' "${fail_files[@]}"
    exit 1
fi
echo "ALL COMPOSITE TESTS PASSED (process-isolated, mode=${MODE})"
