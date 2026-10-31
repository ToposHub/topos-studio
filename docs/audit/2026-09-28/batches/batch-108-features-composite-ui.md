# 批次报告：batch-108 [P1] src/features/composite/ui//

- 编号区间：AUD-10800–AUD-10899（实际使用 AUD-10801–AUD-10816）
- 审查日期：2026-09-28
- 文件数：8（2,719 行），全部通读
- 汇总：Critical 1 · Major 2 · Suggestion 12

验证说明：本项目 .venv 为 PyQt6 6.10.2。`Qt.ItemFlag.UserRole` 经实测
（QApplication + QListWidgetItem 复现）确认不存在，访问即抛 AttributeError。

---

## src/features/composite/ui/node_search_popup.py (行数: 676)

### 发现

- [AUD-10801] [Critical] `Qt.ItemFlag.UserRole` 在 PyQt6 中不存在，弹窗构造即抛 AttributeError，Add Node 搜索弹窗完全不可用
  - 位置: src/features/composite/ui/node_search_popup.py:341（另见 366、387、475、544、552、617）
  - 证据:
    ```python
    # line 128（__init__ 无条件执行）
    self._populate_tabs()
    # line 340-341（_populate_all_tab，对每个真实节点执行）
    item = QListWidgetItem(f"  {display_name}")
    item.setData(Qt.ItemFlag.UserRole, node_type)
    ```
    实测（本项目 .venv，PyQt6 6.10.2）：
    `AttributeError: type object 'ItemFlag' has no attribute 'UserRole'`
  - 影响: `NodeSearchPopup.__init__` → `_populate_tabs` → `_populate_all_tab` 在第一个真实节点条目即抛未捕获 AttributeError。`node_editor_widget.py:2212` 的 `NodeSearchPopup(NODE_CATEGORIES, ...)` 构造无 try/except，Tab 键/工具栏的 Add Node 弹窗一打开即失败（槽函数异常、弹窗永不显示）。同文件 `graph_node_search_popup.py:240` 使用的正确写法是 `Qt.ItemDataRole.UserRole`。
  - 建议: 全部 7 夂替换为 `Qt.ItemDataRole.UserRole`。
  - 置信度: 高

- [AUD-10802] [Major] 表头判断 `flags() & NoItemFlags` 恒为假（NoItemFlags==0），关键字过滤不会跳过类别表头
  - 位置: src/features/composite/ui/node_search_popup.py:472-473（同类无效判断另见 550、616）
  - 证据:
    ```python
    for i in range(list_widget.count()):
        item = list_widget.item(i)
        if item.flags() & Qt.ItemFlag.NoItemFlags:
            continue  # Skip headers
    ```
  - 影响: `NoItemFlags` 值为 0，`flags() & 0` 恒为假值，`continue` 永不执行。过滤时类别表头（如 "── Color ──"）按自身文本参与匹配：输入 "color" 会显示没有任何匹配子节点的孤儿表头。550/616 处因后续 `if node_type:` 兜底无实际危害。
  - 建议: 改为 `if item.flags() == Qt.ItemFlag.NoItemFlags:` 或按本文件 543 行的正确写法 `not (item.flags() & Qt.ItemFlag.ItemIsSelectable)`。
  - 置信度: 高

- [AUD-10803] [Suggestion] `_get_display_name` 用宽泛 `except Exception: pass` 吞掉 i18n 查错
  - 位置: src/features/composite/ui/node_search_popup.py:404-411
  - 证据:
    ```python
    try:
        from src.shared.i18n.i18n import tr_or_none
        translated = tr_or_none(i18n_key)
        if translated:
            return translated
    except Exception:
        pass
    ```
  - 影响: 违反"不得宽泛 except 掩盖领域错误"铁律；`tr_or_none` 内部任何错误（而非仅导入失败）都被静默吞掉，本地化缺失时无从排查。
  - 建议: 仅捕获 `ImportError`（或单独 try 导入与调用），其余异常记录日志。
  - 置信度: 高

- [AUD-10804] [Suggestion] 过滤路径每次按键对每个条目做全类别线性扫描，复杂度 O(条目×类别×别名表)
  - 位置: src/features/composite/ui/node_search_popup.py:470-488、492-498
  - 证据:
    ```python
    for i in range(list_widget.count()):
        ...
        node_class = self._get_node_class(node_type)   # 每条目遍历全部类别
    ...
    def _get_node_class(self, node_type: str) -> Optional[type]:
        for category, nodes in self._node_categories.items():
            for ntype, nclass in nodes:
                if ntype == node_type:
                    return nclass
    ```
  - 影响: `_on_search_changed` 挂在 `textChanged` 上每键触发；每次过滤都重复线性查找，节点类别增多后输入卡顿（纯 UI 性能模式问题，非渲染管线）。
  - 建议: `_populate_tabs` 时预构建 `node_type → node_class` 字典并随 `set_node_categories` 更新。
  - 置信度: 高

- [AUD-10805] [Suggestion] 多个方法缺失类型注解
  - 位置: src/features/composite/ui/node_search_popup.py:150、247、311、317、348、369、453、572 等
  - 证据:
    ```python
    def _setup_ui(self):
    def _apply_stylesheet(self):
    def _populate_tabs(self):
    def _filter_list(self, list_widget: QListWidget, filter_text: str):
    ```
  - 影响: 与项目强类型规范不一致；无运行时影响。
  - 建议: 补齐返回注解 `-> None:`。
  - 置信度: 高

### 通过项
- 信号防递归：`textChanged`/`currentRowChanged`/`currentChanged` 均由用户交互驱动，无程序化 setText/setCurrentIndex 回灌路径；`set_node_categories` 有 `_all_list` 存在性守卫。
- Enter 双路径（returnPressed 与 keyPressEvent）互斥不重复触发：QLineEdit 焦点下回车被行编辑消费，不会冒泡到 dialog 的 keyPressEvent。
- QSettings 读取（`_load_favorites`/`_load_recent_nodes`）有 `isinstance(..., list)` 校验，坏数据回退空列表。
- 无 GPU/OpenGL 触点、无跨线程数据；纯 UI 组件。
- 收藏/最近列表上限 20，无界增长风险已控。

---

## src/features/composite/ui/node_thumbnail.py (行数: 543)

### 发现

- [AUD-10806] [Suggestion] ThumbnailWidget 单通道视图中 R 与 B 的显示颜色互换（缺陷位于当前未被引用的控件类中）
  - 位置: src/features/composite/ui/node_thumbnail.py:216-217、277-290
  - 证据:
    ```python
    # line 277-280（CHANNEL_R：返回 RGB 序，r 在槽 0）
    r = data[:, :, 2]  # BGR 中 R 在 index 2
    return np.stack([r, np.zeros_like(r), np.zeros_like(r)], axis=-1)
    # line 216（对返回值一律再做 BGR→RGB 反转）
    processed = processed[:, :, [2, 1, 0]]
    ```
  - 影响: `_apply_channel_mode` 对 RGBA 模式返回 BGR 序（需反转），但对 R/B 单通道模式返回的已是 RGB 序数组；216 行一律反转后，R 通道视图实际显示为蓝色、B 通道显示为红色，误导通道检查。注：全库检索确认 `ThumbnailWidget` 当前无任何导入方（composite_page 仅导入 `NodeThumbnailManager` 与 `linear_rgb_to_display`），故为未接线代码中的潜在缺陷，降为 Suggestion。
  - 建议: 统一 `_apply_channel_mode` 的返回通道序（全部返回 BGR），或在单通道分支直接构造与输入同序数组，去掉 216 行的盲目反转。
  - 置信度: 高

- [AUD-10807] [Suggestion] 类型注解错误：`Tuple = None` 默认值与 `callable` 用作类型
  - 位置: src/features/composite/ui/node_thumbnail.py:450-451、398、419
  - 证据:
    ```python
    def update_thumbnail_from_data(self, node_id: str, data: Optional[np.ndarray],
                                    size: Tuple[int, int] = None):
    ...
    self._render_data_callback: Optional[callable] = None
    ...
    def set_render_data_callback(self, callback: callable):
    ```
  - 影响: `size: Tuple[int, int] = None` 与注解矛盾；`callable` 是内建函数而非类型（应为 `Optional[Tuple[int, int]]` / `Optional[Callable]`），静态检查无法校验回调签名。
  - 建议: 修正为 `Optional[Tuple[int, int]]` 与 `Callable[[str], Optional[np.ndarray]]`。
  - 置信度: 高

### 通过项
- 线程安全：缩略图数据在 UI 线程生成；渲染线程经 `node_thumbnail_ready` queued 信号传 bytes，`composite_page._on_node_thumbnail_ready` 在 UI 线程转 numpy 后调用本模块，无跨线程 GPU 访问。
- HDR 正确性：`linear_rgb_to_display` 末尾 `np.clip(0,1)` 属显示域编码（8-bit 输出必需），内部管线数据未被 clamp；ST 2084/HLG/sRGB OETF 与 viewer 着色器约定一致（100-nits 归一化有注释说明）。
- 显示设置同步：`set_display_settings`（manager）比较后 `clear_cache()` 避免旧 ODT 缓存混用。
- 批处理防抖：QTimer singleShot 500ms 合并待更新请求；缓存上限 MAX_THUMBNAILS 且保护选中节点。
- 异常路径：`update_from_numpy`/`update_thumbnail_from_data` 捕获异常后降级为错误占位图/日志，不向 UI 传播。

---

## src/features/composite/ui/perf_panel.py (行数: 230)

### 发现

- [AUD-10808] [Suggestion] `_trace_snapshots` 宽泛 `except Exception: pass`
  - 位置: src/features/composite/ui/perf_panel.py:89-98
  - 证据:
    ```python
    if callable(provider):
        try:
            return list(provider())
        except Exception:
            # The panel must remain usable if the renderer is being torn
            # down while the user opens the export action.
            pass
    ```
  - 影响: 注释给出理由（渲染器销毁竞态），但 `except Exception` 同时掩盖 provider 内部的任何逻辑错误，且无日志。
  - 建议: 窄化为 `RuntimeError`（已删除 C++ 对象的典型症状）并 `logger.debug` 记录。
  - 置信度: 中

- [AUD-10809] [Suggestion] 每帧调用 `_trace_snapshots()` 触发 provider 全量列表拷贝，仅为显示一个计数
  - 位置: src/features/composite/ui/perf_panel.py:179-181
  - 证据:
    ```python
    lines.append("")
    lines.append(f"Trace samples: {len(self._trace_snapshots())}")
    ```
  - 影响: `update_from_snapshot` 挂在 frame_ready 上逐帧执行；每帧额外调用一次 provider（render monitor 历史的 list 拷贝）并整段重排文本。诊断面板下影响有限，但与"减少不必要的计算"基准相悖。
  - 建议: 缓存 trace 计数（仅在导出/低频时刻刷新），或让 provider 提供 `len()`。
  - 置信度: 高

- [AUD-10810] [Suggestion] 缺失类型注解
  - 位置: src/features/composite/ui/perf_panel.py:32、39、184、193、222
  - 证据:
    ```python
    def __init__(self, parent=None):
    def _init_ui(self):
    def _copy_to_clipboard(self):
    def _show_context_menu(self, pos):
    def _find_parent_dock(self):
    ```
  - 影响: 与项目强类型规范不一致；无运行时影响。
  - 建议: 补齐 `parent: Optional[QWidget]`、`-> None`、`pos: QPoint`、`-> Optional[QDockWidget]`。
  - 置信度: 高

### 通过项
- `export_report` 有空路径校验，异常窄捕获 `(OSError, TypeError, ValueError)` 并以 bool 返回结果。
- 线程安全：`update_from_snapshot` 由 composite_page 的帧就绪槽在 UI 线程直调，面板无跨线程状态。
- dock 浮动/停靠切换经上下文菜单用户驱动，无信号重入。
- 历史 deque `maxlen=120` 有界，无内存增长风险。

---

## src/features/composite/ui/port_item.py (行数: 200)

### 发现

- [AUD-10811] [Suggestion] `set_compact(False)` 不恢复尺寸/画笔/笔刷，仅实现单向紧凑化
  - 位置: src/features/composite/ui/port_item.py:151-159
  - 证据:
    ```python
    def set_compact(self, compact: bool):
        self._compact = compact
        if compact:
            r = 3  # Smaller hit area
            self.setRect(-r, -r, 2 * r, 2 * r)
            self.setPen(QPen(Qt.PenStyle.NoPen))
            self.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self.update()
    ```
  - 影响: 传 `False` 时 rect 保持 6px、pen/brush 保持 NoPen/NoBrush，端口永久不可见。当前调用方仅 `node_editor_widget.py:464/468` 传 True（dot 节点一次性紧凑化），故为潜在陷阱而非现行缺陷。
  - 建议: 要么实现恢复分支（还原 PORT_RADIUS、调 `_update_brush()`、恢复画笔），要么收紧签名为一次性 `make_compact()` 防误用。
  - 置信度: 高

### 通过项
- hover 进入/离开的画笔恢复与连接计数状态一致（`_connected` 兼容 property 语义正确）。
- `decrement_connection` 用 `max(0, ...)` 防负计数；`is_at_capacity` 区分输入/输出端口语义。
- `itemChange` 仅在 `ItemScenePositionHasChanged` 时通知父级，无递归风险。
- 类级 `DATA_TYPE_COLORS` QColor 常量共享只读，安全。
- 无 GPU/线程/undo 维度触点；纯图元绘制。

---

## src/features/composite/ui/preview_connection_item.py (行数: 158)

### 发现

- [AUD-10812] [Suggestion] 模块级单例动画驱动强引用条目，`_advance`/`update_path` 缺少对端 C++ 对象已销毁的防护
  - 位置: src/features/composite/ui/preview_connection_item.py:53-57、61-68、116-126
  - 证据:
    ```python
    def _advance(self) -> None:
        self._offset -= 1.0
        for item in self._items:
            item._set_dash_offset(self._offset)
    ...
    _flow_driver: Optional[_PreviewFlowDriver] = None   # 模块级，永不重置
    ```
  - 影响: 若条目的 C++ 对象在未调用 `remove_from_scene()` 的情况下被销毁（例如编辑器 widget 整体销毁而无人清理），60ms 定时器将持续对悬空 wrapper 抛 RuntimeError，其余条目动画中断且永不自停。当前 `node_editor_widget` 在全部已知路径（set_preview_node 1460、场景重建 854、端点节点删除 527）都正确调用了 `_clear_preview_connection()`，故为潜在风险。
  - 建议: `_advance` 内捕获 `RuntimeError` 并移除失效条目；或给 `_PreviewFlowDriver` 提供随编辑器销毁的清理钩子。
  - 置信度: 中

### 通过项
- 定时器按需启停（add/remove 检查 `_items` 空集），无空闲唤醒。
- `setPen` 修改基于 `self.pen()` 返回的副本，无共享状态污染。
- 非交互标志（不可选/不可动/无鼠标按钮）设置完整，不会被误当作真实连线编辑。
- 贝塞尔几何与 ConnectionItem 镜像，`update_path` 对 None 端点有守卫。
- 生命周期归 `NodeEditorWidget` 管理且端点双向注册/注销对称（见 node_editor_widget 1459-1494）。

---

## src/features/composite/ui/project_settings_section.py (行数: 183)

### 发现

- [AUD-10813] [Suggestion] `set_settings_callback` 与 section 级信号中继构成双通道，并用时回调将收到双份通知
  - 位置: src/features/composite/ui/project_settings_section.py:124-127、181-183
  - 证据:
    ```python
    # _build_content：内嵌 widget 信号 → section 信号中继
    self._settings_widget.settings_changed.connect(
        self.settings_changed.emit)
    ...
    # set_settings_callback：内嵌 widget 信号 → callback 直连（第二通道）
    def set_settings_callback(self, callback) -> None:
        self._settings_widget.settings_changed.connect(callback)
    ```
  - 影响: 调用方若同时连接 `section.settings_changed` 与使用 `set_settings_callback`，同一变更触发两次回写/重渲染。当前 `node_properties_panel.py:4501` 仅使用信号连接，无人调用 `set_settings_callback`，故为 API 易误用问题。另：该方法 `callback` 参数无类型注解。
  - 建议: 删除 `set_settings_callback`（与中继重复），或改为内部断言防双连。
  - 置信度: 高

### 通过项
- 折叠（collapse/toggle_collapse 幂等）与 pin 均为用户驱动，无程序化状态回灌，无递归风险。
- 样式常量复用 shared 层，选择器不适用的原因有注释说明（62-63 行）。
- `load_from_graph` 为纯关键字透传代理，无状态突变。
- 无 GPU/线程/undo 触点。

---

## src/features/composite/ui/project_settings_widget.py (行数: 402)

### 发现

- [AUD-10814] [Major] FPS 组合框选择 "Custom" 时 `float("__custom__")` 抛 ValueError，settings_changed 中断发射
  - 位置: src/features/composite/ui/project_settings_widget.py:163、399（对照正确的守卫写法 354-359；`_CUSTOM_KEY = "__custom__"` 定义于 comp_clip_settings_dialog.py:65）
  - 证据:
    ```python
    # line 163：currentIndexChanged 直连 _emit_changed，无 Custom 守卫
    self._fps_combo.currentIndexChanged.connect(self._emit_changed)
    ...
    # line 399：
    "fps": float(self._fps_combo.currentData() or 24.0),
    ...
    # 对照：分辨率组合框有守卫（line 357-359）
    data = self._resolution_combo.itemData(index)
    if data == _CUSTOM_KEY or data is None:
        return
    ```
  - 影响: 用户在帧率下拉中选择 "Custom"（尚未经 `_set_fps_value` 写入过浮点 itemData 时 currentData 即 `"__custom__"`）→ `_emit_changed` 内 `float("__custom__")` 抛 ValueError，槽内未捕获异常（回溯刷屏），本次 settings_changed 丢失，自定义帧率设置流程损坏；与分辨率行的处理不对称。
  - 建议: 为 FPS 增加与分辨率一致的 `_on_fps_changed` 处理器：`data == _CUSTOM_KEY` 时 return（或弹出自定义值输入），其余情况再 `_emit_changed`。
  - 置信度: 高

### 通过项
- 信号防递归完备：`load_from_graph` 全程 `_block_emit` try/finally；宽高联动用 `blockSignals` 包住对侧 setValue；`_sync_resolution_combo` 同样 blockSignals；`_on_resolution_preset_changed` 有 `_block_emit` 守卫。
- `_set_fps_value` 预设匹配容差 1e-6，自定义值回写 itemData 后选择 Custom 项，行为正确。
- `set_timeline_bound` 成组禁用/恢复并同步 tooltip，Edit 托管元数据时防止 UI 双写。
- 背景色节点选择逻辑区分显式标记（PROJECT_BACKGROUND_PARAM）与遗留 last-Constant 回退，且 color space 归一化经 `ConstantNode.normalize_color_parameters`。
- 输入验证：宽高 clamp 到 64..16384；fps/时长有默认回退（24.0）；`_FPS_PRESETS` itemData 数值类型匹配。
- 无 GPU/线程触点；参数回写由上层（CompositePage）负责。

---

## src/features/composite/ui/scene_outliner_panel.py (行数: 327)

### 发现

- [AUD-10816] [Suggestion] `_apply_filter` 对每个条目重新 DFS 全部后代，整体 O(n²)，且挂在 textChanged 每键触发
  - 位置: src/features/composite/ui/scene_outliner_panel.py:314-327
  - 证据:
    ```python
    for item in self._items.values():
        matches = not needle or needle in item.text(0).casefold()
        descendant_match = any(
            needle in child.text(0).casefold()
            for child in self._descendants(item)
        ) if needle else False
        item.setHidden(not (matches or descendant_match))
    ```
  - 影响: 每个条目各自 `_descendants` 全展开，深层级大场景（数千节点）时每次键入触发平方级遍历，UI 卡顿。当前场景层级通常数百节点，影响中等。
  - 建议: 先做一次 DFS 计算"自身或后代匹配"的节点集合，再线性设置 setHidden；并对父链展开复用该结果。
  - 置信度: 高（复杂度结论）/影响中

### 通过项
- 信号防递归完备：`clear`/`set_hierarchy`/`set_selected_node`/`_on_item_changed`（solo 清旧、locked 回弹、批量可见性）均以 `_updating` try/finally 包裹程序化 checkState 变更，信号只在锁外发射。
- 输入验证：`hidden_ids`/`locked_ids` 逐项 `str()` 化并过滤空串；`summary` 非 dict 归 None；`triangle_count` 经 `int(... or 0)`。
- 线程边界：只消费元数据 dict（经 queued 信号送达），不触 GL 对象，符合渲染/UI 解离声明。
- 锁定链检查 `_item_or_parent_is_locked` 沿 parent 上溯，逻辑正确。
- 汇总行在可见性变更后重算，隐藏计数与 `_hidden_ids` 一致。

---

## 汇总统计

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-10801 |
| Major | 2 | AUD-10802、AUD-10814 |
| Suggestion | 12 | AUD-10803–10807、AUD-10808–10813、AUD-10816 |

（AUD-10800、AUD-10815 未使用。）
