# Batch-002 静态审查报告 — color/core/async_renderer.py

- 日期: 2026-09-28
- 审查范围: `src/features/color/core/async_renderer.py`（6,669 行，已全文通读，1–6669 连续分块读完）
- 问题编号区间: AUD-0200 – AUD-0214
- 汇总: Critical 1 / Major 5 / Suggestion 9

---

## src/features/color/core/async_renderer.py (行数: 6669)

### 发现

- [AUD-0200] [Critical] `_do_bake_layer` 引用未定义变量 `render_request_id`，AI Bake 每次必然抛 NameError
  - 位置: src/features/color/core/async_renderer.py:6428
  - 证据:
    ```python
    prog = self.pipeline.build_composition_shader(
        has_curves=has_curves,
        has_warper=has_warper,
        has_lut3d=has_lut3d,
        has_soft_clip=has_soft_clip,
        grain_enabled=False,
        has_qualifier=False,
        has_hsl_curve=has_hsl_curve,
        has_mixer=has_mixer,
        plugins=layer_plugins,
        request_id=render_request_id,      # ← 6428
        layer_id=pl_id,
    )
    ```
    全文件 grep 确认 `render_request_id` 只在 `_do_render_impl` 内绑定（2319 行：`render_request_id = params.get('render_request_id')`），`_do_bake_layer`（6255–6502）方法体内与模块层均无绑定。
  - 影响: 首个未冻结的前缀层走到该分支即触发 `NameError: name 'render_request_id' is not defined`；异常经 `try/finally`（finally 正常释放 VRAM）后被 `run()` 主循环的宽泛 `except Exception` 吞掉，`layer_baked_ready` 永不发射——AI Baking 功能对任何活动层 100% 失效，且仅以 stderr traceback 形式可见。
  - 建议: 在 `_do_bake_layer` 开头从 `data` 提取（如 `render_request_id = data.get('render_request_id')`）或显式传 `request_id=None`；并为 bake 路径补一条端到端测试。
  - 置信度: 高

- [AUD-0201] [Major] `_ensure_color_output_fbo` 尺寸变化时立即释放仍在被 Composite/monitor 消费的 ColorOutput 纹理，未走 deferred-release
  - 位置: src/features/color/core/async_renderer.py:5797-5806
  - 证据:
    ```python
    if fbo is not None:
        try:
            fbo.release()
        except Exception:
            logger.debug("Failed to release %s ColorOutput FBO", output_target)
    if tex is not None:
        try:
            tex.release()
        except Exception:
            logger.debug("Failed to release %s ColorOutput texture", output_target)
    return new_tex, new_fbo
    ```
    对比前缓冲路径的延迟释放（5712-5713 行）：`self.garbage.append((time.time(), self.fbo_pair[i], self.tex_pair[i]))`（2 秒后才释放）。ColorOutput 的 glo 已通过 `color_output_ready` / `monitor_color_output_ready`（2787-2790 行）发布给跨上下文消费者。
  - 影响: DeckLink monitor / Composite 消费者持有旧 glo 并可能仍在自己的上下文/线程中 blit；渲染线程立即删除共享纹理名，消费者下一帧采样到已删除（或已被驱动复用）的纹理 → 单帧花屏/黑帧，且与 fbo_pair 的延迟释放策略不一致。
  - 建议: 旧 ColorOutput tex/fbo 同样进入 `garbage` 延迟释放，或发布 fence 后再延迟删除。
  - 置信度: 中

- [AUD-0202] [Major] `_yuv_rgb_output_cache` 按尺寸无限累积纹理，窗口多次缩放造成 GPU 内存持续增长
  - 位置: src/features/color/core/async_renderer.py:1255-1280
  - 证据:
    ```python
    size_key = (width, height)
    if size_key not in self._yuv_rgb_output_cache:
        self._yuv_rgb_output_cache[size_key] = []
        self._yuv_rgb_output_ring_idx[size_key] = 0
    cache_list = self._yuv_rgb_output_cache[size_key]
    if len(cache_list) < self._yuv_rgb_output_ring_size:   # 每尺寸固定 3 张
        tex = self.ctx.texture((width, height), 4, dtype='f2')
        ...
    ```
    全文件仅在 `_cleanup`（6034-6039 行）清空该缓存，运行期间对旧尺寸条目无任何淘汰。
  - 影响: 每个出现过的视口尺寸永久保留 3 张 RGBA float16 视口级纹理（4K 视口下每张约 64MB，每尺寸约 192MB）；用户连续拖拽改变窗口大小时 GPU 内存单调增长直至关闭渲染器。
  - 建议: 限制缓存键数量（如 LRU，仅保留最近 1–2 个尺寸），尺寸切换时释放旧尺寸的 ring。
  - 置信度: 高

- [AUD-0203] [Major] matte 重生成异常时缓存悬挂：旧 matte 已归还纹理池，但 `matte_cache` 仍保留可"命中"的旧条目
  - 位置: src/features/color/core/async_renderer.py:3299-3309（源分辨率路径，3432-3435 视口路径同样模式）
  - 证据:
    ```python
    if cached and cached['params_hash'] == params_hash:
        src_matte_tex = cached['matte']
    else:
        if cached:
            self.fb_manager.release_temp_texture(cached['matte'])   # 归还池，可被再分配
        src_matte_tex = self._generate_source_matte(
            layer, input_tex, src_w, src_h
        )                                                          # 此处抛异常则缓存未更新
        self.matte_cache[l_id] = {'matte': src_matte_tex, 'params_hash': params_hash}
    ```
    已确认 `FramebufferManager.release_temp_texture` 会把纹理放回 `_texture_pool` 供复用（gl_backend.py:107-114）。
  - 影响: 若 `_generate_source_matte`（或视口路径的生成代码）在旧纹理归还后抛异常（如 keyer/blur/morphology 着色器缺失导致 KeyError、纹理分配 OOM），`matte_cache[l_id]` 仍指向池内纹理；下一帧参数未变 → 哈希"命中" → 返回一张可能已被池再分配为 ping/pong 临时目标的纹理 → matte 与其他 pass 双重读写同一纹理，出现画面污染/渲染错误。
  - 建议: 先生成新 matte 成功后再释放旧条目，或在生成抛异常时立即 `matte_cache.pop(l_id)` 作废缓存。
  - 置信度: 高（机制确定，触发依赖异常路径）

- [AUD-0204] [Major] `_last_scope_render_params` 浅拷贝长期持有 UI 侧可变 layer DTO，GPU scope lane 稍后在渲染线程直接复用
  - 位置: src/features/color/core/async_renderer.py:2917、1757-1777
  - 证据:
    ```python
    self._last_scope_render_params = dict(params)     # 2917：浅拷贝，layers 列表仍为调用方对象
    ...
    scope_params = getattr(self, '_last_scope_render_params', None)
    ...
    output_params = dict(scope_params)                # 1773：仍是浅拷贝
    output_params['max_proxy_dim'] = requested_max_dim
    output_params['color_output_target'] = 'scope'
    if not self._do_render_color_output(output_params): ...
    ```
  - 影响: 缓存跨帧存活（直到下次 front 发布才替换）。播放期间 GPU_SCOPE lane 低优先级、可能晚于提交很久才执行 `_do_render_color_output`，届时 UI 线程若正在原地对同一批 layer dict（curves_lut、qualifier_params 等）做参数拖拽修改，渲染线程无锁读取被并发写入的嵌套结构 → 撕裂读/偶发异常，与"命令负载冻结快照"的其余队列约定不一致。
  - 建议: 缓存时做深层冻结拷贝（`copy.deepcopy` 或 DTO 序列化快照），确保 scope 重渲使用不可变快照。
  - 置信度: 中（取决于 UI 是否原地复用/修改同一 DTO；机制上无任何隔离）

- [AUD-0205] [Major] legacy 裸帧路径用 `max() > 1.0` 启发式猜尺度，float32 HDR 帧会被整帧错误除以 255
  - 位置: src/features/color/core/async_renderer.py:5192-5197
  - 证据:
    ```python
    else:
        frame_data = rgba.astype(np.float32)
        if frame_data.max() > 1.0:
            frame_data /= 255.0
        frame_data = np.ascontiguousarray(frame_data.astype(np.float16))
        legacy_dtype = 'f2'
    ```
  - 影响: 该分支明确处理 float32 输入；场景线性 HDR 数据（数值合法大于 1.0）会被整体缩小 255 倍后上传 → 画面严重偏暗，HDR 数据被无声破坏。8-bit 判断已有 `dtype == np.uint8` 精确分支，此启发式对 float 输入本质是猜测。
  - 建议: 删除该启发式，float 输入要求调用方显式声明尺度（如带 `scale` 字段），或仅对 `dtype == np.uint8`/`uint16` 做归一化。
  - 置信度: 中（缺陷确定，但该 legacy 路径的实际使用频率未知）

- [AUD-0206] [Suggestion] `load_prog` 吞掉着色器编译失败，后续代码用 `self.utility_shaders['keyer']` 等无守卫下标，失败后每帧 KeyError
  - 位置: src/features/color/core/async_renderer.py:812-821（3347/3443/3518/3604/3640 等消费点）
  - 证据:
    ```python
    def load_prog(name, path):
        try:
            ...
            self.utility_shaders[name] = self.ctx.program(...)
        except Exception as e:
            logger.error(f"to load utility shader {name}: {e}")   # 吞错，且日志文案残缺
    ```
    消费点如 3347 行：`prog_keyer = self.utility_shaders['keyer']`（无 `.get`/None 检查）。
  - 影响: 着色器文件缺失/编译失败时启动仅记一条日志，之后每次带 qualifier/mask 的渲染抛 KeyError，被 `run()` 宽泛 except 吞掉 → 功能静默失效且难以定位（与 AUD-0203 叠加会触发缓存悬挂）。日志消息 "to load utility shader" 还丢了动词。
  - 建议: 编译失败时记录致命状态并禁用对应功能路径，或统一用 `.get()` + 显式降级日志；修正日志文案。
  - 置信度: 高

- [AUD-0207] [Suggestion] `_do_update_aux_tex` lut3d 上传失败时保留旧 LUT 纹理，GPU 状态与模型不一致
  - 位置: src/features/color/core/async_renderer.py:4104-4109
  - 证据:
    ```python
    elif tex_type == 'lut3d':
        path = data['path']
        tex = self._upload_3dlut(path)
        if tex:
            if 'lut3d' in self.layer_textures[l_id]: self.layer_textures[l_id]['lut3d'].release()
            self.layer_textures[l_id]['lut3d'] = tex
    ```
  - 影响: 新 LUT 文件损坏（`_upload_3dlut` 返回 None，仅 warning）时，模型已切到新 `active_lut_path`，渲染继续绑定旧 LUT → 用户看到的始终是上一个 LUT，无 UI 层错误提示。
  - 建议: 上传失败时清除该层 lut3d 槽并回发失败信号，让 UI 能回滚选择或提示。
  - 置信度: 高

- [AUD-0208] [Suggestion] `_do_upload_baked_texture` / `_do_upload_ai_plugin_result` 缺形状校验，写入失败时新建纹理泄漏
  - 位置: src/features/color/core/async_renderer.py:6529-6547、6592-6626
  - 证据:
    ```python
    if image_array.dtype != np.float32:
        image_array = image_array.astype(np.float32)
    h, w = image_array.shape[:2]          # 无 ndim/通道数校验
    ...
    tex = self.ctx.texture((w, h), 3, dtype='f4')
    tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
    tex.write(image_array.tobytes())      # write 抛错则 tex 未 release 也未入表
    self.baked_textures[layer_id] = tex
    ```
  - 影响: 外部（EXR 回读/AI 推理）数组若为 2 维或 4 通道（baked 路径只按 3 通道建纹理），`tobytes()` 长度不匹配 → write 失败 → 已创建的纹理对象既未释放也未登记，泄漏一个 GL 纹理；对比 AI 路径有通道归一化（6599-6604），baked 路径完全没有。
  - 建议: 两处统一做 `ndim==3 且 shape[2]==3` 校验；tex 创建后用 try/except 包裹 write 并在失败时 release。
  - 置信度: 高

- [AUD-0209] [Suggestion] 池属 matte 纹理在 `_do_remove_layer`/`_do_clear_aux_tex`/`_cleanup` 中被直接 `.release()`，绕过 `fb_manager` 池记账
  - 位置: src/features/color/core/async_renderer.py:1108-1113、1130-1135、6008-6011
  - 证据:
    ```python
    # _do_remove_layer（1130-1135）：
    if layer_id in self.matte_cache:
        try:
            self.matte_cache[layer_id]['matte'].release()   # 直接销毁池租借纹理
    ```
    而正常替换路径用 `self.fb_manager.release_temp_texture(cached['matte'])`（3304、3433 行）。已确认 `release_temp_texture` 是"归还池供复用"语义（gl_backend.py:107-114），`get_temp_texture` 会把纹理记入 `_active_textures`。
  - 影响: 直接 release 后纹理仍留在 `fb_manager._active_textures`，其配套 FBO 仍留在 `_fbo_cache`——池内出现已销毁对象；当前恰好 `cleanup()` 不遍历 `_active_textures` 才未触发双重释放，属于脆弱的所有权混用，后续任何人把该纹理经池路径再释放即双重删除。
  - 建议: matte 缓存条目的释放统一走 `release_temp_texture`；`_cleanup` 中同样如此（在 `fb_manager.cleanup()` 之前归还可顺带由池统一释放）。
  - 置信度: 高

- [AUD-0210] [Suggestion] 多处裸 `except:` 捕获（含 SystemExit/KeyboardInterrupt）
  - 位置: src/features/color/core/async_renderer.py:4179-4181、5881-5884
  - 证据:
    ```python
    for tex in target_list:
        try: tex.release()
        except: logger.debug("Failed to release texture during _do_upload cleanup")
    ```
    （`_process_garbage` 5883-5884 同样：`try: fbo.release(); tex.release() except: ...`）
  - 影响: 裸 except 会吞掉 `KeyboardInterrupt`/`SystemExit` 等非 Exception 异常，干扰停线程；也掩盖真实错误类型。
  - 建议: 改为 `except Exception`。
  - 置信度: 高

- [AUD-0211] [Suggestion] `run()` 早退/初始化异常路径不执行 `_cleanup()`，泄漏离屏 surface 与共享上下文
  - 位置: src/features/color/core/async_renderer.py:429-437
  - 证据:
    ```python
    if not self.shared_pyqt_context.makeCurrent(self.surface):
        logger.error(f"Failed to make context current on RenderThread")
        return                                    # 直接 return，未走 _cleanup
    self.ctx = moderngl.create_context()          # 抛错则异常直接冲出 run()
    self.fb_manager = FramebufferManager(self.ctx)
    self._load_utility_shaders()                  # passthrough 等编译无 try 包裹
    ```
  - 影响: makeCurrent 失败、`create_context` 或内建着色器编译抛错时，`run()` 结束但 `is_running` 仍为 True、`_stop_event` 未消费、`surface`/`shared_pyqt_context` 未释放；调用方 `stop()` 要白等 10 秒超时。
  - 建议: 将初始化段包入 try/except 并在失败路径统一调用 `_cleanup()`（或至少 doneCurrent + 释放 surface）后再退出。
  - 置信度: 中

- [AUD-0212] [Suggestion] `_do_render_video_frame` 在复制前直接改写调用方持有的 `render_params` 字典（跨线程写 UI 侧对象）
  - 位置: src/features/color/core/async_renderer.py:5229-5231
  - 证据:
    ```python
    viewport_size = render_params.get('viewport_size', (width, height))
    if viewport_size[0] <= 0 or viewport_size[1] <= 0:
        render_params['viewport_size'] = (width, height)   # 此处 dict(render_params) 拷贝（5253 行）尚未发生
    ```
  - 影响: `render_params` 是 `data['render_params']` 引用，属 UI 线程构造的载荷；渲染线程对其实施写操作。若调用方复用同一 dict 提交后续帧，会产生跨线程数据竞争（虽然仅发生在非法尺寸时）。
  - 建议: 一进入方法就 `render_params = dict(render_params)` 再做修正。
  - 置信度: 高

- [AUD-0213] [Suggestion] `_cleanup` 尾部 fbo_pair/tex_pair/pyramid/reference 释放无 try 包裹，单个失败中断剩余清理与 doneCurrent
  - 位置: src/features/color/core/async_renderer.py:6206-6213
  - 证据:
    ```python
    for fbo in self.fbo_pair:
        if fbo: fbo.release()
    for tex in self.tex_pair:
        if tex: tex.release()
    for tex in self.pyramid_textures:
        if tex: tex.release()
    for tex in self.reference_pyramid_textures:
        if tex: tex.release()
    ```
  - 影响: 同函数内其余释放均带 try/except，唯独这一段没有；任一 release 抛异常（如纹理已被外部销毁）会跳过 `shared_pyqt_context.doneCurrent()`（6214 行）与 backend 台账释放（6217-6219 行），上下文残留 current。
  - 建议: 与本函数其他段落一致，逐个 try/except。
  - 置信度: 高

- [AUD-0214] [Suggestion] 类型注解缺失（仅记录）
  - 位置: src/features/color/core/async_renderer.py（示例：`submit`(532)、`_handle_command`(912)、`_do_update_uniforms`(1159)、`_do_upload`(4166)、`_generate_matte`(3249)、`_ensure_fbos`(5710)）
  - 证据:
    ```python
    def submit(self, cmd_type, data=None):
    def _handle_command(self, cmd_type, data):
    def _do_upload(self, img_u8, is_reference=False, original_size=None, is_video=False):
    ```
  - 影响: 核心入口无参数/返回类型标注，与仓库强类型规范不一致，影响可维护性与 IDE 检查。
  - 建议: 按项目规范补齐 `-> None` / `Optional[...]` 等注解（新代码如 `_do_render_gpu_scope` 已达标，可作参照）。
  - 置信度: 高

### 通过项

- 线程安全（GL 调用位置）: 所有 GL 资源创建/释放都在渲染线程 `run()`/`_do_*`/`_cleanup` 内、GL mutex 下执行；`__init__` 中的 QOffscreenSurface/共享上下文创建属于 Qt 要求的 GUI 线程操作，且正确处理了 doneCurrent/makeCurrent 包夹与 moveToThread。
- 线程安全（队列与 lane）: `submit`/`_drain_pending_slots`/`discard_pending_*` 全程持 `_pending_lock`；drain 哨兵去重有独立锁；full-res 取消注册表全程持 `_full_res_cancel_lock`，register/cancel/finish 生命周期闭合（含 resize 失败与 shutdown 路径）。
- GPU 资源 deferred-release（前缓冲）: `fbo_pair`/`tex_pair` 替换走 `garbage` 列表 2 秒延迟释放，`_process_garbage(force=True)` 在 shutdown 兜底。
- GPU 资源生命周期（GpuDecodedFrame）: lane 合并替换（submit 内）、生成号过期丢弃、wrap 失败/异常、`_release_queued_gpu_frames`、`_cleanup` 均调用 `safe_release_gpu_frame`；G-P0-05 前帧保留策略（绑定成功后才释放旧帧、失败恢复）实现正确。
- 全分辨率导出路径: PBO ring 异步读回 + resize 前 fence drain + shutdown drain + 取消复查 + 进度钳制；失败路径均发射终止信号并清理请求记账，源 FBO 不会在 DMA 进行中被释放。
- 取消路径: `_do_render_impl` 每层前检查 `is_cancelled`；`_do_bake_layer` 前缀循环检查 `_cancel_requested`；`_do_bake_layer` 的临时纹理（bake/scratch/temp_full）有完整 try/finally 释放。
- 内嵌 GLSL 正确性: `mask_combine_fs`/`mask_preview_fs`/`matte_sample_fs`（838-907 行）浮点字面量均带 `.0`、无循环、无动态注入变量；`clamp(mask, 0.0, 1.0)` 仅用于 UI matte 预览显示（合理）；UV 映射均基于 u_uv_rect 显式换算。
- 跨上下文同步: 前缓冲发布用 GLsync fence + glFlush 替代每帧 finish；fence 的 retire/publish 只在渲染线程持锁执行，macOS 下保留 ctypes 指针避免 int() 转换异常。
- 信号防递归: 本文件不做 UI 控件程序化更新，无递归风险；信号载荷为不可变或新建对象（int/新建 ndarray/描述符）。
- 状态提交一致性: 渲染器只消费命令负载，不直接改 model；降噪参数经 `set_video_denoise_params` 序列化为 dict 副本后投递，无绕过。
- 输入验证（部分）: `_materialize_contiguous_semiplanar_frame` 对半平面尺寸奇偶、缓冲大小、ndim 做了完整校验；`_upload_3dlut` 对 LUT_3D_SIZE/条目数做校验后才分配。
- 性能模式: 持久化 raw_view/ping/pong 纹理、YUV ring、FBO/VAO 缓存、`video_render_sync_mode` 可配置同步均为有效优化；scope lane 的 `ctx.finish()` 属文档化的限频低优先级通道；flash 诊断 readback 仅在 `_flash_diag.enabled()` 时执行。
- 停止路径: `stop()` 带超时并记录可见告警；shutdown 顺序（先释放 GPU 资源再 doneCurrent）正确。
