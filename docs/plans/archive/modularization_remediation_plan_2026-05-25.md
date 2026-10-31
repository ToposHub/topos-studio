# Topos Color 模块化验收问题修复计划表

版本：2026-05-25  
状态：基于当前模块化改造后的验收结果  
目标：修复当前 feature flag / Registry / 目录拆分未完成的问题，并给出下一轮可执行计划  
核心原则：**先让禁用真正可用，再做删除验证，最后迁移目录**

---

## 0. 当前结论

当前项目已经完成了一部分模块化基础设施：

- 已新增 `src/app/`
- 已新增 `src/shared/`
- 已新增 `src/features/`
- 已新增 `src/features/color/facade.py`
- 已新增 `src/features/edit/module.py`
- 已新增 `src/features/deliver/module.py`
- 已新增 `src/composite/module.py`
- 已抽出 `src/shared/keyframe.py`
- `src/composite/keyframe.py` 已变成兼容 re-export
- `src/main.py` 已初步接入 feature flags 和 FeatureRegistry
- `src/core/save_router.py` 已初步加入 handler 注册机制

但是目前还没有达到“功能可禁用 / 可删除”的目标。

当前最关键的问题是：

1. `Edit` 关闭会崩溃。
2. `Deliver` 关闭有崩溃风险。
3. `MainWindow` 仍然直接创建 Edit / Composite / Deliver 的很多对象。
4. FeatureRegistry 目前更像“登记表”，还没有真正接管页面创建。
5. `SaveRouter` 仍然硬依赖 Edit。
6. 目录结构仍然混杂，`features/` 只是新增了外壳，没有真正收拢 core/ui/adapters。
7. `tests/media` 有 1 个失败。
8. `topos.db` 被修改，可能不应进入模块化提交。

因此下一轮不要继续追求“大搬家”，应该先完成下面三个里程碑：

```text
Milestone A：feature flag 真正可用
Milestone B：Registry 真正接管页面/菜单/保存入口
Milestone C：目录迁移，按 feature 收拢文件
```

---

## 1. 验收时发现的问题清单

| ID | 严重级别 | 问题 | 当前表现 | 影响 | 修复阶段 |
|---|---|---|---|---|---|
| M-001 | P1 | Edit 关闭会崩溃 | `TOPOS_FEATURE_EDIT=0` 时 `setup_menus()` 访问 `None.toggleViewAction()` | Edit feature flag 不可用 | 阶段 1 |
| M-002 | P1 | Edit 关闭仍实例化 EditPage 等对象 | `create_panels()` 无条件 `EditPage()` | 删除 `src/ui/edit` 必崩 | 阶段 1 |
| M-003 | P1 | Deliver 关闭有空指针风险 | 默认布局无条件访问 `self.dock_deliver` | Deliver feature flag 不可靠 | 阶段 1 |
| M-004 | P1 | 页面切换按钮未过滤 Edit/Deliver | Page switcher 只过滤 Composite | 用户可切到已关闭页面 | 阶段 1 |
| M-005 | P1 | 默认布局无条件创建 Edit/Deliver 布局 | `apply_default_layout()` 访问 Edit/Deliver dock | 关闭模块时布局初始化崩溃 | 阶段 1 |
| M-006 | P1 | 页面切换 hook 无条件访问 Edit | `enter_page_hook("Edit")` / `leave_page_hook("Edit")` 访问 `self.edit_page` | 关闭 Edit 后页面切换崩溃 | 阶段 1 |
| M-007 | P2 | Registry 没有真正驱动 MainWindow | Registry 注册了 module，但 MainWindow 仍自己创建页面 | 架构没有真正解耦 | 阶段 2 |
| M-008 | P2 | CompositeModule 保存 handler 是空保存 | `_save_composite_handler()` 只 `to_dict()` 然后 return True | SaveRouter handler 化不完整 | 阶段 2 |
| M-009 | P2 | SaveRouter 顶层依赖 Edit | `from src.core.edit.project import ColorStackData` | 删除 Edit core 会导致 SaveRouter import 失败 | 阶段 2 |
| M-010 | P2 | `src/features/edit` 只是壳 | 实际 core/ui 仍在 `src/core/edit` 和 `src/ui/edit` | 目录仍混乱 | 阶段 3 |
| M-011 | P2 | `src/features/deliver` 只是壳 | 实际 Deliver UI 仍在 `src/ui/deliver_page.py` | 删除 Deliver 不成立 | 阶段 3 |
| M-012 | P2 | Composite 仍在 `src/composite` | 还没有迁移到 `src/features/composite` | 目录不统一 | 阶段 3 |
| M-013 | P2 | Color 仍分散在 `src/core` / `src/ui` | Color facade 有了，但核心和 UI 仍散落 | 长期维护不清晰 | 阶段 4 |
| M-014 | P2 | `tests/media` 失败 1 个 | `AudioMetadata.bytes_per_sample` 默认值与测试不一致 | 测试未全绿 | 阶段 0 |
| M-015 | P3 | `topos.db` 被修改 | git status 显示 binary diff | 可能污染模块化提交 | 阶段 0 |

---

## 2. 当前验证结果

已通过：

```bash
python3 -m compileall src
.venv/bin/pytest tests/test_config_manager.py -q
.venv/bin/pytest tests/test_nodes/test_time_nodes.py tests/test_nodes/test_color_nodes.py -q
```

结果：

```text
compileall: passed
test_config_manager.py: 4 passed
test_time_nodes.py + test_color_nodes.py: 85 passed
```

未通过：

```bash
.venv/bin/pytest tests/media -q
```

结果：

```text
1 failed, 1376 passed, 44 skipped
```

失败点：

```text
tests/media/test_audio_metadata.py::TestAudioMetadataBytesPerSample::test_no_bit_depth_default
```

当前代码：

```python
if self.bit_depth is None:
    return 2  # 默认 16-bit
return (self.bit_depth // 8) * self.channels
```

测试期望：

```python
AudioMetadata(sample_rate=48000, channels=2, bit_depth=None).bytes_per_sample == 4
```

修复建议：

```python
if self.bit_depth is None:
    return 2 * self.channels  # 默认 16-bit，每声道 2 bytes
return (self.bit_depth // 8) * self.channels
```

---

## 3. 目标目录结构

你期望的最终结构是对的，建议坚持：

```text
src/
  app/
    __init__.py
    main_window_shell.py
    app_context.py
    feature_registry.py
    module_loader.py
    page_contract.py
    save_contract.py
    export_contract.py
    layout_manager.py
    menu_manager.py

  shared/
    __init__.py
    keyframe.py
    media/
    gpu/
    project/
    config/
    i18n/
    logging/
    render_contracts.py
    feature_flags.py

  features/
    color/
      __init__.py
      module.py
      contracts.py
      facade.py
      adapters.py
      core/
      ui/
      shaders/

    edit/
      __init__.py
      module.py
      contracts.py
      adapters.py
      core/
      ui/

    composite/
      __init__.py
      module.py
      contracts.py
      adapters.py
      core/
      ui/
      nodes/
      shaders/

    deliver/
      __init__.py
      module.py
      contracts.py
      adapters.py
      core/
      ui/
```

但不要马上全部移动。正确顺序是：

1. 先修 feature flag 崩溃。
2. 再让 Registry 真正驱动页面、菜单、保存 handler。
3. 再迁移 Composite。
4. 再迁移 Deliver。
5. 再迁移 Edit。
6. 最后才考虑 Color。

原因：Color 是核心调色主线，不能作为第一批搬迁对象。

---

## 4. 阶段 0：先修测试和工作区卫生

### 目标

在继续模块化前，让测试基线干净，避免把无关失败和模块化失败混在一起。

### 任务表

| 步骤 | 文件 | 动作 | 验证 |
|---|---|---|---|
| 0.1 | `src/core/media/metadata.py` | 修复 `bytes_per_sample` 默认 bit depth 逻辑 | `.venv/bin/pytest tests/media/test_audio_metadata.py -q` |
| 0.2 | `topos.db` | 确认是否需要保留修改；如果不是模块化必要改动，不纳入提交 | `git status --short` |
| 0.3 | `docs/modularization_baseline_2026-05-25.md` | 保留基线文档 | `git status --short docs/...` |

### `bytes_per_sample` 修复方式

当前：

```python
if self.bit_depth is None:
    return 2  # 默认 16-bit
return (self.bit_depth // 8) * self.channels
```

应改为：

```python
if self.bit_depth is None:
    return 2 * self.channels  # 默认 16-bit，每声道 2 bytes
return (self.bit_depth // 8) * self.channels
```

### 验收命令

```bash
python3 -m compileall src
.venv/bin/pytest tests/media/test_audio_metadata.py -q
.venv/bin/pytest tests/media -q
```

### 通过标准

- `tests/media` 全绿，或者只剩已有且明确记录的非相关失败。
- `topos.db` 是否保留有明确决定。

---

## 5. 阶段 1：让 feature flag 真正可用

### 目标

关闭 Edit / Deliver / Composite 后，应用能够启动，不显示对应页面，不创建对应 dock，不执行对应菜单逻辑。

### 当前问题

当前 `MainWindow` 仍有大量无条件访问：

- `self.edit_page = EditPage()`
- `self.media_pool_panel = MediaPoolPanel()`
- `self.timeline_panel = TimelinePanel()`
- `self.edit_properties_panel = EditPropertiesPanel()`
- `self.edit_effects_panel = EditEffectsPanel()`
- `panels_menu.addAction(self.dock_edit_media_pool.toggleViewAction())`
- `self.dock_deliver.setVisible(True)`
- `pages = ["Library", "Edit", "Color", "Composite", "Deliver"]`

### 设计规则

每个可选模块必须满足：

```python
if feature disabled:
    不 import 深层模块
    不实例化 UI
    不创建 dock
    不添加菜单 action
    不添加 page button
    不进入 page hook
    不注册 save handler
```

### 具体修复表

| ID | 文件 | 当前问题 | 修复方式 | 验证 |
|---|---|---|---|---|
| 1.1 | `src/ui/main_window.py` | `create_panels()` 无条件创建 Edit UI | 用 `_edit_available` 包住 EditPage / MediaPool / Timeline / Properties / Effects 创建；关闭时全部设为 `None` | offscreen 构造 |
| 1.2 | `src/ui/main_window.py` | `setup_docks()` 创建 properties/edit effects 不完整受控 | Edit 关闭时不要创建 Edit 专属 dock；共享 properties dock 如 Color 仍需要，则改名为 generic properties 或只在需要时创建 | offscreen 构造 |
| 1.3 | `src/ui/main_window.py` | `setup_menus()` 无条件添加 Edit dock actions | 只有 dock 不为 None 才 addAction | offscreen 构造 |
| 1.4 | `src/ui/main_window.py` | `apply_default_layout()` 无条件生成 Edit layout | `if self._edit_available:` 才生成 Edit layout | `TOPOS_FEATURE_EDIT=0` 启动 |
| 1.5 | `src/ui/main_window.py` | `apply_default_layout()` 无条件生成 Deliver layout | `if self._deliver_available:` 才生成 Deliver layout | `TOPOS_FEATURE_DELIVER=0` 启动 |
| 1.6 | `src/ui/main_window.py` | page switcher 未过滤 Edit / Deliver | 根据 `_edit_available`、`_deliver_available` 过滤 pages | UI 检查 |
| 1.7 | `src/ui/main_window.py` | `switch_to_page("Edit")` 可进入禁用页 | 在 `switch_to_page` 开头拒绝不可用页面并 fallback 到 Color | 自动测试 |
| 1.8 | `src/ui/main_window.py` | `enter_page_hook/leave_page_hook` 无条件访问 Edit | 加 `if self._edit_available and self.edit_page` | offscreen 构造 |
| 1.9 | `src/ui/main_window.py` | `_enforce_page_dock_visibility` 对 None 调 `setVisible` | 过滤 None：`for d in edit_only: if d: d.setVisible(False)` | offscreen 构造 |
| 1.10 | `src/ui/main_window.py` | saved current_page 可能是禁用页 | 初始化时如果 saved page disabled，改为 Color | 启动测试 |

### 推荐辅助函数

在 `MainWindow` 内新增：

```python
def _is_page_available(self, page_name: str) -> bool:
    page = page_name.lower()
    if page == "edit":
        return self._edit_available
    if page == "composite":
        return self._composite_available
    if page == "deliver":
        return self._deliver_available
    return page in ("library", "color")

def _iter_existing_docks(self, docks: list[Any]) -> list[Any]:
    return [dock for dock in docks if dock is not None]
```

然后把所有：

```python
for d in [self.dock_edit_media_pool, ...]:
    d.setVisible(False)
```

改成：

```python
for d in self._iter_existing_docks([...]):
    d.setVisible(False)
```

### offscreen 验证脚本

每次修改后跑：

```bash
QT_QPA_PLATFORM=offscreen TOPOS_FEATURE_EDIT=0 TOPOS_FEATURE_DELIVER=0 TOPOS_FEATURE_COMPOSITE=0 .venv/bin/python - <<'PY'
import sys
from PyQt6.QtWidgets import QApplication
from src.shared.feature_flags import load_feature_flags
from src.utils.config_manager import ConfigManager
from src.ui.main_window import MainWindow

app = QApplication(sys.argv)
flags = load_feature_flags(ConfigManager())
window = MainWindow(feature_flags=flags)
print("constructed", window._edit_available, window._deliver_available, window._composite_available)
window.close()
app.quit()
PY
```

### 通过标准

以下组合都必须能构造窗口：

```bash
TOPOS_FEATURE_EDIT=0
TOPOS_FEATURE_DELIVER=0
TOPOS_FEATURE_COMPOSITE=0
TOPOS_FEATURE_EDIT=0 TOPOS_FEATURE_DELIVER=0 TOPOS_FEATURE_COMPOSITE=0
```

页面按钮中不能出现已关闭功能。

---

## 6. 阶段 2：让 Registry 真正接管页面、菜单、保存入口

### 目标

现在 `FeatureRegistry` 已经存在，但 `MainWindow` 仍然直接创建大部分功能。阶段 2 要让 Registry 变成真正的功能入口。

### 当前问题

`src/main.py` 注册了：

```python
ColorModule()
CompositeModule()
```

但是：

- `MainWindow` 自己创建 `CompositePage`
- `MainWindow` 自己创建 `EditPage`
- `MainWindow` 自己创建 `DeliverPage`
- Registry 的 `PageDescriptor` 没有被主窗口消费
- `AppContext.save_router` 创建时还没有传回 Registry
- `CompositeModule.register_save_handlers()` 基本没发挥作用

### 修复表

| ID | 文件 | 当前问题 | 修复方式 | 验证 |
|---|---|---|---|---|
| 2.1 | `src/main.py` | 先创建 registry，再创建 MainWindow，但 context 没 main_window / save_router | 创建 MainWindow 后补齐 `app_context.main_window` 和 `app_context.save_router`，再 register modules，或把 registry 初始化放到 MainWindow 内部 | 启动 |
| 2.2 | `src/app/feature_registry.py` | 注册时只立即调用 module hooks | 支持 `bind_context(context)` / `register_all()`，避免 context 不完整 | 单元测试 |
| 2.3 | `src/ui/main_window.py` | 不消费 `PageDescriptor` | 页面按钮从 registry + builtin pages 生成 | UI 检查 |
| 2.4 | `src/composite/module.py` | 创建 page 但 MainWindow 不用 | MainWindow 从 registry 获取 `composite` page factory 创建 | Composite 开启测试 |
| 2.5 | `src/features/edit/module.py` | `register_pages()` 返回空 | 返回 Edit PageDescriptor；第一版可 factory 调用旧 `EditPage` | Edit 开启测试 |
| 2.6 | `src/features/deliver/module.py` | `register_pages()` 返回空 | 返回 Deliver PageDescriptor | Deliver 开启测试 |
| 2.7 | `src/core/save_router.py` | handler 注册是补丁，默认硬编码还在 | 保留 fallback，但模块开启时使用 handler；模块关闭时禁止进入对应 handler | 保存测试 |
| 2.8 | `src/composite/module.py` | `_save_composite_handler` 空保存 | 改成调用 MainWindow 原 `_get_composite_graph_data` 或 Composite service，确保实际写入工程 | 保存/重开工程 |

### 推荐 App 初始化顺序

当前顺序应调整为：

```python
config_manager = ConfigManager()
feature_flags = load_feature_flags(config_manager)

app_context = AppContext(
    config_manager=config_manager,
    feature_flags=feature_flags,
    i18n=I18N,
    logger=logger,
)

registry = FeatureRegistry()
registry.initialize(app_context)

window = MainWindow(
    feature_flags=feature_flags,
    feature_registry=registry,
)

app_context.main_window = window
app_context.save_router = window.save_router

discover_and_register_modules(registry)
window.build_registered_pages()
```

如果现阶段不适合大改启动顺序，也可以临时：

```python
window = MainWindow(...)
window.attach_feature_registry(registry)
```

### 通过标准

- 关闭某个 feature 时，该 module 不注册。
- 开启某个 feature 时，页面来自 module factory。
- `MainWindow` 不再顶部直接 import Edit/Deliver/Composite 深层 UI。
- `FeatureRegistry.get_all_pages()` 的结果能决定页面切换器。

---

## 7. 阶段 3：完成 `features/` 目录收拢

### 目标

让文件结构真正接近：

```text
features/edit/core + ui
features/composite/core + ui + nodes
features/deliver/core + ui
features/color/core + ui + shaders
```

但迁移必须分批，不要一次移动所有功能。

---

## 8. 阶段 3A：迁移 Composite 到 `src/features/composite`

### 为什么先迁移 Composite

Composite 当前已经基本独立在 `src/composite/`，迁移成本最低。

### 目标结构

从：

```text
src/composite/
  node_engine.py
  composite_renderer.py
  composite_export_renderer.py
  evaluators/
  nodes/
  ui/
  shaders.py
  ...
```

迁到：

```text
src/features/composite/
  __init__.py
  module.py
  contracts.py
  adapters.py
  core/
    node_engine.py
    composite_renderer.py
    composite_export_renderer.py
    evaluators/
    ...
  nodes/
  ui/
  shaders/
```

### 迁移方式

不要直接删除 `src/composite`。  
第一轮保留兼容层：

```text
src/composite/__init__.py
src/composite/node_engine.py
src/composite/ui/...
```

兼容层可以 re-export 或薄包装：

```python
from src.features.composite.core.node_engine import *
```

### 迁移步骤

| 步骤 | 动作 | 验证 |
|---|---|---|
| 3A.1 | 新建 `src/features/composite/` 完整目录 | `python3 -m compileall src` |
| 3A.2 | 移动 `src/composite/module.py` 到 `src/features/composite/module.py` | import 测试 |
| 3A.3 | 移动 core 文件到 `features/composite/core/` | `pytest tests/test_nodes` |
| 3A.4 | 移动 `nodes/` 到 `features/composite/nodes/` | `pytest tests/test_nodes` |
| 3A.5 | 移动 `ui/` 到 `features/composite/ui/` | GUI 启动 |
| 3A.6 | 在旧 `src/composite` 留兼容 re-export | 旧 import 不坏 |
| 3A.7 | 更新新代码 import 到新路径 | `rg "from src.composite"` |
| 3A.8 | 最后执行删除测试 | rename 测试 |

### 删除测试

```bash
mv src/composite src/composite.disabled
TOPOS_FEATURE_COMPOSITE=0 python3 -m compileall src
mv src/composite.disabled src/composite
```

### 通过标准

- 新代码不再依赖旧 `src/composite`。
- 旧工程仍可打开。
- 旧路径兼容层仍可用。
- `TOPOS_FEATURE_COMPOSITE=0` 时可启动。

---

## 9. 阶段 3B：迁移 Deliver 到 `src/features/deliver`

### 当前文件

```text
src/ui/deliver_page.py
src/core/export/
src/core/edit/timeline_export.py
src/ui/export_panel.py
src/ui/export_dialog.py
src/ui/quick_export_dialog.py
```

注意：`export_panel.py` 当前可能属于 Color 的快速导出，也可能属于 Deliver，需要先确认职责。

### 目标结构

```text
src/features/deliver/
  module.py
  contracts.py
  adapters.py
  core/
    encoder.py
    presets.py
    frame_provider.py
    sequence_exporter.py
    timeline_export.py
  ui/
    deliver_page.py
    export_dialog.py
    quick_export_dialog.py
```

### 迁移原则

- 如果某个导出 UI 是 Color 页面常用，不要急着迁走。
- 先让 Deliver 可禁用，再移动文件。
- 先抽 ExportSource contract，让 Color/Edit/Composite 注册导出源。

### 修复表

| 步骤 | 动作 | 验证 |
|---|---|---|
| 3B.1 | `DeliverModule.register_pages()` 返回真实 PageDescriptor | Deliver 开启 |
| 3B.2 | `MainWindow` 通过 registry 创建 DeliverPage | Deliver 开启 |
| 3B.3 | 关闭 Deliver 时不创建 page/dock/menu | offscreen |
| 3B.4 | 抽 `ExportSource` protocol | 单元测试 |
| 3B.5 | 迁移 `deliver_page.py` 到 `features/deliver/ui/` | GUI |
| 3B.6 | 旧路径保留兼容 re-export | compileall |

### 通过标准

- `TOPOS_FEATURE_DELIVER=0` 启动不崩。
- Deliver 页面按钮消失。
- Deliver 菜单/保存 handler 消失。
- Color 仍能保存和快速导出，除非快速导出明确归 Deliver。

---

## 10. 阶段 3C：迁移 Edit 到 `src/features/edit`

### 当前文件

```text
src/core/edit/
src/ui/edit/
```

### 目标结构

```text
src/features/edit/
  module.py
  contracts.py
  adapters.py
  core/
    project.py
    timeline_v2.py
    commands.py
    selection.py
    clip_params.py
    effect_instance.py
    effect_registry.py
    timeline_ops.py
    timeline_render_plan.py
    timeline_preview_renderer.py
    edit_project_io.py
    ...
  ui/
    edit_page.py
    media_pool_panel.py
    timeline_panel.py
    timeline_view.py
    timeline_items.py
    edit_properties_panel.py
    edit_effects_panel.py
    source_viewer.py
    program_viewer.py
    ...
```

### 迁移难点

Edit 被很多模块引用：

- `SaveRouter`
- `DeliverPage`
- `media/proxy_manager`
- `project/offline_asset_manager`
- `ui/main_window`
- `video_playback`
- `shader_compiler` 通过 `effect_preview_quality`

所以 Edit 不能直接大搬家。

### 建议先拆公共部分

部分文件其实不应该属于 Edit，而应该属于 shared：

| 当前文件 | 建议去向 | 原因 |
|---|---|---|
| `src/core/edit/effect_preview_quality.py` | `src/shared/render_quality.py` 或 `src/shared/effects/preview_quality.py` | shader/video/plugin 都在用，不是 Edit 独有 |
| `src/core/edit/effect_instance.py` | 暂留 Edit，后续评估是否 shared/effects | Color/Timeline 可能共用 |
| `src/core/edit/clip_params.py` | `features/edit/core`，但 Transform/Crop 可抽 shared | 部分参数可跨功能 |
| `src/core/edit/project.py` | `features/edit/core/project.py` | Edit 项目模型 |

### 迁移步骤

| 步骤 | 动作 | 验证 |
|---|---|---|
| 3C.1 | 先让 Edit 关闭完全可用 | offscreen |
| 3C.2 | 把 `effect_preview_quality.py` 抽到 shared | `pytest tests/test_nodes tests/media` |
| 3C.3 | `EditModule.register_pages()` 创建 EditPage | GUI |
| 3C.4 | MainWindow 移除 Edit 顶层 import | compileall |
| 3C.5 | 移动 `src/ui/edit` 到 `features/edit/ui` | GUI |
| 3C.6 | 旧 `src/ui/edit` 保留兼容层 | compileall |
| 3C.7 | 移动 `src/core/edit` 到 `features/edit/core` | tests/media |
| 3C.8 | 旧 `src/core/edit` 保留兼容层 | full tests |
| 3C.9 | 执行 Edit 删除测试 | rename |

### 删除测试

最终目标：

```bash
mv src/features/edit src/features/edit.disabled
TOPOS_FEATURE_EDIT=0 python3 -m compileall src
mv src/features/edit.disabled src/features/edit
```

注意：这个目标只有在 `src/core/edit` 和 `src/ui/edit` 旧路径不再是真实现路径后才有意义。

---

## 11. 阶段 4：整理 Color，但不要先移动核心

### 当前 Color 文件分布

Color 当前散在：

```text
src/core/image_engine.py
src/core/shader_compiler.py
src/core/async_renderer.py
src/core/gl_backend.py
src/core/layer.py
src/core/color_manager.py
src/core/color_session.py
src/core/color_output.py
src/core/lut_exporter.py
src/ui/canvas.py
src/ui/widgets/color_wheel.py
src/ui/adjustment_panel.py
src/ui/hsl_curves_panel.py
src/ui/lut_panel.py
src/ui/mask_panel.py
src/ui/qualifier_panel.py
src/ui/soft_clip_panel.py
src/ui/raw_panel.py
src/ui/plugin_panel.py
src/core/shaders/
```

### 原则

Color 是核心调色主线，不能像 Composite/Edit 那样先搬。  
正确做法是先建立清晰外壳：

```text
src/features/color/
  module.py
  facade.py
  contracts.py
  adapters.py
```

然后再按低风险顺序迁移 UI：

1. 先迁移纯 UI 面板。
2. 再迁移辅助 manager。
3. 最后才评估是否迁移渲染核心。

### Color 推荐目标结构

```text
src/features/color/
  module.py
  facade.py
  contracts.py
  adapters.py
  ui/
    canvas.py
    grading_panel.py
    adjustment_panel.py
    hsl_curves_panel.py
    lut_panel.py
    mask_panel.py
    qualifier_panel.py
    soft_clip_panel.py
    raw_panel.py
    plugin_panel.py
  core/
    color_session.py
    color_output.py
    color_manager.py
    lut_exporter.py
  shaders/
    color_convert.glsl
    blur.glsl
    keyer.glsl
    morphology.glsl
```

暂时保留在 `src/core`：

```text
src/core/image_engine.py
src/core/shader_compiler.py
src/core/async_renderer.py
src/core/gl_backend.py
src/core/layer.py
```

这些可以后续改名为：

```text
src/shared/gpu/
src/shared/render/
```

但不要在下一轮做。

### Color 验收清单

每次碰 Color 相关文件都必须验证：

- 打开图片。
- 调整 Lift/Gamma/Gain。
- 调整曲线。
- 调整 HSL 曲线。
- 应用 LUT。
- 启用 Mask。
- 启用 Qualifier。
- 启用 Soft Clip。
- 多图层混合。
- 保存并重开。
- 导出。
- Composite 开启时能收到 Color output。

---

## 12. SaveRouter 修复计划

### 当前问题

`src/core/save_router.py` 顶层 import：

```python
from src.core.edit.project import ColorStackData
```

这会导致：

- 删除 Edit core 时 SaveRouter import 失败。
- Color 保存被 Edit 模型绑住。

### 修复方向

把 `ColorStackData` 移出 Edit，放到 shared project schema：

```text
src/shared/project/color_stack.py
```

或者：

```text
src/features/color/contracts.py
```

推荐放 shared：

```text
src/shared/project/color_stack.py
```

因为 Edit、Color、SaveRouter 都会用。

### 迁移步骤

| 步骤 | 动作 | 验证 |
|---|---|---|
| S.1 | 新建 `src/shared/project/color_stack.py` | compileall |
| S.2 | 从 `src/core/edit/project.py` 移出或复制 `ColorStackData` | unit tests |
| S.3 | `src/core/edit/project.py` 从 shared re-export | 旧 import 不坏 |
| S.4 | `SaveRouter` 改 import shared | compileall |
| S.5 | Color/Edit 保存测试 | 手工 |

### SaveRouter handler 化下一步

不要让 module handler 空转。每个 handler 必须实际保存：

```python
router.register_handler("color", color_save_handler.save)
router.register_handler("edit", edit_save_handler.save)
router.register_handler("composite", composite_save_handler.save)
router.register_handler("deliver", deliver_save_handler.save)
```

短期可以继续让 handler 调用原私有方法，但注册点要清楚：

```python
save_router.register_handler("color", save_router._save_from_color)
```

等功能模块稳定后，再把实现挪到各自 module。

---

## 13. MainWindow 拆分计划

### 当前问题

`src/ui/main_window.py` 仍然是超级文件，包含：

- App shell
- Feature flag 判断
- 页面切换
- Dock layout
- 菜单创建
- Edit 项目逻辑
- Color 调色逻辑
- Composite 逻辑
- Deliver 导出逻辑
- SaveRouter 回调

这会持续阻碍模块化。

### 拆分目标

迁移到：

```text
src/app/main_window_shell.py
src/app/menu_manager.py
src/app/layout_manager.py
src/app/page_manager.py
```

但不要马上把 MainWindow 文件整体移动。先抽纯逻辑。

### 拆分步骤

| 步骤 | 抽出内容 | 新文件 | 风险 |
|---|---|---|---|
| MW.1 | feature/page 可用性判断 | `src/app/page_manager.py` | 低 |
| MW.2 | page switcher 生成 | `src/app/page_manager.py` | 中 |
| MW.3 | dock visibility 工具函数 | `src/app/layout_manager.py` | 低 |
| MW.4 | menu action 添加 helper | `src/app/menu_manager.py` | 低 |
| MW.5 | Composite 页面逻辑 | `features/composite/adapters.py` | 中 |
| MW.6 | Edit 页面逻辑 | `features/edit/adapters.py` | 高 |
| MW.7 | Deliver 导出入口 | `features/deliver/adapters.py` | 中 |

### 第一轮只做这些

```python
def add_dock_action_if_present(menu, dock):
    if dock is not None:
        menu.addAction(dock.toggleViewAction())

def hide_docks(docks):
    for dock in docks:
        if dock is not None:
            dock.setVisible(False)
```

这两个 helper 可以先放在 `MainWindow`，后续再抽到 `layout_manager.py`。

---

## 14. 删除能力目标定义

### 什么叫“可禁用”

Feature flag 关闭后：

- 不显示页面。
- 不创建 UI。
- 不注册菜单。
- 不注册保存 handler。
- 不进入页面 hook。
- 不执行该功能专属导出。

但文件仍然可以存在。

### 什么叫“可删除”

功能目录被 rename 或删除后：

- App 仍能 import。
- App 仍能启动。
- 其它功能仍能使用。
- 旧工程中的该功能数据不会被误删。

### 删除测试顺序

Composite：

```bash
mv src/composite src/composite.disabled
TOPOS_FEATURE_COMPOSITE=0 python3 -m compileall src
mv src/composite.disabled src/composite
```

Deliver，迁移后：

```bash
mv src/features/deliver src/features/deliver.disabled
TOPOS_FEATURE_DELIVER=0 python3 -m compileall src
mv src/features/deliver.disabled src/features/deliver
```

Edit，迁移后：

```bash
mv src/features/edit src/features/edit.disabled
TOPOS_FEATURE_EDIT=0 python3 -m compileall src
mv src/features/edit.disabled src/features/edit
```

Color 不做删除目标。

---

## 15. 下一轮执行顺序建议

### 第一批：必须先修

| 顺序 | 任务 | 原因 |
|---|---|---|
| 1 | 修 `AudioMetadata.bytes_per_sample` 测试失败 | 让测试基线干净 |
| 2 | 处理 `topos.db` 是否保留 | 避免提交污染 |
| 3 | 修 Edit/Deliver 关闭崩溃 | 当前 P1 阻断 |
| 4 | 修 page switcher 过滤 | 禁用功能不能显示 |
| 5 | 修默认布局对 None dock 的访问 | 启动稳定 |

### 第二批：Registry 真接管

| 顺序 | 任务 | 原因 |
|---|---|---|
| 6 | 让 MainWindow 消费 PageDescriptor | Registry 才有意义 |
| 7 | EditModule/DeliverModule 返回真实 page descriptor | 模块真正注册页面 |
| 8 | CompositeModule 接管 CompositePage 创建 | Composite 真模块化 |
| 9 | SaveRouter handler 注册补齐 | 保存路由模块化 |

### 第三批：目录迁移

| 顺序 | 任务 | 原因 |
|---|---|---|
| 10 | 迁移 Composite 到 `features/composite` | 最独立，风险最低 |
| 11 | 迁移 Deliver 到 `features/deliver` | 中等风险 |
| 12 | 抽 Edit shared 依赖 | 为迁移 Edit 准备 |
| 13 | 迁移 Edit 到 `features/edit` | 高风险，后做 |
| 14 | 整理 Color UI 到 `features/color/ui` | 核心稳定后再做 |

---

## 16. 每阶段固定验收命令

基础：

```bash
python3 -m compileall src
.venv/bin/pytest tests/test_config_manager.py -q
.venv/bin/pytest tests/test_nodes/test_time_nodes.py tests/test_nodes/test_color_nodes.py -q
```

媒体：

```bash
.venv/bin/pytest tests/media -q
```

关闭功能启动：

```bash
QT_QPA_PLATFORM=offscreen TOPOS_FEATURE_EDIT=0 .venv/bin/python - <<'PY'
import sys
from PyQt6.QtWidgets import QApplication
from src.shared.feature_flags import load_feature_flags
from src.utils.config_manager import ConfigManager
from src.ui.main_window import MainWindow
app = QApplication(sys.argv)
window = MainWindow(feature_flags=load_feature_flags(ConfigManager()))
print("OK")
window.close()
app.quit()
PY
```

全部关闭可选功能：

```bash
QT_QPA_PLATFORM=offscreen TOPOS_FEATURE_EDIT=0 TOPOS_FEATURE_DELIVER=0 TOPOS_FEATURE_COMPOSITE=0 .venv/bin/python - <<'PY'
import sys
from PyQt6.QtWidgets import QApplication
from src.shared.feature_flags import load_feature_flags
from src.utils.config_manager import ConfigManager
from src.ui.main_window import MainWindow
app = QApplication(sys.argv)
window = MainWindow(feature_flags=load_feature_flags(ConfigManager()))
print("OK")
window.close()
app.quit()
PY
```

Import 边界检查：

```bash
rg -n "from src.composite|import src.composite" src --glob '*.py'
rg -n "from src.core.edit|from src.ui.edit|import src.core.edit|import src.ui.edit" src --glob '*.py'
rg -n "from src.ui.deliver_page|import src.ui.deliver_page" src --glob '*.py'
```

---

## 17. 人工验收清单

### Feature flag 人工验收

Edit 关闭：

- 启动不崩。
- Page switcher 没有 Edit。
- Window menu 没有 Edit 专属面板。
- 打开单图 Color 正常。
- 保存 Color 正常。

Deliver 关闭：

- 启动不崩。
- Page switcher 没有 Deliver。
- 导出入口如果属于 Deliver，应隐藏或提示不可用。
- Color 保存不受影响。

Composite 关闭：

- 启动不崩。
- Page switcher 没有 Composite。
- Edit 中 Open in Composite 禁用或提示不可用。
- 打开含 Composite 数据工程不崩。

### Color 回归

每次涉及 `src/core/image_engine.py`、`shader_compiler.py`、`async_renderer.py`、`layer.py`、Color UI 时必须执行：

- 打开图片。
- 调整 Lift/Gamma/Gain。
- 调整 Curves。
- 调整 HSL Curves。
- 应用 LUT。
- 应用 Mask。
- 应用 Qualifier。
- 应用 Soft Clip。
- 保存并重开。
- 导出。

---

## 18. 当前项目目录混乱的具体整理建议

### 18.1 短期不要追求一步到位

当前虽然有 `src/features`，但真实代码还在旧位置。  
这是正常过渡状态，但必须避免长期停在这里。

建议每次迁移一个模块，迁移后保留旧路径兼容层。

### 18.2 推荐最终归属表

| 当前路径 | 最终路径 | 优先级 |
|---|---|---|
| `src/composite/module.py` | `src/features/composite/module.py` | 高 |
| `src/composite/node_engine.py` | `src/features/composite/core/node_engine.py` | 高 |
| `src/composite/composite_renderer.py` | `src/features/composite/core/composite_renderer.py` | 高 |
| `src/composite/composite_export_renderer.py` | `src/features/composite/core/composite_export_renderer.py` | 高 |
| `src/composite/evaluators/` | `src/features/composite/core/evaluators/` | 高 |
| `src/composite/nodes/` | `src/features/composite/nodes/` | 高 |
| `src/composite/ui/` | `src/features/composite/ui/` | 高 |
| `src/ui/deliver_page.py` | `src/features/deliver/ui/deliver_page.py` | 中 |
| `src/core/export/` | `src/features/deliver/core/` 或 `src/shared/export/` | 中 |
| `src/core/edit/` | `src/features/edit/core/` | 中高 |
| `src/ui/edit/` | `src/features/edit/ui/` | 中高 |
| `src/ui/widgets/color_wheel.py` | `src/features/color/ui/grading_panel.py` | 中 |
| `src/ui/hsl_curves_panel.py` | `src/features/color/ui/hsl_curves_panel.py` | 中 |
| `src/ui/lut_panel.py` | `src/features/color/ui/lut_panel.py` | 中 |
| `src/ui/mask_panel.py` | `src/features/color/ui/mask_panel.py` | 中 |
| `src/core/color_session.py` | `src/features/color/core/color_session.py` | 中 |
| `src/core/color_output.py` | `src/features/color/core/color_output.py` | 中 |
| `src/core/shaders/` | `src/features/color/shaders/` 或 `src/shared/gpu/shaders/` | 中 |
| `src/core/image_engine.py` | 暂留 `src/core/` | 低，最后 |
| `src/core/async_renderer.py` | 暂留 `src/core/` 或未来 `src/shared/gpu/` | 低，最后 |
| `src/core/shader_compiler.py` | 暂留 `src/core/` 或未来 `src/features/color/core/` | 低，最后 |
| `src/core/gl_backend.py` | 暂留 `src/core/` 或未来 `src/shared/gpu/` | 低，最后 |
| `src/core/layer.py` | 暂留 `src/core/` 或未来 `src/features/color/core/` | 低，最后 |

---

## 19. 给后续 AI 的执行提示

后续 AI 执行时，必须先读：

```text
AGENTS.md
.clinerules
docs/feature_modularization_implementation_plan_2026-05-24.md
docs/modularization_remediation_plan_2026-05-25.md
```

每次只做一个小阶段。  
不要同时：

- 修 P1 崩溃
- 移动目录
- 改 SaveRouter
- 改 Color 渲染

最推荐的下一次任务 prompt：

```text
请按照 docs/modularization_remediation_plan_2026-05-25.md 的阶段 0 和阶段 1 执行：
1. 修复 AudioMetadata.bytes_per_sample 测试失败。
2. 修复 Edit/Deliver/Composite feature flag 关闭后的 MainWindow 初始化崩溃。
3. 不移动目录。
4. 不修改调色核心渲染文件。
5. 跑 compileall、test_config_manager、test_time_nodes、test_color_nodes、tests/media/test_audio_metadata.py。
6. 用 offscreen 脚本验证 TOPOS_FEATURE_EDIT=0 TOPOS_FEATURE_DELIVER=0 TOPOS_FEATURE_COMPOSITE=0 可构造 MainWindow。
```

---

## 20. 完成标准

本修复计划完成后，至少应达到：

1. `TOPOS_FEATURE_EDIT=0` 可启动。
2. `TOPOS_FEATURE_DELIVER=0` 可启动。
3. `TOPOS_FEATURE_COMPOSITE=0` 可启动。
4. 三个可选功能全部关闭也可启动。
5. Page switcher 不显示关闭功能。
6. Window menu 不显示关闭功能的 dock action。
7. `tests/media` 全绿或无新增失败。
8. Registry 至少真正接管 Composite 页面创建。
9. SaveRouter 不再顶层依赖 Edit-only 模型。
10. Composite 文件可以开始迁移到 `features/composite`。

---

## 21. 重要提醒

目录整齐不是第一目标。  
第一目标是：

```text
关掉某功能，不崩。
删掉某功能，其它功能还能跑。
调色主线不变。
```

当前最应该先修的是 feature flag 关闭后的空指针和无条件实例化问题。  
这些不修，后面的目录迁移会越搬越乱。

