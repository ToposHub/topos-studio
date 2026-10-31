# batch-026 审查报告 — src/app/controllers/dock_layout_controller.py

- 批次: batch-026 [P1] 主题 src/app/controllers//
- 编号区间: AUD-2600–AUD-2699（本报告实际使用 AUD-2600–AUD-2612）
- 审查日期: 2026-09-28
- 审查方式: 全文通读（1–3092 行，分块 Read），关键结论经跨文件只读核实
  （src/shared/db/database.py、src/shared/layout/store.py、src/shared/config/config_manager.py、
  src/app/main_window.py、src/app/controllers/file_controller.py、page_controller.py、
  menu_toolbar_controller.py、src/features/deliver/module.py、src/features/color/module.py）

统计: Critical 0 / Major 4 / Suggestion 9

---

## src/app/controllers/dock_layout_controller.py (行数: 3092)

### 发现

- [AUD-2600] [Major] 浮动 dock 尺寸/位置在“重新停靠瞬间”被停靠几何覆盖写入 floating_* 配置键
  - 位置: src/app/controllers/dock_layout_controller.py:2076-2082
  - 证据:
    ```python
    else:
        # Dock is being docked - save its floating size and position
        dock_size_key = f"dock_{obj_name}_floating_size"
        dock_pos_key = f"dock_{obj_name}_floating_pos"

        self.config_manager.set(dock_size_key, f"{dock.width()}x{dock.height()}")
        self.config_manager.set(dock_pos_key, f"{dock.x()},{dock.y()}")
    ```
  - 影响: `topLevelChanged(False)` 触发时 dock 已被 reparent 进主窗口：`x()/y()` 是主窗口相对坐标（子部件坐标系），下次浮动经 `dock.move(x, y)`（第 2073 行）却按屏幕坐标解释，浮动窗口会出现在无关位置甚至屏幕外；`width()/height()` 在该时刻已非浮动窗口尺寸（具体值取决于 Qt 布局时序），用户设定的浮动尺寸丢失。file_controller.py:6827-6835 在关闭时已正确保存“仍处于浮动状态”的 dock 尺寸，本分支会在用户停靠面板时用错误值将其覆盖。
  - 建议: 删除停靠分支的写回（保留 closeEvent 的正确保存路径）；若需即时持久化，应在 `is_floating=True` 期间（如 floating 状态的 resizeEvent/moveEvent 结束时）保存，而非停靠信号里。恢复浮动位置时复用 `_ensure_window_on_screen` 的钳制逻辑（当前 2056-2075 的恢复分支对屏幕外坐标无任何钳制）。
  - 置信度: 中（坐标语义错误确定无疑；尺寸具体取值取决于 Qt 在信号时刻是否已完成 relayout）

- [AUD-2601] [Major] dock_properties 等 5 个 Edit 右侧 dock 未注册进 all_docks，浮动后无法获得可拖拽标题栏
  - 位置: src/app/controllers/dock_layout_controller.py:967-1020（成员表）、2183-2185（标题栏更新遍历）、3076-3092（_sync_floating_docks 同样只遍历 all_docks）
  - 证据:
    ```python
    if self.dock_edit_info:
        self.all_docks.append(self.dock_edit_info)
    if self.dock_ai_edit_assistant:
        self.all_docks.append(self.dock_ai_edit_assistant)
    # Keyframes/Spline 底部 dock 必须在 all_docks 里：否则浮动时
    # _perform_update_all_titlebars 不会给它们安装 FloatingDockTitleBar，
    # 顶部只剩 1px 隐形标题栏——既无法拖动浮动窗口，也无法拖回停靠。
    ```
    而 `dock_properties`（566 行）、`dock_edit_effects`（582 行）、`dock_caption_workspace`（591 行）、`dock_adr_take_sheet`（604 行）、`dock_multicam`（619 行）均经 `create_custom_dock` 创建（自带 1px 隐藏标题栏并连接 topLevelChanged），却从未被 append 进 all_docks（已 grep 全文件核实）。
  - 影响: 用户浮动 Properties/Effects/Captions/ADR/Multicam 任一面板后，`_perform_update_all_titlebars` 遍历不到它们，不会安装 `FloatingDockTitleBar`——浮动窗口顶部只剩 1px 抓取区，无法拖动也无法拖回停靠（正是本文件 1001-1003 行注释自述的故障模式）；启动恢复时 `_sync_floating_docks` 同样跳过它们。仅能靠右键菜单的 Dock 项救回。
  - 建议: 将这 5 个 dock 按现有模式加入 all_docks；或改 `_perform_update_all_titlebars`/`_sync_floating_docks` 为 `self.findChildren(QDockWidget)` 全量遍历（同时保留 1px 判定去重）。
  - 置信度: 高

- [AUD-2602] [Major] 三处裸 `except: pass` 吞掉包括 KeyboardInterrupt/SystemExit 在内的一切异常
  - 位置: src/app/controllers/dock_layout_controller.py:2064、2074、3047
  - 证据:
    ```python
    if saved_size:
        try:
            width, height = map(int, saved_size.split('x'))
            dock.resize(width, height)
        except:
            pass
    ```
    （2074 行同型：`x, y = map(int, saved_pos.split(','))`；3047 行 `_restore_docked_sizes` 中 `width, height = map(int, saved_size.split('x'))` 后 `except: pass`）
  - 影响: 直接违反项目铁律“不得宽泛 except 掩盖领域错误”。配置值损坏以外的任何异常（如 dock 已被销毁引发的 RuntimeError）都被静默吞掉，无从诊断布局恢复失效的根因。
  - 建议: 改为 `except ValueError:`（解析失败预期路径）并 `logger.warning` 记录原始值；恢复分支对负值/零值尺寸也应拒绝。
  - 置信度: 高

- [AUD-2603] [Major] `_enforce_page_dock_visibility` 未覆盖 Deliver dock，Deliver 页专属 dock 可泄漏到其他页面
  - 位置: src/app/controllers/dock_layout_controller.py:2880-2956（方法整体无 dock_deliver 分支）
  - 证据:
    ```python
    # Composite page docks — only visible on Composite page
    if self.current_page != "Composite":
        if self.dock_composite:
            self.dock_composite.setVisible(False)
        if self.dock_node_properties is not None:
            self.dock_node_properties.setVisible(False)
    ```
    edit/composite/color_timeline_clips 各有强制隐藏分支，唯独 `dock_deliver` 缺席（全方法 grep 无一处出现）。而其注册声明为页面专属（src/features/deliver/module.py:326-336：`page_ids=("Deliver",)`、`work_mode="video"`），且创建时 `setMinimumWidth(1220); setMinimumHeight(620)`（960-961 行）。
  - 影响: (1) 旧版本/损坏的保存状态若含“非 Deliver 页上 dock_deliver 可见”，本方法（自述职责正是纠正此类状态）不会修复，其他页面被一个最小 1220x620 的巨型 dock 挤占；(2) 硬编码兜底分支（1189 行起）中 `apply_default_layout` 对 all_docks 全员 `setVisible(True)`（2247-2253 行），而 1216 行 `self.page_layouts["Color"] = self.saveState()` 之前没有任何代码隐藏 dock_deliver——出厂文件仅含 "video" 捕获（default_layout.json 实测 `modes=["video"]`），image/"all" 工作模式首启走兜底分支时 Color 页布局会带着 Deliver dock 一起保存并恢复。
  - 建议: 在方法中补 `if self.current_page != "Deliver" and self.dock_deliver: self.dock_deliver.setVisible(False)`；兜底分支的隐藏列表也应包含 dock_deliver（或让 apply_default_layout 尊重面板注册表的 default_visible/page_ids）。
  - 置信度: 中（页面专属意图由 page_ids/独立布局构建/1220px 最小宽度三重佐证；不排除刻意允许用户跨页打开，但与本方法对其他页专属 dock 的处理不一致）

- [AUD-2604] [Suggestion] Edit/Color 布局迁移写回 page_layouts 只处理扁平形态，mode-scoped 结构被顶层混入页面 hex
  - 位置: src/app/controllers/dock_layout_controller.py:1613-1617、1762-1766、1860-1864、1948-1952、2002-2009
  - 证据:
    ```python
    saved_layouts = self.config_manager.get("page_layouts")
    if isinstance(saved_layouts, dict):
        saved_layouts = dict(saved_layouts)
        saved_layouts["Edit"] = edit_state.toHex().data().decode()
        self.config_manager.set("page_layouts", saved_layouts, auto_save=False)
    ```
    正常运行时 page_layouts 为 mode-scoped（file_controller.py:6802-6809 以 `{"<elm>": {page: hex}}` 写入），上述四处迁移会把 `"Edit"`/`"Color"` 的 hex 字符串写到顶层，与模式桶混型；`_migrate_color_mapping_dock_layout`（2002-2009 行）则在 mode-scoped 形态下干脆不写回。对照 `_migrate_composite_scopes_dock_visibility`（1690-1710 行）已实现扁平/分层双形态兼容。
  - 影响: config["page_layouts"] 结构被持久污染（顶层混入 str，永不清理）；下次启动 `LayoutStore.migrate_legacy_page_layouts`（store.py:210-222）会把顶层 str 当 page 迁回 store（若对应 profile 恰好缺失），存在旧状态复活路径；五个迁移行为彼此不一致。
  - 建议: 抽取公共 helper（输入 page_id + 新 state，内部检测扁平/分层并写对层级），供全部迁移复用；顺手让 color_mapping 迁移也写回分层形态。
  - 置信度: 高

- [AUD-2605] [Suggestion] reset_product_layout 未清除 legacy 配置桶，且当前无任何调用点
  - 位置: src/app/controllers/dock_layout_controller.py:2020-2034
  - 证据:
    ```python
    product_mode = self._current_product_mode()
    profiles = self.layout_store.get_profiles_for_mode(product_mode)
    for page_id in list(profiles.keys()):
        self.layout_store.delete_profile(product_mode, page_id)

    # Also clear the legacy page_layouts dict entries
    for page_id in list(self.page_layouts.keys()):
        del self.page_layouts[page_id]
    ```
  - 影响: docstring 承诺"Clear all saved layouts for the current product mode"，但只删了 LayoutStore profile 与内存 dict，未清 config["page_layouts"] 当前模式桶——下次启动 `initialize_page_layouts`（1082-1103 行）会从桶里恢复旧状态（store 已空、无覆盖），"重置"重启后失效。另外全库 grep 仅此一处定义、无调用者（菜单接的是 `reset_layout_to_default`，menu_toolbar_controller.py:382），属死代码，缺陷随接线才会暴露。
  - 建议: 补 `config_manager.set("page_layouts", ...)` 清除当前 elm 桶（或复用 AUD-2604 的 helper）；若短期不接线，标注 deprecated 或删除。
  - 置信度: 高

- [AUD-2606] [Suggestion] apply_default_layout 无条件 setVisible(True)，覆盖 tracker 等面板的“默认隐藏”契约
  - 位置: src/app/controllers/dock_layout_controller.py:2247-2253（对照 262-266 行）
  - 证据:
    ```python
    for d in self.all_docks:
        if d == self.dock_plugin:
            ...
        else:
            d.setVisible(True)
        d.setFloating(False)
    ```
    而 setup_docks 中明确"默认隐藏"：`self.dock_tracker.setVisible(False)`（266 行），面板注册表同样声明 `color.tracker ... default_visible=False`（color/module.py:420-429）。
  - 影响: 出厂/已保存状态分支随后被 restoreState 覆盖，无感；但硬编码兜底分支（image/"all" 模式首启可到达，见 AUD-2603）里 1216 行保存 Color 布局时 tracker 已被重新显示，首启 Color 页会出现本应默认隐藏的 Tracker 面板（min 宽 320、resizeDocks 至 520）。
  - 建议: apply_default_layout 改为读取面板注册表 default_visible（或至少对 tracker 复现 266 行的隐藏），与 plugin 的条件化处理对齐。
  - 置信度: 中

- [AUD-2607] [Suggestion] 静默异常处理无日志（except Exception 后直接 pass / 赋默认值）
  - 位置: src/app/controllers/dock_layout_controller.py:2119-2124、1084-1087、1477-1480、1697-1701
  - 证据:
    ```python
    if pending is not None:
        try:
            self._restore_ai_workflow_payload(pending)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._pending_ai_workflow_payload = None
    ```
    （另三处：`except Exception: elm = "studio"` / `return "all"` / `mode_key = "all"`）
  - 影响: workflow payload 恢复失败、feature_flags 读取失败（可能意味着配置损坏）均无任何痕迹，排障时只能靠猜；回退默认值本身合理，但吞掉原因不合理。
  - 建议: 统一加 `logger.warning("...", exc_info=True)`（一处一行），不改变回退行为。
  - 置信度: 高

- [AUD-2608] [Suggestion] resizeDocks 传入未停靠的裸 QWidget 作占位
  - 位置: src/app/controllers/dock_layout_controller.py:1421-1425
  - 证据:
    ```python
    self.resizeDocks(
        [self.dock_composite, self.dock_viewport, self.dock_node_properties if self.dock_node_properties else QWidget()],
        [600, 800, 300],
        Qt.Orientation.Horizontal
    )
    ```
  - 影响: `resizeDocks` 文档语义针对停靠中的 QDockWidget；传入不在任何 dock 区域、无父对象的裸 QWidget 属未定义行为（当前 Qt 实现大概率忽略该项），且该 widget 无父挂靠、只能等 GC 回收。第三个尺寸 300 也在 node_properties 缺席时静默失效。
  - 建议: 动态构造列表（仅收集存在的 dock）与对应尺寸列表，跳过缺失项。
  - 置信度: 高

- [AUD-2609] [Suggestion] 大量方法缺类型注解
  - 位置: src/app/controllers/dock_layout_controller.py:179（setup_docks）、1069（initialize_page_layouts）、143（_ensure_window_on_screen 的 window 参数）、2047（_on_dock_floating_changed）、2132、2169、2238、2339、2354、2370、2387、2405、2426、2439、2454、2591、2804、2880、3007、3022、3067 等；2488 行 `docks: dict = {}` / `_workspace_dock_map` 返回裸 `dict`
  - 证据:
    ```python
    def setup_docks(self):
    ...
    def _on_dock_floating_changed(self, is_floating, dock):
    ...
    def apply_layout(self, name):
    ```
  - 影响: 违反项目“强类型（所有函数有类型注解）”约束；本文件新方法大多有注解（如 `_load_factory_layout`），旧方法成片缺失，风格割裂。
  - 建议: 按现有风格补 `-> None` / 参数注解（is_floating: bool, dock: QDockWidget, name: str 等）；`_workspace_dock_map -> dict[str, QDockWidget]`。
  - 置信度: 高

- [AUD-2610] [Suggestion] 性能小项：getattr 默认值无条件执行 findChildren；每次停靠触发两次全量写盘
  - 位置: src/app/controllers/dock_layout_controller.py:2183、2081-2082
  - 证据:
    ```python
    docks_to_check = getattr(self, 'all_docks', self.findChildren(QDockWidget))
    ```
    ```python
    self.config_manager.set(dock_size_key, f"{dock.width()}x{dock.height()}")
    self.config_manager.set(dock_pos_key, f"{dock.x()},{dock.y()}")
    ```
  - 影响: (1) `getattr` 的默认值表达式每次都求值，all_docks 存在时 findChildren 的全树枚举结果被直接丢弃——该函数经 50ms 去抖后仍会在每次 dock 移动/可见性变化后执行；(2) config_manager.set 默认 auto_save=True（config_manager.py:650-660，每次全量落盘），停靠一个 dock 连写两次盘（修复 AUD-2600 时可一并处理：auto_save=False + 一次 save_config）。
  - 建议: `docks_to_check = getattr(self, "all_docks", None) or self.findChildren(QDockWidget)`；浮动尺寸保存改批量写。
  - 置信度: 高

- [AUD-2611] [Suggestion] 四段近乎相同的“隐藏页面专属 dock”列表重复且彼此漂移
  - 位置: src/app/controllers/dock_layout_controller.py:1111-1114、1155-1158、1192-1194、1434-1436（另 1421-1452 的 composite 段）
  - 证据:
    ```python
    # 1111-1114（含 caption/adr/multicam）
    for d in [self.dock_edit_media_pool, self.dock_program_viewer, self.dock_source_viewer,
              self.dock_edit_timeline, self.dock_edit_info, self.dock_edit_effects, self.dock_properties,
              self.dock_caption_workspace, self.dock_ai_edit_assistant,
              self.dock_adr_take_sheet, self.dock_multicam]:
    ```
    ```python
    # 1192-1194（少了 caption_workspace/adr_take_sheet/multicam）
    for d in [self.dock_edit_media_pool, self.dock_program_viewer, self.dock_source_viewer,
              self.dock_edit_timeline, self.dock_edit_info, self.dock_edit_effects, self.dock_properties,
              self.dock_ai_edit_assistant]:
    ```
  - 影响: 同一意图四处手抄，已出现成员漂移（1192 段与 1434 段还缺 keyframes/spline/color_timeline_clips）。当前因相关 dock 创建时即处于隐藏态而未爆发，但每新增一个页面专属 dock 都要改四处，漏改即产生跨页泄漏（AUD-2603 的 Deliver 缺口即同源问题）。
  - 建议: 抽取 `_hide_page_scoped_docks()` 统一列表（含 dock_deliver），四处调用。
  - 置信度: 高

- [AUD-2612] [Suggestion] restoreState 返回值被忽略，失败无日志无回退
  - 位置: src/app/controllers/dock_layout_controller.py:2827、2834（另 1182、2365 同型）
  - 证据:
    ```python
    if self.current_page in self.page_layouts:
        ...
        self.restoreState(self.page_layouts[self.current_page])
    else:
        ...
        if last_state:
            self.restoreState(QByteArray.fromHex(last_state.encode()))
    ```
  - 影响: 保存状态与本 build 不兼容时 restoreState 返回 False 且界面停留在 apply_default_layout 的全员可见排列，用户只看到“布局没恢复”，无任何日志（对照 `_restore_layout_from_store` 1564-1569 行至少记了 debug）。
  - 建议: 检查返回值，失败时 logger.warning 并回退到 window_state / 出厂布局。
  - 置信度: 高

### 通过项
- 线程安全：本文件全部为 Qt UI 线程操作，无渲染线程/OpenGL 交互；AI workflow 的 composite 渲染 provider（worker 线程执行）实现在 ai_workflow_composite_render，不在本批次范围
- GPU 资源生命周期：无 ModernGL/纹理/缓冲对象操作；dock_analysis 可见性门控正确连接 `_on_scope_visibility_changed`（325 行，隐藏示波器不耗 GPU 分析时间），`_on_masks_panel_visibility_changed`/`_on_tracker_dock_shown` 均已核实存在
- GLSL/HDR 正确性：本文件无着色器/HDR 数据路径，不适用
- 信号防递归：标题栏更新经 `_titlebar_update_timer`（单次 50ms 去抖，main_window.py:457-460）+ `_updating_titlebars` 状态锁双防护；`update_layout_combobox` 用 blockSignals；layout_combo 仅连接用户触发的 `activated`（menu_toolbar_controller.py:1486），`on_layout_combo_activated` 内 `setCurrentIndex(0)` 不会递归触发
- undo/redo/revision 一致性：布局属配置级状态，按设计不入撤销栈，与 undo 系统无冲突
- 输入验证（部分通过）：`_load_factory_layout` 对 geometry/state/panel_states 类型与可用性校验完整（2656-2687）；`db.load_layout` 缺失时返回 (None, None, None)，`apply_layout` 元组解包安全（已核对 database.py:161-177）；迁移函数对损坏的 "WxH" 字符串有 ValueError 捕获并告警（1820-1825 等）——仅 bare except 部分见 AUD-2602
- 异常降级路径：AI Edit Assistant / AI Workflow / Composite 适配器创建失败均有 `logger.exception` 记录并优雅降级（673-675、814-821、941-948 行）
- 浮动窗口主几何恢复：`_ensure_window_on_screen` 对多显示器/DPI 变化的尺寸+位置双钳制正确，且在 restore/apply 两个路径的多个时点重复钳制（2874-2875、2472）
