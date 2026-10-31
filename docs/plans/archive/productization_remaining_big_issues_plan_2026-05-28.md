# Topos 产品化剩余结构性问题优化计划（2026-05-28）

## 0. 当前结论

本轮审查后，产品化拆分已经完成了大部分基础工程：

- `topos-studio / topos-color / topos-edit / topos-composite` 四个产品预设已经存在。
- Feature flags、PanelRegistry、菜单/快捷键/偏好过滤、物理缺失测试、打包边界配置都有基础实现。
- `Window > Panels` 已经有按 Workspace / Color / Edit / Composite / Deliver / Advanced 分组的注册模型。
- `LayoutStore` / `LayoutProfile` 已经有独立数据模型和测试。
- `.topos` manifest 已经加入 `last_saved_by / required_features / optional_features / present_modules` 等字段。

但还有三类结构性尾项没有真正闭环，属于“继续做会更稳，不能只靠当前测试认为完全产品化”的部分：

| 优先级 | 问题 | 当前状态 | 风险 |
|---|---|---|---|
| P0 | 页面布局保存仍主要走旧 `page_layouts` | `LayoutStore` 已实现，但真实 dock 保存/恢复尚未接管 | 四个产品共用布局时仍可能互相污染 |
| P0 | `.topos` 模块数据保留策略未完全落地 | manifest 字段已加，但保存链路仍以旧区域写入为主 | `topos-color` 打开 studio 项目后保存，未来可能误伤未知模块数据 |
| P1 | 打包脚本还不是实际 bundle 产物 | `build_product.sh` 目前验证配置，没有真正裁剪源码/生成 app | “能分开启动”不等于“发布包物理隔离” |
| P1 | PanelRegistry 与真实 Dock 状态还缺集成校验 | 注册和菜单已有，但还需检查每个 panel_id 对应实际 dock_attr | 后续新增面板时可能菜单项存在但点击无效 |

---

## 1. Phase A：把 LayoutStore 接入真实布局保存/恢复

### 1.1 目标

让布局保存从旧的：

```text
config["page_layouts"][page_name] = Qt saveState hex
```

逐步迁移到：

```text
config["layout_profiles"][product_mode][page_id] = LayoutProfile
```

关键目标：

- 布局按 `product_mode + page_id` 隔离。
- 保存的可见面板用稳定 `panel_id`，不是临时 dock 名或页面显示名。
- 恢复布局时，如果某个产品没有对应 panel，必须跳过而不是崩。
- 旧 `page_layouts` 继续兼容读取，但新保存优先写 `layout_profiles`。

### 1.2 涉及文件

- `src/app/controllers/dock_layout_controller.py`
- `src/app/controllers/page_controller.py`
- `src/app/controllers/file_controller.py`
- `src/shared/layout/profile.py`
- `src/shared/layout/store.py`
- `src/shared/panels/registry.py`
- `src/shared/config/config_manager.py`
- `tests/test_layout_store.py`
- 新增：`tests/test_layout_integration.py`

### 1.3 实施步骤

1. 在 `MainWindow.__init__` 中创建 `self.layout_store = LayoutStore(self.config_manager)`。
2. 在 `initialize_page_layouts()` 开头调用 `self.layout_store.migrate_legacy_page_layouts()`。
3. 新增 `_current_product_mode()`，统一使用 `src.shared.feature_flags.get_product_mode()`。
4. 新增 `_get_registered_panel_ids()`，从 `get_panel_registry().get_all()` 得到当前可用 panel id。
5. 在切换页面前保存当前布局：
   - 继续调用 `self.saveState()` 保存 Qt dock state。
   - 读取当前可见 docks，转换成 `visible_panels`。
   - 写入 `LayoutProfile(product_mode, page_id, visible_panels, qt_state_hex)`。
6. 在切换页面后恢复目标页面布局：
   - `profile = layout_store.get_or_default(product_mode, page_id, enabled_features, registered_panel_ids)`。
   - 如果 `profile.qt_state_hex` 存在，先尝试 `restoreState()`。
   - 如果 `restoreState()` 失败，落回现有默认布局逻辑。
   - 对 `profile.visible_panels` 中已注册的 panel 显示，对缺失 panel 跳过。
7. 保留旧 `self.page_layouts` 作为过渡缓存，但新写入以 `LayoutStore` 为准。
8. 增加一个“重置当前产品布局”的方法，只清当前 `product_mode` 的布局，不清其它产品。

### 1.4 必测项

```bash
.venv/bin/python -m pytest tests/test_layout_store.py tests/test_layout_integration.py -q
TOPOS_PRODUCT_MODE=color .venv/bin/python -m pytest tests/test_layout_integration.py -q
TOPOS_PRODUCT_MODE=studio .venv/bin/python -m pytest tests/test_layout_integration.py -q
```

测试必须覆盖：

- `color` 与 `studio` 的 `Color` 页面布局互不覆盖。
- 旧 `page_layouts` 能迁移到当前产品的 `layout_profiles`。
- 缺失 `edit/composite` 时，布局恢复跳过对应 panel。
- `restoreState()` 返回失败时不影响启动。
- `Window > Panels` 菜单中每个 panel action 都有可解析 dock。

### 1.5 典型坑

- Qt `saveState()` 依赖 `QDockWidget.objectName()`，后续改 objectName 会破坏旧布局。
- `dock_properties` 同时被 Edit / Composite 场景复用，必须通过 `panel_id` 区分产品语义。
- `workspace.*` 面板现在 feature_id 归在 color 下，未来如果做纯素材管理产品，需要改成 `library` 或 app-level。
- 不能让布局恢复失败影响工程文件打开。

---

## 2. Phase B：让 `.topos` 真正支持模块段落保留

### 2.1 目标

让四个产品打开同一个 `.topos` 时遵守这个规则：

```text
当前产品能理解的模块：可以读写
当前产品不能理解的模块：必须保留原始数据，不主动删除
```

例如：

- `topos-color` 打开 studio 工程，只编辑 color 数据。
- 保存时必须保留原来的 edit/composite/deliver 数据。
- `topos-edit` 打开 composite 工程，不能因为自己不加载 composite 而清掉 composite graph。

### 2.2 涉及文件

- `src/shared/project/topos_project_io.py`
- `src/shared/project/save_router.py`
- `src/shared/project/topos_project_read_service.py`
- `src/shared/project/manifest.py`
- `src/shared/project/topos_project.py`
- `tests/test_save_router.py`
- `tests/test_project_document.py`
- 新增：`tests/test_project_module_preservation.py`

### 2.3 推荐实现

新增一个“工程包原始段落缓存”：

```python
class ProjectArchivePayload:
    manifest: dict
    project_json: dict
    module_payloads: dict[str, dict]
    ui_payloads: dict[str, dict]
    unknown_files: dict[str, bytes]
```

加载 `.topos` 时：

- 读取所有已知模块数据。
- 把未知 JSON / 未知文件也放入 raw payload。
- 在 `ToposProject` 或 read service 中保留这份 raw payload。

保存 `.topos` 时：

- 先以 raw payload 为底。
- 当前产品拥有的模块覆盖对应段落。
- 当前产品不拥有的模块原样写回。
- 更新 manifest 的 `last_saved_by / present_modules / optional_features`。

### 2.4 必测项

```bash
.venv/bin/python -m pytest tests/test_project_module_preservation.py -q
.venv/bin/python -m pytest tests/test_save_router.py tests/test_project_document.py -q
```

测试必须覆盖：

- color-only 保存不删除 `edit/timeline_sequences.json`。
- edit-only 保存不删除 `composite/graphs.json`。
- composite-only 保存不删除 edit timeline。
- 未知 `modules/future_feature.json` 被原样保留。
- manifest `last_saved_by` 会更新为当前产品。
- manifest `present_modules` 与包内实际模块一致。

### 2.5 典型坑

- 不要把“未启用模块”当成“用户想删除模块”。
- 不要在 `ToposProjectIO.save()` 中默认重建一个空 ZIP 后只写当前模块。
- 旧工程缺少新 manifest 字段时要有默认值。
- JSON 反序列化失败的未知文件应该按 bytes 原样保留，不要吞掉。

---

## 3. Phase C：把打包边界从配置验证升级为真实产物验证

### 3.1 目标

当前 `scripts/build_product.sh` 已经能输出 include/exclude 配置，但还没有真正生成四个独立产物。下一步要完成：

- 每个产品有独立 entry point。
- 每个产品 bundle 内只包含允许的 feature 目录。
- bundle 启动时环境变量与产品名一致。
- `verify_product_bundle.py` 能检查真实 dist 目录，而不是只检查源码树。

### 3.2 涉及文件

- `pyproject.toml`
- `apps/topos_*/main.py`
- `src/products/packaging.py`
- `scripts/build_product.sh`
- `scripts/verify_product_bundle.py`
- 新增：`packaging/pyinstaller/topos-*.spec`
- 新增：`.github/workflows/product-matrix.yml`

### 3.3 实施步骤

1. 决定打包工具：PyInstaller 或 briefcase 二选一，先用 PyInstaller 更快。
2. 为四个产品生成四个 spec：
   - `topos-studio.spec`
   - `topos-color.spec`
   - `topos-edit.spec`
   - `topos-composite.spec`
3. `build_product.sh` 根据 `get_packaging_config()` 生成 exclude-module 参数。
4. 构建后调用：
   ```bash
   scripts/verify_product_bundle.py topos-color dist/topos-color
   ```
5. CI 矩阵至少跑：
   - `build_product.sh topos-color`
   - `build_product.sh topos-edit`
   - `build_product.sh topos-composite`
   - `build_product.sh topos-studio`
6. 在 CI 中跑物理缺失：
   ```bash
   ./scripts/check_physical_missing_features.sh --product color
   ./scripts/check_physical_missing_features.sh --product edit
   ./scripts/check_physical_missing_features.sh --product composite
   ```

### 3.4 必测项

- `dist/topos-color/src/features/edit` 不存在。
- `dist/topos-color/src/features/composite` 不存在。
- `dist/topos-edit/src/features/composite` 不存在。
- `dist/topos-composite/src/features/edit` 不存在。
- 四个入口模块都能 import。
- `TOPOS_PRODUCT_MODE=topos-color` 与 `TOPOS_PRODUCT_MODE=color` 行为一致。

---

## 4. Phase D：PanelRegistry 与真实 Dock 完整性护栏

### 4.1 目标

每个注册的 `PanelDescriptor` 都必须能找到真实 `QDockWidget`，否则 `Window > Panels` 中会出现点击无效或不出现的问题。

### 4.2 实施步骤

1. 新增 `tests/test_panel_dock_integrity.py`。
2. 用轻量 fake MainWindow 或 Qt test 模式创建 docks。
3. 注册所有可用模块的 panels。
4. 对每个 descriptor 检查：
   - `panel_id` 唯一。
   - `dock_attr` 非空。
   - `hasattr(window, dock_attr)`。
   - dock 有 `toggleViewAction()`。
   - `feature_id` 在产品矩阵中存在。
5. 对 color 模式检查：
   - 没有 Edit / Composite category。
   - Deliver category 是否按当前产品策略显示。

### 4.3 必测项

```bash
TOPOS_PRODUCT_MODE=color .venv/bin/python -m pytest tests/test_panel_registry.py tests/test_panel_dock_integrity.py -q
TOPOS_PRODUCT_MODE=studio .venv/bin/python -m pytest tests/test_panel_registry.py tests/test_panel_dock_integrity.py -q
```

---

## 5. 推荐执行顺序

| 顺序 | 任务 | 原因 |
|---|---|---|
| 1 | Phase A LayoutStore 接入真实 dock 保存/恢复 | 用户最容易感知，且会影响四产品布局隔离 |
| 2 | Phase D Panel-Dock 完整性测试 | 能保护 Phase A，不然 layout 会保存不存在的 panel |
| 3 | Phase B `.topos` 模块保留 | 这是四产品共用工程文件的核心安全线 |
| 4 | Phase C 真实 bundle | 前三项稳定后再做发布包，不然 bundle 只是把 bug 固化 |

---

## 6. 给下一轮 AI 的执行 Prompt

```text
请在 Topos Color 项目中继续完成产品化结构性尾项。

项目路径：
/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02

技术栈：
PyQt6 + ModernGL + GLSL

请先阅读：
docs/productization_remaining_big_issues_plan_2026-05-28.md
docs/productization_tail_implementation_plan_2026-05-28.md

第一优先级只做 Phase A：
1. 把 LayoutStore / LayoutProfile 接入真实 dock layout 保存/恢复。
2. 保持旧 page_layouts 兼容，不要一次性删除旧逻辑。
3. 布局必须按 product_mode + page_id 隔离。
4. 恢复布局时跳过当前产品不可用或未注册 panel。
5. 增加 tests/test_layout_integration.py。

约束：
- 不要重写整个 dock 系统。
- 不要破坏现有 Color/Edit/Composite/Deliver 页面默认布局。
- 不要让 restoreState 失败影响启动。
- 如果发现需要大改，先更新计划文档。

验证：
.venv/bin/python -m pytest tests/test_layout_store.py tests/test_layout_integration.py -q
./scripts/check_physical_missing_features.sh edit composite deliver
```

---

## 7. 完成标准

所有结构性尾项完成后，应满足：

- `topos-color` / `topos-studio` 的同名页面布局互不污染。
- 旧用户配置中的 `page_layouts` 能被迁移。
- 缺失 feature 目录时布局恢复不崩。
- `.topos` 在跨产品保存时保留未知模块数据。
- 四个产品能生成真实 bundle，并通过 `verify_product_bundle.py`。
- `Window > Panels` 中每个面板动作都能定位到真实 dock。
- 物理缺失测试脚本在 macOS 默认 Bash 和带空格项目路径下可运行。
