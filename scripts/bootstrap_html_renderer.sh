#!/usr/bin/env bash
# =============================================================================
# HTML 渲染后端引导脚本（W0-A / W2）
#
# 用途：安装固定的 Playwright python 包 + 匹配的 Chromium headless shell，
# 让 HTML 动效层的确定性抓取后端在本机可用。W2 审计 P1：此前固定版本只写在
# ADR 0001/0002 里，没有任何可执行安装步骤，所以真实浏览器 gate 一直跳过。
#
# 固定版本（ADR 0002）：playwright==1.62.0 + chrome-151.0.7922.34
#   - python 包由 `pip install -e ".[html-renderer]"` 固定；
#   - 浏览器二进制由 `playwright install chromium` 下载该 playwright 版本对应的
#     headless shell build。两侧都固定才能让 backend_build 字符串
#     `playwright-chromium/py1.62.0/chrome-151.0.7922.34` 跨机器可复现——
#     浮动的浏览器会静默改变像素，破坏 W0-A 确定性 gate。
#
# 用法：
#   ./scripts/bootstrap_html_renderer.sh                # 使用 .venv（不存在则创建）
#   VENV_PATH=.venv-ci ./scripts/bootstrap_html_renderer.sh
#
# 退出码：0=成功且后端 is_available() 为 True；1=安装或验证失败。
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VENV_PATH="${VENV_PATH:-.venv}"
if [ ! -d "$VENV_PATH" ]; then
  python -m venv "$VENV_PATH"
fi

PYTHON="$VENV_PATH/bin/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="$VENV_PATH/Scripts/python"  # Windows
fi

echo "==> Installing project with html-renderer extra (pinned playwright)"
"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install -e ".[dev,html-renderer]"

echo "==> Installing pinned Chromium headless shell"
"$PYTHON" -m playwright install chromium

echo "==> Verifying backend availability (must NOT skip)"
"$PYTHON" - <<'PY'
from src.features.teaching_animation.render.html_renderer_spike.playwright_backend import (
    PlaywrightChromiumBackend,
)
backend = PlaywrightChromiumBackend()
if not backend.is_available():
    raise SystemExit(
        "FAIL: pinned Chromium backend reports unavailable. "
        "Re-run `playwright install chromium` and check the headless shell build."
    )
build = backend.backend_build
assert build.startswith("playwright-chromium/py"), build
assert "/chrome-" in build, build
print("OK backend_build =", build)
PY

echo "==> HTML renderer backend ready."
