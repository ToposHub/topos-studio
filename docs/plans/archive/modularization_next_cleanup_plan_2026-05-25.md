# Topos Color 模块化剩余优化计划表

版本：2026-05-26
状态：待修复 / 待优化
基于验收：2026-05-26 人工检查、`compileall`、局部 pytest、feature flag smoke 尝试
目标：删除已经完成的任务记录，只保留当前仍需要处理的优化项、问题项、修复步骤和验收方式。

核心原则：

- 调色核心优先稳定，不为了目录漂亮移动 `image_engine.py`、`shader_compiler.py`、`async_renderer.py`。
- 先修断裂的导入和测试，再继续搬目录。
- 每次迁移必须形成闭环：新路径成为唯一真实源，旧路径只做兼容层，测试和 feature flag 验收通过。
- 不再做大批量复制 + 半删除。所有迁移都小批次提交，小批次验证。

---

## 0. 当前验收结论

当前不能宣布模块化完成。

比较准确的状态是：

```text
features/ 外壳已经建立；
Edit / Deliver / Color / Composite 都有新目录；
但 Composite 和 Edit 仍有旧路径残留、兼容层断裂、测试未同步；
调色核心暂时稳定，应继续保持低风险整理策略。
```

### 已验证通过的部分

这些内容不再列为后续任务，只作为当前基线：

```bash
git status --short
```

当前无输出，工作区干净。

```bash
python3 -m compileall src
```

通过，但有 `src/ui/canvas.py` 的两个 `SyntaxWarning`。

```bash
.venv/bin/pytest tests/test_config_manager.py -q
```

结果：`4 passed`。

```bash
.venv/bin/pytest tests/test_image_engine_state.py tests/test_image_engine_layers.py -q
```

结果：`68 passed`。这说明当前调色基础状态和 layer 状态测试没有被模块化改造破坏。

---

## 1. 当前发现的问题清单

| ID | 严重度 | 问题 | 证据 | 风险 |
|---|---|---|---|---|
| P-001 | 高 | `src/features/composite` 还没有真正独立，仍依赖旧 `src/composite` | `src/features/composite/module.py` 仍 import `src.composite.ui.composite_page` | 不能删除旧 `src/composite`，也不能算 Composite 模块化完成 |
| P-002 | 高 | `src/composite` 仍包含大量真实实现 | `src/composite/node_engine.py`、`src/composite/ui/*`、`src/composite/graph_evaluator_mixin.py` 等仍是业务代码 | 目录仍显混乱，新旧两套职责不清 |
| P-003 | 高 | Edit 旧路径兼容层断裂 | `tests/media/test_video_frame_provider_service.py` 导入 `src.core.edit.edit_frame_coordinator` 时触发 `src/core/edit/__init__.py`，但 `.project` 已不存在 | `tests/media` 失败 4 个，旧 API 使用者会崩 |
| P-004 | 高 | Composite 节点测试还使用旧路径 | `tests/node_test_base.py` 导入 `src.composite.nodes.base_node`，当前路径不存在 | 节点测试无法启动，不能证明 Composite 迁移正确 |
| P-005 | 中 | `MainWindow` 仍直接 import 多个 feature 深层文件 | `src/ui/main_window.py` 顶层 import `features.edit.*`，内部 import `src.composite.*` | Registry 还不是唯一入口，禁用/删除功能时风险高 |
| P-006 | 中 | `module_loader` 仍指向旧 Composite module | `src/app/module_loader.py` import `src.composite.module` | 两套模块入口并存，后续 AI 容易继续改错位置 |
| P-007 | 中 | `src/main.py` Composite 插件发现仍走旧路径 | `src/main.py` import `src.composite.node_engine` | Composite disabled/delete 验收不完整 |
| P-008 | 中 | 历史计划文档之间可能仍有状态冲突 | 旧计划和当前计划可能同时存在，且部分旧内容表达过于乐观 | 后续执行者容易误判状态 |
| P-009 | 中 | 有疑似编辑器临时文件被跟踪 | `src/features/composite/core/.!81660!shaders.py` | 可能污染正式代码和测试结果 |
| P-010 | 中 | offscreen 全禁用 smoke 不可靠 | 当前环境报 `QOpenGLWidget is not supported on this platform`，未输出 `SMOKE_OK` | 缺少可靠的无 GUI 模块化验收脚本 |
| P-011 | 低 | `src/ui/canvas.py` 有两个无效转义警告 | `compileall` 输出 `SyntaxWarning: invalid escape sequence '\)'` | 不阻塞模块化，但应顺手清理 |

---

## 2. 剩余优化总路线

| 阶段 | 目标 | 说明 |
|---|---|---|
| 1 | 修复导入断裂和测试入口 | 先让现有测试能跑，避免继续在坏基线上迁移 |
| 2 | 清理临时文件和文档状态 | 去掉误导信息，保证后续 AI 不被错误状态带偏 |
| 3 | 建立可靠 feature flag 验收 | 不能只靠真实 GUI；需要无 GUI 单测 + GUI smoke 双层验证 |
| 4 | 让 Registry 真正接管可选功能入口 | `MainWindow` 不应直接 import Edit/Deliver/Composite 深层实现 |
| 5 | Composite 收口 | 先修旧路径兼容，再逐步把真实实现移入 `features/composite` |
| 6 | Edit 收口 | 修好旧 `src.core.edit` / `src.ui.edit` 兼容层，再迁剩余引用 |
| 7 | Deliver 收口 | 确保 Deliver 可禁用、可迁移、可独立维护 |
| 8 | Color 低风险整理 | 只整理 UI/facade，核心调色管线暂不移动 |

---

## 3. 阶段 1：修复导入断裂和测试入口

### 目标

先恢复测试可运行性，不继续扩大目录迁移范围。

### 任务表

| ID | 文件 | 问题 | 修复方式 | 验收 |
|---|---|---|---|---|
| T-001 | `src/core/edit/__init__.py` | 仍 import 已迁走的 `.project`、`.serialization` 等 | 改成从 `src.features.edit.core.*` re-export，或删除主动聚合导入，只保留轻量兼容 | `.venv/bin/pytest tests/media/test_video_frame_provider_service.py -q` |
| T-002 | `src/core/edit/effect_preview_quality.py` | 这是旧路径下唯一真实文件之一 | 判断是否迁到 `src/shared/effects/preview_quality.py` 后 re-export | `rg "effect_preview_quality" src tests` |
| T-003 | `tests/media/test_video_frame_provider_service.py` | 测试仍使用旧 `src.core.edit` 路径 | 如果旧路径要兼容，则不改测试；如果决策废弃旧路径，则统一改到 `src.features.edit.core` | `tests/media` 不再因 Edit import 失败 |
| T-004 | `tests/node_test_base.py` | 仍导入 `src.composite.nodes.base_node` | 优先先提供旧路径兼容层；迁移完成后再改测试到 `src.features.composite` | `tests/test_nodes/test_time_nodes.py tests/test_nodes/test_color_nodes.py` 能收集 |
| T-005 | 全部 tests | 大量测试仍引用旧 `src.composite` / `src.core.edit` / `src.ui.edit` | 先列清单，不做批量盲改；按功能分批改 | `rg -n "src\\.composite|src\\.core\\.edit|src\\.ui\\.edit" tests` 数量下降 |

### 推荐修复策略

第一优先级不是“把测试全部改成新路径”，而是先让旧路径兼容层恢复。

原因：

- 当前很多内部代码和测试仍使用旧 import。
- 如果一次性全改测试，容易掩盖真实兼容问题。
- 模块化目标包含“未来可删除某功能文件夹”，但在过渡期需要明确的兼容策略。

建议先做：

```python
# src/core/edit/__init__.py
# 只从 src.features.edit.core re-export 稳定公开对象
```

并为高频旧路径补轻量 wrapper：

```text
src/core/edit/project.py -> re-export src.features.edit.core.project
src/core/edit/timeline_v2.py -> re-export src.features.edit.core.timeline_v2
src/core/edit/edit_frame_coordinator.py -> re-export src.features.edit.core.edit_frame_coordinator
```

Composite 同理，先保证旧测试可以 import，再继续真实迁移。

### 验收命令

```bash
python3 -m compileall src
.venv/bin/pytest tests/media/test_video_frame_provider_service.py -q
.venv/bin/pytest tests/test_nodes/test_time_nodes.py tests/test_nodes/test_color_nodes.py -q
```

通过标准：

- 不再出现 `ModuleNotFoundError: No module named 'src.core.edit.project'`。
- 不再出现 `ModuleNotFoundError: No module named 'src.composite.nodes.base_node'`。
- 不引入调色核心文件变更。

---

## 4. 阶段 2：清理临时文件和状态记录

### 目标

让仓库内容和文档表达真实状态，避免后续 AI 继续沿着错误结论工作。

### 任务表

| ID | 文件 | 问题 | 修复方式 | 验收 |
|---|---|---|---|---|
| H-001 | `src/features/composite/core/.!81660!shaders.py` | 疑似编辑器临时文件被跟踪 | 与 `src/features/composite/core/shaders.py` diff，确认无有效内容后删除 | `git ls-files | rg "\\.!.*!"` 无输出 |
| H-002 | `.gitignore` | 需要确认临时文件规则 | 增加或确认 `.DS_Store`、`__pycache__/`、`*.pyc`、`.!*!*` | `git check-ignore` |
| H-003 | docs | 多份计划文档状态可能互相冲突 | 保留历史计划，但新计划必须作为唯一当前执行入口 | README 或计划顶部指向当前文件 |

### 临时文件检查命令

```bash
git diff -- src/features/composite/core/.!81660!shaders.py src/features/composite/core/shaders.py
git ls-files | rg "\\.!.*!"
```

如果确认是临时文件：

```bash
git rm src/features/composite/core/.!81660!shaders.py
```

通过标准：

- Git 中不再跟踪 `.!...!` 临时文件。
- `python3 -m compileall src` 仍通过。

---

## 5. 阶段 3：建立可靠 feature flag 验收

### 当前问题

当前 offscreen smoke 会在无 OpenGL 平台报：

```text
QOpenGLWidget is not supported on this platform.
```

这不能直接证明真实 GUI 一定坏，但说明现有 smoke 不适合作为稳定 CI 验收。

### 目标

建立两层验收：

```text
无 GUI 单测：验证 feature flag、registry、module 注册，不创建 QOpenGLWidget。
真实 GUI smoke：在有图形环境的机器上验证 MainWindow。
```

### 任务表

| ID | 类型 | 内容 | 验收 |
|---|---|---|---|
| F-001 | 单测 | 新增 `tests/test_feature_flags_registry.py` | 不创建 `MainWindow`，只创建 `FeatureRegistry` 和模块 |
| F-002 | 单测 | 验证 `edit/composite/deliver` disabled 时不注册 page | `registry.get_all_pages()` 不含禁用页 |
| F-003 | 单测 | 验证 Color 始终注册 | disabled 其它功能时仍有 `color` |
| F-004 | 单测 | 验证 module import 不触发深层 GUI import | 使用 monkeypatch/import 检查 |
| F-005 | GUI smoke | 保留真实 `MainWindow` 启动脚本，但标记为手动或有 GUI 环境才跑 | 本机 GUI 启动无崩溃 |

### 推荐无 GUI 测试形态

```python
def test_disabled_optional_modules_do_not_register_pages():
    flags = FeatureFlags({"edit": False, "composite": False, "deliver": False})
    registry = FeatureRegistry()
    registry.initialize(AppContext(feature_flags=flags, ...))
    registry.register_module(ColorModule())
    assert [p.page_id for p in registry.get_all_pages()] == ["color"]
```

### 验收命令

```bash
.venv/bin/pytest tests/test_feature_flags_registry.py -q
python3 -m compileall src
```

通过标准：

- 不需要 OpenGL。
- 不创建 `Canvas`。
- 不创建 `QOpenGLWidget`。
- 能明确证明禁用功能不会注册入口。

---

## 6. 阶段 4：Registry 真正接管入口

### 当前问题

`FeatureRegistry` 已存在，但 `MainWindow` 仍直接 import 可选功能深层文件。

典型位置：

```text
src/ui/main_window.py
src/main.py
src/app/module_loader.py
```

### 目标

`MainWindow` 只认识：

```text
FeatureRegistry
PageDescriptor
FeatureModule
```

不再顶层 import：

```text
src.features.edit.ui.*
src.features.edit.core.*
src.features.deliver.ui.*
src.composite.*
src.features.composite.ui.*
```

### 任务表

| ID | 文件 | 当前问题 | 修复方式 | 验收 |
|---|---|---|---|---|
| R-001 | `src/ui/main_window.py` | 顶层 import Edit 深层 UI/core | 移入 `EditModule`，MainWindow 通过 registry 获取 page/panels | `rg "features\\.edit" src/ui/main_window.py` 明显下降 |
| R-002 | `src/ui/main_window.py` | 内部仍 import `src.composite.*` | 移入 `CompositeModule` 或 composite adapter | `rg "src\\.composite" src/ui/main_window.py` 无业务 import |
| R-003 | `src/app/module_loader.py` | Composite 仍从 `src.composite.module` 注册 | 改成 `src.features.composite.module` | `rg "src\\.composite\\.module" src/app src/main.py` 无输出 |
| R-004 | `src/main.py` | node plugin discovery 走旧 `src.composite.node_engine` | 移到 CompositeModule 内部注册 | disabled composite 时不 import node engine |
| R-005 | `src/app/feature_registry.py` | 已能注册 page，但生命周期仍弱 | 增加清晰的 page 创建/进入/离开/保存 handler 边界 | registry 单测 |

### 验收命令

```bash
rg -n "src\\.composite|features\\.edit|features\\.deliver" src/ui/main_window.py
rg -n "src\\.composite\\.module|src\\.composite\\.node_engine" src/app src/main.py
python3 -m compileall src
.venv/bin/pytest tests/test_feature_flags_registry.py -q
```

通过标准：

- 可选功能 disabled 时，入口注册阶段不会 import 对应深层实现。
- `MainWindow` 不再是 Edit/Composite/Deliver 的装配中心。
- Color 页面仍能正常创建。

---

## 7. 阶段 5：Composite 收口

### 当前问题

Composite 现在是最明显的“双路径”状态：

```text
src/features/composite/     # 新目录，已有部分 core/nodes/render/utils
src/composite/              # 旧目录，仍有 node_engine/ui/renderer 等真实代码
```

并且新目录内部还有反向依赖旧目录：

```text
src/features/composite/evaluators/base_evaluator.py -> src.composite.node_engine
src/features/composite/render/composite_export_renderer.py -> src.composite.graph_evaluator_mixin
src/features/composite/nodes/image_input_node.py -> src.composite.image_loader
src/features/composite/core/node_capabilities.py -> 文档仍提示 src.composite
```

### 目标

Composite 最终结构：

```text
src/features/composite/
  module.py
  core/
    node_engine.py
    graph_snapshot.py
    format.py
    node_capabilities.py
  nodes/
  render/
  evaluators/
  ui/
  utils/
```

旧路径最终只做兼容层：

```text
src/composite/*.py -> from src.features.composite... import *
```

### 任务表

| ID | 内容 | 修复方式 | 验收 |
|---|---|---|---|
| C-001 | 修复 `features/composite/module.py` 反向导入旧 UI | 如果 UI 尚未迁移，先保持兼容但标注；最终改到 `src.features.composite.ui.composite_page` | `CompositeModule.is_available()` 正常 |
| C-002 | 迁移 `node_engine.py` | 从 `src/composite/node_engine.py` 移到 `src/features/composite/core/node_engine.py`，旧路径 re-export | 节点 registry 测试通过 |
| C-003 | 迁移 `image_loader.py` | 移到 `src/features/composite/core/image_loader.py` 或 `adapters/image_loader.py` | image input node 测试通过 |
| C-004 | 迁移 `graph_evaluator_mixin.py` | 移到 `src/features/composite/evaluators/graph_evaluator_mixin.py` | composite cache/render 测试通过 |
| C-005 | 迁移 `ui/` | 迁到 `src/features/composite/ui/`，旧 `src/composite/ui/*` re-export | GUI Composite 页面可打开 |
| C-006 | 旧路径兼容层 | 保留旧 import 可用，直到所有测试迁移 | `python -c "from src.composite.nodes.base_node import Node"` 成功 |
| C-007 | 删除演练 | 临时 rename 旧目录验证 disabled 模式 | `TOPOS_FEATURE_COMPOSITE=0` 下不依赖旧目录 |

### 禁止事项

- 不要一次性移动全部 `src/composite` 文件。
- 不要同时维护两份真实 `node_engine.py`。
- 不要先删除旧路径再修测试。

### 验收命令

```bash
python3 -m compileall src
.venv/bin/pytest tests/test_nodes/test_time_nodes.py tests/test_nodes/test_color_nodes.py -q
.venv/bin/pytest tests/test_composite_graph.py tests/test_composite_format.py -q
rg -n "from src\\.composite|import src\\.composite" src/features/composite
```

通过标准：

- `src/features/composite` 内不再反向 import `src.composite`。
- 旧 `src/composite` 只是 wrapper，不再含主要业务实现。
- Composite disabled 时不注册 Composite page，也不加载 Composite UI。

---

## 8. 阶段 6：Edit 收口

### 当前问题

Edit 真实代码已经在：

```text
src/features/edit/core/
src/features/edit/ui/
```

但旧路径没有形成可靠兼容层：

```text
src/core/edit/__init__.py
src/core/edit/effect_preview_quality.py
src/ui/edit/__init__.py
```

测试和部分共享模块仍引用旧路径。

### 任务表

| ID | 内容 | 修复方式 | 验收 |
|---|---|---|---|
| E-001 | 修复 `src/core/edit` 兼容层 | 为高频旧模块增加 wrapper 文件，或把 `__init__.py` 改轻量 | `tests/media/test_video_frame_provider_service.py` 通过 |
| E-002 | 修复 `src/ui/edit` 兼容层 | 为测试和旧 UI import 提供 wrapper，或统一测试迁移到 `features.edit.ui` | visual/edit tests 可收集 |
| E-003 | 更新共享模块依赖 | `src/core/project/offline_asset_manager.py`、`src/core/media/proxy_manager.py` 等不应强依赖 Edit feature | 抽协议或移入 Edit adapter |
| E-004 | SaveRouter 解耦 | `SaveRouter` 不应在顶层依赖 Edit 具体类 | SaveRouter 单测通过 |
| E-005 | Deliver 对 Edit 的依赖边界 | Deliver 需要 timeline export 时，应通过 adapter/contract 调用 | Deliver disabled/Edit disabled 组合 smoke |

### 兼容层建议

短期可以接受：

```python
# src/core/edit/project.py
from src.features.edit.core.project import *
```

但必须加约束：

- wrapper 不写业务逻辑。
- wrapper 不复制类定义。
- wrapper 后续有删除计划。

### 验收命令

```bash
python3 -m compileall src
.venv/bin/pytest tests/media/test_video_frame_provider_service.py -q
.venv/bin/pytest tests/edit/test_color_stack.py tests/edit/test_edit_project.py -q
rg -n "src\\.core\\.edit|src\\.ui\\.edit" src tests
```

通过标准：

- 旧路径 import 不再崩。
- 新真实源只有 `src/features/edit`。
- `TOPOS_FEATURE_EDIT=0` 时 MainWindow 不创建 Edit panels。

---

## 9. 阶段 7：Deliver 收口

### 当前状态

Deliver 已有：

```text
src/features/deliver/module.py
src/features/deliver/ui/deliver_page.py
```

但需要继续确认：

- 是否还有旧 `src/ui/deliver_page.py` 引用。
- Export 相关代码到底属于 Deliver 还是 Color/Shared。
- Edit timeline export 是否是 Deliver 的硬依赖。

### 任务表

| ID | 内容 | 修复方式 | 验收 |
|---|---|---|---|
| D-001 | 查清旧 Deliver 引用 | `rg "deliver_page|DeliverPage" src tests` | 只剩新路径或兼容层 |
| D-002 | 梳理 export 归属 | `src/core/export` 若多功能共用，放 shared；若仅 Deliver，用 features/deliver/core | import 图清楚 |
| D-003 | Deliver disabled 验收 | disabled 时不创建 page/dock/menu | feature flag 单测 + GUI smoke |
| D-004 | Deliver/Edit 边界 | timeline export 通过 contract 调用，不顶层绑定 Edit | Edit disabled 时 Deliver 行为明确 |

### 验收命令

```bash
rg -n "deliver_page|DeliverPage|features\\.deliver" src tests
python3 -m compileall src
.venv/bin/pytest tests/test_config_manager.py -q
```

通过标准：

- Deliver 页面由 `DeliverModule` 创建。
- disabled Deliver 不影响 Color/Edit/Composite。
- Export 公共代码的归属有明确说明。

---

## 10. 阶段 8：Color 低风险整理

### 当前结论

调色核心是当前软件核心功能，当前不建议移动：

```text
src/core/image_engine.py
src/core/shader_compiler.py
src/core/async_renderer.py
src/core/gl_backend.py
src/core/layer.py
```

这些文件可以通过 `ColorFacade` 暴露能力，但不要为了目录整齐搬走。

### 可优化范围

| ID | 内容 | 修复方式 | 验收 |
|---|---|---|---|
| CO-001 | Color UI import 收口 | 保持 `src/features/color/ui/*` 为真实 UI 源 | Color 页面手测 |
| CO-002 | ColorFacade 明确边界 | 只暴露其它功能需要的 color output / render result / still frame | facade 单测 |
| CO-003 | 调色核心保护测试 | 固定运行 image engine/layer 基础测试 | `tests/test_image_engine_state.py tests/test_image_engine_layers.py` |
| CO-004 | 禁止无关重构 | 迁移 Edit/Composite/Deliver 时不改 shader/render 主线 | code review |

### 每次动 Color UI 后必须手测

- 打开图片。
- Lift/Gamma/Gain 调整。
- 曲线。
- HSL 曲线。
- LUT。
- Mask。
- Qualifier。
- Soft Clip。
- 保存后重开。

### 自动验收命令

```bash
python3 -m compileall src
.venv/bin/pytest tests/test_image_engine_state.py tests/test_image_engine_layers.py -q
```

通过标准：

- 调色基础测试通过。
- 不出现渲染核心回归。
- 不把 Color 核心移入 `features/color/core`，除非后续单独制定高风险迁移计划。

---

## 11. 最终目录目标

目标结构：

```text
src/
  app/
  shared/
  features/
    color/
      facade.py
      module.py
      ui/
    edit/
      module.py
      core/
      ui/
      adapters/
    composite/
      module.py
      core/
      ui/
      nodes/
      render/
      evaluators/
      utils/
    deliver/
      module.py
      core/
      ui/
```

过渡期允许存在：

```text
src/composite/
src/core/edit/
src/ui/edit/
```

但这些旧路径必须满足：

- 只做兼容 wrapper。
- 不放新的真实业务逻辑。
- 不和 `features/*` 形成双源维护。
- 有删除演练和测试覆盖。

---

## 12. 每轮固定验收命令

每完成一个小阶段，至少运行：

```bash
git status --short
python3 -m compileall src
.venv/bin/pytest tests/test_config_manager.py -q
.venv/bin/pytest tests/test_image_engine_state.py tests/test_image_engine_layers.py -q
```

涉及 Edit 时追加：

```bash
.venv/bin/pytest tests/media/test_video_frame_provider_service.py -q
.venv/bin/pytest tests/edit/test_color_stack.py tests/edit/test_edit_project.py -q
```

涉及 Composite 时追加：

```bash
.venv/bin/pytest tests/test_nodes/test_time_nodes.py tests/test_nodes/test_color_nodes.py -q
.venv/bin/pytest tests/test_composite_graph.py tests/test_composite_format.py -q
```

涉及 feature flag / registry 时追加：

```bash
.venv/bin/pytest tests/test_feature_flags_registry.py -q
```

真实 GUI 环境下手动验证：

```bash
TOPOS_FEATURE_EDIT=0 python3 src/main.py
TOPOS_FEATURE_DELIVER=0 python3 src/main.py
TOPOS_FEATURE_COMPOSITE=0 python3 src/main.py
TOPOS_FEATURE_EDIT=0 TOPOS_FEATURE_DELIVER=0 TOPOS_FEATURE_COMPOSITE=0 python3 src/main.py
```

通过标准：

- 禁用功能不显示对应 page/dock/menu。
- Color 仍可正常打开、调色、保存。
- 没有新的 `ModuleNotFoundError`。
- 没有新的旧路径真实业务文件。

---

## 13. 给后续 AI 的执行 Prompt

```text
请执行 docs/modularization_next_cleanup_plan_2026-05-25.md 的阶段 1：

目标：
只修复导入断裂和测试入口，不继续移动业务目录，不改调色核心。

必须处理：
1. 修复 src/core/edit 旧路径兼容层，使 tests/media/test_video_frame_provider_service.py 不再因为 src.core.edit.project 缺失失败。
2. 修复 Composite 旧路径兼容或测试入口，使 tests/test_nodes/test_time_nodes.py 和 tests/test_nodes/test_color_nodes.py 能正常收集。
3. 检查 src/features/composite/core/.!81660!shaders.py 是否为临时文件，如确认无效则删除。
4. 不修改 src/core/image_engine.py、src/core/shader_compiler.py、src/core/async_renderer.py，除非有明确测试失败需要且先说明原因。

验证：
python3 -m compileall src
.venv/bin/pytest tests/media/test_video_frame_provider_service.py -q
.venv/bin/pytest tests/test_nodes/test_time_nodes.py tests/test_nodes/test_color_nodes.py -q
.venv/bin/pytest tests/test_image_engine_state.py tests/test_image_engine_layers.py -q

输出：
列出改动文件、每条验证命令和结果、仍未完成的问题。
```

---

## 14. 完成标准

只有同时满足下面条件，才能把模块化计划标为完成：

- `src/features/*` 是真实功能源，不只是外壳。
- `MainWindow` 不直接 import 可选功能深层实现。
- 旧路径只做 wrapper，且可以解释删除计划。
- `TOPOS_FEATURE_EDIT=0`、`TOPOS_FEATURE_DELIVER=0`、`TOPOS_FEATURE_COMPOSITE=0` 均可启动。
- Composite/Edit/Deliver 的核心测试可以运行并通过。
- Color 的核心测试和人工调色流程通过。
- 文档状态、测试状态、Git 状态三者一致。
