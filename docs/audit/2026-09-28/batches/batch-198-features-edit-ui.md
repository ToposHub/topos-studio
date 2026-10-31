# 批次报告：batch-198 [P1] — src/features/edit/ui//

- 审查日期：2026-09-28
- 编号区间：AUD-19800–AUD-19899（实际使用 AUD-19800–AUD-19812）
- 文件：`src/features/edit/ui/edit_effects_panel.py` (2,270)、`src/features/edit/ui/edit_info_panel.py` (377)
- 汇总：Critical 1 / Major 3 / Suggestion 9
- 跨模块核实：`motion_graphic_generator_registry.py`（subscribe/register 同步分发）、`config_manager.py`（save_config 先合并磁盘再写 dirty keys，多实例安全）、`main_window.py:1374`（面板单例创建）。对 src/ 全程只读。

## src/features/edit/ui/edit_effects_panel.py (行数: 2270)

### 发现

- [AUD-19800] [Critical] MG 生成器卡片双击触发 NameError（`template_display_name` 仅为函数内局部导入），未捕获异常逃出 Qt 槽可致应用中止
  - 位置: src/features/edit/ui/edit_effects_panel.py:1534（方法定义 1524–1536；唯一导入在 1446–1449；信号连接在 1483–1485）
  - 证据:
    ```python
    def _on_motion_graphic_generator_double_clicked_by_id(self, template_key: str) -> None:
        template_id = str(template_key).split(":", 1)[-1]
        template = self._motion_graphic_registry.get(template_id)
        if template is None:
            return
        self.info_label.setText(
            I18N.tr(
                "edit.effects.mg_section_hint",
                "{name}: drag to the timeline to create a generator clip; "
                "adjust it in the MG editor",
                name=template_display_name(template),   # ← NameError
        )
    ```
    而该名字仅在 `_append_motion_graphic_generators` 内局部导入（1447–1449）：
    ```python
        from src.features.teaching_animation.core.content_localization import (
            template_description, template_display_name,
        )
    ```
  - 影响: 全文件模块级 import（56–84 行）不含 `template_display_name`（grep 确认仅 1448/1453/1454/1466/1476/1534 出现）。用户双击任一 MG 生成器卡片（启动即由模板库加载，卡片必然存在）即抛 `NameError`；槽内未捕获异常在 PyQt6 默认行为下打印回溯并 qFatal 中止进程（全仓库未安装自定义 `sys.excepthook`，grep 确认），即整应用崩溃；即使异常被吞，提示功能也从未生效。
  - 建议: 将 `template_display_name`/`template_description` 提升为模块级导入（或在该方法内局部导入）；补充对 `_on_motion_graphic_generator_double_clicked_by_id` 的回归测试。
  - 置信度: 高

- [AUD-19801] [Major] MG 悬停预览的 ToolTip 弹窗与 QMovie 均无父对象，卡片在悬停期间被销毁时留下孤儿窗口和持续解码的 GIF
  - 位置: src/features/edit/ui/edit_effects_panel.py:270–300
  - 证据:
    ```python
    def _show_preview(self) -> None:
        if self._preview_popup is not None or self._preview_path is None:
            return
        movie = QMovie(str(self._preview_path))
        ...
        popup = QLabel(None, Qt.WindowType.ToolTip)
        ...
        popup.setMovie(movie)
        self._preview_popup = popup
        self._preview_movie = movie
        movie.start()
        popup.show()
    ```
    清理只依赖 `leaveEvent`/`closeEvent`（258–268 行）；`_populate_video_effects` 在 `tree.clear()`（1422）前未通知卡片。
  - 影响: 搜索输入每击键、收藏切换、注册表刷新都会 `tree.clear()` 销毁卡片；此时鼠标仍悬停（如在搜索框打字），`leaveEvent` 不会送达已销毁的控件，无父对象的 ToolTip 窗口持续显示并解码 GIF，直到 Python 侧包装对象被 GC（非确定性）；`closeEvent` 对嵌入树中的子控件在正常销毁路径中通常不会被调用。
  - 建议: 给 popup/movie 设置父对象（或 `WA_DeleteOnClose`），并在卡片析构（`destroyed`/重写 `hideEvent` 兜底）时调用 `_hide_preview`；面板级重建前先遍历关闭所有悬停预览。
  - 置信度: 中（泄漏路径确凿；持续时长取决于 GC 时机）

- [AUD-19802] [Major] 注册表监听器在调用方线程内同步重建 Qt 控件，无 UI 线程亲和性保护；注册表侧还会静默吞掉监听器异常
  - 位置: src/features/edit/ui/edit_effects_panel.py:621–623、1492–1495；对照 src/features/teaching_animation/core/motion_graphic_generator_registry.py:57–63
  - 证据:
    ```python
    # edit_effects_panel.py
    self._motion_graphic_registry_unsubscribe = self._motion_graphic_registry.subscribe(
        self._refresh_motion_graphic_generators
    )
    ...
    def _refresh_motion_graphic_generators(self) -> None:
        """Refresh the palette after an AI/user template is registered."""
        if hasattr(self, "video_effects_tree"):
            self._populate_video_effects(self.video_effects_tree)
    ```
    ```python
    # motion_graphic_generator_registry.py — 监听器在调用方线程同步执行
        for listener in listeners:
            try:
                listener()
            except Exception:
                # A palette refresh must never break template registration.
                pass
    ```
  - 影响: 注册表以 RLock 设计为可跨线程调用；`register()`/`clear()` 直接在调用线程回调监听器，而监听器立即操作 QTreeWidget/QTreeWidgetItem。`src/features/teaching_animation/ai/service.py:242`（AI 会话 `promote_html_template(publish_to_effects=True)`）是注释明示的 AI 注册路径，一旦在 worker 线程执行即构成“非 UI 线程操作控件”铁律违例（崩溃风险）。已核实的现有调用点（editor_window 菜单槽、module 启动、file_controller 工程恢复）均在 UI 线程，故为结构性风险而非现行必崩。此外注册表 `except Exception: pass` 会掩盖监听器内任何错误（含日志缺失）。
  - 建议: 面板侧将回调改为投递到 UI 线程（`QTimer.singleShot(0, ...)` 或 `QMetaObject.invokeMethod` QueuedConnection）；注册表侧至少 `logger.debug(..., exc_info=True)`。
  - 置信度: 中（同步分发与直接重建为事实；存在非 UI 线程调用者未逐线程溯源确认）

- [AUD-19803] [Major] 搜索无防抖：每击键全量销毁并重建整棵 Video 树，且每个效果重复栅格化两次 SVG 图标并做逐项文件系统 IO
  - 位置: src/features/edit/ui/edit_effects_panel.py:1872–1892（每击键重建）；1363–1409（逐效果徽标解析 + 双重图标渲染）
  - 证据:
    ```python
    def _on_search_changed(self, text: str) -> None:
        if not text:
            self._populate_video_effects(self.video_effects_tree)
            return
        matching_effects = self._registry.search(text)
        self.video_effects_tree.clear()          # 每击键全量销毁
        ...
        self._build_effect_section(self.video_effects_tree, "filter", matching_effects)
        self._append_motion_graphic_generators(self.video_effects_tree, filter_text=text)
    ```
    ```python
            effect_item.setIcon(0, _effect_preview_icon(effect_def.effect_type))   # 1388
            ...
            card = _EffectPreviewButton(
                effect_def.effect_type,
                effect_def.name,
                effect_def.description,
                _effect_preview_icon(effect_def.effect_type),                      # 1401 重复渲染
    ```
  - 影响: `textChanged` 每字符触发一次完整重建：`tree.clear()` 销毁全部卡片/区段控件，每个效果执行 `resolve_execution_capability` + `get_effect_catalog().is_known()`，`_effect_preview_icon`（QSvgRenderer+QPainter 栅格化 + `path.is_file()` stat）对同一效果执行两次，MG 区段还重新创建 QMovie 读首帧。效果库与 MG 模板较多时输入明显卡顿，违背项目 4K 实时性能基准对 UI 响应性的要求。
  - 建议: 搜索加 ~200ms 防抖（QTimer）；`_effect_preview_icon` 结果按 effect_type 缓存（一次渲染两处复用）；徽标解析结果在单次重建内按 effect_type 去重。
  - 置信度: 高（代码事实确凿；卡顿幅度随效果数量变化）

- [AUD-19804] [Suggestion] `search_input.clear()` 程序化更新无状态锁，`textChanged` 重入导致 `_populate_video_effects` 双重全量重建（违反“程序化更新控件须状态锁”铁律）
  - 位置: src/features/edit/ui/edit_effects_panel.py:2053–2061（`clear()`）、1294–1303（`_toggle_search_visibility`）、1872–1877（信号处理器）
  - 证据:
    ```python
    def clear(self) -> None:
        """Clear the effects panel state."""
        self._selected_clip_id = None
        self._selected_subtitle_clip_id = None
        self.search_input.clear()                                  # 触发 textChanged("")
        self._populate_video_effects(self.video_effects_tree)      # 第二次重建
    ```
  - 影响: 搜索框有文本时调用 `clear()`：`search_input.clear()` 触发 `_on_search_changed("")` 已做一次全量重建，随后 2058 行再重建一次（双倍销毁/创建抖动）；`_toggle_search_visibility` 收起搜索时同样经由信号触发一次隐藏的全量重建。行为幂等无数据错误，但属铁律明示的防递归缺失并浪费重建。
  - 建议: 引入 `self._updating_search = True` 状态锁包裹程序化 `clear()`，或 `textChanged` 连接改为 `blockSignals` 模式。
  - 置信度: 高

- [AUD-19805] [Suggestion] `effect_favorites` 外部持久化配置值未验证类型/元素，损坏配置可使面板初始化抛 TypeError
  - 位置: src/features/edit/ui/edit_effects_panel.py:628–638
  - 证据:
    ```python
    favorites_list = self._config_manager.get("effect_favorites")
    if favorites_list is None:
        return set()
    return set(favorites_list)
    ```
  - 影响: 配置文件为外部数据（手改/损坏/迁移残留）。若值为 int 等不可迭代对象，`set(...)` 在面板构造时抛 `TypeError`，阻断 Edit 页创建；若为字符串 `"abc"` 则静默得到 `{'a','b','c'}` 垃圾收藏。违反“外部数据须验证”铁律。
  - 建议: 校验 `isinstance(favorites_list, list)` 且元素为 str，否则回退空集并记 warning。
  - 置信度: 高

- [AUD-19806] [Suggestion] 效果徽标解析的 `except Exception` 静默吞错且无日志（有 noqa 说明意图，但零可观测性）
  - 位置: src/features/edit/ui/edit_effects_panel.py:1363–1377
  - 证据:
    ```python
            try:
                from src.features.shared.effects import (
                    get_effect_catalog, resolve_execution_capability,
                )
                execution = resolve_execution_capability(...)
                restriction_code, restriction_message = \
                    execution.delivery_restriction()
            except Exception:  # noqa: BLE001 — 徽标失败不影响列表
                restriction_code, restriction_message = "", ""
    ```
  - 影响: 交付限制徽标链路（导入/目录/解析）任何真实错误都被无痕吞掉，`（仅预览）` 标注可能整体静默失效，无法与“效果确实无限制”区分，排障无线索。
  - 建议: 保留兜底但补 `logger.debug("delivery badge failed for %s", effect_type, exc_info=True)`。
  - 置信度: 高

- [AUD-19807] [Suggestion] 局部变量注解引用未导入的 `CaptionStylePreset`（静态/类型检查错误；PEP 526 下运行时不求值故不崩溃）
  - 位置: src/features/edit/ui/edit_effects_panel.py:1815
  - 证据:
    ```python
        grouped: Dict[str, List[CaptionStylePreset]] = {}
    ```
    （`_populate_titles` 内；文件顶部及函数内均无该名字的导入，grep 确认全文件仅此一处。）
  - 影响: 函数内局部注解不求值，运行时安全；但 mypy/pyright/pyflakes 报 undefined-name，误导后续维护者在模块/类层级复用该注解时引入真实 NameError。
  - 建议: 在方法内局部导入 `CaptionStylePreset`（与 1795–1801 行其他局部导入一致）或改用 `Any`/字符串注解。
  - 置信度: 高

- [AUD-19808] [Suggestion] `_reflow` 仅对 `range(3)` 设置列 stretch，tile 模式允许 4 列时第 4 列不参与拉伸分布
  - 位置: src/features/edit/ui/edit_effects_panel.py:516–532
  - 证据:
    ```python
    def _desired_columns(self) -> int:
        ...
        max_columns = 4 if self._tile_mode else 3
        return max(1, min(max_columns, (width + 5) // card_width))

    def _reflow(self) -> None:
        desired = self._desired_columns()
        ...
        for column in range(3):
            self.grid.setColumnStretch(column, 1 if column < desired else 0)
    ```
  - 影响: 动画 tile 模式且面板足够宽时 `desired == 4`，卡片被放入第 4 列（`index % desired == 3`）但该列 stretch 未被设置（保持默认 0），前三列扩张而第 4 列收缩至最小宽度，同一行卡片宽度不一致。
  - 建议: `for column in range(4)`（或 `range(self._desired_columns() + 1)`）统一设置 stretch。
  - 置信度: 高

- [AUD-19809] [Suggestion] 大量硬编码中文 UI 字符串与颜色绕过 I18N/主题令牌；上下文菜单用硬编码标签页索引判断转场页
  - 位置: src/features/edit/ui/edit_effects_panel.py:1086/1095/1104（`addRow("入场时长", …)` 等）、1112（`color: #9aa0a6`）、1851/1857（“请先在时间线中选中…”“已应用字幕特效：…”）、2043；797（`self.tabs.currentIndex() == 1`）
  - 证据:
    ```python
        controls_layout.addRow("入场时长", self.caption_effect_in_frames)
        ...
        controls_layout.addRow("出场时长", self.caption_effect_out_frames)
        ...
        titles_hint.setStyleSheet("color: #9aa0a6; font-size: 10px;")
    ```
    ```python
        self._show_library_context_menu_for_type(
            "",
            global_pos,
            is_transition=self.tabs.currentIndex() == 1,
        )
    ```
  - 影响: 同面板其余文案均走 `I18N.tr`，这批硬编码使英文/其他语言环境下混排中文；`#9aa0a6` 脱离 `C.*` 主题令牌，暗/亮主题切换不一致；标签页索引 1 为隐式契约，插入新标签页即静默错路由。
  - 建议: 文案迁移至 `I18N.tr` 并补翻译键；颜色改用 `C.TEXT_HINT` 类令牌；用 `self.tabs.currentWidget() is self.transitions_tree` 判断。
  - 置信度: 高

- [AUD-19810] [Suggestion] `_start_effect_drag` 为死代码（已被注入式 `custom_start_drag` 取代）
  - 位置: src/features/edit/ui/edit_effects_panel.py:1975–2005
  - 证据:
    ```python
    def _start_effect_drag(self, supported_actions: Qt.DropAction) -> None:
        """
        Start drag operation for effect item.

        This is called by the tree widget when dragging starts.
        """
        selected_items = self.video_effects_tree.selectedItems()
    ```
    （grep 确认全文件无任何调用/连接；实际拖拽由 `_inject_drag_support` 注入的 `custom_start_drag`（2148–2181）与卡片 `drag_requested` 路径承担。）
  - 影响: 31 行与现行实现重复且注释声称“tree 会调用它”具有误导性；且此旧版未区分 `MIME_TYPE_GENERATOR`，若被误复用会使生成器拖拽错走效果附加路由。
  - 建议: 删除该方法（2064–2066 行遗留注释一并清理）。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：纯 QtWidgets 面板，无 ModernGL/OpenGL 调用（UI 线程铁律不适用）；QMovie 采用惰性创建 + `CacheNone` + 视口裁剪（`_MAX_ACTIVE_TRANSITION_PREVIEWS`）+ `hideEvent`/切页释放，转场预览资源管理良好
- GLSL·HDR 正确性：不涉及（无 GLSL、无 HDR 像素数据路径）
- 状态提交一致性：面板对时间线零直接写——应用效果/转场/字幕特效/标题模板全部经 `effect_add_requested`/`transition_apply_requested`/`caption_effect_apply_requested`/`title_template_selected` 信号交由控制器命令路径（undo/redo 归接收方）；收藏与转场预览偏好即时经 ConfigManager 持久化，且 `save_config` 先合并磁盘再写 dirty keys（已核实 config_manager.py:616–641），多实例无互踩
- 异常与取消路径（除 AUD-19800/19802/19806 外）：`_load_transition_preview_mode/_size` 对非法偏好值有白名单回退；`menu.exec` 后动作判空；拖拽 payload 组装路径无裸异常
- 输入验证（除 AUD-19805 外）：MIME 解码用 utf-8 显式 decode；`_set_transition_preview_size`/`set_preview_size` 均校验预设键；模板 id 经 registry.get 判空
- 线程安全（除 AUD-19802 外）：面板自身仅在 UI 线程被调用，QTimer.singleShot(0) 合并同步事件（`_transition_preview_sync_scheduled` 防重入标志使用正确）
- 类型注解：公共方法签名均有类型注解（个别事件参数 `event`/`pos` 未标注 QMouseEvent/QPoint，属可选改进，不计发现）

## src/features/edit/ui/edit_info_panel.py (行数: 377)

### 发现

- [AUD-19811] [Suggestion] `_format_frame_time` 用 `int()` 截断构造时基，分数帧率（23.976/29.97）下时码显示漂移
  - 位置: src/features/edit/ui/edit_info_panel.py:75–77
  - 证据:
    ```python
        hours, remainder = divmod(frame_number, int(frame_rate * 3600.0))
        minutes, remainder = divmod(remainder, int(frame_rate * 60.0))
        seconds, frames = divmod(remainder, max(1, int(round(frame_rate))))
    ```
  - 影响: 29.97 fps 时 `int(29.97*3600)`=107891（浮点 107891.999…截断）、`int(29.97*60)`=1798、`round(29.97)`=30，三个基数不一致；长片段下 HH:MM:SS:FF 相对真实帧号可漂移数帧至秒级，`frames` 字段按 30 归一化与实际 29.97 不符（非丢帧时码）。仅影响检查器显示，不影响数据。
  - 建议: 统一按 `frame_number / frame_rate` 换算总秒数后再 divmod，或用 `round` 保持基数一致；如需精确时码引入丢帧算法。
  - 置信度: 高（截断数学确凿；用户可见影响为显示级）

- [AUD-19812] [Suggestion] `_value_labels` 只写不读（死状态），且以显示文本为键，跨区段同名标签互相覆盖
  - 位置: src/features/edit/ui/edit_info_panel.py:88、181、375
  - 证据:
    ```python
        self._value_labels: dict[str, QLabel] = {}
    ...
        self._value_labels.clear()
    ...
            self._value_labels[label_text] = value
    ```
    （grep 全 src 确认除本文件这三处外无任何读取。）
  - 影响: 面板没有任何消费者，属预留死状态；若未来启用原地更新，"Name" 同时出现在 Clip/Media/Timeline 区段（194/229/153 行），后写覆盖前写会更新错目标行。
  - 建议: 删除该字典，或改为 `(section_title, label_text)` 二元组键并提供公共更新接口。
  - 置信度: 高

### 通过项
- 线程安全：无自定义信号、无定时器、无跨线程状态；纯被动展示控件，仅 UI 线程经 set_clip/set_asset/set_timeline 更新
- GPU 资源生命周期：不涉及任何 GPU/ModernGL 资源
- GLSL·HDR 正确性：不涉及（只读展示 metadata，无像素处理，无 clamp 需求）
- 信号防递归：面板不发出任何信号，无递归面
- 状态提交一致性：纯只读检查器，不产生 undo/redo/revision 状态，无持久化写入
- 异常与取消路径：全部格式化函数以窄 `except (TypeError, ValueError)` 包裹并给出 "—" 回退（`_format_duration` 还先 `max(0.0, …)` 防负值）；无宽泛 except
- 输入验证：对缺失/异型 metadata 防御充分（`_present`/`_text` 过滤 None/空/unknown/n-a、Fraction 型 fps 经 num/den 分支、`_timeline_fps` 兜底 24.0）
- 性能模式：`_clear_content` 用 `deleteLater` 批量清理，无每帧路径，无热点
- 类型注解：完整（`from __future__ import annotations` + 全方法注解），i18n 全部经 `I18N.tr`，无硬编码颜色（统一用 `C.*`）
