#!/usr/bin/env bash
# check_physical_missing_features.sh
#
# Verifies that the application shell can start when feature directories are
# physically removed. This tests the "product boundary" — each product should
# work without needing the source code of disabled features.
#
# Usage:
#   ./scripts/check_physical_missing_features.sh [feature1] [feature2] ...
#   ./scripts/check_physical_missing_features.sh --product <mode>
#
# Examples:
#   ./scripts/check_physical_missing_features.sh edit
#   ./scripts/check_physical_missing_features.sh edit composite deliver
#   ./scripts/check_physical_missing_features.sh --product color
#
# The script temporarily moves the specified feature directories aside,
# runs import and unit tests, then restores them.
# Uses _backup_features/ for all backups.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
BACKUP_DIR="$PROJECT_ROOT/_backup_features"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# All valid optional features
VALID_FEATURES="edit composite deliver"
VALID_PRODUCT_MODES="color edit composite"

product_removes_for() {
    case "$1" in
        color) echo "edit composite" ;;
        edit) echo "composite" ;;
        composite) echo "edit" ;;
        *) return 1 ;;
    esac
}

# Parse arguments
FEATURES_TO_REMOVE=()
PRODUCT_MODE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --product)
            if [ -z "${2:-}" ]; then
                echo -e "${RED}--product requires a mode argument${NC}"
                exit 1
            fi
            PRODUCT_MODE="$2"
            shift 2
            ;;
        --help|-h)
            echo "Usage: $0 [--product <mode>] [feature1] [feature2] ..."
            echo ""
            echo "Options:"
            echo "  --product <mode>   Remove features for a product mode ($VALID_PRODUCT_MODES)"
            echo "  --help             Show this help"
            echo ""
            echo "Available features: $VALID_FEATURES"
            echo "Product modes: color (removes edit,composite) edit (removes composite) composite (removes edit)"
            exit 0
            ;;
        *)
            FEATURES_TO_REMOVE+=("$1")
            shift
            ;;
    esac
done

# If --product was specified, derive features to remove
if [ -n "$PRODUCT_MODE" ]; then
    PRODUCT_REMOVES="$(product_removes_for "$PRODUCT_MODE" || true)"
    if [ -z "$PRODUCT_REMOVES" ]; then
        echo -e "${RED}Unknown product mode: $PRODUCT_MODE${NC}"
        echo "Valid modes: $VALID_PRODUCT_MODES"
        exit 1
    fi
    # Append product-mode removals (user can also add extras)
    for feat in $PRODUCT_REMOVES; do
        FEATURES_TO_REMOVE+=("$feat")
    done
fi

if [ ${#FEATURES_TO_REMOVE[@]} -eq 0 ]; then
    echo "Usage: $0 [--product <mode>] <feature1> [feature2] ..."
    echo "Available features: $VALID_FEATURES"
    echo "Product modes: $VALID_PRODUCT_MODES"
    exit 1
fi

# Validate feature names
for feat in "${FEATURES_TO_REMOVE[@]}"; do
    if ! echo "$VALID_FEATURES" | grep -qw "$feat"; then
        echo -e "${RED}Invalid feature: $feat${NC}"
        echo "Valid features: $VALID_FEATURES"
        exit 1
    fi
done

# De-duplicate features
FEATURES_TO_REMOVE=($(echo "${FEATURES_TO_REMOVE[@]}" | tr ' ' '\n' | sort -u | tr '\n' ' '))

# Build the disable env var
DISABLE_LIST=$(IFS=,; echo "${FEATURES_TO_REMOVE[*]}")

echo "=============================================="
echo "Physical Missing Features Test"
echo "=============================================="
echo "Removing features: ${FEATURES_TO_REMOVE[*]}"
echo "TOPOS_DISABLE_FEATURES=$DISABLE_LIST"
echo ""

# Move feature directories aside
MOVED_DIRS=()
cleanup() {
    local exit_code=$?
    echo ""
    echo "Restoring moved directories..."
    for entry in "${MOVED_DIRS[@]}"; do
        SRC="$PROJECT_ROOT/src/features/${entry}"
        BACKUP="$BACKUP_DIR/${entry}"
        if [ -d "$BACKUP" ]; then
            mv "$BACKUP" "$SRC"
            echo "  Restored: $entry"
        fi
    done
    rmdir "$BACKUP_DIR" 2>/dev/null || true
    echo -e "${GREEN}Cleanup complete.${NC}"
    exit $exit_code
}
trap cleanup EXIT INT TERM

mkdir -p "$BACKUP_DIR"

for feat in "${FEATURES_TO_REMOVE[@]}"; do
    SRC="$PROJECT_ROOT/src/features/$feat"
    BACKUP="$BACKUP_DIR/$feat"
    if [ -d "$SRC" ]; then
        mv "$SRC" "$BACKUP"
        MOVED_DIRS+=("$feat")
        echo "  Moved aside: $feat"
    else
        echo -e "  ${YELLOW}Warning: $feat directory not found at $SRC${NC}"
    fi
done

echo ""
echo "Running tests with physical directories removed..."
echo ""

export TOPOS_DISABLE_FEATURES="$DISABLE_LIST"
export TOPOS_PRODUCT_MODE="${PRODUCT_MODE:-color}"

RESULT=0

# Test 1: Can import main_window
echo -e "${YELLOW}[1/4] Testing main_window import...${NC}"
if "$VENV_PYTHON" -c "from src.app.main_window import MainWindow; print('OK')" 2>&1; then
    echo -e "${GREEN}  PASS: main_window import${NC}"
else
    echo -e "${RED}  FAIL: main_window import${NC}"
    RESULT=1
fi
echo ""

# Test 2: Feature flags registry tests
echo -e "${YELLOW}[2/4] Testing feature flags registry...${NC}"
if "$VENV_PYTHON" -m pytest "$PROJECT_ROOT/tests/test_feature_flags_registry.py" -q 2>&1; then
    echo -e "${GREEN}  PASS: feature flags registry${NC}"
else
    echo -e "${RED}  FAIL: feature flags registry${NC}"
    RESULT=1
fi
echo ""

# Test 3: Page context tests
echo -e "${YELLOW}[3/4] Testing page context...${NC}"
if "$VENV_PYTHON" -m pytest "$PROJECT_ROOT/tests/test_page_context.py" -q 2>&1; then
    echo -e "${GREEN}  PASS: page context${NC}"
else
    echo -e "${RED}  FAIL: page context${NC}"
    RESULT=1
fi
echo ""

# Test 4: Product physical missing smoke tests (if they exist)
if [ -f "$PROJECT_ROOT/tests/test_product_physical_missing.py" ]; then
    echo -e "${YELLOW}[4/4] Testing product physical missing smoke...${NC}"
    if "$VENV_PYTHON" -m pytest "$PROJECT_ROOT/tests/test_product_physical_missing.py" \
        -q -m "physical_missing" 2>&1; then
        echo -e "${GREEN}  PASS: product physical missing smoke${NC}"
    else
        echo -e "${RED}  FAIL: product physical missing smoke${NC}"
        RESULT=1
    fi
    echo ""
else
    echo -e "${CYAN}[4/4] Skipping product physical missing smoke (test file not found)${NC}"
    echo ""
fi

echo "=============================================="
if [ $RESULT -eq 0 ]; then
    echo -e "${GREEN}ALL TESTS PASSED${NC}"
else
    echo -e "${RED}SOME TESTS FAILED${NC}"
fi
echo "=============================================="

exit $RESULT
