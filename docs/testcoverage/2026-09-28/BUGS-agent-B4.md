# BUGS-agent-B4：dock_layout_controller.py 补测发现的疑似缺陷

- 日期：2026-09-28
- 模块：`src/app/controllers/dock_layout_controller.py`
- 来源：Plan-02 补测（`tests/app/test_dock_layout_controller_coverage.py`，65 用例）
- 处置原则：**只报告，不修改 src**。三个问题的"当前行为"均已用锁定断言写进测试
  （见各条"锁定用例"），修复后应把断言翻转为正确预期。

严重度定义：high（用户可见数据/状态错误）> medium（状态不一致，特定路径才暴露）>
low（健壮性缺口）。

---

## BUG-B4-01（high）：5 个 Edit dock 未注册进 all_docks，浮动后不可拖拽、尺寸不持久

- 位置：`DockLayoutMixin.setup_docks` 的 `self.all_docks = [...]` 构造块及其后的
  append 序列（dock_layout_controller.py ~L967-1020）。
- 现象：以下 5 个 dock 通过 `create_custom_dock` 创建，但从未进入 `all_docks`：

  | 属性 | objectName |
  |------|-----------|
  | `dock_properties` | dockProperties |
  | `dock_edit_effects` | dockEditEffects |
  | `dock_caption_workspace` | dockCaptionWorkspace |
  | `dock_adr_take_sheet` | dockAdrTakeSheet |
  | `dock_multicam` | dockMulticam |

  注册集合 = 基础列表（26 个）+ 条件 append（edit_media_pool / program_viewer /
  source_viewer / edit_timeline / edit_info / ai_edit_assistant / composite 家族 /
  deliver）。源码里正好缺上述 5 个。
- 后果：源码 L1001-1003 的注释已经说明过这类缺口对 Keyframes/Spline 的教训——不在
  `all_docks` 里的 dock：
  1. `_perform_update_all_titlebars` / `_sync_floating_docks` 跳过它们 → 浮动后
     顶部只剩创建时安装的 1px 隐形标题栏，**无法拖动浮动窗口、也无法拖回停靠**
     （与 Keyframes/Spline 当年的 bug 同机制，tests/ui/
     test_composite_dock_floating_titlebar.py 是那次修复的回归测试）；
  2. `_save_all_docked_sizes` / `_restore_docked_sizes` 跳过它们 → 停靠尺寸
     不持久化，布局恢复后 splitter 比例丢失。
  受影响的是 Edit 页核心面板（Properties / Effects / Captions / ADR / Multicam），
  用户把它们浮动后就会踩中。
- 锁定用例：
  `test_created_docks_missing_from_all_docks_are_exactly_the_known_five`
  （源码级断言：`created - registered == {上述 5 个}`，注释指明修复后应收紧为
  `== set()`）；行为侧另见
  `test_registered_docks_get_titlebar_maintenance`（注册成员获得标题栏维护）。
- 建议修复方向：在 setup_docks 的 append 段为 5 个属性补 `all_docks.append(...)`
  （注意它们都是条件创建，需沿用 `if self.dock_xxx:` 判空），然后把锁定用例断言
  翻转为 `missing == set()`。

## BUG-B4-02（medium）：_enforce_page_dock_visibility 漏掉 dock_caption_workspace

- 位置：`DockLayoutMixin._enforce_page_dock_visibility` 的 `edit_only` 列表
  （dock_layout_controller.py ~L2910-2913）。
- 现象：`edit_only` 收录了 program_viewer / source_viewer / edit_timeline /
  edit_info / edit_effects / ai_edit_assistant / adr_take_sheet / multicam，唯独没有
  `dock_caption_workspace`；该方法其余分支也从未引用它。
- 后果：Captions 是 Edit 专属面板（setup_docks 中与 Properties 同栈 tab 化，且
  `initialize_page_layouts` 的"保存布局前置隐藏列表"（~L1111-1114、~L1155-1158）
  都包含它）。但页面可见性强制这一最终防线不覆盖它：一份把 Captions 泄漏到
  Color/Composite 页的旧布局，恢复后 Captions 会留在错误页面；ADR / Multicam 会被
  强制隐藏而 Captions 不会，行为不一致。
- 锁定用例：`test_enforce_caption_workspace_is_not_page_hidden_bug_pin`
  （Color 页 enforce 后 ADR/Multicam 隐藏、Captions 仍可见，注释指向本条）。
- 建议修复方向：把 `self.dock_caption_workspace` 加进 `edit_only` 列表，然后把
  锁定用例断言翻转为 `_hidden(...) is True`。

## BUG-B4-03（medium）：initialize_page_layouts 的页 dock 隐藏清单漏掉 dock_deliver
与 dock_node_properties（靠后续分支的 hide-all 兜底才不暴露）

- 位置：`DockLayoutMixin.initialize_page_layouts` 硬编码分支的收尾隐藏段
  （dock_layout_controller.py ~L1434-1452；对比 ~L1111-1136 的前置隐藏段）。
- 现象：收尾段逐项隐藏 edit/composite 家族 dock，但清单不含
  `dock_deliver` 和 `dock_node_properties`：
  - `dock_node_properties` 在 Composite 分支被 `setVisible(True)`（~L1353）后，
    收尾段从不重新隐藏——实测 initialize 结束后它在非 Composite 页保持可见
    （锁定用例第一段）。
  - `dock_deliver` 在 Deliver 分支被 `setVisible(True)`（~L1331）后，只有当
    Composite 分支随后执行其 `for d in self.all_docks: setVisible(False)` 时才被
    **顺带**隐藏；Composite 不可用的配置下（如相关 feature 关闭/图像模式变体）
    它在非 Deliver 页保持可见（锁定用例第二段用
    `_DockHost(composite_available=False)` 复现）。
- 后果：不变量"页面专属 dock 只在本页可见"在这两条路径上靠巧合维持而非靠设计；
  `_enforce_page_dock_visibility` 本身倒是有 dock_node_properties（~L2934），
  但 `_enforce` 不含 dock_deliver（见下），initialize 与 enforce 两套清单都不完整。
- 附带发现（同族，未单列）：`_enforce_page_dock_visibility` 完全没有
  `dock_deliver` 分支——非 Deliver 页上它同样不会被强制隐藏（锁定用例
  `test_enforce_deliver_dock_is_not_page_hidden_bug_pin`）。修复时两处应一并补齐。
- 锁定用例：`test_initialize_falls_back_to_hardcoded_defaults`（两段 BUG-B4-03
  pin：node_properties 泄漏 + 无 Composite 分支时 deliver 泄漏）、
  `test_enforce_deliver_dock_is_not_page_hidden_bug_pin`。
- 建议修复方向：initialize 收尾隐藏清单补 `dock_deliver` / `dock_node_properties`
  （以及前置两段清单对齐）；`_enforce_page_dock_visibility` 补
  `if self.current_page != "Deliver": hide dock_deliver`；然后把上述锁定断言翻转。

---

## 补测过程记录（非 src 缺陷，供后续测试参考）

- Qt 6.10.2 offscreen 下，`apply_default_layout` / `initialize_page_layouts` 这类
  长 `tabifyDockWidget` 链在**已 show 的** QMainWindow 上有约 50-70% 概率段错误
  （生产路径总在 show 之前执行，故线上不受影响；`tests/edit/test_workspace_presets.py`
  亦采用不 show 宿主）。本文件提供 `layout_host`（不 show）fixture 供这些路径
  使用，`host`（show）fixture 用于需要 `isVisible()` 语义的用例。
- 交付物：`tests/app/test_dock_layout_controller_coverage.py`，65 个用例全绿；
  单文件即覆盖 `dock_layout_controller.py` 可执行行 55%（基线 21%；未覆盖主体是
  `setup_docks` 的完整 MainWindow 装配，需真实面板栈，不属于 headless 可测单元）。
