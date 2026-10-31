# Topos Color 模块化剩余任务执行计划表

版本：2026-05-26
状态：继续实施用
适用范围：在当前兼容层已恢复、测试入口已打通之后，继续完成真正的模块化收口。

本计划只写剩余任务，不重复已经完成的工作。

---

## 0. 当前基线

### 0.1 已经完成或基本完成

当前已经不需要从头再做这些事：

| 已完成项 | 当前结果 |
|---|---|
| Edit 旧路径兼容层恢复 | `src.core.edit.*` 旧 import 已能工作 |
| Composite 节点旧路径 wrapper | `src.composite.nodes.*` 大部分旧 import 已能工作 |
| Composite 未实现节点占位 | `TextNode`、`RotoShapeNode`、AI placeholder 节点已补齐 |
| Feature flag registry 单测 | `tests/test_feature_flags_registry.py` 已存在并通过 |
| 临时文件删除 | `src/features/composite/core/.!81660!shaders.py` 已标记删除 |
| `.gitignore` 临时文件规则 | 已能匹配 `.!81660!shaders.py` 这类编辑器临时文件 |
| 调色核心保护 | 当前未发现 `image_engine.py` / `shader_compiler.py` / `async_renderer.py` 被模块化破坏 |

### 0.2 当前通过的验收

最近一次验收结果：

```bash
python3 -m compileall src
```

通过。

```bash
.venv/bin/pytest tests/media -q
```

结果：

```text
1377 passed, 44 skipped
```

```bash
.venv/bin/pytest tests/test_nodes -q
```

结果：

```text
677 passed, 4 skipped
```

```bash
.venv/bin/pytest tests/test_config_manager.py tests/test_feature_flags_registry.py tests/test_image_engine_state.py tests/test_image_engine_layers.py -q
```

结果：

```text
112 passed
```

```bash
git diff --check
```

通过。

### 0.3 当前必须注意的 Git 状态

当前工作区不是干净状态，包含大量未提交迁移结果：

- `.gitignore` 修改。
- `docs/modularization_next_cleanup_plan_2026-05-25.md` 修改。
- `src/app/module_loader.py`、`src/main.py`、`src/ui/main_window.py` 修改。
- `src/features/composite/*` 新增/修改。
- `src/composite/*` 大量 compatibility wrapper 新增/修改。
- `src/core/edit/*` 大量 compatibility wrapper 新增/修改。
- `src/ui/edit/*` compatibility wrapper 新增。
- `tests/test_feature_flags_registry.py` 新增。

下一步开始前，建议先做一次人工 review，然后提交当前稳定基线。

建议提交信息：

```text
refactor: restore modularization compatibility baseline
```

不要把后续 Composite UI 真实迁移混进这个提交。

---

## 1. 当前剩余问题清单

| ID | 优先级 | 问题 | 当前证据 | 修复目标 |
|---|---|---|---|---|
| R-001 | 高 | Composite UI 还没有真实迁到 `src/features/composite/ui` | `src/features/composite/module.py` 仍 import `src.composite.ui.composite_page` | 新路径成为真实 UI 源，旧路径只做 wrapper |
| R-002 | 高 | `MainWindow` 仍有 Composite 深层 fallback import | `src/ui/main_window.py` 仍 import `src.composite.ui.*` | Composite 创建逻辑移入 `CompositeModule` 或 adapter |
| R-003 | 高 | `src/composite` 还不是纯兼容层 | `src/composite/ui/*` 仍是真实 UI，`ai_cache_manager.py` 等仍是真实代码 | 旧目录只保留 wrapper，不再写业务逻辑 |
| R-004 | 中 | Composite export 入口仍走旧路径 | `src/ui/main_window.py` 仍 import `src.composite.composite_export_renderer` | 改到 `src.features.composite.render.composite_export_renderer` |
| R-005 | 中 | AI inference 仍依赖旧 Composite cache manager | `src/core/ai_models/inference_engine.py` import `src.composite.ai_cache_manager` | 明确归属：迁到 features/composite 或 shared/ai |
| R-006 | 中 | `node_capabilities.py` 文档仍提示旧路径 | `src/features/composite/core/node_capabilities.py` docstring 使用 `src.composite` | 改成新路径文档 |
| R-007 | 中 | Edit 旧路径 wrapper 尚未做删除演练 | `src/core/edit/*` 大量 wrapper 还在 | 验证 Edit disabled 时旧路径是否可删除 |
| R-008 | 中 | Deliver/export 归属还没完全定 | `src/ui/deliver_page.py` wrapper 已有，但 export 代码仍在 core/ui | 判断 shared/export 与 features/deliver/core 的边界 |
| R-009 | 中 | 真实 GUI feature flag 验收仍缺 | 目前主要是单测和局部 smoke | 在有 GUI 环境下验证禁用 Edit/Composite/Deliver |
| R-010 | 低 | placeholder 节点不是真实现 | Text/AI/Roto 当前只是不崩占位 | 后续单独做真实节点功能，不混入模块化收口 |

---

## 2. 阶段 0：提交当前稳定基线

### 目标

把已经恢复通过的兼容层和测试基线固定下来，避免下一阶段迁移失败时难以回退。

### 任务表

| ID | 任务 | 操作 | 验收 |
|---|---|---|---|
| S0-001 | 查看改动范围 | `git diff --stat` | 确认只包含模块化/兼容层/测试/文档 |
| S0-002 | 检查临时文件 | `git ls-files | rg "\\.!.*!|\\.DS_Store|__pycache__|\\.pyc$"` | 无输出 |
| S0-003 | 跑基线测试 | 运行本节下方命令 | 全部通过 |
| S0-004 | 提交当前基线 | `git add ... && git commit` | 当前改动进入单独提交 |

### 验收命令

```bash
git diff --check
python3 -m compileall src
.venv/bin/pytest tests/test_config_manager.py tests/test_feature_flags_registry.py tests/test_image_engine_state.py tests/test_image_engine_layers.py -q
.venv/bin/pytest tests/media -q
.venv/bin/pytest tests/test_nodes -q
```

### 通过标准

- 所有命令通过。
- Git 提交只描述“兼容层恢复 + 测试入口恢复”。
- 不把下一阶段 Composite UI 迁移混进这个提交。

---

## 3. 阶段 1：Composite UI 真实迁移

### 目标

把真实 Composite UI 从：

```text
src/composite/ui/
```

迁到：

```text
src/features/composite/ui/
```

旧路径只保留 wrapper。

### 待迁移文件

第一批建议迁移这些 UI 文件：

```text
src/composite/ui/composite_page.py
src/composite/ui/composite_viewport.py
src/composite/ui/node_properties_panel.py
src/composite/ui/perf_panel.py
src/composite/ui/viewer_tool_system.py
src/composite/ui/viewer_handles.py
src/composite/ui/viewer_handle_drawing.py
src/composite/ui/node_editor_widget.py
src/composite/ui/node_graph_commands.py
src/composite/ui/node_editor_actions.py
```

第二批迁移剩余 UI 支撑：

```text
src/composite/ui/backdrop_item.py
src/composite/ui/connection_item.py
src/composite/ui/graph_layout.py
src/composite/ui/graph_node_search_popup.py
src/composite/ui/group_commands.py
src/composite/ui/keyframe_editor.py
src/composite/ui/minimap_widget.py
src/composite/ui/node_item.py
src/composite/ui/node_search_popup.py
src/composite/ui/node_thumbnail.py
src/composite/ui/port_item.py
src/composite/ui/sticky_note_item.py
```

### 实施步骤

| ID | 步骤 | 说明 | 验收 |
|---|---|---|---|
| CUI-001 | 新建 `src/features/composite/ui/` | 加 `__init__.py` | `python3 -m compileall src/features/composite/ui` |
| CUI-002 | 迁移第一批核心 UI | 优先迁 page/viewport/properties/perf | `rg "src\\.composite\\.ui" src/features/composite/ui` 尽量无结果 |
| CUI-003 | 更新内部相对 import | UI 内部使用相对 import 或 `src.features.composite.ui.*` | compileall |
| CUI-004 | 更新 `CompositeModule` | `module.py` 改 import `src.features.composite.ui.composite_page` | module 单测/feature flag 单测 |
| CUI-005 | 更新 `MainWindow` fallback | 优先新路径，旧路径只作为临时 fallback；完成后删除 fallback | `rg "src\\.composite\\.ui" src/ui/main_window.py` 下降 |
| CUI-006 | 旧 UI wrapper | `src/composite/ui/*.py` 改为 re-export 新路径 | 旧测试仍可 import |

### 注意事项

- 不要改 Color 核心渲染文件。
- 不要一次性重构 UI 逻辑，只做路径迁移。
- 如果迁移某个 UI 文件后出现循环 import，先保留该文件在旧路径，并记录为阻塞项，不要硬拆。
- 每一批迁移后必须跑测试，不要等全部搬完。

### 验收命令

```bash
python3 -m compileall src
.venv/bin/pytest tests/test_feature_flags_registry.py -q
.venv/bin/pytest tests/test_nodes -q
.venv/bin/pytest tests/test_composite_graph.py tests/test_composite_format.py -q
rg -n "from src\\.composite\\.ui|import src\\.composite\\.ui" src/features src/ui/main_window.py
```

### 通过标准

- `src/features/composite/ui` 是真实 UI 源。
- `src/composite/ui` 只做 wrapper。
- `CompositeModule` 不再直接 import 旧 Composite UI。
- 节点测试仍通过。

---

## 4. 阶段 2：Composite 旧目录收口

### 目标

让旧目录：

```text
src/composite/
```

变成纯兼容层。

### 当前仍需处理的旧目录类型

| 类型 | 示例 | 目标归属 |
|---|---|---|
| UI | `src/composite/ui/*` | `src/features/composite/ui/*` |
| core graph | `node_engine.py`、`graph_migration.py` | `src/features/composite/core/*` |
| render/evaluator | `graph_evaluator_mixin.py` | `src/features/composite/evaluators/*` |
| media/image input | `image_loader.py` | `src/features/composite/core/image_loader.py` |
| AI cache/compute | `ai_cache_manager.py`、`ai_compute_manager.py` | 先放 `src/features/composite/core/ai_*`，后续可考虑 shared |
| renderer glue | `composite_renderer.py`、`prefetch_scheduler.py` | `src/features/composite/render/*` |
| shader library | `shader_library.py` | `src/features/composite/core/` 或 `render/` |
| undo stack | `undo_stack.py` | `src/features/composite/core/undo_stack.py` |
| video context | `video_context.py` | 如果只给 Composite，用 `features/composite/core`；若 Edit 也用，放 shared |

### 实施步骤

| ID | 步骤 | 说明 | 验收 |
|---|---|---|---|
| CS-001 | 列出旧目录真实文件 | `find src/composite -maxdepth 2 -type f` | 明确哪些不是 wrapper |
| CS-002 | 分组迁移 core/render/evaluator | 每组一个提交 | compileall + 相关测试 |
| CS-003 | 替换新目录内部反向 import | `src/features/composite` 内不应 import `src.composite` | `rg "src\\.composite" src/features/composite` |
| CS-004 | 旧路径全部 wrapper 化 | 每个旧文件只 re-export 新路径 | 手查 + import smoke |
| CS-005 | 删除演练 | 临时 rename 旧目录做 disabled 验收 | 见下方删除演练 |

### 删除演练

只在所有 wrapper 化完成后做：

```bash
mv src/composite src/composite.disabled
TOPOS_FEATURE_COMPOSITE=0 python3 -m compileall src
.venv/bin/pytest tests/test_config_manager.py tests/test_feature_flags_registry.py -q
mv src/composite.disabled src/composite
```

注意：

- 删除演练只验证 disabled 模式。
- 不要求 Composite enabled 时旧目录不存在，因为旧 import 兼容可能仍依赖 wrapper。
- 如果 disabled 模式仍依赖旧目录，说明可选功能隔离还没完成。

---

## 5. 阶段 3：MainWindow / Registry 解耦

### 目标

`MainWindow` 不再知道 Composite/Edit/Deliver 的深层实现路径。

长期目标：

```text
MainWindow -> FeatureRegistry -> FeatureModule -> PageDescriptor/factory
```

而不是：

```text
MainWindow -> src.features.edit.ui.*
MainWindow -> src.composite.ui.*
MainWindow -> src.features.deliver.ui.*
```

### 当前仍残留的问题

```text
src/ui/main_window.py still imports src.composite.ui.*
src/ui/main_window.py still imports src.composite.format
src/ui/main_window.py still imports src.composite.composite_export_renderer
```

### 任务表

| ID | 任务 | 修复方式 | 验收 |
|---|---|---|---|
| MW-001 | Composite page 创建移出 MainWindow | `CompositeModule.register_pages()` 返回 page factory | 进入 Composite page 正常 |
| MW-002 | Composite docks 创建移出 MainWindow | 先通过 module 提供 `create_docks()` 或 adapter | Color/Edit 不知道 Composite docks |
| MW-003 | Composite export renderer 移出 MainWindow | `CompositeModule` 提供 export handler | 导出 Composite 正常 |
| MW-004 | Composite format import 改新路径 | `src.features.composite.core.format` | compileall |
| MW-005 | FeatureRegistry 生命周期补齐 | page enter/leave/save handler 统一走 registry | registry 单测 |
| MW-006 | MainWindow 顶层 Edit import 继续降低 | Edit-only 类型放到 lazy import 或 EditModule adapter | `TOPOS_FEATURE_EDIT=0` 不触发 Edit UI import |

### 验收命令

```bash
rg -n "src\\.composite|features\\.edit\\.ui|features\\.deliver\\.ui" src/ui/main_window.py
python3 -m compileall src
.venv/bin/pytest tests/test_feature_flags_registry.py -q
```

通过标准：

- `MainWindow` 对可选功能只通过 registry/module 交互。
- 禁用功能时不创建对应 page/dock/menu。
- Color 页面启动不依赖 Composite/Edit/Deliver 深层 UI。

---

## 6. 阶段 4：Edit 旧路径收口

### 当前状态

当前 Edit 真实源已经是：

```text
src/features/edit/core/
src/features/edit/ui/
```

旧路径主要是兼容层：

```text
src/core/edit/
src/ui/edit/
```

### 剩余目标

| ID | 任务 | 修复方式 | 验收 |
|---|---|---|---|
| E-001 | 确认旧路径全是 wrapper | 检查 `src/core/edit/*.py` 和 `src/ui/edit/*.py` | 无真实业务逻辑 |
| E-002 | 减少非 Edit 模块对 Edit 具体类依赖 | `offline_asset_manager`、`proxy_manager`、`SaveRouter` 等改 contract/adapters | `rg "src\\.features\\.edit|src\\.core\\.edit" src/core src/shared` |
| E-003 | Edit disabled 删除演练 | 临时 rename `src/features/edit` 或旧路径组合测试 | disabled 模式仍可启动 Color |
| E-004 | Edit UI wrapper 删除计划 | 等测试全改新路径后删除旧 wrapper | visual/edit tests 迁移完成 |

### 验收命令

```bash
python3 -m compileall src
.venv/bin/pytest tests/edit/test_color_stack.py tests/edit/test_edit_project.py -q
.venv/bin/pytest tests/media/test_video_frame_provider_service.py -q
TOPOS_FEATURE_EDIT=0 .venv/bin/pytest tests/test_feature_flags_registry.py -q
```

通过标准：

- 旧路径 import 仍可用。
- 新业务只写到 `src/features/edit`。
- Edit disabled 不影响 Color/Composite/Deliver 的基础注册。

---

## 7. 阶段 5：Deliver / Export 归属收口

### 当前状态

Deliver 页面已经有：

```text
src/features/deliver/ui/deliver_page.py
src/ui/deliver_page.py  # compatibility wrapper
```

但 export 相关代码还分布在：

```text
src/core/export/
src/ui/export_panel.py
src/ui/export_dialog.py
src/ui/quick_export_dialog.py
```

### 决策规则

| 文件类型 | 归属判断 |
---|---|
| 只服务 Deliver 页面 | 放入 `src/features/deliver/` |
| Color/Edit/Composite 都可能导出 | 放入 `src/shared/export/` |
| UI 快捷导出入口 | 如果 Color 也使用，暂留 `src/ui`，并写清楚原因 |

### 任务表

| ID | 任务 | 修复方式 | 验收 |
|---|---|---|---|
| D-001 | 梳理 export 引用 | `rg "core\\.export|ExportPanel|ExportDialog|QuickExport" src tests` | 列清归属 |
| D-002 | Deliver disabled 验收 | 禁用 Deliver 不应创建 Deliver page/dock/menu | GUI smoke |
| D-003 | Export shared 化 | 如果多功能共用，迁到 `src/shared/export` | 相关导出测试通过 |
| D-004 | Deliver module save/export handler | DeliverModule 管理自己的 actions | MainWindow 更少知道 Deliver |

---

## 8. 阶段 6：Color 保护与低风险整理

### 核心原则

不要为了目录整洁移动这些文件：

```text
src/core/image_engine.py
src/core/shader_compiler.py
src/core/async_renderer.py
src/core/gl_backend.py
src/core/layer.py
```

这些是当前调色主线，除非有单独的高风险迁移计划，否则保持不动。

### 可做的低风险事项

| ID | 任务 | 修复方式 | 验收 |
|---|---|---|---|
| CO-001 | ColorFacade 边界文档化 | 写清其它模块只能通过 facade 拿 color output | 文档 + 单测 |
| CO-002 | Color UI 已迁移文件检查 | 确认 `src/features/color/ui` 是真实 UI 源 | Color 页面手测 |
| CO-003 | 调色测试固定化 | 每阶段固定跑 image engine/layer 测试 | 见验收命令 |
| CO-004 | 禁止无关渲染改动 | review 时检查核心文件是否被误改 | `git diff -- src/core/image_engine.py ...` |

### 每次必须跑

```bash
.venv/bin/pytest tests/test_image_engine_state.py tests/test_image_engine_layers.py -q
```

人工手测：

- 打开图片。
- Lift/Gamma/Gain。
- 曲线。
- HSL 曲线。
- LUT。
- Mask。
- Qualifier。
- Soft Clip。
- 保存后重开。

---

## 9. 每阶段固定验收命令

每做完一个阶段，至少运行：

```bash
git diff --check
python3 -m compileall src
.venv/bin/pytest tests/test_config_manager.py tests/test_feature_flags_registry.py -q
.venv/bin/pytest tests/test_image_engine_state.py tests/test_image_engine_layers.py -q
```

涉及 Composite：

```bash
.venv/bin/pytest tests/test_nodes -q
.venv/bin/pytest tests/test_composite_nodes_ai.py tests/test_composite_nodes_input.py tests/test_composite_nodes_mask.py -q
.venv/bin/pytest tests/test_composite_graph.py tests/test_composite_format.py -q
```

涉及 Edit：

```bash
.venv/bin/pytest tests/media/test_video_frame_provider_service.py -q
.venv/bin/pytest tests/edit/test_color_stack.py tests/edit/test_edit_project.py -q
```

涉及媒体/播放：

```bash
.venv/bin/pytest tests/media -q
```

真实 GUI 环境下：

```bash
TOPOS_FEATURE_EDIT=0 python3 src/main.py
TOPOS_FEATURE_COMPOSITE=0 python3 src/main.py
TOPOS_FEATURE_DELIVER=0 python3 src/main.py
TOPOS_FEATURE_EDIT=0 TOPOS_FEATURE_COMPOSITE=0 TOPOS_FEATURE_DELIVER=0 python3 src/main.py
```

---

## 10. 提交策略

不要把剩余任务塞进一个大提交。

推荐拆分：

| 提交 | 内容 |
---|---|
| 1 | 当前 compatibility baseline + tests |
| 2 | Composite UI 第一批迁移 |
| 3 | Composite UI 第二批迁移 |
| 4 | Composite old path wrapper 化 |
| 5 | MainWindow / Registry 解耦 |
| 6 | Edit wrapper cleanup / delete drill |
| 7 | Deliver/export 归属收口 |
| 8 | docs + final acceptance |

每个提交都必须能独立通过基础验收。

---

## 11. 给下一个 AI 的执行 Prompt

```text
请按 docs/modularization_remaining_tasks_plan_2026-05-26.md 继续执行。

当前状态：
- 兼容层和测试入口已经恢复。
- tests/media 通过：1377 passed, 44 skipped。
- tests/test_nodes 通过：677 passed, 4 skipped。
- feature flag registry 测试通过。
- 调色核心暂时稳定，不要移动 image_engine.py / shader_compiler.py / async_renderer.py。

下一步只做阶段 0 和阶段 1：
1. 先确认当前 compatibility baseline 是否应该单独提交。
2. 然后迁移 Composite UI 第一批文件到 src/features/composite/ui。
3. 旧 src/composite/ui 文件只做 re-export wrapper，不复制业务逻辑。
4. 更新 src/features/composite/module.py，让它优先 import 新 UI 路径。
5. 不要修改 Color 核心渲染文件。

每一步必须验证：
git diff --check
python3 -m compileall src
.venv/bin/pytest tests/test_feature_flags_registry.py -q
.venv/bin/pytest tests/test_nodes -q
.venv/bin/pytest tests/test_image_engine_state.py tests/test_image_engine_layers.py -q

输出：
- 修改了哪些文件。
- 哪些旧路径仍然保留为 wrapper。
- 哪些 src.composite.ui import 尚未清除以及原因。
- 所有验证命令和结果。
```

---

## 12. 最终完成标准

只有同时满足下面条件，才能宣布模块化完成：

- `src/features/composite`、`src/features/edit`、`src/features/deliver` 是真实功能源。
- `src/composite`、`src/core/edit`、`src/ui/edit`、`src/ui/deliver_page.py` 只做兼容 wrapper。
- `MainWindow` 不直接 import 可选功能深层实现。
- `FeatureRegistry` / `FeatureModule` 是可选功能入口。
- `TOPOS_FEATURE_EDIT=0`、`TOPOS_FEATURE_COMPOSITE=0`、`TOPOS_FEATURE_DELIVER=0` 都能启动。
- Composite disabled 时，不依赖 `src/composite` 真实实现。
- Edit disabled 时，不依赖 `src/features/edit` 真实实现。
- Color 核心测试和人工调色流程通过。
- 所有计划文档状态与实际代码状态一致。
