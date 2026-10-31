# batch-111 审查报告 — src/features/composite/ui（timeline 系列纯数据/UI 模块）

- 批次: batch-111 [P1]
- 编号区间: AUD-11100–AUD-11199
- 文件数: 5（合计 2,129 行）
- 审查维度: 线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失
- 统计: Critical 0 · Major 5 · Suggestion 13
- 背景: 本批次 5 个文件均无 GPU/GLSL 代码（GUI-free 数据层 + 1 个 QWidget），GLSL·HDR 与 GPU 生命周期维度整体不适用；`timeline_session.py` 文件头标注 DORMANT（无生产入口），`timeline_shortcuts` / `timeline_time_display` / `timeline_view_model` 已接线至 `keyframes_panel.py` / `spline_panel.py`。

---

## src/features/composite/ui/timeline_layout.py (行数: 407)

### 发现

- [AUD-11100] [Suggestion] `_project_tokens()` 宽泛 `except Exception` 且每次解析都重建 token 映射，无缓存
  - 位置: src/features/composite/ui/timeline_layout.py:205-217（及 253-257 每次调用）
  - 证据:
    ```python
    def _project_tokens() -> Mapping[str, str]:
        try:
            from src.shared.ui.style_sheet import C
            return {k: v for k, v in vars(C).items()
                    if isinstance(v, str) and v.startswith("#")}
        except Exception:
            return {}
    ```
  - 影响: ① `style_sheet` 中任何真实错误（如语法错误、循环导入）被静默吞掉，界面悄悄回落到参考色而非项目主题色，违反「不得宽泛 except 掩盖领域错误」；② `resolve_token()` 每次调用都执行 import + `vars()` + 字典推导，而 `timeline_icons.py:365` 等处在 QPainter 绘制路径中调用它（每个 error 图标每次 paint 一次），刷新频繁时是无谓的重复分配。
  - 建议: 收窄为 `except ImportError`；在模块级或 `lru_cache` 缓存 token 映射（主题仅在启动/切换时变化）。
  - 置信度: 高

- [AUD-11101] [Suggestion] 未使用的导入 `field`
  - 位置: src/features/composite/ui/timeline_layout.py:35
  - 证据:
    ```python
    from dataclasses import dataclass, field
    ```
  - 影响: 本模块所有 dataclass（`ColumnSpec`/`KeyframeShapeSpec`/`IconSpec`/`RowStateStyle`）均未使用 `field()`，属于冗余导入，误导读者以为存在 default_factory。
  - 建议: 删除 `field` 导入。
  - 置信度: 高

### 通过项
- 线程安全: 纯数据模块，无可变共享状态，GUI-free（符合自述设计规则）
- GPU 资源生命周期 / GLSL·HDR: 不适用（无 GPU/GLSL 代码）
- 信号防递归: 不适用（无 Qt 信号）
- 输入验证: `left_column_widths` 对负数/零宽度输入做了 `max(0, int(...))` 防御；三种预算路径（默认/最小/紧凑）经逐档验算均精确铺满 `total_width`，不会越界或负宽
- 性能模式: 除 AUD-11100 外，常量表与查找函数均为 O(常量)
- 类型注解: 完整（含返回类型）

---

## src/features/composite/ui/timeline_session.py (行数: 348)

### 发现

- [AUD-11102] [Major] `TimelineSessionState.from_dict` 对外部文件中的 `schema_version` 无保护，非数值直接抛异常中断整个会话状态加载
  - 位置: src/features/composite/ui/timeline_session.py:291-292
  - 证据:
    ```python
    # schema_version — preserve the file's value for migration decisions
    # but clamp to the current version after applying defaults.
    state.schema_version = int(data.get("schema_version",
                                        TIMELINE_SESSION_VERSION))
    ```
  - 影响: 同函数内其它所有字段都有 try/except 或词表校验（`pixels_per_frame`、`filter_mode`、`display_mode` 等），唯独 `schema_version` 裸 `int()`。工程文件中该键为 `null`（`int(None)`→TypeError）或非数字字符串（`int("abc")`→ValueError）时，`from_dict` 整体抛出，恰好违背模块自述的前向兼容契约（"a newer file opens in an older build without losing prefs"）与「外部数据须验证」铁律。注: 本模块文件头标注 DORMANT、暂无生产接线，接线后此路径位于工程文件加载链上。
  - 建议: 与 `pixels_per_frame` 一致地包 `try/except (TypeError, ValueError)` 回落 `TIMELINE_SESSION_VERSION`。
  - 置信度: 高（代码路径确定；当前无生产调用方，影响为接线后的明确风险）

- [AUD-11103] [Suggestion] `ColumnModel.from_dict` 用宽泛 `except Exception: continue` 静默丢弃畸形列配置
  - 位置: src/features/composite/ui/timeline_session.py:217-220
  - 证据:
    ```python
    for raw in incoming:
        try:
            cfg = ColumnConfig.from_dict(raw)
        except Exception:
            continue
    ```
  - 影响: `ColumnConfig.from_dict` 只会抛 TypeError/ValueError/AttributeError（`data.get` 在非 Mapping 上、`int()` 转换失败），裸 `except Exception` 同样会吞掉调用方无意的编程错误，且无日志，用户列配置悄悄丢失无从排查。
  - 建议: 收窄为 `except (TypeError, ValueError, AttributeError)`，必要时加调试日志。
  - 置信度: 高

- [AUD-11104] [Suggestion] `ColumnModel.column_widths` 在总宽不足时返回的列宽之和可超出 `total_width`（无紧凑回退）
  - 位置: src/features/composite/ui/timeline_session.py:142-146
  - 证据:
    ```python
    name_spec = spec_by_name.get("name")
    if name_spec is not None:
        remainder = max(name_spec.min_width, total_width - fixed_total)
        widths["name"] = remainder
    return widths
    ```
  - 影响: 当 `total_width < fixed_total + 80` 时，name 列被钉在 min_width=80，总宽超出面板宽度，尾部列（value/bind/expr）被裁剪到可视区外。对比 `timeline_layout.left_column_widths`（batch 内同目录）专门有紧凑优先级路径保证精确铺满，此处没有对齐该行为。模块 DORMANT，属潜在缺陷。
  - 建议: 复用/镜像 `left_column_widths` 的紧凑回退策略，或至少在超出时按比例收缩可压缩列。
  - 置信度: 中（宽度语义可能有意"宁溢不缩"，但与同目录解析器行为不一致）

- [AUD-11105] [Suggestion] `horizontal_scroll` 反序列化无范围约束（可有负值/超大值）
  - 位置: src/features/composite/ui/timeline_session.py:308-311
  - 证据:
    ```python
    try:
        state.horizontal_scroll = float(data.get("horizontal_scroll", 0.0))
    except (TypeError, ValueError):
        state.horizontal_scroll = 0.0
    ```
  - 影响: `pixels_per_frame` 被镜像 clamp 到 [0.05, 256]，而 `horizontal_scroll` 只做了类型防御、未做范围校验；持久化的负数或超大滚动值会原样进入视图状态，可能导致打开工程后时间轴视口落在内容之外。
  - 建议: 加 `max(0.0, ...)`（或按内容长度上界的 clamp）。
  - 置信度: 中（下游若再 clamp 则影响很小）

### 通过项
- 线程安全: 纯数据 + GUI-free，无共享可变状态
- GPU/GLSL/HDR、信号防递归: 不适用
- 状态提交一致性: `to_dict`/`from_dict` 往返字段齐全，`schema_version` 随文件保留；未知列名按前向兼容策略丢弃、未知键不覆盖（符合模块契约）
- 输入验证: `filter_mode`/`display_mode` 词表校验、`pixels_per_frame` clamp、`expanded_node_ids` 逐元素 `str()` 过滤均正确
- 性能模式: 列合并/排序规模固定（11 列），无问题
- 类型注解: 完整

---

## src/features/composite/ui/timeline_shortcuts.py (行数: 438)

### 发现

- [AUD-11106] [Major] `handle_key` 以 `except Exception: pass` 吞掉命令处理器异常，按键静默失败
  - 位置: src/features/composite/ui/timeline_shortcuts.py:301-307
  - 证据:
    ```python
    handler = self.handlers.get(command)
    if handler is not None:
        try:
            handler()
        except Exception:
            # A handler error must not poison the dispatcher; production
            # wiring can log here.
            pass
    ```
  - 影响: 该分发器已接线生产（`keyframes_panel.py:398` 把面板键事件路由到 `handle_key`）。任何命令处理器（如 `add_keyframe_at_playhead`、seek）抛出的领域错误被完全吞掉且无日志，用户按键无响应、无任何可排查痕迹，直接违反「不得宽泛 except 掩盖领域错误」铁律。注释自认"production wiring can log here"但从未落实。
  - 建议: 至少 `logging.exception` 后再继续（保住不中断分发的语义），或按项目日志规范上报。
  - 置信度: 高

- [AUD-11107] [Suggestion] `CMD_SET_WORK_IN/OUT`（单字母 I/O/B/N）缺失于 `_TEXT_SUPPRESSED_COMMANDS`，焦点守卫对这几个字母失效
  - 位置: src/features/composite/ui/timeline_shortcuts.py:354-365（对照绑定表 99-102）
  - 证据:
    ```python
    "I": CMD_SET_WORK_IN,
    "O": CMD_SET_WORK_OUT,
    "B": CMD_SET_WORK_IN,
    "N": CMD_SET_WORK_OUT,
    ...
    _TEXT_SUPPRESSED_COMMANDS: Set[str] = {
        CMD_NAV_PREV_KEYFRAME, CMD_NAV_NEXT_KEYFRAME, ...
        CMD_FILTER_ANIMATED, CMD_FILTER_MODIFIED, CMD_FILTER_EXPRESSION,
        CMD_FIT_SELECTION, CMD_FIT_ALL, CMD_ZOOM_IN, CMD_ZOOM_OUT,
        CMD_CENTER_PLAYHEAD, CMD_TOGGLE_SNAP,
    }
    ```
  - 影响: 模块自述目标是"a focused search box never triggers a timeline command"。所有其它单字母命令（P/S/R/A/T/U/E/F/J/K）都在抑制集合中，唯独 I/O/B/N 不在：文本输入框聚焦时按这四个字母会触发设置工作区入/出点而不是输入字符。当前生产接线（`keyframes_panel._install_shortcut_event_filters` 只装在面板/视图上，搜索框 `QLineEdit` 的事件不经过分发器）暂不触发，属守卫契约内的潜在缺陷。
  - 建议: 把 `CMD_SET_WORK_IN`/`CMD_SET_WORK_OUT` 加入 `_TEXT_SUPPRESSED_COMMANDS`。
  - 置信度: 高（集合成员事实确定；当前接线不触发，影响为潜在）

- [AUD-11108] [Suggestion] `_normalise_sequence` 文档承诺 Cmd↔Ctrl 查找期折叠，实现只做空白清理，两者不一致
  - 位置: src/features/composite/ui/timeline_shortcuts.py:178-190
  - 证据:
    ```python
    """Canonicalise a key sequence string for lookup.

    Strips whitespace, collapses ``Cmd`` ↔ ``Ctrl`` *aliases* only at
    lookup time ...
    """
    if not isinstance(seq, str):
        return ""
    s = seq.strip()
    parts = [p.strip() for p in s.split("+")]
    return "+".join(parts)
    ```
  - 影响: 实现并未把 `Cmd+K` 折叠为 `Ctrl+K`。默认绑定表两种拼写都登记所以默认可用，但用户自定义绑定只登记一种拼写时，另一平台的另一种拼写查找即失败——依赖文档所述行为的宿主会踩坑。
  - 建议: 实现真正的别名折叠（如把 `Cmd` 归一为 `Ctrl` 后查找），或修正 docstring。
  - 置信度: 高（文档/实现不一致确凿；触发依赖自定义绑定）

- [AUD-11109] [Suggestion] `handle_key(now_ms=None)` 把"未知时间"当作 0，省略参数的调用方任意两次 U/E 都算双击
  - 位置: src/features/composite/ui/timeline_shortcuts.py:258-259、328-330
  - 证据:
    ```python
    def handle_key(self, sequence: str, *, now_ms: Optional[float] = None,
                   modifiers_only: bool = False) -> Optional[str]:
    ...
    now = now_ms if now_ms is not None else 0.0
    if (self._last_key == seq and self._last_key_command is not None
            and 0.0 <= now - self._last_key_ms <= self.double_tap_ms):
    ```
  - 影响: 调用方不传 `now_ms` 时，两拍时间差恒为 `0.0 - 0.0 = 0`，落在 `[0, 350]` 窗口内，于是相隔任意久的两次 U 都升级为 `FILTER_MODIFIED`（E 同理）。生产调用（`keyframes_panel.py:398-400`）显式传了时间戳，当前不受影响；这是留给后续宿主/脚本接线的 API 陷阱（参数默认值与 docstring 的"callers pass ... values"约定之间无强制）。
  - 建议: `now_ms` 缺省时用 `time.monotonic()*1000` 兜底，或将参数改为必填。
  - 置信度: 高（逻辑确定；生产路径当前不受影响）

### 通过项
- 线程安全: UI 线程单线程使用，无共享可变状态
- GPU/GLSL/HDR、状态提交一致性: 不适用（无渲染/持久化代码）
- 信号防递归: 分发器返回命令名而非再发 Qt 信号，无递归风险；焦点守卫抑制时正确重置双击痕迹（`self._last_key = None`，297 行），文本框里连打 "uu" 不会误触发过滤
- 异常与取消路径: 除 AUD-11106 外，`is_text_input_focused`/`_default_focus_widget` 的异常回落 None 合理（Qt 不可用时无焦点可查）
- 输入验证: `_normalise_sequence` 对非字符串返回空串；`_is_text_input` 对无 MRO 对象防御正确
- 性能模式: 查找均为 O(绑定表)，无问题
- 类型注解: 完整

---

## src/features/composite/ui/timeline_time_display.py (行数: 344)

### 发现

- [AUD-11110] [Major] 回车提交非法输入时 `editingFinished` 紧随 `returnPressed` 触发取消，设计要求的"红框 + 保持编辑态"实际不可达
  - 位置: src/features/composite/ui/timeline_time_display.py:152-153、253-262、268-269
  - 证据:
    ```python
    self._edit.returnPressed.connect(self._on_edit_commit)
    self._edit.editingFinished.connect(self._on_edit_cancel)
    ...
    def _on_edit_commit(self) -> None:
        text = self._edit.text().strip()
        frame = self._parse_user_input(text)
        if frame is None:
            # Invalid → red border, stay in edit mode so the user can fix it.
            self._edit.setProperty("invalid", True)
            ...
            return
    ```
  - 影响: Qt 中 QLineEdit 无 validator 时按 Enter 会**同时**发出 `returnPressed` 与 `editingFinished`（后者还覆盖失焦）。非法输入路径 `_on_edit_commit` 提前 return 保持编辑态，但紧接着 `_on_edit_cancel`→`_exit_edit()` 立即关闭编辑框——真实键盘路径下用户永远停留在"红框待修正"状态，错误反馈只剩一瞬。现有测试（`tests/ui/test_timeline_time_display.py::test_commit_invalid_does_not_emit`）直接调用 `_on_edit_commit()`，绕过了 Qt 信号链，因此未暴露此缺陷。
  - 建议: 提交流程加"本回合已处理"守卫（如 `_committing` 标志令 `_on_edit_cancel` 短路），或只连 `editingFinished` 并在其中区分回车/失焦。
  - 置信度: 高

- [AUD-11111] [Major] 错误边框选择器写成 `QLineEdit#invalid`（objectName 匹配），代码却设置动态属性，红框规则永远不生效
  - 位置: src/features/composite/ui/timeline_time_display.py:169、248、258
  - 证据:
    ```python
    f"QLineEdit#invalid {{ border: 1px solid {error}; }}"
    ...
    self._edit.setProperty("invalid", False)   # _enter_edit
    ...
    self._edit.setProperty("invalid", True)    # _on_edit_commit 失败路径
    self._edit.setStyleSheet("")  # force re-eval of the #invalid rule
    ```
  - 影响: Qt 样式表中 `#id` 匹配 objectName，而控件的 objectName 从未被设为 `"invalid"`；设置的是动态属性，属性选择器语法应为 `QLineEdit[invalid="true"]`。因此非法输入时 C.ERROR 红色边框永远不出现，直接违背计划 §16.3 B1"时间码无效时使用 C.ERROR 边框"（模块 docstring 自引该要求）。与 AUD-11110 叠加后，非法输入的反馈完全失效（无声关闭、无红框、不动播放头）。
  - 建议: 规则改为 `QLineEdit[invalid="true"]`，并在 `setProperty` 后保持现有 unpolish/polish；删除无效的 `setStyleSheet("")`。
  - 置信度: 高

- [AUD-11112] [Suggestion] `setFamily` 传入逗号分隔字体列表不生效，等宽字体意图静默失败
  - 位置: src/features/composite/ui/timeline_time_display.py:132、142
  - 证据:
    ```python
    f = QFont(self._top_label.font())
    f.setPointSize(9 if self._compact else 10)
    f.setBold(True)
    f.setFamily("Menlo, Consolas, Monaco, monospace")
    ```
  - 影响: `QFont.setFamily` 只接受单个 family；逗号串会被当成一个不存在的字体名，匹配失败后回落到应用默认比例字体。时间码/帧号本应用等宽字体防跳宽（模块自述 fixed width 的补充手段），Qt 6.1+ 应使用 `setFamilies(["Menlo", "Consolas", ...])`。
  - 建议: 改用 `QFont.setFamilies([...])`（或 `setStyleHint(QFont.StyleHint.Monospace)`）。
  - 置信度: 高

- [AUD-11113] [Suggestion] 未使用的导入 `C`
  - 位置: src/features/composite/ui/timeline_time_display.py:38
  - 证据:
    ```python
    from src.shared.i18n.i18n import I18N
    from src.shared.ui.style_sheet import C
    ```
  - 影响: 文件体内没有任何 `C.` 引用（颜色全部经 `timeline_layout.resolve_token`），仅 docstring 提及"C.ERROR"。冗余导入。
  - 建议: 删除该导入。
  - 置信度: 高

- [AUD-11114] [Suggestion] 解析器接受负数/无上界帧号，且提交时先用未钳制值更新自身显示
  - 位置: src/features/composite/ui/timeline_time_display.py:303-305、342-344、263-266
  - 证据:
    ```python
    # Pure integer → frame.
    try:
        return int(text)
    except ValueError:
        pass
    ...
    self._exit_edit()
    if frame != self._current_frame:
        self.set_current_frame(frame)
        self.frame_committed.emit(frame)
    ```
  - 影响: 输入 `"-5"` 直接返回 -5；timecode 的 `hh` 无上界（`"99999:0:0:0"` 得到巨大帧号）。提交时控件先 `set_current_frame(未钳制值)` 再发信号，宿主 `seek_to_frame`（keyframes_panel.py:2408-2410）clamp 后再回写，产生一次显示跳变后回正的自不一致瞬态。当前唯一生产宿主有 clamp，影响轻微。
  - 建议: 在 `_parse_user_input` 返回前按 [0, ∞)（或注入的 start/end_frame 范围）钳制/拒绝。
  - 置信度: 中

### 通过项
- 线程安全: 仅 UI 线程操作 QWidget，无跨线程访问
- GPU 资源生命周期 / GLSL·HDR: 不适用（纯 Qt Widgets 绘制，无 ModernGL）
- 信号防递归: `frame_committed`→宿主 seek→`set_current_frame` 链路中 `set_current_frame` 不回发信号，无环路；同帧重复提交有去重（`frame != self._current_frame`）
- 状态提交一致性: 显示模式/菜单模式分离存储正确；`set_fps` 1e-9 浮点比较避免无谓刷新
- 异常与取消路径: Esc 经 eventFilter 取消编辑正确；`fps` 有 `max(1.0, ...)` 防除零；timecode 各字段范围校验（mm/ss<60、ff<fps）正确
- 性能模式: 固定宽度 + `f"{frame:05d}"` 防跳宽，符合自述设计
- 类型注解: 公共方法完整（事件处理器参数未标注，Suggestion 级以下，不单列）

---

## src/features/composite/ui/timeline_view_model.py (行数: 592)

### 发现

- [AUD-11115] [Major] `select_keyframe` 替换模式只清 keyframes 不清 rows（仅丢弃旧 primary），且不把新关键帧所在行加入 rows，破坏与 `sync_from_model` 的选择不变量
  - 位置: src/features/composite/ui/timeline_view_model.py:447-453（对照 540-547 的不变量）
  - 证据:
    ```python
    if not additive:
        self._selection.keyframes.clear()
        self._selection.rows.discard(self._selection.primary_row_id or "")
    self._selection.keyframes.add((row_id, frame))
    self._selection.primary_row_id = row_id
    self._selection.primary_frame = frame
    self._notify()
    ```
    而 `sync_from_model`（546 行）确立的不变量是：
    ```python
    self._selection.rows.add(row_id)   # 选中关键帧 ⇒ 其属性行也选中
    ```
  - 影响: 替换式选关键帧后：① 旧的非 primary 行残留高亮；② 新关键帧的行不在 `rows` 中。`apply_to_model` 按 `rows` 写 `lrow.selected`，结果"点选中而行不高亮、旧行残留高亮"——与 `sync_from_model`/`apply_to_model` 往返（生产实际使用的唯一路径，keyframes_panel.py:2156/2168）建立的状态相互矛盾，正是该类要消除的"各视图私有选择副本"问题。当前 `select_keyframe` 尚无生产调用方（面板只用 sync/apply），属接线 Spline/AI 写入口后的显现缺陷。
  - 建议: 替换模式下 `rows.clear()` 后 `rows.add(row_id)`，与 sync 不变量对齐。
  - 置信度: 中（行为可证；是否"有意半清空"无注释佐证，但与同类方法及 sync 不变量明显不一致）

- [AUD-11116] [Suggestion] `_notify` 以 `except Exception: pass` 吞掉订阅者异常，无日志
  - 位置: src/features/composite/ui/timeline_view_model.py:407-413
  - 证据:
    ```python
    for cb in list(self._subscribers):
        try:
            cb(snapshot)
        except Exception:
            # A buggy subscriber must not break selection state or other
            # subscribers. Production code may log here.
            pass
    ```
  - 影响: 与 AUD-11106 同型：逐订阅者隔离本身合理（已是 list 拷贝、继续传播），但完全静默使面板/Spline 订阅回调中的真实错误不可见，违反宽泛 except 铁律的精神。
  - 建议: 至少 `logging.exception` 保留隔离语义。
  - 置信度: 高

- [AUD-11117] [Suggestion] `build_timeline_rows` 把 `current_value` 填成"最后一个关键帧的值"而非当前帧处的值，字段语义误导
  - 位置: src/features/composite/ui/timeline_view_model.py:312
  - 证据:
    ```python
    current_value=(kfs[-1].value if kfs else None),
    ```
  - 影响: 字段名 `current_value` 与 `TimelineViewState.current_frame` 并存，AI/脚本/Graph Editor 等目标消费者（模块自述受众）会理解为"当前帧取值"，实际是末关键帧值（播放头在中间时两者不同）。无注释说明该近似。
  - 建议: 改为按当前帧插值取值，或重命名为 `last_keyframe_value` 并加注释。
  - 置信度: 中（可能是有意的占位近似，但语义误导属实）

### 通过项
- 线程安全: UI 线程数据层，无锁需求；订阅列表通知时用 `list()` 拷贝防迭代中修改
- GPU/GLSL/HDR: 不适用
- 信号防递归: 观察者模式 + `begin_batch/end_batch` 合并通知；`sync_from_model` 的 before/after 快照比较确保无变化不发事件、嵌套批次正确延迟
- 状态提交一致性: `sync_from_model`/`apply_to_model` 互为逆操作且逐字段对称；`parse_row_id` 对畸形 id 全部回落 `unknown` 不抛异常；row_id 构造/解析往返一致
- 异常与取消路径: `build_timeline_rows` 对缺 `track_for`/缺属性的 legacy 模型逐项 `getattr`/`hasattr` 防御正确
- 性能模式: 行投影/查找为线性扫描，规模与行数同阶，适配 UI 数据层
- 类型注解: 完整（含 dataclass 字段与返回类型）

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 5 | AUD-11102, AUD-11106, AUD-11110, AUD-11111, AUD-11115 |
| Suggestion | 13 | AUD-11100/11101/11103/11104/11105/11107/11108/11109/11112/11113/11114/11116/11117 |
