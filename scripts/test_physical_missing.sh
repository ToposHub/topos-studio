#!/usr/bin/env bash
# test_physical_missing.sh — Matrix runner for physical missing feature tests.
#
# Runs check_physical_missing_features.sh for every product mode combination,
# ensuring each product works when its disabled modules are physically removed.
#
# Usage:
#   ./scripts/test_physical_missing.sh               # Run full matrix
#   ./scripts/test_physical_missing.sh --quick        # Only color-only mode
#
# This script delegates to check_physical_missing_features.sh which handles
# directory backup/restore via _backup_features/ and trap-based cleanup.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check_physical_missing_features.sh"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

QUICK_MODE=false
if [ "${1:-}" = "--quick" ]; then
    QUICK_MODE=true
fi

if [ ! -x "$CHECK_SCRIPT" ]; then
    echo -e "${RED}Error: $CHECK_SCRIPT not found or not executable${NC}"
    exit 1
fi

# Matrix: label -> args for check_physical_missing_features.sh
# Each entry tests a specific product configuration.
declare -a MATRIX_LABELS=()
declare -a MATRIX_ARGS=()

# Row 1: Color-only product (edit + composite physically removed)
MATRIX_LABELS+=("Color product (edit, composite removed)")
MATRIX_ARGS+=("--product color")

# Row 2: Edit product (composite physically removed)
MATRIX_LABELS+=("Edit product (composite removed)")
MATRIX_ARGS+=("--product edit")

# Row 3: Composite product (edit physically removed)
MATRIX_LABELS+=("Composite product (edit removed)")
MATRIX_ARGS+=("--product composite")

# Row 4: All optional features removed (maximal stress test)
if [ "$QUICK_MODE" = false ]; then
    MATRIX_LABELS+=("Stress test (edit, composite, deliver removed)")
    MATRIX_ARGS+=("edit composite deliver")
fi

TOTAL=${#MATRIX_LABELS[@]}
PASSED=0
FAILED=0
FAILURES=()

echo "========================================================"
echo " Physical Missing Feature CI Matrix"
echo "========================================================"
echo " Matrix size: $TOTAL configurations"
echo " Quick mode: $QUICK_MODE"
echo ""

for i in "${!MATRIX_LABELS[@]}"; do
    LABEL="${MATRIX_LABELS[$i]}"
    ARGS="${MATRIX_ARGS[$i]}"
    NUM=$((i + 1))

    echo ""
    echo -e "${CYAN}━━━ Matrix [$NUM/$TOTAL]: $LABEL ━━━${NC}"
    echo ""

    if bash "$CHECK_SCRIPT" $ARGS; then
        echo -e "${GREEN}  MATRIX [$NUM/$TOTAL] PASSED: $LABEL${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}  MATRIX [$NUM/$TOTAL] FAILED: $LABEL${NC}"
        FAILED=$((FAILED + 1))
        FAILURES+=("$LABEL")
    fi

    echo ""
done

echo "========================================================"
echo " Matrix Summary: $PASSED passed, $FAILED failed (total $TOTAL)"
echo "========================================================"

if [ ${#FAILURES[@]} -gt 0 ]; then
    echo ""
    echo -e "${RED}Failures:${NC}"
    for f in "${FAILURES[@]}"; do
        echo -e "  ${RED}- $f${NC}"
    done
fi

exit $FAILED
