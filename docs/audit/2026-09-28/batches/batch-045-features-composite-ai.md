# batch-045 审查报告 — src/features/composite/ai//

任务书：docs/audit/2026-09-28/assignments/batch-045.md（编号区间 AUD-4500–AUD-4599）
审查范围：2 个文件，348 行。统计：Critical 0 / Major 2 / Suggestion 5。

消费方调研（用于影响评估）：`src/features/composite/edit_service.py:43-52` 以
`SUPPORTED_NODE_OPS` 为协议对齐基准；`tests/ai/test_cg_cp_ix.py`、
`tests/composite/test_script_graph_ops.py`、`tests/composite/test_graph_ai_ops_extended.py`
直接导入本模块。当前仓库内无生产代码调用 `CompositePlan.plan_hash()` 做审批绑定。

## src/features/composite/ai/__init__.py (行数: 0)

### 发现

（无）

### 通过项
- 通过（无逻辑）：空文件（0 字节），未导出任何符号；包内符号需经
  `src.features.composite.ai.composite_plan` 显式导入，与现有测试的导入方式一致。

## src/features/composite/ai/composite_plan.py (行数: 348)

### 发现

- [AUD-4500] [Major] frozen 数据类持有外部可变 Mapping，plan_hash/to_dict 可在构造后被篡改
  - 位置: src/features/composite/ai/composite_plan.py:115（另见 49-50、148-162）
  - 证据:
    ```python
    parameters: Mapping[str, Any] = field(default_factory=dict)   # L115
    ...
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()  # L161-162
    ```
  - 影响: `frozen=True` 仅冻结属性重绑定；构造时传入的 dict（以及
    `output_spec`/`keyframes`/`details` 等 Mapping 字段）按引用保留。调用方在
    构造后原地修改该 dict，`plan_hash()` 与 `to_dict()` 的输出随之改变——模块
    自述的"hash-bound composite execution plan"契约（保证审批时哈希与执行时
    一致）失效，且无任何报错。当前仓库内仅测试消费此哈希，但
    `tests/ai/test_cg_cp_ix.py:197-208` 的哈希稳定性依赖调用方不再改动入参字典这一隐式约定。
  - 建议: 在 `__post_init__` 中对 Mapping 字段做防御性深拷贝并转
    `types.MappingProxyType`（或存 `json.dumps` 后的规范串、哈希一次性预计算
    缓存），使哈希在构造时刻固定。
  - 置信度: 高（机制确定；生产影响取决于未来接入审批管线的调用方）

- [AUD-4501] [Major] validate_composite_plan 校验缺口：空 node_type / 空 node_id 不拦截，docstring 声称的检查未实现
  - 位置: src/features/composite/ai/composite_plan.py:296（另见 310-316、285-289）
  - 证据:
    ```python
    for op in plan.operations:
        if op.kind == "add_node" and op.node_type and available and op.node_type not in available:
            ...
        if op.kind in ("connect", "disconnect") and (not op.from_port or not op.to_port):
            ...
    ```
    docstring（L288-289）：“Checks that every referenced node type exists …
    and no operation references an unknown node.”
  - 影响: (1) `add_node` 且 `node_type=""` 时条件短路，无任何 finding——添加
    无类型节点必然在服务层失败，却通过 dry-run 门禁；(2) `remove_node`/
    `set_param`/`duplicate_node`/`group`/`ungroup`/`set_output_target`/
    `connect`/`disconnect` 均需目标节点，但 `node_id=""` 从不被校验
    （`node_id` 仅在 L301/308 作为 finding 附带字段使用）；(3) docstring 第三项
    "no operation references an unknown node" 无对应实现（port 检查不等于
    节点存在性检查）。作为 CP-03/CP-09 预渲染门禁，无效计划可静默通过并在
    执行期才报错。
  - 建议: 增加 `add_node` 空 `node_type`（error 级）与"需目标节点 op 的
    `node_id` 非空"校验；接收 `existing_node_ids` 参数时校验引用存在；同步
    修正 docstring 与实际检查集合。
  - 置信度: 高

- [AUD-4502] [Suggestion] __all__ 漏导出 NODE_OP_DUPLICATE_NODE / NODE_OP_SET_OUTPUT_TARGET
  - 位置: src/features/composite/ai/composite_plan.py:324-348（对照 95-96、101）
  - 证据:
    ```python
    NODE_OP_DUPLICATE_NODE = "duplicate_node"     # L95
    NODE_OP_SET_OUTPUT_TARGET = "set_output_target"  # L96
    ...
    "NODE_OP_UNGROUP",                            # L346 —— __all__ 到此为止
    "validate_composite_plan",                    # L347
    ```
  - 影响: T-P1-60 批次 B 新增的两个常量进入了 `SUPPORTED_NODE_OPS` 但未进
    `__all__`，`from ... import *` 的消费方拿不到它们，协议常量导出不一致。
  - 建议: 将两个常量补入 `__all__`。
  - 置信度: 高

- [AUD-4503] [Suggestion] validate_composite_plan 用字符串字面量而非 NODE_OP_* 常量比对 kind
  - 位置: src/features/composite/ai/composite_plan.py:296、310
  - 证据:
    ```python
    if op.kind == "add_node" and op.node_type and available and op.node_type not in available:
    ...
    if op.kind in ("connect", "disconnect") and (not op.from_port or not op.to_port):
    ```
  - 影响: 同文件已定义 `NODE_OP_ADD_NODE`/`NODE_OP_CONNECT`/`NODE_OP_DISCONNECT`
    （L87-89）作为协议单一事实来源；字面量与之脱钩，若常量值调整，校验静默失配。
  - 建议: 改用常量比对。
  - 置信度: 高

- [AUD-4504] [Suggestion] available_node_types 为空时 fail-open，且 checks_run 仍宣称 node_type_exists 已运行
  - 位置: src/features/composite/ai/composite_plan.py:282、292、296
  - 证据:
    ```python
    available_node_types: Iterable[str] = (),    # L282
    ...
    checks: list[str] = ["node_type_exists", "gpu_available", "port_references"]  # L292
    ...
    if op.kind == "add_node" and op.node_type and available and op.node_type not in available:
    ```
  - 影响: 默认（空注册表）下节点类型检查整体跳过，但报告的 `checks_run` 仍
    包含 "node_type_exists"，门禁结果对上层具有误导性；调用方忘记传注册表时
    静默失去该保护（fail-open）。
  - 建议: 空注册表时不把 "node_type_exists" 计入 `checks_run`（或区分
    skipped 状态）；或要求显式传 `None` 表示放弃该检查。
  - 置信度: 中（跳过逻辑可能是刻意设计，但 checks_run 的表述与实际不符是客观的）

- [AUD-4505] [Suggestion] severity / check 字符串未做枚举校验，拼错将使门禁静默放行
  - 位置: src/features/composite/ai/composite_plan.py:196-199、243-248（消费点 214-219、259-264）
  - 证据:
    ```python
    @dataclass(frozen=True)
    class ComposabilityFinding:
        aspect: str
        severity: str             # error / warning / info   ← 仅注释约束
        ...
    class CompositeQualityFinding:
        check: str
        severity: str             # ← 无 __post_init__ 校验
    ```
    而 `is_blocker = any(f.severity == "error" ...)`、`is_deliverable = not self.errors`。
  - 影响: 本模块对 `desired_effect`（L58）和 `kind`（L123）都有枚举校验，
    唯独 severity 只有注释约束。外部（LLM/适配层）构造 finding 时写成
    "Error"/"fatal" 等同义串，`errors` 过滤为空 → `is_deliverable=True`，
    质量门禁对坏结果放行且无告警。
  - 建议: 为 severity（必要时连同 check 名）定义 frozenset 并在
    `__post_init__` 校验，或提供工厂函数集中构造。
  - 置信度: 中（当前模块内部字面量正确；风险来自外部构造方）

- [AUD-4506] [Suggestion] plan_hash 的 json.dumps(default=str) 静默字符串化，可能产生哈希碰撞
  - 位置: src/features/composite/ai/composite_plan.py:161
  - 证据:
    ```python
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    ```
  - 影响: `default=str` 使任意不可序列化对象（含自定义对象）静默退化为
    str——不同类型的参数值可能得到同一规范串（哈希碰撞，漂移检测失真）；
    另外键不受 `default` 影响，参数字典含混合类型键时仍会抛 TypeError，
    哈希计算在 to_dict 路径上意外失败。
  - 建议: 去掉 `default=str`，让不可序列化参数在构造期显式报错；或先用
    严格模式序列化、失败即抛领域错误。
  - 置信度: 高（机制确定；实际触发取决于 parameters 内容）

### 通过项
- 线程安全：纯不可变数据类 + 无模块级可变共享状态，无锁需求（可变 Mapping 引用问题见 AUD-4500）。
- GPU 资源生命周期：不创建/持有任何 ModernGL 或 OpenGL 对象，仅 `requires_gpu` 布尔元数据；无泄漏面。
- GLSL·HDR 正确性：本文件无 GLSL 代码、无像素数据处理；`score`/`overall_score` 为元数据评分，不涉及 HDR clamp 约束。
- 信号防递归：无 Qt 控件/信号，不适用。
- 异常路径：`__post_init__`（L55-63、120-124、142-146）用 ValueError 抛出明确领域错误，无宽泛 except；无取消路径需求。
- 性能模式：`plan_hash`/`to_dict` 为 O(n) 小规模计算，`frozenset` 成员查询 O(1)；`tuple(dict.fromkeys(checks))`（L320）对常量列表去重无害。
- 类型注解：全部函数与字段均有类型注解，含 `from __future__ import annotations` 的现代联合语法。
- 持久化一致性：`to_dict` 输出 JSON 可序列化结构（受 AUD-4500/AUD-4506 的引用与序列化问题影响外，无其他问题）；`plan_hash` 未混入 `render_cost_estimate` 等可变元数据，绑定语义合理。
