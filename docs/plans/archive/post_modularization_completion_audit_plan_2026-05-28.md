# Topos Color 模块拆分完成度复查与尾项优化计划（2026-05-28）

## 1. 总结结论

你这轮计划表的完成度已经明显提高：剪辑、调色、合成、导出已经从“页面级开关”推进到“模块可贡献菜单、偏好页、快捷键、多语言”的阶段。

当前可以判断为：

- `topos-studio` 作为完整软件的模块化基础已经基本成立。
- `topos-color` 作为调色主产品，关闭 Edit / Composite 后的核心路径已经能跑通。
- 菜单系统已经有 `ActionRegistry` + `MenuBuilder`，不再只是登记描述符。
- 偏好设置已经支持模块页贡献，Color / Edit / Deliver / Composite 都有注册入口。
- 快捷键已经有 `feature_id` 过滤，模块也开始注册自己的快捷键。
- i18n 已在 `FeatureRegistry.register_module()` 阶段加载模块翻译。

但还不能说“随便删除任意模块目录就一定完整可打包”。现在已经能做到部分物理缺失验证，剩下的问题主要是产品化收口、测试矩阵和少量 app 层深层 import 清理。

## 2. 本次复查直接修复的问题

### 2.1 PreferencesRegistry 排序错误

问题：

偏好设置页注册后按 `(category, order, page_id)` 排序，导致侧边栏顺序变成：

```text
Advanced, Appearance, Color & Processing, Export, ...
```

这会破坏原本用户熟悉的顺序。

修复：

改为按 `(order, category, page_id)` 排序。

文件：

- `src/shared/preferences/__init__.py`
- `tests/test_menu_contributions.py`

验证：

模块化 PreferencesDialog 现在显示：

```text
General, Appearance, Color & Processing, Performance, Export, Shortcuts, File & Storage, Advanced
```

### 2.2 Edit 目录物理缺失时 MainWindow 导入崩溃

问题：

临时移走 `src/features/edit` 并设置：

```bash
TOPOS_DISABLE_FEATURES=edit,composite,deliver
```

再导入 `src.app.main_window` 会失败：

```text
ModuleNotFoundError: No module named 'src.features.edit'
```

根因：

`src/app/controllers/edit_controller.py` 顶部直接导入 `src.features.edit.commands_facade`。

修复：

将该 facade import 改为可选导入。Edit 模块缺失时，MainWindow 仍可导入；如果误调用 Edit 命令，会得到清晰的：

```text
RuntimeError: Edit feature is unavailable
```

文件：

- `src/app/controllers/edit_controller.py`

## 3. 本次验证结果

### 3.1 编译检查

```bash
.venv/bin/python -m compileall -q src tests
```

结果：通过。

### 3.2 Feature / Menu / Preferences 注册测试

```bash
.venv/bin/python -m pytest \
  tests/test_feature_flags.py \
  tests/test_feature_flags_registry.py \
  tests/test_menu_contributions.py -q
```

结果：81 passed。

### 3.3 Color 产品模式测试

```bash
TOPOS_PRODUCT_MODE=color .venv/bin/python -m pytest \
  tests/test_page_context.py \
  tests/test_feature_flags.py \
  tests/test_feature_flags_registry.py \
  tests/test_menu_contributions.py -q
```

结果：99 passed, 1 warning。

### 3.4 模块化 PreferencesDialog 冒烟测试

场景：

- Color enabled
- Deliver enabled
- Edit disabled
- Composite disabled
- 传入 Color / Deliver 模块偏好页

结果：

- 页面顺序正确。
- 不显示 `Video Playback`。
- 显示 `Export`。
- `_apply_settings()` 正常。
- 模块页 `restore_defaults()` 正常。

### 3.5 物理删除模块目录测试

场景：

临时移走：

```text
src/features/edit
src/features/composite
src/features/deliver
```

并设置：

```bash
TOPOS_DISABLE_FEATURES=edit,composite,deliver
```

验证：

```bash
import src.app.main_window
```

结果：通过。

继续运行：

```bash
TOPOS_DISABLE_FEATURES=edit,composite,deliver .venv/bin/python -m pytest \
  tests/test_page_context.py \
  tests/test_feature_flags_registry.py -q
```

结果：72 passed, 1 warning。

## 4. 当前完成度判断

### 4.1 已经基本完成的部分

#### 菜单拆分

状态：基本完成。

依据：

- `src/shared/actions/ActionRegistry` 存在。
- `src/app/menu/menu_builder.py` 存在。
- `ColorModule / EditModule / CompositeModule / DeliverModule` 都有 `register_menu_actions()`。
- `MenuToolbarMixin.setup_menus()` 会调用 `_register_module_menu_actions()` 和 `_build_module_menus()`。
- 模块菜单项会按 `feature_id` 过滤。

剩余风险：

- File 菜单仍混合 app-level 手写项和模块贡献项，这是合理过渡状态，但要继续防止重复 QAction。
- 更深层的 timeline / node graph 操作菜单还没有全部迁移成模块贡献。

#### 偏好设置拆分

状态：基本完成第一版。

依据：

- `src/shared/preferences/PreferencesRegistry` 存在。
- `PreferencePageDescriptor` 支持 `page_object`。
- `PreferencesDialog` 支持 `module_pages`。
- `ColorProcessingPreferencePage`、`ExportPreferencePage`、`EditPreferencePage`、`CompositePreferencePage` 已存在。
- load / apply / restore defaults 已能委托给模块页。

剩余风险：

- `PreferencesDialog` 仍有大量 app-level 代码，后续可以继续瘦身。
- Composite preference 目前还是 placeholder。
- 模块页异常现在会被吞掉，后续应至少记录 warning，避免真实设置保存失败时无声失败。

#### 快捷键拆分

状态：已接入，仍需扩展覆盖。

依据：

- `ShortcutAction.feature_id` 存在。
- `ShortcutManager.get_actions(enabled_features=...)` 支持过滤。
- `ShortcutDialog` 支持 enabled feature filtering。
- Edit / Composite / Deliver module 已有 `register_shortcuts()`。

剩余风险：

- 现在模块快捷键主要覆盖项目级动作。
- 时间线剪切、节点图、合成查看器等更细快捷键后续还要继续迁移。

#### 多语言拆分

状态：已接入生命周期。

依据：

- `I18N.load_module_translations()` 存在。
- 各模块有 `register_i18n()`。
- `FeatureRegistry.register_module()` 会调用模块 i18n 注册。
- `FeatureRegistry.reload_i18n()` 已提供统一重载入口。

剩余风险：

- 翻译 key 冲突没有检测。
- 切语言后所有已打开 UI 的动态刷新仍依赖现有窗口逻辑，模块页不会自动局部刷新。

## 5. 剩余大问题与优化计划

### P0. 建立“物理缺失模块”CI 矩阵

目标：

以后你真的拆 `topos-color / topos-edit / topos-composite` 时，不靠人工临时 `mv` 目录验证。

实施步骤：

1. 增加脚本或 pytest fixture，临时隐藏可选模块目录并自动恢复。
2. 新增测试组：
   - color-only flags：只关闭 feature，不删除目录。
   - color-only physical-missing：删除 Edit / Composite / Deliver 目录。
   - deliver-without-edit：保留 Deliver，删除 Edit，验证导出降级。
3. 明确哪些测试不适用于物理缺失场景，例如直接导入 `src.features.edit.module` 的测试。
4. 在 CI 中单独跑：

```bash
TOPOS_DISABLE_FEATURES=edit,composite,deliver pytest tests/test_page_context.py tests/test_feature_flags_registry.py
```

验收标准：

- color-only physical-missing 场景下 `src.app.main_window` 可导入。
- app-level tests 不直接依赖被删除模块。
- 不出现未恢复临时目录的脏工作区。

### P1. 清理 app 层剩余深层 import

目标：

让 `src/app` 不再随手 import `src.features.edit.*`、`src.features.composite.*`、`src.features.deliver.*` 深层实现。

当前仍可搜索到的典型位置：

- `src/app/main_window.py`
- `src/app/controllers/file_controller.py`
- `src/app/controllers/edit_controller.py`
- `src/app/controllers/composite_controller.py`
- `src/app/controllers/deliver_controller.py`
- `src/app/controllers/dock_layout_controller.py`

实施步骤：

1. 先保留所有运行时行为。
2. 把 Edit 深层类型访问收拢到 `src/features/edit/facade.py` 或已有 adapter。
3. 把 Composite adapter / export provider 访问收拢到 `CompositeModule` 或 runtime facade。
4. Deliver UI 和 export provider 访问收拢到 `DeliverModule`。
5. 每迁移一类 import，就跑一次：

```bash
rg -n "from src\\.features\\.(edit|composite|deliver)" src/app src/shared
```

验收标准：

- `src/app` 只允许 import feature module/facade/adapter，不直接 import core/ui 深层文件。
- 禁用模块时不导入深层实现。
- 物理删除可选模块时 app shell 仍可导入。

### P1. 扩展模块菜单和快捷键覆盖

目标：

不只项目级动作模块化，Timeline / Node Graph / Deliver source 等动作也由模块贡献。

实施步骤：

1. 列出当前手写 QAction 和快捷键。
2. 标注 owner：
   - app shell
   - color
   - edit
   - composite
   - deliver
3. 对 edit/composite/deliver owner 的动作迁移到模块 `register_menu_actions()` / `register_shortcuts()`。
4. 给每个 action 定义稳定 action id，例如：
   - `edit.timeline.cut`
   - `edit.timeline.ripple_delete`
   - `composite.node.add`
   - `composite.node.bypass`
   - `deliver.export.current_source`
5. 增加测试：
   - Edit disabled 时不出现 edit action。
   - Composite disabled 时不出现 composite action。
   - Deliver disabled 时不出现 export action。

验收标准：

- ShortcutDialog 中 disabled feature 的动作不显示。
- MenuBuilder 不生成 disabled feature 的 QAction。
- 不存在重复 shortcut id。

### P1. 明确四个产品的打包边界

建议产品定义：

```text
topos-studio      = color + edit + composite + deliver
topos-color       = color + lightweight deliver
topos-edit        = edit + deliver + minimal color viewer/transform
topos-composite   = composite + deliver + minimal color viewer/transform
```

注意：

`topos-color` 不建议完全删除 Deliver。调色软件至少需要“导出当前调色结果”。可以删除 timeline export，但保留 still/image export。

实施步骤：

1. 为每个产品写一个 product preset。
2. 把“功能 flag”和“打包 include/exclude”分开。
3. 每个产品建立独立入口：
   - `src/products/topos_studio.py`
   - `src/products/topos_color.py`
   - `src/products/topos_edit.py`
   - `src/products/topos_composite.py`
4. 每个入口只注册自己允许的 module。
5. 打包脚本按产品裁剪资源、locale、插件和模块目录。

验收标准：

- 每个产品入口可启动。
- 每个产品只显示自己的页面、菜单、快捷键、偏好设置。
- 裁剪目录后仍能通过 smoke test。

### P2. 给模块偏好页增加真实单元测试

目标：

避免以后某个模块页缺 import、缺默认值、控件 key 改名导致 Preferences 打不开。

实施步骤：

1. 给每个模块页建独立测试：
   - create_widget
   - load
   - apply
   - restore_defaults
2. 使用 fake ConfigManager，避免测试写真实 config。
3. 覆盖 feature filtering：
   - Edit disabled 时不创建 Edit page。
   - Deliver disabled 时不创建 Export page。

验收标准：

- 每个模块偏好页都有测试。
- 新增配置项没有对应 load/apply/defaults 时测试失败。

## 6. 最终判断

如果只按“现在能不能继续开发一个以调色为主、剪辑/合成为加分项的软件”来判断：已经可以。

如果按“未来能不能拆成四个独立软件”来判断：方向正确，核心基础已经搭好，但还需要完成上面 P0/P1 的产品化收口。

现在最重要的下一步不是再大规模重构，而是把“物理缺失模块测试矩阵”和“app 层 deep import 清单”固定下来。这样 AI 后续继续帮你拆时，不会把已经拆好的边界又悄悄打穿。
