# batch-395 [P2] src/shared/video// 审查报告

- 批次：batch-395（编号区间 AUD-39500–AUD-39599）
- 文件：`src/shared/video/video_async_renderer.py`（1,052 行，已全文通读）
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失
- 背景备注：该类为 v2 遗留入口（类 docstring 自述 v3 路径已由 AsyncRenderer 承担），`video_playback.py` 在 v3 激活时跳过本类；但离线工厂 `timeline_preview_renderer.py` 仍会创建并 start/stop_thread 本类，代码路径仍可达。

## src/shared/video/video_async_renderer.py (行数: 1052)

### 发现

- [AUD-39500] [Critical] 预分配转换缓冲跨线程别名：队列中待上传帧的引用会被生产者线程复用改写
  - 位置: src/shared/video/video_async_renderer.py:277
  - 证据:
    ```python
    # 转换为 float32（归一化到 [0, 1]）
    frame_float = self._convert_frame_to_float_generic(frame, input_format)
    h, w = frame.shape[:2]

    # 上传帧（带格式信息）
    self.submit(VideoRenderCommand.UPLOAD_FRAME, {
        'image': frame_float,
    ```
    而 `_convert_frame_to_float_generic` / `_convert_frame_to_float` 返回的是同一个复用缓冲（L371: `self._convert_buffer = np.empty((h, w, 3), dtype=np.float32)`，L380: `return self._convert_buffer`）。
  - 影响: `upload_and_render_frame` 在调用方线程把共享缓冲对象排入队列，渲染线程延后消费（L643: `image.astype(np.float16)`、L646: `texture.write(image_f16.tobytes())`）。当调用方在渲染线程消费上一条命令前处理下一帧（seek/拖动/突发解码/GPU 繁忙时必现），缓冲内容被覆盖，上一条 UPLOAD_FRAME 上传的是新帧数据配旧帧号——帧撕裂/串帧，属渲染错误；astype/tobytes 与写入交错还可能产生半新半旧的脏帧。
  - 建议: 每次上传持有独立缓冲（双缓冲队列或从帧对象池借出，消费完成后再归还），或将转换移入渲染线程；至少在队列中保留 numpy 数组的不可变副本。
  - 置信度: 高

- [AUD-39501] [Critical] YUV 渲染路径失效：UPLOAD_YUV_FRAME 从不设置 current_frame_tex，RENDER_FRAME 渲染的是过期 RGB 帧
  - 位置: src/shared/video/video_async_renderer.py:817
  - 证据:
    ```python
    def _do_render_frame(self, data):
        ...
        if not self.ctx or not self.current_frame_tex:
            return
    ```
    而 YUV 上传只更新这些状态（L783-787）：
    ```python
            # 更新状态
            self._is_yuv_frame = True
            self._yuv_format = yuv_format
            self.current_frame_index = frame_index
            self.frame_size = (y.shape[1], y.shape[0])  # (width, height)
    ```
    `current_frame_tex` 仅在 `_do_upload_frame`（L648）被赋值；`_is_yuv_frame`/`_yuv_y_tex` 等在渲染路径中从未被读取。
  - 影响: `upload_yuv_and_render_frame` 上传 YUV 平面后提交 RENDER_FRAME：若之前上传过 RGB 帧，屏幕显示的是上一帧（渲染错误）；若从未有 RGB 帧，则静默不渲染也不发 frame_ready。YUV 带宽优化（P4.4）端到端不可用。
  - 建议: 在 YUV 上传后用 YUVGPUUploader 的转换程序把 YUV 渲染到 FBO 并设置 current_frame_tex（或直接渲染到 fbo_pair），或暂时移除/标注该公共 API 不可用，避免调用方误用。
  - 置信度: 高

- [AUD-39502] [Major] 非 float32 图像原样写入 f2 纹理：字节被当作 float16 解释，画面错乱
  - 位置: src/shared/video/video_async_renderer.py:642
  - 证据:
    ```python
                try:
                    if image.dtype == np.float32:
                        image_f16 = image.astype(np.float16)
                    else:
                        image_f16 = image
                    texture.write(image_f16.tobytes())
    ```
  - 影响: 公共命令 API（UPLOAD_FRAME）不做输入 dtype 验证：直接 submit uint8/uint16 帧（便捷方法 `upload_and_render_frame` 之外的合法用法）时，原始整数字节按 float16 位模式写入 `dtype='f2'` 纹理（`_get_texture_format` 恒返回 'f2'），显示为噪点/错乱颜色，且无任何告警。
  - 建议: 在 `_do_upload_frame` 校验 `image.dtype`，非 float32/float16 先归一化转换再写纹理；或按 dtype 选择纹理格式并匹配字节布局。
  - 置信度: 高

- [AUD-39503] [Major] HDR float 帧按逐帧最大值归一化：亮度逐帧重缩放，违反 HDR 不得无理由归一化的铁律
  - 位置: src/shared/video/video_async_renderer.py:443
  - 证据:
    ```python
        elif dtype_name in ('float32', 'float16'):
            # Float 数据，假设已归一化到 [0, 1]
            min_val = frame.min()
            max_val = frame.max()

            # 检查是否需要归一化（值范围不在 [0, 1]）
            needs_normalize = min_val < 0.0 or max_val > 1.0
    ```
    （L450: `'max_value': max_val if needs_normalize else 1.0,`，随后 L505: `self._convert_buffer = frame.astype(np.float32) / max_value`）
  - 影响: 场景参考的 HDR 数据（如 max=4.0 的亮帧）被除以 4.0、下一帧 max=2.0 的除以 2.0——播放中高光随内容逐帧重缩放，亮度跳变、HDR 意图被破坏（.clinerules：HDR 内部数据不得无理由 clamp/归一化）。附带每帧两次全帧 CPU min/max 归约（4K float32 约 8M 像素 × 2 遍）的固定开销。
  - 建议: float 输入默认不做按帧归一化（保留原值进入 f2 纹理，f2 动态范围足够）；归一化仅在使用方显式声明 max_value 时进行；min/max 检测至少应缓存或仅在首帧执行。
  - 置信度: 高（机制确定；实际触发取决于调用方是否经此路径传 HDR float）

- [AUD-39504] [Major] 纹理池 acquire 失败的 fallback 每帧新建纹理且不释放上一张 fallback 纹理：GPU 内存泄漏
  - 位置: src/shared/video/video_async_renderer.py:660
  - 证据:
    ```python
            else:
                logger.error("Pool acquire failed, fallback to direct creation")
                try:
                    self.current_frame_tex = self.ctx.texture(
                        size=(w, h), components=components, dtype=texture_dtype
                    )
                    self.current_frame_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
                    self.current_frame_tex.write(image.astype(np.float16).tobytes())
    ```
  - 影响: fallback 路径直接覆盖 `self.current_frame_tex` 而不 release 旧对象（池正常路径的纹理归池所有可不释放，但先前 fallback 创建的纹理无人持有引用即泄漏）。持续 acquire 失败时每帧泄漏一张 4K RGBA f2 纹理（约 66MB），数十帧内耗尽 GPU 内存。另注意此处 `image.astype(np.float16)` 与 AUD-39502 同源的 dtype 假设。
  - 建议: fallback 赋值前检查旧 `current_frame_tex` 是否为池外纹理并 release；或统一登记到 deferred-release 列表统一释放。
  - 置信度: 高

- [AUD-39505] [Major] 构造函数对主 GL 上下文做 doneCurrent/makeCurrent 干预：在其他页面上下文活跃期构造曾致崩溃（仓库内有规避注释佐证）
  - 位置: src/shared/video/video_async_renderer.py:151
  - 证据:
    ```python
        # 创建共享上下文（与 AsyncRenderer 类似的模式）
        current_surface = self.main_context.surface()
        if current_surface:
            self.main_context.doneCurrent()
    ```
    （L168-169: `if current_surface: self.main_context.makeCurrent(current_surface)`）
  - 影响: `src/app/controllers/file_controller.py:4359` 注释明确记载："VideoAsyncRenderer 在 Color 页面 GL 上下文仍活跃时做 doneCurrent/makeCurrent 导致崩溃"，调用方被迫绕行（避免 switch_to_page 钩子触发构造）。该构造期上下文窃取对构造时机敏感，是已发生的崩溃源。
  - 建议: 将共享上下文创建移到 `run()` 首行（渲染线程内，且首次 makeCurrent 前创建），或经 GLContextManager 全局互斥锁串行化构造期的 doneCurrent/makeCurrent，消除对调用时机的隐含要求。
  - 置信度: 高（崩溃事实有仓库注释佐证；本文件内为构造期风险条件）

- [AUD-39506] [Major] 线程退出不做 doneCurrent，moderngl ctx 不 release 仅置 None：GL 对象可能在错误线程被销毁
  - 位置: src/shared/video/video_async_renderer.py:246
  - 证据:
    ```python
        # 清理资源
        self._cleanup_gl_resources()
        logger.info(f"Thread exited cleanly")
    ```
    而 `_cleanup_gl_resources` 末尾（L969）仅：
    ```python
        self.ctx = None
    ```
    全文无 `self.shared_context.doneCurrent()`、无 `self.ctx.release()`、无 surface/context 的显式释放。
  - 影响: `run()` 返回时 shared_context 仍 current 在已死亡的渲染线程；moderngl 上下文（及其包装的 GL 对象）仅靠 Python GC 在任意线程析构，跨线程销毁 GL 资源是未定义行为，可致崩溃（违背项目 deferred-release 纪律）。QOffscreenSurface 同样无销毁时机保障。
  - 建议: 线程退出前 `ctx.release()`（或 moderngl 的等价释放）→ `shared_context.doneCurrent()`；surface 在主线程 `deleteLater()`。
  - 置信度: 中（析构线程不可控属 CPython 实现细节，但生命周期缺口本身确定）

- [AUD-39507] [Major] GC 后备释放机制是空操作：_gc_resources 列表从未被填充
  - 位置: src/shared/video/video_async_renderer.py:173
  - 证据:
    ```python
        # weakref.finalize 后备释放机制
        # GC 时确保基本资源释放（不能调用 stop_thread 因为涉及线程等待）
        self._gc_resources: List[Any] = []
        self._finalizer = weakref.finalize(
            self,
            self._cleanup_on_gc,
            self._gc_resources
        )
    ```
  - 影响: 全文（含 `_cleanup_on_gc`，L1030-1052）没有任何 `self._gc_resources.append(...)`；回调遍历空列表，注释承诺的"GC 时确保基本资源释放"完全不生效，与 AUD-39506 叠加后对象失联即泄漏。反过来说，若日后真的把 ModernGL 对象塞进该列表，在 GC 线程直接调 `res.release()` 同样违反线程规则——机制设计本身也需要重新评估。
  - 建议: 要么在资源创建处登记（并改为投递到存活渲染线程执行 release 的 deferred 队列），要么删除该机制避免虚假安全感。
  - 置信度: 高

- [AUD-39508] [Major] frame_ready 发出的纹理 glo 在视口变化重建 FBO 时被释放：跨线程纹理 ID 生命周期竞态
  - 位置: src/shared/video/video_async_renderer.py:849
  - 证据:
    ```python
        # 完成渲染并发射信号
        self.ctx.finish()
        tex_id = self.tex_pair[front_idx].glo
        self.frame_ready.emit(tex_id)
    ```
    而下次渲染视口尺寸变化时（L864-877）：
    ```python
        if self.fbo_pair[0] is None or self.fbo_pair[0].size != (width, height):
            # 释放旧的 FBO
            for fbo in self.fbo_pair:
                if fbo:
                    try:
                        fbo.release()
    ```
  - 影响: `tex_id` 是裸 OpenGL 纹理名，消费方（主线程，如 video_playback 的 `_on_video_frame_ready`）在共享上下文里使用它期间，渲染线程可能因 resize 释放同名纹理 → 消费方采样到已删除纹理（驱动可能复用该 ID 给新对象），显示错乱内容。
  - 建议: 释放旧 FBO 纹理前确认消费方已换帧（帧号 generation gate 或双缓冲持有确认），或对旧纹理走延迟释放队列（N 帧后再释放）。
  - 置信度: 中（竞态窗口取决于消费方持有时长，本文件内无法完全确认）

- [AUD-39509] [Major] 着色器加载失败被静默吞掉：清屏后的空白帧仍按成功 emit frame_ready
  - 位置: src/shared/video/video_async_renderer.py:590
  - 证据:
    ```python
        try:
            self.utility_shaders['passthrough'] = self.ctx.program(
                vertex_shader=passthrough_vs,
                fragment_shader=passthrough_fs
            )
        except Exception as e:
            logger.error(f"Failed to load shaders: {e}")
    ```
    而 `_do_render_frame`（L838-851）`if prog:` 之外仍执行清屏、缓冲翻转并 `self.frame_ready.emit(tex_id)`。
  - 影响: 着色器编译失败时渲染器进入"僵尸正常"状态：每帧 emit 灰屏（0.1,0.1,0.1 清屏色）纹理当作成功结果，`error_occurred` 从不触发，UI 无从得知视频管线已坏（宽泛 except 掩盖领域错误）。
  - 建议: `_load_shaders` 失败时 emit error_occurred 并终止 run()（与 ModernGL 初始化失败同路径）；`prog` 缺失时不应 emit frame_ready。
  - 置信度: 高

- [AUD-39510] [Major] 命令循环宽泛 except 吞掉命令级异常：既不上报也不重试，消费方对丢帧无感知
  - 位置: src/shared/video/video_async_renderer.py:242
  - 证据:
    ```python
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Error in render loop: {e}")
                traceback.print_exc()
    ```
  - 影响: 任一命令（上传/渲染）抛出的领域错误（尺寸非法、纹理写入失败等）仅打印日志后命令被丢弃，`error_occurred` 不发射、frame_ready 也不发——消费方等待帧永远不会到来，且违反 .clinerules"不得宽泛 except 掩盖领域错误"。
  - 建议: 捕获后区分错误类别 emit error_occurred（携带命令类型），必要时对可重试命令重新入队；至少保证每个命令要么 emit frame_ready 要么 emit error_occurred。
  - 置信度: 高

- [AUD-39511] [Major] _render_quad 每帧创建并销毁 VBO/IBO/VAO：与纹理池化（P4.1）理念相悖的每帧 GPU 对象churn
  - 位置: src/shared/video/video_async_renderer.py:904
  - 证据:
    ```python
        # 创建 VAO
        vbo = self.ctx.buffer(vertices.tobytes())
        ibo = self.ctx.buffer(indices.tobytes())
    ...
        # 清理
        vbo.release()
        ibo.release()
        vao.release()
    ```
  - 影响: 全屏四边形数据是常量，却在每个 RENDER_FRAME（60fps 下每秒 180 次 GL 对象创建 + 180 次释放）重复分配，徒增驱动调用与内存分配器压力，挤占 4K/60 实时预算；同一代码库为避免每帧纹理创建专门引入了 VideoTexturePool，此处同类反模式未治理。
  - 建议: 在 `_load_shaders` 阶段一次性创建 VBO/IBO/VAO 并缓存到 self，`_cleanup_gl_resources` 统一释放。
  - 置信度: 高（代码事实确定；帧时间影响为中等）

- [AUD-39512] [Suggestion] 每帧 ctx.finish() 强制全流水线同步，可用 fence sync 替代
  - 位置: src/shared/video/video_async_renderer.py:849
  - 证据:
    ```python
        # 完成渲染并发射信号
        self.ctx.finish()
        tex_id = self.tex_pair[front_idx].glo
        self.frame_ready.emit(tex_id)
    ```
  - 影响: glFinish 每帧阻塞 CPU 直到 GPU 排空，破坏 CPU/GPU 并行，在 4K 场景可损失数毫秒帧预算；信号本身只携带纹理 ID 并不读取像素，无需完全同步。
  - 建议: 共享上下文消费前用 GL fence（glFenceSync + 客户端等待）做细粒度同步，或依赖 `frame_ready` 消费方在自己的上下文里隐式同步。
  - 置信度: 高

- [AUD-39513] [Suggestion] _get_texture_format 全部分支返回 'f2'：死分支且 float32 HDR 被无条件降为 float16 而文档未言明
  - 位置: src/shared/video/video_async_renderer.py:525
  - 证据:
    ```python
        if dtype == 'uint8':
            # uint8: 使用 float16 纹理（现有默认）
            return 'f2'
        elif dtype == 'uint16':
            ...
            return 'f2'
        elif dtype == 'float32':
            # float32 HDR: 使用 float16 纹理节省内存
            return 'f2'
    ```
  - 影响: 五个分支返回值相同，函数形同虚设；float32 HDR 无条件降精度（f2 尾数 10 位）对 >11 档渐变的调色画面可能产生条带，且决策被掩盖在死分支里。
  - 建议: 删除分支直接 `return 'f2'` 并注释策略；或让 float32 真正可选 'f4'（经参数开关）。
  - 置信度: 高

- [AUD-39514] [Suggestion] 命令合并直接 peek queue.Queue 内部 deque，越过 Queue 的锁协议
  - 位置: src/shared/video/video_async_renderer.py:220
  - 证据:
    ```python
                    next_item = self.command_queue.queue[0]
                    if next_item[0] == VideoRenderCommand.RENDER_FRAME:
                        self.command_queue.get_nowait()
                        item = next_item
    ```
  - 影响: 访问 `queue.queue`（受 `mutex`/`not_empty` 条件变量保护的双端队列）属实现细节依赖，在 CPython/GIL 下恰好安全，但换解释器或 Queue 实现变化即成数据竞争；merge 分支的 task_done 配对也依赖细致推演才平衡，脆弱。
  - 建议: 用受控的丢弃策略替代 peek（如向队列投递哨兵合并标记），或封装一个带锁的命令队列类对外只暴露原子 take-merged 操作。
  - 置信度: 中

- [AUD-39515] [Suggestion] upload_yuv_and_render_frame 在调用线程无锁读 current_frame_index（渲染线程并发写）
  - 位置: src/shared/video/video_async_renderer.py:331
  - 证据:
    ```python
            'frame_index': frame_index if frame_index >= 0 else self.current_frame_index + 1,
    ```
  - 影响: `current_frame_index` 由渲染线程在 `_do_upload_frame`/`_do_upload_yuv_frame` 更新，调用线程读取无同步；突发提交时帧号可能重复或跳号，干扰池的 LRU 帧序判断（VideoTexturePool 依赖 frame_index 判新旧）。
  - 建议: 帧号由调用方自己维护递增计数，或读取时加 `self._mutex`（注意该 mutex 目前实际未被使用）。
  - 置信度: 中

- [AUD-39516] [Suggestion] stop 后 submit 触发僵尸重启；quit() 对自定义 run() 无效
  - 位置: src/shared/video/video_async_renderer.py:250
  - 证据:
    ```python
    def submit(self, cmd_type, data=None):
        """线程安全地提交渲染命令"""
        if not self.isRunning():
            self.start()
        self.command_queue.put((cmd_type, data))
    ```
    （`stop_thread` L973-976 置 `is_running=False` 后调 `quit()`；`run()` 被重写为自定义循环，不跑 QEventLoop，quit() 是 no-op）
  - 影响: `stop_thread()` 后 `is_running` 永远为 False，但 submit 仍会 start()：run() 重新 makeCurrent、完整初始化 ModernGL/池/上传器后立即退出并清理，命令永不执行——调用方以为渲染器活着；且 `isRunning()`/`start()` 检查-启动非原子，并发 submit 可能双重 start 触发 Qt 告警。
  - 建议: submit 检查 `self.is_running` 为 False 时拒绝入队并告警；去掉无效的 quit()（或改用退出事件 + wait 确认）。
  - 置信度: 高

- [AUD-39517] [Suggestion] 多个核心方法缺少参数/返回类型注解
  - 位置: src/shared/video/video_async_renderer.py:88
  - 证据:
    ```python
    def __init__(self, main_context):
    ...
    def _handle_command(self, cmd_type, data):        # L553
    def _do_render_frame(self, data):                 # L808
    def _ensure_fbos(self, width, height):            # L862
    def _render_quad(self, program):                  # L889
    def stop_thread(self):                            # L971
    def get_current_frame_texture(self):              # L982
    ```
  - 影响: 与项目"所有函数有类型注解"的约束不符；`main_context` 应为 `QOpenGLContext`，`data` 应为 `Dict[str, Any]`，缺失注解削弱 IDE/静态检查对跨线程接口的把关。
  - 建议: 补齐 `__init__`、`_handle_command`、`_load_shaders`、`_do_render_frame`、`_do_update_params`、`_do_cleanup`、`_ensure_fbos`、`_render_quad`、`stop_thread`、`is_initialized`、`get_current_frame_texture` 的注解。
  - 置信度: 高

- [AUD-39518] [Suggestion] 预分配缓冲在 RGB uint16 路径被整块替换；UPLOAD_FRAME 的 frame_size 参数被忽略
  - 位置: src/shared/video/video_async_renderer.py:505
  - 证据:
    ```python
            else:
                # RGB，直接归一化
                self._convert_buffer = frame.astype(np.float32) / max_value
    ```
    以及 `_do_upload_frame`（L614-620）取出后从未使用：
    ```python
        image = data.get('image')
        frame_size = data.get('frame_size')
        input_format = data.get('input_format', {})

        if image is None:
            return
    ```
  - 影响: L505 每帧重新分配数组，与"预分配以避免每帧分配"的设计注释（L141-143）相悖；`frame_size` 传入即弃（实际用 `image.shape`），误导调用方以为该参数生效。
  - 建议: uint16 RGB 路径改用 `np.divide(..., out=self._convert_buffer)` 写入预分配缓冲；删除 frame_size 或真正以其为准校验 shape。
  - 置信度: 高

### 通过项
- 信号防递归：本类仅单向发射 frame_ready/error_occurred，不回接自身信号，无递归触发面。
- 状态提交一致性：纯渲染器，不涉及 undo/redo/revision/持久化，无状态提交面需要校验。
- 渲染线程内所有 GL 操作（初始化、命令处理）均持 GLContextManager 全局互斥锁，与 AsyncRenderer 共享上下文的互斥模式一致。
- 命令合并逻辑中 task_done() 调用配对平衡（get 一次、合并项各一次，无漏配/多配）。
- `_convert_frame_to_float` 的 BGR→RGB 通道映射正确（B→R 槽、G→G、R→B 槽）。
- `_ensure_fbos` 的释放顺序正确（先 framebuffer 后其颜色附件纹理），且仅在同一尺寸键上重建。
- ModernGL 初始化失败路径正确：emit error_occurred 并 return，不进入命令循环。
- `__enter__`/`__exit__` 上下文管理器协议正确委托 stop_thread，异常路径也会清理。
- YUV 上传前置校验基本到位（ctx/uploader 存在性、y 平面非空、格式分支未知时报错返回）。
- GLSL 着色器本身（passthrough vs/fs）语法正确、数字字面量均带 `.0`、无循环与 clamp 问题。
