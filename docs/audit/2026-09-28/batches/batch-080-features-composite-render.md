# 批次报告：batch-080 — src/features/composite/render/

- 审查日期：2026-09-28
- 编号区间：AUD-8000–AUD-8099
- 审查文件：composite_export_renderer.py (1,521)、composite_frame_renderer.py (777)、composite_graph_render_service.py (261)，共 2,559 行
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解
- 交叉验证（只读）：evaluators/base_evaluator.py、render/render_scheduler.py、render/cache_manager.py、render/gpu_resource_pool.py、render/pbo_readback.py、shared/gpu/composite_gpu_frame_handle.py、render/composite_renderer.py、composite/export_provider.py、tests/composite/test_tiling.py

---

## src/features/composite/render/composite_export_renderer.py (行数: 1,521)

### 发现

- [AUD-80001] [Critical] 瓦片渲染跨瓦片复用首瓦片裁剪后的 SOURCE 缓存，大图导出/大帧预览静默产错图
  - 位置: src/features/composite/render/composite_export_renderer.py:1135-1137（同型代码 989-990、1240-1242）
  - 证据:
    ```python
                # Clear export cache between tiles to free GPU memory
                if self._cache_manager:
                    self._cache_manager.clear_export_cache()
    ```
    每瓦片调用 `self._evaluate_snapshot(render_mode=RenderMode.EXPORT, tile_context=tile_ctx)`（1109-1112 / 958-961），瓦片间仅清 EXPORT 类缓存。
  - 影响: 机制（已沿调用链核实）：瓦片 1 求值时源节点输出被裁剪到瓦片 1 的 padded_roi（base_evaluator.py:907-912），并以 **SOURCE** 类别写入缓存（cache_manager.py `_determine_category`：SOURCE 优先于 EXPORT；`_SOURCE_NODE_TYPES = {color_input, constant, image_input, motion_graphic, text}`，cache_manager.py:130-132）。`clear_export_cache()` 只清 `category == EXPORT` 的条目（cache_manager.py:458-468），SOURCE 条目存活；而导出路径的缓存 key 不含瓦片 ROI（compute_key 无 tile 参数，roi=None——ROI 传播仅 PREVIEW+viewer_roi 启用）。瓦片 2 起以相同 key 命中首瓦片裁剪纹理（base_evaluator.py:838-849 主命中路径；679-692 dirty-skip 路径同理——瓦片 1 的 plan 已推进 tracking，瓦片 2 全图判净）。结果：>2048px 且显存估算超预算（needs_tiling）的图中，凡含 image_input/constant/text/color_input 静态源的图，`_render_tiled_with_plan`、`_render_tiled`（死代码）与 `_render_tiled_gpu_stitch`（大帧 srgb_u8 预览）从第 2 个瓦片起全部渲染首瓦片内容——等尺寸内部瓦片静默错图；边缘瓦片尺寸不匹配时 `_stitch_tile` 赋值抛 ValueError，整个渲染报错失败。timeline_clip_input/video_frame/motion_graphic 属 DYNAMIC_SOURCE_NODE_TYPES（render_scheduler.py:106）绕过持久缓存，时间线视频预览不受影响——正因如此该缺陷不易被日常播放路径暴露；tests/composite/test_tiling.py 仅覆盖内存估算与瓦片几何（纯 mock），无逐瓦片像素断言。
  - 建议: 三选一：(1) 缓存 key 纳入 tile_context 的 padded_roi（对齐 viewer ROI 的 roi_suffix 先例）；(2) 瓦片间改用 `cache_manager.clear()` 全量清理（损失跨瓦片共享，但导出路径本就瓦片间清缓存）；(3) 瓦片求值期间对静态 SOURCE 节点强制 bypass（按 tile_context 存在判定）。并补一条“两瓦片内容必须不同”的端到端回归测试。
  - 置信度: 高（机制逐行核实；触发条件为大图+静态源+超预算，属真实导出场景）

- [AUD-80002] [Major] render_gpu_handle 缺少一次性模式的 cleanup 纪律，非持久调用方泄漏全部 GL 资源
  - 位置: src/features/composite/render/composite_export_renderer.py:542-691
  - 证据:
    ```python
        except Exception as exc:
            logger.error("render_gpu_handle failed: %s", exc)
            logger.debug("Stack trace:", exc_info=True)
            trace.fallback_reason = str(exc)
            trace.total_ms = (time.monotonic() - t0) * 1000.0
            emit_trace(trace)
            return make_unavailable_handle(
                str(exc), generation=generation, frame=frame_number,
                trace_id=trace.trace_id)
    ```
    对比同类公开入口的收尾纪律（775-778、408-411）：
    ```python
        finally:
            self._tile_context = None
            if not self._persistent:
                self.cleanup()
    ```
  - 影响: `render_and_readback`、`render_and_readback_passes`、`_render_full_frame`、两条瓦片路径均在 finally 中按 `not self._persistent` 收尾；`render_gpu_handle` 无论成功失败都不调 cleanup。类文档明确"one-shot export path keeps the default build-render-teardown behaviour"，一次性（默认非持久）调用该 API 将泄漏 fb_manager、resource pool、cache manager、shader 库与全部 render node 的 GPU 资源。当前树内调用（composite_frame_renderer.py:308）恒走 persistent 模式故未暴露，属潜伏 API 缺陷。
  - 建议: 在 render_gpu_handle 增加 `finally: if not self._persistent: self.cleanup()`，与其它入口对齐；或在 docstring 显式声明"仅限 persistent 模式调用"。
  - 置信度: 高（代码事实）；实际影响为潜伏（在树内无一次性调用方）

- [AUD-80003] [Major] render_and_readback_passes：render_passes=None 且 srgb_u8 时 AOV 也走 sRGB 读回，违反自身文档契约
  - 位置: src/features/composite/render/composite_export_renderer.py:353-356
  - 证据:
    ```python
        self._readback_output = (
            readback_output if requested_names in (None, ("beauty",))
            else "linear_f32"
        )
    ```
    而 requested_names=None 在 368-372 被展开为 beauty + 全部 AOV；对比 render_and_readback 的守卫（233-237）：
    ```python
        if str(render_pass or "beauty") != "beauty":
            self._readback_output = "linear_f32"
    ```
  - 影响: docstring 承诺 "Mixed beauty/AOV reads are forced to linear_f32"，但当调用方传 `render_passes=None`（读 beauty+全部 AOV）且 `readback_output="srgb_u8"` 时，AOV（depth/object_id/motion 等 data 通道）会被 sRGB OETF 编码成 uint8——数值数据损坏。`_readback` 只看 `self._readback_output` 分支，不检查 selected 的 `color_space="data"`。当前树内调用（export_provider.py:149-151）用默认 linear_f32 不触发，属契约级缺陷。
  - 建议: 判定条件改为"展开后实际 pass 集合 ⊆ {beauty}"再沿用 srgb_u8，或将 None 展开提前到设置 `_readback_output` 之前。
  - 置信度: 高（逻辑确定）；触发依赖调用方组合（现无树内触发）

- [AUD-80004] [Suggestion] `_render_tiled` 为无调用死代码，且 TileContext 构造已与现行瓦片协议漂移
  - 位置: src/features/composite/render/composite_export_renderer.py:1158-1261
  - 证据:
    ```python
                    tile_ctx = TileContext(
                        padded_roi=tile.padded_roi,
                        output_format=output_format,
                    )
    ```
    （1210-1213，缺 tile_id/output_roi/padding；全仓 grep 仅此定义处引用 `_render_tiled(`）
  - 影响: 与 `_render_tiled_with_plan`（1102-1108 显式传 tile_id/output_roi/padding）同功能重复且已漂移；若日后被误复用，将产出降级瓦片。同时其瓦片失败仅记日志。
  - 建议: 删除 `_render_tiled`，或对齐现行 TileContext 协议并在 docstring 标注仅测试使用。
  - 置信度: 高

- [AUD-80005] [Suggestion] `_finish_trace` 在 cleanup 之后读 ctx.info，一次性导出路径设备标识恒为空
  - 位置: src/features/composite/render/composite_export_renderer.py:309-311（cleanup 置空见 1512）
  - 证据:
    ```python
        info = getattr(self.ctx, "info", {}) or {}
        trace.device_identity = str(info.get("GL_RENDERER", ""))
        trace.gl_version = str(info.get("GL_VERSION", ""))
    ```
  - 影响: 非持久路径先执行 `_render_*` 的 finally → `cleanup()` 将 `self.ctx = None`（1512），随后 `_finish_trace` 才运行；所有一次性导出/预览帧 trace 的 device_identity/gl_version 恒为空串，削弱 P0-4 可观测性目标（跨设备对比/回退归因失效）。
  - 建议: 在进入渲染前捕获 `info.get("GL_RENDERER"/"GL_VERSION")` 存于局部/trace，`_finish_trace` 使用缓存值。
  - 置信度: 高

- [AUD-80006] [Suggestion] `render_gpu_handle` 的 render_mode 注解与默认值不符；`_wrapped_context` 属性从未被读取
  - 位置: src/features/composite/render/composite_export_renderer.py:547、122
  - 证据:
    ```python
                          render_mode: "RenderMode" = None,
    ```
    ```python
        self._wrapped_context = context is not None
    ```
  - 影响: 注解声明为 RenderMode 类型却默认 None（应为 `Optional["RenderMode"]`），静态检查失效；`_wrapped_context` 只写不读，属死字段（推断原意是 cleanup 时区分自建/包裹 context，现由 1518-1521 注释承担）。
  - 建议: 注解改 `Optional["RenderMode"]`；删除 `_wrapped_context` 或在 cleanup 分支真正使用它。
  - 置信度: 高

- [AUD-80007] [Suggestion] srgb_u8 读回在 shader 缺失时静默降级为 linear f32，输出 dtype 契约悄悄改变
  - 位置: src/features/composite/render/composite_export_renderer.py:1414-1417、1429-1430
  - 证据:
    ```python
            if srgb_u8:
                prog = self.utility_shaders.get('readback_srgb_bgr')
                if prog is None:
                    srgb_u8 = False  # shader missing → linear fallback
                    prog = self.utility_shaders.get('passthrough')
    ```
  - 影响: 请求 uint8 sRGB 预览的调用方在 `readback_srgb_bgr` 编译失败时拿到 float32 线性数组：dtype、色彩编码双双改变且无日志、无 trace 标记。当前 STANDARD_NAMES 恒含该 shader（shader_library.py:118），触发概率低，但一旦发生排查困难。
  - 建议: 降级时 logger.warning 并在返回的 ExportResult/trace 上记录 `preview_strategy_reason` 类似标记。
  - 置信度: 高（行为确定）；触发概率低

- [AUD-80008] [Suggestion] 用 `'tiles' in dir()` 探测局部变量绑定，反模式且脆弱
  - 位置: src/features/composite/render/composite_export_renderer.py:1150、1255
  - 证据:
    ```python
                tile_count=len(tiles) if 'tiles' in dir() else 0,
    ```
  - 影响: `dir()` 返回当前局部作用域名字列表，语义可用但依赖内省实现；循环内提前异常（tiles 未绑定）与正常路径混在一个表达式里，可读性差。
  - 建议: 在 try 之前初始化 `tiles: list = []`，except 中直接 `len(tiles)`。
  - 置信度: 高

### 通过项
- 线程安全契约明确：文档与调用点均要求调用线程持有 GL context；跨线程 fence 释放经 `_deferred_fence_deletes`（deque，append/popleft 原子）延迟到生产者线程 drain（P0-5），设计正确
- GPU 资源生命周期：`_render_tiled_gpu_stitch`/`_readback`/`_render_quad` 均遵守 unbind-before-release（P1-6），FBO 不手动释放避免 InvalidObject 残留（1016-1020 注释与实现一致）；pool acquire 计入预算（P1-6）
- HDR 正确性：linear_f32 导出读回无 clamp(0,1)，保留超 1 值；AOV 在 render_and_readback 强制 linear（233-237）；sRGB 编码仅限预览 srgb_u8 路径
- 信号防递归：本文件无 Qt 信号，维度不适用
- 异常与取消路径：所有公开入口 try/except 返回结构化 error，瓦片失败 fail-fast 而非静默黑块（P1-6 语义），无宽泛裸 except 掩错（异常均记录）
- 输入验证：`render_and_readback` 的 render_pass/readback_output 做了归一化；`_create_headless_context` 对 env 覆盖值做类型校验并降级
- 性能模式：srgb_u8 GPU 端 OETF+BGR+flip 单次 u8 读回、PBO 异步 lane、瓦片 f4→f1 读回目标选择（macOS RGBA8UI 问题规避，1423-1430）均符合 4K 实时基准方向；无多余 ctx.finish()（P2-10）
- 状态一致性：P1-8 修复正确——分析用 plan 传 `update_tracking=False`，仅求值内 plan 推进 dirty 状态

---

## src/features/composite/render/composite_frame_renderer.py (行数: 777)

### 发现

- [AUD-80010] [Suggestion] 时间线查找失败时静默回退到字典中第一个 timeline，多时间线工程可能取错源
  - 位置: src/features/composite/render/composite_frame_renderer.py:368-371
  - 证据:
    ```python
        if timeline_sequences:
            timeline = timeline_sequences.get(timeline_id) if timeline_id else None
            if timeline is None:
                timeline = next(iter(timeline_sequences.values()), None)
    ```
  - 影响: `timeline_id` 缺失或不在序列表中时，任意取第一个 timeline；若 clip_id 恰好也在该时间线中命中（或不命中后 clip_local_frame 归零 + source_start 回退），静默渲染错误片段/帧，无日志无诊断。
  - 建议: 多于一个序列时禁止回退（记 warning 并返回 None, None），或至少 logger.warning 记录回退目标。
  - 置信度: 中（单时间线工程常见，回退可能是有意兼容；但多时间线场景风险真实）

- [AUD-80011] [Suggestion] 异常/早退路径不清 `last_frame_trace`，失败帧携带上一帧的性能归因
  - 位置: src/features/composite/render/composite_frame_renderer.py:242-247（早退路径 160-163、171-175 只清 last_diagnostics）
  - 证据:
    ```python
        except Exception as e:
            logger.error(
                f"CompositeFrameRenderer render error (graph={graph_id}, "
                f"frame={timeline_frame}): {e}"
            )
            return None
    ```
  - 影响: `self.last_frame_trace`（98 声明）在失败帧保留前帧值，T-P1-60 步骤 6 的调用方读取时会拿到过期 attribution，误导性能分析。
  - 建议: 在所有 return None 路径统一 `self.last_frame_trace = None`（或封装 `_reset_frame_state()`）。
  - 置信度: 高（状态残留确定）；影响轻微

- [AUD-80012] [Suggestion] 未使用导入 `Dict`、`TextureResult`；内部渲染器类型退化为 Any
  - 位置: src/features/composite/render/composite_frame_renderer.py:36、42、92
  - 证据:
    ```python
    from typing import Any, Dict, Optional
    ...
    from src.features.composite.nodes.base_node import TextureResult
    ...
        self._export_renderer: Any = None
    ```
  - 影响: `Dict`、`TextureResult` 全文无引用（注解均用内置 dict / Any）；`_export_renderer: Any` 放弃了类型检查，可用 TYPE_CHECKING 前向引用真实类。
  - 建议: 删除两个未用导入；`if TYPE_CHECKING: from ...composite_export_renderer import CompositeExportRenderer` 并以字符串注解标注。
  - 置信度: 高

- [AUD-80013] [Suggestion] `collect_async_preview(wait=True)` 的"终帧正确性"契约被 pbo_readback 中重复定义的 get_latest 削弱
  - 位置: src/features/composite/render/composite_frame_renderer.py:508-526
  - 证据:
    ```python
        if self._async_pbo is None:
            return None
        result = self._async_pbo.get_latest(wait_newest=wait)
        if result is None:
            return None
    ```
    （docstring 511-515 承诺 wait=True "blocks until the newest enqueued frame's DMA completes"）
  - 影响: 交叉验证发现 src/features/composite/render/pbo_readback.py 中 `poll_ready`/`get_latest` 各定义了两次（296/451、341/497），后一定义静默覆盖前者；生效版本（497-523）在 wait_newest 分支忽略 `_wait_entry_fence` 的超时返回值、直接 `_map_entry` 映射在途 PBO——T-P1-71 有界等待修复只存在于被覆盖的死版本中。本类的终帧收集可能在 DMA 未完成时读到未定稿数据；非阻塞分支也可能把恰好在本次 poll 完成的最新帧丢弃后返回 None。缺陷主体在 pbo_readback.py（属其他批次），此处记录消费者契约受损。
  - 建议: 修复 pbo_readback.py 的重复定义（删除旧版，保留尊重 `_wait_entry_fence` 返回值的版本）；本类可在 wait=True 返回 None 时补一次重试或记录诊断。
  - 置信度: 高（重复定义为代码事实，覆盖行为已逐行核对）

### 通过项
- 输入验证：`render_comp_clip_frame`/`render_comp_clip_gpu` 均校验 graph_data 非空且含 'nodes' 键
- GPU 资源生命周期：provider 按 key 复用并显式 `_release_provider()`；async slot 纹理经池分配/回收，cleanup 顺序正确（PBO → slot 纹理 → export renderer → 反序列化缓存 → provider）
- 线程安全：docstring 明确 NOT thread-safe、每线程一实例，与实际使用（PreviewRenderWorker/导出线程）一致
- 状态一致性：反序列化缓存以 `id(graph_data)` + 持久 revision token（graph_revision/revision/modified_at）双键，强引用防 id 复用，`invalidate_graph_cache()` 提供显式作废契约；`timeline_sync_vt` 注入/移除对称
- 异常路径：两个公开渲染入口异常均被捕获并返回 None/不可用 handle，不向 UI 线程抛出；`render_comp_clip_gpu` 遵守"never None, never raises"契约
- 性能模式：COMPOSITE_GRAPH_RENDERER_VERSION 身份常量正确用于渲染器实现变更时的缓存键失效（P2-17）；preview 用 srgb_u8 GPU 读回路径；retain_frame 替代整帧清缓存（TL-C03）且失败时降级 clear
- GLSL/HDR：本文件不含 shader；GPU handle 传递 color_space/alpha 语义元数据

---

## src/features/composite/render/composite_graph_render_service.py (行数: 261)

文件头已标注 DORMANT(2026-08-19 审计)：已实现有测试、无生产入口、有意休眠。以下发现按休眠代码的降级标准评定。

### 发现

- [AUD-80020] [Suggestion] telemetry 列表只增不减，长时间运行内存无界增长
  - 位置: src/features/composite/render/composite_graph_render_service.py:243-252（读取侧 239-241）
  - 证据:
    ```python
        self._telemetry.append({
            "frame": frame,
            "path": path,
            "elapsed_ms": round(elapsed_ms, 1),
            "success": success,
            "error": error,
        })
        self._revision_counter += 1
    ```
  - 影响: `get_telemetry()` 仅返回末 100 条但从不裁剪 `_telemetry`；逐帧追加 dict，长期播放会话内存持续增长（休眠服务，影响有限）。
  - 建议: append 后 `del self._telemetry[:-100]`（或用 deque(maxlen=100)）。
  - 置信度: 高

- [AUD-80021] [Suggestion] `GpuFrameHandle` dataclass 与 `_revision_counter` 为死代码
  - 位置: src/features/composite/render/composite_graph_render_service.py:43-56、74、252
  - 证据:
    ```python
    @dataclass(frozen=True)
    class GpuFrameHandle:
        """Handle to a GPU-rendered frame (texture in GL context)."""
        texture_id: int
    ```
    ```python
        self._revision_counter = 0
    ```
  - 影响: 模块内无任何代码使用 `GpuFrameHandle`（render_gpu 实际返回 shared 层的 `CompositeGpuFrameHandle`）；`_revision_counter` 只在 252 递增、从未读取。二者会误导接线者以为存在旧版 handle 协议。
  - 建议: 接线前删除；或在 GpuFrameHandle docstring 标注 deprecated-by-CompositeGpuFrameHandle。
  - 置信度: 高

- [AUD-80022] [Suggestion] `gl_context` 仅作能力开关从未被绑定使用，API 语义有误导
  - 位置: src/features/composite/render/composite_graph_render_service.py:71-72、167-176
  - 证据:
    ```python
        if self._gl_context is None:
            ...
            logger.debug("CompositeGraphRenderService GPU path unavailable: no GL context")
            return None
    ```
  - 影响: 构造参数 `gl_context` 存储后除 None 判定外全程未使用——既不 make_current 也不传给渲染器；实际求值依赖调用线程当前 context（CompositeExportRenderer 内 `moderngl.create_context()`）。调用方若误以为服务负责绑定 context，会在错误线程/无 context 状态下渲染。
  - 建议: 接线时将 context 显式传入 CompositeExportRenderer(context=...) 或在 docstring 强化"caller must make current"契约。
  - 置信度: 中（注释 180-181 已声明 caller 负责绑定，属文档-实现冗余而非错误；但参数形同虚设）

- [AUD-80023] [Suggestion] cleanup 使用无日志的宽泛 `except Exception: pass`
  - 位置: src/features/composite/render/composite_graph_render_service.py:256-260
  - 证据:
    ```python
        if self._composite_renderer is not None:
            try:
                self._composite_renderer.cleanup()
            except Exception:
                pass
            self._composite_renderer = None
    ```
  - 影响: 违反项目铁律"不得宽泛 except 掩盖领域错误"（本批次其他文件同类路径均 logger.debug）；清理失败（如 GL 上下文已丢失时的释放异常）完全不可见。
  - 建议: 改为 `except Exception as e: logger.debug(f"...: {e}")`，与 CompositeFrameRenderer.cleanup 保持一致。
  - 置信度: 高

### 通过项
- 输入验证：render_gpu 校验 `isinstance(graph_data, dict)` 且含 'nodes'；render_cpu 委托下游校验
- 异常路径：两个渲染入口全程 try/except 返回 None + telemetry 记录，永不向调用方抛出
- 状态一致性：CPU/GPU 路径共用同一 CompositeFrameRenderer 实例并统一 cleanup，无重复资源
- 性能模式：telemetry 计时用 monotonic；GPU 路径零 CPU 读回（handle 契约）
- GLSL/HDR、信号防递归、线程安全：文件无 shader、无 Qt 信号；休眠适配器层，维度不适用
