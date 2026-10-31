# batch-051 审查报告 — src/features/composite/core//

批次：batch-051 [P1]；编号区间 AUD-5100–AUD-5199；审查日期 2026-09-28。
文件已全文通读（2,810 行，无跳读），并交叉核对了
`nodes/base_node.py`（set_parameter/_dirty 语义）、`core/graph_migration.py`、
`core/layer_model.py`、`utils/expression_parser.py` 及调用方
（`ui/node_graph_commands.py`、`core/layer_stack_controller.py`、
`app/controllers/file_controller.py`、`features/composite/edit_service.py`）。

---

## src/features/composite/core/node_engine.py (行数: 2810)

### 发现

- [AUD-5100] [Major] remove_node_and_reconnect 缺少 remove_node 具备的表达式/绑定/父子关系清理
  - 位置: src/features/composite/core/node_engine.py:578-598
  - 证据:
    ```python
    # Remove the node
    del self.nodes[node_id]

    # Clear graph reference if it's a GroupNode
    if isinstance(node, GroupNode):
        node._node_graph = None

    # Mark downstream nodes as dirty
    self._mark_dirty_downstream(node_id)

    self._force_full_eval = True
    ```
    对比 remove_node（497-511 行）会执行 `self._expression_manager.remove_node(node_id)`、
    清空其它节点指向被删节点的 `parameter_bindings` 与 `parent_id`。
  - 影响: 该方法是处理节点删除的主路径（node_graph_commands.py:118、layer_stack_controller.py:665、
    composite_page.py:2454 均调用）。删除后表达式管理器残留悬空依赖边；其它节点的 pick-whip
    绑定仍指向已删节点（get_parameter_binding 返回坏绑定）；`sample_value`（1500-1502 行）
    对缺失节点静默返回 0.0，引用被删节点的表达式参数会无声跳变到 0（错误渲染值）；被删节点的
    子节点 parent_id 悬空。
  - 建议: 将 remove_node 中的清理逻辑（expression manager、parameter_bindings、parent_id）
    提取为共享辅助方法，在两个删除路径中统一调用。
  - 置信度: 高

- [AUD-5101] [Major] remove_node_and_reconnect 在连接已被剥离后才调用 _mark_dirty_downstream，实际是空操作
  - 位置: src/features/composite/core/node_engine.py:579-592
  - 证据:
    ```python
    # Remove all connections involving this node
    self.connections = [
        c for c in self.connections
        if c.src_node_id != node_id and c.dst_node_id != node_id
    ]

    # Remove the node
    del self.nodes[node_id]
    ...
    # Mark downstream nodes as dirty
    self._mark_dirty_downstream(node_id)
    ```
    `_mark_dirty_downstream`（2351-2365 行）沿 `conn.src_node_id == current` 的边向下传播，
    但此时这些边已全部被过滤掉。
  - 影响: 被重连的下游节点 `_dirty` 保持 False、端口/revision 记账未更新。本类自身的
    evaluate() 因 `_force_full_eval = True` 清空整缓存而免疫，但任何依赖 `_dirty` 标志的
    增量求值方（如 CompositeRenderer 的独立求值路径、下游消费者）会看到陈旧状态。
  - 建议: 在过滤连接之前先对原下游节点集合做 dirty 标记，或直接对 `dst_conn.dst_node_id`
    逐个标记。
  - 置信度: 高（逻辑确定）；影响为中（受 _force_full_eval 缓解）

- [AUD-5102] [Major] evaluate() 遍历节点与缓存期间不持图锁，锁纪律不一致
  - 位置: src/features/composite/core/node_engine.py:1313-1344
  - 证据:
    ```python
    for node_id in eval_order:
        node = self.nodes[node_id]
        if node is None:
            continue
        if not node._dirty and node_id in self._evaluation_cache:
            self._evaluation_cache.move_to_end(node_id)
            cached = self._evaluation_cache[node_id]
        ...
        outputs = node.evaluate(input_textures, ctx, fb_manager)
        self._evaluation_cache[node_id] = outputs
    ```
    仅 topological_sort()/get_input_connections() 各自短暂取锁，主循环内
    `self.nodes[node_id]`、`_evaluation_cache`、`node._dirty` 均为无锁访问。
  - 影响: 若求值线程运行期间 UI 线程删除节点则 `self.nodes[node_id]` 抛 KeyError；
    与 `_mark_dirty_downstream` 的 `_dirty` 写入存在数据竞争。docstring 声明该方法保留给
    非求值/测试用途，故未升级为 Critical。
  - 建议: 在循环前一次性持锁拷贝所需的快照（或文档化"仅限单线程调用"并在断言中防护）。
  - 置信度: 高（不一致性客观）；真实暴露面中

- [AUD-5103] [Major] to_dict()/validate()/两个 migrate_* 公有方法无锁读写图状态
  - 位置: src/features/composite/core/node_engine.py:2087-2140（to_dict）、2367-2464、2466-2522
  - 证据:
    ```python
    def to_dict(self) -> dict:
        """Serialize the entire graph to a dict."""
        from .graph_migration import COMPOSITE_SCHEMA_VERSION
        data = {
            ...
            "nodes": [node.to_dict() for node in self.nodes.values()],
    ```
    `migrate_color_input_to_topos_color` 直接改写 `self.connections`、`self.nodes`
    （2403-2457 行）；`migrate_output_format_mode` 直接 set_parameter（2517-2521 行），
    全程无 `with self._lock`。
  - 影响: to_dict 被 file_controller/edit_service/bake_service 等调用，若序列化在工作线程
    执行而 UI 线程同时增删节点，会触发 `RuntimeError: dictionary changed size during
    iteration`；migrate_* 是公有 API（可在绑定 ColorStack 时再次调用），与渲染线程
    snapshot() 并发会竞态。
  - 建议: to_dict 全程持锁（deepcopy 可在锁内完成）；migrate_* 与 clear() 一样持
    `self._lock` 并 bump generation。
  - 置信度: 高（缺锁客观）；跨线程调用场景中

- [AUD-5104] [Major] from_dict 直接索引连接字段，损坏的项目数据在加载期抛未验证异常
  - 位置: src/features/composite/core/node_engine.py:2158-2176
  - 证据:
    ```python
    for node_data in data.get("nodes", []):
        node_type = node_data.get("type")
        ...
    for conn_data in data.get("connections", []):
        graph.connect(
            conn_data["src_node_id"],
            conn_data["src_port_name"],
            conn_data["dst_node_id"],
            conn_data["dst_port_name"],
        )
    ```
    `migrate_graph` 在版本号已最新时原样返回（graph_migration.py:870-871），不做结构校验。
  - 影响: 结构合法但字段缺失/类型错误的 JSON（手编、第三方工具或部分损坏的保存文件）在
    `conn_data["src_node_id"]` 处抛 KeyError、非 dict 节点项抛 AttributeError；
    file_controller.py:811 调用处无本地 try/except，整个工程加载失败甚至崩溃。违反
    "外部数据须验证" 铁律。
  - 建议: 连接/节点条目先做 dict 与必需字段校验，非法条目记录错误并跳过（或集中上报），
    而非让异常穿透加载路径。
  - 置信度: 高

- [AUD-5105] [Major] from_dict 将 groups/viewport_state/viewer_state 按引用别名赋值且无结构验证
  - 位置: src/features/composite/core/node_engine.py:2188-2199
  - 证据:
    ```python
    # Deserialize groups if present
    if "groups" in data:
        graph._node_groups = data["groups"]
    ...
    # Deserialize UI state (viewport and viewer)
    if "viewport_state" in data:
        graph._viewport_state = data["viewport_state"]
    if "viewer_state" in data:
        graph._viewer_state = data["viewer_state"]
    ```
  - 影响: (1) 图内部状态与调用方传入 dict 共享引用，外部后续修改会直接改变图状态；
    (2) 恶意/损坏数据中 group 缺 `'node_ids'` 键时，`get_group_for_node`（2060 行
    `if node_id in group['node_ids']`）抛 KeyError。对比 backdrops/sticky_notes 均做了
    deepcopy + 规范化（_load_backdrops/_load_sticky_notes）。
  - 建议: 仿照 _load_backdrops 做 deepcopy、dict 类型检查与 'node_ids' 规范化。
  - 置信度: 高

- [AUD-5106] [Suggestion] evaluate() 宽泛 except Exception 仅记 warning，不回写节点错误状态
  - 位置: src/features/composite/core/node_engine.py:1346-1353
  - 证据:
    ```python
            except Exception as e:
                logger.warning(f"Error evaluating node {node_id} ({node.node_type}): {e}")
                self._evaluation_cache[node_id] = {}
                # Enforce cache size limit after error entry too
                while len(self._evaluation_cache) > self.EVALUATION_CACHE_MAX_SIZE:
                    self._evaluation_cache.popitem(last=False)
    ```
  - 影响: 节点求值失败被吞掉，输出静默为空/黑，而 NodeSnapshot 明确携带
    `state/error_message` 字段可供上报；用户无从得知失败原因。
  - 建议: 失败时设置 `node.error_message`/状态（与快照通道一致），至少在 UI 可见处上报。
  - 置信度: 高

- [AUD-5107] [Suggestion] resolve_all_expressions 以副作用方式改写节点活参数
  - 位置: src/features/composite/core/node_engine.py:947-952
  - 证据:
    ```python
            for nid, params in resolved.items():
                node = self.nodes.get(nid)
                if node:
                    for param_name, value in params.items():
                        if node.has_expression(param_name):
                            node.parameters[param_name] = value
    ```
  - 影响: 与 snapshot 路径"只读、不改活节点"（get_expression_errors 文档明确此契约）不一致；
    帧相关的解析值渗入 node.parameters，随后 to_dict()/undo 快照会把某一帧的动画值固化为
    静态参数。
  - 建议: 改为纯函数返回值（如 resolve_node_expressions 那样），或文档化并仅在需要
    烘焙时调用。
  - 置信度: 高

- [AUD-5108] [Suggestion] resolve_node_expressions 硬编码 fps=24.0，忽略 self.fps
  - 位置: src/features/composite/core/node_engine.py:931-936
  - 证据:
    ```python
            values, _, _ = self._evaluate_snapshot_parameters(
                frame,
                (input_attrs or {}).get("width", 1920),
                (input_attrs or {}).get("height", 1080),
                24.0,
            )
    ```
  - 影响: 非 24fps 合成中依赖 `valueAtTime`/`loopOut` 的表达式按错误时间基解析。
    当前仓库内未发现 src/ 调用方（仅模块内定义），故降为 Suggestion。
  - 建议: 增加 fps 参数并默认 `self.fps`。
  - 置信度: 高

- [AUD-5109] [Suggestion] _evaluate_snapshot_parameters 无条件为所有节点生成参数别名深拷贝
  - 位置: src/features/composite/core/node_engine.py:1476-1477
  - 证据:
    ```python
        for node_id, params in list(raw_params.items()):
            raw_params[self._expression_alias(node_id)] = copy.deepcopy(params)
    ```
  - 影响: 即使图中完全没有表达式，每帧快照也要把全部节点参数再深拷贝一份（内存与 CPU
    翻倍），与 4K 实时基准的快照开销目标相悖。
  - 建议: 仅当 `expressions` 非空时生成别名条目，或只为被表达式引用的节点生成。
  - 置信度: 高

- [AUD-5110] [Suggestion] 图遍历复杂度 O(V·E)/O(n²)：环检测、下游标脏逐节点全量扫连接；拓扑排序用 list.pop(0)
  - 位置: src/features/composite/core/node_engine.py:2346-2348、2363-2365、1280-1281
  - 证据:
    ```python
            for conn in self.connections:
                if conn.src_node_id == current:
                    stack.append(conn.dst_node_id)
    ...
            while queue:
                node_id = queue.pop(0)
    ```
  - 影响: `_would_create_cycle`/`_mark_dirty_downstream` 在 while 循环内每次全量扫描
    connections；from_dict 加载期 N 次 connect() 各触发一次全图标脏，大图（数百节点/边）
    加载与交互明显劣化。
  - 建议: 遍历前一次性构建邻接表；Kahn 队列改用 `collections.deque.popleft()`。
  - 置信度: 高

- [AUD-5111] [Suggestion] 加载期 connect() 失败被静默丢弃且每次成功连接都 bump generation
  - 位置: src/features/composite/core/node_engine.py:2170-2176
  - 证据:
    ```python
        for conn_data in data.get("connections", []):
            graph.connect(
                conn_data["src_node_id"],
                ...
            )
    ```
  - 影响: 端口缺失/类型不兼容的连接在反序列化时无提示消失（用户保存的图加载后连线丢失且
    无诊断）；同时每次 connect 触发 `_mark_dirty_downstream` + generation 自增（见 AUD-5110）。
  - 建议: 收集失败的连接并在加载结果中上报；加载路径可绕过逐次 generation/dirty 记账，
    最后统一 bump。
  - 置信度: 高

- [AUD-5112] [Suggestion] snapshot() 拓扑成环时静默回退插入序，且循环体内重复 import copy
  - 位置: src/features/composite/core/node_engine.py:1807-1811、1700-1701
  - 证据:
    ```python
            try:
                eval_order = self._snapshot_topological_sort(
                    list(node_snaps), conn_snaps)
            except ValueError:
                eval_order = list(self.nodes.keys())
    ...
                for nid, node in self.nodes.items():
                    import copy
    ```
  - 影响: 组边界展平引入环时求值顺序退化为插入序且无日志，渲染结果不确定且难排查；
    循环内 `import copy` 遮蔽模块级导入（第 13 行），纯噪声。
  - 建议: 回退分支至少 logger.warning；删除循环内 import（模块顶部已导入）。
  - 置信度: 高

- [AUD-5113] [Suggestion] get_sticky_note/get_group 等取值器返回内部活 dict 引用
  - 位置: src/features/composite/core/node_engine.py:992-1002、2037-2047
  - 证据:
    ```python
        def get_sticky_note(self, note_id: str) -> Optional[Dict]:
            ...
            with self._lock:
                return self._sticky_notes.get(note_id)
    ...
        def get_group(self, group_id: str) -> Optional[Dict]:
            ...
            with self._lock:
                return self._node_groups.get(group_id)
    ```
  - 影响: 调用方在锁外修改返回的 dict 会绕过锁与 generation 记账直接改变图状态（对比
    set_backdrop/add_sticky_note 均存 deepcopy）。get_sticky_notes/get_all_groups 返回浅拷
    贝，嵌套值仍共享。
  - 建议: 返回 deepcopy，与写入侧对称。
  - 置信度: 高

- [AUD-5114] [Suggestion] unregister_node_type 对非 DEFERRED 类型也置 None，违反自述的注册表契约
  - 位置: src/features/composite/core/node_engine.py:2658-2660（对照注释 119-129）
  - 证据:
    ```python
        node_class = NODE_REGISTRY[node_type]
        NODE_REGISTRY[node_type] = None  # Keep placeholder for compatibility
    ```
    模块注释声明 "A None entry outside this map [DEFERRED_NODE_TYPES] is an unexplained
    placeholder bug — the registry contract test enforces exactly that split."
  - 影响: 任何插件类型（非 motion_graphic）注销后注册表留下不在 DEFERRED_NODE_TYPES 中的
    None 项，按注释将触发注册表契约测试失败。
  - 建议: 对非 DEFERRED 类型直接 `del NODE_REGISTRY[node_type]`，或将类型加入 DEFERRED
    记录后再置 None。
  - 置信度: 中（取决于契约测试的具体断言范围）

- [AUD-5115] [Suggestion] 关键签名缺类型注解 / 隐式 Optional
  - 位置: src/features/composite/core/node_engine.py:1293、1235、2351、2600、2779
  - 证据:
    ```python
    def evaluate(self, ctx, fb_manager) -> Optional[TextureResult]:
    ...
    def disconnect_by_dst(self, dst_node_id: str, dst_port_name: str):
    ...
    def _mark_dirty_downstream(self, node_id: str):
    ...
    def register_node_type(node_type: str, node_class: Type[Node], category: str = None) -> bool:
    ...
    def create_node_by_type(node_type: str, node_id: str = None, **kwargs) -> Optional[Node]:
    ```
  - 影响: evaluate 的 ctx/fb_manager 完全无类型；多处隐式 Optional（`str = None`）不符合
    项目强类型要求。
  - 建议: 补充注解并改为 `Optional[str] = None`。
  - 置信度: 高

- [AUD-5116] [Suggestion] 求值缓存 LRU 驱逐与 clear() 直接丢弃 TextureResult，未走 deferred-release
  - 位置: src/features/composite/core/node_engine.py:1342-1343、616
  - 证据:
    ```python
                while len(self._evaluation_cache) > self.EVALUATION_CACHE_MAX_SIZE:
                    self._evaluation_cache.popitem(last=False)  # Remove oldest
    ...
            self._evaluation_cache.clear()
    ```
  - 影响: TextureResult 持有 OpenGL texture_id；缓存驱逐/清图时仅丢弃引用。若纹理由
    fb_manager 统一持有则无害（当前渲染器自管），但该引擎路径一旦被启用即违反项目
    "GPU 资源按 deferred-release 模式释放" 铁律。
  - 建议: 在驱逐/清空前通过渲染线程的 deferred-release 队列释放其持有的纹理，或文档化
    所有权归 fb_manager。
  - 置信度: 中（所有权归属未在本文件内定义）

- [AUD-5117] [Suggestion] has_connected_output 只检查第一个 OutputNode
  - 位置: src/features/composite/core/node_engine.py:1357-1363
  - 证据:
    ```python
        with self._lock:
            for nid, node in self.nodes.items():
                if isinstance(node, OutputNode):
                    return len(self.get_input_connections(nid)) > 0
        return False
    ```
  - 影响: 图中存在多个 output 类型节点且第一个无输入时误报 False（validate() 逐个检查则正确）。
  - 建议: 遍历全部 OutputNode，任一有输入即 True（或按产品语义取主 Output）。
  - 置信度: 高（逻辑）；多 Output 场景罕见

### 通过项
- GLSL/HDR 正确性：本文件为纯图模型层，无 GLSL 代码；不触碰 HDR 像素数据，无 clamp(0,1) 问题
- 信号防递归：本模块不含 Qt 信号，不适用
- GPU 生命周期（删除路径）：remove_node 明确文档化"GPU 清理交由调用方经渲染线程调度"，不在此处触碰 GL（缓存丢弃路径见 AUD-5116）
- connect() 校验完整：类型兼容、成环检测先于替换旧连接；单输入满载替换/多输入满载拒绝的策略有注释且实现一致
- set_node_parameter 增量失效链完整：base_node.set_parameter 内部维护 _dirty/param_revision/bump_output_revision（base_node.py:643-651），值变化时叠加表达式传递失效
- NaN/Inf 防护：_evaluate_snapshot_parameters 对非有限浮点回退安全基值（1597-1601 行），符合"不向 GPU uniform 送 NaN"约束
- set_node_parent 拒绝自身/祖先环（724-732 行），并同步 bump 三个 revision
- LRU 求值缓存有 100 上限（成功与异常路径都执行驱逐），防止无界增长
- 快照不可变契约：snapshot() 在锁内完成 deepcopy，渲染线程拿到的是无共享可变状态（除文档化的 LayerStack 共享）
- 反序列化容错：backdrops/sticky_notes 支持 dict/list 两种形态并 deepcopy 规范化；layer 栈加载捕获 KeyError/TypeError 跳过坏条目；migrate_graph 迁移链版本化
- 遗留迁移：color_input→topos_color 迁移含独立图（无 timeline 源）保护分支；Output format_mode 迁移含 image_input 尺寸为 0 的防护（P1-04 注释）
- 组边界展平：_resolve_group_port 有递归访问保护，_flatten_group_boundaries 去重边且不改活图
- 表达式循环依赖：set_node_expression 检测到环时回滚表达式并返回错误（843-847 行）

---

## 汇总

| 严重度 | 数量 |
|--------|------|
| Critical | 0 |
| Major | 6（AUD-5100 ~ AUD-5105） |
| Suggestion | 12（AUD-5106 ~ AUD-5117） |

无 Critical 发现：本文件为图模型/注册表层，不直接触碰 GLSL、HDR 数据与 UI 信号；
最高风险集中在删除路径的状态清理缺口（AUD-5100/5101）、锁纪律不一致（AUD-5102/5103）
与外部数据加载验证（AUD-5104/5105）。
