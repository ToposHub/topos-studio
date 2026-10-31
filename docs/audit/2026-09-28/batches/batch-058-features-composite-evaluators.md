# batch-058 审查报告 — src/features/composite/evaluators/

- 批次：batch-058 [P1]
- 编号区间：AUD-5800–AUD-5899
- 审查日期：2026-09-28
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解

---

## src/features/composite/evaluators/base_evaluator.py (行数: 3444)

### 发现

- [AUD-5801] [Major] `_apply_fused_chain` 输出 TextureResult 未传播上游 revision，池 glo 回收后可致下游缓存键陈旧命中
  - 位置: src/features/composite/evaluators/base_evaluator.py:3417
  - 证据:
    ```python
    return {
        out_name: TextureResult(
            texture_id=out_tex.glo, width=w, height=h,
            format=input_tex.format,
            bbox=input_tex.bbox,
            alpha_premultiplied=alpha_premultiplied,
            color_space=color_space,
        )
    }
    ```
    对比 `_convert_to_linear`（行 2822）显式 `revision=tex.revision` 并注释："IDT 结果若固定 revision=0，下游缓存键永不变化 → 逐帧 cache hit → 播放画面冻结在首帧"。`TextureResult.revision` 默认 0（base_node.py:109）；CacheManager 键含上游 (id, revision)（cache_manager.py:196-198）。
  - 影响: 融合链缓存条目被驱逐、其纹理归还池后，同尺寸再次 acquire 大概率复用同一 glo；revision 恒为 0 时，下游节点跨驱逐周期可能命中内容不同的旧条目（陈旧帧/冻结画面）。文件自身已确认此类缺陷机制（composite_renderer.py:871-876 "bind a recycled GL name"）。
  - 建议: 返回结果携带 `revision=input_tex.revision`（或对每次融合执行使用单调递增的会话级 revision 计数）。
  - 置信度: 中（机制在代码中确证；触发依赖池 glo 复用与条目驱逐时序）

- [AUD-5802] [Major] `_apply_merge_blend` 渲染后未解绑纹理单元 0，违背本文件 P1-6 绑定纪律
  - 位置: src/features/composite/evaluators/base_evaluator.py:2419
  - 证据:
    ```python
    self._render_quad(prog)

    if has_mask:
        bind_texture_unit(0, 2)
    bind_texture_unit(0, 1)
    ```
    而输入 A 在行 2299 通过 `self._bind_external_texture(input_a.texture_id)` 绑定到单元 0。同文件其余路径均显式解绑单元 0（行 1783-1785、1911-1912、1950-1952、2809-2810、3407-3409，注释 "P1-6: … never leave unit 0 bound"）。
  - 影响: input_a 为池化纹理，可能在下游合成期间被释放/回收；单元 0 残留绑定旧 GL 名，后续未显式重绑单元 0 的绘制将采样已回收内容（绿帧/花屏，项目已按 P1-6 修复过同类缺陷）。
  - 建议: 渲染后追加 `self._unbind_texture_unit(0)`，与其余效果路径保持一致。
  - 置信度: 高

- [AUD-5803] [Major] merge 前自动色彩空间对齐分配的转换纹理无所有权归属，依赖池预算驱逐兜底
  - 位置: src/features/composite/evaluators/base_evaluator.py:2281
  - 证据:
    ```python
    # S2-D: Auto-align color spaces before merge
    input_a, input_b = self._auto_align_color_spaces(input_a, input_b)
    ```
    `_convert_to_linear`（行 2771）`out_tex = self._resource_pool.acquire_texture(w, h)` 分配新纹理；而求值器所有权收集（行 970）`owned_ids = self._collect_owned_texture_ids(ns, outputs, working_inputs)` 的 upstream_ids 来自**原始** working_inputs——转换后的 A'/B' 既不在 working_inputs 也不在 outputs（merge 在 CACHE_OWNED_TYPES 中，owns_color_texture=True，node_capabilities.py:305-313）。
  - 影响: 每次触发色彩空间转换的 merge 求值泄漏 1–2 张全分辨率池纹理，无人释放，直到 GPUResourcePool 预算驱逐；与文件行 899-903 注释描述并已为 IDT 修复的泄漏类（pre_source_texture_ids 差集）完全同型，此处未修。
  - 建议: 将转换纹理 ID 并入该节点缓存条目的 owned_texture_ids，或加入 `_transient_texture_ids_to_release`。
  - 置信度: 高

- [AUD-5804] [Major] `_upload_ai_result_texture` 结果纹理 revision 固定为 1，未随输入内容演进
  - 位置: src/features/composite/evaluators/base_evaluator.py:1591
  - 证据:
    ```python
    return TextureResult(
        texture_id=tex.glo,
        width=w,
        height=h,
        revision=1,
        alpha_premultiplied=True,
    )
    ```
  - 影响: 与 AUD-5801 同型的键混叠：AI 节点旧缓存条目驱逐、glo 归还池后被新结果复用时，下游 (glo, revision=1) 键与旧条目相同 → 陈旧命中，AI 合成结果不更新。
  - 建议: 传播 `reference.revision`（或对每次上传使用单调递增 revision）。
  - 置信度: 中

- [AUD-5805] [Major] `_readback_texture_fullres` 对 HDR 线性数据无条件 clamp(0,1)，合成回写丢失高光
  - 位置: src/features/composite/evaluators/base_evaluator.py:1573
  - 证据:
    ```python
    arr = np.flipud(arr[:, :, :3]).copy()
    if want_bgr:
        arr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    return np.clip(arr, 0.0, 1.0)
    ```
    管线为 HDR float（行 2883-2886：PQ 解码后 "Linear result is in nits (0..10000) — HDR working space, unclamped"）。
  - 影响: AI 掩码区域内 >1.0 的 HDR 值在合成结果（`image_np * (1.0 - m) + cached * m`，行 1546）中被压平——掩码内高光数据丢失，违反"HDR 内部数据不得无理由 clamp"铁律。AI 模型输入域 [0,1] 是理由，但合成回写路径使用了同一 clamp 后的数据。`_read_ai_cache_result` 行 1534-1535 的 `np.clip(..., 0.0, 1.0)` 同理。
  - 建议: 仅在张量送入模型前 clamp；掩码合成使用未 clamp 的线性读回值（缓存写读可保留各自域并显式注释理由）。
  - 置信度: 中

- [AUD-5806] [Major] 节点求值异常路径无纹理回滚，且警告日志不带堆栈
  - 位置: src/features/composite/evaluators/base_evaluator.py:1034
  - 证据:
    ```python
    except Exception as e:
        node_ms = (time.perf_counter() - node_start) * 1000.0
        if perf:
            perf.record_node_eval(node_id, np.node_type, node_ms, False, True, str(e))
        logger.warning(f"Error evaluating {node_id} ({np.action}): {e}")
        cache[node_id] = {}
    ```
    try 块内 `_evaluate_node`/tile 裁剪/IDT 已从池中 acquire 的纹理在异常时无任何释放；文件顶部 `import traceback`（行 13）从未使用。
  - 影响: 持续出错的节点（如 uniform 类型错误、AUD-5811 的 TypeError）每帧泄漏其已分配纹理直至预算驱逐；`logger.warning` 丢失堆栈，违背"不得宽泛 except 掩盖领域错误"的可诊断性要求。
  - 建议: except 中释放本节点本次求值新分配的池纹理（记录差集），并改用 `logger.exception(...)`。
  - 置信度: 中

- [AUD-5807] [Suggestion] 陈旧请求取消路径的纹理释放循环是死代码
  - 位置: src/features/composite/evaluators/base_evaluator.py:697
  - 证据:
    ```python
    if self._scheduler.should_cancel_evaluation(request_id):
        for tex in cache.get(node_id, {}).values():
            if isinstance(tex, TextureResult) and tex.texture_id > 0:
                if tex.texture_id not in self._get_source_texture_ids():
                    self._resource_pool.release_texture_by_id(tex.texture_id)
    ```
    该检查位于 node_id 处理起点，`cache[node_id]` 尚未写入（eval_order 无重复），循环体恒为空。
  - 影响: 无实际释放发生（外层 `_release_transient_textures` 兜底，故无功能损坏），但误导维护者以为此处有清理。
  - 建议: 删除该循环，或改为遍历本轮 `cache` 中已求值节点的临时纹理。
  - 置信度: 高

- [AUD-5808] [Suggestion] dirty-skip 分支设置 output_result 未校验 `_is_selected_output`，与 cache-hit 分支不对称
  - 位置: src/features/composite/evaluators/base_evaluator.py:690
  - 证据:
    ```python
    if is_output_node_type(np.node_type):
        output_result = cached_outputs.get("display")
    ```
    而 cache-hit 路径（行 844-845）为 `if is_output_node_type(np.node_type) and _is_selected_output(ns):`。
  - 影响: 在 `selected_dependencies` 为 None 的回退场景（输出目标缺失，行 499-503）且图中存在多个 output 节点时，后求值的非选定 output 节点可覆盖 viewer 拿到的 display，呈现错误输出。
  - 建议: dirty-skip 分支同样加 `_is_selected_output(ns)` 判断。
  - 置信度: 中

- [AUD-5809] [Suggestion] 未用导入与死代码：math/traceback/GPUResourcePool/RenderScheduler/NodePlan、两个未接线局部变量、无调用的 `_first_output_name`
  - 位置: src/features/composite/evaluators/base_evaluator.py:11
  - 证据:
    ```python
    import math          # 全文件仅出现在注释中
    import traceback     # 唯一一次出现即本行
    ...
    from ..render.gpu_resource_pool import GPUResourcePool  # 仅类型导入未使用
    from ..render.render_scheduler import (
        DYNAMIC_SOURCE_NODE_TYPES,
        RenderScheduler, RenderPlan, NodePlan,   # RenderScheduler/NodePlan 未使用
    )
    ```
    行 635-636 `evaluated_port_revisions: Dict[str, Dict[str, int]] = {}` / `evaluated_upstream_revisions: ...` 声明后全函数从未读写；行 3159 `_first_output_name` 全仓库无调用（`_first_image_port` 已取代之）。
  - 影响: 误导审阅者以为存在相应数据流；`_motion_blur_sub_frames` 同类问题见 AUD-5812。
  - 建议: 删除未用导入与死变量/死方法，或接线 `evaluated_*`（若端口修订跟踪原计划走本地收集）。
  - 置信度: 高

- [AUD-5810] [Suggestion] 跨模块访问他人私有属性（fb_manager/_resource_pool/perf/scheduler 内部结构）
  - 位置: src/features/composite/evaluators/base_evaluator.py:3019
  - 证据:
    ```python
    if hasattr(self.fb_manager, '_active_textures'):
        for tex in self.fb_manager._active_textures:
            if tex.glo == texture_id:
                return tex
    if hasattr(self._resource_pool, '_pooled_textures'):
        for tex_list in self._resource_pool._pooled_textures.values():
    ```
    另：行 1088 `perf._node_eval_records`、行 1495 `getattr(scheduler, '_latest_request_id', 0)`。
  - 影响: 对被访模块内部结构的强耦合，重命名/重构即静默失效（hasattr 掩盖）；`_get_texture_by_id` 线性扫描在多纹理时为 O(n)。
  - 建议: 由 fb_manager/resource_pool 提供 `get_texture_by_id()` 公共 API；perf/scheduler 暴露只读访问器。
  - 置信度: 高

- [AUD-5811] [Suggestion] `_create_ephemeral_node` 时间范围门控未做 int 容错，与源节点门控路径不一致
  - 位置: src/features/composite/evaluators/base_evaluator.py:3085
  - 证据:
    ```python
    if (not is_output_node_type(ns.node_type) and ns.time_range
            and ns.time_range.get("enabled", True)):
        cur = frame_number
        start = ns.time_range.get("comp_start", cur)
        end = ns.time_range.get("comp_end", cur)
        if cur < start or cur > end:
            node.disabled = True
    ```
    对比 `_node_time_range_active`（行 121-129）对 comp_start/comp_end 有 `try: int(...) except (TypeError, ValueError)` 容错。
  - 影响: 旧工程/损坏数据中 comp_start 为字符串时抛 TypeError，节点落入行 1034 宽泛 except 被置空输出（节点消失），而同类源节点路径可容错——外部数据须验证铁律未一致执行。
  - 建议: 复用 `_node_time_range_active(ns, frame_number)` 做该门控。
  - 置信度: 高

- [AUD-5812] [Suggestion] `_motion_blur_sub_frames` 中 `if n == 1` 为不可达分支，且 samples=1 的用户意图被静默改写
  - 位置: src/features/composite/evaluators/base_evaluator.py:1810
  - 证据:
    ```python
    n = max(2, int(samples))
    base = float(frame) + float(offset)
    if n == 1:
        return [base]
    ```
  - 影响: 死代码；调用方（行 1845 `samples = max(2, min(16, samples))`）请求 samples=1 时实际渲染 2 个相同采样，浪费一次全画布 GPU pass。
  - 建议: 若需支持单采样应在 `max` 之前判断；否则删除该分支并 clamp 注释说明下限为 2。
  - 置信度: 高

- [AUD-5813] [Suggestion] 多个方法缺少参数/返回类型注解
  - 位置: src/features/composite/evaluators/base_evaluator.py:3185
  - 证据:
    ```python
    def _execute_fused_chain(self, chain, snapshot, cache, plan,
                             render_mode, cache_mgr, request_id):
    ```
    同类：`_fallback_individual_eval`（行 3273）、`_render_quad(self, program)`（行 3038）、`_get_white_mask_texture`（行 2994）、`_collect_scene_input_sig(self, node) -> str` 的 node 参数（行 1194）、`_apply_idt_to_source_outputs(outputs: dict) -> dict`（行 2827，缺 value 类型）、`_load_shaders(self)`（行 3429）。
  - 影响: 违反项目强类型约定，混合注入的协作面难以静态检查。
  - 建议: 按 .clinerules 补全类型注解（chain: FusedChain、snapshot: GraphSnapshot、cache: Dict[str, Dict[str, TextureResult]] 等）。
  - 置信度: 高

- [AUD-5814] [Suggestion] deprecated color_input 节点在每次快照同步都打 info 日志
  - 位置: src/features/composite/evaluators/base_evaluator.py:343
  - 证据:
    ```python
    if ns.node_type == "color_input":
        logger.info(
            "Deprecated color_input node detected in graph. "
            "Consider migrating to ToposColorNode."
        )
    ```
    `_sync_render_nodes` 随每次求值执行。
  - 影响: 含遗留 color_input 的工程在播放/交互期间每帧每节点产生一条 INFO 日志——日志洪水与 I/O 开销，淹没有价值的 [Render] 诊断。
  - 建议: 用实例级 set 记录已告警的 node_id，每会话仅提示一次并降为 warning。
  - 置信度: 高

- [AUD-5815] [Suggestion] 求值循环直接改写"不可变"快照节点的 port_revisions
  - 位置: src/features/composite/evaluators/base_evaluator.py:1052
  - 证据:
    ```python
    for port_name, tex_result in outputs.items():
        if isinstance(tex_result, TextureResult) and tex_result.is_valid:
            current_rev = ns.port_revisions.get(port_name, 0)
            ns.port_revisions[port_name] = current_rev + 1
    ```
    NodeSnapshot 文档声明 "Immutable snapshot of a single node's state"（graph_snapshot.py:33）。
  - 影响: 契约与实践不一致：正确性依赖"每个快照仅被一个求值器独占"的隐式纪律；若同一快照对象被两条求值路径复用（如预览+导出共享请求快照），修订计数相互覆盖。配套局部变量 `evaluated_port_revisions` 未接线（AUD-5809）表明该数据流未完成。
  - 建议: 将端口修订写回本地收集结构（evaluated_*）或快照附带的 eval-side 容器，保持 NodeSnapshot 只读。
  - 置信度: 中

### 通过项
- 线程安全：整个 mixin 在渲染线程执行，无 UI 线程 GL 调用；scene payload/ID picking/正交纹理旁路每帧清空重建（行 528-579），eval_ctx 逐帧同步且无跨线程共享可变状态；快照由 UI 线程持锁深拷贝交付（node_engine.snapshot 行 1678-1700）
- 信号防递归：文件内无 Qt 信号/控件操作，不适用（纯求值层）
- GLSL·HDR 正确性（IDT 主路径）：sRGB/BT.709/PQ/HLG/ACEScg 传输与初基正确区分（行 2865-2917）；PQ 解码结果保持 nits 域不 clamp；`_metadata_get` 正确处理 dict/对象双形态元数据；浮点字面量规范
- 状态提交一致性：fps=0 哨兵防回写（行 376-385）；`_cache_invalidated_needed` 触发含下游消费者的级联缓存失效防死亡 glo 复用（行 988-1002）；磁盘缓存 opt-in 读写对称且与内存缓存互不重复计数（行 851-881、1017-1025）
- 异常与取消路径：融合链失败降级为逐节点求值且释放差集纹理（行 3229-3250）；IDT 转换失败透传并记日志不杀整帧（行 2922-2934）；陈旧请求及时 return None 且外层 `_release_transient_textures` 全路径兜底（composite_renderer.py:877/884/929/1078）；AI_RESULT_READY 经 `_clear_eval_cache()` 强制重求值（composite_renderer.py:1653-1660）
- 输入验证：`_node_time_range_active`/`_source_gate_frame` 对损坏 time_range/override 的 int 容错（行 121-146）；关键帧损坏轨道跳过不中断整节点（行 3115-3155）；mask 尺寸不匹配重采样而非广播（行 1440-1446）
- 性能模式：内容寻址缓存 + pin 保护帧内纹理；dirty-skip/磁盘缓存/AI 零回读（P2-9）；LOD 上采样仅按 estimated_bbox 触发；融合链保守门控与失败回退
- GPU 资源生命周期（多数路径）：tile/ROI/IDT 源节点转换纹理经 pre_source/pre_idt 差集归入条目所有权（行 904-944、971-1012）；运动模糊采样/累加器在 finally 中释放且所有权转移显式（行 1931-1938）；绘制后解绑纹理单元纪律（除 AUD-5802 外）；白掩码纹理 1×1 常驻缓存避免池零纹理黑掩码缺陷
