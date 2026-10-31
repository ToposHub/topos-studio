# 批次报告：batch-307 — src/features/teaching_animation/ui（层树 / HTML 源编辑器 / 关键帧视图模型）

- 审查日期：2026-09-28
- 编号区间：AUD-30700–AUD-30799
- 文件数：4（1,393 行）
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性（undo·redo·revision·持久化）/ 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失
- 交叉验证依赖：`core/commands.py`（ReorderElementCommand）、`core/model.py`（with_elements_reordered、SceneTransform、ScenePage、TeachingSceneDocument）、`core/hierarchy.py`、`core/html_layer.py`、`render/evaluator.py`（ANIMATABLE_STYLE_PROPERTIES）、`shared/keyframe.py`（KeyframeTrack/Keyframe API）、`ui/motion_graphic_editor_window.py`（信号接线与宿主处理）

---

## src/features/teaching_animation/ui/html_source_editor.py (行数: 172)

### 发现

- [AUD-30700] [Suggestion] HTML 草稿字典 `_drafts` 永不清理，跨场景/删层后无限驻留
  - 位置: src/features/teaching_animation/ui/html_source_editor.py:24,111,124-132
  - 证据:
    ```python
    self._drafts: dict[str, str] = {}
    ...
    def discard_pending_draft(self) -> None:
        """Restore the saved source for the visible layer and clear its draft."""
        if self._layer is None:
            return
        self._drafts.pop(self._layer_key(self._layer), None)
    ...
    def _remember_current_draft(self) -> None:
        if self._updating or self._layer is None:
            return
        key = self._layer_key(self._layer)
        draft = self.editor.toPlainText()
        if draft == self._original:
            self._drafts.pop(key, None)
        else:
            self._drafts[key] = draft
    ```
  - 影响: 只有当前可见层的草稿可被 `discard_pending_draft` 清除；切换层时 `_remember_current_draft` 会把旧层草稿永久存回字典。切换/关闭场景（`set_layer(None)`）或删除图层后，旧层（甚至旧场景）的草稿仍留在内存中；若同 `layer_id` 的新层再次出现（如粘贴/导入复用 id），旧草稿会意外复活为"未应用草稿"并触发宿主的未保存提示（editor_window.py:1454/2986 依赖 `has_pending_draft`）。
  - 建议: 在 `set_layer(None)` 或场景切换入口清空 `_drafts`（或仅保留当前层键）；删除图层时由宿主调用清理对应键。
  - 置信度: 高

### 通过项
- 信号防递归：`textChanged→_on_text_changed`、`setPlainText` 全部由 `self._updating` 状态锁保护，程序化更新不会回灌信号（含 `set_layer`/`discard_pending_draft` 的 try/finally 复位）。
- 线程安全：纯 UI 线程控件，无 OpenGL/渲染线程交互。
- GPU 资源生命周期 / GLSL·HDR：不适用（无 GPU 资源、无着色器路径）。
- 状态提交一致性：`_apply` 仅 emit `sourceEditRequested`，sanitize 与 undo 由宿主 `ReplaceHtmlSourceCommand` 承担（editor_window.py:2061-2070 已核实接线），编辑器自身不绕过命令栈。
- 异常与取消路径：无宽泛 `except`；`_apply` 对空源显式拒绝。
- 输入验证：`str(layer.sanitized_html)`、`source.strip()` 空检查齐全。
- 性能模式：草稿保存为不可变 str 引用替换，按键每次 textChanged 无拷贝热点。
- 类型注解：完整（含 `_drafts: dict[str, str]`）。

---

## src/features/teaching_animation/ui/layer_tree_model.py (行数: 183)

### 发现

- [AUD-30701] [Suggestion] 全局背景取值对显式 `null` 未防御，`str(None)` 产生字面量 "None"
  - 位置: src/features/teaching_animation/ui/layer_tree_model.py:170-174
  - 证据:
    ```python
    rows.append(LayerRow(
        id=GLOBAL_BACKGROUND_ROW_ID,
        ...
        background_value=str(
            scene.background.get("css", scene.background.get("color", ""))
            if isinstance(scene.background, Mapping) else ""
        ),
    ))
    ```
  - 影响: `dict.get(k, default)` 在键存在但值为 `null`（JSON 反序列化得到 None）时返回 None，`str(None)` 得到 `"None"` 非空串——面板（layer_tree_panel.py:72-73）据 `background_value.strip()` 判断"是否回退全局"，会把 `"None"` 误判为已设置的背景值，行标签不再显示 "· Global"，且 tooltip/下游展示错误字面量。
  - 建议: 取值后校验 `isinstance(value, str)`，非 str（含 None）按空回退处理。
  - 置信度: 中

### 通过项
- 线程安全 / GPU 资源生命周期 / GLSL·HDR：纯 Python 无 Qt 视图模型，不适用（无状态、无 IO）。
- 信号防递归：无信号，不适用。
- 状态提交一致性：只读映射，不产生文档变更。
- 异常与取消路径：无 `except`，无吞错。
- 输入验证：`_element_label` 对 content 的 str/Mapping 分支均做 isinstance 检查；`scene.background` Mapping 检查；`z_index` 由模型层保证 int（model.py:154-155 已核实）。
- 性能模式：单次 `sorted` O(N log N) + 每页过滤，规模为场景图级（数百元素），可接受。
- 类型注解：完整（dataclass 字段、函数签名均有注解）。

---

## src/features/teaching_animation/ui/layer_tree_panel.py (行数: 612)

### 发现

- [AUD-30702] [Major] 重排序号按"可见行"计算而非全文档元组索引：任一页面折叠时 footer 移动与拖拽落点均写入错误元组位置
  - 位置: src/features/teaching_animation/ui/layer_tree_panel.py:463-464（footer 移动）、503-518 与 528-535（拖拽）
  - 证据:
    ```python
    # The document tuple is paint-ascending, while the element sequence in
    # this list is reversed (top-most first).
    all_element_rows = [index for index, row in enumerate(self._rows) if not row.readonly]
    self.reorderRequested.emit(current_id, len(all_element_rows) - 1 - target)
    ...
    element_count = len(element_ids)
    if element_count <= 0:
        return
    # Element sequence is z-descending; the document tuple is
    # paint-ascending. new_tuple_index = N-1-position.
    new_tuple_index = max(0, min(element_count - 1,
                                 element_count - 1 - current_element_pos))
    self.reorderRequested.emit(rid, new_tuple_index)
    ```
    而 `element_ids` 仅收集当前列表可见项（503-518 行按 `self.list_widget.count()` 遍历）；`self._rows` 由 `build_layer_rows(..., expanded_page_ids=...)` 生成，折叠页的元素行根本不存在。
  - 影响: `ReorderElementCommand` → `scene.with_elements_reordered`（commands.py:311-320、model.py:693-698）把该索引解释为**全量** `scene.elements` 元组索引。设总元素 N、可见元素 M<N（其它页折叠），发出的索引被压低至 M-1-pos，元素被静默插入错误元组位置（索引恒在合法域内，不会抛 IndexError）：等 z_index 元素的 tie-break 绘制顺序错乱、序列化顺序与用户所见不符；footer 上移/下移在折叠另一页时同样错位。属于确定性的静默数据错置（可 undo 恢复，故不评 Critical）。
  - 建议: 索引换算改为基于 `scene.elements` 全量集合（如按可见顺序映射到全量 z 降序序列），或按"同页内相对位置/目标邻居元素 id"语义发信号，由宿主换算全量索引。
  - 置信度: 高（换算算术与消费端语义均已核实；触发前提是存在折叠页）

- [AUD-30703] [Major] 多行拖拽（`start != end`）直接 return：Qt 已完成的视觉移动既不提交命令也不重建，界面与文档顺序脱节
  - 位置: src/features/teaching_animation/ui/layer_tree_panel.py:488-499
  - 证据:
    ```python
    def _rows_moved(self, _parent: object, start: int, end: int,
                    _destination: object, _row: int) -> None:
        if self._updating or start != end:
            return
        # QListWidget has already applied the move.  Read the new visual order
        # (headers included) and map it back: ...
        current = self.list_widget.currentItem()
        if current is None:
            return
        rid = str(current.data(_ROLE_ROW_ID))
    ```
  - 影响: 面板开启 `ExtendedSelection`（198 行）+ `InternalMove`（197 行），可多选后整体拖拽。若 Qt 以连续块发出 `rowsMoved(start<end)`，本守卫在 Qt 已改动列表视觉顺序后静默返回——不 emit 任何 `reorderRequested`/`pageMoveRequested` 也不 `_rebuild`，列表显示顺序与文档（undo 栈、序列化、绘制）持续不一致直到下一次 `set_scene`；若 Qt 拆成多次单行 `rowsMoved`，则每次只提交 `currentItem()` 一行，其余被移动行的视觉新位置同样丢失提交。两种路径都造成静默脱节。
  - 建议: `start != end`（或多选拖拽）时收集全部受影响行并逐行/整体提交命令，失败或无法映射时立即按 `self._rows` 重建列表以回滚视觉状态。
  - 置信度: 中（多选 InternalMove 的具体 rowsMoved 粒度随 Qt 版本而异，但两条路径下均存在未提交脱节；单行拖拽路径正常）

- [AUD-30704] [Suggestion] 多选删除逐个 emit `removeRequested`，产生 N 个独立 undo 步骤
  - 位置: src/features/teaching_animation/ui/layer_tree_panel.py:431-436
  - 证据:
    ```python
    def _delete_current(self) -> None:
        # Batch delete (multi-select): emit remove for every selected element
        # row.  The HTML-layer row is read-only and never removed here.
        for rid in self.selected_row_ids:
            if not self._is_html_row(rid) and not self._is_background_row(rid):
                self.removeRequested.emit(rid)
    ```
  - 影响: 宿主 `_remove_element`（editor_window.py:1761-1763）对每个 id 独立执行 `RemoveElementCommand`，一次多选删除变成 N 条 undo 记录；用户按一次 Ctrl+Z 只恢复一个层，且中途状态为部分删除。
  - 建议: 面板增加批量信号（如 `removeRequested(tuple)`）或宿主将 N 次删除合并为一个 `CompositeCommand`。
  - 置信度: 高

- [AUD-30705] [Suggestion] 多处形参注解退化为 `object` 并依赖 `type: ignore`；`_element_count` 为死代码
  - 位置: src/features/teaching_animation/ui/layer_tree_panel.py:133,367,488,547,602-603
  - 证据:
    ```python
    def _toggle(self, icon: object, tooltip: str, *, checked: bool) -> QToolButton:
        btn = QToolButton(self)
        btn.setIcon(icon)  # type: ignore[arg-type]
    ...
    def _show_context_menu(self, position: object) -> None:
        """Expose page management where pages now live: the Layers tree."""
        item = self.list_widget.itemAt(position)  # type: ignore[arg-type]
    ...
    def _element_count(self) -> int:
        return sum(1 for r in self._rows if not r.readonly)
    ```
  - 影响: `icon` 实为 QIcon、`position` 实为 QPoint，宽注解使静态检查失效且必须散布 `# type: ignore`；`_element_count` 全文件无调用点，属遗留死代码。
  - 建议: 收紧为真实类型（`QIcon`、`QPoint`）删除对应 ignore；删除 `_element_count`。
  - 置信度: 高

### 通过项
- 信号防递归：`_rebuild`/`set_selection` 全程 `self._updating` 状态锁；`_selection_changed`、`_rows_moved` 入口均检查锁；`LayerItemWidget` 的 `setChecked` 先于 `toggled` 连接（141/114-116 行），无程序化触发；页面展开双向同步链（`pageExpansionChanged → _set_page_expansion → set_expanded_pages`）两侧 setter 均不回发信号，无环路（editor_window.py:1144-1151、mg_keyframes_panel.py:229-235 已核实）。
- 线程安全：纯 UI 线程，无渲染线程/OpenGL 调用。
- GPU 资源生命周期 / GLSL·HDR：不适用。
- 状态提交一致性：所有变更均经信号交由宿主命令栈执行（reorder/remove/reparent/pageMove/pageCreate/Rename/Duration/Reorder/Remove 均已在 editor_window 中确认接 undo 命令），面板不直接改文档。
- 异常与取消路径：无宽泛 `except`（仅 `_update_action_state` 捕获窄化 `ValueError` 用于 index 查找回退，合理）。
- 输入验证：`set_scene`/`set_expanded_pages` 对 expanded 集合与 `scene.page_ids` 求交防脏 id；`set_selection` 忽略不存在 id；`_toggle_page_expanded` 校验 page 归属。
- 性能模式：整表重建为既定"thin renderer"模式（与 color/layer_panel 同构）；`_update_action_state` 的 O(行数) 扫描在 UI 事件频率下可接受。

---

## src/features/teaching_animation/ui/mg_keyframes_model.py (行数: 426)

### 发现

- [AUD-30706] [Suggestion] 全局背景取值复刻了 `str(None)→"None"` 的 null 盲区
  - 位置: src/features/teaching_animation/ui/mg_keyframes_model.py:260-265
  - 证据:
    ```python
    rows.append(MgKeyframesRow(
        row_id="background:global",
        kind="background",
        label="Global Background",
        editable=False,
        page_spans=((0, max(0, scene.duration_frames - 1)),),
        background_value=str(
            scene.background.get("css", scene.background.get("color", ""))
            if isinstance(scene.background, Mapping) else ""
        ),
    ))
    ```
  - 影响: 与 layer_tree_model.py:170-174 同一模式：JSON `null` 的 `"css"`/`"color"` 值经 `str()` 变成非空 `"None"`，`background_value` 语义（空=回退全局）被破坏，时间轴背景行显示/回退判断错误。
  - 建议: 与 AUD-30701 一并提取共享取值函数并做 str/None 校验。
  - 置信度: 中

- [AUD-30707] [Suggestion] 背景行标签硬编码英文，绕过 i18n（同模块其它标签均走 I18N.tr）
  - 位置: src/features/teaching_animation/ui/mg_keyframes_model.py:249,258
  - 证据:
    ```python
            rows.append(MgKeyframesRow(
                row_id=f"background:{page.page_id}",
                kind="background",
                page_id=page.page_id,
                label="Background",
                ...
        label="Global Background",
    ```
    对比 layer_tree_model.py:155-166 的 `I18N.tr("motion_graphic.page.background", "Background")` / `I18N.tr("motion_graphic.page.global_background", "Global Background")`。
  - 影响: 非 English locale 下关键帧时间轴的背景行仍显示英文，与层树同类行不一致，违反项目 i18n 约定。
  - 建议: 复用 `motion_graphic.page.background` / `motion_graphic.page.global_background` 两个已有 key。
  - 置信度: 高

### 通过项
- 线程安全 / GPU 资源生命周期 / GLSL·HDR：纯 Python 视图模型，不适用（无 Qt、无渲染资源）。
- 信号防递归：无信号，不适用。
- 状态提交一致性：所有 `track_with_*` 辅助函数先 `copy_track`（to_dict/from_dict 深拷贝）再改动，绝不原地修改文档中的 track；`keyframe_with_frame` 用 `dataclasses.replace` 完整保留 Bezier/切线手柄，避免克隆丢元数据；改动结果交由宿主命令栈（panel → `tracks_changed → _apply_keyframe_edits`）提交，撤销安全。
- 异常与取消路径：`property_filter` 非法值显式 `raise ValueError`（169 行），无宽泛 except；`track_with_keyframes_moved` 的唯一性前置条件已在调用方 `mg_keyframes_panel.py:1760` 实现"全组无碰撞"校验。
- 输入验证：`static_value_for` 对 style 值做 (TypeError, ValueError) 防御回退；transform 路径由 `SceneTransform.__post_init__` 保证有限数值（model.py:56-65 已核实），`float()` 不会抛出；`FrameMapper.x_to_frame` 除法有 1e-9 下界、`zoom_at` 夹紧 ppf ∈ [0.05, 256]。
- API 一致性：`effective_tracks` 的 local→scene 覆盖次序与 evaluator `_tracks_for`（evaluator.py:62-67）逐行一致；`KeyframeTrack(param_name, keyframes, min_val, max_val)` 位置参数次序与 shared/keyframe.py:133-136 字段序一致；`add_keyframe` 同帧替换语义与 `track_with_keyframe_added` 文档一致。
- 性能模式：`copy_track` 走单次序列化往返，量级为单 track 关键帧数，非每帧热路径；`build_keyframe_rows` 排序一次 + 每页线性过滤，规模可接受。
- 类型注解：完整（含 `frozenset[str] | Iterable[str]` 默认 `frozenset()` 避免可变默认值）。

---

## 统计

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 2 | AUD-30702, AUD-30703 |
| Suggestion | 6 | AUD-30700, AUD-30701, AUD-30704, AUD-30705, AUD-30706, AUD-30707 |

两个 Major 均集中于 `layer_tree_panel.py` 的拖拽/移动重排路径（折叠页索引错置、多行拖拽视觉与文档脱节）；无 Critical。其余文件以 i18n/清理类质量项为主。
