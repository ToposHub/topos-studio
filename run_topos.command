#!/bin/bash

# ███████╗████████╗███████╗██╗░░░░░██╗░░░░░
# ██╔════╝╚══██╔══╝██╔════╝██║░░░░░██║░░░░░
# █████╗░░░░░██║░░░█████╗░░██║░░░░░██║░░░░░
# ██╔══╝░░░░░██║░░░██╔══╝░░██║░░░░░██║░░░░░
# ██║░░░░░░░░██║░░░███████╗███████╗███████╗
# ╚═╝░░░░░░░░╚═╝░░░╚══════╝╚══════╝╚══════╝
# Topos Color - GPU-Accelerated Video/Image Grading Software
#
# run_topos.command — 一键启动脚本 (带架构检测 + 依赖自检)
# Launch script with architecture detection & dependency self-check

set -euo pipefail

# ── 颜色定义 / Color Definitions ─────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
ORANGE='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# ── 图标 / Icons ─────────────────────────────────────────────────────────────
ICON_OK="  ✅"
ICON_FAIL="  ❌"
ICON_WARN="  ⚠️ "
ICON_INFO="  ℹ️ "
ICON_ROCKET="🚀"
ICON_PKG="📦"
ICON_GAME="🎮"
ICON_GEAR="⚙️ "
ICON_CHIP="🖥️ "

# ── 进入脚本所在目录 ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
cd "$SCRIPT_DIR" || exit 1

# Each source checkout keeps its own user settings and dock layout.  Without
# this, multiple cloned Topos worktrees all write to the macOS-wide
# ~/Library/Application Support/Topos/config.json and overwrite one another's
# window state.  An explicitly supplied TOPOS_CONFIG_PATH still takes priority
# for release builds and automation.
if [ -z "${TOPOS_CONFIG_PATH:-}" ]; then
  export TOPOS_CONFIG_PATH="$SCRIPT_DIR/.topos/config.json"
fi

# ── 加载用户 PATH (安全方式，不 source 整个 .zshrc) ──────────────────────────
# .zshrc 包含 zsh 专有语法 (autoload/compinit)，在 bash 中 source 会导致 set -e 退出
# 只提取用户自定义的 PATH 条目即可
if [ -f "$HOME/.zshrc" ]; then
  # 临时关闭 nounset，避免引用未定义环境变量时报错
  _old_opts="$(set +o)"
  set +eu
  # 从 .zshrc 中提取 export PATH=... 行并 eval
  _user_path_line=$(grep -E '^\s*export\s+PATH=' "$HOME/.zshrc" 2>/dev/null | tail -1 || true)
  if [ -n "$_user_path_line" ]; then
    eval "$_user_path_line" 2>/dev/null || true
  fi
  # 提取 DYLD_LIBRARY_PATH (如果有)
  _dyld_line=$(grep -E '^\s*export\s+DYLD_LIBRARY_PATH=' "$HOME/.zshrc" 2>/dev/null | tail -1 || true)
  if [ -n "$_dyld_line" ]; then
    eval "$_dyld_line" 2>/dev/null || true
  fi
  # 提取 GI_TYPELIB_PATH (如果有)
  _gi_line=$(grep -E '^\s*export\s+GI_TYPELIB_PATH=' "$HOME/.zshrc" 2>/dev/null | tail -1 || true)
  if [ -n "$_gi_line" ]; then
    eval "$_gi_line" 2>/dev/null || true
  fi
  eval "$_old_opts" 2>/dev/null || true
  unset _old_opts _user_path_line _dyld_line _gi_line
fi

echo ""
echo -e "${BOLD}${ICON_ROCKET} 正在启动 Topos Color... / Starting Topos Color...${NC}"
echo "────────────────────────────────────────"

# ══════════════════════════════════════════════════════════════════════════════
# Step 1: 检测系统环境 / Detect System Environment
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${BOLD}${ICON_CHIP} Step 1/4: 检测系统环境 / Detecting System...${NC}"
echo ""

# ── 检测操作系统 ──────────────────────────────────────────────────────────────
OS_NAME="$(uname -s)"
case "$OS_NAME" in
  Darwin)
    OS_LABEL="macOS"
    MACOS_VERSION="$(sw_vers -productVersion 2>/dev/null || echo 'unknown')"
    echo -e "${ICON_OK} ${GREEN}操作系统 / OS: macOS ${MACOS_VERSION}${NC}"
    ;;
  Linux)
    OS_LABEL="Linux"
    # 尝试检测发行版
    if [ -f /etc/os-release ]; then
      . /etc/os-release
      echo -e "${ICON_OK} ${GREEN}操作系统 / OS: Linux (${NAME})${NC}"
    else
      echo -e "${ICON_OK} ${GREEN}操作系统 / OS: Linux${NC}"
    fi
    ;;
  *)
    echo -e "${ICON_WARN} ${YELLOW}操作系统 / OS: ${OS_NAME} (未经测试 / Untested)${NC}"
    OS_LABEL="$OS_NAME"
    ;;
esac

# ── 检测 CPU 架构 ─────────────────────────────────────────────────────────────
HARDWARE_NAME="$(uname -m)"
case "$HARDWARE_NAME" in
  arm64)
    ARCH_LABEL="arm64"
    CHIP_LABEL="Apple Silicon"
    echo -e "${ICON_OK} ${GREEN}芯片 / Chip: Apple Silicon (arm64)${NC}"
    ;;
  x86_64)
    ARCH_LABEL="x86_64"
    # macOS 上可能是 Rosetta
    if [ "$OS_NAME" = "Darwin" ]; then
      # 检查是否通过 Rosetta 运行
      if [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0)" = "1" ]; then
        echo -e "${ICON_WARN} ${YELLOW}芯片 / Chip: Apple Silicon (通过 Rosetta 运行 x86_64 / running x86_64 via Rosetta)${NC}"
        echo -e "         ${ORANGE}建议使用 arm64 原生 Python 以获得最佳性能${NC}"
        echo -e "         ${ORANGE}Recommend using arm64 native Python for best performance${NC}"
      else
        echo -e "${ICON_OK} ${GREEN}芯片 / Chip: Intel (x86_64)${NC}"
      fi
      CHIP_LABEL="Intel"
    else
      echo -e "${ICON_OK} ${GREEN}架构 / Architecture: x86_64${NC}"
      CHIP_LABEL="x86_64"
    fi
    ;;
  aarch64)
    ARCH_LABEL="aarch64"
    CHIP_LABEL="ARM64"
    echo -e "${ICON_OK} ${GREEN}架构 / Architecture: aarch64 (ARM64)${NC}"
    ;;
  *)
    ARCH_LABEL="$HARDWARE_NAME"
    CHIP_LABEL="$HARDWARE_NAME"
    echo -e "${ICON_INFO} ${DIM}架构 / Architecture: ${HARDWARE_NAME}${NC}"
    ;;
esac

# ══════════════════════════════════════════════════════════════════════════════
# Step 2: 检测 Python / Detect Python
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${BOLD}${ICON_GEAR} Step 2/4: 检测 Python 环境 / Detecting Python...${NC}"
echo ""

PYTHON_CMD=""
VENV_ACTIVE=0

if [ -x ".venv/bin/python" ]; then
  PYTHON_CMD=".venv/bin/python"
  VENV_ACTIVE=1
  echo -e "${ICON_OK} ${GREEN}找到虚拟环境 / Found virtual environment: .venv${NC}"

  # 检查 venv 的架构是否与系统匹配
  VENV_ARCH=$($PYTHON_CMD -c "import struct; print('arm64' if struct.calcsize('P') == 8 and __import__('platform').machine() == 'arm64' else 'x86_64')" 2>/dev/null || echo "unknown")
  if [ "$VENV_ARCH" != "$ARCH_LABEL" ] && [ "$VENV_ARCH" != "unknown" ]; then
    echo -e "${ICON_WARN} ${YELLOW}虚拟环境架构 (${VENV_ARCH}) 与系统架构 (${ARCH_LABEL}) 不匹配！${NC}"
    echo -e "         ${ORANGE}Virtual env architecture (${VENV_ARCH}) doesn't match system (${ARCH_LABEL})!${NC}"
    echo ""
    echo -e "         ${CYAN}建议重建虚拟环境 / Recommend recreating virtual environment:${NC}"
    echo -e "         ${CYAN}  rm -rf .venv && python3 -m venv .venv && source .venv/bin/activate${NC}"
    echo ""
  fi
else
  echo -e "${ICON_WARN} ${YELLOW}未找到 .venv，尝试系统 Python / .venv not found, trying system Python...${NC}"
  if command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
  elif command -v python &> /dev/null; then
    PYTHON_CMD="python"
  else
    echo -e "${ICON_FAIL} ${RED}未找到 Python！请先安装 Python 3.10+${NC}"
    echo -e "${ICON_FAIL} ${RED}Python not found! Please install Python 3.10+${NC}"
    if [ "$OS_NAME" = "Darwin" ]; then
      echo ""
      echo -e "         ${CYAN}macOS 安装方法 / macOS install methods:${NC}"
      echo -e "         ${CYAN}  1. 从 python.org 下载官方安装器 (推荐 arm64 版本)${NC}"
      echo -e "         ${CYAN}  2. brew install python@3.12${NC}"
    fi
    echo ""
    echo "按回车键关闭窗口 / Press Enter to close..."
    read
    exit 1
  fi
fi

# 检查 Python 版本
PY_VERSION=$($PYTHON_CMD -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>/dev/null || echo "0.0.0")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]); then
  echo -e "${ICON_FAIL} ${RED}Python 版本过低: ${PY_VERSION}，需要 3.10+${NC}"
  echo -e "${ICON_FAIL} ${RED}Python version too old: ${PY_VERSION}, requires 3.10+${NC}"
  echo ""
  echo "按回车键关闭窗口 / Press Enter to close..."
  read
  exit 1
fi

# 检查 Python 本身架构
PY_ARCH=$($PYTHON_CMD -c "import platform; print(platform.machine())" 2>/dev/null || echo "unknown")
echo -e "${ICON_OK} ${GREEN}Python 版本 / Python version: ${PY_VERSION} (${PY_ARCH})${NC}"

# ══════════════════════════════════════════════════════════════════════════════
# Step 3: 依赖自检 / Dependency Self-Check
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${BOLD}${ICON_PKG} Step 3/4: 依赖自检 / Dependency Check${NC}"
echo ""

# 格式: "显示名|import名|级别|后果中文|后果English|功能区域"
DEPENDENCIES=(
  "PyQt6|PyQt6|C|应用无法启动|App won't start|Core UI"
  "NumPy|numpy|C|应用无法启动|App won't start|Core Data"
  "OpenCV|cv2|C|无法处理图像和视频|Cannot process images & videos|Image/Video"
  "ModernGL|moderngl|C|GPU 渲染不可用|GPU rendering unavailable|GPU Rendering"
  "PyOpenGL|OpenGL|H|剪辑与合成页面不可用|Edit & Composite pages unavailable|Edit & Composite"
  "Pillow|PIL|H|无法加载部分图片格式|Cannot load some image formats|Image Loading"
  "SciPy|scipy|H|部分调色算法降级|Some color algorithms degraded|Color Grading"
  "ONNX Runtime|onnxruntime|H|AI 降噪等功能不可用|AI features (denoise etc.) unavailable|AI Features"
  "PyAV|av|H|视频解码/编码不可用|Video decode/encode unavailable|Video Processing"
  "QtAwesome|qtawesome|H|工具栏图标缺失|Toolbar icons missing|UI Icons"
  "MessagePack|msgpack|H|项目序列化失败|Project serialization fails|Save/Load"
  "psutil|psutil|O|性能监控不可用|Performance monitoring unavailable|System Monitor"
  "Send2Trash|send2trash|O|删除文件不经过回收站|Files deleted permanently (no trash)|File Management"
  "Flask|flask|O|内部预览服务不可用|Internal preview service unavailable|Preview"
  "python-vlc|vlc|O|VLC 媒体播放不可用|VLC media playback unavailable|Media Playback"
  "rawpy|rawpy|O|RAW 格式图片无法打开|Cannot open RAW image files|RAW Support"
  "Pillow-HEIF|pillow_heif|O|HEIF/HEIC 格式不可用|HEIF/HEIC format unavailable|HEIF Support"
  "OpenColorIO|PyOpenColorIO|O|专业色彩管理(OCIO)不可用,使用内置 sRGB/Linear|Pro colour management (OCIO) unavailable, using built-in sRGB/Linear|Colour Management"
  "trimesh|trimesh|O|3D 模型导入(glTF/OBJ/STL/PLY)不可用,降级为基础几何|3D model import (glTF/OBJ/STL/PLY) unavailable, falls back to primitives|3D Import"
  "pyassimp|pyassimp|O|FBX/3DS 模型导入不可用|FBX/3DS model import unavailable|3D Import (FBX)"
)

# macOS 专属依赖
if [ "$OS_NAME" = "Darwin" ]; then
  DEPENDENCIES+=(
    "pyobjc (VideoToolbox)|objc|H|macOS VideoToolbox 零拷贝解码不可用|macOS VideoToolbox zero-copy decode unavailable|macOS Video"
  )
fi

# Linux 专属依赖
if [ "$OS_NAME" = "Linux" ]; then
  DEPENDENCIES+=(
    "PyGObject|gi|H|GStreamer 媒体功能不可用|GStreamer media features unavailable|GStreamer"
    "pycairo|cairo|O|Cairo 绑定不可用|Cairo bindings unavailable|Cairo"
  )
fi

CRITICAL_FAILED=0
HIGH_FAILED=0
OPTIONAL_FAILED=0
TOTAL_CHECKS=${#DEPENDENCIES[@]}
PASSED=0

# 收集缺失包名用于智能安装
MISSING_PIP_PACKAGES=()

for dep in "${DEPENDENCIES[@]}"; do
  IFS='|' read -r name import_name severity consequence_cn consequence_en feature_area <<< "$dep"

  if $PYTHON_CMD -c "import ${import_name}" 2>/dev/null; then
    echo -e "${ICON_OK} ${GREEN}${name}${NC}  ${DIM}${feature_area}${NC}"
    PASSED=$((PASSED + 1))
  else
    # 智能映射 import 名到 pip 包名
    case "$import_name" in
      PIL)              pip_name="Pillow" ;;
      cv2)              pip_name="opencv-python" ;;
      gi)               pip_name="PyGObject" ;;
      cairo)            pip_name="pycairo" ;;
      PyOpenColorIO)    pip_name="opencolorio" ;;
      objc)             pip_name="pyobjc-framework-VideoToolbox pyobjc-framework-CoreMedia pyobjc-framework-Quartz pyobjc-framework-IOSurface" ;;
      *)                pip_name="$import_name" ;;
    esac

    case "$severity" in
      C)
        echo -e "${ICON_FAIL} ${RED}${BOLD}${name}${NC}  ${RED}[关键/CRITICAL]${NC}"
        echo -e "           ${RED}→ ${consequence_cn} / ${consequence_en}${NC}"
        CRITICAL_FAILED=$((CRITICAL_FAILED + 1))
        ;;
      H)
        echo -e "${ICON_WARN} ${YELLOW}${name}${NC}  ${YELLOW}[重要/HIGH]${NC}"
        echo -e "           ${ORANGE}→ ${consequence_cn} / ${consequence_en}${NC}"
        HIGH_FAILED=$((HIGH_FAILED + 1))
        ;;
      O)
        echo -e "${ICON_INFO} ${DIM}${name}${NC}  ${DIM}[可选/OPTIONAL]${NC}"
        echo -e "           ${DIM}→ ${consequence_cn} / ${consequence_en}${NC}"
        OPTIONAL_FAILED=$((OPTIONAL_FAILED + 1))
        ;;
    esac
    MISSING_PIP_PACKAGES+=("$pip_name")
  fi
done

# ── 系统级依赖预检 (macOS Apple Silicon) ─────────────────────────────────────
if [ "$OS_NAME" = "Darwin" ] && [ "$ARCH_LABEL" = "arm64" ]; then
  # 检查 Homebrew 是否安装
  if ! command -v brew &> /dev/null; then
    echo ""
    echo -e "${ICON_WARN} ${YELLOW}未检测到 Homebrew (Apple Silicon 推荐安装)${NC}"
    echo -e "         ${ORANGE}Homebrew not found (recommended for Apple Silicon)${NC}"
    echo -e "         ${DIM}安装 / Install: /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"${NC}"
  else
    # 检查 Homebrew 架构
    BREW_PREFIX="$(brew --prefix 2>/dev/null || echo '')"
    if [[ "$BREW_PREFIX" == "/usr/local" ]] && [ "$ARCH_LABEL" = "arm64" ]; then
      echo ""
      echo -e "${ICON_WARN} ${YELLOW}检测到 x86 Homebrew (/usr/local)，建议使用 arm64 版 (/opt/homebrew)${NC}"
      echo -e "         ${ORANGE}Detected x86 Homebrew. Recommend using arm64 version (/opt/homebrew)${NC}"
    fi
  fi
fi

# ── 系统级依赖预检：FBX/3DS 需要的 libassimp ─────────────────────────────────
# pyassimp (Python 绑定) 可能已装，但 FBX/3DS 实际解析依赖系统级 libassimp。
# 仅当用户装了 pyassimp 时才检测系统库，避免对不用 3D 导入的用户造成噪音。
if $PYTHON_CMD -c "import pyassimp" 2>/dev/null; then
  ASSIMP_LIB_OK=0
  if [ "$OS_NAME" = "Darwin" ]; then
    # macOS: 尝试常见 dylib 路径 + Homebrew 路径
    # 检查目录是否存在，避免 find 报错
    for _lib_dir in /usr/local/lib /opt/homebrew/lib; do
      if [ -d "$_lib_dir" ] && find "$_lib_dir" -name 'libassimp*.dylib' 2>/dev/null | grep -q .; then
        ASSIMP_LIB_OK=1
        break
      fi
    done
    # 如果上面没找到，尝试用 brew --prefix 获取正确路径
    if [ "$ASSIMP_LIB_OK" -eq 0 ] && command -v brew &>/dev/null; then
      _brew_assimp_prefix="$(brew --prefix assimp 2>/dev/null || echo '')"
      if [ -n "$_brew_assimp_prefix" ] && [ -d "$_brew_assimp_prefix/lib" ]; then
        if find "$_brew_assimp_prefix/lib" -name 'libassimp*.dylib' 2>/dev/null | grep -q .; then
          ASSIMP_LIB_OK=1
        fi
      fi
    fi
    # 最后尝试 otool 检查 pyassimp 模块的依赖
    if [ "$ASSIMP_LIB_OK" -eq 0 ]; then
      _pyassimp_helper="$($PYTHON_CMD -c 'import pyassimp.helper as h; print(h.__file__)' 2>/dev/null || echo '')"
      if [ -n "$_pyassimp_helper" ] && otool -L "$_pyassimp_helper" 2>/dev/null | grep -qi assimp; then
        ASSIMP_LIB_OK=1
      fi
    fi
  elif [ "$OS_NAME" = "Linux" ]; then
    # Linux: ldconfig 或常见 .so 路径
    if ldconfig -p 2>/dev/null | grep -qi 'libassimp' \
       || find /usr/lib /usr/local/lib -name 'libassimp.so*' 2>/dev/null | grep -q .; then
      ASSIMP_LIB_OK=1
    fi
  fi

  if [ "$ASSIMP_LIB_OK" -eq 0 ]; then
    echo ""
    echo -e "${ICON_WARN} ${YELLOW}pyassimp 已安装但缺少系统库 libassimp (FBX 导入需要)${NC}"
    echo -e "         ${ORANGE}pyassimp installed but system library libassimp missing (required for FBX)${NC}"
    if [ "$OS_NAME" = "Darwin" ]; then
      echo -e "         ${CYAN}安装 / Install: brew install assimp${NC}"
      # 若有 Homebrew，提示自动安装
      if command -v brew &> /dev/null; then
        echo -en "${BOLD}是否用 Homebrew 安装 assimp？ / Install assimp via Homebrew? [Y/n]: ${NC}"
        read -r _assimp_answer
        case "$_assimp_answer" in
          [nN][oO]|[nN])
            echo -e "${DIM}跳过，FBX 导入将不可用（其它功能不受影响）/ Skipped, FBX import unavailable${NC}"
            ;;
          *)
            echo -e "${ICON_PKG} ${DIM}brew install assimp ...${NC}"
            brew install assimp 2>&1 || echo -e "${ICON_WARN} ${YELLOW}安装失败，请手动运行 brew install assimp${NC}"
            ;;
        esac
      fi
    elif [ "$OS_NAME" = "Linux" ]; then
      echo -e "         ${CYAN}Ubuntu/Debian: sudo apt install libassimp-dev${NC}"
      echo -e "         ${CYAN}Fedora:       sudo dnf install assimp-devel${NC}"
    fi
  fi
fi

# ── 系统级依赖预检 (Linux) ──────────────────────────────────────────────────
if [ "$OS_NAME" = "Linux" ]; then
  # 检查 PyGObject 系统依赖
  if ! $PYTHON_CMD -c "import gi" 2>/dev/null; then
    if ! dpkg -s libgirepository1.0-dev &>/dev/null 2>&1; then
      echo ""
      echo -e "${ICON_WARN} ${YELLOW}PyGObject 缺少系统依赖 / PyGObject missing system deps${NC}"
      echo -e "         ${CYAN}Ubuntu/Debian: sudo apt install libgirepository1.0-dev gcc libcairo2-dev pkg-config python3-dev${NC}"
      echo -e "         ${CYAN}Fedora: sudo dnf install gobject-introspection-devel cairo-devel pkg-config python3-devel${NC}"
    fi
  fi
fi

# ── 自检结果汇总 / Summary ───────────────────────────────────────────────────
echo ""
echo "────────────────────────────────────────"
echo -e "${BOLD}自检结果 / Check Summary:${NC}"
echo -e "  通过/Passed: ${GREEN}${PASSED}${NC} / ${TOTAL_CHECKS}"
[ "$OS_NAME" = "Darwin" ] && echo -e "  系统/System: macOS (${CHIP_LABEL}, ${PY_ARCH})"
[ "$OS_NAME" = "Linux" ] && echo -e "  系统/System: Linux (${PY_ARCH})"

if [ "$CRITICAL_FAILED" -gt 0 ]; then
  echo -e "  关键缺失/Critical:  ${RED}${CRITICAL_FAILED}${NC}"
fi
if [ "$HIGH_FAILED" -gt 0 ]; then
  echo -e "  重要缺失/High:      ${YELLOW}${HIGH_FAILED}${NC}"
fi
if [ "$OPTIONAL_FAILED" -gt 0 ]; then
  echo -e "  可选缺失/Optional:  ${DIM}${OPTIONAL_FAILED}${NC}"
fi

# ══════════════════════════════════════════════════════════════════════════════
# Step 4: 处理缺失依赖 / Handle Missing Dependencies
# ══════════════════════════════════════════════════════════════════════════════

TOTAL_FAILED=$((CRITICAL_FAILED + HIGH_FAILED + OPTIONAL_FAILED))

if [ "$TOTAL_FAILED" -gt 0 ]; then
  echo ""
  echo "────────────────────────────────────────"

  if [ "$CRITICAL_FAILED" -gt 0 ]; then
    echo -e "${RED}${BOLD}${ICON_FAIL} 存在关键依赖缺失，无法启动！/ Critical dependencies missing, cannot start!${NC}"
    echo ""
    echo -e "${CYAN}修复方法 / Fix:${NC}"
    echo -e "  ${PYTHON_CMD} -m pip install -r requirements.txt"
    echo ""
    echo "按回车键关闭窗口 / Press Enter to close..."
    read
    exit 1
  fi

  echo -e "${YELLOW}${BOLD}${ICON_WARN} 部分功能将不可用 / Some features will be unavailable${NC}"
  echo ""

  TOTAL_IMPORTANT=$((HIGH_FAILED + OPTIONAL_FAILED))
  if [ "$TOTAL_IMPORTANT" -gt 0 ]; then
    echo -e "${CYAN}修复方法 / Fix:${NC}"
    echo -e "  ${PYTHON_CMD} -m pip install ${MISSING_PIP_PACKAGES[*]}"
    echo ""

    echo -en "${BOLD}是否自动安装缺失依赖？ / Auto-install missing dependencies? [Y/n]: ${NC}"
    read -r answer
    case "$answer" in
      [nN][oO]|[nN])
        echo ""
        echo -e "${YELLOW}跳过安装，继续启动... / Skipping install, continuing...${NC}"
        ;;
      *)
        echo ""
        echo -e "${ICON_PKG} 正在安装缺失依赖 (${ARCH_LABEL}) / Installing missing dependencies...${NC}"
        echo ""

        # 智能安装：根据架构选择正确的方式
        if [ "$OS_NAME" = "Darwin" ] && [ "$ARCH_LABEL" = "arm64" ]; then
          echo -e "${DIM}  Apple Silicon 模式 — 使用 arm64 原生 wheels${NC}"
        elif [ "$OS_NAME" = "Darwin" ] && [ "$ARCH_LABEL" = "x86_64" ]; then
          echo -e "${DIM}  Intel Mac 模式 — 使用 x86_64 wheels${NC}"
        fi

        $PYTHON_CMD -m pip install ${MISSING_PIP_PACKAGES[*]} 2>&1 || {
          echo ""
          echo -e "${ICON_WARN} ${YELLOW}部分依赖安装失败，尝试使用 requirements.txt 全量安装...${NC}"
          echo -e "${ICON_WARN} ${YELLOW}Some installs failed, trying full requirements.txt...${NC}"
          echo ""
          $PYTHON_CMD -m pip install -r requirements.txt 2>&1 || true
        }
        echo ""
        echo -e "${ICON_OK} ${GREEN}依赖安装完成 / Dependencies installed${NC}"
        ;;
    esac
  fi
fi

# ══════════════════════════════════════════════════════════════════════════════
# Step 5: 启动应用 / Launch Application
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo "────────────────────────────────────────"
echo -e "${BOLD}${ICON_GAME} Step 4/4: 启动 Topos Color / Launching Topos Color...${NC}"
echo "────────────────────────────────────────"
echo ""

$PYTHON_CMD src/main.py

# ── 退出处理 / Exit Handler ──────────────────────────────────────────────────
echo ""
echo -e "${DIM}程序已退出 / Application exited.${NC}"
echo -e "${DIM}按回车键关闭窗口 / Press Enter to close...${NC}"
read
