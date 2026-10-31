# batch-232 审查报告 — src/features/ai_workflow/core/（8 文件，2,793 行）

编号区间：AUD-23200–AUD-23299。
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。
跨文件事实均已用只读 grep 验证（`params_schema`、`allow_multi`、`target_duration`、`node_<id>_params` 消费方、节点删除 API）。

汇总：Critical 0 · Major 8 · Suggestion 13。

---

## src/features/ai_workflow/core/__init__.py (行数: 0)

### 通过项
- 通过（无逻辑）：空文件（`wc -l` = 0），无任何导出、无代码。

---

## src/features/ai_workflow/core/node_specs.py (行数: 495)

### 发现

- [AUD-23200] [Major] NodeSpecRegistry.register 无版本比较，旧版本 spec 可静默覆盖新版本
  - 位置: src/features/ai_workflow/core/node_specs.py:235-241
  - 证据:
    ```python
    def register(self, spec: WorkflowNodeSpec) -> None:
        if spec.spec_id in self._specs:
            existing = self._specs[spec.spec_id]
            if existing.version == spec.version:
                return  # 幂等
            # 版本升级：保留较新（简化：直接覆盖）
        self._specs[spec.spec_id] = spec
    ```
  - 影响: 注释宣称"保留较新"，但 `existing` 只用于同版本幂等判断，从未与 `spec.version` 比较；注册顺序决定最终版本——后注册的 v1.0.0 可覆盖已注册的 v2.0.0（版本降级）。validator/compiler 按 registry 中 spec 校验端口与能力，降级后端口契约可能与已保存 graph 不符，产生难以定位的校验漂移。
  - 建议: 用 `SkillDraftVersion`（或元组比较 major/minor/patch）拒绝 downgrade，或至少在覆盖时记录警告。
  - 置信度: 高

- [AUD-23201] [Major] register_official_specs 宽泛 except Exception 吞掉适配器全部错误且无日志（与文档"只记日志"不符）
  - 位置: src/features/ai_workflow/core/node_specs.py:471-485（docstring 466-467）
  - 证据:
    ```python
    try:
        from src.features.ai_workflow.adapters.composite_adapter import (
            register_composite_node_specs,
        )
        register_composite_node_specs(registry)
    except Exception:  # noqa: BLE001 — adapters 可选依赖；缺失不阻断核心 spec
        pass
    ```
    （comfyui 分支 479-485 同样 `except Exception: pass`；docstring 写明"注册失败只记日志不阻断"）
  - 影响: 捕获范围远超 ImportError——`register_composite_node_specs` 内部的 TypeError/AttributeError 等真实缺陷被静默吞掉，Composite/ComfyUI spec 无声消失（画布 Tab 搜不到），且文档承诺的日志一行都没有，违反 .clinerules"不得宽泛 except 掩盖领域错误"。
  - 建议: 仅捕 `ImportError`（或 `ModuleNotFoundError`），其余异常照常抛出；确实吞掉的分支用 `logging.getLogger(__name__).warning(...)` 记录原因。
  - 置信度: 高

- [AUD-23202] [Suggestion] failure_strategy 字段完全无校验（retry_policy 有枚举校验）
  - 位置: src/features/ai_workflow/core/node_specs.py:169-185
  - 证据:
    ```python
    retry_policy: str = "fail"  # fail / skip / fallback / ask_user（plan §3.3）
    failure_strategy: str = "fail"

    def __post_init__(self) -> None:
        ...
        if self.retry_policy not in ("fail", "skip", "fallback", "ask_user"):
            raise ValueError(f"invalid retry_policy: {self.retry_policy!r}")
    ```
  - 影响: 第三方 spec 可把 failure_strategy 写成任意字符串（如 "ignore" / 拼错），frozen 校验层放行，运行期失败策略回退到未定义行为。
  - 建议: 在 `__post_init__` 中对 failure_strategy 增加与 retry_policy 一致的受控枚举校验。
  - 置信度: 高

- [AUD-23203] [Suggestion] inputs/outputs 容器内端口 direction 与容器语义不一致时不拒绝
  - 位置: src/features/ai_workflow/core/node_specs.py:151-153, 187-193
  - 证据:
    ```python
    inputs: Tuple[NodePortSpec, ...] = ()
    outputs: Tuple[NodePortSpec, ...] = ()

    @property
    def input_ports(self) -> Tuple[NodePortSpec, ...]:
        return tuple(p for p in self.inputs if p.direction == "input")
    ```
  - 影响: 把 `direction="output"` 的端口误放进 `inputs`（或反之）不报错——`input_ports`/`output_ports` 与 validator 的逐端口 direction 过滤会静默忽略它，spec 作者拿不到任何提示，端口形同虚设。
  - 建议: `__post_init__` 校验 `all(p.direction == "input" for p in self.inputs)` 及 outputs 对应方向。
  - 置信度: 高

### 通过项
- 线程安全：registry 为启动期单线程装配的 in-memory 字典，无跨线程发布路径（本批次范围内无并发写场景）。
- GPU 资源生命周期 / GLSL·HDR：纯声明式数据，不涉及 OpenGL 与浮点渲染语义。
- 信号防递归：无 Qt 信号。
- 输入验证（部分通过）：spec_id 格式、category、executor_id、retry_policy、端口 direction/name/artifact_type 非空均有 `__post_init__` 校验；executor_id 受控枚举无任意代码执行后端（红线达成）。
- 状态提交一致性：frozen dataclass + `to_dict` 序列化完整；16 个 M4 spec 数量与 plan §4.1 相符。

---

## src/features/ai_workflow/core/publish_as_skill.py (行数: 235)

### 发现

- [AUD-23204] [Suggestion] _bump_to_version 存在死变量与失实注释，且不校验目标版本不低于 draft 当前版本
  - 位置: src/features/ai_workflow/core/publish_as_skill.py:218-229
  - 证据:
    ```python
    def _bump_to_version(self, draft: SkillDraft, target_version: str) -> SkillDraft:
        """把 draft version bump 到目标发布版本。"""
        target = SkillDraftVersion.from_string(target_version)
        # 用 major bump 推进；若还不够再 minor/patch
        current = draft.version
        # 直接构造目标版本的 draft（frozen → replace）
        from dataclasses import replace
    ```
  - 影响: `current` 赋值后从未使用；"用 major bump 推进"的注释与"直接构造目标版本"的实现不符；目标版本低于 draft 当前版本时无任何检查（本 helper 内可降级），若 P4 门禁不查单调性则可能以低版本发布覆盖语义更新。
  - 建议: 删除死变量与失实注释；比较 `target` 与 `draft.version`，低版本抛 ValueError。
  - 置信度: 高

- [AUD-23205] [Suggestion] 类型注解弱：_infer_evidence_kinds 返回裸 list、register_into 形参为 Any
  - 位置: src/features/ai_workflow/core/publish_as_skill.py:169, 196, 205
  - 证据:
    ```python
    def register_into(self, registry: Any, release: SkillRelease) -> List[Any]:
        """注册发布产物进 SkillRegistry（聊天可见）。返回诊断列表。"""
        return self._gate.register_into(registry, release)
    ...
    def _infer_evidence_kinds(self, graph: WorkflowGraph) -> list:
    ...
        kinds: list = []
    ```
  - 影响: `registry: Any` 放弃了 SkillRegistry 类型约束（传错对象运行期才报错）；`-> list` / `kinds: list` 无元素类型，IDE/静态检查无法验证 SegmentKind 流转。
  - 建议: 注解为 `list[SegmentKind]`（函数已局部 import SegmentKind）与具体 registry 协议类型。
  - 置信度: 高

### 通过项
- 线程安全：WorkflowPublisher 无共享可变状态（registry/gate 均只读引用）。
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及。
- 异常与取消路径：门禁失败通过 PublishFromWorkflowResult 显式返回 deny_code/deny_message，不抛裸异常。
- 状态提交一致性：发布走 P4 同一 SkillReleaseGate（不绕过 eval/executor 检查），draft 以 `dataclasses.replace` 派生新实例，原对象不变。
- 输入验证（部分通过）：model_requirements 推断去重（seen 集合）；发布产物 skill_id 保持 draft. 前缀约定。

---

## src/features/ai_workflow/core/template_registry.py (行数: 603)

### 发现

- [AUD-23206] [Major] 产品粗剪模板 audio_feat 节点输出无任何消费边，音频特征分析白跑
  - 位置: src/features/ai_workflow/core/template_registry.py:459-462, 481-516（注释 494-495）
  - 证据:
    ```python
    WorkflowNode(
        node_id="audio_feat", spec_id="topos.analysis.audio_features",
        ui={"x": 340, "y": 320},
    ),
    ...
    # 两路分析汇入 planner（analysis 端口；planner 只接一个 analysis，
    # 取 scene_cut 为主分析；audio_feat 作为旁路证据——本模板用 scene_cut）
    WorkflowEdge(
        from_port=PortRef(node_id="scene_cut", port="analysis"),
        to_port=PortRef(node_id="planner", port="analysis"),
    ),
    ```
  - 影响: 全部 9 条边中没有一条 from `audio_feat`——该节点会被执行（消耗 `audio_features` 能力与算力）但输出无人消费；且它 `required_capabilities=("audio_features",)`，host 缺该能力时这个"死"节点反而以 MISSING_CAPABILITY error 阻塞整个模板编译。模板 docstring 宣称"Scene Cut + Audio Features（双分析并行）→ Rough Cut Plan 据分析产 EditPlan"，与实际接线不符；注释"两路分析汇入 planner"自相矛盾。
  - 建议: 要么把 audio_feat.analysis 连到 planner（planner.analysis 需改 allow_multi 或加聚合端口），要么删掉该节点并在描述中更正。
  - 置信度: 高

- [AUD-23207] [Major] register_official_templates 宽泛 except Exception: pass，吞掉 Composite 模板构造的真实错误且无日志
  - 位置: src/features/ai_workflow/core/template_registry.py:321-324
  - 证据:
    ```python
    try:
        registry.register(_composite_render_import_template())
    except Exception:  # noqa: BLE001 — Composite spec 可选依赖；缺失跳过该模板
        pass
    ```
  - 影响: `_composite_render_import_template()` 抛出的任何非 ImportError 错误（如 WorkflowGraph/PortRef 校验 ValueError——模板自身接线错误）都被静默吞掉，"合成渲染后导入"模板无声消失且无任何诊断，违反宽泛 except 铁律；模板是纯 dataclass 构造，真正需要豁免的只有 adapters spec 缺失场景。
  - 建议: 收窄为 `except ImportError` 并 `logging.warning` 记录；模板自身构造错误应让它炸出来。
  - 置信度: 高

- [AUD-23208] [Suggestion] instantiate 不校验 param_overrides 的 node_id 与参数键，typo 被静默丢弃
  - 位置: src/features/ai_workflow/core/template_registry.py:137-151
  - 证据:
    ```python
    merged_overrides: Dict[str, Dict[str, Any]] = dict(t.default_param_overrides)
    if param_overrides:
        for nid, params in param_overrides.items():
            base = dict(merged_overrides.get(nid, {}))
            base.update(params)
            merged_overrides[nid] = base
    # 构造新节点（应用参数覆盖）
    new_nodes = tuple(
        replace(
            n,
            params={**n.params, **merged_overrides.get(n.node_id, {})},
        )
    ```
  - 影响: `param_overrides={"asr ": {...}}`（node_id 拼错）或参数名拼错时不报错、不生效——用户以为改了参数，副本仍按模板默认值运行（外部输入未验证）。
  - 建议: 对 overrides 中不存在的 node_id / 不在 spec params_schema 的键给出 ValueError 或至少 warning。
  - 置信度: 高

- [AUD-23209] [Suggestion] instantiate 副本与冻结官方模板共享可变 dict 引用，"官方模板 frozen"红线仅靠约定维持
  - 位置: src/features/ai_workflow/core/template_registry.py:145-160
  - 证据:
    ```python
    new_nodes = tuple(
        replace(
            n,
            params={**n.params, **merged_overrides.get(n.node_id, {})},
        )
        for n in t.graph.nodes
    )
    return WorkflowGraph(
        workflow_id=wf_id,
        name=t.display_name,
        nodes=new_nodes,
        edges=t.graph.edges,
        ui=t.graph.ui,
        policy=t.graph.policy,
    ```
  - 影响: `replace(n, params=...)` 后新节点仍与模板节点共享同一 `ui` dict（及 params 内层嵌套结构）；`edges`/`ui`/`policy` 对象亦直接共享。任何持有副本引用的代码若直接改 dict（绕过 WorkflowGraphEdit），会同时污染官方模板，破坏"frozen"红线。当前调用链（GraphEdit 全 replace）安全，属脆弱设计。
  - 建议: instantiate 时对 `ui`/`params` 做深拷贝（`copy.deepcopy` 或递归 dict()），或提供只读视图。
  - 置信度: 高

### 通过项
- 线程安全：注册表启动期装配，无并发写。
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及（纯数据模板）。
- 状态提交一致性：instantiate 生成新 workflow_id（uuid 后缀），不篡改模板条目；含 apply_approved_plan 的两个模板（talking-head / 官方 apply spec 使用处）均接 `approval.token → apply.approval` 边，符合审批红线。
- 输入验证（部分通过）：模板节点 spec_id 均存在于官方 M4 spec（除 composite 可选三个）；translate 节点满足 params_schema required `target_language`。
- 异常与取消路径：require() 未知模板抛 KeyError 带上下文。

---

## src/features/ai_workflow/core/workflow_compiler.py (行数: 407)

### 发现

- [AUD-23210] [Major] invocation.inputs 的 target_duration 硬编码 720，模板节点声明的 target_duration（60/90）不流入运行时
  - 位置: src/features/ai_workflow/core/workflow_compiler.py:294
  - 证据:
    ```python
    inputs: Dict[str, Any] = {
        # workflow 输入契约（invocation_to_workflow_input 读取这些）
        "asset_ids": list(selected_asset_ids),
        "brief_id": brief_id,
        "base_revision": 0 if base_revision is None else int(base_revision),
        "analysis_snapshot_hash": analysis_snapshot_hash,
        "target_duration": 720,
    ```
    （交叉验证：运行时消费方 src/features/ai_harness/legacy_adapters.py:70/88 `target_duration=int(inv.inputs.get("target_duration", 720))`；而模板 src/features/ai_workflow/core/template_registry.py:359 `params={"target_duration": 60}`、:465 `params={"target_duration": 90}` 只进 `node_planner_params` 键，全库 grep 无任何运行时消费 `node_<id>_params` 的代码。）
  - 影响: "口播精剪"（60）与"产品粗剪"（90）模板在 planner 节点上声明的目标时长是死配置——editorial/workflow 后端（SkillInput/WorkflowInput）恒取 720；用户在模板画廊看到的时长意图与实际规划时长不符，产出粗剪长度错误。acceptance 测试（test_p5_acceptance.py:76,116）把 720 钉死，说明是未接线的已知简化而非笔误。
  - 建议: `_build_inputs` 接受可选 target_duration 参数，host 从含该参数的节点（或模板元数据）解析后传入；模板侧确保声明值与编译值一致。
  - 置信度: 高

- [AUD-23211] [Suggestion] compile 持有 base_revision 却不接入 validator 的 stale revision 检查，STALE_REVISION 在编译路径永不触发
  - 位置: src/features/ai_workflow/core/workflow_compiler.py:153-175（validator 签名见 workflow_validator.py:129-136, 172-180）
  - 证据:
    ```python
        base_revision: int | None = None,
    ...
        # 1. 校验
        findings = self._validator.validate(graph, available_capabilities=caps)
    ```
  - 影响: validator 支持 `current_revision`/`expected_revision` 双参 stale 检查，但唯一的编译集成点只传了 capabilities——plan §12.1 列出的"stale revision"校验在 Run 按钮路径上形同虚设（除非 host 另行单独调用 validator，未发现此类调用）。
  - 建议: compile 增加 `current_revision` 参数并透传 `expected_revision=base_revision`，或文档化由 host 在 invoke 前自查。
  - 置信度: 中

### 通过项
- 线程安全：compile 为纯函数式构造（无实例可变状态），可重入。
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及（编译期纯 Python，不执行任务）。
- 状态提交一致性：plan_hash 剔除 invocation_id/created_at，同一输入重复编译 hash 稳定（注释与实现一致）；user_text=""、source=workflow 审计分流红线达成；timeline 写经 RunPolicy + approval，编译产物 preview_only_until_approval。
- 异常与取消路径：validator error 时返回 success=False + diagnostics（不抛），SkillCompiler 异常自然上抛由调用方处理。
- 性能模式：_stable_hash 单次 json.dumps，图规模小，无热点。
- 输入验证（部分通过）：validator 先行（unknown spec/cycle/port/approval token）；sanitize_for_storage 应用于对外 dict。

---

## src/features/ai_workflow/core/workflow_graph_edit.py (行数: 188)

### 发现

- [AUD-23212] [Suggestion] _is_json_safe 接受 NaN/Inf 浮点（严格 JSON 非法），move_node 同样不校验有限性
  - 位置: src/features/ai_workflow/core/workflow_graph_edit.py:35-46, 72-82
  - 证据:
    ```python
    def _is_json_safe(value: Any) -> bool:
        """递归判断 value 是否 JSON-safe（对齐 ``JsonValue`` 联合类型）。"""
        if value is None or isinstance(value, (bool, int, float, str)):
            return True
    ...
    def move_node(self, graph: WorkflowGraph, node_id: str,
                  x: float, y: float) -> WorkflowGraph:
        """移动单个节点（改 ``ui.x`` / ``ui.y``）。"""
        node = graph.require_node(node_id)  # KeyError if missing
        new_ui: Dict[str, Any] = dict(node.ui)
        new_ui["x"] = float(x)
    ```
  - 影响: `float("nan")` 通过 JSON-safe 检查并进入 params/ui；`json.dumps` 默认产出 `NaN`/`Infinity` token，属非法 JSON——持久化文件在严格解析器（JS 侧 / 第三方导入校验）下失败；NaN x/y 使节点布局失效。Python 侧 round-trip 不报错，问题延迟暴露。
  - 建议: `_is_json_safe` 对 float 加 `math.isfinite` 判断；move_node/move_nodes 对 x/y 同样拒绝非有限值。
  - 置信度: 高

- [AUD-23213] [Suggestion] 变更层缺 remove_node/remove_nodes API，画布删除节点无模型层支撑
  - 位置: src/features/ai_workflow/core/workflow_graph_edit.py:49-66（仅有 add_node）
  - 证据:
    ```python
    class WorkflowGraphEdit:
        """冻结 WorkflowGraph 的纯函数式变更层。
        ...
        def add_node(self, graph: WorkflowGraph, node: WorkflowNode) -> WorkflowGraph:
            """加一个节点。重复 node_id 抛 ValueError（靠 WorkflowGraph.__post_init__）。"""
            existing = {n.node_id for n in graph.nodes}
            if node.node_id in existing:
                raise ValueError(f"duplicate node_id: {node.node_id!r}")
            new_nodes = graph.nodes + (node,)
            return replace(graph, nodes=new_nodes)
    ```
    （交叉验证：UI 协议层已有 `workflow_canvas_adapter.py:105 remove_node_visual`，但全 ai_workflow 无任何模型级 remove_node 实现，workflow_panel.py 中也无删除处理。）
  - 影响: 增/移/参数/连线四类画布编辑中"删节点"缺失——宿主要么无法删除节点（UI 只删 visual 不删模型，重载即复活），要么绕过本层手写 `replace(graph, nodes=...)` 并自行清理挂接边，易漏删边导致 ghost edge（由 __post_init__ 拒绝后更易踩坑）。
  - 建议: 增加 `remove_node`（连同其挂接 edges 一并过滤，若 spec 声明则校验下游 required 断连）。
  - 置信度: 高（API 缺失为事实；宿主实际行为影响为中）

### 通过项
- 线程安全：无状态服务，方法纯函数式返回新 graph。
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及（无 Qt/ModernGL import，符合本层红线）。
- 状态提交一致性：所有变更经 `dataclasses.replace` + `__post_init__` 复验（重复 node_id/自连/ghost node 拒绝）；原 graph 不被修改。
- 异常与取消路径：错误以 KeyError/ValueError 明确抛出，无吞异常。
- 输入验证（部分通过）：add_edge 显式重复边校验；set_node_param JSON-safe 检查（NaN 缺口见 AUD-23212）。

---

## src/features/ai_workflow/core/workflow_models.py (行数: 447)

### 发现

- [AUD-23214] [Suggestion] WorkflowPolicy._NETWORK_VALUES/_TIMELINE_VALUES 为死字段且是实例字段（非 ClassVar），校验反而用内联字面量
  - 位置: src/features/ai_workflow/core/workflow_models.py:188-199
  - 证据:
    ```python
    _NETWORK_VALUES: Tuple[str, ...] = field(
        default=("ask", "allow", "forbidden"), repr=False, compare=False,
    )
    _TIMELINE_VALUES: Tuple[str, ...] = field(
        default=("approval_required", "forbidden"), repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        if self.network not in ("ask", "allow", "forbidden"):
            raise ValueError(f"invalid network policy: {self.network!r}")
    ```
  - 影响: 两个"常量"被声明成 dataclass 实例字段（会出现在 `fields()`/`asdict` 中、可被构造参数覆盖），却从未被校验引用——校验重复内联同一组字面量，两处未来不同步时校验形同虚设。
  - 建议: 改为 `ClassVar[Tuple[str, ...]]`（对齐 WorkflowGraph.SCHEMA 的写法）并在 `__post_init__` 引用。
  - 置信度: 高

- [AUD-23215] [Suggestion] from_dict 对畸形 notes/groups/exposed_inputs 条目静默丢弃（外部数据未显式验证）
  - 位置: src/features/ai_workflow/core/workflow_models.py:244-260, 380-384
  - 证据:
    ```python
        notes = tuple(
            (str(n[0]), str(n[1])) for n in notes_raw
            if isinstance(n, (list, tuple)) and len(n) >= 2
        )
    ...
        exposed = tuple(
            (str(x.get("name", "")), str(x.get("node_id", "")), str(x.get("port", "")))
            for x in exposed_raw if isinstance(x, dict)
        )
    ```
  - 影响: 第三方导入/历史持久化的 workflow 中畸形条目被无声丢掉——用户导入后画布注释/分组/暴露输入悄悄少了一块，无任何诊断，违背"外部数据须验证"（对比：schema 不符会抛 ValueError，同一入口宽严不一）。
  - 建议: 对被跳过的条目收集并在结果上附带 warning（或抛带条目索引的 ValueError）。
  - 置信度: 高

- [AUD-23216] [Suggestion] exposed_inputs 只校验 node 存在，不校验 port 存在、方向为输入、exposed name 重复
  - 位置: src/features/ai_workflow/core/workflow_models.py:314-319
  - 证据:
    ```python
        # exposed_inputs 引用的 node 必须存在
        for exposed_name, node_id, port in self.exposed_inputs:
            if node_id not in existing:
                raise ValueError(
                    f"exposed_input {exposed_name!r} references unknown node: {node_id!r}"
                )
    ```
  - 影响: 拼错 port 名（如 "asset" 而非 "assets"）或暴露输出端口、同名暴露两次均可通过构造；validator 侧 connected_inputs 把该 typo 端口记为已连接，错误静默转移到运行时（host 注入实际值时才发现端口不存在）。
  - 建议: `__post_init__` 中对照 spec 无从可查（models 层不知 spec），至少校验 name 非空且唯一；port 存在性下沉到 WorkflowValidator（其持有 registry）。
  - 置信度: 高

### 通过项
- 线程安全：全 frozen 数据类，无共享可变状态（dict 字段浅冻结的通用限制见 AUD-23209 所述共享路径，模型层自身无 mutation API）。
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及（纯数据，无 Qt/OpenGL/模型实例，符合 plan §3.1 端口红线）。
- 状态提交一致性：content_hash 基于 sort_keys JSON 稳定可复现且不含 hash 本身；to_dict/from_dict 对 schema 字段严格（不符抛 ValueError）。
- 异常与取消路径：所有非法构造以带上下文 ValueError 拒绝（node_id 重复含重复清单、ghost edge 指明方向）。
- 性能模式：查询方法线性扫描，图规模（几十节点）无问题。
- 类型注解：完整（JsonValue/ClassVar/Optional 均有）。

---

## src/features/ai_workflow/core/workflow_validator.py (行数: 418)

### 发现

- [AUD-23217] [Major] edge 引用不存在的端口名被静默跳过，注释"端口不存在已在 spec 层处理"不实——全链路无端口存在性检查
  - 位置: src/features/ai_workflow/core/workflow_validator.py:266-275（connected_inputs 242-246）
  - 证据:
    ```python
        for e in graph.edges:
            ...
            from_type = self._output_port_type(from_spec, e.from_port.port)
            to_type = self._input_port_type(to_spec, e.to_port.port)
            if from_type is None or to_type is None:
                continue  # 端口不存在已在 spec 层处理
    ```
    ```python
        for e in graph.edges:
            connected_inputs.setdefault(e.to_port.node_id, set()).add(e.to_port.port)
    ```
  - 影响: node_specs（spec 定义）与 workflow_models（graph 构造）均只校验节点存在性，不校验端口名——注释所称的"spec 层处理"不存在。输出端口 typo（如 `transcript`→`transcripts`）被 continue 完全放过；输入端口 typo 因 connected_inputs 无条件收录该端口名，连 MISSING_REQUIRED_INPUT 都不会触发。坏边直达运行时 executor 才失败，validator 的核心价值（编译前拒绝）落空。
  - 建议: from/to 任一端口在 spec 中查不到时产出 `UNKNOWN_PORT` error finding（而非 continue）。
  - 置信度: 高

- [AUD-23218] [Major] 不校验 allow_multi：多条边可连入声明为单值的输入端口
  - 位置: src/features/ai_workflow/core/workflow_validator.py:248-264（spec 声明见 node_specs.py:77-79）
  - 证据:
    ```python
            # 必填输入必须有连接
            for p in spec.inputs:
                if p.direction == "input" and p.required:
                    if p.name not in connected_inputs.get(n.node_id, set()):
    ```
    ```python
        required: bool = True
        allow_multi: bool = False
    ```
  - 影响: 全库 grep 确认 `allow_multi` 除定义与测试外无任何执行点——`allow_multi=False`（默认）的输入端口（如 planner.analysis）可被两条以上边连入且校验通过，运行时该端口取哪条边的值未定义（顺序巧合决定）；这是模板作者被迫"只接一个 analysis"（template_registry.py:494-495 注释）的直接根源。
  - 建议: 在 `_check_port_compatibility` 中统计每个 (node_id, port) 的入边数，超过 1 且 `allow_multi=False` 时产出 error。
  - 置信度: 高

- [AUD-23219] [Major] 节点 params 全链路无 params_schema 校验（声明即死数据；官方模板自身也设 schema 外参数）
  - 位置: src/features/ai_workflow/core/workflow_validator.py:129-182（validate 全部检查项，无 params 相关）；node_specs.py:320-323（schema 声明）、workflow_compiler.py:308-310（原样透传）
  - 证据:
    ```python
        # 节点参数（host / executor 可读取）
        for n in graph.nodes:
            if n.params:
                inputs[f"node_{n.node_id}_params"] = dict(n.params)
    ```
    ```python
            params_schema={"type": "object", "properties": {
                "language": {"type": "string"},
                "enable_diarization": {"type": "boolean"},
            }},
    ```
  - 影响: 全库 grep 确认 params_schema 仅被 UI（workflow_inspector/workflow_form_page）用于渲染控件，validator/compiler 均不校验——类型错（`language: 3`）、必填缺失（translate 的 target_language）、未知键全部通过校验直达 executor。且官方模板自身就设置了无 schema 依据的参数（selected_assets 的 `media_kind`、planner 的 `target_duration`、caption 的 `track_name`——对应 spec 未声明任何 params_schema），说明契约与实现已脱节。
  - 建议: validator 增加 params 对 schema 的检查（required 缺失 + 类型不符为 error，未知键为 warning），同时补齐官方 spec 的 params_schema 声明。
  - 置信度: 高

- [AUD-23220] [Suggestion] approval token 检查硬编码官方 spec_id，未按 timeline_write_required 属性覆盖自定义节点
  - 位置: src/features/ai_workflow/core/workflow_validator.py:344-347
  - 证据:
    ```python
        """红线：apply_approved_plan 节点必须有 approval_token 输入连接。"""
        for n in graph.nodes:
            if n.spec_id != "topos.timeline.apply_approved_plan":
                continue
    ```
  - 影响: 第三方 spec 声明 `timeline_write_required=True` 时不经该 token 检查（仅剩 RunPolicy 运行期兜底），"timeline 写需 approval_token"的红线在静态校验层对非官方节点失效。
  - 建议: 改为按 `spec.timeline_write_required` 属性筛选（保留对官方 apply spec 的等价覆盖）。
  - 置信度: 中

### 通过项
- 线程安全：validator 无状态，validate 可重入。
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及。
- 状态提交一致性：stale revision 双参比较逻辑正确（仅集成缺失见 AUD-23211）；budget 超限为 warning、能力缺失为 error，severity 分层符合 plan §6.4。
- 异常与取消路径：纯发现收集（findings），不抛异常不中断，调用方按 severity 决策。
- 性能模式：Kahn 拓扑 O(V+E)，`queue.pop(0)` 对图规模无实际影响。
- DAG 环检测、capability/network 检查、budget 累计逻辑本身正确（含 report.any 通配端口豁免处理）。

---

## 附：跨文件验证记录（只读检索）

- `params_schema` 消费方：仅 `ai_workflow/ui/workflow_inspector.py`、`ui/workflow_form_page.py`（渲染控件）与 adapters 声明——无校验点（支撑 AUD-23219）。
- `allow_multi` 消费方：仅 `core/node_specs.py:78` 定义与 `tests/test_node_specs.py:36`——无执行点（支撑 AUD-23218）。
- `target_duration` 数据流：`workflow_compiler.py:294` 硬编码 720 → `ai_harness/legacy_adapters.py:70,88` 消费；`node_<id>_params` 无任何运行时消费方（支撑 AUD-23210）。
- 模型级节点删除：ai_workflow 内仅 `ui/workflow_canvas_adapter.py:105 remove_node_visual`（视觉层），无 `remove_node` 图操作（支撑 AUD-23213）。
