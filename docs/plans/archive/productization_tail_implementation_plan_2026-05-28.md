# Topos 产品化尾项收尾实施计划（2026-05-28）

## 0. 目标与结论

本计划用于把当前已经完成的模块拆分，继续收口成可长期维护、可测试、可打包的产品矩阵：

```text
topos-studio      # 剪辑 + 调色 + 合成 + 导出，完整软件
topos-color       # 调色主产品，剪辑/合成可物理缺失
topos-edit        # 剪辑产品，保留必要调色/导出基础能力
topos-composite   # 合成产品，保留必要调色/导出基础能力
```

当前项目已经具备基础：

- `src/shared/feature_flags.py` 已有 `PRODUCT_MODE_PRESETS`。
- `src/main.py` 已支持 `--product color|studio|edit|composite`。
- `src/app/feature_registry.py` 已能注册模块生命周期。
- `src/app/menu/menu_builder.py` 与 `src/shared/actions` 已支持模块贡献菜单。
- `src/shared/preferences` 与 `src/app/ui/preferences_dialog.py` 已支持模块贡献偏好页。
- `src/shared/ui/shortcut_manager.py` 已支持按 `feature_id` 过滤快捷键。
- `scripts/check_physical_missing_features.sh` 与 `scripts/test_physical_missing.sh` 已经提供物理缺失测试雏形。

剩余工作不是重新拆架构，而是把边界、测试、入口、打包和覆盖率固定下来。

## 1. 总体执行顺序

建议按下面顺序交给 AI 分阶段完成，不要一次性全改：

| 阶段 | 优先级 | 主题 | 目标 |
|---|---:|---|---|
| Phase 1 | P0 | 物理缺失模块 CI 矩阵 | 禁用/移除模块时，启动、注册、菜单、偏好、快捷键测试稳定 |
| Phase 2 | P0 | app 层深导入清理 | `src/app` 不再硬依赖可选模块内部实现 |
| Phase 3 | P1 | 菜单与快捷键覆盖扩展 | 时间线、节点图、导出等模块动作统一登记、过滤、测试 |
| Phase 3A | P1 | `Window > Panels` 面板分组 | 面板按 Workspace / Color / Edit / Composite / Deliver 分类，用户更容易识别 |
| Phase 3B | P1 | 工程文件与页面布局优化 | `.topos` schema、模块数据、页面布局按产品/页面/模块隔离 |
| Phase 4 | P1 | 产品入口固化 | 提供 `topos-studio/topos-color/topos-edit/topos-composite` 明确启动入口 |
| Phase 5 | P1 | 打包边界与产物检查 | 明确四个产品各自包含/排除的模块、资源、测试与 smoke 验证 |
| Phase 6 | P2 | 长期维护护栏 | 文档、CI、边界扫描、异常日志与回归矩阵 |

每个阶段完成后都必须跑对应验收命令，避免“看起来拆完了，但打包时崩”的问题。

## 2. Phase 1：物理缺失模块 CI 矩阵

### 2.1 目标

把“临时移动目录手测”变成稳定可重复的测试矩阵，验证禁用模块不只是隐藏 UI，而是真的可以在源代码缺失时启动核心外壳。

### 2.2 涉及文件

- `scripts/check_physical_missing_features.sh`
- `scripts/test_physical_missing.sh`
- `pytest.ini`
- `tests/conftest_module_isolation.py`
- `tests/test_feature_flags.py`
- `tests/test_feature_flags_registry.py`
- `tests/test_page_context.py`
- `tests/test_menu_contributions.py`
- 可新增：`tests/test_product_physical_missing.py`
- 可新增：`.github/workflows/product-matrix.yml`（如果项目开始使用 GitHub Actions）

### 2.3 推荐矩阵

| 场景 | 环境变量 | 物理移除目录 | 必跑测试 |
|---|---|---|---|
| studio 完整版 | `TOPOS_PRODUCT_MODE=studio` | 无 | feature/menu/page/preferences/shortcut |
| color 逻辑禁用 | `TOPOS_PRODUCT_MODE=color` | 无 | feature/menu/page/preferences/shortcut |
| color 物理缺失 | `TOPOS_PRODUCT_MODE=color TOPOS_DISABLE_FEATURES=edit,composite` | `edit`, `composite` | import/main_window/page/menu/preferences |
| color 极限精简 | `TOPOS_PRODUCT_MODE=color TOPOS_DISABLE_FEATURES=edit,composite,deliver` | `edit`, `composite`, `deliver` | import/main_window/page/menu/preferences |
| edit 产品 | `TOPOS_PRODUCT_MODE=edit` | `composite` | edit/page/menu/shortcut/deliver |
| composite 产品 | `TOPOS_PRODUCT_MODE=composite` | `edit` | composite/page/menu/shortcut/deliver |
| deliver 无 edit | `TOPOS_DISABLE_FEATURES=edit` | `edit` | deliver 降级、无 timeline export 崩溃 |

### 2.4 实施步骤

1. 统一两个现有脚本。
   - 保留功能更完整的 `scripts/check_physical_missing_features.sh`。
   - 将 `scripts/test_physical_missing.sh` 改成调用 `check_physical_missing_features.sh` 的矩阵脚本，避免两套移动/恢复逻辑。
   - 所有临时目录统一放在项目内 `_backup_features/` 或系统临时目录，并确保 `trap` 永远恢复。

2. 新增 pytest 级别的 smoke 测试。
   - 测试 `load_feature_flags()` 在四个产品模式下返回预期 flags。
   - 测试 `FeatureRegistry` 在缺失模块时不注册该模块。
   - 测试 `src.app.main_window` 在禁用模块时可 import。
   - 测试 `MenuBuilder` 不生成禁用模块菜单项。
   - 测试 `PreferencesDialog` 不显示禁用模块页面。
   - 测试 `ShortcutManager.get_actions(enabled_features=...)` 不返回禁用模块快捷键。

3. 给测试打 marker。
   - 物理缺失测试统一标记 `@pytest.mark.physical_missing`。
   - 产品模式测试统一标记 `@pytest.mark.color_product` / `studio_product`。
   - 直接依赖模块源码的测试继续标记 `edit_core` / `composite_core` / `deliver_core`。

4. 补 CI 命令。
   - 如果暂时没有 CI，先写入 `docs/test_acceptance_matrix_2026-05-27.md` 或新建 `docs/product_test_matrix.md`。
   - 如果添加 GitHub Actions，使用 matrix 跑 `product_mode` 与 `missing_features`。

### 2.5 验收命令

```bash
.venv/bin/python -m compileall -q src tests
```

```bash
.venv/bin/python -m pytest \
  tests/test_feature_flags.py \
  tests/test_feature_flags_registry.py \
  tests/test_page_context.py \
  tests/test_menu_contributions.py -q
```

```bash
TOPOS_PRODUCT_MODE=color .venv/bin/python -m pytest \
  tests/test_feature_flags.py \
  tests/test_feature_flags_registry.py \
  tests/test_page_context.py \
  tests/test_menu_contributions.py -q
```

```bash
./scripts/check_physical_missing_features.sh edit composite deliver
```

```bash
./scripts/check_physical_missing_features.sh edit
./scripts/check_physical_missing_features.sh composite
```

### 2.6 易踩坑

- 移动目录后 Python import cache 可能残留，测试 fixture 需要清理 `sys.modules` 中的 `src.features.edit/composite/deliver`。
- `trap` 失败会导致源码目录留在备份位置，脚本必须在失败时也恢复。
- 测试不能直接 import 已被物理移除的模块；这类测试必须用 marker 排除。
- 物理缺失测试不能并行跑同一个工作区，否则目录移动会互相干扰。
- `color` 是核心功能，不能被禁用；测试应该明确验证这一点。

## 3. Phase 2：继续清理 app 层深导入

### 3.1 目标

让 `src/app` 只依赖模块公开接口、registry、facade 或 adapter，不直接依赖可选模块内部文件。这样物理删除 `src/features/edit` 或 `src/features/composite` 时，app 外壳不会在 import 阶段崩溃。

### 3.2 检查命令

```bash
rg -n "from src\\.features\\.(edit|composite|deliver)|import src\\.features\\.(edit|composite|deliver)" src/app src/shared
```

### 3.3 边界规则

允许：

- `src/app/module_loader.py` 中可选导入模块入口，例如 `src.features.edit.module`。
- `src/main.py` 启动注册阶段的可选导入，但必须被 feature flag 与 `try/except ImportError` 包住。
- `src/features/*/module.py` 内部导入本模块实现。
- 模块公开 facade，例如 `src.features.edit.commands_facade`，但 app 层使用时必须可选降级。

不允许：

- `src/app/controllers/*` 顶层直接导入 `src.features.edit.core.*`。
- `src/app/ui/*` 顶层直接导入 `src.features.composite.ui.*`。
- `src/shared/*` 依赖具体产品模块。
- 禁用模块后，只是打开首页也触发可选模块内部 import。

### 3.4 实施步骤

1. 先扫描所有 app/shared 深导入。
2. 按风险排序：
   - import 阶段必崩的顶层导入优先。
   - 只在函数内部按需导入的次之。
   - 类型注解导入改为 `if TYPE_CHECKING:`。
3. 为每个模块建立或补齐 facade。
   - Edit：命令、timeline export、timeline state、selection 等公开接口。
   - Composite：node graph、export provider、viewer/page 创建公开接口。
   - Deliver：导出页、导出服务、依赖 edit timeline 的降级接口。
4. app 层只调用 registry/context/facade。
5. 缺失模块时返回清晰错误：
   - UI 上隐藏入口。
   - 后端误调用时抛 `RuntimeError("Edit feature is unavailable")` 一类明确异常。

### 3.5 推荐新增测试

- `test_app_layer_has_no_forbidden_feature_imports`
  - 扫描 `src/app` 与 `src/shared`。
  - 白名单只允许 `src/main.py`、`src/app/module_loader.py`、必要的可选 facade。

- `test_main_window_imports_when_optional_features_are_missing`
  - 临时隐藏 `edit/composite/deliver`。
  - 设置 `TOPOS_DISABLE_FEATURES=edit,composite,deliver`。
  - 验证 `import src.app.main_window` 通过。

- `test_deliver_without_edit_degrades_cleanly`
  - 禁用或缺失 Edit。
  - Deliver 如果可用，不能 import 崩溃。
  - 与 timeline 相关的 export 入口应隐藏、禁用或提示不可用。

### 3.6 易踩坑

- `TYPE_CHECKING` 可以解决类型注解导入，但运行时仍需要真实 fallback。
- `except ImportError` 不要吞掉模块内部真实 bug。更稳妥做法是只捕获缺失目标模块，或至少记录完整 warning。
- `src/features/deliver/ui/deliver_page.py` 目前与 Edit timeline export 有关系，要重点检查 Deliver 无 Edit 的降级路径。
- 不要把所有 facade 变成巨型公共垃圾桶；每个 facade 只暴露跨模块真正需要的能力。

## 4. Phase 3：扩展菜单与快捷键覆盖

### 4.1 目标

让模块相关菜单、上下文菜单、工具栏动作、快捷键都走统一贡献机制，并能被产品模式过滤。

### 4.2 现有基础

- `src/shared/actions/__init__.py`
- `src/app/menu/menu_builder.py`
- `src/app/controllers/menu_toolbar_controller.py`
- `src/features/color/module.py`
- `src/features/edit/module.py`
- `src/features/composite/module.py`
- `src/features/deliver/module.py`
- `src/shared/ui/shortcut_manager.py`
- `tests/test_menu_contributions.py`

### 4.3 需要覆盖的动作清单

Color：

- 调色工具面板切换。
- LUT 导入/导出。
- 节点/图层/遮罩相关命令。
- scopes、viewer overlays、comparison mode。

Edit：

- timeline cut/delete/split/ripple。
- insert/overwrite/append。
- selection、trim、snap、blade。
- timeline zoom、fit、marker。

Composite：

- add node、delete node、duplicate node。
- node graph search。
- connect/disconnect。
- group/ungroup。
- viewer A/B、fit、zoom、cache、render preview。

Deliver：

- export current frame。
- export timeline。
- export composite graph。
- preset management。
- queue start/stop。

### 4.4 实施步骤

1. 先列出当前手写 QAction。
   ```bash
   rg -n "QAction\\(|addAction\\(|addMenu\\(|setShortcut\\(" src/app src/features
   ```

2. 给动作定义统一 ID。
   - 格式建议：`color.lut.import`、`edit.timeline.split`、`composite.node.add`、`deliver.queue.start`。
   - 不要用 UI 文案当 ID。

3. 模块在 `register_menu_actions()` 中注册顶层菜单动作。

4. 模块在 `register_shortcuts()` 中注册默认快捷键。
   - 每个 action 必须有 `feature_id`。
   - 跨模块共用动作必须归到 app/shared 或明确 owner。

5. 对上下文菜单分两类处理：
   - 简单动作：直接走 `ActionRegistry`。
   - 需要运行时上下文的动作：注册 descriptor，触发时由当前 page/controller 查询上下文。

6. 给冲突检测加测试。
   - action ID 不重复。
   - shortcut 默认键不冲突，或有明确允许冲突表。
   - 禁用 feature 后快捷键列表不出现该 feature 的动作。

### 4.5 验收命令

```bash
.venv/bin/python -m pytest tests/test_menu_contributions.py -q
```

```bash
TOPOS_PRODUCT_MODE=color .venv/bin/python -m pytest tests/test_menu_contributions.py -q
```

建议新增后再跑：

```bash
.venv/bin/python -m pytest -m "modular or color_product or studio_product" -q
```

### 4.6 易踩坑

- 同一个 QAction 不能被多个菜单重复持有后出现状态不同步。
- 快捷键冲突不一定立刻报错，但用户会觉得随机失效。
- 页面级动作需要 active page context，否则在错误页面也可能触发。
- 菜单隐藏和动作禁用是两回事：产品不存在时隐藏；功能暂不可用时禁用。
- 文案要走 i18n key，不要把中文/英文硬编码在 descriptor 深处。

## 5. Phase 3A：`Window > Panels` 面板分组与 Panel Registry

### 5.1 结论

建议整理，而且应该尽快整理。

原因：

- 用户在 `Window > Panels` 下能直接看出某个面板属于 Color / Edit / Composite / Deliver 哪个功能区。
- 产品模式过滤会更自然：`topos-color` 不显示 Edit / Composite 面板组。
- 后续删除模块目录时，不会因为菜单里还引用某个 dock 而崩溃。
- 页面布局保存可以按 panel_id 管理，而不是到处硬编码 `dock_xxx`。

### 5.2 推荐菜单结构

建议最终变成：

```text
Window
  Panels
    Workspace
      Viewport
      Library
      Files
      Info
      Presets
    Color
      Layers
      Color Wheel
      Curves
      HSL Curves
      Mixer
      Warper
      Blur/Sharpen
      Film Grain
      Masks
      Qualifier
      3D LUT
      Soft Clip
      Camera Raw
      Scopes
      Crop & Transform
      Effects
    Edit
      Media Pool
      Source Viewer
      Program Viewer
      Timeline
      Properties
      Edit Effects
      Audio Mixer
    Composite
      Node Graph
      Properties
      Performance
    Deliver
      Export
      Deliver
    Advanced
      Script
```

分类可微调，但原则必须固定：

- Workspace：跨产品通用面板。
- Color：只服务调色页面或调色栈的面板。
- Edit：只服务剪辑、时间线、素材池的面板。
- Composite：只服务节点合成的面板。
- Deliver：导出、渲染队列、交付预设。
- Advanced：脚本、诊断、开发者工具，不要混进主要工作流。

### 5.3 推荐新增 PanelDescriptor

新增共享注册结构：

```text
src/shared/panels/
  __init__.py
  registry.py
```

建议 descriptor 字段：

```python
PanelDescriptor(
    panel_id="color.color_wheel",
    title_key="panel.color_wheel",
    fallback_title="Color Wheel",
    feature_id="color",
    category="Color",
    page_ids=["Color"],
    dock_attr="dockGrading",
    order=300,
    default_visible=True,
)
```

字段含义：

- `panel_id`：稳定 ID，不随 UI 文案变化。
- `feature_id`：用于产品模式过滤。
- `category`：用于 `Window > Panels` 分组。
- `page_ids`：该面板主要归属哪些页面。
- `dock_attr`：当前 MainWindow 上对应的 `QDockWidget` 属性名，过渡期可用。
- `order`：同组排序。
- `default_visible`：默认布局时是否显示。

### 5.4 实施步骤

1. 先做静态映射，不急着移动 dock 创建逻辑。
   - 当前 dock 仍由 `DockLayoutControllerMixin.setup_docks()` 创建。
   - 新增一个 `_register_panel_descriptors()`，把已存在的 dock 注册进 PanelRegistry。

2. 改造 `Window > Panels` 构建逻辑。
   - 当前代码在 `src/app/controllers/menu_toolbar_controller.py` 里逐个 `panels_menu.addAction(self.dock_xxx.toggleViewAction())`。
   - 改成按 PanelRegistry 分组生成子菜单。
   - 找不到 dock 的 descriptor 直接跳过并记录 warning。

3. 模块开始贡献自己的 panel descriptor。
   - ColorModule 注册 Color 组。
   - EditModule 注册 Edit 组。
   - CompositeModule 注册 Composite 组。
   - DeliverModule 注册 Deliver 组。

4. 给每个 panel descriptor 加 `feature_id` 与 `page_ids`。
   - 禁用 feature 时不显示该面板。
   - 页面不可用时不显示该页面专属面板。

5. 补 i18n。
   - 新增 `menu.panels.workspace`、`menu.panels.color`、`menu.panels.edit` 等 key。
   - 面板 title 仍复用现有 `panel.*` key。

### 5.5 验收命令

```bash
.venv/bin/python -m pytest tests/test_menu_contributions.py -q
```

建议新增：

```bash
.venv/bin/python -m pytest tests/test_panel_registry.py -q
```

```bash
TOPOS_PRODUCT_MODE=color .venv/bin/python -m pytest \
  tests/test_panel_registry.py tests/test_menu_contributions.py -q
```

### 5.6 必测点

- `Window > Panels > Color` 在 studio/color 模式显示。
- `Window > Panels > Edit` 在 color 模式不显示。
- `Window > Panels > Composite` 在 color/edit 模式不显示。
- 物理删除 `src/features/edit` 时，不生成 Edit 面板菜单。
- 每个可见 panel action 都能 toggle 对应 dock。
- 没有重复 `panel_id`。
- 没有 descriptor 指向不存在的 `dock_attr`。

### 5.7 易踩坑

- `dock_properties` 同时被 Edit 和 Composite 复用，不能简单归死到一个模块。建议用两个 descriptor 指向同一 dock，但通过 active page 决定显示文案，或拆成 `edit.properties` 与 `composite.properties` 的逻辑面板。
- `dock_export` 和 `dock_deliver` 可能都与导出相关，命名要区分：一个是 Export 面板，一个是 Deliver 页面/队列面板。
- `Layers` 在当前实现更偏 Color，但未来如果 Edit/Composite 也有 layer 概念，不要复用同一个 `panel_id`。
- 不要在菜单构建时创建 dock；dock 生命周期仍应在布局控制器或模块页面创建阶段完成。

## 6. Phase 3B：工程文件与页面布局保存优化

### 6.1 结论

需要优化。

模块拆分后，工程文件和布局保存不应该继续只按“当前窗口状态”粗暴保存。否则会出现：

- `topos-color` 打开 studio 项目时读到 Edit/Composite 数据，不知道该忽略、隐藏还是报错。
- 用户在 Color 页面保存的布局，把 Edit/Composite dock 状态一起带进去。
- 删除某个模块后，旧布局里引用了不存在的 dock，restore 时异常或布局错乱。
- 四个产品共用配置时，`topos-color` 和 `topos-studio` 的布局互相污染。

### 6.2 工程文件目标

`.topos` 应该成为统一工程包，但内部结构要模块化、版本化、可降级。

建议目标结构：

```text
project.topos
  manifest.json
  project.json
  modules/
    color.json
    edit.json
    composite.json
    deliver.json
  ui/
    layout.json
    view_state.json
  assets/
  cache/
```

如果当前 `.topos` 已是 ZIP 包，则优先在现有 `src/shared/project/topos_project.py` 与 `ToposProjectIO` 上演进，不要另起一种格式。

### 6.3 manifest 建议字段

```json
{
  "schema_version": 2,
  "created_by": "topos-studio",
  "last_saved_by": "topos-color",
  "app_version": "0.x",
  "required_features": ["color"],
  "optional_features": ["edit", "composite", "deliver"],
  "present_modules": ["color", "edit", "composite"],
  "active_page": "Color",
  "project_id": "uuid",
  "migration_history": []
}
```

规则：

- `required_features` 缺失时，必须提示不能完整打开。
- `optional_features` 缺失时，可以打开，但对应页面隐藏或只读。
- `present_modules` 表示工程里实际包含哪些模块数据。
- `last_saved_by` 用于判断是 color-only 保存过，还是 studio 保存过。
- schema 必须支持 migration。

### 6.4 模块数据所有权

建议明确：

| 数据 | Owner | 保存位置 |
|---|---|---|
| 素材引用、项目基础信息 | shared/project | `project.json` |
| 调色栈、图层、LUT、mask、raw 参数 | color | `modules/color.json` |
| 素材池、时间线、clip、track、edit ui state | edit | `modules/edit.json` |
| 节点图、composition、viewer state、cache hints | composite | `modules/composite.json` |
| 导出预设、队列草稿、交付目标 | deliver | `modules/deliver.json` |
| 页面布局、可见面板、浮动位置 | app/shell | `ui/layout.json` |

### 6.5 打开跨产品工程的规则

| 当前产品 | 打开工程含 Edit | 打开工程含 Composite | 行为 |
|---|---:|---:|---|
| `topos-studio` | 是 | 是 | 完整打开 |
| `topos-color` | 是 | 是 | 只加载 Color/共享素材，保留但不修改 Edit/Composite 数据 |
| `topos-edit` | 是 | 是 | 加载 Edit/Color，Composite 数据保留但不主动编辑 |
| `topos-composite` | 是 | 是 | 加载 Composite/Color，Edit 数据保留但不主动编辑 |

重要原则：

- color-only 保存 studio 项目时，不能删除未知模块数据。
- 没有模块代码时，仍要能保留该模块的原始 JSON payload。
- 如果用户修改并保存工程，只更新当前产品拥有的模块 section。

### 6.6 页面布局保存目标

建议把布局分成三层：

1. User default layout
   - 存在本机 config。
   - 按 `product_mode + page_id` 分开。
   - 用于新项目和 Reset Layout。

2. Project workspace layout
   - 存在 `.topos/ui/layout.json`。
   - 记录该项目上次使用的页面、面板可见性、dock 状态。
   - 打开项目时可以恢复，但要过滤当前产品不存在的 panel。

3. Session layout
   - 当前运行期间临时状态。
   - 切页面时使用，关闭软件后可以选择写回 user/project。

### 6.7 layout.json 建议结构

```json
{
  "schema_version": 1,
  "product_mode": "studio",
  "active_page": "Color",
  "layouts": {
    "Color": {
      "qt_state_hex": "...",
      "visible_panels": ["workspace.viewport", "color.color_wheel"],
      "active_panel_tabs": {
        "bottom": "color.color_wheel"
      }
    },
    "Edit": {
      "qt_state_hex": "...",
      "visible_panels": ["edit.timeline", "edit.media_pool"]
    }
  },
  "floating_panels": {
    "color.scopes": {"x": 100, "y": 100, "w": 500, "h": 320}
  }
}
```

### 6.8 实施步骤

1. 不要马上删除现有 `page_layouts`。
   - 先新增 `LayoutProfile` / `LayoutStore`，读取新格式。
   - 旧 config 的 `page_layouts` 作为 migration 输入。

2. 把布局 key 从页面文案改成稳定 page_id。
   - 避免中文/英文切换后找不到布局。
   - 例如 `color`, `edit`, `composite`, `deliver`，不要依赖 `"Color"`。

3. 保存前过滤 panel。
   - 只保存 PanelRegistry 中存在且当前产品可用的 panel。
   - 对缺失模块 panel 只保留原始 payload，不参与 restore。

4. restore 时做兼容。
   - 旧布局有不存在的 dock：跳过，不报 fatal。
   - 旧布局缺少新 panel：使用默认位置。
   - Qt `restoreState()` 失败：回退到该产品该页面默认布局。

5. 工程保存集成。
   - `SaveRouter` 保存工程时，把当前 project workspace layout 写入 `.topos/ui/layout.json`。
   - 单素材 `.topos` 与 EditProject `.topos` 都走同一 layout 写入策略。

### 6.9 推荐新增测试

- `test_project_manifest_preserves_unknown_module_payload`
- `test_color_product_opening_studio_project_preserves_edit_and_composite_sections`
- `test_project_save_updates_only_owned_module_sections`
- `test_layout_store_uses_product_and_page_keys`
- `test_layout_restore_skips_missing_panels`
- `test_legacy_page_layouts_migrate_to_layout_profiles`
- `test_window_panels_menu_groups_match_panel_registry`

### 6.10 验收命令

```bash
.venv/bin/python -m pytest tests/test_project_document.py tests/test_save_router.py -q
```

```bash
.venv/bin/python -m pytest tests/test_panel_registry.py tests/test_menu_contributions.py -q
```

```bash
TOPOS_PRODUCT_MODE=color .venv/bin/python -m pytest \
  tests/test_project_document.py tests/test_save_router.py tests/test_panel_registry.py -q
```

### 6.11 易踩坑

- 不要把 UI layout 当成工程必需数据；布局坏了不能导致工程打不开。
- 不要在 color-only 保存时清空 Edit/Composite section，这是跨产品工程最危险的坑。
- Qt `saveState()` 是二进制状态，必须外面再包一层可理解的 JSON 元数据。
- dock objectName 一旦改变，旧布局会失效；所以需要稳定 `panel_id` 做兼容层。
- 工程 schema migration 必须可重复执行，不能跑一次后第二次坏。

## 7. Phase 4：固化四个产品启动入口

### 7.1 目标

不要只依赖用户记住：

```bash
python -m src.main --product color
```

而是提供明确入口：

```bash
topos-studio
topos-color
topos-edit
topos-composite
```

### 7.2 推荐实现

新增：

```text
src/products/
  __init__.py
  studio.py
  color.py
  edit.py
  composite.py
```

每个文件只做一件事：设置产品模式，然后调用 `src.main.main()`。

示例：

```python
import os
from src.main import main


def run() -> int | None:
    os.environ.setdefault("TOPOS_PRODUCT_MODE", "color")
    return main()
```

同时保留 `src/main.py --product`，方便开发和测试。

### 7.3 产品预设建议

当前预设：

```python
"color": {"library": True, "color": True, "edit": False, "composite": False, "deliver": True}
"studio": {"library": True, "color": True, "edit": True, "composite": True, "deliver": True}
"edit": {"library": True, "color": True, "edit": True, "composite": False, "deliver": True}
"composite": {"library": True, "color": True, "edit": False, "composite": True, "deliver": True}
```

建议暂时保持。原因：

- Color 是核心引擎，不建议独立关闭。
- Edit 和 Composite 可能仍需要基础色彩管理、viewer、导出。
- Deliver 作为导出能力可以默认保留，但要支持无 Edit 降级。

### 7.4 验收命令

```bash
TOPOS_PRODUCT_MODE=studio .venv/bin/python -m src.main --test-startup
TOPOS_PRODUCT_MODE=color .venv/bin/python -m src.main --test-startup
TOPOS_PRODUCT_MODE=edit .venv/bin/python -m src.main --test-startup
TOPOS_PRODUCT_MODE=composite .venv/bin/python -m src.main --test-startup
```

如果新增 `src/products/*`：

```bash
.venv/bin/python -c "from src.products.color import run; print(callable(run))"
.venv/bin/python -c "from src.products.studio import run; print(callable(run))"
.venv/bin/python -c "from src.products.edit import run; print(callable(run))"
.venv/bin/python -c "from src.products.composite import run; print(callable(run))"
```

### 7.5 易踩坑

- `src.main.main()` 当前内部会 `sys.exit(app.exec())`，入口函数如果要作为 console script，最好逐步改成返回 exit code，最后统一 `sys.exit(main())`。
- `--product` 和 `TOPOS_PRODUCT_MODE` 优先级要明确。当前逻辑是 env 已存在时不被 CLI 覆盖，是否符合预期需要确认。
- 四个入口不要复制完整启动代码，否则以后会分叉。
- 产品名称、窗口标题、配置目录、日志文件名后续可能也要产品化。

## 8. Phase 5：打包边界与产物检查

### 8.1 目标

让每个产品打包时只包含自己需要的模块和资源，并且缺失模块不会被 hidden import 拉回包内。

### 8.2 当前缺口

项目当前未看到：

- `pyproject.toml`
- `setup.py`
- `setup.cfg`
- `packaging/`
- `.github/workflows/`

因此打包阶段需要先建立最小 packaging 结构。

### 8.3 推荐文件

```text
pyproject.toml
packaging/
  pyinstaller/
    topos-studio.spec
    topos-color.spec
    topos-edit.spec
    topos-composite.spec
  product_manifest.py
scripts/
  build_product.sh
  verify_product_bundle.py
```

### 8.4 product manifest

建议定义一个中心 manifest，而不是在多个 spec 文件里手写分支。

示例字段：

```python
PRODUCTS = {
    "topos-color": {
        "mode": "color",
        "entry": "src.products.color:run",
        "include_features": ["color", "deliver"],
        "exclude_features": ["edit", "composite"],
        "include_resources": ["src/features/color/locales", "src/features/color/shaders"],
    },
}
```

### 8.5 打包检查内容

每个产物必须检查：

- 能启动到 `--test-startup`。
- bundle 内不包含被排除的 feature 目录。
- 菜单不显示被排除 feature 的页面入口。
- 偏好设置不显示被排除 feature 的页面。
- 快捷键列表不显示被排除 feature 的动作。
- i18n 资源存在。
- shader / LUT / plugin 资源路径可解析。

### 8.6 验收命令

开发阶段：

```bash
.venv/bin/python -m src.main --product color --test-startup
.venv/bin/python -m src.main --product studio --test-startup
.venv/bin/python -m src.main --product edit --test-startup
.venv/bin/python -m src.main --product composite --test-startup
```

打包后建议：

```bash
./scripts/build_product.sh topos-color
./scripts/verify_product_bundle.py dist/topos-color --expect color --forbid edit composite
```

### 8.7 易踩坑

- PyInstaller hidden imports 可能把被排除模块重新打进包里。
- Qt/PyQt 插件、OpenGL、WebEngine、shader、locale 都需要资源路径策略。
- `sys.path.append(...)` 在开发环境能工作，但打包后路径可能不同。
- 动态 import 和资源扫描要走统一 helper，不能各模块各写一套。
- macOS app bundle、Windows exe、Linux AppImage 的资源目录结构不同。

## 9. Phase 6：长期维护护栏

### 9.1 必须保留的边界测试

建议新增或强化：

- `test_product_mode_presets_are_explicit`
- `test_color_cannot_be_disabled`
- `test_app_layer_has_no_forbidden_feature_imports`
- `test_optional_module_registration_survives_missing_directory`
- `test_menu_actions_filtered_by_product_mode`
- `test_shortcuts_filtered_by_product_mode`
- `test_preferences_filtered_by_product_mode`
- `test_window_panels_grouped_by_feature`
- `test_panel_registry_filters_by_product_mode`
- `test_project_manifest_preserves_unknown_module_payload`
- `test_layout_restore_skips_missing_panels`
- `test_i18n_modules_have_unique_keys`
- `test_product_entrypoints_set_expected_mode`

### 9.2 文档维护

建议维护五份轻量文档：

- `docs/product_test_matrix.md`
  - 记录每个产品模式必须跑哪些测试。
- `docs/product_packaging_boundaries.md`
  - 记录每个产品包含/排除哪些 feature 与资源。
- `docs/module_boundary_rules.md`
  - 记录 app/shared/features 之间允许和禁止的 import。
- `docs/panel_registry_rules.md`
  - 记录 panel 分组、panel_id 命名、Window 菜单生成规则。
- `docs/project_document_schema.md`
  - 记录 `.topos` manifest、模块 section、layout section 和 migration 规则。

### 9.3 Review 清单

每次 AI 改模块边界后，必须检查：

- 有没有新增 app/shared 到 optional feature 的深导入。
- 有没有新增没有 `feature_id` 的菜单动作或快捷键。
- 有没有新增没有 `feature_id` / `panel_id` 的 dock 面板。
- 有没有新增不受产品模式过滤的偏好页。
- `Window > Panels` 是否仍按 Workspace / Color / Edit / Composite / Deliver 分组。
- color-only 保存 studio 工程时，是否保留未知模块 section。
- 页面布局 restore 是否能跳过缺失 panel。
- 有没有新增硬编码中文/英文菜单文案。
- 物理缺失脚本是否还能恢复目录。
- `TOPOS_PRODUCT_MODE=color` 是否还能通过基础测试。

## 10. 推荐给下一轮 AI 的执行 Prompt

可以直接复制下面 prompt 给 AI：

```text
请按照 docs/productization_tail_implementation_plan_2026-05-28.md 执行 Phase 1。

项目背景：
- PyQt6 + ModernGL + GLSL 的调色/剪辑/合成软件。
- 当前目标是产品化收尾，不是重构所有功能。
- 必须遵守模块边界：src/app 与 src/shared 不应顶层硬依赖可选模块内部实现。
- 不能回退用户已有改动。

任务：
1. 统一 scripts/check_physical_missing_features.sh 与 scripts/test_physical_missing.sh 的职责。
2. 新增或补齐物理缺失模块测试。
3. 给测试加合适 pytest marker。
4. 跑计划文档中的 Phase 1 验收命令。
5. 如果发现小问题直接修复；如果是大结构问题，写入新的优化计划文档。

输出：
- 修改文件清单。
- 测试结果。
- 尚未完成的风险。
```

Phase 1 稳定后，再给 AI：

```text
请按照 docs/productization_tail_implementation_plan_2026-05-28.md 执行 Phase 2。
重点清理 src/app 与 src/shared 对 src.features.edit/composite/deliver 的深导入。
要求先用 rg 扫描，建立白名单，再逐个改为 registry/facade/optional import。
每修一个边界都补测试，最后跑物理缺失矩阵。
```

Window / Panels 和工程布局优化可以给 AI：

```text
请按照 docs/productization_tail_implementation_plan_2026-05-28.md 执行 Phase 3A 和 Phase 3B。

任务：
1. 为 Window > Panels 设计并实现 PanelDescriptor / PanelRegistry。
2. 将现有 Panels 菜单改为 Workspace / Color / Edit / Composite / Deliver / Advanced 分组。
3. 面板必须按 feature_id 和 product mode 过滤。
4. 设计 LayoutStore / LayoutProfile，使布局按 product_mode + page_id + panel_id 保存和恢复。
5. 设计 .topos 工程 manifest 与模块 section 规则，保证 color-only 保存不会删除 Edit/Composite 数据。
6. 添加测试覆盖 panel 分组、禁用模块过滤、缺失 panel 跳过、旧 page_layouts migration。

约束：
- 不要重写整个 dock 系统，先用 descriptor 包住现有 QDockWidget。
- 不要破坏现有 .topos 兼容性。
- 如果遇到大结构问题，先写新的细化计划文档，不要一次性硬改。
```

## 11. 最终完成标准

只有同时满足以下条件，才可以说产品化收尾真正完成：

- `topos-color` 在 `edit/composite` 目录物理缺失时仍能启动基础窗口。
- `topos-studio` 全功能仍能通过模块注册、菜单、偏好、快捷键测试。
- `topos-edit` 不依赖 Composite 源码也能启动。
- `topos-composite` 不依赖 Edit 源码也能启动。
- Deliver 在无 Edit 时有明确降级路径，不会 import 崩溃。
- app/shared 层没有未白名单的 optional feature 深导入。
- 菜单、偏好页、快捷键都按 product mode 过滤。
- `Window > Panels` 已按 Workspace / Color / Edit / Composite / Deliver / Advanced 分组。
- 每个 dock 面板都有稳定 `panel_id`、`feature_id`、`page_ids` 和产品过滤规则。
- `.topos` 工程文件有 manifest、schema_version、模块 section 和 migration 策略。
- color-only 打开并保存 studio 工程时，不会删除 Edit/Composite 原始数据。
- 页面布局按 `product_mode + page_id + panel_id` 保存，缺失模块 panel 能跳过并回退默认布局。
- 四个产品都有独立入口或 console script。
- 打包 manifest/spec 明确每个产品包含与排除的模块资源。
- 物理缺失测试能一键跑完并自动恢复目录。

完成到这里后，后续把三个能力拆成独立软件就不再是“赌架构”，而是按 manifest 与入口组合产品。
