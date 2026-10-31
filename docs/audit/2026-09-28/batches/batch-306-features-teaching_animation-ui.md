# batch-306 — src/features/teaching_animation/ui//

编号区间：AUD-30600–AUD-30699。审查日期：2026-09-28。
所有行号与证据均 Read 自仓库真实代码；跨文件事实（调用方、evaluator 钳制、AI 命令默认区间、
page-deck notify、窗口 closeEvent 等）均已逐一核验对应源文件。

---

## src/features/teaching_animation/ui/__init__.py (行数: 42)

### 发现

（无）

### 通过项
- 通过（无逻辑）：纯 re-export 包入口。`__all__` 中全部 20 个名字（EditorWindowRegistry、
  HTML_LAYER_ROW_ID、HtmlInspector、HtmlBrowserPreview、HtmlDevToolsPanel、LayerRow、LayerTreePanel、
  MotionGraphicEditorWindow、HtmlSourceEditor、PreviewBridge、SceneDiff、SceneEditorController、
  SceneInspector、SceneTimeline、SceneView、StoryboardPanel、WebEnginePreviewBridge、
  build_layer_rows、diff_scenes）均有对应的顶层 import，逐一核对无缺失、无拼写漂移。
- 模块 docstring 明确导入本包不创建 Qt 对象（延迟到显式实例化），无导入副作用。

---

## src/features/teaching_animation/ui/align.py (行数: 115)

### 发现

（无）

### 通过项
- 纯几何计算，无 Qt、无场景突变、无 I/O：无线程安全 / GPU 生命周期 / 异常取消面。
- 未知 mode 显式 `raise ValueError`（输入验证）；少于 2 个元素返回 `{}`，distribute 在恰好
  2 个元素时步长等于跨度、位置不变，与 docstring“≥3 才有内部间距”的约定一致。
- distribute 按中心排序、`step = (last-first)/(n-1)`（n≥2 已保证，无除零）。
- `build_alignment_updates` 跳过无位移元素、完整保留 width/height/scale_x/scale_y/rotation，
  与 `_box` 的 effective-box 约定一致；输出的 `{"transform": new_transform}` 形状可直连
  `UpdateElementCommand`。
- 类型注解完整（含返回 `dict[str, tuple[float, float]]` / `Mapping[str, SceneTransform]`）。

---

## src/features/teaching_animation/ui/asset_library_panel.py (行数: 744)

### 发现

- [AUD-30600] [Major] “重新生成预览”在 UI 线程同步渲染整段 GIF，期间界面冻结
  - 位置: src/features/teaching_animation/ui/asset_library_panel.py:732-739
  - 证据:
    ```python
    try:
        scene = load_preset_scene(record.manifest, record.folder)
        render_preset_gif(scene, record.folder / record.manifest.preview)
    except Exception as exc:  # noqa: BLE001 — surface any preview failure
        QMessageBox.warning(self, I18N.tr("motion_graphic.library.preview_failed",
                                          "Preview Failed"), str(exc))
        return
    ```
    `presets/preview.py:36-90` 的 `render_preset_gif` 为纯 CPU 同步路径：按 `scene.width ×
    scene.height`（可为 1920×1080/4K）渲染最多 24 帧（QSvgRenderer 光栅化 + PIL LANCZOS 缩放 +
    `optimize=True` 的 GIF 编码），无进度提示、无取消。
  - 影响: 单击工具按钮后主线程阻塞数秒（大分辨率场景更久），事件循环停摆——整个编辑器窗口
    无响应、无法取消，违背项目“重渲染不占 UI 线程”的基准。
  - 建议: 移入渲染任务队列/后台线程（完成后再回 UI 线程刷新缩略图），期间禁用按钮并可取消；
    或先落盘低分辨率预览。
  - 置信度: 高

- [AUD-30601] [Suggestion] 搜索每敲一键全量重建网格并重新解码每张 GIF 首帧，无图标缓存
  - 位置: src/features/teaching_animation/ui/asset_library_panel.py:303、606-621、522-534
  - 证据:
    ```python
    self._search_edit.textChanged.connect(lambda _text: self._refill())
    ```
    `_refill` 对每条记录执行：
    ```python
    preview = Path(record.preview_path) if record.preview_path else None
    item.setIcon(self._static_preview_icon(preview))
    ```
    而 `_static_preview_icon` 每次新建 `QMovie`、`jumpToFrame(0)` 并做 SmoothTransformation 缩放：
    ```python
    movie = QMovie(str(preview))
    movie.jumpToFrame(0)
    pixmap = movie.currentPixmap()
    if not pixmap.isNull():
        icon = QIcon(pixmap.scaled(
            self.grid.iconSize(), Qt.AspectRatioMode.KeepAspectRatioByExpanding, ...
    ```
  - 影响: 每次键入 = grid.clear + N 次 GIF 首帧解码 + N 次平滑缩放。当前内置库约 14 个包尚可，
    但用户库增长后搜索输入会出现可感卡顿（成本随库大小线性放大，且换列宽缩放也整列重算）。
  - 建议: 以 (preset_id, 图标尺寸) 为键缓存首帧 QIcon；或对 textChanged 做 150ms 防抖。
  - 置信度: 高（机制）；影响幅度随库规模变化。

- [AUD-30602] [Suggestion] `_regenerate_preview` 用宽泛 `except Exception` 把编程错误伪装成“预览失败”
  - 位置: src/features/teaching_animation/ui/asset_library_panel.py:735
  - 证据:
    ```python
    except Exception as exc:  # noqa: BLE001 — surface any preview failure
        QMessageBox.warning(self, I18N.tr("motion_graphic.library.preview_failed",
                                          "Preview Failed"), str(exc))
    ```
  - 影响: noqa 注释说明作者有意为之且异常有用户可见反馈（非静默），但 AttributeError/TypeError
    等代码缺陷也会被归并为“Preview Failed”，掩盖真实根因，违背“不得宽泛 except 掩盖领域错误”。
  - 建议: 收敛为领域异常（PresetError、OSError、ValueError——preview.py 已明确 `raise ValueError`
    于零帧场景），其余异常照常抛出或单独记日志。
  - 置信度: 高

- [AUD-30603] [Suggestion] `catalog` 属性缺少返回类型注解
  - 位置: src/features/teaching_animation/ui/asset_library_panel.py:454-457
  - 证据:
    ```python
    @property
    def catalog(self):
        """The unified package catalog (presets + HTML templates)."""
        return self._catalog
    ```
  - 影响: 本文件其余签名均带注解，此处为唯一缺口；`MgPackageCatalog` 类型在 172-173 行已可引用。
  - 建议: `def catalog(self) -> MgPackageCatalog:`（导入移至模块顶部或 TYPE_CHECKING 块）。
  - 置信度: 高

### 通过项
- 信号防递归：`_rebuild_categories` 用 `blockSignals` 保护 combo 程序化填充（471-481）。
- QMovie 生命周期正确：视口裁剪（`_sync_thumbnail_movies` 只为可见卡启动动画）、`_clear_thumbnail_movies`
  先 stop+deleteLater 再 `grid.clear()`（544-549, 603-604），hide/refill 均覆盖；定时器 parent 到 self，
  面板销毁时不会悬空触发。
- 事件过滤：Resize 走 `_retile_timer` 延迟一拍取最终 viewport 宽度，避免陈旧几何；搜索框 Escape
  消费事件并返回 True（383-399）。
- QSettings 读取有 int 解析容错 + 列数范围钳制（309-317）。
- 磁盘操作（import/export/delete）均捕获领域异常 `PresetError` 并给用户反馈；zip 导入在
  library 层已做路径穿越（绝对路径/`..`）与 128 MiB 大小校验（library.py:298-303, 310-314）——外部数据验证到位。
- `PresetSaveDialog` 空名由调用方校验（motion_graphic_editor_window.py:2610-2611 `if not payload["name"]: return`）。
- GPU/GLSL、HDR 维度不适用（本文件无渲染面）。

---

## src/features/teaching_animation/ui/editor_controller.py (行数: 346)

### 发现

- [AUD-30610] [Major] 自动打点写入的关键帧会被已存在的窄区间轨道静默钳制，渲染出错误动画
  - 位置: src/features/teaching_animation/ui/editor_controller.py:227-240（结合 180-189）
  - 证据:
    ```python
    existing = next((item for item in self._scene.animations
                     if item.element_id == element_id and item.property == property_name), None)
    if existing is None:
        lo = 0.0 if min_val is None else float(min_val)
        hi = max(1.0, abs(value)) if max_val is None else float(max_val)
        track = KeyframeTrack(property_name, min_val=lo, max_val=hi)
    ...
    track = KeyframeTrack.from_dict(existing.track.to_dict())
    track.add_keyframe(Keyframe(frame, value))
    ```
    `min_val/max_val` 只在“新建轨道”时生效。`_auto_key_transform` 为坐标传入宽区间
    （186 行 `wide = {"min_val": -1_000_000.0, "max_val": 1_000_000.0}`），但若该 (element, property)
    已存在窄区间轨道——AI 路径 `ai/commands.py:87` 对 `set_animation` 默认建 `[0.0, 1.0]`：
    ```python
    track = KeyframeTrack(property_name, min_val=float(args.get("min_val", 0.0)), max_val=float(args.get("max_val", 1.0)))
    ```
    则宽区间被忽略；评估侧 `render/evaluator.py:51` 经 `track.get_value(frame)` 取值，
    `src/shared/keyframe.py:205` `_clamp_range` 钳到 `[min_val, max_val]`。
  - 影响: 在 AI 建过的 "x" 轨道上拖拽元素并自动打点，关键帧原始值（如 x=850）被存入但求值时
    钳为 1.0——预览/导出的动画位置错误且无任何告警（数据未丢，行为静默错）。
  - 建议: `add_keyframe` 在 existing 轨道区间容不下 value 时扩展 min/max（或返回提示）；
    至少对 transform 属性复用 `mg_keyframes_model.property_range` 的宽区间重设轨道边界。
  - 置信度: 高（机制三处源码互证；触发前提是已存在窄区间同属性轨道）

- [AUD-30611] [Major] 一次拖拽 + 自动打点会拆成最多 6 条独立撤销记录，与“一手势一撤销”的既定约定相悖
  - 位置: src/features/teaching_animation/ui/editor_controller.py:172-177、187-189
  - 证据:
    ```python
    diff = self.execute(UpdateElementCommand(element_id, updates))
    # Auto-key (UX-14): ...
    if self._auto_key and original.transform != current.transform:
        self._auto_key_transform(element_id, original.transform, current.transform)
    ```
    ```python
    for name in ("x", "y", "scale_x", "scale_y", "rotation"):
        if getattr(before, name) != getattr(after, name):
            self.add_keyframe(element_id, name, frame, float(getattr(after, name)), **wide)
    ```
    每个 `add_keyframe` 内部各自 `self.execute(...)`（236/240 行）。同文件 `apply_track_edits`
    的 docstring 明确约定 “One batch becomes one undo entry, so a multi-keyframe drag is a single
    reversible edit”（250-252）。
  - 影响: x、y 同时移动的一次拖拽 = 元素更新 + x 打点 + y 打点共 3 步撤销，最多 6 步；每次
    execute 还各触发一轮 `_notify` 场景刷新。用户需按多次 Ctrl+Z 才回到拖拽前，且中途状态
    （元素已复位但关键帧残留）是可停留的非法混合态。
  - 建议: 把 `UpdateElementCommand` 与各 `AddAnimationCommand/UpdateAnimationTrackCommand`
    合并为一个 `CompositeCommand`（同 apply_track_edits 的做法），一次 execute、一次通知。
  - 置信度: 高

- [AUD-30612] [Suggestion] `commit_preview` 的字段快照遗漏 `layout` 与 `page_id`，预览可改但提交会静默丢弃
  - 位置: src/features/teaching_animation/ui/editor_controller.py:166-171（对照 49-53）
  - 证据:
    ```python
    updates = {
        "type": current.type, "z_index": current.z_index, "visible": current.visible,
        "transform": current.transform, "style": current.style, "content": current.content,
        "asset_ref": current.asset_ref, "template_id": current.template_id,
        "local_animation_tracks": current.local_animation_tracks,
    }
    ```
    而 `diff_scenes` 追踪的字段清单含 `"layout", "page_id"`（49-53 行）。`preview_element` 用
    `dc_replace(element, **normalized)` 可更新任意字段。
  - 影响: 当前调用方只传 `transform`（motion_graphic_editor_window.py:1672、1681），故为潜伏缺陷；
    一旦有代码以 preview/commit 路径预览 `layout`/`page_id`，提交时这两项被静默还原（预览所见
    与撤销记录不一致）。
  - 建议: 从 `current` 动态生成快照（或补齐 `getattr(current, "layout")`、`getattr(current, "page_id")`），
    并加断言防止字段清单再次漂移。
  - 置信度: 中（缺陷为潜伏态，现调用方未触发）

- [AUD-30613] [Suggestion] `SceneDiff.changed_fields` 用 `= None` 默认值 + `object.__setattr__` 修补 + type-ignore
  - 位置: src/features/teaching_animation/ui/editor_controller.py:23-30
  - 证据:
    ```python
    @dataclass(frozen=True)
    class SceneDiff:
        changed_element_ids: frozenset[str] = frozenset()
        changed_fields: Mapping[str, frozenset[str]] = None  # type: ignore[assignment]
        ...
        def __post_init__(self) -> None:
            if self.changed_fields is None:
                object.__setattr__(self, "changed_fields", {})
    ```
  - 影响: 类型系统上 `Mapping` 可为 None，调用方（或静态检查）无法信任该注解；绕过 frozen
    dataclass 的初始化路径也掩盖了“可变默认值”这一真实动机。
  - 建议: `changed_fields: Mapping[str, frozenset[str]] = field(default_factory=dict)`，去掉
    `__post_init__` 与 type-ignore。
  - 置信度: 高

- [AUD-30614] [Suggestion] `apply_track_edits` 的 `edits` 参数缺类型注解
  - 位置: src/features/teaching_animation/ui/editor_controller.py:242
  - 证据:
    ```python
    def apply_track_edits(self, edits) -> SceneDiff | None:
    ```
    docstring 已描述其为 `(element_id, property, track_or_None)` 序列。
  - 建议: `edits: Sequence[tuple[str, str, KeyframeTrack | None]]`。
  - 置信度: 高

### 通过项
- 状态提交一致性：execute/undo/redo 统一走“前值快照 → 栈操作 → clamp 帧 → diff → dirty → notify”，
  undo/redo 均正确置 dirty。
- `commit_preview` 先把场景恢复到 original 再执行命令，保证撤销基线是拖拽前状态；相等则不产生命令。
- `apply_track_edits` 先构建全部命令再一次性执行（单命令直执行 / 多命令 CompositeCommand），
  中途 `ValueError` 不会留下部分应用。
- `render()` 在 try/finally 中 `renderer.cleanup()`，异常路径不泄漏渲染器；每次调用独立
  prepare/cleanup，无跨调用状态。
- `save()` 先 saver 后置 clean，saver 抛异常时 dirty 保持 True（不会误标已保存）。
- `_notify` 迭代 `tuple(self._listeners)` 快照，监听器可在回调中安全增删。
- `set_frame`/`_clamp_current_frame` 对 0 长度 duration（min→-1→max(0,·)）等边界安全。
- `add_keyframe`/`apply_track_edits` 对不存在的 element 显式 `ValueError`（输入验证）。
- 线程边界：文档对象不可变、控制器仅 UI 线程持有，无跨线程可变共享；无 GPU/GLSL/HDR 面。

---

## src/features/teaching_animation/ui/editor_window_registry.py (行数: 57)

### 发现

- [AUD-30620] [Suggestion] 重开路径忽略传入的 scene 实例与新回调，正确性完全依赖“调用方永远传 store 当前实例”
  - 位置: src/features/teaching_animation/ui/editor_window_registry.py:21-27
  - 证据:
    ```python
    self.scene_store.register(scene)
    window = self._windows.get(scene.scene_id)
    if window is not None:
        window.show()
        window.raise_()
        window.activateWindow()
        return window
    ```
    同一 `scene_id` 已有窗口时，`saver`/`insert_callback`/`render_cache_callback` 被静默丢弃，
    窗口继续编辑其构造时的旧实例；传入的 `scene`（可能是由宿主重建的新实例）仅覆盖 store 注册表。
  - 影响: 若宿主以新实例（如项目重载后反序列化的同 id 文档）调用 open，被抬起的旧窗口仍编辑
    旧对象，其下次保存会把旧内容 register 回 store，悄悄覆盖新实例（stale-write）。当前调用方
    （module.py `_open_scene`，始终取 `get_shared_scene_store().get(scene_id)`）恰好满足假设，
    但该契约既未写在文档也未防御。
  - 建议: 重开路径比较传入 scene 与窗口当前 controller.scene 的身份，不同则驱动窗口切换文档
    （已有 `_scene_selected` 机制可用）；或至少在 docstring 声明“必须传 store 当前实例”的契约。
  - 置信度: 中（缺陷为契约脆弱性，现调用序列未触发）

### 通过项
- `unregister` 的窗口同一性（`is`）校验防止新窗口被旧窗口的注销事件误删（48-50）。
- `close_all` 迭代 `tuple(...)` 快照，close 期间回调 unregister 不会破坏迭代（55-57）。
- geometry 记忆/恢复对称（remember_geometry / open 中 restoreGeometry）。
- 无线程安全、GPU、GLSL/HDR 面（纯 UI 线程注册表）；`get` 返回类型注解完整。

---

## src/features/teaching_animation/ui/html_browser_preview.py (行数: 712)

### 发现

- [AUD-30630] [Suggestion] profile 先于 view 创建且同为 self 直接子对象，与注释声称的销毁顺序保证相矛盾；安全目前仅靠窗口显式调用 close()
  - 位置: src/features/teaching_animation/ui/html_browser_preview.py:160-171
  - 证据:
    ```python
    profile = QWebEngineProfile(f"topos-motion-graphic-{id(self)}", self)
    ...
    view = QWebEngineView(self)
    # Keep the page below its view in the QObject tree so the page is
    # destroyed before the profile when the preview widget is torn down.
    page = QWebEnginePage(profile, view)
    ```
    profile 是 self 的直接子对象且创建早于 view；page 是 view 的子对象（孙级）。若部件经父链
    析构（Qt 按 children 列表序删除，非 widget 重排时即创建序），profile 会在 view/page 之前
    被删——正是注释想避免的“profile 先于 page 死亡”情形。
  - 影响: 当前唯一兜底是 motion_graphic_editor_window.closeEvent:3006 显式调用
    `html_browser_preview.close()`（内部按正确顺序 deleteLater）。任何绕过 closeEvent 的销毁
    路径（如父窗口 deleteLater 而未派发 close、测试代码直接销毁）都可能触发 QtWebEngine
    “profile 释放时仍有 page 存活”的告警甚至关闭崩溃。
  - 建议: 在 ensure_browser 中把 profile 的创建移到 view/page 之后，或创建后
    `profile.setParent(...)` 调整顺序；使注释声称的顺序由对象树本身保证，而非依赖外部调用约定。
  - 置信度: 中（顺序矛盾由代码可证；实际崩溃取决于 Qt 版本与销毁路径）

- [AUD-30631] [Suggestion] `set_page` 对一次翻页发出两次 `pageChanged`（Python 立即发 + JS 桥异步回发）
  - 位置: src/features/teaching_animation/ui/html_browser_preview.py:292-299
  - 证据:
    ```python
    self._pending_page = max(0, min(int(index), self._page_count - 1))
    self._current_page = self._pending_page
    if self._view is not None and self._ready:
        self._view.page().runJavaScript(
            f"window.toposPageDeck && window.toposPageDeck.show({self._pending_page});"
        )
    self.pageChanged.emit(self._current_page)
    ```
    而 deck 的 `show()` 末尾无条件 `notify(current)`（core/html_page_deck.py:778），其
    （751 行）`if (bridge && bridge.notifyPageChanged) bridge.notifyPageChanged(index)` →
    `_on_bridge_page_changed`（707-709）再次 `pageChanged.emit`。加载完成路径同理双发
    （`_on_load_finished` → `set_page` + deck 引导 `show(0)`）。
  - 影响: 消费方 `motion_graphic_editor_window._browser_page_changed`（1254 起）会同步执行两轮
    set_frame/timeline/inspector 联动；结果幂等但产生双倍信号扇出，`_syncing_browser_page`
    防递归锁也要多进出一次，徒增刷新与日后引入非幂等消费者时的误动作风险。
  - 建议: `set_page` 不立即 emit，仅依赖桥回发确认；或记录上次已发页号、同值不重发。
  - 置信度: 高（双发机制两侧源码可证）

- [AUD-30632] [Suggestion] 冗余的函数内局部 import（json / QTimer 均已可用）
  - 位置: src/features/teaching_animation/ui/html_browser_preview.py:313-314、489-490
  - 证据:
    ```python
    if self._view is None or not self._ready:
        return
    import json
    literal = json.dumps(str(node_id), ensure_ascii=False)
    ```
    模块顶部第 11 行已有 `import json`；`resizeEvent` 内每次再 `from PyQt6.QtCore import QTimer`。
  - 影响: 无功能影响；属代码整洁度问题（每次 resize 触发一次局部 import 查找）。
  - 建议: 删除局部 import，统一用模块顶部导入。
  - 置信度: 高

### 通过项
- JS 注入安全（输入验证）：`select_node` 的 node_id 经 `json.dumps` 转义（314）、overlay 的
  markup/tool 同样（522-523）、数值全部经 `int()`/`float()` 或 `:.4f` 格式化后拼入脚本，
  无字符串直插漏洞；`setHtml` 绑定 about:blank 且 `LocalContentCanAccessFileUrls/RemoteUrls`
  均 False、无持久 Cookie。
- 桥接信号的消费端有输入验证：`_browser_text_edited` 先 `element_by_id` 判存在
  （motion_graphic_editor_window.py:1387-1391），伪造的 element_id 无法注入场景。
- 资源释放：`close()` 依 view → page → profile 顺序 deleteLater，`setDevToolsPage(None)`、
  `setPage(None)` 与 page/profile 删除均有 RuntimeError/Exception 防护；窗口 closeEvent:3006
  确认调用。
- 环境回退：offscreen QPA 与 WebEngine 缺依赖均有占位文案且不抛异常（142-158）。
- 事件过滤：滚轮消费 `accept()` + `return True` 正确；触控板 pixelDelta 回退处理合理；
  zoom 钳制 [0.1, 4.0]。
- `resizeEvent` 经 `QTimer.singleShot(0, ...)` 延迟 fit 计算，避免布局未稳时取错误尺寸。
- `runJavaScript` 调用均在 `_ready` 判定之后；`set_native_time` 对负值钳为 0。
- 信号声明齐全，无程序化回写自身控件的回路（无防递归锁需求）。

---

## src/features/teaching_animation/ui/html_devtools_panel.py (行数: 135)

### 发现

（无）

### 通过项
- 信号防递归：本面板信号全部由用户点击驱动，无程序化回写自身控件的路径，不需要状态锁。
- 状态一致性：`set_has_unapplied_changes` 幂等（同值早退），同步 discard 按钮、状态文案并
  发出 `draftChanged`；`set_devtools_widget` 替换路径先 `setParent(None)+deleteLater` 旧部件，
  置空路径恢复占位符并复位三按钮与草稿态（105-124）。
- `set_devtools_widget` 同对象早退避免重复 reparent（106-107）。
- i18n 全部经 `I18N.tr` 且带默认值；类型注解完整；无线程/GPU/GLSL/HDR 面；异常面不适用
  （纯展示控件，加载由宿主处理）。

---

## src/features/teaching_animation/ui/html_inspector.py (行数: 346)

### 发现

- [AUD-30640] [Major] 数值行直接对外部 `parameter_ui` 元数据做 `float()`/`int()` 强转，畸形模板数据会在场景刷新中抛未捕获异常
  - 位置: src/features/teaching_animation/ui/html_inspector.py:246-252、339-346
  - 证据:
    ```python
    minimum = float(ui.get("min", min(0.0, value)))
    fallback_maximum = max(100.0 if integer else 2.0, value * 2.0)
    maximum = float(ui.get("max", fallback_maximum))
    step = float(ui.get("step", 1.0 if integer else 0.01))
    decimals = int(ui.get("decimals", 0 if integer else 2))
    ```
    而 `_parameter_ui` 只校验 Mapping 形状，不校验数值字段的类型：
    ```python
    raw = self._layer.metadata.get("parameter_ui", {})
    if not isinstance(raw, Mapping):
        return {}
    entry = raw.get(token, {})
    return entry if isinstance(entry, Mapping) else {}
    ```
    对照同文件 `_token_specs`（332-337）对 schema 逐项 isinstance 校验。`parameter_ui` 源自
    HTML 生成管线写入的 layer.metadata（模板/AI 产出的半外部数据）。另 `_add_choice_row` 的
    `spec.get("enum", ())`（302）若为字符串会按字符迭代。
  - 影响: `min: "abc"` / `step: None` 之类的元数据使 `float()`/`int()` 抛 ValueError/TypeError，
    异常沿 `set_layer` → 窗口 `_refresh_scene` 上抛，属性面板刷新中断（严重时整窗刷新链路挂掉），
    违背“外部数据须验证”铁律。
  - 建议: 仿照 `_token_specs` 对每个数值字段做 `isinstance(x, (int, float))` 校验，非法即回退
    默认值；enum 用 `isinstance(x, (list, tuple))` 过滤。
  - 置信度: 中（触发依赖畸形元数据；校验缺口本身为实证）

- [AUD-30641] [Suggestion] 文本行的 `editingFinished` 不比较初值，纯焦点经过也会发出补丁命令
  - 位置: src/features/teaching_animation/ui/html_inspector.py:194-196
  - 证据:
    ```python
    field.editingFinished.connect(
        lambda f=field, op=operation, tgt=target: self._emit(op, tgt, f.text()))
    ```
    Qt 的 `editingFinished` 在失焦/回车时无条件触发，与内容是否改变无关；消费端
    `_apply_html_edit`（motion_graphic_editor_window.py:2047-2060）直接构造 `HtmlPatch` 并
    `execute(ApplyHtmlPatchCommand(...))`，命令侧（core/html_layer_commands.py:266-275）无
    同值去重，一律应用并提升 revision。
  - 影响: 点击进入再点出任意文本/token 字段即产生一条空效果撤销记录并 bump layer.revision
    /source_hash 相关状态，污染撤销栈（“什么都没做却多了一步 Undo”）。
  - 建议: 记录初值，`editingFinished` 时值未变则不发；或命令层对 no-op patch 短路。
  - 置信度: 高（机制两侧可证；影响为撤销栈噪音）

### 通过项
- 信号防递归：`set_layer` 重建期间全程持 `_updating` 状态锁，`_emit` 在锁内静默（82-101, 323-325），
  与项目防递归铁律一致。
- `set_layer` 的身份短路（`layer is self._layer`）正确规避同轮双重重建引发的 macOS
  QComboBox 崩溃（注释记录的既有缺陷）。
- 颜色/布尔/枚举行均在 `setChecked`/`setCurrentIndex` 之后再 connect，程序化初始化不会触发
  发射；数值行采用 defer/commit（valueChanged 暂存、interactionEnded 提交）实现“一手势一撤销”。
- `_token_specs` 对外部 schema 有 isinstance/name 校验；`_token_initial_value` 的正则经
  `re.escape(token)` 且限定 `[^;}]+`。
- `_clear_form` 以首个 persistent 区段为终止条件，生成区段永远插在其之前，删除循环终止正确。
- `_add_numeric_row` 对 token 值本身 `float(raw_value)` 有 ValueError 回退为文本行（236-239）。
- `select_node` 返回 bool 供调用方判断；类型注解总体完整。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 4 | AUD-30600, AUD-30610, AUD-30611, AUD-30640 |
| Suggestion | 11 | AUD-30601, AUD-30602, AUD-30603, AUD-30612, AUD-30613, AUD-30614, AUD-30620, AUD-30630, AUD-30631, AUD-30632, AUD-30641 |

总计 15 项发现；无发现文件 3 个（__init__.py、align.py、html_devtools_panel.py，均注明通过理由）。
