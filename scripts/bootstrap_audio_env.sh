#!/usr/bin/env bash
# =============================================================================
# Audio DSP 环境引导脚本（RMA-01 阶段 A1）
#
# 用途：在干净的虚拟环境里安装项目依赖 + dev/test 依赖，并验证 SciPy DSP 后端
# 真的被交付。审计文档（RMA-01）的核心结论：requirements 声明了 scipy 但目标环境
# 未安装 → capability gate 静默降级 → EQ/LUFS/normalize 产出不可信。
#
# 本脚本把"安装 + 验证"固化为可重复的一步，供开发者和 CI 复用。
#
# 用法：
#   ./scripts/bootstrap_audio_env.sh          # 使用 .venv（不存在则创建）
#   VENV_PATH=.venv-ci ./scripts/bootstrap_audio_env.sh
#
# 退出码：0=成功且 scipy 后端可用；1=安装或验证失败。
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VENV_PATH="${VENV_PATH:-.venv}"

# 颜色输出（CI 友好，无 TTY 时不退化）
GREEN=$'\033[0;32m'
RED=$'\033[0;31m'
YELLOW=$'\033[1;33m'
NC=$'\033[0m'

log() { echo "${GREEN}[bootstrap]${NC} $*"; }
warn() { echo "${YELLOW}[bootstrap]${NC} $*"; }
err() { echo "${RED}[bootstrap]${NC} $*" >&2; }

# ── 1. 创建/复用虚拟环境 ──
if [ ! -d "$VENV_PATH" ]; then
  log "Creating virtual environment at $VENV_PATH"
  python3 -m venv "$VENV_PATH"
fi

if [ -x "$VENV_PATH/bin/python" ]; then
  PY="$VENV_PATH/bin/python"
elif [ -x "$VENV_PATH/Scripts/python.exe" ]; then
  PY="$VENV_PATH/Scripts/python.exe"
else
  err "No python interpreter found in $VENV_PATH"
  exit 1
fi

# ── 2. 升级 pip ──
log "Upgrading pip"
"$PY" -m pip install --upgrade pip >/dev/null

# ── 3. 安装项目依赖（含 scipy）+ dev/test 依赖 ──
log "Installing requirements.txt (includes scipy>=1.17.0)"
"$PY" -m pip install -r requirements.txt

# dev 依赖（pytest 等）声明在 pyproject.toml [project.optional-dependencies] dev，
# 但 requirements.txt 不含它们；为让音频回归可跑，这里显式补装。
log "Installing dev/test dependencies (pytest, pytest-qt)"
"$PY" -m pip install pytest pytest-qt

# ── 4. RMA-01 gate：验证 SciPy DSP 后端真的可用 ──
log "Verifying SciPy DSP backend present (RMA-01 gate)"
"$PY" - <<'PYEOF'
import sys
try:
    import scipy
    from scipy.signal import lfilter  # EQ/LUFS 实际依赖点
except ImportError as e:
    print(f"[bootstrap] FAIL: scipy backend missing after install: {e}", file=sys.stderr)
    sys.exit(1)

# 复用项目能力探测器，确保 audio_capabilities 也判定为 available
from src.features.edit.core.audio_capabilities import probe_audio_capabilities
caps = probe_audio_capabilities(check_output_device=False)
if not caps.scipy.available:
    print(f"[bootstrap] FAIL: audio_capabilities.scipy.available=False: {caps.scipy.reason}", file=sys.stderr)
    sys.exit(1)
if not caps.eq_lufs_available:
    print("[bootstrap] FAIL: eq_lufs_available=False after clean install", file=sys.stderr)
    sys.exit(1)
print(f"[bootstrap] OK: scipy {scipy.__version__} present, EQ/LUFS backend available")
PYEOF

log "Audio environment ready. Run tests with:"
echo "    $PY -m pytest -q tests/edit/test_audio_capabilities.py tests/edit/test_audio_export_preflight.py"
