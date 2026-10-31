# batch-309 审查报告 — src/features/teaching_animation/ui//

编号区间：AUD-30900–AUD-30999。审查日期：2026-09-28。
审查对象：`src/features/teaching_animation/ui/motion_graphic_editor_window.py`（3,017 行，全文通读）。
跨文件核对（只读）：`ui/editor_controller.py`、`ui/layer_tree_panel.py`、`ui/scene_timeline.py`、`ui/html_browser_preview.py`、`ui/mg_keyframes_panel.py`、`ui/scene_inspector.py`、`core/commands.py`、`core/model.py`、`core/html_page_deck.py`、`core/html_layer_commands.py`、`render/scene_renderer_router.py`、`render/svg_renderer.py`。

---

## src/features/teaching_animation/ui/motion_graphic_editor_window.py (行数: 3,017)

### 发现

- [AUD-30900] [Critical] 删除最后一页未捕获模型 ValueError，Qt 槽内未处理异常可致进程中止
  - 位置: src/features/teaching_animation/ui/motion_graphic_editor_window.py:1169
  - 证据:
    ```python
    def _remove_page_requested(self, page_id: str) -> None:
        self.editor_controller.execute(RemovePageCommand(str(page_id)))
        frame = min(self.editor_controller.current_frame,
                    self.editor_controller.scene.duration_frames - 1)
    ```
    而 `core/model.py:751-752`：`if len(self.pages) == 1: raise ValueError("a scene must keep at least one page")`；`ui/layer_tree_panel.py:577-581` 的页头右键菜单无条件提供 "Remove Page"。
  - 影响: 单页场景（新建默认即单页）右键 Remove Page → `with_page_removed` 抛 ValueError → 经 `execute` 穿入 Qt 信号槽 → PyQt6 对槽内未处理异常默认调用 qFatal 中止进程，编辑器直接崩溃。同文件 `_move_elements_to_page`(1141)、`_reparent_element`(1965) 均有 try/except ValueError 防护，唯独此处缺失。
  - 建议: 与 `_move_elements_to_page` 一致地 `try/except ValueError` 转为 statusBar 提示；同时可在 layer_tree 菜单对最后一页禁用 Remove Page。
  - 置信度: 高

- [AUD-30901] [Major] `save()` 无保护调用宿主 saver 与缓存协调器，Ctrl+S / 关闭保存时 I/O 异常直接穿入 Qt 槽
  - 位置: src/features/teaching_animation/ui/motion_graphic_editor_window.py:2973
  - 证据:
    ```python
    def save(self) -> None:
        self._scene_store.register(self.editor_controller.scene)
        if self._saver is not None:
            self.editor_controller.save(self._saver)
        else:
            self.editor_controller.mark_clean()
        from ..scene_cache import get_shared_scene_cache_coordinator
        get_shared_scene_cache_coordinator().schedule(self.editor_controller.scene)
    ```
    `editor_controller.save`（editor_controller.py:337-339）直接 `saver(self._scene)`；宿主 saver 落盘可抛 OSError。对比同文件 `_start_full_preview_cache`(1523-1528) 对同一 `coordinator.schedule` 有 `except (ImportError, RuntimeError, TypeError, ValueError)` 防护，且 `_render_video_cache`(1468-1473) 对回调有 BLE001 防护。
  - 影响: 磁盘满/权限错误/网络盘失联时，Ctrl+S、`_render_video_cache` 前置保存或 closeEvent 的 Save 分支抛未处理异常 → 崩溃，且脏标记未恢复、用户丢失保存时机。
  - 建议: saver 与 `coordinator.schedule` 均包 try/except（OSError、RuntimeError），失败时 statusBar 提示并保持 `is_dirty`。
  - 置信度: 高（代码路径确定；崩溃表现取决于 PyQt excepthook 配置）

- [AUD-30902] [Major] 关闭时 DevTools 保存路径：apply 失败仍显示成功并强制 save+close，未应用更改被静默丢弃；失败分支滞留 `_close_after_devtools_apply`
  - 位置: src/features/teaching_animation/ui/motion_graphic_editor_window.py:2223
  - 证据:
    ```python
        self._apply_html_source(source)          # 内部吞掉 TypeError/ValueError，无返回值
        if not self.html_source_editor.has_pending_draft:
            self.html_devtools.set_has_unapplied_changes(False)
        self.statusBar().showMessage(
            I18N.tr("motion_graphic.status.live_dom_applied", "DevTools DOM/CSS applied to the HTML layer; undo is available"), 5000,
        )
        if self._close_after_devtools_apply:
            self._close_after_devtools_apply = False
            self.save()
            QTimer.singleShot(0, self.close)
    ```
    且早退分支 2180-2184（浏览器未就绪返回 ""）与 2190-2205（构造失败）均不清除 closeEvent(2999-3003) 设置的 `self._close_after_devtools_apply = True`。
  - 影响: (a) 用户在 closeEvent 选择 Save 且 DevTools 有未应用更改时，若 `_apply_html_source` 内部失败（sanitizer ValueError），错误消息被随后的成功消息覆盖，窗口仍被 save+关闭，DevTools 更改静默丢失（数据丢失）；(b) 早退分支滞留标志后，用户之后任意一次手动 DevTools Apply 会意外触发 save+关闭整个编辑器。
  - 建议: `_apply_html_source` 返回 bool 并据此分支；所有失败/早退路径统一清除 `_close_after_devtools_apply` 并保持窗口打开。
  - 置信度: 高（逻辑确定；触发条件依赖 sanitizer/浏览器就绪状态）

- [AUD-30903] [Major] 拖拽期间每个 mouse-move 触发全量 `_refresh_scene`（忽略 diff），图层树/关键帧面板/原生 overlay 全部重建
  - 位置: src/features/teaching_animation/ui/motion_graphic_editor_window.py:427
  - 证据:
    ```python
    self.editor_controller.subscribe(lambda scene, _diff: self._refresh_scene(scene))
    ```
    拖拽路径 `_element_dragged`(1665-1682) 每次 mouse-move 调 `preview_element`，而 `editor_controller.preview_element`（editor_controller.py:140-153）末尾 `self._notify(diff)`；`_refresh_scene` 进而调用 `layer_tree.set_scene`（layer_tree_panel.py:251-256 每次 `build_layer_rows` + `_rebuild` 全量重建）、`keyframes_panel.set_scene`、storyboard `set_scenes`、native overlay 字符串重组等。
  - 影响: 多元素/多页场景中拖拽一个图层时，每个鼠标移动事件做 O(元素数) 的行重建 + 关键帧轨道重建 + HTML overlay 重新生成，拖拽明显卡顿，违背流畅直接操纵与实时性能基准。
  - 建议: 利用被丢弃的 `_diff`（如仅 transform 变更时只更新 scene_view/时间轴游标），或对 preview 类通知节流/合并（QTimer coalesce），命令提交时才做全量刷新。
  - 置信度: 高（调用链确定；卡顿程度随场景规模，标中）

- [AUD-30904] [Major] `_apply_live_dom_source` 的异常保护放在命令构造而非 `execute`，DevTools Apply 的 `apply()` 抛 ValueError 未捕获
  - 位置: src/features/teaching_animation/ui/motion_graphic_editor_window.py:2188
  - 证据:
    ```python
        try:
            html_command = ReplaceHtmlSourceCommand(str(source))
        except (TypeError, ValueError) as exc:
            ...
        if native_command is not None:
            self.editor_controller.execute(CompositeCommand(
                (html_command, native_command), label="DevTools Apply"))
    ```
    `ReplaceHtmlSourceCommand` 是纯字段 dataclass（html_layer_commands.py:124-131，构造无校验），真正校验在 `apply`（132-138 行 `raise ValueError("HTML source cannot be empty")` 等及 sanitizer 内部），而 2210 行的 `execute(...)` 在 try 块之外。
  - 影响: 活 DOM 源触发 apply 阶段校验/sanitizer 异常时，异常穿入 DevTools Apply 按钮槽 → 未处理异常 → 崩溃风险。现有 try 实际是死防护。
  - 建议: 把 `execute(CompositeCommand(...))` 纳入 try/except（对齐 `_apply_html_source` 2065-2070 的写法），或让命令在构造期完成校验。
  - 置信度: 中（路径确定；apply 抛错概率取决于 sanitizer 对活 DOM 的容忍度）

- [AUD-30905] [Suggestion] `_request_html_preview` 在线程已停但 finished 事件未送达时不清除 pending，陈旧请求稍后被重新启动
  - 位置: src/features/teaching_animation/ui/motion_graphic_editor_window.py:2818
  - 证据:
    ```python
        if thread is not None and thread.isRunning():
            self._html_preview_pending = request
            thread.requestInterruption()
            ...
            return
        if thread is not None:
            thread.deleteLater()
            self._html_preview_thread = None
        self._start_html_preview(*request)   # 未清 self._html_preview_pending
    ```
    随后 `_on_html_preview_finished`(2865-2874) 会把残留的旧 pending 再启动一次（结果因 generation 过期在 2849 被忽略）。
  - 影响: 一次多余的过期场景/帧渲染（浏览器捕获开销），无用户可见错误；属竞态卫生问题。
  - 建议: 直接启动新请求前 `self._html_preview_pending = None`。
  - 置信度: 高（逻辑成立，影响轻微）

- [AUD-30906] [Suggestion] `_stop_html_preview` 在 UI 线程无限期 `thread.wait()`，取消链路失效时关闭冻结
  - 位置: src/features/teaching_animation/ui/motion_graphic_editor_window.py:2888
  - 证据:
    ```python
        if thread is not None and thread.isRunning():
            thread.requestInterruption()
            thread.wait()
    ```
  - 影响: CancellationToken 依赖浏览器后端及时拆解（见 106-111 行注释）；若后端挂起，closeEvent 在 UI 线程无限期阻塞，界面冻结。
  - 建议: `thread.wait(timeout_ms)`，超时则记录并放弃等待（deleteLater 由 Qt 兜底）。
  - 置信度: 中

- [AUD-30907] [Suggestion] `QTimer.singleShot` 回调未绑定窗口生命周期，窗口销毁后触发会对已删除 wrapper 抛 RuntimeError
  - 位置: src/features/teaching_animation/ui/motion_graphic_editor_window.py:1055
  - 证据:
    ```python
        QTimer.singleShot(50, self._restore_front_after_application_activate)
    ```
    （同类：1095 行 `QTimer.singleShot(0, self._restore_front_after_refresh)`、2232 行 `QTimer.singleShot(0, self.close)`）
  - 影响: `WA_DeleteOnClose` 删除 C++ 对象后若定时器仍触发（应用切换后 50ms 内关闭的窄窗口），`isVisible()`/`raise_()` 对已删除 wrapper 抛 RuntimeError，定时器回调内未处理 → 崩溃风险。
  - 建议: 使用 `QTimer.singleShot(ms, self, callback)`（带接收者的重载，Qt6.7+/PyQt6 支持 receiver 形式）或改用成员 QTimer 并 setParent。
  - 置信度: 低（触发窗口极窄，但模式在多处重复）

- [AUD-30908] [Suggestion] 类型注解缺失/不精确
  - 位置: src/features/teaching_animation/ui/motion_graphic_editor_window.py:2156
  - 证据:
    ```python
    def _build_native_property_command(self, live_native_overlay: str):
        """Translate native-overlay DevTools edits into element commands.
    ```
    另 2693 行 `def _apply_keyframe_edits(self, edits: list) -> None:` 的 `list` 未参数化。
  - 影响: 与项目强类型约定不符；返回值结构（tuple[CompositeCommand | None, tuple[str, ...]]）只能靠 docstring 推断。
  - 建议: 补 `-> tuple[CompositeCommand | None, tuple[str, ...]]`；`edits: list[Any]` 或更精确的轨道编辑类型。
  - 置信度: 高

- [AUD-30909] [Suggestion] 869-870 行 import 续行缩进异常
  - 位置: src/features/teaching_animation/ui/motion_graphic_editor_window.py:869
  - 证据:
    ```python
            from ..render.html_native_overlay import (
        native_animation_css, native_overlay_css_variables, native_overlay_markup)
    ```
  - 影响: 括号内合法但续行缩进低于语句头，违反常规风格，干扰阅读与格式化工具。
  - 建议: 续行与 `from` 对齐（同 2768-2769 行的正确写法）。
  - 置信度: 高

### 通过项
- 线程安全：HTML 预览渲染完全在 `_HtmlPreviewThread`（QThread）中执行，结果经 queued 信号 + generation 校验（2849/2860）回到 UI 线程后才触碰 Qt 控件；UI 线程无 OpenGL 调用（`editor_controller.render` 为同步 CPU SVG/XML 路径，svg_renderer.py 无 moderngl/QOpenGL 引用；HTML 捕获走 headless 后端在 worker 内）
- GPU 资源生命周期：`_HtmlPreviewThread.run` 在 finally 中 best-effort `renderer.cleanup()`（134-141）；本文件不直接持有 ModernGL/纹理对象；线程关闭路径 wait+deleteLater 配对（2884-2892）
- GLSL·HDR 正确性：不适用——文件无 GLSL 代码、无 HDR 色彩数学、无 clamp(0.0,1.0) 类操作
- 信号防递归：`_set_selection` 单一 chokepoint 以 `source` 抑制回声（1583-1588）；layer_tree/keyframes/inspector 的 `set_selection` 均有 `_updating` 状态锁（layer_tree_panel.py:290、mg_keyframes_panel.py:206、scene_inspector.py:367）；`SceneTimeline.set_frame` blockSignals（scene_timeline.py:253-255）；`_syncing_browser_page` 防浏览器页面回声（1256-1271、900-905）
- 状态提交一致性（除 AUD-30902/30904 外）：所有结构化编辑均经命令栈（undoable）；DevTools authored+native 组合为单 CompositeCommand 单步撤销（2206-2216）；拖拽 begin_preview/commit_preview 配对（1645-1663）；`set_frame` 静默无回声（editor_controller.py:118-120），frame_changed 手动同步三处游标
- 异常与取消路径（除已列发现）：HTML 预览 worker 边界 BLE001 转 renderFailed 信号（131-133）；`_render_video_cache`/`_start_full_preview_cache` 对宿主回调有防护（1468-1473、1505-1510）；`_export_html_document` finally 关闭 service（1442-1443）；`_import_html_triggered` 捕获 OSError/UnicodeError 并以 utf-8-sig 读取（2088-2115）
- 输入验证：页面时长 getInt 限 1..36000（1195-1201）；页时长拖拽 `max(1, int(...))`（1212）；预设加载 PresetError 捕获（2539-2545）；模板实例化捕获 RuntimeError/TypeError/ValueError（2504-2514）；`fps` 除法安全——模型不变量保证 `fps_num/fps_den > 0`（model.py:421-422），`detect_page_count` 恒返回 ≥1（html_page_deck.py:382-384），`PageDeckTiming.__post_init__` 完整校验正数/长度（324-339）；`set_page` 内部钳位，`set_page(10**9)` 安全（html_browser_preview.py:292-293）；`read_authored_document` 未就绪时回调 ""（409-412）
- 性能模式（除 AUD-30903 外）：`set_document` 以内容比较跳过重复装载（html_browser_preview.py:219-222）；`set_native_overlay`/`set_native_draw_tool` 变更短路（243-249、271-277）；`set_native_time` 单 style 写而非 DOM patch（251-269）；预览请求按 generation 合并只保留最新（2818-2836）
