# Batch 049 审查报告：src/features/composite/core（layer_stack_compiler / layer_stack_controller）

- 批次：batch-049 [P1]
- 编号区间：AUD-4900–AUD-4999（实际使用 AUD-4900–AUD-4915）
- 审查日期：2026-09-28
- 审查方式：两文件全量通读；关键结论交叉验证于 `src/features/composite/core/node_engine.py`（NodeGraph API/锁/generation）、`src/features/composite/nodes/blend_node.py`（MergeNode 参数）、`src/features/composite/core/shaders.py`（merge opacity 语义）、`src/features/composite/core/layer_model.py`（模型契约）、`src/features/composite/core/undo_stack.py`（merge_key）、`tests/composite/test_layer_stack_controller.py`、`src/features/composite/ui/composite_page.py`（sync 调用点）
- 统计：Critical 1 / Major 4 / Suggestion 11

---

## src/features/composite/core/layer_stack_compiler.py (行数: 702)

### 发现

- [AUD-4900] [Major] 编译器对已存在 Merge 节点立即写参数，“干跑”编译实际产生了图变异且不受 undo 约束
  - 位置: src/features/composite/core/layer_stack_compiler.py:694-702（及 674-691、602-625）
  - 证据:
    ```python
    def _project_param(node: Any, key: str, value: Any,
                       result: CompileResult, node_id: str) -> None:
        """Project immediately when possible, or defer until node creation."""
        if node is None:
            result.changes.append(CompileChange(
                kind="set_parameter", node_id=node_id,
                param_name=key, value=value))
            return
        _try_set_param(node, key, value, result, node_id)
    ```
    而 `_try_set_param` 内 `setter = getattr(node, "set_parameter", None); if callable(setter): setter(key, value)` 直接改写活动节点；`_project_layer_state`（602-625）对每个 `i < len(merge_ids)` 的层调用它。
  - 影响: `LayerStackController.sync_from_node_graph(repair=False)`（composite_page.py:960 在节点编辑后调用）文档承诺“干跑、只报告、不改图”，但其内部 `compiler.compile()` 会把 blend_mode/opacity/mask_channel/mask_invert 立即写到已回收的 auto-Merge 节点上。这些写入 (a) 违反 dry-run 契约，(b) 不在任何 undo 快照事务内——undo 无法回退它们，后续操作的 before 快照会把被污染状态固化为“历史”。
  - 建议: `_project_param` 一律记录 `set_parameter` CompileChange，由控制器在应用阶段（且仅在 repair/正常事务内）统一落盘；或至少为 compile 增加 `dry_run` 标志屏蔽 eager 写。
  - 置信度: 高

- [AUD-4901] [Major] opacity/blend 投影落点错位：中间层 opacity 调制的是“上方整组”，底层与单层栈完全不投影（文档承诺的 Transform 回退未实现）
  - 位置: src/features/composite/core/layer_stack_compiler.py:602-619（映射逻辑）；佐证：src/features/composite/core/shaders.py:380 “opacity modulates the foreground”
  - 证据:
    ```python
    for i, lid in enumerate(layer_ids):
        layer = layers.get(lid)
        if layer is None or not layer.managed:
            continue
        if i < len(merge_ids):
            merge_id = merge_ids[i]
            ...
            _project_param(node, "blend_mode", bm, result, merge_id)
            _project_param(node, "opacity", layer.opacity, result, merge_id)
    ```
    拓扑为 `M0(fg=root[0], bg=root[1]) → M1(fg=M0.result, bg=root[2])`，shader 中 opacity 只调制 fg。
  - 影响: 对 `[A,B,C]`：B(i=1) 的 opacity 写到 M1，M1 的 fg 是 (A∘B) 整组——调暗 B 会连带调暗 A，违背 layer_model.py:124-126 声明的 AE 语义（“mirror the Merge node that combines this layer with the one below”）；C(底层) 与单层栈（无 Merge）没有任何投影目标，`set_layer_opacity` 静默无效——tests/composite/test_layer_stack_controller.py:506-517 自己也只敢断言 “no owning merge ... just assert no crash”。方法 docstring（585-591 行）承诺的 “drive Transform.opacity if the root supports it” 回退在代码中不存在。
  - 建议: 顶层之外改用“每层一个调制点”的正确拓扑（如底层 opacity 投影到其 Transform/root 节点，或改为 fg=root[i] 的逐层合并链）；至少实现 docstring 承诺的 Transform.opacity 回退，覆盖单层与底层。
  - 置信度: 高

- [AUD-4902] [Suggestion] 新建 Merge 的摆放基准点计算后从未使用，`_merge_placement_hint` 实为死代码
  - 位置: src/features/composite/core/layer_stack_compiler.py:286-294（及 298-309）
  - 证据:
    ```python
    # Create the shortfall.  Position them in a column to the left
    # of the output for readability.
    base_x, base_y = self._merge_placement_hint(stack)
    for i in range(len(existing), needed):
        mid = self._new_node_id(prefix="layer_merge")
        result.changes.append(CompileChange(
            kind="create_node", node_id=mid,
            node_type=MERGE_NODE_TYPE))
        existing.append(mid)
    ```
  - 影响: `base_x/base_y` 未被引用，create_node 变更也不携带 position；自动 Merge 全部落到节点默认位置（相互堆叠），注释承诺的“输出左侧一列”布局从未生效。
  - 建议: 在 CompileChange 上增加可选 position 字段（或复用 set_parameter 记录），由控制器创建节点时写入 `node.position = (base_x, base_y + i * step)`。
  - 置信度: 高

- [AUD-4903] [Suggestion] `_layer_is_live` 每层重扫全表判断 solo，编译复杂度 O(N²)
  - 位置: src/features/composite/core/layer_stack_compiler.py:235-238
  - 证据:
    ```python
    # Solo handling: if any layer is soloed, non-solo layers drop out.
    any_solo = any(l.solo for l in layers.values())
    if any_solo and not layer.solo:
        return False
    ```
  - 影响: `compile()` 对 stack.layer_ids 逐层调用 `_layer_is_live`，每次全量扫描 layers；大合成（数百层）下每次属性变更触发的重编译都是 O(N²)。编译处于交互热路径（任何 set_layer_property 都会触发）。
  - 建议: 在 `compile()` 顶部一次性计算 `any_solo` 并传入（与 `_CompileCtx` 同样的 per-compile 缓存模式）。
  - 置信度: 高

- [AUD-4904] [Suggestion] `_CompileCtx.managed_node_ids` 类型注解退化为裸 `set`
  - 位置: src/features/composite/core/layer_stack_compiler.py:60
  - 证据: `managed_node_ids: set = field(default_factory=set)`
  - 影响: 同类字段 `input_index` 均带完整泛型（`Dict[Tuple[str, str], str]`）；裸 `set` 丢失元素类型信息，不符合项目强类型约束。
  - 建议: 改为 `managed_node_ids: set[str] = field(default_factory=set)`（或 `Set[str]` 与文件其余风格一致）。
  - 置信度: 高

### 通过项
- 线程安全：编译器实例除 graph 引用外无状态（_CompileCtx 为局部对象），无自有线程/锁，符合 UI 线程内使用约定
- GPU 资源生命周期：不直接接触 ModernGL/纹理；节点删除仅以 remove_node 变更记录形式输出，由控制器经 graph API 执行（GPU 清理按 NodeGraph.remove_node 注释的渲染线程路径处理）
- GLSL/HDR 正确性：不产生 GLSL；投影的 opacity 值域在 Layer.from_dict / 控制器处已钳制到 [0,1]，无对 HDR 颜色的无理由 clamp 路径
- 信号防递归：编译器不持有任何 Qt 信号
- 输入验证：缺失 layer/root 一律降级为 result.warnings 并跳过，不抛异常；matte 缺失时回退为无 matte 连接并告警
- 冲突检测：_detect_chain_conflict 在任何变更产出之前运行，命中即整体不动图（ok=False + conflict_reason），且允许列表包含全部层的 root/source 以区分“陈旧连接”与“用户改线”
- 幂等与回收：existing Merge 复用、surplus 记录删除、shortfall 记录创建，重跑不产生重复节点；变更顺序为 create/remove → connect/disconnect → set_parameter，供控制器按序应用

---

## src/features/composite/core/layer_stack_controller.py (行数: 1976)

### 发现

- [AUD-4905] [Critical] `_recompile_stack_recursive` 丢弃嵌套（Precomp 子栈）编译产生的变更：只聚合父栈 changes，子栈 create/connect 变更从未被应用，但子栈 `generated_merge_node_ids` 已被改写
  - 位置: src/features/composite/core/layer_stack_controller.py:1684-1704
  - 证据:
    ```python
    child_result = self._recompile_stack_recursive(child, layers, active)
    aggregate.warnings.extend(child_result.warnings)
    if child_result.conflict:
        return child_result
    # Child changes are appended below and applied in order together
    # with the parent so the whole nested compile is one undoable
    # graph mutation.

    parent_result = self.compiler.compile(stack, layers)
    aggregate.warnings.extend(parent_result.warnings)
    aggregate.merge_node_ids = parent_result.merge_node_ids
    aggregate.changes.extend(parent_result.changes)
    ```
    全函数只有最后一处 `aggregate.changes.extend(parent_result.changes)`，`child_result.changes` 从未并入；而 1702-1703 行 `stack.generated_merge_node_ids = list(parent_result.merge_node_ids)` 在递归的每一层都会写回。
  - 影响: 当主栈操作（add/remove/move/set_layer_property/rebuild）触发的递归编译发现子栈需要拓扑修复（典型：用户在节点模式删掉了子栈的 auto-Merge，或子栈连接漂移）时，子栈的 create_node/connect 变更被静默丢弃——Precomp 内容按旧/断链渲染（渲染错误），且子栈 `generated_merge_node_ids` 已被覆盖为“从未创建的节点 id”，该损坏状态会随工程保存持久化。注释本身明确声称 “Child changes are appended below”，实现与设计矛盾（漏掉 `aggregate.changes.extend(child_result.changes)` 一行）。直接以 `stack_id=child` 操作时可绕过（子栈作为顶层编译、变更正常应用，tests/composite/test_layer_stack_controller.py:64-85 仅覆盖该路径），故平时被掩盖。
  - 建议: 在递归返回处按序 `aggregate.changes.extend(child_result.changes)`（子先父后，与注释一致）；同时为“主栈 rebuild 应修复嵌套栈”补一条回归测试。`sync_from_node_graph(repair=True)` 也只编译 main 栈，建议同样走递归路径。
  - 置信度: 高

- [AUD-4906] [Major] `duplicate_layer` 复制 Precomp 层时共享同一个 child_stack_id：双引用别名 + 删除任一层会拔掉对方的子栈
  - 位置: src/features/composite/core/layer_stack_controller.py:1099-1113（Layer 构造）与 1133-1143（_clone_node）；关联删除路径 906-927
  - 证据:
    ```python
    def _clone_node(self, node_id: str) -> Optional[str]:
        node = self.graph.get_node(node_id)
        if node is None:
            return None
        new = node.__class__.from_dict(node.to_dict())
        # Generate a fresh id (avoid collisions).
        new.id = self._new_node_id(prefix=f"{node.node_type}_dup")
    ```
    PrecompNode 的 `child_stack_id` 参数随 to_dict/from_dict 原样复制，duplicate_layer 未创建新 LayerStack 也未改写该参数。
  - 影响: 复制出的 Precomp 层与原层指向同一子栈——编辑其一即同时改两个；随后 `remove_layer` 任意一份时会按该层的 `child_stack_id` 执行 `_nested_stack_ids` → `remove_layer_stack` + 节点回收（915-996 行），存活的那份 Precomp 留下悬空 `child_stack_id`（渲染为空 + “no child stack” 告警）。AE 语义下 Ctrl+D 的 Precomp 应是独立合成。
  - 建议: duplicate_layer 对 `layer_type == LAYER_TYPE_PRECOMP` 走专门分支：深拷贝子栈（及子层节点）、生成新 stack_id 并写回克隆节点的 `child_stack_id`；或至少在复制 Precomp 时拒绝并提示。
  - 置信度: 高

- [AUD-4907] [Major] 属性/移动/复制操作全部锚定 main 栈：嵌套栈层的 parent 与 track_matte 校验对着 main 的 layer_ids 比对，必然失败；move/duplicate 直接不支持嵌套层
  - 位置: src/features/composite/core/layer_stack_controller.py:1170-1172（stack 取值）、1200-1205（matte 校验 `matte_stack is not stack`）、1222-1224（`value not in stack.layer_ids`）、1016-1017（move_layer）、1059-1060（duplicate_layer）、1329（set_layer_parent）
  - 证据:
    ```python
    def set_layer_property(self, layer_id: str, prop: str, value: Any, ...):
        ...
        stack = self.main_stack()
        ...
        elif prop == "track_matte_layer_id":
            if value is not None:
                matte_stack = self._stack_for_layer(value)
                if (value == layer_id or self.graph.get_layer(value) is None
                        or matte_stack is not stack):
                    return False
        ...
        elif prop == "parent_layer_id" and value is not None:
            if value not in stack.layer_ids or value == layer_id:
                return False
    ```
  - 影响: 对 Precomp 子栈内的层：`parent_layer_id`/`track_matte_layer_id`（经 set_layer_property 通用路径）与 `set_layer_parent` 的校验全部针对 main 栈的成员表——合法的同子栈父层/matte 层被判为非法，静默 return False（功能缺陷）；`move_layer`/`duplicate_layer` 对嵌套层直接返回 False/None。对比之下 `set_layer_track_matte`（1289 行）用 `_stack_for_layer` 是正确写法，说明这是遗漏而非设计。注：opacity 等非校验属性因递归编译（1692 行）仍能生效，掩盖了部分症状。
  - 建议: `set_layer_property`/`set_layer_parent`/`move_layer`/`duplicate_layer` 统一改用 `self._stack_for_layer(layer_id)` 定位所属栈（缺失再回退 main），校验与 `_recompile`、`emit_changed` 均作用于真实所属栈。
  - 置信度: 高

- [AUD-4908] [Suggestion] 异常回滚不一致：新流程有 `_restore(before)`，老流程（add_layer/add_media_layer/remove_layer/move_layer/set_layer_property/duplicate_layer）中途异常留下半套变更且无 undo 记录
  - 位置: src/features/composite/core/layer_stack_controller.py:380-403（add_layer）、493-568（add_media_layer）、902-1009（remove_layer）、1019-1040（move_layer）、1234-1258（set_layer_property）、1065-1131（duplicate_layer）；对照 435-453、696-720、746-792 的回滚版本
  - 证据: add_media_payload 的模式：
    ```python
    try:
        ...
        return self.add_media_layer(...)
    except Exception:
        self._restore(before)
        raise
    ```
    而 add_layer 在 `stack.layer_ids.insert` + `upsert_layer` 之后直接调用 `self._recompile(stack)`，无 try/except。
  - 影响: 一旦 `_recompile`/`_snapshot`/注入的 node_factory 在变更中段抛出（如自定义工厂抛错、to_dict 序列化失败），层已插入、链路半接线，既没有回滚也没有 undo 条目——违反模块 docstring 声明的“一次操作一个 undo 步”的原子性契约（plan §10）。
  - 建议: 为所有变更型公开方法统一套用 `try: ... except: self._restore(before); raise` 模板（可提炼为装饰器/上下文管理器）。
  - 置信度: 中

- [AUD-4909] [Suggestion] `_apply_changes` 硬编码绝对导入 MergeNode，绕过注入的 node_factory
  - 位置: src/features/composite/core/layer_stack_controller.py:1719-1722
  - 证据:
    ```python
    if ch.kind == "create_node":
        if ch.node_type == MERGE_NODE_TYPE:
            from src.features.composite.nodes.blend_node import MergeNode
            node = MergeNode(node_id=ch.node_id)
        else:
            node = self._create_node(ch.node_type, ch.node_id)
    ```
  - 影响: 构造器注入的 `node_factory`（文档说明“Injected so tests can swap it”）对 merge 创建不生效——测试/脚本环境的替身与生产行为分叉；`src.` 绝对导入也与本模块其余相对导入风格不一致（包根变化时失效）。
  - 建议: 删除特判，统一走 `self._create_node(ch.node_type, ch.node_id)`（默认工厂本就经 NODE_REGISTRY 解析 "merge"）。
  - 置信度: 高

- [AUD-4910] [Suggestion] `_new_layer_id` 的首个成员测试恒为 False（str 与 Layer 对象比较）
  - 位置: src/features/composite/core/layer_stack_controller.py:1965-1967
  - 证据:
    ```python
    # Avoid collisions with existing layer ids.
    while cand in self.graph.get_all_layers() or \
            self.graph.get_layer(cand) is not None:
    ```
  - 影响: `get_all_layers()` 返回 Layer 对象列表，`cand in [...]` 永远不命中，去重实际只靠第二个条件；条件具有误导性，读者会误以为存在第二种冲突源。
  - 建议: 删除 `cand in self.graph.get_all_layers()` 子句，或改为对 layer_id 集合的判断。
  - 置信度: 高

- [AUD-4911] [Suggestion] 每次属性变更（含可合并的 opacity 拖动）都做全图快照，交互热路径成本高
  - 位置: src/features/composite/core/layer_stack_controller.py:1234（set_layer_property 的 before）、1795-1830（_snapshot 全量序列化）
  - 证据:
    ```python
    before = self._snapshot()
    ...
    self._push_undo(action_type, before, after, ...)
    ```
    `_snapshot` 对 `self.graph.nodes.items()` 全量 `to_dict()` + 全部连接 + 表达式表。
  - 影响: 拖动 opacity 滑杆（mergeable=True，merge_key 只合并 undo 记录）时每个 tick 都序列化整图节点参数/关键帧，大工程（数百节点）下交互明显卡顿；这是纯 Python 层的重复深序列化。
  - 建议: 对 mergeable 的属性引入节流/延迟快照（拖动结束时才取 after），或为属性类操作实现“仅记录 layer 单项 + 参数旧/新值”的轻量 undo 动作，结构操作才走全图快照。
  - 置信度: 高

- [AUD-4912] [Suggestion] undo/恢复链路上的宽泛 `except` 可能掩盖领域错误，静默丢失节点/连接
  - 位置: src/features/composite/core/layer_stack_controller.py:1924-1927（_push_undo 吞掉 UndoAction 导入失败→undo 被静默禁用）、1867-1881（from_dict 失败 continue，节点无声消失）、1886-1890（connect 失败 pass）、1913-1916（set_expression 失败 pass）
  - 证据:
    ```python
    try:
        from .undo_stack import UndoAction
    except Exception:
        return
    ...
    try:
        rebuilt = node_cls.from_dict(nd)
    except Exception:
        continue
    ```
  - 影响: .clinerules 明确禁止宽泛 except 掩盖领域错误。这里一旦注册表缺类（`NODE_REGISTRY.get` 为 None 的分支同样静默 `continue`，1868-1870）或 from_dict 抛错，undo 后图会缺节点/缺连接且无任何日志——用户视角即“undo 弄丢了我的节点”，极难排查。
  - 建议: 至少为每个静默分支补 `logger.warning`（携带 node_id/原因）；`_push_undo` 的导入失败应显式报错或降级日志。
  - 置信度: 高

- [AUD-4913] [Suggestion] 输入验证缺口：label_color 任意值放行、add_media_layer 的 layer_type 未校验
  - 位置: src/features/composite/core/layer_stack_controller.py:1173-1233（mutable_props 无 label_color 分支）、467-475（layer_type 形参直接透传）
  - 证据:
    ```python
    mutable_props = {
        "name", "enabled", "solo", "locked", "shy", "label_color",
        ...
    }
    if prop not in mutable_props or not hasattr(layer, prop):
        return False
    if prop == "name":
        ...
    elif prop in {"enabled", "solo", ...}:
    ```
    label_color 没有对应校验分支；`add_media_layer(..., layer_type: str = "media")` 也未对照 `ALL_LAYER_TYPES` 校验。
  - 影响: 外部/脚本调用可写入非字符串 label_color 或任意 layer_type 并持久化到工程文件，下游 UI/加载端必须额外容错（Layer.from_dict 也只矫正 opacity/blend/matte，不矫正这两项）。
  - 建议: 为 label_color 增加字符串/颜色格式校验；layer_type 对照 `ALL_LAYER_TYPES` 白名单。
  - 置信度: 高

- [AUD-4914] [Suggestion] `duplicate_layer` 丢失 solo/parent_layer_id/track_matte_* 字段
  - 位置: src/features/composite/core/layer_stack_controller.py:1099-1113
  - 证据:
    ```python
    new_layer = Layer(
        layer_id=new_layer_id,
        name=f"{layer.name} copy",
        root_node_id=new_root_id or layer.root_node_id,
        source_node_id=new_source_id or layer.source_node_id,
        layer_type=layer.layer_type,
        blend_mode=layer.blend_mode,
        opacity=layer.opacity,
        label_color=layer.label_color,
        enabled=layer.enabled,
        shy=layer.shy,
        in_frame=layer.in_frame,
        out_frame=layer.out_frame,
        managed=layer.managed,
    )
    ```
  - 影响: 复制 solo 层得到的是非 solo 副本——在 any_solo 语义下副本立即从渲染中消失（用户以为复制失败）；AE 的 Ctrl+D 保留 parenting，此处丢失。matte 不复制可能是有意（AE 保持 matte 独立）但未注释说明。
  - 建议: 至少复制 `solo`；`parent_layer_id` 复制（同栈校验已由 _would_create_parent_cycle 兜底）；matte 字段的行为加注释明确意图。
  - 置信度: 中

- [AUD-4915] [Suggestion] `_restore` 绕过 NodeGraph 的 RLock 直接清写内部容器
  - 位置: src/features/composite/core/layer_stack_controller.py:1843-1852、1884
  - 证据:
    ```python
    self.graph._layer_stacks.clear()
    ...
    self.graph._layers.clear()
    ...
    # Connections: rebuild from snapshot.
    self.graph.connections = []
    ```
  - 影响: NodeGraph 的所有公开变更方法（add_node/remove_node/connect/...）都在 `with self._lock` 下操作；_restore 成批清空/重建 `_layer_stacks`、`_layers`、`connections` 时不持锁。当前依赖“全部发生在 UI 线程”的约定（snapshot 文档要求 UI 线程调用）才无竞态，但任何后台线程一旦按 `_lock` 的存在假设读取图（锁的存在本身即信号），undo/redo 窗口内会读到撕裂状态；也与 graph 自己的 `_rebuild_layer_index`（“Already holding _lock from the caller”）纪律不一致。
  - 建议: 为 NodeGraph 提供一个锁内的 `restore_layer_state(stacks, layers, connections)` 公共 API，或导出锁供控制器在 _restore 期间持有；至少加注释固化 UI 线程约定。
  - 置信度: 中

### 通过项
- 线程安全：控制器无 OpenGL 调用（铁律“UI 线程不得操作 OpenGL”天然满足）；除 _restore 直写外全部经 NodeGraph 公共 API（内部持 RLock）；render 侧经不可变 GraphSnapshot 传递
- GPU 资源生命周期：不持有 ModernGL 对象；节点删除经 graph.remove_node（其文档明确 GPU 清理由渲染线程 deferred-release 处理）
- GLSL/HDR：无着色器代码；opacity/blend 投影值经校验与 Layer.from_dict 边界钳制；fit 缩放 `min(1.0, ...)` 无 HDR clamp 问题（Transform 数学已按 pivot=center、先缩放后平移的矩阵序核对，`translate_x = comp_w/2 - source_w/2` 语义正确）
- 信号防递归：emit_changed 在状态落定、undo 入栈之后发射；已核查唯一订阅方 keyframes_panel._on_layer_stack_changed 为只读重建，无同步回写控制器的路径
- 输入验证（主栈场景）：opacity 有限域/NaN 校验、blend_mode/matte_mode 白名单、parent 环检测（含防预存环的 seen 集合）、insert_index 钳制、in/out frame 交叉校验均到位
- 异常与取消路径（新流程）：create_precomp_layer / add_media_payload / add_model3d_payload 均有 snapshot 回滚 + re-raise；编译 conflict 时不动图并 emit layer_stack_conflict
- 状态提交一致性：表达式依赖随快照捕获并经 set_expression 重建（BUG-1 修复）；_apply_changes 失败面 surface 为 warnings（finding #11 修复）；P0-05 帧门变更后 bump_generation 强制渲染快照刷新；结构操作 is_compressed=False 防压缩
- ID 生成：layer/node id 均 uuid + 冲突探测
