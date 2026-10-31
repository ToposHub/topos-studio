# batch-056 审查报告 [P1] src/features/composite/

- 编号区间：AUD-5600–AUD-5699
- 审查日期：2026-09-28
- 交叉核实依赖：`src/shared/keyframe.py`、`src/features/composite/core/node_engine.py`、`src/features/composite/nodes/base_node.py`、`src/features/composite/core/output_targets.py`、宿主调用点 `src/app/controllers/ai_editing_mixin.py` / `src/features/composite/ui/timeline_commands.py` / `src/features/composite/ui/composite_page.py`

## src/features/composite/edit_service.py (行数: 1020)

### 发现

- [AUD-5601] [Major] set_expression/remove_expression 直接调用 Node 层方法，绕过引擎的环检测、表达式依赖管理器注册、脏传播与 generation 递增
  - 位置: src/features/composite/edit_service.py:758
  - 证据:
    ```python
    if kind == "set_expression":
        if not node.set_expression(pdef.name, operation["expression"]):
            raise CompositeEditError(f"invalid expression for {node.id}.{pdef.name}")
        return
    if kind == "remove_expression":
        node.remove_expression(pdef.name)
        return
    ```
  - 影响: 对照 `node_engine.py:810-866` 的 `set_node_expression`/`remove_node_expression`：正规路径会 `self._expression_manager.set_expression(...)` 注册依赖、`detect_circular_dependency(...)` 拒绝循环表达式、`_propagate_expression_change(...)` 传播脏标记并 `self._generation += 1`。plan 路径全部跳过：同一 plan 可建立互相引用的循环表达式（引擎防线失效）；`_expression_manager` 依赖图与节点 `_expressions` 脱节，后续增量编辑的脏传播基于陈旧依赖——本文件 `_restore_graph_in_place` 的注释（996-1010 行）自己承认该状态会"增量渲染可能喂出旧纹理"。已核实宿主 `ai_editing_mixin.apply_composite_edit`（1874-1897 行）与 `timeline_commands._apply` 均无补偿注册。
  - 建议: apply 阶段改走 `graph.set_node_expression(...)` / `graph.remove_node_expression(...)`（或补齐 manager 注册 + 环检测 + 传播 + generation 递增）。
  - 置信度: 高

- [AUD-5602] [Major] 关键帧/参数/表达式类操作不递增 graph._generation，乐观锁 base_generation 与 graph_revision 缓存 token 失真
  - 位置: src/features/composite/edit_service.py:314
  - 证据:
    ```python
    return CompositeApplyResult(
        True, plan.plan_id, plan.content_hash(), before_generation, self.generation,
        before=before, after=self.graph.to_dict(), report=report,
    )
    ...
    node.set_keyframe_track(pdef.name, track)
    ```
  - 影响: `Node.set_parameter`/`set_keyframe_track`（base_node.py:643-652, 557-618）只递增节点 `param_revision`，不动 `_generation`；仅拓扑 op（add/remove/connect/disconnect）经引擎 API 递增。纯关键帧 plan 成功后 `generation_after == generation_before`：其一，`validate` 的 `base_generation` 乐观锁（234-237 行）无法发现两个基于同一代的 plan 先后落盘的冲突；其二，`NodeGraph.to_dict` 把 `graph_revision=int(self._generation)` 写成 headless Comp Clip 预览的缓存失效 token（node_engine.py:2094-2099），`composite_frame_renderer.py:718/761-773` 以它作 cache key，参数/关键帧编辑后 token 不变，存在旧帧复用的渲染陈旧风险。`generation` 属性文档"incremented on every graph mutation"被破坏。
  - 建议: apply 成功后统一调用 `graph.bump_generation()`（node_engine.py:1389），或让各 mutation op 走会递增 generation 的 graph 层 API。
  - 置信度: 中（乐观锁失真为确定事实；预览缓存陈旧取决于 renderer 的 cache key 组合，标中）

- [AUD-5603] [Major] validate/apply 状态不对称：同一 plan 内 remove_node 之后的操作仍可引用被删节点
  - 位置: src/features/composite/edit_service.py:321
  - 证据:
    ```python
    node_id = str(operation.get("node_id") or "")
    param_name = str(operation.get("param_name") or "")
    node = self.graph.get_node(node_id)
    if node is None and topo_state is not None \
            and node_id in topo_state.get("added", ()):
        # 同一 plan 里刚 add 的节点尚未入图：用临时实例校验参数语义，
        # apply 阶段节点已真实入图，走正常路径。
        node = _peek_node(topo_state.get("added_types", {}).get(node_id))
    ```
  - 影响: `_node_and_param` 只查 `topo_state["added"]`，不查 `removed`（对照 `set_param`/`connect` 走的 `_resolve_node_for_topology`，609-610 行有 removed 检查）。plan `[remove_node X, add_keyframe X.foo]` 在 validate 阶段 X 仍在图内、校验通过；apply 阶段 remove 已执行，754 行 `_node_and_param` 抛 "node not found"，被 304 行宽 except 捕获后整体回滚，具体 op 诊断丢失。同类缺口：`group` 校验（500-504 行，get_node 通过即放行）后 `create_group` 会把已删 node id 永久存进组（node_engine.py:1935-1956 不校验成员存在）；`set_output_target`（482-490 行）不查 removed，apply 后 `_viewer_state.output_target_node_id` 悬空指向已删节点。
  - 建议: `_node_and_param`、`set_output_target`、`group` 的校验查询 `topo_state["removed"]` 并拒绝。
  - 置信度: 高

- [AUD-5604] [Major] add_node 对注册表 None 占位类型（如 motion_graphic）在 validate 阶段抛未捕获 TypeError
  - 位置: src/features/composite/edit_service.py:422
  - 证据:
    ```python
    node_type = str(operation.get("node_type") or "")
    registry = _node_registry()
    if node_type not in registry:
        raise CompositeEditError(
            f"unknown node_type: {node_type!r} (available: {sorted(registry)})")
    ...
                probe = registry[node_type](node_id="_probe")
    ```
  - 影响: `NODE_REGISTRY` 含值为 None 的延迟注册占位（node_engine.py:127-145，`"motion_graphic": None`）。`"motion_graphic" not in registry` 为 False → 校验放行；带 parameters 时 `None(node_id="_probe")` 抛 `TypeError: 'NoneType' object is not callable`，而 `validate()`（240-247 行）只捕获 `CompositeEditError`，原始异常直接逃逸给宿主（`ai_editing_mixin.validate_composite_edit` 无 try 包裹）。不带 parameters 时校验通过、apply 阶段（644-646 行）才抛 CompositeEditError 触发整体回滚。
  - 建议: 改为 `node_class = registry.get(node_type); if node_class is None: raise CompositeEditError(...)`。
  - 置信度: 高

- [AUD-5605] [Major] set_parameter 对 str/file/hdri/channel 类型缺少类型校验：缺 value 键通过校验后 apply 抛 KeyError，任意标量可写入 str 参数
  - 位置: src/features/composite/edit_service.py:395
  - 证据:
    ```python
    elif kind == "set_parameter":
        if not _json_safe_scalar(operation.get("value")):
            raise CompositeEditError("set_parameter.value must be JSON-safe")
        if pdef.ptype in {"float", "int"}:
    ...
        if kind == "set_parameter":
            node.set_parameter(pdef.name, operation["value"])
    ```
  - 影响: 缺 "value" 键时 `.get("value")` 返回 None，`_json_safe_scalar(None)` 为 True，且 None 不落在 float/int/color/vec2/vec3/bool/option 任何校验分支 → 校验通过；apply 756 行 `operation["value"]` 抛 `KeyError('value')`，落入 304 行宽 except 整体回滚而非具体诊断。另外 str/file/hdri/channel 类型仅做 JSON-safe 检查，`set_parameter`/`set_param` 可把 bool/int 写入 str 类参数（`Node.set_parameter` 不校验类型，base_node.py:643-652 原样存储），下游按字符串消费时会在求值期崩溃。
  - 建议: 强制 "value" 必填；对 str/file/hdri/channel 增加 `isinstance(value, str)` 校验（set_param 分支 567-595 行同改）。
  - 置信度: 高

- [AUD-5606] [Major] add_node.position 完全未校验：类型错误在 apply 期才爆，inf/nan 可入库
  - 位置: src/features/composite/edit_service.py:654
  - 证据:
    ```python
    position = operation.get("position")
    if isinstance(position, (list, tuple)) and len(position) == 2:
        node.position = [float(position[0]), float(position[1])]
    ```
  - 影响: `_validate_node_operation` 的 add_node 分支（419-458 行）不检查 position。`position=["a","b"]` 通过校验，apply 期 `float("a")` 抛 ValueError → 整体回滚（validate/apply 不对称）；`position=[inf, inf]` 通过 isinstance 检查被原样存入节点，`Node.to_dict` 序列化 `"position": list(self.position)`（base_node.py:1188）后 `json.dumps` 产出非标准 JSON 的 `Infinity`，破坏工程持久化与严格 JSON 读取方。
  - 建议: validate 增加 position 必须为长度 2 的有限数值序列检查。
  - 置信度: 高（类型不匹配确定；inf 持久化后果标中）

- [AUD-5607] [Major] group/ungroup 的存在性未在 validate 阶段校验，非法 plan 整体回滚而非给出诊断
  - 位置: src/features/composite/edit_service.py:492
  - 证据:
    ```python
    if kind in ("group", "ungroup"):
        group_id = str(operation.get("group_id") or "")
        if not group_id:
            raise CompositeEditError("group_id is required")
        if kind == "group":
            node_ids = operation.get("node_ids")
            if not isinstance(node_ids, (list, tuple)) or not node_ids:
                raise CompositeEditError("node_ids must be a non-empty list")
    ...
            if not self.graph.create_group(group_id, node_ids,
                                           {"title": title}):
                raise CompositeEditError(f"group already exists: {group_id!r}")
    ```
  - 影响: 校验只查 group_id 非空与 node_ids 形态；不查 group 是否已存在（apply 744-746 行才失败）、ungroup 不查 group 是否存在（apply 750-751 行才失败）。这类 plan validate 全绿、apply 整体回滚，错误被归并为通用 "composite plan rolled back"，脚本/AI 拿不到具名诊断。
  - 建议: validate 阶段查询图内组集合（`_node_groups` 或补公开 API）预检存在性/唯一性。
  - 置信度: 高

- [AUD-5608] [Major] CompositeEditPlan.from_dict 对畸形输入抛非领域异常（TypeError/ValueError 逃逸边界）
  - 位置: src/features/composite/edit_service.py:74
  - 证据:
    ```python
    operations = data.get("operations", ())
    if not isinstance(operations, (list, tuple)):
        raise CompositeEditError("composite plan operations must be an array")
    return cls(
        plan_id=str(data.get("plan_id") or "composite-plan"),
        operations=tuple(dict(op) for op in operations),
        composition_id=(str(data["composition_id"]) if data.get("composition_id") is not None else None),
        base_generation=(int(data["base_generation"]) if data.get("base_generation") is not None else None),
    ```
  - 影响: 该类是对脚本/AI 暴露的 JSON-safe 领域边界，但 operations 元素非 Mapping 时 `dict(op)` 抛 `TypeError`/`ValueError`，`base_generation="abc"` 时 `int(...)` 抛 `ValueError`——都不是 `CompositeEditError`。`validate`/`apply`（224/289 行 `_coerce_plan`）先走 from_dict，原始异常穿透到宿主（违反"外部数据须验证、领域错误可展示"铁律）。
  - 建议: 逐元素 `isinstance(op, Mapping)` 检查并抛 CompositeEditError；base_generation 解析包 try/except ValueError。
  - 置信度: 高

- [AUD-5609] [Suggestion] duplicate_node 不克隆 keyframe_tracks/_expressions，与"克隆类型与全部参数"注释矛盾；position_offset=0 被静默改为 40
  - 位置: src/features/composite/edit_service.py:694
  - 证据:
    ```python
    if kind == "duplicate_node":
        # T-P1-60：克隆类型与全部参数（不克隆连接——新节点默认独立，
        # 连线由后续 connect op 显式声明，保持计划可预测）
    ...
                offset = float(operation.get("position_offset", 40.0) or 40.0)
    ...
            for name, value in (getattr(source, "parameters", {}) or {}).items():
                try:
                    node.set_parameter(str(name), value)
    ```
  - 影响: 复制带动画/表达式的节点后，全部关键帧轨道与表达式丢失（动画存于 `node.keyframe_tracks`，不在 `parameters`）；注释声明"全部参数"与实现不符。`or 40.0` 把显式 `position_offset: 0`（falsy）变成 40.0，零偏移不可表达；position_offset 的类型也未校验（`float("x")` → 回滚）。
  - 建议: 深拷贝 `source.keyframe_tracks` 与 `_expressions` 到新节点；offset 改 `float(operation.get("position_offset", 40.0))` 去掉 `or`，并在 validate 阶段校验数值。
  - 置信度: 中（是否克隆动画属产品语义；注释与实现矛盾、0 偏移失效为事实）

- [AUD-5610] [Suggestion] int 参数取值校验与归一化在三条路径上不一致：set_parameter 拒绝非整数，add_keyframe 静默取整，set_keyframes 原样存浮点
  - 位置: src/features/composite/edit_service.py:398
  - 证据:
    ```python
    if pdef.ptype in {"float", "int"}:
        _validate_value(operation.get("value"), pdef.min_val, pdef.max_val,
                        require_int=(pdef.ptype == "int"))
    ...
        _validate_value(value, pdef.min_val, pdef.max_val)
    ...
            track.keyframes = []
            for data in operation["keyframes"]:
                track.add_keyframe(Keyframe.from_dict(dict(data)))
    ```
  - 影响: `_validate_typed_value` 末行（915 行）不传 `require_int`，关键帧路径接受 int 参数的 3.7；apply 时 `add_keyframe` 经 `_normalize_typed_value` 取整（924 行 `int(round(float(value)))`）而 `set_keyframes` 直接 `Keyframe.from_dict` 原样入轨——同一 int 参数经不同 op 产生不同轨道数据，求值期向 int 语义参数喂浮点。
  - 建议: `_validate_typed_value` 对 int 传 `require_int=True`；`set_keyframes` apply 复用 `_normalize_typed_value`。
  - 置信度: 高（不一致确定；下游后果标中）

- [AUD-5611] [Suggestion] 回滚路径自身无异常保护，且恢复流程多处 `except Exception: pass` 静默吞错
  - 位置: src/features/composite/edit_service.py:304
  - 证据:
    ```python
    except Exception as exc:
        # 所有操作在 validate 阶段已经通过；这里仅防止宿主/节点异常造成半套计划。
        _restore_graph_in_place(self.graph, before)
        return CompositeApplyResult(
            False, plan.plan_id, plan.content_hash(), before_generation, self.generation,
            report=report, error=f"composite plan rolled back: {exc}",
        )
    ...
        live_manager = getattr(graph, "_expression_manager", None)
        if live_manager is not None:
            try:
                live_manager.clear()
            except Exception:
                pass
    ```
  - 影响: `_restore_graph_in_place`（978-1014 行）内若 `NodeGraph.from_dict` 或 1011-1014 行的直接属性操作抛异常，会替换原始错误并从 `apply` 二次抛出，图处于半恢复状态；`set_node_graph`/`clear`/`set_expression` 三段恢复失败均被静默吞掉（992、1001-1002、1009-1010 行），表达式重注册失败无任何诊断，直接落入 AUD-5601 所述的陈旧依赖图风险。
  - 建议: 回滚调用包一层 try，把恢复失败信息并入返回的 error；静默分支至少记录日志。
  - 置信度: 中

- [AUD-5612] [Suggestion] 同一 plan 内多条连线打同一单输入端口不产生告警，引擎静默替换使计划与实际图结构不符
  - 位置: src/features/composite/edit_service.py:530
  - 证据:
    ```python
    if kind == "connect":
        if from_node == to_node:
            raise CompositeEditError("self-connection is not allowed")
    ...
                topo_state["connected"].add(
                    (from_node, from_port, to_node, to_port))
    ```
  - 影响: `NodeGraph.connect`（node_engine.py:1194-1206）对单输入端口满载时替换旧连接。plan `[connect A→B.in, connect C→B.in]` 两条都过校验，apply 后第一条连线被静默丢弃，脚本/AI 对最终图结构的预期落空且无 warning 诊断。
  - 建议: validate 基于 `topo_state["connected"]` + 图内连接检测目标端口占用冲突，输出 warning。
  - 置信度: 中

- [AUD-5613] [Suggestion] _validate_color_value 硬编码 [0,1]，忽略 ParamDef 的 min_val/max_val
  - 位置: src/features/composite/edit_service.py:872
  - 证据:
    ```python
    def _validate_color_value(value: Any) -> None:
        if (not isinstance(value, (list, tuple)) or len(value) != 4
                or any(isinstance(component, bool)
                       or not isinstance(component, (int, float))
                       or not math.isfinite(float(component))
                       or not 0.0 <= float(component) <= 1.0
                       for component in value)):
    ```
  - 影响: 与 vec2/vec3 分支（889-901 行，读 pdef 范围）不一致。已核实当前全部 color ParamDef（constant/fog/lens_flare/godrays/relight/pointcloud3d/volume3d）均用默认 [0,1]，暂无实际冲突；但未来任何声明更宽色域（min<0 或 max>1）的 HDR 颜色参数会被此函数错误拒绝。
  - 建议: color 分支同样读取 pdef.min_val/max_val。
  - 置信度: 中

- [AUD-5614] [Suggestion] 领域对象全程 Any 类型 + 直接读写引擎私有属性
  - 位置: src/features/composite/edit_service.py:166
  - 证据:
    ```python
    def __init__(self, graph: Any):
        self.graph = graph
    ...
                        if self.graph._would_create_cycle(from_node, to_node):
    ...
            self.graph._viewer_state = viewer_state
    ```
  - 影响: graph/node/pdef 均为 Any（166、321、599-601、620 行），类型检查无法捕获引擎接口漂移；直接调用私有 `_would_create_cycle`（538 行）、直接赋值 `_viewer_state`（738 行，绕过 `_lock`）、`_restore_graph_in_place` 直接操作 `_generation/_topology_revision/_force_full_eval/_evaluation_cache`（1011-1014 行），与引擎公开 API 契约脱钩。
  - 建议: 为 Graph/Node/ParamDef 定义 typing.Protocol；状态写入优先走引擎公开方法。
  - 置信度: 高（事实确定，属质量改进）

- [AUD-5615] [Suggestion] 校验期临时节点实例无缓存，大 plan 上重复构造
  - 位置: src/features/composite/edit_service.py:819
  - 证据:
    ```python
    def _peek_node(node_type: str | None) -> Any:
        """临时实例化一个注册节点类用于端口语义校验（不入图、无副作用）。"""
        if not node_type:
            return None
        try:
            return _node_registry()[node_type](node_id="_peek")
        except Exception:
            return None
    ```
  - 影响: 引用同一 plan 内新节点的每个 keyframe/connect 操作（330、513-515、571 行）都会重新实例化一个临时节点；大计划（同一新节点上百个关键帧操作）校验开销线性放大。同类小项：`snapshot` 对每个节点调用两次 `get_param_defs()`（178、191 行）。
  - 建议: 按 node_type 缓存 peek 实例（plan 级生命周期），snapshot 内缓存 param_defs 字典。
  - 置信度: 高（事实确定，量级影响小）

### 通过项

- 线程安全：服务自身除 graph 引用外无共享可变状态；按模块文档约定由宿主在主线程持锁调用（snapshot/validate 直接读图集合未走 `_lock`，依赖该契约，未计入缺陷）
- GPU 资源生命周期：本文件不创建/持有 GPU 对象；回滚清空 `_evaluation_cache` 与 `NodeGraph.clear()` 行为一致
- GLSL·HDR 正确性：无着色器代码；color 值域 [0,1] 与现有全部 color ParamDef 声明域一致，非无理由 clamp；插值/外推语义交由 keyframe 层（已核实 add_keyframe 对重复帧为原位替换）
- 信号防递归：纯领域层，无 Qt 控件与信号连接
- 异常与取消路径：apply 整体 try + 回滚保留 graph identity；失败结果携带 error 与完整 report；回滚后正确重绑 GroupNode 子图引用并重注册表达式（缺陷仅见 AUD-5611 的保护缺失）
- 输入验证（frame/handle/tangent/interpolation/extrap/color/vec/bool/option、set_keyframes 重复帧、node_id 合法性、连接端口/类型匹配、自环）：覆盖完整（例外已在 AUD-5604/5605/5606/5607 列出）
- 状态提交一致性：before/after 以完整 `to_dict()` 交宿主形成单一 undo 组（composite_page.record_external_composite_edit），undo/redo 复用 `_restore_graph_in_place`；plan hash 可重放（例外见 AUD-5601/5602）
- 性能模式：逐操作 O(1) 校验 + plan 级集合模拟；`_safe_copy`/`_json_safe_scalar` 正确拒绝非有限浮点与非 JSON 类型（小项见 AUD-5615）
