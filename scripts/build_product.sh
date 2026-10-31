#!/usr/bin/env bash
# build_product.sh — Build a specific Topos product variant.
#
# Usage:
#   scripts/build_product.sh <product-name> [--dry-run]
#
# Where <product-name> is one of: topos-studio, topos-color, topos-edit, topos-composite
#
# Options:
#   --dry-run    Validate configuration without actually building bundles.
#                This works even without PyInstaller installed.
#
# This script:
#   1. Validates the product name against PRODUCT_PRESETS
#   2. Uses src.products.packaging to compute include/exclude configuration
#   3. Sets TOPOS_PRODUCT_MODE env var
#   4. In --dry-run mode: validates config only
#   5. In build mode: generates PyInstaller spec and builds real bundle
#      (requires PyInstaller to be installed)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---- Argument handling ----

DRY_RUN=false

if [ $# -lt 1 ]; then
    echo "Usage: $0 <product-name> [--dry-run]" >&2
    echo "  product-name: topos-studio | topos-color | topos-edit | topos-composite" >&2
    echo "  --dry-run:    Validate config without building" >&2
    exit 1
fi

PRODUCT_NAME="$1"
shift

# Parse remaining flags
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        *)
            echo "ERROR: Unknown option '$1'" >&2
            exit 1
            ;;
    esac
done

# ---- Validate product name ----

VALID_PRODUCTS=("topos-studio" "topos-color" "topos-edit" "topos-composite")

is_valid=false
for p in "${VALID_PRODUCTS[@]}"; do
    if [ "$PRODUCT_NAME" = "$p" ]; then
        is_valid=true
        break
    fi
done

if [ "$is_valid" = false ]; then
    echo "ERROR: Unknown product '$PRODUCT_NAME'" >&2
    echo "  Valid products: ${VALID_PRODUCTS[*]}" >&2
    exit 1
fi

if [ "$DRY_RUN" = true ]; then
    echo "=== DRY RUN: Validating $PRODUCT_NAME ==="
else
    echo "=== Building $PRODUCT_NAME ==="
fi

# ---- Compute packaging configuration ----

cd "$PROJECT_ROOT"

PYTHON=".venv/bin/python"
if [ ! -f "$PYTHON" ]; then
    PYTHON="python3"
fi

# Helper: run a Python snippet and capture output
run_python() {
    $PYTHON -c "
import sys, os
sys.path.insert(0, '$PROJECT_ROOT')
os.environ['TOPOS_PRODUCT_MODE'] = '$PRODUCT_NAME'
$1
"
}

echo "--- Packaging configuration ---"
run_python "
import json
from src.products.packaging import get_packaging_config
config = get_packaging_config('$PRODUCT_NAME')
print(json.dumps(config, indent=2, default=str))
"

if [ $? -ne 0 ]; then
    echo "ERROR: Failed to compute packaging config for '$PRODUCT_NAME'" >&2
    exit 1
fi

# ---- Set product mode environment variable ----

export TOPOS_PRODUCT_MODE="$PRODUCT_NAME"
echo "--- TOPOS_PRODUCT_MODE=$TOPOS_PRODUCT_MODE ---"

# ---- Verify TOPOS_PRODUCT_MODE aliases resolve the same ----
# "color" and "topos-color" should resolve to the same preset

ALIAS_CHECK=$(run_python "
import os
from src.products import get_current_product

# Test full name
os.environ['TOPOS_PRODUCT_MODE'] = '$PRODUCT_NAME'
full = get_current_product().name

# Test short name (strip 'topos-' prefix)
short_name = '$PRODUCT_NAME'.replace('topos-', '', 1)
os.environ['TOPOS_PRODUCT_MODE'] = short_name
short = get_current_product().name

if full != short:
    print(f'MISMATCH: full={full} short={short}')
    sys.exit(1)
else:
    print(f'OK: both resolve to {full}')
" 2>&1)

if echo "$ALIAS_CHECK" | grep -q "MISMATCH"; then
    echo "WARNING: TOPOS_PRODUCT_MODE alias mismatch:" >&2
    echo "  $ALIAS_CHECK" >&2
fi
echo "Alias check: $ALIAS_CHECK"

# ---- Compute include/exclude directories ----

INCLUDE_DIRS=$(run_python "
import json
from src.products.packaging import get_packaging_config
config = get_packaging_config('$PRODUCT_NAME')
print(json.dumps(config['include_dirs']))
")

EXCLUDE_DIRS=$(run_python "
import json
from src.products.packaging import get_packaging_config
config = get_packaging_config('$PRODUCT_NAME')
print(json.dumps(config['exclude_dirs']))
")

echo "Include dirs: $INCLUDE_DIRS"
echo "Exclude dirs: $EXCLUDE_DIRS"

# ---- Compute entry point ----

ENTRY_MAP_LIB=$($PYTHON -c "
import json
entry_map = {
    'topos-studio': 'apps.topos_studio.main:main',
    'topos-color': 'apps.topos_color.main:main',
    'topos-edit': 'apps.topos_edit.main:main',
    'topos-composite': 'apps.topos_composite.main:main',
}
print(json.dumps(entry_map.get('$PRODUCT_NAME', '')))
")

echo "Entry point: $ENTRY_MAP_LIB"

# ---- Dry-run mode: stop here after validation ----

if [ "$DRY_RUN" = true ]; then
    echo ""
    echo "=== DRY RUN COMPLETE: $PRODUCT_NAME config validated ==="
    echo ""
    echo "Summary:"
    echo "  Product:       $PRODUCT_NAME"
    echo "  Entry point:   $ENTRY_MAP_LIB"
    echo "  Include dirs:  $INCLUDE_DIRS"
    echo "  Exclude dirs:  $EXCLUDE_DIRS"
    echo "  TOPOS_PRODUCT_MODE: $TOPOS_PRODUCT_MODE"
    echo ""
    echo "To build for real, run without --dry-run (requires PyInstaller)."
    exit 0
fi

# ---- Check PyInstaller availability ----

if ! $PYTHON -c "import PyInstaller" 2>/dev/null; then
    echo ""
    echo "ERROR: PyInstaller is not installed." >&2
    echo "  Install with: $PYTHON -m pip install pyinstaller" >&2
    echo "  Or run with --dry-run to validate config without building." >&2
    exit 1
fi

PYINSTALLER_VERSION=$($PYTHON -c "import PyInstaller; print(PyInstaller.__version__)")
echo "PyInstaller version: $PYINSTALLER_VERSION"

# ---- Determine spec file ----

# R7：必须是 .spec 扩展名——PyInstaller 按 `.spec` 后缀区分 spec 模式与
# 入口脚本模式（`_spec.py` 会被当脚本打包成"结构坏包"，入口=spec 源码）
SPEC_NAME="${PRODUCT_NAME//-/_}.spec"
SPEC_FILE="$PROJECT_ROOT/packaging/pyinstaller/$SPEC_NAME"

if [ ! -f "$SPEC_FILE" ]; then
    echo "ERROR: Spec file not found: $SPEC_FILE" >&2
    echo "  Expected one of:" >&2
    ls "$PROJECT_ROOT/packaging/pyinstaller/"*.spec >&2 2>/dev/null || true
    exit 1
fi

echo "Using spec file: $SPEC_FILE"

# ---- Build output directory ----

DIST_DIR="$PROJECT_ROOT/dist/$PRODUCT_NAME"
BUILD_DIR="$PROJECT_ROOT/build/$PRODUCT_NAME"

echo "Output directory: $DIST_DIR"
echo "Build directory:  $BUILD_DIR"

# ---- Clean previous build artifacts ----

if [ -d "$DIST_DIR" ]; then
    echo "Cleaning previous dist: $DIST_DIR"
    rm -rf "$DIST_DIR"
fi

if [ -d "$BUILD_DIR" ]; then
    echo "Cleaning previous build: $BUILD_DIR"
    rm -rf "$BUILD_DIR"
fi

# ---- Run PyInstaller ----

echo ""
echo "--- Running PyInstaller ---"

# R7：.spec 模式下 --name/--exclude-module 不允许（makespec 专属选项）——
# 名称/入口/排除表全部由 spec 内部（base_spec + 产品预设）决定，单一真相

# Export product mode for the build process
export TOPOS_PRODUCT_MODE="$PRODUCT_NAME"

$PYTHON -m PyInstaller \
    --distpath "$DIST_DIR" \
    --workpath "$BUILD_DIR" \
    --noconfirm \
    --clean \
    "$SPEC_FILE"

BUILD_EXIT=$?

if [ $BUILD_EXIT -ne 0 ]; then
    echo ""
    echo "ERROR: PyInstaller build failed with exit code $BUILD_EXIT" >&2
    exit $BUILD_EXIT
fi

# ---- Verify the built bundle ----

echo ""
echo "--- Verifying built bundle ---"

VERIFY_SCRIPT="$PROJECT_ROOT/scripts/verify_product_bundle.py"

if [ -f "$VERIFY_SCRIPT" ]; then
    BUNDLE_PATH="$DIST_DIR/$PRODUCT_NAME"
    if [ -d "$BUNDLE_PATH" ]; then
        echo "Running bundle verification on: $BUNDLE_PATH"
        # R7：验证失败 = 构建失败（曾降级为 WARNING 掩盖结构坏包——
        # codec 缺失/特征泄漏都必须拦下，不允许"带病完成"）
        if ! $PYTHON "$VERIFY_SCRIPT" "$PRODUCT_NAME" "$BUNDLE_PATH"; then
            echo "ERROR: Bundle verification FAILED" >&2
            exit 1
        fi
        echo "Bundle verification PASSED"
    else
        echo "WARNING: Bundle directory not found at $BUNDLE_PATH" >&2
        echo "  Expected directory structure: dist/$PRODUCT_NAME/$PRODUCT_NAME/" >&2
    fi
else
    echo "WARNING: verify_product_bundle.py not found, skipping verification" >&2
fi

# ---- Status report ----

echo ""
echo "=============================================="
echo "=== BUILD COMPLETE: $PRODUCT_NAME ==="
echo "=============================================="
echo ""
echo "Summary:"
echo "  Product:       $PRODUCT_NAME"
echo "  Entry point:   $ENTRY_MAP_LIB"
echo "  Include dirs:  $INCLUDE_DIRS"
echo "  Exclude dirs:  $EXCLUDE_DIRS"
echo "  Dist dir:      $DIST_DIR"
echo "  Build dir:     $BUILD_DIR"
echo "  TOPOS_PRODUCT_MODE: $TOPOS_PRODUCT_MODE"
echo ""

if [ -d "$DIST_DIR" ]; then
    echo "Bundle contents:"
    ls -la "$DIST_DIR/" 2>/dev/null || true
    if [ -d "$DIST_DIR/$PRODUCT_NAME" ]; then
        echo ""
        echo "Application directory:"
        ls -la "$DIST_DIR/$PRODUCT_NAME/" 2>/dev/null || true
    fi
fi

echo ""
echo "=== Done ==="
