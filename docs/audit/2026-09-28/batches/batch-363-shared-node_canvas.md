# 批次报告：batch-363 [P2] src/shared/node_canvas/

编号区间：AUD-36300–AUD-36399。文件 2 个，共 440 行。
汇总：Critical 0 / Major 1 / Suggestion 3。
交叉验证（只读 grep）：模块被 `src/features/ai_workflow/ui/`（workflow_panel / canvas_view / canvas_adapter / node_item / connection_item）及 `tests/test_node_canvas_protocol.py` 引用；`GraphUiSignals` 全仓库仅以类属性方式使用（未实例化）；`UndoStack` 仅在 `workflow_panel.py` 于 UI 线程使用，`changed` 信号当前无宿主连接。

## src/shared/node_canvas/graph_ui_protocol.py (行数: 312)

### 发现

- [AUD-36301] [Suggestion] `GraphUiSignals` 以 `@dataclass(frozen=True)` 实现常量命名空间，`ALL` 被注解后意外成为实例字段
  - 位置: src/shared/node_canvas/graph_ui_protocol.py:154-185
  - 证据:
    ```python
    @dataclass(frozen=True)
    class GraphUiSignals:
        """画布必须发出的信号名集合（Protocol 契约文档）。"""
        ...
        ALL: Tuple[str, ...] = (
        NODE_ADDED, NODE_REMOVED, NODE_CONNECTED, NODE_DISCONNECTED,
        ...
        ASSETS_DROPPED,
    )
    ```
    （`NODE_ADDED` 等常量无类型注解故不是字段；`ALL: Tuple[str, ...]` 是类体内唯一被注解的属性。）
  - 影响: dataclass 会把 `ALL` 当作字段并生成 `__init__`，`GraphUiSignals(ALL=("x",))` 可构造出与契约不一致的实例；`dataclasses.fields()` / `asdict()` 的语义与"纯常量命名空间"的意图相悖，易误导后续维护者。当前仓库只以 `GraphUiSignals.ALL` 类属性方式使用（grep 验证），故为潜在问题而非现行故障。
  - 建议: 移除 `@dataclass` 装饰改为普通常量类；或将 `ALL` 注解改为 `ClassVar[Tuple[str, ...]]` 使其不成为实例字段。
  - 置信度: 高

### 通过项
- 线程安全：全部为 frozen dataclass + 元组的不可变数据；`DEFAULT_VISUAL_TOKENS` 单例只读共享，无可变共享状态。
- GPU 资源生命周期：不 import `moderngl` / `composite.core`，无 GPU 资源，符合模块红线（docstring 声明与代码一致）。
- GLSL·HDR 正确性：不适用（无着色器/像素处理代码）。
- 信号防递归：仅声明信号名字符串常量，模块自身无 emit 逻辑。
- 状态提交一致性：不涉及 undo/redo/revision/持久化。
- 异常与取消路径：`NodeCanvasBase` 未实现方法统一 `raise NotImplementedError`，无吞异常路径。
- 输入验证：`node_token` / `port_token` / `status_color` 对未知 category/artifact_type/status 返回明确的灰色 fallback 令牌，行为有测试覆盖（test_node_canvas_protocol.py）。
- 性能模式：token 查找为 ≤12 元素的线性扫描，仅在节点/端口创建或状态变更时调用，非每帧热路径；浮点字段均显式 `.2`/`.0` 形式（0.2/3.0）。
- 类型注解：完整（含返回类型与 `Tuple[...]` 精确类型）。

## src/shared/node_canvas/undo_stack.py (行数: 128)

### 发现

- [AUD-36302] [Major] `undo_fn` / `redo_fn` 抛错被宽泛 `except Exception` 吞掉后动作仍在双栈间转移，栈状态与模型状态可能失配
  - 位置: src/shared/node_canvas/undo_stack.py:97-119
  - 证据:
    ```python
    action = self._undo_list.pop()
    try:
        action.undo_fn()
    except Exception:  # noqa: BLE001 — 不让 undo_fn 抛错污染栈
        _LOG.exception("undo_fn raised; ignoring (action=%r)", action.description)
    self._redo_list.append(action)
    self.changed.emit()
    ```
    （`redo()` 在 109-119 行对 `redo_fn` 有完全对称的结构。）
  - 影响: 若闭包在半途抛错（例如 workflow_panel 的 `_apply` 先改了 `self._current_graph` 再改视觉时中途失败），动作仍进入 `_redo_list` 且 `changed` 照常发射——UI 按钮态显示操作"成功"，随后 redo 会 在不一致状态上执行 `redo_fn`，可能导致图状态静默损坏。这是项目铁律"不得宽泛 except 掩盖领域错误"的边界情形：虽有 `_LOG.exception`（非静默）且为 docstring 文档化的设计取舍，但领域错误对调用方/host 完全不可见，无恢复路径。
  - 建议: 增加失败通知路径（如 `error = pyqtSignal(str)` 或可选回调）让 host 触发全量重建（`build_from_graph`）；或仅在闭包成功时转移动作、失败时回插原栈并 emit 失败信号。
  - 置信度: 高（行为确定；因属文档化设计取舍的边界风险，定 Major 而非 Critical）

- [AUD-36303] [Suggestion] 撤销栈无深度上限，也无 move 类动作合并（coalesce）机制
  - 位置: src/shared/node_canvas/undo_stack.py:91-95
  - 证据:
    ```python
    def push(self, action: EditAction) -> None:
        """压入一个动作；清空 redo 分支。"""
        self._undo_list.append(action)
        self._redo_list.clear()
        self.changed.emit()
    ```
  - 影响: 每个动作持有捕获旧/新位置映射的闭包（host workflow_panel.py 的 `_apply(old_map)` / `_apply(new_map)` 捕获整个 moves 字典），长编辑会话下 `_undo_list` 无界增长造成内存膨胀；对比被精简替代的 Composite 版（含 coalesce/persist）连最基本的深度上限也没有。
  - 建议: 增加可配置 `max_depth`（如默认 200），push 时裁剪最旧动作；可选对相同 description 的连续 move 动作做合并。
  - 置信度: 高

- [AUD-36304] [Suggestion] `EditAction` 的 `undo_fn` / `redo_fn` 默认为无操作闭包，漏传时静默无错误
  - 位置: src/shared/node_canvas/undo_stack.py:51-53
  - 证据:
    ```python
    description: str = ""
    undo_fn: Callable[[], None] = field(default=lambda: None)
    redo_fn: Callable[[], None] = field(default=lambda: None)
    ```
  - 影响: 调用方若忘记传闭包，`EditAction()` 仍合法构造；push 时照常清空 redo 分支（丢弃历史），undo/redo 时静默无操作且不产生任何错误信号。当前唯一宿主 workflow_panel.py:362 显式传入两个闭包，属潜在风险而非现行缺陷。
  - 建议: 去掉默认值使两字段必填；或 push 时校验闭包非默认空操作并告警。
  - 置信度: 高

### 通过项
- 线程安全：纯 `QObject`，仅在 UI 线程创建与调用（workflow_panel.py 中由 `undo_requested` / `redo_requested` 信号直连 `undo` / `redo` 槽），无跨线程访问路径，无需锁。
- GPU 资源生命周期：无 OpenGL / GPU 资源，无 moderngl / composite import，符合红线（docstring 声明与代码一致）。
- GLSL·HDR 正确性：不适用。
- 信号防递归：`changed` 为无 payload 的尾随 emit；宿主刷新按钮态不会回调 push/undo/redo（当前宿主尚未连接 `changed`，grep 验证）。
- 状态提交一致性：push 清空 redo 分支、undo/redo 双栈转移对称、clear 双清 + emit，线性撤销栈语义自洽。
- 异常与取消路径（除 AUD-36302 外）：空栈 undo/redo 为 no-op 前置检查，无其他异常路径。
- 输入验证：`can_undo` / `can_redo` / `depth` 查询接口行为明确。
- 性能模式：栈操作均为 O(1)（push/undo/redo）或 O(n) 仅 clear；无热路径问题。
- 类型注解：完整（`Callable[[], None]`、`QObject | None`、`List[EditAction]`）。
