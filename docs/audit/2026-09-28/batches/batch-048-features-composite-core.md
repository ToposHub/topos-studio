# batch-048 审查报告 — src/features/composite/core

- 批次: batch-048 [P1] 主题 src/features/composite/core//
- 编号区间: AUD-4800–AUD-4899（本报告使用 AUD-48001–AUD-48015）
- 文件数: 5（共 2,686 行）
- 审查维度: 线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解
- 汇总: Critical 1 · Major 1 · Suggestion 13

交叉验证说明: `run_preflight_data` 当前唯一调用点为 `src/app/controllers/composite_controller.py:965`（传入活动图 `page.get_composite_data()` 的序列化结果）；`LAYER_BLEND_MODE_TO_NODE` 的 12 个目标 token 已对照 `src/features/composite/nodes/blend_node.py` 的 `BLEND_MODES` 全部核对存在；`ImageLoader` 在 `src/features/composite/render/composite_renderer.py:476` 于渲染线程（无 Qt 事件循环的手动队列线程）构造并使用，`cancel_all` 在渲染器清理路径（composite_renderer.py:2006）调用。上述事实影响多条发现的严重度判定。

---

## src/features/composite/core/graph_preflight.py (行数: 1199)

### 发现

- [AUD-48001] [Suggestion] `_registered_port_schema` 用无日志的宽泛 `except Exception` 吞掉节点构造错误，端口契约校验被静默跳过
  - 位置: src/features/composite/core/graph_preflight.py:659-663
  - 证据:
    ```python
    def _registered_port_schema(
            node_type: str, parameters: dict, node_id: str
            ) -> Optional[Tuple[Dict[str, str], Dict[str, str]]]:
        try:
            from .node_engine import NODE_REGISTRY
            node_cls = NODE_REGISTRY.get(node_type)
            ...
            return (
                {str(port.name): str(port.data_type) for port in node.inputs},
                {str(port.name): str(port.data_type) for port in node.outputs},
            )
        except Exception:
            # A third-party node constructor may require optional dependencies.
            # Its capability/unknown-node diagnostic remains actionable; port
            # preflight must not make the whole report crash.
            return None
    ```
  - 影响: 任何内置/第三方节点构造器抛错（编程错误、缺依赖、需要 GL 上下文等）时，该节点的全部端口方向/类型校验被静默跳过，序列化图中的非法连接可能通过导出预检；同时构造器的领域错误被完全掩盖（违反 .clinerules “不得宽泛 except 掩盖领域错误”）。对比同文件 `probe_exr_header`（line 260-262）失败时有 `logger.warning`，此处无任何日志。
  - 建议: 至少加 `logger.debug/warning` 记录被吞异常；仅捕获可预期的导入/构造失败（如 ImportError + 构造器显式声明的依赖错误），并区分“无 schema”与“构造崩溃”两种返回。
  - 置信度: 高

- [AUD-48002] [Suggestion] 序列化 payload 的 isinstance 防护不一致：`_check_groups` 已诊断的非法映射仍会使 `_flatten_group_connections` / 边界环检测崩溃
  - 位置: src/features/composite/core/graph_preflight.py:810（及 791、1105）
  - 证据:
    ```python
    # _flatten_group_connections.resolve() — line 805-813
    while current_id in groups:
        marker = (current_id, current_port)
        if marker in visited:
            return str(node_id), str(port_name)
        visited.add(marker)
        target = _group_target(groups[current_id][mapping_name].get(current_port))
    ```
    而 `_check_groups` 对同一数据显式按非 dict 诊断（line 722-730）：
    ```python
    mappings = info[mapping_name]
    if not isinstance(mappings, dict):
        report.issues.append(PreflightIssue(
            code="group.invalid_mapping", severity=SEVERITY_ERROR, ...))
        continue
    ```
    `_group_info`（line 692-693）存入原始值时不做类型检查：`"inputs": nd.get("input_mappings") or {}`。
  - 影响: `_flatten_group_connections` 的 docstring（line 529-531）声称失效 Group 端点“left alone here to avoid hiding the more useful mapping diagnostic”，但若某 Group 的映射是非 dict（外部工具/旧版工程数据），`_check_groups` 报出 `group.invalid_mapping` 后，line 810/791 的 `.get()` 仍会抛 AttributeError 使整个 `run_preflight_data` 崩溃——预检对话框变成未捕获异常而非诊断列表。同理 `_check_layers`（line 1105 `for lid, ld in (layers or {}).items():`，`ld` 非 dict 时 `(ld or {}).get` 亦抛错）缺少其兄弟函数（`_check_precomps` 988-991、`_check_layer_chain_conflicts` 1070-1071、`_check_track_mattes` 1143-1144）都有的 isinstance 防护。当前唯一调用链传入活动图 `to_dict()` 数据，实际触发概率低。
  - 建议: 在 `_group_info` 入口把非 dict 映射规范化为 `{}`（诊断已由 `_check_groups` 负责）；`resolve()`/边界环循环用 `isinstance(..., dict)` 短路；`_check_layers` 补齐与兄弟函数一致的 dict 防护。
  - 置信度: 中

- [AUD-48003] [Suggestion] `_check_render_pass_contract` 用原始 connections 而非 Group 展平后的边，Group 内的 Scene3D AOV 端口绕过检查
  - 位置: src/features/composite/core/graph_preflight.py:455-471
  - 证据:
    ```python
    for connection in graph_data.get("connections") or ():
        if not isinstance(connection, dict):
            continue
        source_id = str(connection.get("src_node_id", ""))
        source_port = str(connection.get("src_port_name", ""))
        if (node_types.get(source_id) != "scene3d"
                or source_id not in scene_ids
                ...
    ```
    而拓扑/可达性均使用 `effective_connections = _flatten_group_connections(nodes, connections)`（line 166），`scene_ids` 的 `contributing` 即由展开边推导（line 248-261）。
  - 影响: Scene3D 收进 Group 后，其 AOV 输出端口的原始连接 src 是 Group 节点而非 scene3d，`render_pass.port_disabled` 检查被跳过（漏报）；实际求值走展开边，诊断与运行时拓扑不一致。
  - 建议: 该检查改用 `effective_connections`（需将其传入 `_check_render_pass_contract`），与环检测/可达性保持同一展开视图。
  - 置信度: 中

- [AUD-48004] [Suggestion] “图中没有输出节点”在 export 模式下仍为 WARNING，与模块自身 severity 政策矛盾
  - 位置: src/features/composite/core/graph_preflight.py:210-214
  - 证据:
    ```python
    if nodes and not output_ids:
        report.issues.append(PreflightIssue(
            code="output.missing", severity=SEVERITY_WARNING,
            message="图中没有输出节点，渲染没有输出面。",
            suggestion="添加一个 Output 节点并连接到最终结果。"))
    ```
    模块 docstring 政策（line 19-22）：“error — the render cannot produce a trustworthy result (cycle, Output without input, ...)”；`output.no_input` 即为 ERROR（line 220-225）。
  - 影响: 导出模式下无任何 Output 的图 `report.ok` 为 True，导出流程可继续，而下游（`_resolve_output_resolution` 以 Output 节点为权威）只能靠回退分辨率，可能渲染出非预期结果或空结果。未逐一追踪无 Output 时导出路径的最终行为，故不确定是否构成实际缺陷。
  - 建议: `output.missing` 的 severity 按 `mode == MODE_EXPORT` 提升为 ERROR（与 `node.placeholder`/`output.missing_path` 的模式化 severity 一致），或确认视频模式等无 Output 终端的合法场景后保持现状并加注释。
  - 置信度: 低

- [AUD-48005] [Suggestion] `run_preflight` 的 `graph` 参数无类型注解；`_check_groups.visit` 的 `path` 参数从未被读取
  - 位置: src/features/composite/core/graph_preflight.py:1192（及 753、766）
  - 证据:
    ```python
    def run_preflight(graph, mode: str = MODE_PREVIEW,
                      check_fs: bool = False,
                      ...
    ```
    ```python
    def visit(group_id: str, path: List[str]) -> None:
        state = membership_state.get(group_id, 0)
        ...
            visit(member_id, path + [group_id])
    ```
  - 影响: 全文件其余函数均有完整注解，唯入口函数 `run_preflight` 缺失（接受任意 "live NodeGraph" 却无协议/类型描述）；`path` 参数在每层递归做无意义的列表拼接（分配开销），且其从未被读取说明环路径信息可能本应上报告却被丢弃。
  - 建议: 为 `graph` 补注解（如 `NodeGraph` 或结构化 Protocol）；删除 `path` 参数或在 `group.membership_cycle` 诊断中真正使用路径信息。
  - 置信度: 高

### 通过项
- 线程安全: 纯函数分析服务，无共享可变状态、无 Qt/GL 依赖，符合模块声明
- GPU 资源生命周期: 不创建/持有任何 GL 对象
- GLSL·HDR 正确性: 不涉及着色器；LUT 扩展名白名单（.cube/.3dl）与模型格式 probe 逻辑合理
- 信号防递归: 无信号通路
- 状态提交一致性: 只读分析，不改动图/undo 历史（与 composite_controller 的 view-only 注释一致）
- 异常与取消路径: `probe_import`/`probe_exr_header` 异常均有日志且不中断报告；无取消语义需求（单次纯函数）
- 输入验证: 端口方向/类型、表达式参数/节点引用/循环、Group 成员与映射、Precomp 堆栈环、图层引用/时序/轨道遮罩（含自引用与环）覆盖全面；占位节点按 preview/export 模式分级正确
- 性能模式: Kahn 拓扑 O(V+E) 一次完成；`schema_for` 按节点缓存 schema；`probe_import` 仅扩展名+存在性检查（无重 IO）
- 输出依赖/重名检测、orphan/unreachable 区分逻辑正确

---

## src/features/composite/core/graph_snapshot.py (行数: 184)

### 发现

- [AUD-48006] [Suggestion] 文档声明"Immutable snapshot"的三个 dataclass 均未加 `frozen=True`，跨线程不可变契约无任何强制
  - 位置: src/features/composite/core/graph_snapshot.py:31-32（及 89、98）
  - 证据:
    ```python
    @dataclass
    class NodeSnapshot:
        """Immutable snapshot of a single node's state.
    ```
    ```python
    @dataclass
    class GraphSnapshot:
        ...
        Created on the UI thread under NodeGraph._lock, then passed to the
        render thread for lock-free evaluation.
    ```
  - 影响: UI 线程在移交快照后若原地修改 `parameters`/`port_revisions`/`connections` 等 mutable 字段（或任何持有者误改），渲染线程的无锁读取即出现数据竞争，且不会有任何报错——契约只存在于注释。同项目 `PreflightIssue`（graph_preflight.py:65）已示范 `@dataclass(frozen=True)`。注：frozen 只能防字段重绑定，dict/list 内部仍可变，故需配合构建时深拷贝（`parameters` 注释声称 deep copy，但本文件无法验证调用方）。
  - 建议: 三个快照类加 `frozen=True`（`eq=False` 可选）以强制字段级不可变；或在类 docstring 明示“构建后禁止修改”并由调用方测试覆盖（用 `Mapping`/`Tuple` 类型表达只读意图更佳）。
  - 置信度: 中

### 通过项
- 线程安全: 锁内构建+无锁消费的快照模式本身成立（除 48006 的契约未强制）
- GPU 资源生命周期: 仅记录 `color_texture_id` 等元数据，不持有 GL 对象
- GLSL·HDR 正确性: 不涉及像素/着色器数据
- 信号防递归: 无信号
- 状态提交一致性: revision 体系（param_revision / port_revisions / upstream_port_revisions / color_revision / topology_revision）字段齐备、默认值确定，port 级脏检测语义自洽
- 输入验证: 各 getter 对缺失节点/端口返回 0/[]/None，无 KeyError 路径
- 性能模式: 帮助函数为 O(E) 线性扫描的便捷接口，无逐像素/重复计算
- 类型注解: 完整（含字段语义注释）

---

## src/features/composite/core/image_loader.py (行数: 753)

### 发现

- [AUD-48007] [Critical] `cancel_all` 在 2 秒有界等待后无条件清空 keep-alive 列表，仍在运行的解码线程失去全部引用，可被循环 GC 在运行中销毁 QThread 导致进程崩溃
  - 位置: src/features/composite/core/image_loader.py:692-714
  - 证据:
    ```python
        # Wait outside mutex to avoid deadlock
        for w in to_wait:
            w.wait(2000)  # Max 2s per thread
        self._cancelled_workers.clear()

    def _cancel_worker_unlocked(self, node_id: str):
        """Cancel and remove worker for node_id. Must hold mutex."""
        worker = self._active_workers.pop(node_id, None)
        if worker is not None:
            worker.cancel()
            # Keep reference so GC doesn't collect while still running
            self._cancelled_workers.append(worker)
    ```
  - 影响: `cancel_all` 先把所有 active worker 移入 `_cancelled_workers`（该列表存在的唯一目的就是“防止运行中的线程被 GC”），再对每个最多等 2 秒，然后无条件 `clear()`。解码超过 2 秒（4K 多层 EXR 很常见；`cancel()` 只是置标志，`run()` 中的完整解码照常执行）的 worker 在 `cancel_all` 返回后：不在 `_active_workers`、不在 `_cancelled_workers`、局部 `to_wait` 已随函数返回丢弃——唯一剩余引用是 `worker._on_completed` lambda 默认参数 `w=worker` 形成的 worker↔lambda 引用环，只能被循环 GC 回收；GC 回收运行中的 QThread 包装会触发 Qt 的 "QThread: Destroyed while thread is still running" 致命路径（进程崩溃）。`cancel_all` 在渲染器清理路径（composite_renderer.py:2006）调用，正对应“大图解码进行中关闭/切换合成”的场景。机制由代码完全确定，实际触发依赖解码时长与 GC 时机。
  - 建议: `clear()` 改为只移除 `isFinished()` 的 worker（复用 `_cleanup_finished_workers_unlocked` 的过滤并在锁内执行）；对超时未结束的 worker 记录日志并保留引用（如挂到 loader 属性直至其 `finished`），或先 `wait()` 无界/显著加大超时后再释放。
  - 置信度: 中

- [AUD-48008] [Major] 仅含 Y（亮度）通道的 EXR 解码为全黑图像：读取循环只取 R/G/B/A，缺失通道零填充，Y 数据从未被读出
  - 位置: src/features/composite/core/image_loader.py:439-466（2.x 路径）、317-325（3.x 路径）、447
  - 证据:
    ```python
        # Pick channels to read
        if has_alpha:
            request = ["R", "G", "B", "A"]
            components = 4
        else:
            request = ["R", "G", "B"]
            components = 3
        ...
        for ch_name in request:
            if ch_name in channels:
                raw = exr_file.channel(ch_name, FLOAT)
                arr = np.frombuffer(raw, dtype=np.float32).reshape(height, width)
            else:
                # Missing channel — fill with defaults
                if ch_name == "A":
                    arr = np.ones((height, width), dtype=np.float32)
                else:
                    arr = np.zeros((height, width), dtype=np.float32)
    ```
    而代码明知 Y 通道存在（line 447）却只用于位深推断：
    ```python
        ch_type = header["channels"].get("R", header["channels"].get("Y"))
    ```
    3.x 路径 `_decode_exr3_part`（line 317-325）同样只请求 R/G/B(/A)，`_pixels` 返回 None 时零填充。
  - 影响: 灰度 matte/遮罩类 Y-only EXR（OpenEXR 合法且常见）被静默解码成 components=3 的全零图像——无任何报错，渲染输出为黑图（错误的渲染结果）。这与通用 cv2 路径形成不一致：后者支持单通道（line 183-185 `img[:, :, np.newaxis]`，`DecodedImage.components` 注释也声明支持 1）。
  - 建议: 读通道时若 R/G/B 缺失而存在 "Y"（或 luminance 命名），用 Y 数据广播填充三个通道（或按单通道 `components=1` 返回，与 cv2 路径对齐）；两代绑定路径同步修复。
  - 置信度: 高

- [AUD-48009] [Suggestion] `cancel_all` 在互斥锁外执行 `self._cancelled_workers.clear()`，违反“所有公共方法线程安全”的类契约
  - 位置: src/features/composite/core/image_loader.py:701-706
  - 证据:
    ```python
        finally:
            self._mutex.unlock()

        # Wait outside mutex to avoid deadlock
        for w in to_wait:
            w.wait(2000)  # Max 2s per thread
        self._cancelled_workers.clear()
    ```
    类 docstring（line 621-623）: "Thread safety: all public methods are safe to call from any thread."
  - 影响: 等待期间另一线程调用 `request_decode` → `_cancel_worker_unlocked` 会向 `_cancelled_workers` 追加新 worker，随后被无锁 `clear()` 一并移除（该 worker 既不在 active 也不在 cancelled 列表，复现 48007 的失引用问题）；与 `_cleanup_finished_workers_unlocked` 的锁内重赋值也存在竞态。当前唯一使用方（渲染线程）单线程串行调用，实际触发需要未来出现跨线程调用者。
  - 建议: 等待结束后重新加锁再清理（只清除已结束的 worker），全程不裸操作共享列表。
  - 置信度: 中

- [AUD-48010] [Suggestion] 被取消的 worker 永不触发 `_cleanup_finished_workers_unlocked`，已结束的 QThread 对象在会话内累积；且并发解码线程数无上限
  - 位置: src/features/composite/core/image_loader.py:716-720（及 116-117、656-680）
  - 证据:
    ```python
    def _cleanup_finished_workers_unlocked(self):
        """Remove cancelled workers that have finished."""
        self._cancelled_workers = [
            w for w in self._cancelled_workers if w.isRunning()
        ]
    ```
    清理仅由 `_on_decode_completed`/`_on_decode_failed` 触发，而被取消的 worker 在交付前提前返回:
    ```python
            if self._cancelled:
                return
    ```
  - 影响: 用户连续改路径/EXR part 选择时，每个被取消的 worker 都完整跑完解码但不交付（也不触发清理），其 QThread 对象滞留 `_cancelled_workers` 直到后续某次成功/失败交付才被过滤；同时 `request_decode` 对并发 worker 数量无上限（每节点一个），N 个 ImageInput 节点首轮求值即 N 路全分辨率并发解码，4K float32 EXR 每路 100MB+，存在内存峰值风险。
  - 建议: 在 `request_decode`/`cancel_for_node` 末尾也调用一次 `_cleanup_finished_workers_unlocked`（或定期清理）；考虑加一个小型并发上限（如信号量）排队解码请求。
  - 置信度: 中

- [AUD-48011] [Suggestion] `np.frombuffer` 产生的只读数组经 `ascontiguousarray` 不会被复制，`depth_data`/`extra_channels` 以只读视图形式逃逸出公共 API
  - 位置: src/features/composite/core/image_loader.py:475-477（及 495-498）
  - 证据:
    ```python
                raw_depth = exr_file.channel(depth_channel_name, FLOAT)
                depth_data = np.frombuffer(raw_depth, dtype=np.float32).reshape(height, width)
                depth_data = np.ascontiguousarray(depth_data)
    ```
  - 影响: OpenEXR 绑定返回不可变 `bytes` 时，`np.frombuffer` 的结果 `writeable=False`；`ascontiguousarray` 对已连续的数组原样返回，不产生可写副本。下游若对 `DecodedImage.depth_data`/`extra_channels` 做原地写（如深度归一化）会抛 "read-only" ValueError。未排查所有消费者，实际是否触发未证实。
  - 建议: 改用 `np.array(np.frombuffer(...), dtype=np.float32)` 或 `np.frombuffer(...).copy()` 明确产出可写数组，与 `data` 主通道（`np.stack` 天然可写）保持一致。
  - 置信度: 低

- [AUD-48012] [Suggestion] 容器字段/属性类型注解未参数化（`list`、`dict` 裸类型）
  - 位置: src/features/composite/core/image_loader.py:62-64（及 631-632）
  - 证据:
    ```python
        exr_part: str = ""        # part name this image came from ("" = single part)
        exr_parts: list = field(default_factory=list)   # all part names in the file
        exr_channels: list = field(default_factory=list)  # channels in the selected part
        extra_channels: dict = field(default_factory=dict)  # name -> float32 (h, w)
    ```
    ```python
        self._active_workers: dict = {}  # node_id -> ImageDecodeWorker
    ```
  - 影响: 语义只存在于注释，静态检查（mypy/pyright）无法验证元素类型与用法一致，与同文件其余完整注解风格不统一。
  - 建议: 改为 `List[str]`、`Dict[str, np.ndarray]`、`Dict[str, ImageDecodeWorker]`。
  - 置信度: 高

### 通过项
- GLSL·HDR 正确性: LDR 按位深正确归一化（uint8/255.0、uint16/65535.0），EXR/HDR/DPX 保持原生线性范围、无 clamp(0,1)（符合 HDR 铁律）；EXR 强制 "linear"、扩展名驱动的 color_space_hint 与 _LINEAR_EXTENSIONS 一致
- 信号防递归: worker 的 Qt 信号仅作通知，请求路径全部走直连回调+命令队列，无信号回流回路
- 输入验证: 空路径仅取消不解码；`os.path.isfile` 前置；mtime 参与新鲜度；EXR part/extra_channels 选择纳入身份式新鲜度判定（`_on_decode_completed` 的 worker 身份比对修复了同文件不同 part 的陈旧交付）
- 异常与取消路径: `decode_image_file` 的 IO 异常由 `run()` 捕获并经 `decode_failed` 带错误串交付；取消后不交付；3.x 绑定缺 MultiPartInputFile 时正确分派 `_decode_exr3_part` 防 InputFile 静默读 part 0
- 线程安全（除 48007/48009）: 回调在 `start()` 前于互斥锁内安装；交付先在锁内做身份判定再锁外交付，无死锁路径（worker 回调不会反向等待 loader 锁的持有者）
- 状态提交一致性: 无持久化职责；request_id/keep-alive 语义在注释中明确

---

## src/features/composite/core/layer_model.py (行数: 321)

### 发现

- [AUD-48013] [Suggestion] `Layer.from_dict` 验证缺口：布尔字段未强转、`in_frame`/`out_frame` 未做类型检查、`layer_type` 未对照 `ALL_LAYER_TYPES` 校验
  - 位置: src/features/composite/core/layer_model.py:203-218
  - 证据:
    ```python
            enabled=data.get("enabled", True),
            solo=data.get("solo", False),
            locked=data.get("locked", False),
            shy=data.get("shy", False),
    ```
    ```python
            in_frame=data.get("in_frame"),
            out_frame=data.get("out_frame"),
            layer_type=data.get("layer_type", LAYER_TYPE_MEDIA),
            managed=data.get("managed", True),
            collapsed=data.get("collapsed", True),
    ```
    对比同函数中已验证的字段（line 185-197 opacity clamp、blend_mode/track_matte_mode 白名单、line 213 `track_matte_invert=bool(...)`)。
  - 影响: 手工编辑/旧版导出的工程文件里 `"enabled": "false"`（非空字符串为真值）会把隐藏图层加载为可见；`"in_frame": "12"` 之类的字符串会原样进入 `NodeSnapshot.layer_frame_gates` 的时间门（预检 `_check_layers` 的 `isinstance(in_f, int)` 只会静默跳过时序校验）；拼错的 `layer_type` 被静默持久化，UI 分类逻辑失配。
  - 建议: 布尔字段统一 `bool(data.get(...))`；in/out_frame 用 `_safe_int` 风格强制 Optional[int]（非法值回退 None）；`layer_type` 不在 `ALL_LAYER_TYPES` 时回退 `LAYER_TYPE_MEDIA` 或标记为 external。
  - 置信度: 高

### 通过项
- 线程安全/GPU 资源生命周期/GLSL·HDR 正确性/信号防递归: 被动数据类，无相关接触面（符合模块 §3 "data classes here are passive" 约定）
- 状态提交一致性: `to_dict`/`from_dict` 字段完全对称；未知键忽略、缺省回退的前向兼容策略与注释一致；opacity 在数据边界 clamp 属标量 UI 值（非 HDR 像素数据），不违反 HDR 规则；`_safe_int` 保护 version
- 输入验证（除 48013）: opacity/blend_mode/track_matte_mode 三处核心验证正确
- 性能模式: `LayerMap` O(1) 查找、`copy()` 深拷贝隔离，无隐蔽共享
- 类型注解: 除 `__iter__` 缺返回注解外完整；`LAYER_BLEND_MODE_TO_NODE` 12 个目标 token 已对照 blend_node.py `BLEND_MODES` 全部核实存在（含 over/min/max/color-dodge/color-burn/soft-light/hard-light）

---

## src/features/composite/core/layer_provenance.py (行数: 229)

### 发现

- [AUD-48014] [Suggestion] `for_node` 对每个图层重复全量溯源（每次 `_trace_chain` 重建全图 incoming 表），整图逐节点查询放大为 O(N·L·E)
  - 位置: src/features/composite/core/layer_provenance.py:118-121（及 184-187）
  - 证据:
    ```python
            for layer_id in getattr(stack, "layer_ids", []) or []:
                provenance = self.for_layer(str(layer_id), str(stack.stack_id))
                if provenance is None or target not in provenance.chain_node_ids:
                    continue
    ```
    ```python
        incoming: Dict[str, List[str]] = {}
        for connection in getattr(self.graph, "connections", []) or []:
            incoming.setdefault(str(connection.dst_node_id), []).append(
                str(connection.src_node_id))
    ```
  - 影响: `for_layer` 每次调用都遍历全部 connections 重建邻接表并重走该层链；`for_node` 对堆栈内每层各调一次。调用方若对图中每个节点问询（如着色/上下文菜单），复杂度为 O(节点数 × 图层数 × (边数+链长))，大合成（数百节点/边）下明显卡顿。
  - 建议: 在 `for_node` 入口按 stack 预构建一次 incoming 索引并逐层复用；或提供一次性构建的 node→layers 反查缓存（provenance 本身已 immutable，适合缓存）。
  - 置信度: 中

- [AUD-48015] [Suggestion] `_trace_chain.visit` 为无界递归，病态长链会触发 RecursionError
  - 位置: src/features/composite/core/layer_provenance.py:192-207
  - 证据:
    ```python
        def visit(node_id: str) -> None:
            if node_id in visited or node_id in stop_ids:
                return
            node = self.graph.get_node(node_id)
            if node is None:
                return
            visited.add(node_id)
            result.append(node_id)
            ...
            for upstream_id in incoming.get(node_id, []):
                visit(upstream_id)
    ```
  - 影响: 链长超过解释器递归上限（默认约 1000）时抛 RecursionError。正常特效链远短于此，但自由图模式下手工长链（或 precomp 合并后的深链）无环检查兜底（本函数有 visited 防环，仅深度受限于栈）。同文件其他 visit 系（group/precomp/matte）深度受嵌套层级约束，无此风险。
  - 建议: 改为显式栈的迭代遍历（保持 root-first/source-last 的 DFS 前序语义）。
  - 置信度: 低

### 通过项
- 线程安全: 无状态解析器（仅持 graph 引用），只读不锁定
- GPU 资源生命周期/GLSL·HDR 正确性/信号防递归: 无相关接触面
- 状态提交一致性: 只读桥接，不触碰 undo/持久化；generated Merge 显式标 read_only=True 防误改
- 输入验证: 空 node_id 短路；layer/stack 缺失返回 None/[]；对 graph 属性全部 getattr 防御式访问；`_stack_has_conflict` 与预检 `layer.managed_chain_conflict` 判定条件一致（roots ∪ generated 为白名单、仅 A/B 口）
- 类型注解: 完整
