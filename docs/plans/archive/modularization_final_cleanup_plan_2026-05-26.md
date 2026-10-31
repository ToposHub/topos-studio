# Topos Color 模块化最终清理计划（2026-05-26）

> 目标：在不扰动核心调色管线的前提下，把当前"新目录可运行 + 旧目录兼容 wrapper"的状态，推进到"功能目录边界清晰、禁用功能不影响其它功能、最终可删除某个功能目录"的状态。

> **状态更新（2026-05-26）**：阶段 A~F 已完成 ✅，阶段 G/H 目录骨架已就位 ✅。
> - A ✅ commit 7775718 | B ✅ commit 9fb04ca | C ✅ 100% wrapper | D ✅ commit 24dc21d
> - E ✅ CompositeRuntime + MainWindowAdapter 创建完成，MainWindow 直接 import 从 6→0
> - F ✅ 删除演练通过（TOPOS_FEATURE_COMPOSITE=0 + mv 目录后 compile + 112 tests pass）
> - G/H ✅ adapters 目录已创建，Edit/Deliver 耦合已分析，当前适合小步迭代

---

## 0. 当前基线结论

本计划基于 2026-05-26 当前工作区状态编写。

### 已经完成

- `src/features/composite/ui` 已存在，并且 `CompositePage` 已能从新路径导入。
- `src/features/composite/module.py` 已使用 `src.features.composite.ui.composite_page.CompositePage`。
- Composite 页面打不开的主要阻断已定位并修复：迁移后部分 import 仍指向错误目录，例如 `render/graph_evaluator_mixin.py`、`render/image_loader.py`、`core/keyframe.py`、`core/expression_parser.py`。
- 已补齐一批 `src/composite/*` 兼容 wrapper，让旧测试和旧调用点继续可用。
- 已验证 Composite 页面可以离屏实例化：
  - `CompositeModule().is_available() == True`
  - `CompositePage` 来自 `src.features.composite.ui.composite_page`
  - `NodeGraph` 和 `NodeEditorWidget` 可初始化
- 已通过的关键验证：
  - `.venv/bin/python -m compileall -q src`
  - `git diff --check`
  - `tests/test_feature_flags_registry.py`
  - `tests/test_composite_nodes_input.py`
  - `tests/test_composite_nodes_ai.py`
  - `tests/test_nodes`
  - `tests/test_keyframe.py`
  - `tests/test_expression_parser.py`
  - `tests/test_render_scheduler.py`
  - `tests/test_parallel_evaluation.py`
  - `tests/test_tiling.py`
  - `tests/test_composite_tile.py`
  - `tests/test_node_capabilities.py`
  - `tests/test_composite_snapshot.py`
  - `tests/test_composite_node_contract.py`
  - `tests/test_composite_preset.py`

### 仍然存在

- `src/composite` 仍然存在，而且现在承担兼容 wrapper 职责；它还不能直接删除。
- `src/ui/main_window.py` 仍有 Composite 直连逻辑和旧路径 fallback，说明 MainWindow 还没有完全变成纯应用壳。
- `src/composite/ui` 旧 UI 目录仍在，未来需要压缩成 wrapper 或完全删除。
- 当前仍有一个单测口径问题需要单独处理：
  - `tests/test_gpu_resource_pool_v2.py::TestVRAMMonitor::test_get_vram_budget_mb`
  - 当前代码按二进制 MB 计算 `1024 * 1024 KB * 0.7 = 716.8 MB`
  - 测试期望 `700.0 MB`
  - 同文件另一个 2GB 用例期望 `1433.6 MB`，这又符合二进制 MB，因此这里需要先统一 VRAM 预算单位口径。

---

## 1. 必须保护的边界

### 绝对不要顺手改

- `src/core/image_engine.py`
- `src/core/shader_compiler.py`
- `src/core/async_renderer.py`
- `src/core/gl_backend.py`
- 调色插件 `.tglsl` 执行链路
- Color 页面核心参数、GPU texture 输出、LUT/曲线/HSL 等调色行为

### 本轮允许改动范围

- `src/features/composite/**`
- `src/composite/**` wrapper 层
- `src/app/**` FeatureRegistry / ModuleLoader 相关薄层
- `src/ui/main_window.py` 中 Composite/Edit/Deliver 的接线逻辑，但必须小步提交
- `tests/**` 中与迁移路径、wrapper 兼容、feature flag、Composite runtime 相关的测试
- `docs/**`

---

## 2. 阶段 A：先提交当前修复基线

### 目的

把“Composite 页面恢复可用”的修复先固化，避免后续大清理时混在一起。

### 操作

1. 查看工作区：

```bash
git status --short
```

2. 跑最小页面可用验证：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python - <<'PY'
import sys
from PyQt6.QtWidgets import QApplication
from src.features.composite.module import CompositeModule
from src.features.composite.ui.composite_page import CompositePage

app = QApplication.instance() or QApplication(sys.argv)
mod = CompositeModule()
page = CompositePage()

assert mod.is_available() is True
assert CompositePage.__module__ == "src.features.composite.ui.composite_page"
assert type(page.node_graph).__name__ == "NodeGraph"
assert type(page.node_editor).__name__ == "NodeEditorWidget"

page.cleanup()
page.deleteLater()
print("Composite page baseline OK")
PY
```

3. 跑回归：

```bash
.venv/bin/python -m compileall -q src
git diff --check
.venv/bin/python -m pytest tests/test_feature_flags_registry.py tests/test_composite_nodes_input.py tests/test_composite_nodes_ai.py tests/test_nodes -q
.venv/bin/python -m pytest tests/test_keyframe.py tests/test_expression_parser.py tests/test_render_scheduler.py tests/test_parallel_evaluation.py tests/test_tiling.py tests/test_composite_tile.py tests/test_node_capabilities.py tests/test_composite_snapshot.py tests/test_composite_node_contract.py tests/test_composite_preset.py -q
```

4. 如果通过，提交：

```bash
git add src/features/composite src/composite docs/modularization_final_cleanup_plan_2026-05-26.md
git commit -m "fix: restore composite feature after modular migration"
```

### 验收

- Composite 页面能打开。
- 新路径 `src.features.composite.ui.composite_page.CompositePage` 可导入。
- 旧路径 `src.composite.*` 兼容测试不再因为缺模块失败。
- 调色相关 smoke tests 不失败。

---

## 3. 阶段 B：统一 VRAM 预算单位口径

### 当前问题

`VRAMMonitor.get_vram_budget_mb()` 的测试中，1GB 和 2GB 的期望值口径不一致：

- `1024 * 1024 KB` 按二进制 MB 应为 `1024 MB`，70% 是 `716.8 MB`
- 测试却期望 `700.0 MB`
- `2048 * 1024 KB` 的 70% 测试期望 `1433.6 MB`，这是二进制 MB 口径

### 推荐修复

优先统一为二进制 MB，因为 GPU/VRAM 通常按 KiB/MiB 口径报告。

修改测试：

```python
assert budget == 716.8  # 70% of 1024 MiB
```

如果产品侧希望 UI 显示十进制 GB/MB，则不要改底层 `get_vram_budget_mb()`，而是在 UI 层显示时格式化。

### 验证

```bash
.venv/bin/python -m pytest tests/test_gpu_resource_pool.py tests/test_gpu_resource_pool_v2.py -q
```

### 验收

- GPU resource pool v1/v2 测试全部通过。
- 没有为了满足单个测试而在代码里加入 `1GB` 特判。

---

## 4. 阶段 C：把 `src/composite` 收敛成纯 wrapper 包

### 当前状态

`src/composite` 还不能删除，因为旧测试和旧代码仍引用：

- `src.composite.render_scheduler`
- `src.composite.graph_snapshot`
- `src.composite.gpu_resource_pool`
- `src.composite.node_capabilities`
- `src.composite.cache_manager`
- `src.composite.pass_fusion`
- `src.composite.keyframe`
- `src.composite.color_space`
- `src.composite.tile_scheduler`
- `src.composite.tile_padding_computer`
- `src.composite.preset_manager`
- `src.composite.expression_parser`
- `src.composite.composite_export_renderer`
- `src.composite.nodes.*`
- `src.composite.ui.*`

### 目标

短期目标不是马上删除 `src/composite`，而是先保证其中所有 `.py` 文件都只是 wrapper。

### 操作

1. 列出非 wrapper 文件：

```bash
find src/composite -type f -name "*.py" -maxdepth 3 | sort
```

2. 对每个文件检查是否只包含：

```python
"""Compatibility wrapper ..."""
from src.features.composite... import *
```

3. 重点处理旧 UI：

```bash
find src/composite/ui -type f -name "*.py" | sort
```

每个 `src/composite/ui/*.py` 都应改为转发到：

```text
src/features/composite/ui/*.py
```

4. 重点处理旧 nodes：

```bash
find src/composite/nodes -type f -name "*.py" | sort
```

每个 `src/composite/nodes/*.py` 都应改为转发到：

```text
src/features/composite/nodes/*.py
```

### 验证

```bash
.venv/bin/python -m compileall -q src/composite src/features/composite
.venv/bin/python -m pytest tests/test_nodes tests/test_composite_nodes_input.py tests/test_composite_nodes_ai.py tests/test_composite_snapshot.py tests/test_composite_node_contract.py -q
```

### 验收

- `src/composite` 中没有真实业务实现，只剩 wrapper。
- 旧 import 仍可运行。
- 新代码不再新增 `src.composite.*` import。

---

## 5. 阶段 D：建立 Composite import 防回流规则

### 目的

避免后续 AI 或人工继续把新代码写回旧路径。

### 操作

新增一个测试，例如：

```text
tests/test_modular_import_boundaries.py
```

测试规则：

- `src/features/composite/**` 不允许 import `src.composite.*`
- `src/features/composite/**` 不允许 import `composite.*`
- `src/app/**` 可以 import `src.features.composite.module`
- `src/ui/main_window.py` 暂时允许少量旧 fallback，但必须列入 allowlist

### 示例逻辑

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_features_composite_does_not_import_legacy_composite():
    offenders = []
    for path in (ROOT / "src/features/composite").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "src.composite" in text or "from composite" in text or "import composite" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
```

### 验证

```bash
.venv/bin/python -m pytest tests/test_modular_import_boundaries.py -q
```

### 验收

- 新 feature 目录不会再回流到旧 `src/composite`。
- 未来删除 wrapper 前可以靠测试快速定位剩余旧依赖。

---

## 6. 阶段 E：MainWindow Composite 解耦

### 当前问题

`src/ui/main_window.py` 仍直接创建和管理 Composite 页面的大量细节：

- `CompositePage`
- `CompositeViewer`
- `NodePropertiesPanel`
- `PerfPanel`
- Composite dock
- Composite 页面切换时的 viewport swap
- Composite 保存/导出回调

这意味着 Composite 并没有完全成为独立 feature。

### 推荐目标

MainWindow 只保留：

- 页面切换入口
- 通用 dock 容器
- 通用事件转发
- `FeatureRegistry` / `PageDescriptor` 消费逻辑

Composite 自己负责：

- 创建自己的页面、viewer、properties、perf panel
- 连接 Composite 内部 signals
- 暴露少量 MainWindow 需要的能力：
  - `get_page()`
  - `get_viewer()`
  - `get_graph_data()`
  - `get_viewer_state()`
  - `load_composite_data(data)`
  - `on_color_output(texture_id, width, height, revision)`
  - `render_export(...)`

### 实施步骤

1. 在 `src/features/composite/module.py` 增加显式接口：

```python
def create_runtime(self, context: AppContext) -> CompositeRuntime:
    ...
```

2. 新增文件：

```text
src/features/composite/adapters/main_window_adapter.py
```

职责：

- 接 MainWindow 的 color output signal
- 接 SaveRouter callbacks
- 接 export callback
- 不实现 UI 业务

3. 从 `MainWindow.create_panels()` 中移除直接 import Composite UI 类的逻辑，改成通过 module/runtime 获取。

4. 保留兼容 fallback 一轮提交，不要一次删光。

### 验证

```bash
.venv/bin/python -m compileall -q src
.venv/bin/python -m pytest tests/test_feature_flags_registry.py tests/test_page_context.py tests/test_config_manager.py -q
QT_QPA_PLATFORM=offscreen .venv/bin/python - <<'PY'
import sys
from PyQt6.QtWidgets import QApplication
from src.features.composite.module import CompositeModule
from src.features.composite.ui.composite_page import CompositePage
app = QApplication.instance() or QApplication(sys.argv)
assert CompositeModule().is_available()
page = CompositePage()
page.cleanup()
print("Composite runtime smoke OK")
PY
```

### 验收

- MainWindow 中 Composite 直接 import 明显减少。
- 禁用 composite feature flag 时，Edit/Color/Deliver 不受影响。
- Composite 页面仍可打开。

---

## 7. 阶段 F：Composite delete drill

### 目的

验证“禁用 Composite 后，删除 Composite feature 不影响 Color/Edit/Deliver”。

### 前置条件

必须先完成：

- `src/composite` wrapper-only
- MainWindow 不再强依赖 Composite UI
- Feature flag 可控

### 操作

1. 临时禁用 Composite：

```bash
TOPOS_FEATURE_COMPOSITE=0 .venv/bin/python -m pytest tests/test_feature_flags_registry.py tests/test_config_manager.py tests/test_image_engine_state.py tests/test_image_engine_layers.py -q
```

2. 临时改名目录做删除演练：

```bash
mv src/features/composite /tmp/topos_composite_disabled_check
mv src/composite /tmp/topos_composite_legacy_disabled_check
TOPOS_FEATURE_COMPOSITE=0 .venv/bin/python -m compileall -q src
TOPOS_FEATURE_COMPOSITE=0 .venv/bin/python -m pytest tests/test_config_manager.py tests/test_image_engine_state.py tests/test_image_engine_layers.py -q
mv /tmp/topos_composite_disabled_check src/features/composite
mv /tmp/topos_composite_legacy_disabled_check src/composite
```

### 注意

不要提交 `mv` 删除演练结果。这个阶段只是证明边界成立。

### 验收

- Composite disabled 时，应用启动/Color 测试不 import Composite。
- 删除 Composite 目录演练后，非 Composite 测试仍通过。

---

## 8. 阶段 G：Edit 和 Deliver 的同类清理

### Edit 当前目标

最终结构：

```text
src/features/edit/
  core/
  ui/
  adapters/
```

### Deliver 当前目标

最终结构：

```text
src/features/deliver/
  core/
  ui/
  adapters/
```

### 操作

1. 搜索旧路径：

```bash
rg -n "src\\.core\\.edit|src\\.ui\\.edit|src\\.deliver|src\\.ui\\.deliver|src\\.features\\.deliver|src\\.features\\.edit" src tests -g "*.py"
```

2. 把真实实现集中到 `src/features/edit/**` 和 `src/features/deliver/**`。

3. 旧路径先做 wrapper，等测试迁移完成后再删除。

### 验证

```bash
.venv/bin/python -m pytest tests/edit -q
.venv/bin/python -m pytest tests/test_feature_flags_registry.py tests/test_page_context.py -q
```

### 验收

- Edit 禁用时不影响 Color/Composite/Deliver。
- Deliver 禁用时不影响 Color/Edit/Composite。
- 旧路径不会新增真实实现。

---

## 9. 阶段 H：最终目录目标

最终建议结构：

```text
src/
  app/
    module_loader.py
    page_contract.py
    app_context.py
  shared/
    feature_flags.py
    i18n/
    config/
    media/
    gpu/
    project_schema/
  features/
    edit/
      core/
      ui/
      adapters/
    color/
      core/
      ui/
      shaders/
      adapters/
    composite/
      core/
      ui/
      nodes/
      render/
      evaluators/
      utils/
      adapters/
    deliver/
      core/
      ui/
      adapters/
  ui/
    main_window.py
    widgets/
```

### 关键判断标准

- `src/features/<feature>` 里可以有真实业务实现。
- `src/app` 只做应用壳，不承载 feature 业务。
- `src/shared` 只放跨功能基础能力。
- 旧目录如果还在，只能是 wrapper。
- 删除某个 feature 目录时，只要该 feature flag 关闭，其它功能可启动、可测试。

---

## 10. 每阶段提交建议

建议按下面粒度提交，不要一次做完：

1. `fix: restore composite feature after modular migration`
2. `test: lock composite import boundaries`
3. `refactor: make legacy composite package wrapper-only`
4. `refactor: move composite runtime wiring behind feature module`
5. `test: add composite disabled delete drill coverage`
6. `refactor: clean edit legacy wrappers`
7. `refactor: clean deliver legacy wrappers`

每个提交前至少跑：

```bash
.venv/bin/python -m compileall -q src
git diff --check
.venv/bin/python -m pytest tests/test_feature_flags_registry.py -q
```

涉及 Composite 时额外跑：

```bash
.venv/bin/python -m pytest tests/test_nodes tests/test_render_scheduler.py tests/test_tiling.py tests/test_composite_snapshot.py tests/test_composite_node_contract.py -q
```

涉及 Color 或 MainWindow 时额外跑：

```bash
.venv/bin/python -m pytest tests/test_image_engine_state.py tests/test_image_engine_layers.py tests/test_config_manager.py -q
```

---

## 11. 给后续 AI 的执行提示词

```text
你在 Topos Color 项目中继续模块化收尾。

技术栈：
- PyQt6 + ModernGL + GLSL
- Color 调色管线是核心，禁止顺手修改 src/core/image_engine.py、shader_compiler.py、async_renderer.py、gl_backend.py。

当前目标：
按照 docs/modularization_final_cleanup_plan_2026-05-26.md 执行。
先完成阶段 A/B/C，不要一次做完所有阶段。

硬性约束：
1. 每次只做一个阶段。
2. 任何旧路径删除前，先改成 wrapper 并跑测试。
3. src/features/composite 不允许再 import src.composite。
4. MainWindow 解耦要小步，不要重写 MainWindow。
5. 每阶段完成后运行计划中的验证命令。

输出要求：
- 列出修改文件。
- 列出验证命令和结果。
- 如果失败，停止并说明第一处失败原因。
```

---

## 12. 最终完成定义

全部完成时应满足：

- Composite 页面能正常打开。
- Color 调色管线回归测试通过。
- `src/composite` 不再包含真实实现，或者已在确认无依赖后删除。
- `src/features/composite` 内无 `src.composite` 旧路径 import。
- MainWindow 不直接创建 Composite 的内部 UI 组件。
- 关闭 Composite feature flag 后，Color/Edit/Deliver 仍能启动和测试。
- 删除 Composite feature 目录演练通过。
- Edit/Deliver 至少完成同样的 wrapper-only 迁移策略。
