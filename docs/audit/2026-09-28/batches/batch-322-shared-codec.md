# batch-322 [P2] src/shared/codec/topos_binding.py 审查报告

编号区间：AUD-32200–AUD-32299。文件 1 个（4,449 行），已全文通读。
审查方法补充：对可疑点做了只读交叉验证——ctypes 整数字段回绕行为
（`c_uint16 = 70000 → 4464`、`c_uint8 = 300 → 44`，均不抛错）、
`ctypes.cast` 对 bytes 的 `_objects` 保活（实测存活），
以及 native 侧契约（`native/topos_codec/src/codec/codec.c` 的
`tc_frame_config_validate`、`src/mov/mov.c` 的 faststart/add_audio_track、
`src/codec/decoder_ctx.c` 的 batch plane-view struct_size 校验、
`include/topos_codec.h` 的 cvt stride 0=紧凑语义）。

## src/shared/codec/topos_binding.py (行数: 4,449)

### 发现

- [AUD-32201] [Major] movie_config 未验证 width/height/qp/timescale 数值域，ctypes 静默回绕可落入合法域并绕过 native 校验
  - 位置: src/shared/codec/topos_binding.py:4166
  - 证据:
    ```python
    cfg.visible_width = width
    cfg.visible_height = height
    ...
    cfg.qp_base = qp          # :4195，c_uint8
    ...
    cfg.timescale = timescale # :4206，c_uint32
    ```
    而 profile/pixel_format/bit_depth/alpha/audio 均有显式白名单校验（:4171-4246）。
  - 影响: 实测 ctypes 对 `c_uint16`/`c_uint8` 字段的越界赋值静默取模回绕
    （70000→4464、300→44）。width=70000 回绕为 4464 落在 C 侧 1..16384
    合法域（codec.c:444-448 只查回绕后的值），编码器会静默产出错误分辨率
    的文件；qp=300 回绕为 44 同样通过 `qp_base > TC_QP_MAX` 检查
    （codec.c:450-453），静默劣化画质；负 timescale 回绕为巨大 u32。
    本模块已定义 `TOPOS_QP_MAX=95`（:76）与 `TC_CODEC_MAX_DIM=16384`
    （:72）却未在本函数应用。调用方为产品导出/代理路径
    （src/shared/export/topos_encoder.py:1154、src/shared/media/proxy_generator.py:640,967）。
  - 建议: 在 movie_config（及任何直接构造 `_CFrameConfig` 的辅助）中按
    `1..65535`（或 16384 上限）、`0..TOPOS_QP_MAX`、timescale>0 等显式
    raise，与同函数既有 fail-fast 风格一致。
  - 置信度: 高（回绕行为已实测复现；触发需越界值到达绑定层）。

- [AUD-32202] [Major] ParallelSequentialDecoder worker 在 try 块之前的异常导致主线程永久阻塞且泄漏 fd
  - 位置: src/shared/codec/topos_binding.py:4362
  - 证据:
    ```python
    def worker(wid, in_q, out_q):
        movie = ToposMovieFile(self._codec, self._path)   # :4363 try 之外
        budget = ...
        dec = ToposDecoder(self._codec, max_slice_workers=budget)  # :4369
        lease = None
        if getattr(self._codec, "_has_decoder_worker_budget", False):
            from src.shared.codec.decode_worker_budget import DECODE_WORKER_BUDGET
            lease = DECODE_WORKER_BUDGET.acquire(...)      # :4374
        ...
        try:                                               # :4381
            for idx in range(wid, total, workers):
    ```
    主线程消费端为 `while finished < workers: idx, payload = out_q.get()`（:4425-4426）。
  - 影响: `ToposMovieFile` 构造失败（探测后文件被删/权限变化）、
    `tc_decoder_create` 返回 OOM、budget acquire 或 import 抛错时，
    异常发生在把 `(None, e)` 投递到 out_q 的 try/except（:4399-4400）
    之前——worker 线程静默死亡且不发送完成哨兵，主线程在 `out_q.get()`
    上永久挂起（消费该迭代器的导出任务冻结，只能强杀）；同时 :4363 打开
    的 fd 不会走 worker 的 finally 清理，发生泄漏。当前仓内使用方为
    tools/bench_decode_worker_budget.py 等工具，但类按导出管线 API 设计。
  - 建议: 把 movie/dec/lease 的创建一并纳入 try/except BaseException →
    `out_q.put((None, e))` 的范围（或在 worker 最外层整体兜底），并确保
    部分构造失败时关闭已打开的 movie。
  - 置信度: 高（结构性缺口确定；触发条件为资源紧张/环境异常）。

- [AUD-32203] [Major] ToposMuxFile 顺序写回调未持实例锁，与 seek_write 回调的加锁不对称（Windows 下文件指针竞争）
  - 位置: src/shared/codec/topos_binding.py:2488
  - 证据:
    ```python
    def _write_cb(self, _ctx, data, length):
        try:
            chunk = ctypes.string_at(data, length)
            return 0 if os.write(self._fd, chunk) == length else -14  # 无锁
    ...
    def _seek_write_cb(self, _ctx, off, data, length):
        ...
            return 0 if _pwrite_full(self._fd, chunk, off,
                                     self._wlock) == length else -14    # 持锁
    ```
    注释自述“与顺序写共用实例锁互斥”（:2499），但顺序写侧从未 acquire。
  - 影响: Windows 无 `os.pwrite`，`_pwrite_full` 走 lseek→write→恢复指针
    模拟（:172-177）。若 native 侧并发/交错触发 write 与 seek_write 回调，
    无锁的 `os.write` 会移动共享 fd 偏移，插入 _pwrite_full 的 seek 与
    write 之间，导致数据写错偏移（moov/mdat 错位）。读侧
    （ToposMovieFile._read_cb :2013）则一致加锁，无此问题。POSIX 路径
    pwrite 不依赖 fd 偏移，不受影响。
  - 建议: `_write_cb` 同样在 `self._wlock` 内执行 `os.write`（或在
    Windows 分支统一串行化），使两侧互斥名副其实。
  - 置信度: 中（锁不对称是事实；是否可实际竞争取决于 native mux 回调
    的线程化方式，未在 C 侧确认到并发调用点）。

- [AUD-32204] [Major] decode_reduced/decode_request/decode_batch 未复用 C4 TLS scratch，逐帧全平面零填分配
  - 位置: src/shared/codec/topos_binding.py:3396
  - 证据:
    ```python
    # decode_reduced（:3394-3398）与 decode_request（:4444-4448 同构）
    for p in range(info.plane_count):
        w, h = self.plane_geometry(info, p)
        arr = (ctypes.c_uint16 * (w * h))()      # 每帧新分配+零填
    # decode_batch（:3496-3499）帧×平面同样
    ```
    对比 decode() 的 C4 优化（:3334-3337）：TLS grow-only scratch 平铺，
    其注释记载旧实现“每帧 47.7MB（4K444）的 ctypes 数组零填……把纯
    native ~22ms 的解码拖到 ~45ms”。
  - 影响: `decode_batch` 文档明示“高频连续解码应优先本入口”（:3466），
    却保留了 C4 已在 decode() 中消除的逐帧全平面 calloc+缺页成本，
    4K 实时基准下系统性拖慢批量路径；decode_request（AUTO_2K 预览）与
    decode_reduced 同构但分辨率较低、影响较小。
  - 建议: 将 C4 的 per-thread scratch（`_dec_tls`）推广到这三个入口
    （batch 需按批总量平铺），保持“返回 bytes 自有拷贝”契约不变。
  - 置信度: 中（代码不对称为事实；性能幅度引自本文件自述的实测注释，
    未另行测量）。

- [AUD-32205] [Suggestion] ParallelSequentialDecoder 死代码与全局线程池副作用不恢复
  - 位置: src/shared/codec/topos-color-multitask /topos-color01/src/shared/codec/topos_binding.py:4358
  - 证据:
    ```python
    PREFETCH = 2  # 每路在飞令牌数（含正在解码的 1 帧）   # 从未使用
    ...
    self._owns_codec = codec is None                       # :4332 从未读取
    ...
    def __exit__(self, *_exc) -> None:                     # :4347-4350
        pass
    ```
    另 `__init__` 调 `self._codec.set_slice_threads(int(threads))`（:4334-4335）。
  - 影响: 每路实际在飞帧数为 1（令牌发放逻辑 :4417-4440），宣称的
    PREFETCH=2 流水深度未实现；`_owns_codec` 暗示的条件清理不存在；
    `set_slice_threads` 是进程级全局旋钮（:2983-2985 自述），后台用途
    （R6 建议后台 threads=1）结束后池上限停留在 1，拖累后续前台解码，
    `__exit__` 不恢复原值。
  - 建议: 删除或实现 PREFETCH；`__enter__` 记录原 `slice_threads()` 并在
    `__exit__` 恢复（仅当本实例主动设置时）；删除 `_owns_codec` 或补齐
    对应清理。
  - 置信度: 高。

- [AUD-32206] [Suggestion] ToposDecoder 平面输入校验缺口：make_views 不查 dtype/C 连续，max_slice_workers 负值回绕，batch 不校验 views_list 长度
  - 位置: src/shared/codec/topos_binding.py:1090
  - 证据:
    ```python
    views[p].pixels = arr.ctypes.data        # 无 dtype/contiguous 检查
    ```
    对比编码侧 `_fill_frame_input` 的显式校验（:219-222）。另：
    `max_slice_workers=int(max_slice_workers) if max_slice_workers else 0`
    （:810）负值经 c_uint32 回绕为 0xFFFFFFFF；`decode_views_batch`
    按 `packets` 长度铺 flat views（:1113-1116），`len(views_list) != n`
    不检查（C 侧 plane-view struct_size 校验兜底为 INVALID_ARGUMENT，
    decoder_ctx.c:736，非崩溃但报错难懂）。
  - 影响: 调用方传入非 '<u2'/非 C 连续/转置数组时解码输出静默错乱；
    负 worker 预算语义反转；views_list 长度错的报错远离根因。
  - 建议: make_views 复用 `_fill_frame_input` 的 dtype/contiguity 断言；
    `__init__` 与 `set_max_slice_workers` 同样 `max(0, ...)`；batch 入口
    前置 `len(views_list) == len(packets)` 检查。
  - 置信度: 高（缺口为事实；触发需调用方误用）。

- [AUD-32207] [Suggestion] 多个 public API 参数缺类型注解
  - 位置: src/shared/codec/topos_binding.py:202
  - 证据:
    ```python
    def _fill_frame_input(cin, planes) -> list:            # :202
    def prepare(self, packet, size: Optional[int] = None)  # :851
    def decode_views(self, packet, views, plane_count: int,  # :905
                     size: Optional[int] = None)
    def decode_views_batch(self, packets, views_list):     # :1094
    def make_views(self, arrays) -> "ctypes.Array":        # :1080
    def feed(self, packet, size ..., planes: Optional[List] = None)  # :1476
    ```
  - 影响: packet（bytes/ctypes 缓冲双态）、views/planes（numpy 平面）等
    关键参数类型契约只能靠 docstring，静态检查与 IDE 补全失效；与全模块
    其余强类型风格不一致。
  - 建议: 补 `packet: Union[bytes, ctypes.Array]`、`views: "ctypes.Array"`、
    `planes: Optional[List["np.ndarray"]]` 等注解（numpy 以 TYPE_CHECKING
    前置引用避免运行时依赖）。
  - 置信度: 高。

- [AUD-32208] [Suggestion] 杂项：encode() 文档引用不存在的方法；decode() 死变量 widths
  - 位置: src/shared/codec/topos_binding.py:3108
  - 证据:
    ```python
    stats_out 可传入 _CFrameStats() 实例接收统计（通常用 encode_with_stats）。
    ```
    全文无 `encode_with_stats` 定义（实际方法为 `encode_frame` :3220）。
    另 decode() 内 `widths = []`（:3331）仅被 append（:3341）从不读取。
  - 影响: 文档误导调用方查找不存在的 API；死变量徒增每帧微小开销并
    混淆阅读。
  - 建议: 文档改为 `encode_frame`；删除 widths。
  - 置信度: 高。

### 通过项
- GPU 资源生命周期：本模块不触碰 OpenGL（纯 CPU codec 绑定，N/A 于
  UI 线程铁律）；原生句柄（ToposDecoder/ToposGopContext/ToposMovieFile/
  ToposMuxFile）均幂等 close + 上下文管理器，构造失败路径正确关闭 fd
  （:1999-2002、:2480-2482）、decode_surface_lease 的 begin/end 有 finally
  配对（:1064-1078）（worker 前置区例外已立案 AUD-32202）。
- GLSL·HDR 正确性：全链路 uint16 平面、无 clamp(0,1) 滥用；结构镜像以
  C 侧 struct_size 双向校验（_verify_abi :4284-4298、cpu_features
  :2904-2908、capabilities :2925-2929）；色彩码表未知标签显式失败设计
  （:84-108）。
- 信号防递归：无 Qt 控件/信号，不适用。
- 状态提交一致性：无 undo/redo 状态；GOP 参考事务（encode_frame BTS
  重试、abort/reset_to_i）按 C 契约透传且重试确定性有契约注释支撑
  （:1513-1521）。
- 异常与取消路径：所有 native 调用状态码显式检查并携带 last_error；
  可选符号缺失 fail-fast 且 __init__ 将 AttributeError 转可操作错误
  （:2794-2806）；消费端取消（stop 令牌 + join）路径正确（除 AUD-32202）。
- 输入验证（已覆盖维度）：movie_config 的 profile/pixel_format/bit_depth/
  alpha/audio 白名单完备（:4171-4246）、set_raw_meta 长度域（:2563-2566）、
  decode request 枚举域（:725-761）、convert_packed_rgb/convert_yuv444_to_422
  的 dtype/形状/连续性校验（:3577-3599、:3674-3703）、音频 chunk 区间
  越界检查（:2365-2379、:2402-2421）；C 侧 add_audio_track 对轨名长度
  有界拷贝（mov.c:1104-1112）、faststart 仅用 dst->write 顺序写
  （mov.c dst 调用点核对）——未立案项均验证无问题。
- 性能模式（已覆盖维度）：encode/encode_sized/encode_scalable/GOP
  encode 的 grow-only 复用缓冲 + BTS 升级重试 + 精确长度单拷
  （:3143-3146、:3290-3292、:1548-1550）；movie 侧 packet 复用缓冲与
  批量聚读 arena（:2030-2165）；零拷贝 numpy 平面路径（:202-226）。
- ctypes 内存安全：bytes/numpy 指针的 keepalive 模式正确
  （`_fill_frame_input` keep 列表挂 cin :3117；`ctypes.cast` 的
  `_objects` 保活经实测确认，decode_batch 无显式 keep 亦安全）；
  回调 trampoline 由实例属性持引用（:1983、:2467-2468）。
- 线程安全（已覆盖维度）：ToposCodec._enc_lock 串行化共享编码 scratch
  （:3119、:3182、:3257）；解码 scratch 走 threading.local（:2816、
  :3334）；ToposMovieFile 读回调全路径持锁（:2013）；mux 写侧例外已
  立案 AUD-32203。
