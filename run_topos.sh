#!/bin/bash

# ███████╗████████╗███████╗██╗░░░░░██╗░░░░░
# ██╔════╝╚══██╔══╝██╔════╝██║░░░░░██║░░░░░
# █████╗░░░░░██║░░░█████╗░░██║░░░░░██║░░░░░
# ██╔══╝░░░░░██║░░░██╔══╝░░██║░░░░░██║░░░░░
# ██║░░░░░░░░██║░░░███████╗███████╗███████╗
# ╚═╝░░░░░░░░╚═╝░░░╚══════╝╚══════╝╚══════╝
# Topos Color - GPU-Accelerated Video/Image Grading Software
#
# run_topos.sh — Linux 一键启动脚本 (带架构检测 + 依赖自检)
# Linux launch script with architecture detection & dependency self-check

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
cd "$(dirname "$0")" || exit 1

echo ""
echo -e "${BOLD}${ICON_ROCKET} 正在启动 Topos Color... / Starting Topos Color...${NC}"
echo "────────────────────────────────────────"

# ══════════════════════════════════════════════════════════════════════════════
# Step 1: 检测系统环境 / Detect System Environment
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${BOLD}${ICON_CHIP} Step 1/4: 检测系统环境 / Detecting System...${NC}"
echo ""

# ── 检测发行版 ────────────────────────────────────────────────────────────────
DISTRO_NAME="Unknown Linux"
DISTRO_ID=""
if [ -f /etc/os-release ]; then
  . /etc/os-release
  DISTRO_NAME="${PRETTY_NAME:-$NAME}"
  DISTRO_ID="${ID:-unknown}"
fi
echo -e "${ICON_OK} ${GREEN}操作系统 / OS: ${DISTRO_NAME}${NC}"

# ── 检测 CPU 架构 ─────────────────────────────────────────────────────────────
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64)
    echo -e "${ICON_OK} ${GREEN}架构 / Architecture: x86_64${NC}"
    ;;
  aarch64|arm64)
    echo -e "${ICON_OK} ${GREEN}架构 / Architecture: ARM64 (aarch64)${NC}"
    ;;
  *)
    echo -e "${ICON_INFO} ${DIM}架构 / Architecture: ${ARCH}${NC}"
    ;;
esac

# ── 检测 GPU 环境 ─────────────────────────────────────────────────────────────
HAS_NVIDIA=false
HAS_AMD=false
HAS_INTEL=false

if command -v nvidia-smi &>/dev/null; then
  HAS_NVIDIA=true
  GPU_INFO="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo 'NVIDIA GPU')"
  echo -e "${ICON_OK} ${GREEN}GPU: ${GPU_INFO}${NC}"
elif [ -d /sys/class/drm ]; then
  for card in /sys/class/drm/card*/device/vendor; do
    [ -f "$card" ] || continue
    vendor="$(cat "$card" 2>/dev/null || echo '')"
    case "$vendor" in
      0x10de) HAS_NVIDIA=true; echo -e "${ICON_OK} ${GREEN}GPU: NVIDIA${NC}"; break ;;
      0x1002) HAS_AMD=true;     echo -e "${ICON_OK} ${GREEN}GPU: AMD${NC}"; break ;;
      0x8086) HAS_INTEL=true;   echo -e "${ICON_OK} ${GREEN}GPU: Intel${NC}"; break ;;
    esac
  done
fi

# ── 检测 GPU 驱动 (OpenGL) ────────────────────────────────────────────────────
if command -v glxinfo &>/dev/null; then
  GL_RENDERER="$(glxinfo -B 2>/dev/null | grep 'OpenGL renderer string' | cut -d: -f2- | xargs || echo 'unknown')"
  if [ "$GL_RENDERER" != "unknown" ]; then
    echo -e "${ICON_OK} ${GREEN}OpenGL: ${GL_RENDERER}${NC}"
  fi
else
  echo -e "${ICON_WARN} ${YELLOW}glxinfo 未安装，无法检测 OpenGL 驱动 / glxinfo not installed, cannot detect OpenGL${NC}"
  echo -e "         ${DIM}安装 / Install: sudo apt install mesa-utils${NC}"
fi

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
else
  echo -e "${ICON_WARN} ${YELLOW}未找到 .venv，尝试系统 Python / .venv not found, trying system Python...${NC}"
  if command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
  elif command -v python &> /dev/null; then
    PYTHON_CMD="python"
  else
    echo -e "${ICON_FAIL} ${RED}未找到 Python！请先安装 Python 3.10+${NC}"
    echo -e "${ICON_FAIL} ${RED}Python not found! Please install Python 3.10+${NC}"
    echo ""
    echo -e "         ${CYAN}Ubuntu 安装方法 / Ubuntu install:${NC}"
    echo -e "         ${CYAN}  sudo apt update && sudo apt install python3 python3-venv python3-pip${NC}"
    echo ""
    echo "按回车键退出 / Press Enter to exit..."
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
  echo -e "         ${CYAN}Ubuntu 升级方法 / Ubuntu upgrade:${NC}"
  echo -e "         ${CYAN}  sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.12${NC}"
  echo ""
  echo "按回车键退出 / Press Enter to exit..."
  read
  exit 1
fi
echo -e "${ICON_OK} ${GREEN}Python 版本 / Python version: ${PY_VERSION}${NC}"

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
  "PyGObject|gi|H|GStreamer 媒体功能不可用|GStreamer media features unavailable|GStreamer"
  "psutil|psutil|O|性能监控不可用|Performance monitoring unavailable|System Monitor"
  "Send2Trash|send2trash|O|删除文件不经过回收站|Files deleted permanently (no trash)|File Management"
  "Flask|flask|O|内部预览服务不可用|Internal preview service unavailable|Preview"
  "rawpy|rawpy|O|RAW 格式图片无法打开|Cannot open RAW image files|RAW Support"
  "Pillow-HEIF|pillow_heif|O|HEIF/HEIC 格式不可用|HEIF/HEIC format unavailable|HEIF Support"
  "pycairo|cairo|O|Cairo 绑定不可用|Cairo bindings unavailable|Cairo"
)

CRITICAL_FAILED=0
HIGH_FAILED=0
OPTIONAL_FAILED=0
TOTAL_CHECKS=${#DEPENDENCIES[@]}
PASSED=0
MISSING_PIP_PACKAGES=()

for dep in "${DEPENDENCIES[@]}"; do
  IFS='|' read -r name import_name severity consequence_cn consequence_en feature_area <<< "$dep"

  if $PYTHON_CMD -c "import ${import_name}" 2>/dev/null; then
    echo -e "${ICON_OK} ${GREEN}${name}${NC}  ${DIM}${feature_area}${NC}"
    PASSED=$((PASSED + 1))
  else
    # 智能映射 import 名到 pip 包名
    case "$import_name" in
      PIL)       pip_name="Pillow" ;;
      cv2)       pip_name="opencv-python" ;;
      gi)        pip_name="PyGObject" ;;
      cairo)     pip_name="pycairo" ;;
      *)         pip_name="$import_name" ;;
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

# ── Linux 系统级依赖预检 ─────────────────────────────────────────────────────
# PyGObject 和 pycairo 需要 C 系统库
NEED_SYSTEM_DEPS=false

if ! $PYTHON_CMD -c "import gi" 2>/dev/null; then
  NEED_SYSTEM_DEPS=true
fi
if ! $PYTHON_CMD -c "import cairo" 2>/dev/null; then
  NEED_SYSTEM_DEPS=true
fi

if [ "$NEED_SYSTEM_DEPS" = true ]; then
  echo ""
  echo -e "${ICON_WARN} ${YELLOW}检测到 Python 包需要系统级 C 库 / Python packages need system C libraries${NC}"

  case "$DISTRO_ID" in
    ubuntu|debian|linuxmint|pop)
      echo -e "         ${CYAN}Ubuntu/Debian 安装系统依赖 / Install system deps:${NC}"
      echo -e "         ${CYAN}  sudo apt install libgirepository1.0-dev gcc libcairo2-dev pkg-config python3-dev${NC}"
      SYSTEM_INSTALL_CMD="sudo apt install libgirepository1.0-dev gcc libcairo2-dev pkg-config python3-dev"
      ;;
    fedora|rhel|centos|rocky|alma)
      echo -e "         ${CYAN}Fedora/RHEL 安装系统依赖 / Install system deps:${NC}"
      echo -e "         ${CYAN}  sudo dnf install gobject-introspection-devel cairo-devel pkg-config python3-devel${NC}"
      SYSTEM_INSTALL_CMD="sudo dnf install gobject-introspection-devel cairo-devel pkg-config python3-devel"
      ;;
    arch|manjaro|endeavouros)
      echo -e "         ${CYAN}Arch/Manjaro 安装系统依赖 / Install system deps:${NC}"
      echo -e "         ${CYAN}  sudo pacman -S gobject-introspection cairo pkg-config python${NC}"
      SYSTEM_INSTALL_CMD="sudo pacman -S gobject-introspection cairo pkg-config python"
      ;;
    *)
      echo -e "         ${CYAN}请安装以下系统库 / Please install system libraries:${NC}"
      echo -e "         ${CYAN}  gobject-introspection, cairo, pkg-config, python3-dev${NC}"
      SYSTEM_INSTALL_CMD=""
      ;;
  esac
fi

# OpenGL 驱动预检
if ! command -v glxinfo &>/dev/null; then
  echo ""
  echo -e "${ICON_WARN} ${YELLOW}缺少 OpenGL 工具 (glxinfo) / Missing OpenGL utils (glxinfo)${NC}"
  echo -e "         ${ORANGE}如遇 GPU 渲染问题，请安装 / If GPU rendering fails, install:${NC}"
  case "$DISTRO_ID" in
    ubuntu|debian|linuxmint|pop)
      echo -e "         ${CYAN}  sudo apt install mesa-utils libgl1-mesa-glx libegl1-mesa${NC}"
      ;;
    fedora|rhel|centos)
      echo -e "         ${CYAN}  sudo dnf install mesa-libGL mesa-libEGL glx-utils${NC}"
      ;;
  esac
fi

# ── 自检结果汇总 / Summary ───────────────────────────────────────────────────
echo ""
echo "────────────────────────────────────────"
echo -e "${BOLD}自检结果 / Check Summary:${NC}"
echo -e "  通过/Passed: ${GREEN}${PASSED}${NC} / ${TOTAL_CHECKS}"
echo -e "  系统/System: ${DISTRO_NAME} (${ARCH})"

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

if [ "$TOTAL_FAILED" -gt 0 ] || [ "$NEED_SYSTEM_DEPS" = true ]; then
  echo ""
  echo "────────────────────────────────────────"

  if [ "$CRITICAL_FAILED" -gt 0 ]; then
    echo -e "${RED}${BOLD}${ICON_FAIL} 存在关键依赖缺失，无法启动！/ Critical dependencies missing!${NC}"
    echo ""

    # 如果需要系统依赖，先提示安装
    if [ "$NEED_SYSTEM_DEPS" = true ] && [ -n "$SYSTEM_INSTALL_CMD" ]; then
      echo -e "${CYAN}Step A — 安装系统级依赖 / Install system-level deps:${NC}"
      echo -e "  ${SYSTEM_INSTALL_CMD}${NC}"
      echo ""
    fi

    echo -e "${CYAN}Step B — 安装 Python 依赖 / Install Python deps:${NC}"
    echo -e "  ${PYTHON_CMD} -m pip install -r requirements.txt${NC}"
    echo ""
    echo "按回车键退出 / Press Enter to exit..."
    read
    exit 1
  fi

  echo -e "${YELLOW}${BOLD}${ICON_WARN} 部分功能将不可用 / Some features will be unavailable${NC}"
  echo ""

  TOTAL_IMPORTANT=$((HIGH_FAILED + OPTIONAL_FAILED))

  # 如果需要系统依赖，先处理
  if [ "$NEED_SYSTEM_DEPS" = true ] && [ -n "$SYSTEM_INSTALL_CMD" ]; then
    echo -e "${CYAN}系统依赖 / System deps:${NC}"
    echo -e "  ${SYSTEM_INSTALL_CMD}${NC}"
    echo ""
  fi

  if [ "$TOTAL_IMPORTANT" -gt 0 ]; then
    echo -e "${CYAN}Python 依赖 / Python deps:${NC}"
    echo -e "  ${PYTHON_CMD} -m pip install ${MISSING_PIP_PACKAGES[*]}${NC}"
    echo ""

    echo -en "${BOLD}是否自动安装缺失的 Python 依赖？ / Auto-install missing Python deps? [Y/n]: ${NC}"
    read -r answer
    case "$answer" in
      [nN][oO]|[nN])
        echo ""
        echo -e "${YELLOW}跳过安装，继续启动... / Skipping install, continuing...${NC}"
        ;;
      *)
        echo ""
        echo -e "${ICON_PKG} 正在安装缺失的 Python 依赖 / Installing missing Python dependencies..."
        echo ""

        # 如果缺少系统依赖，先提醒
        if [ "$NEED_SYSTEM_DEPS" = true ]; then
          echo -e "${ICON_WARN} ${YELLOW}注意：PyGObject/pycairo 需要先安装系统依赖，否则可能编译失败${NC}"
          echo -e "${ICON_WARN} ${YELLOW}Note: PyGObject/pycairo require system deps first${NC}"
          if [ -n "$SYSTEM_INSTALL_CMD" ]; then
            echo -en "${BOLD}是否先运行系统依赖安装？ / Install system deps first? [y/N]: ${NC}"
            read -r sys_answer
            case "$sys_answer" in
              [yY][eE][sS]|[yY])
                echo ""
                echo -e "${ICON_PKG} 安装系统依赖 / Installing system dependencies..."
                echo -e "${DIM}  ${SYSTEM_INSTALL_CMD}${NC}"
                echo ""
                eval "$SYSTEM_INSTALL_CMD" 2>&1 || {
                  echo -e "${ICON_WARN} ${YELLOW}系统依赖安装失败 (需要 sudo 权限) / System deps install failed (needs sudo)${NC}"
                }
                echo ""
                ;;
            esac
          fi
        fi

        $PYTHON_CMD -m pip install ${MISSING_PIP_PACKAGES[*]} 2>&1 || {
          echo ""
          echo -e "${ICON_WARN} ${YELLOW}部分安装失败，尝试全量安装... / Some failed, trying full install...${NC}"
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
