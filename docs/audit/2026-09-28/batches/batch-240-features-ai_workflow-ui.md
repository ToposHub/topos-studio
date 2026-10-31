# batch-240 审查报告 — src/features/ai_workflow/ui（workflow_node_search / workflow_panel）

批次：batch-240 [P2] · 编号区间：AUD-24000–AUD-24099 · 审查日期：2026-09-28
范围：`src/features/ai_workflow/ui/workflow_node_search.py`、`src/features/ai_workflow/ui/workflow_panel.py`
交叉核实文件（只读）：`workflow_canvas_view.py`、`workflow_canvas_adapter.py`、`workflow_form_page.py`、`workflow_graph_edit.py`、`undo_stack.py`、`ai_assistant_composer.py`（AiRunDrawer）、`node_specs.py`、`publish_as_skill.py`。

两文件均为纯 Qt UI（无 moderngl / composite 引入，符合 plan §1.4.1 红线），不涉及 GLSL/HDR 与 GPU 资源维度。

---

## src/features/ai_workflow/ui/workflow_node_search.py (行数: 116)

### 发现

- [AUD-24001] [Suggestion] 过滤未按文档匹配完整 description，仅匹配截断到 48 字符的描述
  - 位置: src/features/ai_workflow/ui/workflow_node_search.py:79
  - 证据:
    ```python
    desc = getattr(spec, "description", "") or ""
    label = display
    if desc:
        label = f"{display}  —  {desc[:48]}"
    ```
    而 `_filter_items`（:93-95）只对 `item.text()` 与 spec_id 做子串匹配：
    ```python
    display = item.text().lower()
    sid = (item.data(UserRole) or "").lower()
    match = (not needle) or needle in display or needle in sid
    ```
  - 影响: 模块 docstring（:12）与 `_filter_items` docstring（:87）均声称匹配 description；实际只有描述前 48 字符进入可搜索文本，用户按描述后半段关键词搜索会漏掉节点。
  - 建议: 过滤时从 UserRole 附带存完整 `description`（或单独 role），`_filter_items` 对该字段匹配；或去掉 label 截断。
  - 置信度: 高

- [AUD-24002] [Suggestion] 未使用的导入 `List`
  - 位置: src/features/ai_workflow/ui/workflow_node_search.py:21
  - 证据:
    ```python
    from typing import Any, List
    ```
  - 影响: 全文无 `List` 使用；死导入，徒增阅读噪音并触发 linter 告警。
  - 建议: 删除 `List`，仅保留 `Any`（或进一步收窄类型，见 AUD-24003）。
  - 置信度: 高

- [AUD-24003] [Suggestion] 构造参数类型注解退化为 `Any`
  - 位置: src/features/ai_workflow/ui/workflow_node_search.py:40
  - 证据:
    ```python
    def __init__(self, registry: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self._registry = registry
    ```
  - 影响: `registry` 实为 `NodeSpecRegistry`（core 层类型可得，且不违反"ui 不引 core 渲染依赖"——node_specs 是纯数据层）；`parent` 实为 Qt parent widget。`Any` 使调用方与 IDE 均失去校验。
  - 建议: `registry: NodeSpecRegistry`、`parent: Optional[QWidget] = None`（文件已 import Qt 类型可复用）。
  - 置信度: 高

### 通过项
- 线程安全：纯 QDialog 模态弹窗（`exec()` 阻塞），无跨线程状态，无后台访问。
- GPU 资源生命周期 / GLSL·HDR 正确性：不适用——无 moderngl/OpenGL 引入，符合红线（:14-17）。
- 信号防递归：`textChanged → _filter_items` 仅 `setHidden`，不改文本，无信号回路。
- 异常与取消路径：无 `except` 吞错；`hasattr` 防御 registry 缺 `all_specs`（:71）；QDialog 自带 Esc/reject。
- 输入验证：`_confirm_selection` 对空 spec_id 守卫（:110-112），不会 emit 空 id。
- 性能模式：列表线性过滤，spec 数量级小（official specs 启动一次性注册），无问题。
- 持久化/撤销维度：本文件不持有可持久化状态。

---

## src/features/ai_workflow/ui/workflow_panel.py (行数: 658)

### 发现

- [AUD-24004] [Major] 撤销栈覆盖不完整：加节点 / 加边 / 改参数不产生 undo 动作，Ctrl+Z 会错误回退更早的移动
  - 位置: src/features/ai_workflow/ui/workflow_panel.py:362-365（唯一 push 点）、:374-377（add_edge 无 push）、:399-401（set_node_param 无 push）、:437-438（add_node 无 push）
  - 证据: 全文件只有 `_on_nodes_moved` 压栈：
    ```python
    self.undo_stack.push(EditAction(
        description="move nodes",
        undo_fn=_undo, redo_fn=_redo,
    ))
    ```
    而 `_on_node_connected` / `_on_parameter_changed` / `_add_node_from_search` 改完 graph 即返回，例如：
    ```python
    try:
        self._current_graph = self._edit.add_edge(
            self._current_graph, from_node, from_port, to_node, to_port,
        )
        self.adapter.add_connection_visual(from_node, from_port, to_node, to_port)
    except ValueError:
        pass
    ```
    同时 view 的撤销请求直连该栈：
    ```python
    self.adapter.view.undo_requested.connect(self.undo_stack.undo)
    ```
  - 影响: undo_stack.py 自述"画布编辑（节点拖拽/连线/参数）需要 undo/redo"（undo_stack.py:10），但连线和参数编辑不可撤销。用户在画布加节点/连线/改参数后按 Ctrl+Z，栈顶仍是上一次"move nodes"，要么撤销了一个不相关的移动（语义错位），要么（从未移动过时）无响应——加上的节点/边/参数改动无法撤销，属状态提交一致性功能缺陷。
  - 建议: 为 add_node / add_edge / set_node_param 各自构造 EditAction（undo_fn 用 `_edit` 的对应逆操作：remove_node/remove_edge/set_node_param 回旧值 + 视觉逆操作），push 进 undo_stack；至少在文档中明确当前撤销范围。
  - 置信度: 高

- [AUD-24005] [Major] 表单页与画布 Inspector 各持参数快照，跨视图编辑后互不刷新，显示值与 Run 执行值不一致
  - 位置: src/features/ai_workflow/ui/workflow_panel.py:394-403（改参后不刷新任何视图）、:417-440（加节点后不重建表单）、:585（form_page.set_graph 仅在 _set_current_graph 调用）
  - 证据: 参数变更只写 graph：
    ```python
    def _on_parameter_changed(self, node_id: str, key: str, value: object) -> None:
        if self._current_graph is None:
            return
        try:
            self._current_graph = self._edit.set_node_param(
                self._current_graph, node_id, key, value,
            )
        except (ValueError, KeyError):
            pass
    ```
    而表单页每节点控件是从快照构建的（workflow_form_page.py:120 `inspector.set_node(node.node_id, spec, dict(node.params))`），画布 Inspector 同样只在选中时灌快照（:392 `self.inspector.set_node(node_id, spec, dict(node.params))`），两处之后都不再同步。`_add_node_from_search` 加节点后也未调用 `form_page.set_graph`。
  - 影响: (1) 表单页改参数 → 画布 Inspector 显示旧值；画布 Inspector 改参数 → 表单页控件显示旧值；(2) 画布 Tab 加的新节点永远不出现在参数表单页。Run/编译读 `current_graph`（真实新值），用户看到的却是旧值——UI 显示状态与实际执行状态失同步，可能带着错误认知发起运行。
  - 建议: `_on_parameter_changed` 成功后向另一视图广播刷新（如在 inspector/form_page 上提供 `refresh_param(node_id, key, value)`，内部用状态锁防回环）；`_add_node_from_search` 成功后增量重建或追加表单节。
  - 置信度: 高

- [AUD-24006] [Suggestion] `set_run_drawer` 的 `_updating` 锁从未被读取，是无效的装饰性防递归锁
  - 位置: src/features/ai_workflow/ui/workflow_panel.py:80、:642-646
  - 证据:
    ```python
    self._updating = True
    try:
        self.run_drawer.set_state(state)
    finally:
        self._updating = False
    ```
    全文件检索 `self._updating` 仅这两处写、无任何读（无 `if self._updating: return` 之类判断）；`run_drawer.artifact_clicked` 等信号也未被该锁拦截。
  - 影响: docstring 声称"带 ``_updating`` 锁防递归（仿 ai_edit_assistant_panel.py:795）"，但该锁不提供任何保护。当前恰无实害（已核实 AiRunDrawer.set_state 内部不同步 emit 信号，按钮信号仅在用户点击时发出，ai_assistant_composer.py:1041-1083、:1492），但这只是巧合而非防护，后续 drawer 若在 set_state 中 emit 信号即成真回路，且读者会被注释误导。
  - 建议: 要么删除该锁并修正注释；要么真正用起来——在会被 `set_state` 间接触发的槽入口处检查 `if self._updating: return`。
  - 置信度: 高

- [AUD-24007] [Suggestion] 程序化 `add_connection_visual` 会再入触发 `node_connected`，当前仅靠"重复边 ValueError 被吞"终止，且正确性依赖赋值顺序
  - 位置: src/features/ai_workflow/ui/workflow_panel.py:374-377（配合 workflow_canvas_view.py:208-213）
  - 证据: view 在**程序化**添加连线视觉时也 emit 用户信号：
    ```python
    def add_connection_item(self, conn: WorkflowConnectionItem) -> None:
        self._scene.addItem(conn)
        self._connection_items[conn.key] = conn
        self.node_connected.emit(*conn.key)
    ```
    panel 侧响应该信号又调程序化添加：
    ```python
    self._current_graph = self._edit.add_edge(...)
    self.adapter.add_connection_visual(from_node, from_port, to_node, to_port)
    ```
  - 影响: 用户拖一次连线 → panel 加边+加视觉 → view 再 emit `node_connected` → panel 再入 `_on_node_connected` → `add_edge` 因重复边抛 ValueError 被吞才终止（深度 2）。`build_from_graph` 路径同理：`_set_current_graph`（:577-581）必须**先**赋 `self._current_graph = graph` 再调 `adapter.build_from_graph(graph)`，重复边检测才成立——若顺序调换，模板图的每条边都会被重复 add_edge。整条链路无状态锁，违反"程序化更新控件须状态锁防递归信号"铁律的意图，属脆弱的隐式递归抑制。
  - 建议: `add_connection_item` 不 emit `node_connected`（把 emit 移到用户拖拽释放路径 workflow_canvas_view.py:567/570，那里本就单独 emit）；或 panel 在程序化更新期间置 `_updating` 锁并在 `_on_node_connected` 入口检查。
  - 置信度: 高（机制已逐行核实；当前行为正确但属隐式耦合）

- [AUD-24008] [Suggestion] `_on_parameter_changed` 静默吞掉 ValueError/KeyError，参数被拒时 UI 与模型失同步且无任何反馈
  - 位置: src/features/ai_workflow/ui/workflow_panel.py:398-403
  - 证据:
    ```python
    try:
        self._current_graph = self._edit.set_node_param(
            self._current_graph, node_id, key, value,
        )
    except (ValueError, KeyError):
        pass
    ```
  - 影响: `set_node_param` 对非 JSON-safe value 抛 ValueError、对未知 node_id 抛 KeyError（workflow_graph_edit.py:112-125）。被吞后控件已显示新值，graph 却保留旧值，用户无感知（无日志、无提示），叠加 AUD-24005 的快照不同步，排查困难。`.clinerules` 禁止宽泛 except 掩盖领域错误——此处虽是定向捕获，但完全静默同样掩盖领域错误。
  - 建议: 捕获后至少 `logging.warning` 并回滚控件显示（或在 Inspector 上打错误态）；KeyError（节点已不存在）可降级为忽略+debug 日志。
  - 置信度: 高

- [AUD-24009] [Suggestion] `set_context_snapshot` 对 `to_public_dict()` 返回值不校验，非 dict 返回会让槽内 AttributeError
  - 位置: src/features/ai_workflow/ui/workflow_panel.py:455-456、:462-470
  - 证据:
    ```python
    getter = getattr(context, "to_public_dict", None)
    data = context if isinstance(context, dict) else (getter() if callable(getter) else {})
    values = {
        "project": data.get("project_id", "—"),
        ...
    ```
  - 影响: 方法签名 `context: object | None` 面向任意宿主对象；若传入对象的 `to_public_dict` 返回 None/列表，`data.get(...)` 直接 AttributeError，Qt 槽内异常打印 traceback、快照渲染中断。属外部数据未验证。
  - 建议: `data = getter() if callable(getter) else {}`，随后 `if not isinstance(data, dict): data = {}`（或记 warning）。
  - 置信度: 高（防御缺失确定；实际触发取决于调用方）

- [AUD-24010] [Suggestion] 大量 UI 字符串硬编码中文未走 tr()，与同文件 tr() 用法不一致
  - 位置: src/features/ai_workflow/ui/workflow_panel.py:121、:124、:159-163、:169、:176-184、:191、:502-504、:512、:514-520、:598
  - 证据（节选）:
    ```python
    self.form_view_button = QPushButton(tr("workflow.toolbar.form", "表单"), self)
    ...
    ("project", "工程"), ("timeline", "时间线"),
    ...
    self.preview_plan_button = QPushButton("预览计划", self)
    self.approval_button = QPushButton("请求审批", self)
    self.apply_plan_button = QPushButton("应用已批准计划", self)
    ...
    self.plan_status_label = QLabel("暂无计划", self)
    ```
  - 影响: 同一文件里 Load/Clear/Run/Cancel/empty_hint 走 `tr(...)` 而视图切换、审批工作区、上下文条、Release Gate 文案全部硬编码；切换语言时该面板半中半英，i18n 覆盖破碎。
  - 建议: 统一改为 `tr("workflow.xxx", "默认中文")` 键值形式。
  - 置信度: 高

- [AUD-24011] [Suggestion] 上下文标签字典在 `_build_ui` 与 `set_context_snapshot` 两处重复定义，漂移即 KeyError
  - 位置: src/features/ai_workflow/ui/workflow_panel.py:158-163 与 :457-461
  - 证据:
    ```python
    # _build_ui
    for key, label in (
        ("project", "工程"), ("timeline", "时间线"),
        ("brief", "Brief"), ("revision", "Revision"),
        ("assets", "素材"), ("provider", "Provider"),
        ("permission", "权限"),
    ):
    ```
    ```python
    # set_context_snapshot
    labels = {
        "project": "工程", "timeline": "时间线",
        "brief": "Brief", "revision": "Revision",
        "assets": "素材", "provider": "Provider",
        "permission": "权限",
    }
    ```
    且 `:472` 用 `self.context_labels[key]` 直取。
  - 影响: 两处 7 个 key 目前一致；将来任一处增删 key（如新增 "model"），`set_context_snapshot` 会 KeyError 崩槽或漏渲染，重复定义无编译期保护。
  - 建议: 提为模块级常量 `_CONTEXT_LABELS`（或类属性），两处共用。
  - 置信度: 高

- [AUD-24012] [Suggestion] 关键公共接口缺类型注解
  - 位置: src/features/ai_workflow/ui/workflow_panel.py:66-72（`registry`、`template_registry`）、:569（`load_graph(self, graph, ...)`）、:636（`set_run_drawer(self, state)`）、:555-558（`current_graph` property 无返回注解）
  - 证据:
    ```python
    def __init__(
        self,
        *,
        registry,
        template_registry,
        parent: Optional[QWidget] = None,
    ) -> None:
    ```
    ```python
    def load_graph(self, graph, skill_id: str = "") -> None:
    ...
    def set_run_drawer(self, state) -> None:
    ```
  - 影响: `graph` 实为 `WorkflowGraph`、`state` 实为 `RunDrawerState`、registry 实为 `NodeSpecRegistry`/`TemplateRegistry`，均为可静态引用的纯数据类型；缺注解使 host/controller 接线失去静态校验，违背项目强类型约定。
  - 建议: 补全为具体类型（import 放方法内延迟亦可，与现有 deferred import 风格一致）。
  - 置信度: 高

- [AUD-24013] [Suggestion] `"timeline" + "_write_requires_approval"` 拼接常量，可读性与可检索性差
  - 位置: src/features/ai_workflow/ui/workflow_panel.py:487
  - 证据:
    ```python
    write_key = "timeline" + "_write_requires_approval"
    diff = {
        ...
        "write_requires_approval": bool(data.get(write_key, False)),
    ```
  - 影响: 字面量 `"timeline_write_requires_approval"` 被人为拆开，grep 该 schema 键定位不到本处；读者需心算拼接，疑似规避某种键名扫描，无注释说明动机。
  - 建议: 直接写 `data.get("timeline_write_requires_approval", False)`；若确有扫描规避需求，加注释说明。
  - 置信度: 高

### 通过项
- GPU 资源生命周期 / GLSL·HDR 正确性：不适用——无 moderngl/OpenGL/composite 引入，符合红线（:20-23）；无浮点字面量与循环边界问题（无 GLSL）。
- 线程安全（UI 边界内）：所有 `_current_graph` 变更均发生在 Qt 信号槽（UI 线程）；`WorkflowGraph` 为 frozen 数据模型，替换为原子赋值，controller 经 property 读取得到的是不可变快照，无半更新状态外泄。
- 撤销栈生命周期：`_set_current_graph`（:583）与 `clear_canvas`（:594）均 `undo_stack.clear()`，图替换/清空后无陈旧 undo 闭包残留（已核对全部 `_current_graph` 赋值路径，节点集合只增不减，`move_nodes` 的 KeyError 前置 `require_node` 不会触发；UndoStack 对闭包异常也有兜底日志）。
- 信号防递归（视觉位置回放）：undo/redo 闭包走 `adapter.set_node_position`，该路径不 emit `nodes_moved`（workflow_canvas_adapter.py:167-173 已注明），无回环。
- 取消路径：`cancel_requested` 信号 + `set_running` 切换 Run/Cancel 使能逻辑自洽（run 中禁 Run 启 Cancel，结束后按 node_count 恢复）。
- 输入验证（多数路径）：`_on_load_clicked` idx<=0 守卫（:299）、`load_template` entry None 守卫（:563-564）、`_on_add_node_requested` 的 spec None 守卫（:421-422）、`_on_node_selected` 的 node/spec None 守卫（:386-391）均到位。
- 授权状态一致性：`_set_current_graph`/`clear_canvas` 均重置 `_approval_grant_id` 并经 `set_plan_preview(can_approve=False)`→`set_approval_state("")` 收回审批按钮，graph 更换后旧 grant 不会被误用。
- 性能模式：无逐帧/每像素逻辑；`_populate_template_combo` 仅构造时一次；`_filter_items` 线性扫描规模小；`json.dumps` 只在低频 setter 中调用。
- 节点搜索弹窗缓存：懒建后 `_populate` 不再刷新，但已核实 NodeSpecRegistry 仅在启动时 `register_official_specs` 一次性填充（dock_layout_controller.py:700-701；publish_as_skill 注册的是 SkillRegistry 而非 node spec），静态数据下无实际陈旧风险。

---

## 统计

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 2 | AUD-24004, AUD-24005 |
| Suggestion | 11 | AUD-24001–24003, AUD-24006–24013 |
