# 安装与环境 / Installation and Environment

> getstarted · 计划 04 P3（2026-09-28）。本文内容整理自 `README.md` / `README_EN.md`（迁移瘦身），未新增事实。
> 平台相关问题排查见 [faq.md](faq.md)；全部文档导航见 [docs/INDEX.md](../INDEX.md)。
> 本文为双语文档：简体中文在前，English follows below。

## 中文

### 1. 兼容性声明

**本项目仅支持 Qt6（PyQt6 ≥ 6.10.2，requirements.txt 所列版本），不支持 Qt5。** 内嵌素材管理器（file_explorer）的前端使用了 `replaceAll`、`:scope`、`aspect-ratio` 等 Web 特性，要求 Chromium ≥ 88；本项目依赖的 QtWebEngine 6.10（Chromium ≥ 122）满足，Qt5 的 QtWebEngine（Chromium ≤ 87）**无法**满足，因此不做 Qt5 兼容。

**Python 版本口径**（P3-DOC-02）：`pyproject.toml` 声明 **Python ≥ 3.10**，CI 在 **3.10 / 3.12** 双版本验证；macOS 26 平台**建议** 3.13+（该平台已知兼容问题的下限仍以 CI 矩阵为准）。Python 3.9 及以下不受支持。

### 2. 安装步骤

```bash
# 1. 创建虚拟环境（架构迁移须知见下文第 4 节）
python3 -m venv .venv
source .venv/bin/activate        # macOS / Linux
# 或 .venv\Scripts\activate      # Windows

# 2. 安装运行依赖
pip install -r requirements.txt

# 3. 以可编辑模式安装本包（同时获得 CLI 命令：topos-studio / topos-color / topos-edit / topos-composite / toposctl / topos-vault）
pip install -e .

# 开发者（含测试依赖）：
pip install -e ".[dev]"
```

### 3. 启动方式

```bash
# 一键启动（自动检测 Python 架构 / GPU / 系统依赖）
./run_topos.command        # macOS
bash run_topos.sh          # Linux
run_topos.bat              # Windows

# 模块方式（选择产品形态）
python -m apps.topos_studio.main
python -m apps.topos_color.main
python -m apps.topos_edit.main
python -m apps.topos_composite.main
# 或统一入口 + 产品参数
python src/main.py --product studio

# 或安装后使用命令
topos-studio
topos-color
topos-edit
topos-composite
```

### 4. 架构迁移须知

> **重要**：虚拟环境 (`.venv`) 不可跨架构复制。从 Intel Mac 迁移到 Apple Silicon（或反向）时，**必须删除旧 `.venv` 并重新创建**。`.venv` 与 CPU 架构绑定，换机器/换芯片后同样需要重建。

```bash
# 迁移步骤
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate  # macOS
# 或 .venv\Scripts\activate  # Windows
pip install -r requirements.txt
```

### 5. 下一步

*   功能与产品形态总览：[features-overview.md](features-overview.md)
*   遇到安装/运行问题：[faq.md](faq.md)（macOS Apple Silicon / macOS 26 / Windows / Linux 平台问题表）

---

## English

### 1. Compatibility

**Topos supports Qt 6 only** (PyQt6 ≥ 6.10.2 and the versions listed in `requirements.txt`); Qt 5 is not supported. The embedded `file_explorer` frontend uses web features such as `replaceAll`, `:scope`, and `aspect-ratio`, which require Chromium 88 or later. The project's QtWebEngine 6.10 dependency bundles Chromium 122 or later; Qt 5's QtWebEngine (Chromium 87 or earlier) does not meet that requirement.

**Python versions** (P3-DOC-02): `pyproject.toml` requires Python ≥ 3.10; CI validates 3.10 and 3.12. Python 3.13+ is recommended on macOS 26, while the supported floor remains the CI matrix. Python 3.9 and earlier are unsupported.

### 2. Installation

```bash
# 1. Create a virtual environment (see section 4 below before switching CPU architectures)
python3 -m venv .venv
source .venv/bin/activate        # macOS / Linux
# On Windows: .venv\Scripts\activate

# 2. Install runtime dependencies
pip install -r requirements.txt

# 3. Install the package in editable mode (also provides the CLI commands:
#    topos-studio / topos-color / topos-edit / topos-composite / toposctl / topos-vault)
pip install -e .

# For development (includes test dependencies):
pip install -e ".[dev]"
```

### 3. Launching Topos

```bash
# Convenience launchers (detect Python architecture, GPU, and system dependencies)
./run_topos.command        # macOS
bash run_topos.sh          # Linux
run_topos.bat              # Windows

# Launch a product module directly
python -m apps.topos_studio.main
python -m apps.topos_color.main
python -m apps.topos_edit.main
python -m apps.topos_composite.main

# Or use the unified entry point with a product selector
python src/main.py --product studio

# Installed console commands
topos-studio
topos-color
topos-edit
topos-composite
toposctl
topos-vault
```

### 4. Moving Between CPU Architectures

> **Important:** A virtual environment (`.venv`) cannot be copied between CPU architectures. When moving between an Intel Mac and Apple Silicon, remove and recreate the environment. `.venv` is tied to the CPU architecture and must be recreated when changing machines or chips.

```bash
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate  # macOS/Linux
# On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 5. Next Steps

- Product and feature overview: [features-overview.md](features-overview.md)
- Troubleshooting installation/runtime issues: [faq.md](faq.md) (macOS Apple Silicon / macOS 26 / Windows / Linux issue tables)
