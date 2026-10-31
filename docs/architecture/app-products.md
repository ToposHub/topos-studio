# App 集成与产品形态（app-products）

- 撰写日期：2026-09-28（计划 04）
- 范围：`src/main.py`、`src/app/`（app_context / module_loader / feature_registry / main_window / controllers / menu / ui / integrations）、`src/products/`、`apps/<product>/main.py`、`src/shared/panels/`、`src/shared/layout/`
- 性质：本层是"产品形态决策 + 装配 + 控制器"层。它不实现渲染（见 color-core / shared-gpu 分册），而是决定**哪些功能进入产品、以什么顺序装配、页面如何切换、面板/菜单如何注册**。
- 所有行号基于 2026-09-28 dev 分支源码；审查佐证引自 `docs/audit/2026-09-28/batches/`（batch-018 ~ batch-041，覆盖 src/app 全域）。

---

## 1. 总览：三层裁剪模型

Topos 用**同一份代码库**产出四个产品。裁剪发生在三层，各有独立机制：

| 层 | 机制 | 数据源 | 生效时机 |
|----|------|--------|----------|
| ① 运行时功能注册 | `FeatureFlags` 门控 `module_loader._FEATURE_MODULES` 注册表 | `TOPOS_PRODUCT_MODE` → `PRODUCT_MODE_PRESETS` | `main.py` 启动时 |
| ② 构建时打包 | `src/products/packaging.py` 输出 include/exclude 目录与插件清单 | `ProductPreset.resolved_enabled` | 打包脚本 |
| ③ 页面/面板可见性 | `_composite_available` / `_edit_available` / `_deliver_available` + `PanelRegistry.feature_id` 过滤 | MainWindow 启动探测 + feature_flags | `create_panels()` / `setup_docks()` / 菜单构建 |

关键点：**Color 永远启用**。`FeatureFlags.is_enabled("color")` 硬编码返回 True（src/shared/feature_flags.py:177-183），`load_feature_flags` Step 6 会把任何 `color=False` 强制改回 True（:280-283）；`src/products/__init__.py` 中 `ALWAYS_ENABLED = {color, library}`（:36）。Color 页面本体不走 FeatureRegistry 工厂（`ColorModule.register_pages` 返回空表，页面由 MainWindow 直接创建，见 src/features/color/module.py:58-73）。

---

## 2. 四产品：预设、入口与差异

### 2.1 产品预设（src/products/__init__.py:71-103）

| 产品 | 内部名 | enabled_features（显式声明） | resolved_enabled（= enabled − disabled ∪ {color, library}） | 定位 |
|------|--------|------------------------------|--------------------------------------------------------------|------|
| Topos Studio | `topos-studio` | 全部（library/color/edit/composite/deliver/teaching_animation） | 全部 | 全功能：调色 + 剪辑 + 节点合成 + 交付 |
| Topos Color | `topos-color` | library, color, deliver | library, color, deliver | 调色专注 + 轻量静帧导出（无 timeline export） |
| Topos Edit | `topos-edit` | library, color, edit, deliver | library, color, edit, deliver | 剪辑专注 + 最小调色 viewer |
| Topos Composite | `topos-composite` | library, color, composite, deliver, teaching_animation | library, color, composite, deliver, teaching_animation | 节点合成专注，Edit 关闭 |

预设解析规则（`ProductPreset.resolved_enabled`，:56-59）：`enabled − disabled ∪ ALWAYS_ENABLED`。`TOPOS_DISABLE_FEATURES` 环境变量可在运行时再剔除非 ALWAYS 项（`get_enabled_features`，:146-167）。

同名信息在 `src/shared/feature_flags.py` 有一份**平行的** `PRODUCT_MODE_PRESETS`（:70-109，键为短名 color/studio/edit/composite），用于把产品模式折叠成 feature flags。两份表必须人工保持同步——这是本层的一个已知重复点。

### 2.2 Studio 专属：工作模式（image / video）

`work_mode` 只在 studio 产品生效（`get_work_mode`，src/shared/feature_flags.py:200-218）：

- `image`：`WORK_MODE_FEATURE_OVERRIDES` 强制 `edit=False`（:130-135），隐藏 Library/Image Batch Export 面板的工作模式过滤（`PanelDescriptor.work_mode`）。
- `video`（默认）：时间线驱动，Edit 页可用；图片 Library 与图片批量导出 dock 被 `_enforce_page_dock_visibility` 强制隐藏（dock_layout_controller.py:2894-2905）。
- 布局持久化按 `get_effective_layout_mode()` 分桶（`studio_image` / `studio_video`，:221-232），两种工作模式的页面布局互不覆盖。

### 2.3 产品入口（apps/<product>/main.py）

四个入口（`apps/topos_studio|topos_color|topos_edit|topos_composite/main.py`）结构完全一致，以 topos_color 为例（全文 45 行）：

```python
os.environ["TOPOS_PRODUCT_MODE"] = "color"     # 在导入任何 src 模块之前设置
def main():
    from src.main import main as _main
    sys.argv = [sys.argv[0]] + _without_product_args(sys.argv[1:])  # 剥掉外部 --product
    sys.argv.extend(["--product", "color"])
    _main()                                     # 复用同一启动链
```

`src/products/topos_*.py` 的 `register(registry)` 已收敛为委托 `module_loader.register_preset_modules(registry, preset)`（R2-3 重构，四个入口文件 39-41 行同构）——历史上"main.py 内联 / module_loader / products register()"三份平行实现曾漂移到 `ai_workflow` 分支在生产路径丢失，收敛后 `_FEATURE_MODULES` 单表驱动（module_loader.py 模块 docstring 记录了此背景）。

### 2.4 打包裁剪（src/products/packaging.py）

`get_packaging_config(product_name)` 把 feature 映射到 `src/features/` 子目录（`_FEATURE_TO_DIR`，library 并入 color 目录），并给出每产品的合成节点插件清单（`_PLUGIN_MAP`：studio/composite 带 `["*"]` 全量，color/edit 为空）。语言包全产品共享不做裁剪。这是**构建时**维度：即使运行时同二进制，发行包可以物理裁掉 `src/features/edit/` 等目录——因此 app 层对 features 内模块的导入必须始终容错（见 §7 红线 4）。

---

## 3. 启动装配链

### 3.1 装配顺序图（main.py → main_window.show()）

```
┌────────────────────────── 进程级预置（QApplication 之前）──────────────────────────┐
│ src/main.py                                                                       │
│  [1] _isolate_macos_native_extension_symbols()   # macOS RTLD_LOCAL 符号隔离(:8-43)│
│  [2] OPENCV_IO_ENABLE_OPENEXR=1; QT_LOGGING_RULES(:51-56)                          │
│  [3] sys.path += PROJECT_ROOT; 源码检出使用本地 .topos/config.json(:59-67)          │
│  [4] win32: QTWEBENGINE_CHROMIUM_FLAGS 软渲染(:80-90)                              │
│  [5] import QtWebEngineWidgets  # 必须先于 QApplication 创建(:92-102)               │
│  [6] argparse: --product / --test-startup / --no-startup-dialog / --project        │
│  [7] TOPOS_PRODUCT_MODE ← --product; QSG_RHI_BACKEND=opengl                        │
│  [8] QSurfaceFormat 4.5 Core + AA_ShareOpenGLContexts(:178-204)                    │
│  [9] QApplication(sys.argv)                                                        │
└───────────────────────────────────────────────────────────────────────────────────┘
                                        │
┌────────────────────────── 上下文与注册表（QApplication 之后）───────────────────────┐
│  [10] ConfigManager → load_feature_flags(config)        # 产品模式折叠为 flags      │
│  [11] I18N.load_locale(保存值 | 系统 locale)                                        │
│  [12] FeatureRegistry() + AppContext(config/flags/i18n/logger) → registry.init     │
│  [13] module_loader.register_color_module(registry)    # 核心，失败快速中止         │
│  [14] module_loader.register_builtin_modules(registry, flags, failures)             │
│        # 顺序遍历 _FEATURE_MODULES：composite→edit→deliver→teaching_animation       │
│        #        →ai_workflow；逐个隔离 ImportError，失败仅记入 failures dict        │
│        # composite 注册后钩子 discover_and_register_plugins()                       │
└───────────────────────────────────────────────────────────────────────────────────┘
                                        │
┌────────────────────────── MainWindow.__init__（src/app/main_window.py:334-764）─────┐
│  [15] _init_ai_editing_state()                         # M1.2 组合根（先占位）      │
│  [16] ActionRegistry; 主题/标题/accent; ConfigManager; LogManager; ColorManager     │
│  [17] DockOptions; PageContextManager; Database; ImageEngine(+history 上限)         │
│  [18] EditProjectIO(可选); ColorSessionManager; engine.tracking_context_provider   │
│  [19] SaveRouter + _setup_save_router_callbacks; ShortcutManager                    │
│  [20] 空闲渲染/遮罩节流/调色防抖/标题栏/备份 各 QTimer                              │
│  [21] 可用性探测: _composite_available(facade 探测) / _edit_available /             │
│        _deliver_available; current_page 解析(工作模式分桶, 失效页回退 Color)         │
│  [22] create_panels()            # 所有 Color 面板 + Registry 页面 + 直接创建回退   │
│  [23] setup_docks()              # ~40 个 PanelDockWidget; Edit/Composite 条件创建  │
│  [24] setup_menus()              # 手写 app 菜单; _register_module_menu_actions     │
│        → _build_module_menus()   # MenuBuilder 从 ActionRegistry 生成 QAction       │
│        → _register_module_panels() + _build_panels_menu()                          │
│  [25] menuBar().setNativeMenuBar(False); hide()    # 菜单统一由工具栏 QToolButton 弹出│
│  [26] setup_toolbar()            # 页面切换按钮组(page_buttons) + 菜单按钮          │
│  [27] setup_statusbar(); setup_connections()       # 数百条信号→控制器槽            │
│  [28] _setup_ai_editing()        # AI 编辑子系统真正装配(仅 _edit_available)        │
│  [29] initialize_page_layouts()  # LayoutStore 迁移/恢复 或 出厂布局+switch(force)  │
│  [30] restore_layout_state(); ColorFacade(self); 上次文件恢复(挂 gl_initialized     │
│        QueuedConnection); 启动弹窗(视频→项目管理器/图像→Welcome/--no-startup-dialog)│
│        ; --project 直开; 更新检查; 批量导出队列恢复; 音频后端诊断                    │
└───────────────────────────────────────────────────────────────────────────────────┘
                                        │
              window.show() → app.exec() → _orderly_shutdown()
              # 关闭: QThreadPool.clear+waitForDone(3s) → 停缩略图 worker → os._exit
```

### 3.2 关键装配约束（源码注释即证据）

1. **QtWebEngine 必须在 QApplication 之前导入**（main.py:92-102）。历史上曾推迟到 `create_panels()` 中 files_panel 顶层导入（R0-6 修复记录），属于启动链改动的高危点。
2. **模块注册的两种失败语义**（module_loader.py:100-116）：Color 走 `guarded=False`——导入失败直接抛异常中止启动（快速失败）；其余模块 `_register_spec` 把 ImportError/异常隔离为 warning（:59-82），失败清单经 `failures` dict 由 main.py 收集，启动后 1.5s 状态栏提示用户（main.py:248-294）。
3. **save_router 后装配**：模块注册先于 MainWindow 存在，因此 `registry.register_all_save_handlers()` 必须在 `app_context.save_router = window.save_router` 之后再跑一遍（main.py:268-275；feature_registry.py:193-210）。
4. **GL ready 前不加载文件**：上次文件恢复挂在 `canvas.gl_viewport.gl_initialized` 信号上且用 `QueuedConnection`——注释明确指出 DirectConnection 会在 `initializeGL()` 内执行 `load_last_file()`，阻塞窗口完成 show()（main_window.py:654-658）。
5. **teaching_animation 的宿主注入**：main.py:270-272 在 window 创建后调用 `attach_main_window(window)`，是"模块需要 MainWindow 引用"这类需求的唯一既定通道。
6. **退出不走解释器终结化**：`_orderly_shutdown` 停完线程池后 `os._exit(exit_code)`，规避 Qt6 原生对象在 CPython GC 顺序下的崩溃（main.py:109-137）。MainWindow 侧 `closeEvent` 另有分阶段停机（§6.3）。

---

## 4. 控制器层：MainWindow 的 Mixin 联邦

### 4.1 结构

MainWindow 是一个"上帝对象 + Mixin 切面"的组合体（main_window.py:333）：

```python
class MainWindow(QMainWindow, EditControllerMixin, DeliverControllerMixin,
                 ColorControllerMixin, CompositeControllerMixin, MenuToolbarMixin,
                 PageManagementMixin, DockLayoutMixin, LayerOpsMixin,
                 ColorWorkspaceMixin, FileOpsMixin, AiEditingMixin):
```

各 Mixin 的 `self.*` 全部解析到 MainWindow 实例——控制器没有独立状态容器，这是有意为之的过渡架构（每个文件头部 docstring 均标注 "extracted from MainWindow"）。

### 4.2 职责边界表

| Mixin（src/app/controllers/） | 行数 | 职责 | 边界要点 |
|---|---|---|---|
| `PageManagementMixin`（page_controller.py） | 3,600 | 页面切换协议、PageContext 协调、Composite 播放条配置 | 见 §4.3；不直接渲染 |
| `DockLayoutMixin`（dock_layout_controller.py） | 3,092 | setup_docks、每页布局持久化（legacy + LayoutStore 双写）、出厂布局、7 个版本化布局迁移、浮动 dock 标题栏 | 见 §5.1 |
| `MenuToolbarMixin`（menu_toolbar_controller.py） | 1,883 | 手写 app 菜单（File/Edit/Effects/Window/Help）、工具栏与页面切换按钮、状态栏、模块菜单/面板接线、欢迎/项目管理器/偏好/快捷键对话框 | `_resolve_menu_callback` = `getattr(self, name)`（:1780-1782） |
| `EditControllerMixin`（edit_controller.py） | 9,861 | 剪辑/时间线/字幕/ADR/多机位/效果命令执行、Edit↔Color/Composite 上下文供给 | 深层导入收敛在 `src.features.edit.commands_facade` 之后 |
| `DeliverControllerMixin`（deliver_controller.py） | 1,901 | 单文件/快速/LUT/视频/批量导出、批量队列持久化与恢复 | 审查佐证 batch-025（AUD-2500~2503） |
| `ColorControllerMixin`（color_controller.py） | 1,841 | 调色/遮罩/LUT/降噪/去模糊/裁剪/笔刷/AI 插件处理 | batch-023（AUD-2301~2303） |
| `CompositeControllerMixin`（composite_controller.py） | 1,352 | 合成帧处理、节点编辑器、合成页切换数据 | batch-024（AUD-2401~2415） |
| `LayerOpsMixin`（layer_controller.py） | 1,910 | 图层/渲染/grading/undo-redo | 渲染经 engine.request_render，不触 GL |
| `FileOpsMixin`（file_controller.py） | 7,313 | 打开/保存/视频加载/事件/备份/closeEvent 完整退出清理 | SaveRouter 路由；batch-028 |
| `ColorWorkspaceMixin`（color_workspace_controller.py） | 257 | Color 页工作区预设（应用/保存/删除），面板枚举限 `workspace./color./advanced.` 前缀 | 纯数据在 features/color/core/workspace_presets |
| `AiEditingMixin`（ai_editing_mixin.py） | 1,902 | AI 编辑子系统组合根：AssistantService/AiEditController/HostBridge/ApprovalService 装配与工程生命周期拆装 | 只在 `_edit_available` 时装配，失败进 FAILED 态不炸工程 |
| `AiEditController`（ai_edit_controller.py） | 6,424 | 面板信号 ↔ AssistantService 映射；经类型化 `AiEditHostBridge` 与宿主解耦；不解析模型输出 | batch-020/021/022 |
| `MediaAnalysisController`（media_analysis_controller.py） | 275 | propose→confirm→execute 三步媒体分析编排；不写 timeline、不碰媒体池 widget | 工程切换 unbind 全部 worker |

另有两个独立协作件：`ai_edit_host_bridge(_impl).py`（类型化宿主协议 + MainWindow 实现）、`ai_workflow_composite_render.py`（AI Workflow 画布与合成渲染桥）。app 层还有 `src/app/integrations/file_explorer/`（adapter + EditBenchmarkHostService，把 file_explorer 的 Flask 桥接进 Edit 渲染器，main_window.py:1204-1225 注册；batch-033/034）与 `src/app/ui/` 的共享面板（files_panel/script_panel/library_panel/info_panel/audio_mixer 及 welcome/project_manager/preferences/shortcut/resize 对话框；batch-038~041）。

### 4.3 页面切换协议（PageManagementMixin）

页面集合固定为 5 个：`PageType = LIBRARY | EDIT | COLOR | COMPOSITE | DELIVER`（main_window.py:108-114）。切页有两条入口：

- `switch_to_page(name, force=False)`——普通切换（工具栏按钮、快捷键；menu_toolbar_controller.py:1399 按钮直连）。
- `switch_to_page_with_context(context)`——带 `PageContext` 的语义切换（Edit→Color 跟 clip、Edit→Composite 带 composite_id、→Deliver 带 deliver_source）。内部置 `_contextual_page_switch_in_progress` 守卫再复用 `switch_to_page`（page_controller.py:1888-1925）。

`switch_to_page` 的完整协议（page_controller.py:130-301）：

```
switch_to_page(page_name, force)
  ├─ _is_page_available() 不可用 → 回退 Color (:143-145)
  ├─ 同页且有布局 且非 force → 直接 return (:156-157)
  │    （force 供首次启动/出厂布局激活使用，否则 Composite 首启属性面板为空）
  ├─ Edit→Color 无上下文 → _switch_to_color_following_edit_playhead() 分流 (:165-172)
  ├─ 工具安全复位: canvas.cancel_active_tools / SAM / brush / picker (:174-189)
  ├─ leave_page_hook(旧页):
  │    ① registry.on_page_leave(page_id)   ← 先通知所有模块 (R-005)
  │    ② Edit/Deliver → edit_page.on_leave()
  │    ③ Color → 停 video_controller + 停时间线预览 worker
  │    ④ Composite → 清 auto-bind 抑制位 / 解除播放帧同步 / comp_pause /
  │       _flush_composite_to_edit_before_leave / adapter.on_leave_page (:358-436)
  ├─ 保存布局: page_layouts[旧页]=saveState() + _save_current_layout_to_store (:194-197)
  ├─ Deliver 独占借用编辑面板 → 离开时归还 (_restore_edit_panels_from_deliver) (:199-207)
  ├─ dock_properties 换回 edit_properties_panel（非 Composite 页）(:209-214)
  ├─ page_buttons 勾选态更新 (:216-218)
  ├─ 恢复布局: _restore_layout_from_store(新页) → legacy page_layouts → 否则播种当前 (:220-229)
  ├─ 进入 Deliver → _attach_edit_panels_to_deliver() 复用真实 Program/Timeline (:231-239)
  ├─ current_page = 新页; _enforce_page_dock_visibility() (:241-248)
  ├─ 按页延迟迁移: Edit×4 / Color×2 / Composite / Deliver 各 QTimer.singleShot (:250-277)
  ├─ enter_page_hook(新页):
  │    ① registry.on_page_enter(page_id)
  │    ② Edit → dock_properties 换面板 + edit_page.on_enter + 惰性建时间线预览渲染器
  │    ③ Composite → set_edit_project + image 模式标志 + 上下文/自动绑定/恢复工作区
  │       三级择路 + 停遗留 Color 播放 + adapter.on_enter_page(tex_id,w,h,rev) (:438-560)
  ├─ update_all_dock_titlebars(); files_panel.refresh_layout(150ms) (:292-296)
  └─ QTimer 50ms → _restore_viewport_after_page_switch()
       （makeCurrent + scene invalidate + engine.render_pipeline('viewport_refresh')
        + 再 50ms 二次渲染 _finalize_viewport_restore; Composite 页只 update）(:301-352)
```

协议级要点：

1. **两级页面钩子**：FeatureRegistry 广播（on_page_enter/leave 打到所有模块，feature_registry.py:165-191）+ MainWindow 内每页专有逻辑。模块若要在切页时做事，优先实现 `FeatureModule.on_page_enter/leave`（page_contract.py:85-101），不要在 app 层再加分支。
2. **跨页数据契约是 `PageContext`**（main_window.py:117-180）：clip_id / asset_id / timeline_frame / source_frame / clip_local_frame / color_stack_id / composite_id / timeline_id / deliver_source，可序列化（to_dict/from_dict）。`PageContextManager` 另维护每页 dirty 位与"最后上下文"。
3. **Deliver 与 Edit 共享活体面板**：Deliver 布局恢复后把**同一实例**的 Program Viewer / TimelinePanel 挂进自己的中央列（`attach_edit_panels`），播放与播放头信号因此继续有效（page_controller.py:199-239、dock_layout_controller.py:2961-2979）。改动这两个面板的 parent 语义时必须同步 Deliver 的挂/摘逻辑。
4. **Composite 进入的三级择路**：上下文进入 → Edit 来源自动绑定选中 clip → 恢复上次打开的合成 → Color session 绑定 → 独立图像 → 空白工作区（page_controller.py:480-525）。这块是切页协议里最复杂的路径，牵涉 GL 纹理交接（`adapter.on_enter_page(tex_id, ...)`），改渲染输出对象（color_output）前必读。

---

## 5. UI 集成

### 5.1 Dock 布局系统

**创建**：`DockLayoutMixin.setup_docks()`（dock_layout_controller.py:179-1027）。四角设置让顶部 Viewport 全宽、底部横跨左右；统一 tab 位置 North；`create_custom_dock(title, widget, obj_name)` 工厂为每个 dock 装 1px 隐藏标题栏、右键菜单、浮动/位置变化信号。约 40 个 dock：Color 侧（viewport/library/files/info/layers/tracker/denoise/deblur/grading/curves/color_mapping/audio_mixer/effects/grain/masks/qualifier/lut/soft_clip/raw/analysis(示波器)/export/crop/plugin(Effects)/script/presets(Gallery)/color_timeline_clips），Edit 侧条件创建（media_pool/program/source/timeline/properties/edit_info/edit_effects/caption_workspace/adr_take_sheet/multicam/ai_edit_assistant），Composite 侧条件创建（composite/node_properties/perf/keyframes/spline/import_report/asset_dependency/scene_outliner/material/scope），另有 dockVideoBatchExport。**native menuBar 被隐藏**，菜单从工具栏 QToolButton 弹出（main_window.py:549-553）。

**每页布局持久化**（双轨过渡）：
- legacy：`page_layouts: dict[page, QByteArray]`，源自由 `saveState()`，存在 config `page_layouts`（按 effective_layout_mode 分桶，:1078-1099）。
- LayoutStore（`src/shared/layout/store.py`）：`LayoutProfile(product_mode, page_id, visible_panels, qt_state_hex)`，保存时双写（`_save_current_layout_to_store`，:1516-1543），恢复时优先（`_restore_layout_from_store` 用 enabled_features + registered_panel_ids 过滤，:1545-1574）。
- 首次启动无存档：优先仓库根 `default_layout.json` 出厂布局（按 work mode 取捕获），再走 `switch_to_page(current_page, force=True)` 完整激活（:1141-1187）；最后兜底硬编码布局（:1189 起）。
- **布局迁移是版本化一次性修复**：`_migrate_edit_properties_dock_width` 等 7 个迁移各有 config 版本键，修复旧存档后不再约束用户手动调整（:1576 起）。新增"默认布局修复"必须沿用此模式，禁止持续 clamp。
- `_enforce_page_dock_visibility()`（:2880-2956）在每次切页后强制页面独占 dock 不串页 + 工作模式过滤（video 隐藏 Library/Image Export，image 隐藏 Timeline Clips/Video Batch Export）。

### 5.2 菜单注册（双轨）

- **App 级手写**：`setup_menus()`（menu_toolbar_controller.py:40-490）构建 File/Edit/Effects/Window/Help，含工程生命周期（New/Open/Project Manager/Save/SaveAs/Close）、Recent Files/Projects、Undo/Redo、偏好等。Save/SaveAs/Close 用 `ApplicationShortcut` 作用域并有 `force_application_shortcut` 属性——因为 dock 子控件持焦时 WindowShortcut 会被吞（:85-104 注释；modeless Motion Graphic 编辑器抢键的路由问题亦有记载）。
- **模块贡献**：模块实现 `register_menu_actions(action_registry)` 注册 `MenuActionDescriptor`（menu_path/action_id/label/callback_name/shortcut_id/feature_id）。`_build_module_menus()`（:1674-1716）用 `MenuBuilder`（src/app/menu/menu_builder.py）把描述符转成 QAction：解析/创建 menu_path 子菜单 → 绑 ShortcutManager 快捷键 → **callback 名经 `getattr(self, callback_name)` 解析到 MainWindow 方法**，解析不到则整个 action 跳过（防空壳菜单项，menu_builder.py:169-197）→ 按 enabled features 过滤 → File 菜单用 `_file_export_anchor` 隐形锚点做 insert-before。
- 已知坑（batch-036）：顶层菜单未注册时整组 action 仅 DEBUG 级静默跳过（AUD-3600，曾致 5 个 View/Timeline 菜单项从未出现）；动态创建的中间子菜单标题不走 i18n（AUD-3601）。**新增模块菜单时必须确认 menu_path 的顶层菜单已在 `_build_module_menus` 注册清单中**（当前注册：File/Edit/Color/Composite/Deliver）。

### 5.3 面板注册（src/shared/panels）

- `PanelDescriptor(panel_id, title_key, fallback_title, feature_id, category, page_ids, dock_attr, order, default_visible, work_mode)`；全局单例 `get_panel_registry()`。
- 模块实现 `register_panels(registry)`（由 `_register_module_panels()` 在 setup_menus 阶段调用，:1788-1802）；`_build_panels_menu()` 按 6 个类别（Workspace/Color/Edit/Composite/Deliver/Advanced）+ enabled features + work mode 过滤构建 Window > Panels 菜单（:1804-1829）。
- **dock 本体仍在 setup_docks 手工创建**，PanelRegistry 只驱动菜单可见性、工作区预设与 LayoutStore 的 visible_panels 集合——`dock_attr` 是描述符到 MainWindow 属性名的字符串映射。`validate_dock_integrity()`（registry.py:173-255）校验每个描述符都映射到真实 dock（空 dock_attr/未知 feature_id/共享 dock 均报 issue），是加面板时的自检工具。
- Color 工作区预设（ColorWorkspaceMixin）与 AI Workflow dock（默认隐藏，flag `ai_workflow` 默认 True）都建立在这套注册之上。

### 5.4 app/ui 共享面板

`src/app/ui/`：library_panel / files_panel（QtWebEngine + WebChannel 承载 external/file_explorer）/ info_panel / audio_mixer / script_panel / library_adapter，以及 welcome_dialog / project_manager_dialog（视频模式启动强制走此入口）/ preferences_dialog / shortcut_dialog / resize_dialog。特征：files_panel 对 file_explorer 的导入全部 try/except 容错（files_panel.py:18-25），因为它属于可选集成。

---

## 6. 关键调用链

### 6.1 调色参数下发（面板 → GPU，红线示范）

```
GradingPanel.grading_changed ──▶ MainWindow.on_grading_changed (ColorControllerMixin)
  ──▶ engine.set_grading_params(...)        # 只改 engine 参数，UI 线程不碰 GL
  ──▶ 交互期: on_interaction_started/ended 界定可撤销边界
       视频播放中: qualifier/soft_clip 的 params_changed 额外接
       _invalidate_video_render_params_if_playing (main_window.py:1592-1593,1677-1678)
  ──▶ engine.render_pipeline(...) 由渲染线程消费
  ◀── engine.frame_ready / color_output_changed (信号回 UI, main_window.py:1473-1486)
```

拖动期间的合流靠 `_grading_debounce_timer`（25ms 单发，main_window.py:448-453）；Color 输出交给 Composite 用的是 `engine.color_output`（texture_id/width/height/revision + `request_color_output()`，page_controller.py:550-559）——**跨页纹理交接只传句柄与版本号，UI 线程从不 makeCurrent 之外的 GL 操作**。反例见 AUD-2500（deliver_controller 的 Composite 导出 fallback 曾在 UI 线程直接建 GL context/纹理），是本层红线的正面教材。

### 6.2 保存链（SaveRouter 单点）

MainWindow 创建 `SaveRouter` 并 `_setup_save_router_callbacks()`（main_window.py:424-426）；模块注册自定义 save handler 的通道是 `register_save_handlers(context)`，但 Color/Edit/Composite/Deliver 四模块当前都留空、依赖 SaveRouter 的 `_save_from_color/_save_from_edit/_save_from_composite/_save_from_deliver` 回调式实现（各 module.py 的 docstring 明示）。Edit 工程另有 `EditProjectIO`（自动保存/备份/恢复，callbacks 在 main_window.py:402-408 注入）。

### 6.3 关闭链

`closeEvent`（main_window.py:1903-2004）顺序：tangent_bridge → tracking_service → edit benchmark service → files_panel(Flask) → `_shutdown_playback_pipeline()`（走带→音频→Color 视频→时间线预览渲染→合成渲染线程，幂等 `_pipeline_shutdown_done`）→ **显式 `FileOpsMixin.closeEvent(self, event)`**（QMainWindow 在 MRO 中先于 mixin，super() 会跳过 FileOps 的保存确认与配置持久化——注释即红线，:1941-1946）。已知缺陷：保存确认对话框弹出前服务已全部停机，用户"取消"后应用半瘫痪（AUD-3500，见 §8）。

---

## 7. 改动注意事项（本层红线）

1. **UI 线程不碰 GPU**：控制器/面板只允许调 engine 的高层 API（`set_*_params` / `request_render` / `render_pipeline` / `request_color_output`）和消费信号（`frame_ready` / `color_output_changed`）。任何"在槽函数里直接建 context、上传纹理、readPixels"都是缺陷（AUD-2500）。例外且仅限两处既定模式：切页后的 `_restore_viewport_after_page_switch` 的 `makeCurrent + update`，以及启动恢复文件的 QueuedConnection 延迟。
2. **切页协议内禁止新增同步重活**：`switch_to_page` 主路径在 UI 线程同步执行并计时打点（[PAGE-SWITCH] 日志）；布局迁移、面板刷新、viewport 恢复都被刻意排进 `QTimer.singleShot`。新增每页逻辑应跟随 enter/leave hook 的两级结构，不要往 `switch_to_page` 主体塞分支。
3. **回调名即契约**：`MenuActionDescriptor.callback_name` 是 MainWindow 方法名字符串，`MenuBuilder` 解析不到就静默（DEBUG）丢弃整个 action。给 MainWindow 方法改名前必须 grep 各 feature module 的 `register_menu_actions`。
4. **features 内模块导入必须可失败**：color-only 构建物理裁掉 `src/features/edit|composite/` 目录（packaging.py），app 层所有对 features 的深层导入都要 try/except 或走 commands_facade / facade 延迟探测（main_window.py:58-76、481-491）。
5. **布局状态键稳定性**：dock 的 `objectName`（如 "dockPresets"）与 config 键、LayoutStore profile、7 个迁移键构成持久化契约；改 dock 更名必须保留旧 objectName 兼容（dock_presets 注释即先例，dock_layout_controller.py:344-346）。
6. **产品裁剪改动要三处同步**：`src/products/__init__.py` 的 PRODUCT_PRESETS、`src/shared/feature_flags.py` 的 PRODUCT_MODE_PRESETS、`module_loader._FEATURE_MODULES`（新特性）+ `packaging.py`（目录映射）。漏一处会出现"flag 开了但模块没注册"或"模块注册了但打包缺失"。
7. **退出路径**：新增常驻线程/QThread 必须同时给 `closeEvent` 或 `_shutdown_playback_pipeline` 停止入口（AUD-3501~3503 是缺失例证），并记住 `main.py` 侧最终 `os._exit` 不跑 Python 终结化——析构逻辑不能依赖 `__del__`。
8. **已知启动残留缺陷**：`src/main.py:291` 在 `module_failures` 非空时调用 `QTimer.singleShot(...)`，但 `QTimer` 未在 main.py 导入（顶部仅 `from PyQt6.QtCore import Qt`）——该分支一触发即 NameError。触碰启动失败提示路径时需先补 `from PyQt6.QtCore import QTimer`。

---

## 8. 已知问题（审查佐证索引）

| 问题 | 出处 | 说明 |
|---|---|---|
| AppContext fail-open + 全 Any 类型 | batch-018（AUD-18001/18002） | feature_flags 缺失时 `is_feature_enabled` 一律放行 |
| closeEvent 先停服务后弹保存确认 | batch-035（AUD-3500） | 取消关闭后应用半瘫痪，`_pipeline_shutdown_done` 不可逆 |
| UpdateChecker/script_engine/denoise 线程无停止路径 | batch-035（AUD-3501~3503） | 关闭链缺口 |
| Deliver 导出 fallback UI 线程建 GL context | batch-025（AUD-2500） | §7 红线 1 的实证 |
| MenuBuilder 未注册顶层菜单静默跳过 / 子菜单未 i18n | batch-036（AUD-3600/3601） | §5.2 已知坑 |
| Composite 渲染回调闭包跨线程操作 Qt 控件 | batch-024（AUD-2401~2403） | 控制器回调线程契约 |
| 遮罩操作不进 undo 栈 / brush 遍历 MaskGroup 崩溃 | batch-023（AUD-2301/2302） | Color 控制器 |
| ai_edit 信号契约竞态 / host bridge 线程边界 | batch-019~022 | AI 编辑子系统 |
| files_panel 无限重试 / 路径一次性消费 | batch-038/040（AUD-3804 等） | app/ui 共享面板 |
| dock 布局系统 13 项 | batch-026（AUD-2600~2612） | 布局双轨过渡期问题 |
| page_controller 14 项 | batch-031（AUD-3100~3113） | 切页协议细节 |
| module_loader / page_contract 7 项 | batch-037（AUD-3200~3205 为 feature_flags/registry） | 注册链 |

---

## 9. 依据的源文件

启动与装配：

- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/main.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/app_context.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/module_loader.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/feature_registry.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/page_contract.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/feature_flags.py`（re-export）

产品形态：

- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/products/__init__.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/products/topos_color.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/products/topos_edit.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/products/topos_composite.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/products/topos_studio.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/products/packaging.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/apps/topos_color/main.py`（其余三个入口同构）
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/shared/feature_flags.py`

主窗口与控制器：

- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/main_window.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/page_controller.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/dock_layout_controller.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/menu_toolbar_controller.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/color_controller.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/color_workspace_controller.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/composite_controller.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/deliver_controller.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/edit_controller.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/file_controller.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/layer_controller.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/ai_editing_mixin.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/ai_edit_controller.py`（头部契约）
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/controllers/media_analysis_controller.py`（头部契约）
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/integrations/file_explorer/`（adapter.py、edit_benchmark_service.py）

UI / 菜单 / 面板注册：

- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/menu/menu_builder.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/shared/panels/registry.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/shared/layout/store.py`、`.../profile.py`（LayoutStore）
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/app/ui/files_panel.py`（头部）

Feature 模块注册面（app 层视角）：

- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/color/module.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/edit/module.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/composite/module.py`
- `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/src/features/deliver/module.py`

审查佐证：`/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/docs/audit/2026-09-28/batches/`（batch-018 ~ batch-041，src/app 全域）。
