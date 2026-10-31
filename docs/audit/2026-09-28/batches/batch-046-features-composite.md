# batch-046 — src/features/composite//（P1）

- 任务书：docs/audit/2026-09-28/assignments/batch-046.md
- 编号区间：AUD-4600–AUD-4699
- 审查日期：2026-09-28
- 文件数：1（268 行）

## src/features/composite/bake_service.py (行数: 268)

Render-in-Place / Bake Subtree 服务：渲染回调注入、序列命名、图重写（disable 目标 + 插入
image_input 烘焙节点 + 重接线消费者）、before/after 全图 dict 的 undo 动作、反查式 Unbake。
当前仅被 tests/composite/test_bake_service.py 引用，尚未接入 UI（A6 里程碑）。

### 发现

- [AUD-4600] [Major] 烘焙重接线静默失败：connect() 返回 False 而非抛异常，except 分支是死代码
  - 位置: src/features/composite/bake_service.py:164-172
  - 证据:
    ```python
    for conn in connections:
        if (getattr(conn, "src_node_id", None) == plan.target_id
                and getattr(conn, "src_port_name", None) == primary_out):
            try:
                graph.connect(plan.bake_node_id, "image",
                              conn.dst_node_id, conn.dst_port_name)
            except Exception as exc:
                logger.warning("bake rewire failed for %s: %s",
                               conn.dst_node_id, exc)
    ```
    而 NodeGraph.connect（src/features/composite/core/node_engine.py:1163-1216）校验失败时
    `return False`，不抛异常。
  - 影响: 连线校验失败（端口缺失/类型不匹配/成环）时既不报警也不计数，消费者仍挂在已被
    disable 的目标上（走 bypass 直通或空结果，内容错误）；随后 `plan.after_graph = graph.to_dict()`
    把半重写状态固化进 undo 动作，烘焙在调用方视角"成功"但拓扑错误且无任何日志。
  - 建议: 检查 `graph.connect(...)` 的布尔返回值，失败时累计并抛出/返回错误，阻止生成
    after_graph 与 undo 动作（或至少强制 logger.warning）。
  - 置信度: 高

- [AUD-4601] [Major] Unbake 忽略 connect() 失败仍删除烘焙节点，可永久断开下游消费者
  - 位置: src/features/composite/bake_service.py:251-267
  - 证据:
    ```python
    for conn in list(getattr(graph, "connections", [])):
        if getattr(conn, "src_node_id", None) == plan.bake_node_id:
            try:
                graph.connect(plan.target_id, primary_out,
                              conn.dst_node_id, conn.dst_port_name)
            except Exception as exc:
                logger.warning("unbake rewire failed for %s: %s",
                               conn.dst_node_id, exc)
    ...
    graph.remove_node(plan.bake_node_id)
    ```
    NodeGraph.remove_node（node_engine.py:497-504）会移除该节点的全部连接。
  - 影响: 重连失败（如烘焙后用户改图导致成环、端口类型/名称变化）时连接返回 False 被忽略，
    随后 remove_node 把烘焙节点的连线一并删除，该消费者输入悬空，下游渲染为空/报错，且无日志。
  - 建议: 逐条检查 connect 返回值；存在失败时先不 remove_node，回滚已做的重连或上报错误，
    由调用方决定是否继续。
  - 置信度: 高

- [AUD-4602] [Major] 重写只处理目标的第一个输出端口，多输出节点（keyer matte/shuffle 等）的次端口消费者烘焙后拿空结果
  - 位置: src/features/composite/bake_service.py:160-166
  - 证据:
    ```python
    primary_out = (target.outputs[0].name
                   if getattr(target, "outputs", None) else "result")
    for conn in connections:
        if (getattr(conn, "src_node_id", None) == plan.target_id
                and getattr(conn, "src_port_name", None) == primary_out):
    ```
    base_node.py:966-984 的 bypass 仅做首输入→首输出映射，其余输出端口返回空。
  - 影响: 烘焙多输出目标（如 keyer 的 "matte" 输出的消费者）时这些连线保持指向 disabled
    目标，bypass 求值不产生对应纹理，下游得到空结果——渲染错误。另外多输入 dst 端口
    （max_connections>1）满载时 connect 会拒绝（旧连线残留），无上限端口则被追加第二条
    来源连线（重复输入）。
  - 建议: 重写前按端口分组枚举目标全部被消费的输出端口；对无对应烘焙产物的次端口要么
    明确拒绝烘焙（校验目标为单图像输出），要么为每个端口生成独立序列；接线后校验消费者
    输入端口连接数没有翻倍。
  - 置信度: 高（机制确定；具体下游表现取决于求值器对空输入的处理）

- [AUD-4603] [Major] bake/unbake 往返会给从未改名的节点永久写入 user_label，冻结 i18n 翻译与实例编号语义
  - 位置: src/features/composite/bake_service.py:223-231, 259-263
  - 证据:
    ```python
    original_title = target.display_title
    ...
    elif not target.user_label:
        original_title = target.node_title
    ...
    target.user_label = (None if plan.original_title
                         == target.node_title
                         else plan.original_title)
    ```
    display_title（base_node.py:394-408）= user_label > i18n 翻译 > node_title+instance_label。
    37 个节点类型声明了 i18n_title_key（如 blur_node.py:55）。
  - 影响: 对 i18n 节点或带 instance_label 的节点（display "模糊1" ≠ node_title "Blur"），
    相等判断永假，unbake 后 user_label 被固化为当时的翻译标题：切换语言不再生效
    （user_label 优先级最高）、节点被持久化为"用户已改名"状态，且随 to_dict 序列化进工程。
  - 建议: plan_bake 时记录 `target.user_label`（含 None）而非仅标题字符串，unbake 原样恢复
    user_label；相等判断不应以 node_title 为基准。
  - 置信度: 高

- [AUD-4604] [Suggestion] 向 ImageInputNode 注入 schema 外参数 cache_to_disk 并随工程序列化
  - 位置: src/features/composite/bake_service.py:146-158
  - 证据:
    ```python
    for param, value in (
            ("file_path", plan.sequence_pattern),
            ("frame_start", max(1, plan.frame_start)),
            ("frame_end", max(1, plan.frame_end)),
            ("cache_to_disk", False),
    ):
    ```
    ImageInputNode.PARAM_DEFS（image_input_node.py:109-135）不含 cache_to_disk；该参数仅存在于
    lens_flare/relight/godrays/depth_matte 等节点；base Node.set_parameter（base_node.py:643-651）
    不按 PARAM_DEFS 校验，直接写入。
  - 影响: 烘焙节点 parameters 里出现未定义参数并被 to_dict 深拷贝持久化，污染工程 schema；
    UI 参数面板/校验层遇到未知键时行为未定义。
  - 建议: 删除 cache_to_disk 写入（image_input 无此语义），或先在 ImageInputNode.PARAM_DEFS
    中正式定义。
  - 置信度: 高

- [AUD-4605] [Suggestion] render_sequence 的 pad/ext 形参可与烘焙节点序列模式静默失配
  - 位置: src/features/composite/bake_service.py:90-95, 110-111；对照 69-87, 146-149
  - 证据:
    ```python
    def render_sequence(graph: Any, plan: BakePlan, output_dir: str,
                        base_name: str,
                        render_fn: Callable[[str, int, str], bool],
                        frames: Optional[Sequence[int]] = None,
                        pad: int = 4, ext: str = "exr",
    ...
        out_path = build_sequence_path(output_dir, base_name, frame, pad, ext)
    ```
    而 plan.sequence_pattern 在 plan_bake 中以硬编码默认 pad=4/ext="exr" 生成
    （`sequence_pattern_for(output_dir, base_name)`），apply_bake_rewrite 也不写 frame_padding。
  - 影响: 调用方传 pad=6 或 ext="dpx" 时文件名为 name.000001.dpx，节点模式仍解析
    name.####.exr（frame_sequence.py:32-35 按 # 个数 zfill）→ 全部帧缺失，missing_policy=error
    时节点 FAILED。当前调用（仅测试）都用默认值，属潜伏契约缺口。
  - 建议: pad/ext 收敛到 BakePlan（plan_bake 一处计算并传递），或 render_sequence 从
    plan.sequence_pattern 推导 pad/ext，禁止两处独立默认。
  - 置信度: 中（机制确定，现无生产调用方触发）

- [AUD-4606] [Suggestion] set_parameter 的 except 回退分支为死代码，且会绕过脏标记追踪
  - 位置: src/features/composite/bake_service.py:152-158
  - 证据:
    ```python
    if hasattr(bake_node, "set_parameter"):
        try:
            bake_node.set_parameter(param, value)
        except (ValueError, TypeError):
            bake_node.parameters[param] = value
    else:
        bake_node.parameters[param] = value
    ```
    base Node.set_parameter（base_node.py:643-651）对任意键只做赋值，从不 raise。
  - 影响: except 与 hasattr 分支实际不可达；若未来 set_parameter 开始校验抛错，回退分支
    直接写 parameters 不更新 _dirty/param_revision，节点处于"参数已变但未标脏"状态。
  - 建议: 删除回退，直接调用 set_parameter；如需容错至少手动补 `bake_node._dirty = True`。
  - 置信度: 高

- [AUD-4607] [Suggestion] render_sequence 的 graph 形参从未使用
  - 位置: src/features/composite/bake_service.py:90-96
  - 证据:
    ```python
    def render_sequence(graph: Any, plan: BakePlan, output_dir: str,
                        base_name: str,
                        render_fn: Callable[[str, int, str], bool],
    ```
    函数体（104-121 行）只引用 plan/output_dir/base_name/render_fn/frames/pad/ext/is_cancelled。
  - 影响: API 误导调用方以为渲染会读图状态；测试也为此传入了 graph。
  - 建议: 移除 graph 形参（同步更新测试与将来的 UI 接线）。
  - 置信度: 高

- [AUD-4608] [Suggestion] 图重写绕过 NodeGraph 的 RLock 直接读 connections、改节点状态
  - 位置: src/features/composite/bake_service.py:136, 164-175, 251-263
  - 证据:
    ```python
    connections = list(getattr(graph, "connections", []))
    ...
    target.disabled = True
    target.user_label = f"{plan.original_title}{BAKED_TITLE_SUFFIX}"
    ```
    NodeGraph 自身的访问器（node_engine.py:1246-1250 get_output_connections 等）都持有
    self._lock；set_node_parameter（node_engine.py:622-637）也是锁内改参。
  - 影响: 仅在"图变更只发生在 UI 线程"的约定下安全；该约定在本模块既未文档化也未强制。
    若在 worker 线程调用（docstring 自称 UI-independent），与渲染线程的图读取存在数据竞争。
  - 建议: 改用 graph.get_output_connections()/set_node_parameter 等锁内接口，或在模块
    docstring 明确"必须 UI 线程调用"并加断言。
  - 置信度: 中（取决于实际调用线程；当前无生产调用方）

- [AUD-4609] [Suggestion] plan_bake 不校验帧范围与 base_name，frame_start<=0 被静默钳制
  - 位置: src/features/composite/bake_service.py:69-87, 148-149
  - 证据:
    ```python
    frame_start=int(frame_start),
    frame_end=int(frame_end),
    ...
    ("frame_start", max(1, plan.frame_start)),
    ("frame_end", max(1, plan.frame_end)),
    ```
  - 影响: frame_end < frame_start 只在渲染阶段以泛化的 "no frames rendered" 报错；frame_start=0
    时文件仍按 0 帧写出（name.0000.exr）但节点参数被钳为 1，范围元数据与磁盘内容不一致；
    base_name 含路径分隔符时可逃逸 output_dir。
  - 建议: plan_bake 入口校验 frame_start >= 0 且 frame_end >= frame_start、base_name 不含
    路径分隔符，违规即 ValueError。
  - 置信度: 中

- [AUD-4610] [Suggestion] make_undo_action 不防护 before/after 为空 dict 的误用，可致整图清空
  - 位置: src/features/composite/bake_service.py:45-46, 194-204
  - 证据:
    ```python
    before_graph: Dict[str, Any] = field(default_factory=dict)
    after_graph: Dict[str, Any] = field(default_factory=dict)
    ...
    undo_fn=lambda d: _apply(d["before"]),
    redo_fn=lambda d: _apply(d["after"]),
    ```
    applier 约定为 NodeGraph.from_dict 重建（测试 134-139 行即此用法）。
  - 影响: 若调用方在 apply_bake_rewrite 之前构造 undo 动作（dataclass 默认 {} 掩盖遗漏），
    undo/redo 会以空 dict 重建图——整个合成被清空（数据丢失）。
  - 建议: make_undo_action 开头断言 before/after 非空且含 "nodes"，空即 raise ValueError。
  - 置信度: 中（属调用契约缺陷，正确流程下不触发）

### 通过项

- GPU 资源生命周期：模块不直接触碰 ModernGL/纹理，渲染经注入的 render_fn 回调，GPU 资源归属渲染方；无需本模块释放。
- GLSL·HDR 正确性：无 GLSL 代码；烘焙产物为 EXR 序列路径（无 clamp(0.0,1.0) 类操作），色彩空间由 image_input 按扩展名推断（.exr→linear）。
- 信号防递归：纯服务层，无 Qt 控件/信号，不适用。
- 异常与取消路径（渲染侧）：render_fn 异常转换为带帧号的错误串返回，is_cancelled 每帧检查，部分写入文件经 written 返回供调用方清理；render_sequence 的 except Exception 有明确语义（中止烘焙）不属掩盖。
- 状态提交一致性（机制）：before/after 全图 dict + 调用方 applier 的 undo/redo 与 tracker-bake 模式一致，幂等重试（bake 节点已存在则复用）已处理；user_label 恢复缺陷已单列 AUD-4603。
- 性能模式：每帧一次回调、无重复磁盘 IO；plan_bake 的一次性全图 to_dict 快照开销可接受。
- 类型注解：全部函数签名与 dataclass 字段均有注解（graph: Any 宽松但非缺失）。
