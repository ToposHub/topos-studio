# batch-082 — src/features/composite/render//（AUD-8200–AUD-8299）

审查人：子智能体（全库审查 2026-09-28）
范围：`src/features/composite/render/composite_renderer.py`（2,200 行）、`src/features/composite/render/compute_shader_manager.py`（679 行）
方法：两文件全文通读；跨文件核对 `base_evaluator.py`、`render_scheduler.py`、`graph_snapshot.py`、`shaders.py`、`blur_evaluator.py`、`image_loader.py`、`composite_page.py` 以验证每条发现。

---

## src/features/composite/render/composite_renderer.py (行数: 2200)

### 发现

- [AUD-8201] [Major] AI 结果与 3D 视图快速路径渲染后缺失 `_release_transient_textures`，瞬态纹理延迟归还资源池
  - 位置: src/features/composite/render/composite_renderer.py:1661-1664（同类问题 :648）
  - 证据:
    ```python
    result = self._evaluate_snapshot()
    if result and result.is_valid:
        self._render_to_output(result, request_id=self._scheduler._latest_request_id)
    else:
        self.frame_ready.emit(self._scheduler._latest_request_id, 0, 0, 0)
    ```
    对比 `_handle_evaluate_graph`（:881-884）与播放驱动循环（:1075-1078）均为 `try: self._render_to_output(...) finally: self._release_transient_textures()`；`_handle_render_3d_view` 成功路径（:647-648）同样只调 `_render_to_output` 后直接更新签名字段。`_transient_texture_ids_to_release`（base_evaluator.py:632）仅在 `_release_transient_textures` 内部清空（base_evaluator.py:1243），不会在下次求值开始时重置。
  - 影响: AI_RESULT_READY 连续到达（无完整 EVALUATE_GRAPH 穿插）或长时间纯相机导航期间，瞬态纹理一直滞留在资源池中被持有，GPU 内存持续占用；4K RGBA16F 单张即数十 MB。
  - 建议: 两个路径均改为 `try: self._render_to_output(...) finally: self._release_transient_textures()`，与其余调用点对齐。
  - 置信度: 高（代码不一致确凿；实际增长幅度取决于瞬态集合内容，为中）

- [AUD-8202] [Major] PREFETCH_EVALUATE 处理器为可达的空实现，预取功能静默失效
  - 位置: src/features/composite/render/composite_renderer.py:2078-2195（核心空转段 :2164-2188）
  - 证据:
    ```python
    for node_id in target_node_ids:
        node_snap = modified_nodes.get(node_id) or snapshot.nodes.get(node_id)
        if node_snap is None:
            continue
        inputs = {}
        for conn in snapshot.get_input_connections(node_id):
            ...
            if upstream_snap:
                # For now, we skip actual texture fetching in prefetch
                pass
        # Note: This is a simplified implementation
    # For a full implementation, we would call evaluate_graph ...
    ```
    该命令确有真实提交方：`src/features/composite/ui/composite_page.py:7098` `self._renderer.submit(CompositeCommand.PREFETCH_EVALUATE, {...})`。处理器的 `original_category`、`inputs` 等局部量构造后从未使用，最终没有任何求值或缓存写入。
  - 影响: UI 认为已提交预取工作并可能据此清除预测（composite_page.py:6859 提交 CLEAR_PREFETCH_CACHE），但渲染端从不产生 PREFETCH 缓存条目——预取功能整体无效，且死代码掩盖了这一点。
  - 建议: 要么实现对修改快照的真实下游求值并写入 `CacheCategory.PREFETCH`，要么在处理器内显式记录"未实现"并移除 UI 侧提交，避免静默失效。
  - 置信度: 高（代码与提交点均已核实；属功能缺陷而非崩溃风险）

- [AUD-8203] [Suggestion] 缩略图回读异常路径泄漏 `thumb_tex`
  - 位置: src/features/composite/render/composite_renderer.py:1625（acquire）、:1645（仅成功路径 release）、:1649-1651（except 直接 return）
  - 证据:
    ```python
    thumb_tex = self._resource_pool.acquire_texture(thumb_w, thumb_h)
    ...
    self._bind_external_texture(tex_id)
    prog['u_texture'].value = 0
    self._render_quad(prog)
    ...
    self._resource_pool.release_texture(thumb_tex)
    return bgr
    except Exception as e:
        logger.warning(f"Texture thumbnail readback error: {e}")
        return None
    ```
  - 影响: `_bind_external_texture`/`_render_quad`/`fbo.read` 任一步抛异常时，池纹理不被归还，且该路径按节点/按参数编辑反复触发时可累积。
  - 建议: 用 `try/finally` 包裹，确保 `release_texture(thumb_tex)` 在异常路径也执行。
  - 置信度: 高

- [AUD-8204] [Suggestion] 播放驱动循环结束时用 `print` 向 stdout 倾倒统计
  - 位置: src/features/composite/render/composite_renderer.py:1100-1114
  - 证据:
    ```python
    finally:
        n = max(drive_stats["frames"], 1)
        print(
            "[DRIVE] frames=%d eval=%.1fms wait=%.1fms render=%.1fms "
            "sleep=%.1fms scope=%.1fms retries=%d (per-frame avg: ..." % (
    ```
  - 影响: 每次自驱播放（含一次 loop 回绕会话）结束都无条件污染 stdout，绕过日志级别控制；发布版控制台/重定向日志混入调试噪音。
  - 建议: 改为 `logger.debug(...)` 或受调试开关控制。
  - 置信度: 高

- [AUD-8205] [Suggestion] `_drive_stopped` 检查本类从不存在的 `_stop_flag`，防线实际为空
  - 位置: src/features/composite/render/composite_renderer.py:973-981
  - 证据:
    ```python
    if getattr(self, '_stop_flag', False) or not getattr(self, '_initialized', True):
        return True
    ```
    全仓 grep：`_stop_flag` 仅在 `audio_playback.py`、`timeline_preview_renderer.py`、`video_playback.py` 中赋值；`CompositeRenderer` 从未设置该属性，`getattr(..., False)` 恒为 False（`is_running=False` 不被此函数检查）。
  - 影响: 当前依赖 `stop_thread()` 同时递增 `_drive_generation` 才能退出；若未来某停止路径只置 `is_running=False`（或仅排 CLEANUP，而 CLEANUP 在驱动循环独占期间不会被消费），循环不会感知，形成只能靠 supersede 退出的隐患。属复制粘贴遗留。
  - 建议: 改为 `if not self.is_running: return True`（读 bool 原子，无锁安全）。
  - 置信度: 高（死检查确凿；现实触发路径目前被 generation 递增覆盖）

- [AUD-8206] [Suggestion] 直接访问调度器私有 `_latest_request_id` 且绕过其锁
  - 位置: src/features/composite/render/composite_renderer.py:1662、:1664
  - 证据:
    ```python
    self._render_to_output(result, request_id=self._scheduler._latest_request_id)
    else:
        self.frame_ready.emit(self._scheduler._latest_request_id, 0, 0, 0)
    ```
    `RenderScheduler` 内部以 `_id_lock` 保护 `_latest_request_id`（render_scheduler.py:497、:1282-1283），并提供 `mark_current/is_stale` 等加锁访问器。
  - 影响: 破坏封装；UI 线程 `submit()`→`advance_request_id` 与此处无锁读并发（CPython 下 int 读原子，实际风险低，但与调度器的加锁约定不一致）。
  - 建议: 在 RenderScheduler 上提供加锁的 `latest_request_id` 属性并使用之。
  - 置信度: 高（代码事实）；功能风险低

- [AUD-8207] [Suggestion] 播放驱动循环直接变异"不可变"快照的 `frame_number`
  - 位置: src/features/composite/render/composite_renderer.py:1054
  - 证据:
    ```python
    self._snapshot.frame_number = frame
    result = self._evaluate_snapshot(
    ```
    文件头注释（:8）声明 "Evaluates against GraphSnapshot (immutable copy from UI thread)"；`GraphSnapshot` 为普通可变 dataclass（graph_snapshot.py:99、:126 `frame_number: int = 0`），同一对象同时被 UI 提交的 request、`_last_render_request` 及 `_on_image_decoded` 的重提交路径引用。
  - 影响: 违反快照不可变契约；被共享引用的快照在渲染线程被改写，未来任何在 UI 线程读取该快照 frame_number 的代码都会读到驱动中的帧号（当前 CPython 下为良性数据竞争，但契约破裂后易被新代码放大）。
  - 建议: 驱动循环内用 `dataclasses.replace(snapshot, frame_number=frame)` 生成新快照并赋给 `self._snapshot`，或在 GraphSnapshot 上强制只读。
  - 置信度: 中（变异事实确凿；当前无观察到的错误行为）

- [AUD-8208] [Suggestion] 空 file_path 的 image_input 节点每次求值都被标记 LOADING，且永无解码到来
  - 位置: src/features/composite/render/composite_renderer.py:1162-1176
  - 证据:
    ```python
    if node.is_ready and not node.needs_file_mtime_check(file_path):
        continue
    node.set_load_loading()
    ...
    image_loader.request_decode(nid, file_path, ...)
    ```
    新建节点 `_gpu_texture=None`、`_load_state=MISSING`（image_input_node.py:158、:172）→ `is_ready` False → 走入标记分支；而 `ImageLoader.request_decode` 对空路径直接 return（image_loader.py:653-654 `if not file_path: return`）。
  - 影响: 未配置路径的节点状态机被反复推入 LOADING，解码永远不会开始也没有失败回调将其复位；状态与真实情况（MISSING/未配置）不符，且每次全图求值重复触发 `set_load_loading` 的状态抖动。
  - 建议: 在 `set_load_loading()` 之前增加 `if not file_path: continue`（或标记为 MISSING 并保留占位符路径）。
  - 置信度: 中（逻辑路径核实；当前未发现 LOADING 状态的外部 UI 消费者，实际用户可见影响待确认）

- [AUD-8209] [Suggestion] RENDER_3D_VIEW 快速路径只更新 `_last_viewer_roi` 而不同步 `_active_viewer_roi`
  - 位置: src/features/composite/render/composite_renderer.py:653-657（对比 :754-760）
  - 证据:
    ```python
    viewer_roi = request.viewer_roi
    self._last_viewer_roi = (
        (viewer_roi.x, viewer_roi.y, viewer_roi.width, viewer_roi.height)
        if viewer_roi is not None and not viewer_roi.is_empty else None
    )
    ```
    `_active_viewer_roi`（`_render_to_output` 全帧重组的依据，:1732）仅在 `_handle_evaluate_graph` 中设置；`cache_compatible` 判定（:588-606）不比较 viewer_roi。
  - 影响: 若携带不同 viewer_roi 的 RENDER_3D_VIEW 请求命中缓存，签名态 `_last_viewer_roi` 被更新而实际重组用的 `_active_viewer_roi` 保持旧值，两份状态分叉；随后同 ROI 的完整 EVALUATE_GRAPH 会因签名已"最新"而被跳过，潜在保留错误裁剪构图的帧。
  - 建议: 快速路径同样从 request 重算 `_active_viewer_roi`（含 PREVIEW/proxy_scale 守卫），或将 viewer_roi 纳入 `cache_compatible` 比较。
  - 置信度: 低（需要 UI 在 ROI 变化时仍发 RENDER_3D_VIEW 才触发；状态分叉本身确凿）

### 通过项
- 线程安全（核心模型）：命令队列单向交付、`_process_command` 全程持全局 GL 互斥、`stop_thread` 有界等待并递增 drive generation 唤醒、`_playback_drive_loop` 睡眠/等待期间正确放锁重锁。
- GPU 资源生命周期：输出三缓冲环采用 fence 延迟释放（`_create_output_release_fence`/`_drain_deferred_output_releases`，含无 sync 对象时的 finish 回退与部分分配失败的环形复位 `_ensure_output_buffers`）；`_cleanup` 单次执行、先取消 worker 再释放 GL 对象、scope 引擎释放持互斥。
- GLSL·HDR 正确性：导出路径 uint8/uint16 归一化、浮点原样转 f16 无 clamp（:1340-1346）；回读边界统一 flipud + RGB→BGR 并显式声明 origin/color order 契约。
- 信号防递归：本文件为渲染线程侧，不直接操作 UI 控件，全部经 `frame_ready`/`metadata_ready`/`node_thumbnail_ready`/`preview_image_ready`/`gpu_scope_ready` 跨线程信号；缩略图数据转 bytes 后发射，避免 numpy 跨线程共享。
- 状态提交一致性：skip 签名覆盖 generation/color_revision/render_mode/ROI/color 纹理与尺寸/proxy_scale/quality_profile/output/view_signature，force_refresh 与图片上传/模型导入路径均强制失效缓存。
- 异常与取消路径：run() 主循环异常被捕获并带栈记录 + `error_occurred` 上报；stale 结果只抑制上屏、不回收仍被缓存引用的纹理（:866-878 注释与实现一致）；同步 API（readback_output/render_export）有超时与 done_event 兜底。
- 输入验证：`_handle_render_export` 校验 `ndim/shape[2]`；`_handle_render_gpu_scope` 校验 ScopeDemand 类型与纹理尺寸；`submit()` 各合并分支保持非同类命令原序回队。
- 性能模式：EVALUATE/RENDER_3D_VIEW/RENDER_GPU_SCOPE 三类命令均 latest-wins 合并；驱动循环用 O(1) 轮询替代反复全图求值；scope 引擎按需懒加载且失败降级一次记日志。

---

## src/features/composite/render/compute_shader_manager.py (行数: 679)

### 发现

- [AUD-8211] [Major] compute 分发之间及下游采样前缺少 glMemoryBarrier（imageStore → texture 读取）
  - 位置: src/features/composite/render/compute_shader_manager.py:516（`_run_blur_pass`）、:600（erode/dilate）
  - 证据:
    ```python
    input_tex.use(0)
    output_tex.bind_to_image(0, read=False, write=True)
    ...
    prog.run(gx, gy, 1)
    ```
    且 `apply_blur_compute` 串链两次 pass（:467 `self._run_blur_pass(prog_h, input_tex, intermediate_tex, ...)` → :473 `prog.run` 后立即用 `texture()` 采样 intermediate）。对应着色器混用 `imageStore(u_output, ...)` 写出与 `texture(u_input, ...)` 采样（shaders.py:3621/3625、:3676/3680）。全仓 grep 确认 `src/` 下无任何 `memory_barrier`/`glMemoryBarrier` 调用。
  - 影响: 按 OpenGL 规范（§2.13.6/7.11），image 写入对后续 texture fetch 不可见需 `glMemoryBarrier(GL_TEXTURE_FETCH_BARRIER_BIT | GL_SHADER_IMAGE_ACCESS_BARRIER_BIT)`；缺失属未定义行为——水平 pass 结果可能未提交即被垂直 pass 采样、输出纹理被下游片元着色器采样同理，表现为模糊/形态学结果错乱或撕裂（驱动相关；GL 4.1 的 macOS 不走此路径，Windows/Linux 独显暴露）。
  - 建议: 在每次 `prog.run` 之后调用 `self._ctx.memory_barrier(moderngl.TEXTURE_FETCH_BARRIER_BIT | moderngl.SHADER_IMAGE_ACCESS_BARRIER_BIT)`（ModernGL Context 支持），至少必须加在 H→V 之间与最终输出返回前。
  - 置信度: 高（规范违反确凿；是否实际出错随驱动而异）

- [AUD-8212] [Major] compute 模糊/腐蚀膨胀路径未实现 mirror(2) 边缘策略，静默退化
  - 位置: src/features/composite/render/compute_shader_manager.py:467-473、:509（edge_policy 透传）
  - 证据:
    ```python
    self._run_blur_pass(prog_h, input_tex, intermediate_tex, radius, w, h, edge_policy)
    ...
    prog['u_edge_policy'].value = edge_policy
    ```
    计算着色器仅处理 0/1：shaders.py:3615-3619 `if (u_edge_policy == 0) {...clamp...} else if (u_edge_policy == 1) {...fract...}`（3670-3674 同）；而 UI 侧 `_EDGE_POLICY_MAP = {"clamp": 0, "wrap": 1, "mirror": 2}`（blur_evaluator.py:29），fragment 路径有完整 mirror 分支（shaders.py:503 `texture(tex, m)`）。
  - 影响: 用户选择 mirror 边缘策略且半径 > 16 时走 compute 路径，越界采样 UV 既不 clamp 也不 mirror，落回纹理自身 wrap 状态——与 fragment 路径结果不一致，大半径模糊边缘出现错误像素（渲染错误类缺陷）。
  - 建议: 在 compute 着色器中补 `else if (u_edge_policy == 2)` 的 mirror UV 分支，或在 `apply_blur_compute`/`should_use_compute_blur` 中对 edge_policy==2 直接返回 None 走 fragment 回退。
  - 置信度: 高（代码路径确凿；触发需 mirror + 大半径参数组合）

- [AUD-8213] [Suggestion] `_output_texture_cache` 为死属性，声明与清理逻辑均无生产者
  - 位置: src/features/composite/render/compute_shader_manager.py:115、:411-418
  - 证据:
    ```python
    self._output_texture_cache: Dict[Tuple[int, int], object] = {}
    ...
    for key, tex in list(self._output_texture_cache.items()):
        try:
            tex.release()
    ```
    全仓 grep：该属性仅在本文件出现（声明 + release 遍历），从未写入；实际纹理均经 `resource_pool.acquire_texture` 管理。
  - 影响: 误导维护者以为存在第二条纹理持有路径；`release()` 中对空字典的遍历为死代码。
  - 建议: 删除该属性及 release() 中对应循环（或接通真正的直接创建路径 `_create_output_texture` 无 pool 分支的登记）。
  - 置信度: 高

- [AUD-8214] [Suggestion] `apply_blur_compute` 中途异常时中间/输出纹理不归还资源池（无 try/finally）
  - 位置: src/features/composite/render/compute_shader_manager.py:463-481（同类 :581-602）
  - 证据:
    ```python
    intermediate_tex = self._create_output_texture(w, h, resource_pool)
    self._run_blur_pass(prog_h, input_tex, intermediate_tex, radius, w, h, edge_policy)
    output_tex = self._create_output_texture(w, h, resource_pool)
    self._run_blur_pass(prog_v, intermediate_tex, output_tex, radius, w, h, edge_policy)
    if resource_pool:
        resource_pool.release_texture(intermediate_tex)
    ```
    `_run_blur_pass` 内 `prog['u_radius'].value = radius` 等统一访问在变量被驱动优化剔除时抛 KeyError，异常沿调用栈传给 blur_evaluator 的宽 except 落回 fragment 路径，已 acquire 的纹理无人释放。
  - 影响: 每次失败泄漏 1-2 张全分辨率 RGBA16F 池纹理（4K 约 64MB/张）；若失败为系统性（uniform 不匹配），每帧泄漏直至 teardown。
  - 建议: 以 try/except 包裹两个 pass，异常时释放已 acquire 的 intermediate/output 再返回 None（维持"返回 None 即回退"的语义）。
  - 置信度: 高（代码模式确凿；触发依赖前置失败）

### 通过项
- 线程安全：程序缓存与 reload/release 均持 RLock；`_warn_unavailable_once` 用模块级锁防日志风暴。
- GPU 资源生命周期：`release()` 逐对象 try/except 释放程序与纹理；probe 编译的测试程序即时 release（:200-201）；Metal 桥初始化失败不阻塞渲染并显式诊断。
- GLSL·HDR 正确性：compute 输出统一 RGBA16F（`dtype='f2'`），无 clamp；工作组分发 ceil(w/16)×ceil(h/16) 与 local_size 16×16 一致，越界 invocation 提前 return。
- 异常与取消路径：能力探测采用 probe 编译而非仅版本串（W4-01 注释与实现一致）；编译失败经 `_fallback_callback` 通知 UI 并返回 None 走 fragment 回退；Metal 失败复用同一回退语义（`_apply_via_metal`）。
- 输入验证：`_parse_gl_version` 对畸形版本串回退 (0,0)；`get_program` 对未知着色器名报错并列出合法名。
- 性能模式：版本低于 4.3 时跳过 probe 编译直接判不可用；不可用告警按能力键全进程一次；shared 变体按半径/尺寸阈值选择。
- 类型注解缺失：公开方法均有参数/返回注解（`cached_programs` 返回裸 `list` 为小瑕疵，不单独立项）。
