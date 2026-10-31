# 批次报告：batch-397 [P2] src/shared/video//

- 编号区间：AUD-39700–AUD-39799
- 审查文件：video_texture_pool.py (378)、video_upload.py (900)、yuv_gpu_self_test.py (332)
- 汇总：Critical 0 · Major 5 · Suggestion 10

交叉验证说明：RenderCommand.UPLOAD_VIDEO_PLANES / UPLOAD_DECODED_VIDEO_FRAME / UPLOAD_VIDEO_FRAME 已对照 `src/shared/video/render_commands.py:17-19` 确认存在；YUVGPUUploader 的 `upload_yuv444` / `set_yuv_matrix(int)` / `set_color_range(int)` / `render_yuv_to_rgb` / `run_r16ui_self_test` / `_r16ui_disabled_reason` 已对照 `src/shared/video/yuv_gpu_upload.py` 确认存在且签名匹配；DecodedFrame 字段已对照 `src/shared/media/frame.py:305-340` 确认。`video_upload.py` 中 PBOUploader / YuvPlaneUploader / UploadPerformanceTester / dispatch_upload_command 在 src/ 下无生产调用点（仅 tests/ 引用，PBOUploader 文档串亦自述为预研/向后兼容，新代码应使用 yuv_gpu_upload.py），已在相关发现中注明影响面。

## src/shared/video/video_texture_pool.py (行数: 378)

### 发现

- [AUD-39700] [Major] 池满换尺寸时新建纹理失败会留下「已释放纹理 + 旧元数据」的损坏槽位，后续 acquire 将返回已释放纹理
  - 位置: src/shared/video/video_texture_pool.py:194-226
  - 证据:
    ```python
            # 释放旧纹理
            if slot.texture:
                try:
                    slot.texture.release()
                except Exception:
                    pass

            # 创建新尺寸纹理
            try:
                texture = self._ctx.texture(
                    size=(width, height),
    ...
            except Exception as e:
                logger.error(f"VideoTexturePool: Failed to recreate texture: {e}")
                return None
    ```
  - 影响: 第 4 步先 `slot.texture.release()` 再建新纹理；若 `self._ctx.texture(...)` 失败（GPU 内存不足等），槽位仍持有已释放的 texture 对象，且 `width/height/components/dtype/frame_index` 保留旧值。下一次 `acquire` 以旧尺寸请求时，第 2 步（复用最旧，不检查可用性）会 `is_size_match` 命中该槽并直接返回已释放纹理；调用方（如 async_renderer.py:4300/4420）对其 `write()` 会抛异常或产生未定义行为，造成渲染错误/丢帧，且该损坏状态会一直存续到 `release_all`。
  - 建议: 重建失败时不要保留半更新状态——先将 `slot.texture = None`、`slot.frame_index = -1`（或将旧纹理的释放推迟到新纹理创建成功之后：先建新、成功后再 release 旧、再替换槽位字段），保证 acquire 失败后池内不残留已释放对象。
  - 置信度: 高

- [AUD-39701] [Major] weakref.finalize 后备释放在任意触发 GC 的线程上直接调用 GL release，违反「GL 仅渲染线程」铁律
  - 位置: src/shared/video/video_texture_pool.py:99-103, 361-378
  - 证据:
    ```python
        self._finalizer = weakref.finalize(
            self,
            self._cleanup_on_gc,
            self._slots  # 引用 slots，但 finalizer 不直接访问 self
        )
    ...
        @staticmethod
        def _cleanup_on_gc(slots: List[VideoTextureSlot]) -> None:
            for slot in slots:
                try:
                    if slot.texture is not None:
                        slot.texture.release()
                except Exception:
                    pass  # GC 清理时忽略所有异常
    ```
  - 影响: CPython 的 GC 在分配对象的线程上运行，包括 UI 线程或解释器关闭阶段。`texture.release()` 内部执行 glDeleteTextures，在无当前上下文/错误线程上调用属于未定义行为；当前实现异常被静默吞掉，实际效果退化为「泄漏 + 潜在驱动层错误」，且与项目 deferred-release（渲染线程统一释放）模式冲突。
  - 建议: `_cleanup_on_gc` 不直接 release，改为将残留纹理投递到渲染线程的 deferred-release 队列（项目已有该机制）；若上下文已销毁则跳过。至少应记录 warning 而非完全静默。
  - 置信度: 中（作为 GC 兜底的意图已注释说明，但跨线程 GL 调用风险真实存在）

- [AUD-39702] [Suggestion] resize_pool 从尾部 pop 释放的是「最新」槽位且不检查 is_available，与 LRU 语义相悖
  - 位置: src/shared/video/video_texture_pool.py:317-323
  - 证据:
    ```python
        # 如果当前槽位超过新大小，释放多余的
        while len(self._slots) > self._pool_size:
            slot = self._slots.pop()
            if slot.texture:
                try:
                    slot.texture.release()
                except Exception:
                    pass
    ```
  - 影响: `pop()` 移除的是最近创建的槽位而非最旧（LRU）槽位；且被释放的槽位可能正持有当前帧（frame_index >= 0），导致在用纹理被释放。当前无生产调用点，风险为潜在。
  - 建议: 按 `last_used_time` 升序选出最旧且 `is_available()` 的槽位释放；若全部在用则推迟收缩。
  - 置信度: 高（行为确定，影响小）

- [AUD-39703] [Suggestion] 类型注解与死代码：texture 默认 None 未标 Optional；_current_size 赋值后从未使用
  - 位置: src/shared/video/video_texture_pool.py:46, 95
  - 证据:
    ```python
    @dataclass
    class VideoTextureSlot:
        """视频纹理槽位"""
        texture: moderngl.Texture = None
    ...
        self._pool_size = pool_size
        self._current_size = (0, 0)  # 当前帧尺寸
    ```
  - 影响: `texture: moderngl.Texture = None` 与实际默认值矛盾，静态检查（mypy）报错并误导调用方认为纹理恒非空；`_current_size` 是死属性。
  - 建议: 改为 `texture: Optional[moderngl.Texture] = None`；删除 `_current_size` 或实现其用途。
  - 置信度: 高

### 通过项

- 信号防递归：本模块无 Qt 信号/控件，不适用
- 状态提交一致性：不参与 undo/redo/revision/持久化，不适用
- LRU 序号设计正确：`_SLOT_ACQUIRE_SEQ = itertools.count(1)` 严格递增，避免了 time.time() 精度平局问题（注释明确）
- release_all 与 finalizer 共享同一 `_slots` list 对象，`clear()` 后 GC 兜底看到空列表，不会双重释放
- `__exit__` 无论异常与否均调用 release_all，上下文管理器路径正确
- acquire 的纹理创建失败路径有 try/except + 日志并返回 None，不会向调用方抛裸异常
- GPU 资源生命周期（常规路径）：release_all/resize_pool/acquire 重建均有 release 调用与异常保护
- 类型注解总体完整（除 AUD-39703 所列）

## src/shared/video/video_upload.py (行数: 900)

### 发现

- [AUD-39710] [Major] upload_planes 8-bit 路径向 f1(R8) 纹理写入 float32 数据，字节数与像素格式不匹配，8-bit planar 上传结果必然错误
  - 位置: src/shared/video/video_upload.py:355-360, 380-382
  - 证据:
    ```python
        if format_info.bit_depth <= 8:
            tex_dtype = 'f1'  # uint8 -> R8
            # 需要归一化到 float
            y_data = y_plane.astype(np.float32) / 255.0
            u_data = u_plane.astype(np.float32) / 255.0
            v_data = v_plane.astype(np.float32) / 255.0
        ...
        y_tex = self._ctx.texture((y_width, y_height), 1, dtype=tex_dtype)
        y_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        y_tex.write(y_data.tobytes())
    ```
  - 影响: dtype='f1' 创建的是 R8（每像素 1 字节）纹理，而 y_data 是 float32（每像素 4 字节）。`write()` 只会取缓冲区前 w*h 字节（即前 1/4 像素的 float 位模式碎片）当作像素值，GL 端按 UNSIGNED_BYTE 解释为乱码——8-bit YUV planar 格式（yuv420p/yuv422p）渲染颜色完全错误。正确做法二选一：R8 纹理直接写原始 uint8 平面（硬件自动归一化），或归一化后转 float16 用 f2。注：本模块当前无生产调用点（仅 tests/ 引用），属预研遗留代码，故未升为 Critical。
  - 建议: 8-bit 分支保留 `dtype='f1'` 时直接 `y_tex.write(np.ascontiguousarray(y_plane).tobytes())`（shader 采样即得 /255 归一化值）；或统一走 float16/f2 路径。补充针对 8-bit planar 的数值校验测试。
  - 置信度: 高

- [AUD-39712] [Major] create_yuv_program 用 CWD 相对路径加载外部 shader，启动目录不同时静默降级到内置 shader
  - 位置: src/shared/video/video_upload.py:410-414, 436-438
  - 证据:
    ```python
        # 加载 shader 源码
        shader_path = 'src/features/color/shaders/video_yuv.glsl'

        try:
            with open(shader_path, 'r') as f:
                shader_source = f.read()
        ...
        except FileNotFoundError:
            logger.warning(f"Shader file not found: {shader_path}, using built-in shader")
            return self._create_builtin_yuv_program()
    ```
  - 影响: 该文件在仓库中真实存在，但相对路径取决于进程启动目录（打包/其他 CWD 启动时必然 FileNotFoundError），随后静默使用内置 shader——若外部 shader 与内置版本行为有差异（外部文件可被独立修改），实际加载的转换逻辑随启动方式漂移，属隐藏功能缺陷。
  - 建议: 以 `pathlib.Path(__file__).resolve()` 推导仓库根/资源目录的绝对路径，或通过统一的 shader 资源加载器获取。
  - 置信度: 高（机制确定；实际后果取决于内外 shader 差异）

- [AUD-39711] [Suggestion] upload_planes 每次调用都创建并释放 3 张纹理，无池化，与项目视频纹理池设计初衷相悖
  - 位置: src/shared/video/video_upload.py:335-390
  - 证据:
    ```python
        # 释放旧纹理
        self._release_textures()
        ...
        # 创建纹理
        y_tex = self._ctx.texture((y_width, y_height), 1, dtype=tex_dtype)
        ...
        u_tex = self._ctx.texture((u_width, u_height), 1, dtype=tex_dtype)
        ...
        v_tex = self._ctx.texture((v_width, v_height), 1, dtype=tex_dtype)
    ```
  - 影响: 类自述用于「高性能视频帧上传」，但逐帧 3 次 texture 创建 + 3 次 release 的 GL 对象 churn 在 4K 下开销显著；VideoTexturePool 正是为消除该模式而存在。当前无生产调用点（预研模块），风险为潜在。
  - 建议: 接入 VideoTexturePool 或按 (尺寸, dtype) 缓存平面纹理，尺寸不变时仅 write。
  - 置信度: 高（行为确定，影响为潜在）

- [AUD-39713] [Suggestion] 类常量 BT709/BT2020/BT601_MATRIX 按行向量存放，与实际转换（R=1.0*y+1.5748*v）不一致，作为矩阵使用会得到错误颜色
  - 位置: src/shared/video/video_upload.py:277-296
  - 证据:
    ```python
    BT709_MATRIX = np.array([
        [1.0, 1.0, 1.0],
        [0.0, -0.1873, 1.8556],
        [1.5748, -0.4681, 0.0]
    ], dtype=np.float32)
    ```
  - 影响: 正确的 BT.709 矩阵第一行应为 (1.0, 0.0, 1.5748)（对照本文件 GLSL 列主序构造与 yuv_gpu_self_test.py 的 CPU 参考均已核实）。当前常量若被 `M @ vec(y,u,v)` 使用会得到 R=y+u+v 的错误结果。文件内未参与计算（仅展示），属误导性地雷。
  - 建议: 转置为正确布局或删除这些常量、直接引用单一权威定义（与 GLSL/CPU 参考同源）。
  - 置信度: 高（布局与正确值不一致已核实；无人当前使用）

- [AUD-39714] [Suggestion] create_yuv_frame_from_planes 位深探测硬编码 '10le'，12/16-bit 格式误标 bit_depth=8
  - 位置: src/shared/video/video_upload.py:786
  - 证据:
    ```python
    metadata = VideoMetadata(
        ...
        pixel_format=pixel_format,
        bit_depth=10 if '10le' in pixel_format else 8
    )
    ```
  - 影响: 'yuv420p12le'、'yuv444p16le' 等格式会得到 bit_depth=8，下游按 8-bit 归一化（/255）处理 12/16-bit 数据导致亮度和色度整体错误。当前仅测试代码调用。
  - 建议: 用 `get_pixel_format(pixel_format).bit_depth`（同文件其他路径已在用）或解析格式名中的位深数字。
  - 置信度: 高（逻辑确定）

- [AUD-39715] [Suggestion] upload_planes 中途异常时已创建纹理未登记进 _textures，异常路径泄漏
  - 位置: src/shared/video/video_upload.py:379-395
  - 证据:
    ```python
        # 创建纹理
        y_tex = self._ctx.texture((y_width, y_height), 1, dtype=tex_dtype)
        y_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        y_tex.write(y_data.tobytes())

        u_tex = self._ctx.texture((u_width, u_height), 1, dtype=tex_dtype)
        ...
        # 存储纹理引用
        self._textures['y'] = y_tex
    ```
  - 影响: y_tex 创建成功后若 u_tex/v_tex 的创建或写入抛出 GL 错误，异常向上传播，此时 y_tex 只存在于局部变量，`self._textures` 尚未登记，下次 `_release_textures()` 无法释放它 → GPU 内存泄漏。
  - 建议: 每创建一张立即登记入 `self._textures`，或用 try/except 在异常时释放已建纹理后 re-raise。
  - 置信度: 高（路径确定，触发罕见）

- [AUD-39716] [Suggestion] 内置 shader limited-range 常数按 8-bit 标度硬编码，10/12-bit 归一化输入存在约 0.3% 系统性色度偏差
  - 位置: src/shared/video/video_upload.py:496-499
  - 证据:
    ```python
            if (color_range == 0) {
                y = (y - 16.0 / 255.0) * 255.0 / 219.0;
                u = (u - 128.0 / 255.0) * (255.0 / 224.0);
                v = (v - 128.0 / 255.0) * (255.0 / 224.0);
    ```
  - 影响: 上传侧按 `(1 << bit_depth) - 1` 归一化（10-bit 白 940 → 0.9189），而 shader 按 8-bit 标度（235/255 = 0.9216）解释 limited 白，导致高位深输入整体偏暗约 0.3%（在自测 0.03 容差内，不致失败，但属系统性色彩误差）。
  - 建议: 由 uniform 传入按位深换算的 scale/offset（如 (1<<(depth-4))-1 / ((1<<depth)-1) 形式），或上传侧统一按 256 标度归一化。
  - 置信度: 中（数值推导确定，实际视觉影响轻微）

### 通过项

- 线程安全：模块文档明确所有 ModernGL 操作仅渲染线程，无跨线程共享可变状态
- PBOUploader 修复后路径正确：`texture.write(current_buffer)` 走 GL_PIXEL_UNPACK_BUFFER DMA，`orphan()` 先行避免 GPU stall，双缓冲轮转正确；缓冲不足时动态扩容并同步 `_buffer_sizes`
- initialize 失败降级路径正确：置 `_initialized=True` 且 `_buffers` 为空，upload_async 检测后回落 upload_direct，有日志不吞错
- release/_release_buffers/_release_textures 均有异常保护与日志
- 输入验证：upload_planes 校验 is_planar/planes 存在/未知像素格式均抛 ValueError；dispatch_upload_command 对未知格式回退 UPLOAD_VIDEO_FRAME 并告警；set_yuv_matrix/set_color_range 对非法值回退默认并告警
- GLSL 正确性（内置 shader）：所有数字字面量带 .0（含 1.140、2.032）；无比 veryth 循环；输出不 clamp 符合 HDR 铁律；uniform int 分支选择矩阵合法
- dispatch_upload_command 引用的 RenderCommand 成员与 render_commands.py 枚举一致；平面/打包格式分流逻辑与 DecodedFrame 结构匹配
- 类型注解完整（函数签名均有注解）

## src/shared/video/yuv_gpu_self_test.py (行数: 332)

### 发现

- [AUD-39720] [Major] run_yuv_gpu_self_test 外层异常路径不清理 output_tex/fbo，GPU 资源泄漏
  - 位置: src/shared/video/yuv_gpu_self_test.py:171-194, 249-272
  - 证据:
    ```python
        output_tex = ctx.texture((fbo_w, fbo_h), components=4, dtype='f1')
        ...
        fbo = ctx.framebuffer(color_attachments=[output_tex])
        ...
        uploader.render_yuv_to_rgb(
            y_tex, u_tex, v_tex, fbo,
            chroma_uv_scale=(1.0, 1.0),
            chroma_uv_offset=(0.0, 0.0)
        )

        # P1.5: Readback and validate
        raw = fbo.read(viewport=(0, 0, fbo_w, fbo_h), components=4)
        ...
    except Exception as e:
        result.reason = f"exception: {e}"
        logger.error(f"YUV GPU self-test exception: {e}", exc_info=True)
        return result
    ```
  - 影响: 正常路径与两次早退（all-black/all-transparent）都调用了 `_cleanup_self_test_resources(fbo, output_tex)`，但若 `render_yuv_to_rgb` 或 `fbo.read`（第 171-194 行之间）抛异常，落入外层 except（269 行）直接返回，fbo 与 output_tex 泄漏。该函数由 async_renderer.py:4765 在渲染器初始化时生产调用，每次渲染器重建触发一次，异常场景（如 shader 编译失败）正是最需要干净退出的场景。
  - 建议: 将 fbo/output_tex 初始化为 None 并放入 `finally:` 统一调用 `_cleanup_self_test_resources`。
  - 置信度: 高

- [AUD-39721] [Suggestion] 自测永久修改 ctx.viewport（4x4）与 uploader 的矩阵/范围状态，不保存恢复
  - 位置: src/shared/video/yuv_gpu_self_test.py:179-184
  - 证据:
    ```python
        # Set BT.709 limited range on uploader
        uploader.set_yuv_matrix(0)  # BT.709
        uploader.set_color_range(0)  # Limited

        # Render
        fbo.use()
        ctx.viewport = (0, 0, fbo_w, fbo_h)
        ctx.clear(0.0, 0.0, 0.0, 0.0)
    ```
  - 影响: 自测把全局 viewport 设为 4x4、把 uploader 切到 BT.709/limited 后不恢复先前值。若调用方（播放开始前的其他 GL 操作）依赖先前 viewport 或已配置的 HDR 矩阵（BT.2020），会被静默覆盖。实际影响有限（后续渲染通常各自设置 viewport/参数），故仅 Suggestion。
  - 建议: 进入时记录 `ctx.viewport` 与 uploader 当前 matrix/range，结束时恢复。
  - 置信度: 中（副作用确定存在，实际影响取决于调用方行为）

- [AUD-39722] [Suggestion] run_r16ui_gpu_self_test 内 import moderngl 未使用；两个入口函数参数缺类型注解；getattr 访问私有属性
  - 位置: src/shared/video/yuv_gpu_self_test.py:130-133, 289-292, 309, 323
  - 证据:
    ```python
    def run_yuv_gpu_self_test(
        ctx,  # moderngl.Context
        uploader,  # YUVGPUUploader
    ) -> YUVGpuSelfTestResult:
    ...
    def run_r16ui_gpu_self_test(
        ctx,
        uploader,
    ) -> YUVGpuSelfTestResult:
        ...
        import moderngl
    ...
            reason = getattr(uploader, '_r16ui_disabled_reason', 'unknown')
    ```
  - 影响: 注释式类型（而非真注解）使静态检查失效；run_r16ui_gpu_self_test 的 `import moderngl`（309 行）在本函数内无任何使用；`_r16ui_disabled_reason` 是 YUVGPUUploader 私有属性，跨模块 getattr 破坏封装且改名即静默失联。
  - 建议: 改为 `ctx: "moderngl.Context", uploader: "YUVGPUUploader"`（模块顶部 TYPE_CHECKING 导入）；删除未用 import；在 YUVGPUUploader 上提供公开的 `r16ui_disabled_reason` 属性。
  - 置信度: 高

### 通过项

- 线程安全：文档与调用方（async_renderer.py:4745 渲染线程）一致，自测仅在渲染线程执行
- 异常与取消路径：外层 try/except 保证自测异常不会中断播放初始化（async_renderer 另有一层捕获）；失败仅禁用 GPU 路径、保留 CPU 回退，符合设计
- GPU 资源生命周期（常规路径）：all-black/all-transparent 早退与正常完成均调用 `_cleanup_self_test_resources`，清理函数本身有异常保护
- 数值正确性：CPU 参考（47-69 行）与内置 GLSL 矩阵的列主序语义逐项核对一致（R=1.0y+1.5748v、G=1.0y-0.1873u-0.4681v、B=1.0y+1.8556u）；limited-range 常数与 shader 完全一致
- 读回行序处理正确：224-227 行注释明确不翻转 Y，避免与源行错位导致 macOS 上误禁用 GPU 路径（有历史修复说明）
- 容差设计合理：ERROR_THRESHOLD=0.03 已考虑 float16 往返精度损失；分母用 `max(count, 1)` 防除零
- 输入验证：upload 失败返回 None 纹理有显式检查（165-168 行）
- run_r16ui_gpu_self_test 委托 uploader 内建自测并透传禁用原因，异常路径返回结构化结果不抛出
