# batch-398 审查报告 [P2] src/shared/video

- 批次：batch-398（编号区间 AUD-39800–AUD-39899）
- 审查日期：2026-09-28
- 审查文件：1 个（3,294 行，已全文通读）
- 交叉验证：调用方 `src/features/color/core/async_renderer.py`、`src/features/composite/nodes/video_frame_node.py`、`src/features/edit/core/timeline_clip_frame_provider.py`、`src/features/edit/core/edit_gpu_preview_renderer.py`、`src/shared/video/video_playback.py`（只读）；moderngl 5.12 关键行为（Framebuffer.use 设 viewport、Texture.write(buffer)、VertexArray.release 不释放 index buffer）已在 venv 中实测验证。

## src/shared/video/yuv_gpu_upload.py (行数: 3294)

### 发现

- [AUD-39800] [Critical] BT.601 矩阵系数误用 PAL 模拟 YUV 值，SD 内容颜色系统性错误
  - 位置: src/shared/video/yuv_gpu_upload.py:1998（副本: 2147、2320、2485）
  - 证据:
    ```python
    // BT.601 matrix (SD video)
    const mat3 bt601 = mat3(
        1.0, 1.0, 1.0,
        0.0, -0.3945, 2.032,
        1.140, -0.581, 0.0
    );
    ```
  - 影响: shader 的色度归一化是数字 YCbCr 的 `(C-128<<s)/(224<<s)`，此归一化下 BT.601 正确系数应为 R:+1.402 / G:-0.344·U-0.714·V / B:+1.772；而 1.140/-0.3945/-0.581/2.032 是 PAL 模拟 YUV 矩阵。红欠饱和约 19%、蓝过饱和约 15%。BT.601 路径真实可达：`src/shared/video/video_playback.py:1728-1729` 将 'smpte170m'/'bt470bg' 映射为 MATRIX_BT601，`video_frame_node.py:741` 同样返回 2。四处 shader 副本（float 3-plane / r16ui 3-plane / nv12 r16ui / nv12 float）全部同错。BT.709/BT.2020 系数已核对无误，仅 601 错。
  - 建议: 四处 bt601 矩阵改为 `mat3(1.0,1.0,1.0, 0.0,-0.3441,1.7720, 1.4020,-0.7141,0.0)`；并以 CPU 参考值（如 run_r16ui_self_test 的做法）补一条 601 自检用例。
  - 置信度: 高

- [AUD-39801] [Major] `_upload_planar_r8` 回退丢失 subsampling，R8 池不可用时 8-bit 422/444 崩溃
  - 位置: src/shared/video/yuv_gpu_upload.py:1610
  - 证据:
    ```python
    if self._r8_pool is None:
        # Fallback to float16 if R8 pool unavailable
        return self.upload_yuv420(y, u, v, frame_index, bit_depth=8, a=a)
    ```
  - 影响: `get_upload_mode(8)` 无条件返回 'r8_direct'（1868-1869），不检查 `_r8_pool` 是否存在。R8 探测失败的 GL 上下文上，`upload_yuv_planar(..., subsampling='422'/'444', bit_depth=8)` 会进入 `_upload_planar_r8` 再回退到硬编码的 `upload_yuv420`，后者在 1212/1297 处断言 `u_width == y_width // 2`（444）或 `(y_width+1)//2`（422）失败 → AssertionError，8-bit 422/444 视频整帧渲染失败；420 内容不受影响。回退同时丢失 alpha_bit_depth 参数。
  - 建议: 回退改为按 subsampling 分发到 `upload_yuv422`/`upload_yuv444`，或 `_upload_planar_r8` 接收 subsampling 参数；`get_upload_mode` 在 `_r8_pool is None` 时返回 'float16'。
  - 置信度: 高

- [AUD-39802] [Major] `run_r16ui_self_test` 覆盖共享的 yuv_matrix/color_range 且不恢复，首帧颜色错误
  - 位置: src/shared/video/yuv_gpu_upload.py:2980
  - 证据:
    ```python
    self.set_yuv_matrix(self.MATRIX_BT709)
    self.set_color_range(self.RANGE_LIMITED)

    fbo.use()
    self._ctx.viewport = (0, 0, test_w, test_h)
    ```
  - 影响: 调用方（`video_frame_node.py:2009-2010`、`async_renderer.py:4502-4503`）在 upload 前设置矩阵/范围。首次遇到 >8-bit 内容时 `_auto_enable_r16ui`（917/1009 等）在 upload 过程中触发自检，将矩阵重置为 BT709/limited 且从不恢复（2966 保存的 `old_self_test` 也从未使用）→ 该帧以错误矩阵渲染；若调用方仅设置一次矩阵，后续帧持续错误。BT.2020 HDR 片源首帧会以 BT.709 呈现（色相明显偏移）。
  - 建议: 自检前保存 `self._yuv_matrix`/`self._color_range` 并在 finally 中恢复；自检使用独立的临时 Program uniform 而非实例状态。
  - 置信度: 高（状态覆盖确定；可见影响取决于调用方重设频率，主流调用方每帧重设，首帧必然错色）

- [AUD-39803] [Major] `run_r16ui_self_test` 异常路径泄漏 FBO 与输出纹理
  - 位置: src/shared/video/yuv_gpu_upload.py:3073
  - 证据:
    ```python
    output_tex = self._ctx.texture((test_w, test_h), components=4, dtype='f1')   # 2975
    fbo = self._ctx.framebuffer(color_attachments=[output_tex])                  # 2978
    ...
    except Exception as e:
        self._r16ui_self_test_passed = False
        self._r16ui_disabled_reason = f"exception: {e}"
        logger.error(f"R16UI self-test exception: {e}", exc_info=True)
        return False
    ```
  - 影响: `_cleanup(fbo, output_tex)` 只在 try 块内的正常路径（3003、3049）调用；fbo/output_tex 创建后任何异常（program 编译失败、read 失败等）都绕过清理 → 每次自检失败泄漏一个 4x4 RGBA 纹理 + FBO。
  - 建议: 将创建与校验包进 try/finally，finally 中调用 `_cleanup`。
  - 置信度: 高

- [AUD-39804] [Major] 纹理池在容量未满时优先抢占 busy 槽位，稳态下每 (plane_type,尺寸) 仅 1 张纹理
  - 位置: src/shared/video/yuv_gpu_upload.py:324
  - 证据:
    ```python
    # 2. 查找匹配尺寸、通道数和平面类型但非空闲的槽位（复用最旧的）
    oldest_slot: Optional[YUVTextureSlot] = None
    oldest_time = float('inf')
    ...
    if oldest_slot:
        oldest_slot.frame_index = frame_index
        oldest_slot.last_used_time = next(_SLOT_ACQUIRE_SEQ)
        return oldest_slot.texture

    # 3. 创建新槽位
    if len(slots) < max_size:
    ```
  - 影响: 槽位一旦使用 `frame_index >= 0` 永不释放（无归还接口），step 1 永远 miss；step 2 在池远未满（step 3 之前）就抢占最旧 busy 槽位。结果：稳态下每个 (plane_type, 尺寸) 组合只存在 1 张纹理，`pool_size=5` 形同虚设，`get_stats` 的 active_frames 恒为全部槽位。当前"upload→立即 render"的调用方恰好安全，但任何 upload(A)→upload(B)→render(A) 顺序（共享 uploader 的 async_renderer 多轨道任务队列、`enable_pbo` 注释中明确提到的 multi-track 4K 场景）会让 A 显示 B 的内容。
  - 建议: 交换 step 2/3 顺序（容量未满时先创建新槽位，仅在池满时按 LRU 抢占），并提供显式 `mark_free(frame_index)` 归还接口。
  - 置信度: 高（机制确定）；触发场景中置信度

- [AUD-39805] [Major] `_last_upload_mode` 全局单值决定渲染 shader 分发，混合位深多轨道时采样器类型不匹配
  - 位置: src/shared/video/yuv_gpu_upload.py:2615（同型: 2793）
  - 证据:
    ```python
    if self._last_upload_mode == 'r16ui_direct':
        return self.render_yuv_r16ui_to_rgb(
            y_tex, u_tex, v_tex, output_fbo,
            chroma_uv_scale, chroma_uv_offset, bit_depth,
            a_tex=a_tex, ...
    ```
  - 影响: render 路径不看传入纹理的 dtype，只看"最后一次上传"的模式。时间线混排 8-bit（f1 纹理）与 10-bit（R16UI 纹理）素材时，若上传顺序与渲染顺序不一致（共享 `self._yuv_gpu_uploader` 的 async_renderer 的 upload 任务与 render walk 分离），8-bit 纹理会被 `usampler2D` 采样（GL 未定义行为，通常输出垃圾或驱动报错），反向则 r16ui 纹理被 sampler2D 采样。`async_renderer.py:3091` 用 `get_last_upload_timing().get('mode')` 做同样假设，脆弱性被上游复制。
  - 建议: 由调用方显式传入 mode（或按纹理 dtype/`texture._dtype` 探测），`render_*_to_rgb` 增加 `mode` 参数替代全局状态分发。
  - 置信度: 机制高；实际触发取决于渲染器队列顺序，中

- [AUD-39806] [Major] `render_nv12_to_rgb` 每帧创建/销毁临时 VBO/IBO/VAO，且异常路径泄漏
  - 位置: src/shared/video/yuv_gpu_upload.py:2834
  - 证据:
    ```python
    vbo = self._ctx.buffer(vertices.tobytes())
    ibo = self._ctx.buffer(indices.tobytes())
    vao = self._ctx.vertex_array(
        shader,
        [(vbo, '2f 2f', 'in_vert', 'in_texcoord')],
        ibo
    )
    vao.render(moderngl.TRIANGLES, vertices=6)
    # 清理临时 buffer
    vbo.release()
    ibo.release()
    vao.release()
    ```
  - 影响: NV12 float 路径是 8-bit 无 R8 池时的每帧热路径（`timeline_clip_frame_provider.py:1824`、`edit_gpu_preview_renderer.py:2783+` 每帧调用）。每帧 3 次 GL 对象分配 + 3 次释放（60fps 下每秒 180 个 GL 对象 churn），与同文件 G-P0-01 修复的"每帧重编译"同类性能反模式；render 抛异常时三个对象全部泄漏（无 try/finally）。
  - 建议: 按 program 缓存 VAO（shader 已缓存于 `_nv12_program`，quad 几何恒定，VAO 可与 `_quad_buffer` 共享并挂到 program→VAO 字典）；临时对象路径加 try/finally。
  - 置信度: 高

- [AUD-39807] [Suggestion] 输入验证依赖 assert（-O 下被剥离），且部分路径无校验
  - 位置: src/shared/video/yuv_gpu_upload.py:929
  - 证据:
    ```python
    # 验证尺寸比例（420 格式）
    assert u_width == y_width // 2, f"U width mismatch: {u_width} vs {y_width//2}"
    assert u_height == y_height // 2, f"U height mismatch: {u_height} vs {y_height//2}"
    ```
  - 影响: 帧数据是外部解码输出（外部数据须验证铁律）。`python -O` 下 assert 全部剥离，尺寸不匹配静默走错路径。另有三处缺口：`upload_yuv420` float 路径未验证 v.shape（只验 u）；`upload_yuv420_r16ui`（1737-1793）完全无尺寸校验；`bit_depth` 未验证，传 0/负数时 `max_val = (1 << 0) - 1 = 0` → `np.divide` 除零产生 inf/NaN 纹理数据。
  - 建议: 尺寸/位深校验改为显式 `raise ValueError`；统一补齐 v 形状与 bit_depth ∈ {8,10,12,16} 检查。
  - 置信度: 高

- [AUD-39808] [Suggestion] 类型注解不精确（None 默认值非 Optional、裸 tuple、返回类型含 str 却标注 float）
  - 位置: src/shared/video/yuv_gpu_upload.py:84
  - 证据:
    ```python
    @dataclass
    class YUVTextureSlot:
        texture: moderngl.Texture = None
    ```
  - 影响: 与模块文档"完整类型注解，强类型设计"目标不符。同类问题：`upload_yuv420/upload_yuv422/... -> tuple`（875 等多处裸 tuple，未写元素类型）；`get_last_upload_timing() -> Dict[str, float]`（1393）实际含 `'mode': str`；`_cleanup(fbo, tex) -> None`（3213）参数无注解；`__exit__` 参数无注解（3167）。
  - 建议: `texture: Optional[moderngl.Texture] = None`；返回 `Tuple[moderngl.Texture, ...]`；timing 类型改 `Dict[str, Any]` 或拆分。
  - 置信度: 高

- [AUD-39809] [Suggestion] 死代码与未使用导入
  - 位置: src/shared/video/yuv_gpu_upload.py:683
  - 证据:
    ```python
    # 格式: {(nbytes, dtype_str): np.ndarray}
    self._staging_buffers: Dict[Tuple[int, str], np.ndarray] = {}
    ```
  - 影响: `_staging_buffers` 只在 `__init__`（683）和 `release`（3152 clear）出现，从未被读写——实际 staging 走 `_staging_rings`；`YUVUploadStats.bandwidth_saved_mb`（115）从未被更新，统计恒为 0；`Union`（41）导入未使用。
  - 建议: 删除 `_staging_buffers` 与 `Union`；`bandwidth_saved_mb` 要么用 `calculate_bandwidth_saved` 累积，要么删除字段。
  - 置信度: 高

- [AUD-39810] [Suggestion] 三处局部 IBO 未跟踪，release() 不释放（GL 对象泄漏）
  - 位置: src/shared/video/yuv_gpu_upload.py:2575（同类: 2255、2413）
  - 证据:
    ```python
    self._quad_buffer = self._ctx.buffer(vertices.tobytes())
    ibo = self._ctx.buffer(indices.tobytes())

    if self._program:
        self._quad_vao = self._ctx.vertex_array(
    ```
  - 影响: 实测确认 moderngl 5.12 的 `VertexArray.release()` 不会释放 index buffer。`_init_quad_buffer`/`_init_r16ui_quad`/`_create_nv12_r16ui_shader` 各创建一个局部 `ibo` 仅由 VAO 持有；`release()`（3079-3161）释放了 VAO 与 `_quad_buffer` 但从不释放这三个 IBO → 每个 uploader 实例生命周期结束后残留 3 个 24 字节 GL buffer 对象。量级小，但违背 deferred-release 纪律。
  - 建议: 将 IBO 存为成员（如 `self._quad_ibo`）并在 release() 中统一释放。
  - 置信度: 高（moderngl 行为已实测）

- [AUD-39811] [Suggestion] `enable_pbo` 含 GL 对象创建/销毁却无线程契约守卫，与并发上传存在竞态
  - 位置: src/shared/video/yuv_gpu_upload.py:781
  - 证据:
    ```python
    if enabled and (
        self._pbo_pool is None or self._pbo_slot_count != desired_slots
    ):
        try:
            if self._pbo_pool is not None:
                self._pbo_pool.release()
            test_pbo = self._ctx.buffer(reserve=1024)
    ```
  - 影响: 该方法创建/释放 GL buffer（必须在渲染/GL 线程，铁律"UI 线程不得操作 OpenGL"），但无断言或文档化的线程检查。`_upload_to_texture`（853-855）先读 `_pbo_enabled` 再读 `_pbo_pool`；若在渲染线程上传期间从其他线程调用 `enable_pbo(False)`，释放与 `upload_to_texture` 竞态 → InvalidObject 异常。
  - 建议: 在文档与实现上强制仅渲染线程调用（如 debug 断言 GL 线程 id），或通过任务队列转发到渲染线程执行。
  - 置信度: 中（当前调用方 `edit_gpu_preview_renderer.py:2473` 在启动配置阶段调用，暂无实际竞态）

- [AUD-39812] [Suggestion] `_upload_alpha_plane` 宽泛 except 吞掉领域错误
  - 位置: src/shared/video/yuv_gpu_upload.py:1496
  - 证据:
    ```python
    except Exception as e:
        logger.warning(f"Alpha plane upload failed (mode={mode}): {e}")
        return None
    ```
  - 影响: 捕获所有异常并降级为"无 Alpha 继续"（静默丢弃透明度信息）。虽然记录了 warning，但把尺寸不匹配、dtype 错误、GL 上下文失效等本质不同的问题统一掩盖，违反"不得宽泛 except 掩盖领域错误"铁律的精神。
  - 建议: 收窄为可预期的异常类型；尺寸/dtype 校验前置为显式 ValueError。
  - 置信度: 高

- [AUD-39813] [Suggestion] `render_yuv_to_rgb` float 路径硬编码 source_uv (1,1)/(0,0)，与 r16ui 路径 API 不对称
  - 位置: src/shared/video/yuv_gpu_upload.py:2645
  - 证据:
    ```python
    self._program['source_uv_scale'].value = (1.0, 1.0)
    self._program['source_uv_offset'].value = (0.0, 0.0)
    self._program['chroma_uv_scale'].value = chroma_uv_scale
    ```
  - 影响: float shader 源码含 source_uv_scale/offset 裁剪 uniform，r16ui 版本（2663-2664）暴露为参数，float 版本却硬编码——需要视口裁剪的 float 路径调用方无法传参（async_renderer 为此绕过此方法自行设 uniform，3082+），API 不一致且易踩坑。
  - 建议: `render_yuv_to_rgb` 增加 `source_uv_scale/source_uv_offset` 参数并与 r16ui 版本对齐。
  - 置信度: 高

### 通过项
- 线程安全：模块契约明确所有 GL 操作限定渲染线程；池/staging/统计均为单线程访问，无共享可变状态跨线程（enable_pbo 例外见 AUD-39811）
- GPU 资源生命周期：`YUVTexturePool` 具备 weakref.finalize GC 后备释放与显式 release_all；`release()` 覆盖全部 program/VAO/pool/PBO 并置空；上下文管理器 `__exit__` 正确（IBO 例外见 AUD-39810）
- GLSL 规范：所有数字字面量显式 `.0`；无循环因此无非常量循环上界问题；四份 shader 结构一致
- HDR 语义：`upload_half_planar` 对浮点样本域不做整数归一化/无 clamp，shader 端 gbr_mode 旁路正确；YUV→RGB 路径无不当 clamp(0,1)
- YUV 归一化与 range 数学：BT.709/BT.2020 系数及 219/224 limited-range、16/128 基准的深度缩放（exp2(shift)）经推导核对正确；R16UI 直传路径的 shader 侧归一化与 CPU 参考一致（自检参考实现 3018-3030 已核对）
- 信号防递归：不适用（无 Qt 控件/信号）
- 状态提交一致性：不适用（无 undo/redo/revision/持久化逻辑）
- 输入验证（部分）：NV12/P010 的 UV 形状用显式 `raise ValueError`（1030/1134/1696）；`topos_chroma_sampling_params` 验证正尺寸与 siting 枚举
- PBO 路径：`texture.write(buffer)` 与 orphan 用法在 moderngl 5.12 实测可用；按大小分池 + 旋转索引正确；`enable_pbo(False)` 释放大缓冲避免内存驻留
- LRU 序号设计：`_SLOT_ACQUIRE_SEQ` 单调计数替代 time.time() 规避 Windows 时钟精度平局导致"释放刚发出纹理"的问题，注释充分
- 纹理创建失败路径返回 None 并由上层 raise RuntimeError，不吞错误（纹理创建的宽泛 except 有 error 日志，可接受）

## 汇总

| 严重度 | 数量 |
|--------|------|
| Critical | 1 |
| Major | 6 |
| Suggestion | 7 |
