# 构建与打包 / Building and Packaging

> getstarted · 计划 04 P3（2026-09-28）。本文内容整理自 `README.md` / `README_EN.md`（迁移瘦身），未新增事实。
> 环境安装见 [installation.md](installation.md)；扩展开发（特性模块/插件/合成节点）见 [development.md](development.md)。
> 本文为双语文档：简体中文在前，English follows below。

## 中文

### 1. 原生编解码器构建与测试门禁

```bash
# 构建并运行原生测试门禁（unit/conformance/interop/abi/fuzz）
bash native/topos_codec/run_tests.sh
# 或直接 CMake 构建；库缺失时 Python 侧会报错并给出上述指引
# 可用环境变量 TOPOS_CODEC_LIB 显式指定动态库路径
```

Python 侧经 ctypes 绑定（`src/shared/codec/`），ABI 变更须同步 `include/` 头文件并更新 ABI 校验。

### 2. Python 测试与开发环境

```bash
# 安装依赖
pip install -e ".[dev]"

# 运行测试（1400+ 测试文件 / 2.2 万+ 用例）
.venv/bin/python -m pytest tests/ -v

# 原生编解码器测试门禁
bash native/topos_codec/run_tests.sh

# 运行特定产品
python -m apps.topos_studio.main
python -m apps.topos_color.main
```

近期套件战绩：`tests/edit` 8216/0、`composite` 5571/0 全绿；W01–W08 AI 工作流回放矩阵 8/8 PASS；性能回归门禁见 `tools/perf_regression_gate.py`。

### 3. 打包（PyInstaller）

*   打包配置位于 `packaging/pyinstaller/`，含四个产品 spec。
*   新增产品变体时需添加对应的打包配置：`packaging/pyinstaller/my_product_spec.py`（完整步骤见 [development.md](development.md) 的"添加新产品变体"）。

### 4. 基准与性能验证工具

*   `tools/` 为工程化脚本库（基准/探针/验证，不随应用分发）：`bench_*`（编解码/播放/导出/合成性能基准）、`probe_*` / `dbg_*`（Metal/VT/D3D11 能力探针与调试）、`verify_*`（i18n / 依赖层级 / 矩阵验证）、`e2e_*`（播放/scrub/seek 端到端基准）、`convert_*`（ONNX 模型转换等）。
*   性能回归门禁：`tools/perf_regression_gate.py`。
*   Windows 硬件解码/编码验证脚本用法见 [faq.md](faq.md) Windows 一节。

---

## English

### 1. Building and Testing the Native Codec

```bash
# Build and run the native unit/conformance/interop/ABI/fuzz gates
bash native/topos_codec/run_tests.sh
# Or build directly with CMake. If the library is missing, Python reports setup guidance.
# Set TOPOS_CODEC_LIB to select a specific shared library path.
```

Python bindings live under `src/shared/codec/` and use `ctypes`. ABI changes must update the headers under `include/` and the ABI checks.

### 2. Python Tests and Development Environment

```bash
# Install the package and development dependencies
pip install -e ".[dev]"

# Run the Python test suite (1,465+ test files / 22,000+ test functions and cases)
.venv/bin/python -m pytest tests/ -v

# Run the native codec gates
bash native/topos_codec/run_tests.sh

# Launch a product directly
python -m apps.topos_studio.main
python -m apps.topos_color.main
```

Recent recorded suite results: `tests/edit` 8,216 passed and `composite` 5,571 passed; the W01–W08 AI workflow replay matrix reports 8/8 passing. See `tools/perf_regression_gate.py` for the performance regression gate.

### 3. Packaging (PyInstaller)

- Packaging configuration lives under `packaging/pyinstaller/`, with one spec per product.
- When adding a new product edition, add a matching spec such as `packaging/pyinstaller/my_product_spec.py` (full steps under “Add a Product Edition” in [development.md](development.md)).

### 4. Benchmark and Verification Tools

- `tools/` holds engineering scripts (benchmarks, probes, verification; not shipped with the app): `bench_*` (codec/playback/export/composite benchmarks), `probe_*` / `dbg_*` (Metal/VT/D3D11 capability probes and debugging), `verify_*` (i18n / dependency layers / matrix verification), `e2e_*` (playback/scrub/seek end-to-end benchmarks), and `convert_*` (ONNX model conversion and more).
- Performance regression gate: `tools/perf_regression_gate.py`.
- Windows hardware decode/encode verification scripts: see the Windows section of [faq.md](faq.md).
