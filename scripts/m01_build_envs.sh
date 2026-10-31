#!/usr/bin/env bash
# M01 · 构建并验证 Mac 隔离依赖环境（核心 UI/视频 + 可选 AI worker）。
# 计划：docs/upgrade_plan_M_mac_2026-10-24.md §M01。
#
# 用法：scripts/m01_build_envs.sh [core|ai|all]
#   core —— 构建核心环境 .venv-m01-core（主 venv 冻结 − AI 栈），pip check + focused 门禁
#   ai   —— 构建 AI worker 环境 .venv-m01-ai（requirements-ai-worker.txt），pip check + 导入冒烟
#
# 现有 .venv 保持原样供对照（不原地升级）；两个新环境互不依赖、可独立删除。

set -euo pipefail
cd "$(dirname "$0")/.."

PY312="${TOPOS_PY312:-/usr/local/Cellar/python@3.12/3.12.13/Frameworks/Python.framework/Versions/3.12/bin/python3.12}"
WHICH="${1:-all}"

build_core() {
  echo "[m01] building core env (.venv-m01-core) from main-venv freeze minus AI stack..."
  "$PY312" -m venv .venv-m01-core
  .venv-m01-core/bin/python -m pip install --disable-pip-version-check --upgrade pip >/dev/null
  # 主 venv 是当前已验证可用的版本组合；按它逐包冻结（排除 AI 栈），
  # 而不是让 resolver 重新选版本——避免引入未经本机验证的新组合。
  # 排除集由反向依赖闭包计算（scripts/m01_core_freeze_filter.py），
  # 须用主 venv 的 python 运行（读主 venv 的发行版元数据）。
  .venv/bin/pip freeze --exclude-editable > /tmp/m01_full_freeze.txt
  .venv/bin/python scripts/m01_core_freeze_filter.py /tmp/m01_full_freeze.txt > /tmp/m01_core_freeze.txt
  .venv-m01-core/bin/python -m pip install --disable-pip-version-check -r /tmp/m01_core_freeze.txt
  echo "[m01] core pip check:"
  .venv-m01-core/bin/python -m pip check
}

build_ai() {
  echo "[m01] building AI worker env (.venv-m01-ai, ASR combo) from requirements-ai-worker.txt..."
  "$PY312" -m venv .venv-m01-ai
  .venv-m01-ai/bin/python -m pip install --disable-pip-version-check --upgrade pip >/dev/null
  .venv-m01-ai/bin/python -m pip install --disable-pip-version-check -r requirements-ai-worker.txt
  echo "[m01] AI worker pip check:"
  .venv-m01-ai/bin/python -m pip check
}

build_tts() {
  echo "[m01] building TTS worker env (.venv-m01-tts) from requirements-ai-worker-tts.txt..."
  "$PY312" -m venv .venv-m01-tts
  .venv-m01-tts/bin/python -m pip install --disable-pip-version-check --upgrade pip >/dev/null
  .venv-m01-tts/bin/python -m pip install --disable-pip-version-check -r requirements-ai-worker-tts.txt
  echo "[m01] TTS worker pip check:"
  .venv-m01-tts/bin/python -m pip check
}

case "$WHICH" in
  core) build_core ;;
  ai) build_ai ;;
  tts) build_tts ;;
  all) build_core; build_ai; build_tts ;;
  *) echo "usage: $0 [core|ai|tts|all]" >&2; exit 2 ;;
esac
echo "[m01] done."
