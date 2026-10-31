# 批次 batch-003 静态审查报告：color/core（auto_color / cache_manager / color_manager / color_output）

- 审查日期：2026-09-28
- 审查范围：`src/features/color/core/` 下 4 个文件，共 2,498 行
- 问题编号区间：[AUD-0300–AUD-0319]
- 审查方式：全文 Read 逐行审查 + 只读 grep 核对调用方（`ColorOutput.update` 的唯一调用点 `image_engine.py:4584`、`max_memory_mb` 全库引用、`CacheWarmer`/`MemoryPressureMonitor`/color 版 `CacheManager` 全库引用）
- 重要背景结论（影响多条评级）：
  - `ColorOutput` 只缓存整数纹理 ID，纹理对象所有权在 AsyncRenderer / composite resource_pool（`output_node.py` 经 `release_texture_by_id` 释放），故"替换不释放"不计为本文件缺陷。
  - `src/features/color/core/cache_manager.py` 中的 `CacheManager`/`CacheWarmer`/`MemoryPressureMonitor` 在全仓库无外部调用方（composite 侧使用的是另一个 `src/features/composite/render/cache_manager.py`）。下列问题均为待接线即触发的真实缺陷，已在影响栏注明。

---

## src/features/color/core/auto_color.py (行数: 261)

### 发现

- [AUD-0300] [Suggestion] 对比度反解在 hi 精确等于 0.18 时发生除零，产生 numpy RuntimeWarning
  - 位置: src/features/color/core/auto_color.py:171
  - 证据:
    ```python
    if 0.05 < hi < 5.0 and stats.luma_p99 - stats.luma_p01 > 1e-3:
        # 0.18 枢轴幂曲线：x' = (x/0.18)^c · 0.18，把 hi 推到目标高光
        c = float(np.log(TARGET_HIGHLIGHT / 0.18) / np.log(hi / 0.18))
        if 0.0 < c < 4.0:
            contrast = _contrast_for_gain(c)
    ```
  - 影响: `hi == 0.18` 时 `np.log(1.0) == 0.0`，除法触发 "divide by zero" RuntimeWarning；结果为 inf/nan，随后被 `0.0 < c < 4.0` 过滤，不会崩溃也不会给出错误建议，仅污染 stderr/警告过滤器。属于数学边缘（浮点精确等于概率极低）。
  - 建议: 分母加 `max(abs(np.log(hi / 0.18)), 1e-9)` 或在计算前显式排除 `abs(hi - 0.18) < eps`。
  - 置信度: 低（需浮点精确命中 0.18；仅告警不致错）

### 通过项
- 线程安全：纯 numpy 无状态函数，无共享可变状态，无 Qt/OpenGL 依赖（与 docstring 声明一致）
- GPU 资源生命周期：不涉及
- HDR 正确性：`np.clip(img[:, :, ::-1], 0.0, None)` 只截负值、保留高光（>1.0 不 clamp）；WB/曝光/对比度反解公式与 docstring 声明的正向模型核对一致（temp=(k_r-k_b)*100、tint=(1-k_g)*200、|p|=sqrt(|stops|/6)*100）
- 信号防递归：无信号/控件
- 输入验证：`_to_float01` 校验 ndim/通道数并显式 raise ValueError
- 参数一致性：所有建议经 `_PARAM_LIMIT`（±100）限幅，与 UI 量级一致
- BGR→RGB 转换与 luma 权重（0.299/0.587/0.114）核对无误
- 类型注解完整

---

## src/features/color/core/cache_manager.py (行数: 1653)

### 发现

- [AUD-0310] [Critical] CacheWarmer.start_warmup 在持有不可重入锁时调用 stop_warmup，必然死锁
  - 位置: src/features/color/core/cache_manager.py:1425
  - 证据:
    ```python
    with self._lock:
        if self._is_running:
            logger.debug("Warmup already running, stopping old warmup")
            self.stop_warmup()

        self._is_running = True
        self._stop_event.clear()
    ```
    而 `stop_warmup()`（:1450-1454）为：
    ```python
    def stop_warmup(self) -> None:
        """停止缓存预热"""
        with self._lock:
            self._stop_event.set()
            self._is_running = False
    ```
  - 影响: `threading.Lock` 不可重入。只要在预热运行中再次调用 `start_warmup`（典型场景：用户在时间线上快速跳转、每次 seek 都重启预热），调用线程在 `stop_warmup` 内第二次 acquire 同一把锁，永久挂起，调用链上层（UI 或播放引擎）随之冻结。附加缺陷：即使修复死锁，`self._warmup_thread = threading.Thread(...)`（:1437）在锁外赋值、`stop_warmup` 锁外读取，并发 start 存在线程引用竞态；`stop_warmup` 的 `join(timeout=2.0)` 超时后旧 warmup 线程可能仍在执行 `decode_callback` 并与新线程并发写同一缓存。注：全仓库当前无 `CacheWarmer` 外部调用方，接线后即触发。
  - 建议: 改用 `threading.RLock`；或将 `stop_warmup` 拆为内部无锁版本 `_stop_locked()` 供锁内调用；`_warmup_thread` 的读写纳入同一把锁保护。
  - 置信度: 高（死锁路径由代码结构直接决定，无并发时序假设）

- [AUD-0311] [Major] 内存压力监控在内存"正常"时也每 5 秒清空缩略图缓存
  - 位置: src/features/color/core/cache_manager.py:1605
  - 证据:
    ```python
    else:
        # 内存使用正常，低压力清理
        self._cache_manager.on_memory_pressure("low")
    ```
    而 `on_memory_pressure` 的 low 分支（:1302-1305）为：
    ```python
    if level == "low":
        # 低压力：清理缩略图
        self.clear_tier(CacheTier.THUMBNAIL)
        logger.info("Cleared thumbnail cache (low pressure)")
    ```
  - 影响: 监控循环（默认 check_interval=5s）在 `usage_ratio < warning_threshold*0.8`（即系统内存完全正常）时仍触发 "low" 压力事件，清空全部缩略图缓存。缩略图缓存（max 1000 条）永远无法积累，媒体浏览器/时间线每 5 秒重复生成缩略图，造成周期性 CPU/IO 浪费和 UI 卡顿——与缓存分层设计（THUMBNAIL 为"最容易重新生成"但应当能留住）矛盾。
  - 建议: else 分支不做任何清理（直接 pass）；或引入滞后（hysteresis），只在压力级别**上升**时触发对应清理。
  - 置信度: 高（两段代码行为直接组合即得，注释"内存使用正常"与动作自相矛盾）

- [AUD-0312] [Major] `import psutil` 位于 try 块之外，psutil 缺失时监控线程直接崩溃，try 内 `except ImportError` 是死分支
  - 位置: src/features/color/core/cache_manager.py:1594
  - 证据:
    ```python
    def _monitor_loop(self) -> None:
        """监控循环（后台线程）"""
        import psutil

        while not self._stop_event.is_set():
            try:
                memory = psutil.virtual_memory()
                ...
            except ImportError:
                logger.warning("psutil not available, memory monitoring disabled")
                break
    ```
  - 影响: `import psutil` 在 `try` 外执行。若运行环境未安装 psutil，模块导入在循环开始前抛出未捕获 ImportError，daemon 线程终止并向 stderr 打印 traceback，内存监控静默失效；try 内的 `except ImportError` 分支永远不可达，作者预期的优雅降级从未生效。
  - 建议: 将 `import psutil` 移入 try 块（或在 `start()` 中预检并记录后拒绝启动）。
  - 置信度: 高

- [AUD-0313] [Major] CacheConfig.max_memory_mb 定义但全模块未实现任何字节容量控制，缓存内存实际上限仅由"条目数 × 单帧大小"决定
  - 位置: src/features/color/core/cache_manager.py:357
  - 证据:
    ```python
    max_decode_frames: int = 32
    ...
    max_memory_mb: float = 2048.0  # 2GB
    ```
    而 LRU 淘汰只按条目数（:466）：`while len(self._cache) > self._max_size:`。grep 全库确认本文件的 `max_memory_mb` 仅出现在此定义处，无任何读取/比较逻辑（composite 侧另一个 CacheManager 有独立实现）。
  - 影响: 文档与配置承诺的 2GB 缓存内存上限完全不存在。以 4K master 帧（3840×2160×4×2B ≈ 66MB float16）计，max_master=8 即 ~530MB；max_thumbnails=1000 若缩略图为位图亦可达数百 MB，且 put 路径不感知字节。内存压力只能依赖外部 MemoryPressureMonitor 的系统级百分比兜底（而该监控自身有 AUD-0311/0312 缺陷）。
  - 建议: 在 `LRUCache.put` 中按 `total_bytes` 维护字节上限并触发淘汰（先修 AUD-0314 的统计），或在 CacheManager 层定期巡检总量。
  - 置信度: 高（"配置项存在但无效"为代码事实）

- [AUD-0314] [Major] LRUCache 字节统计系统性错误：覆盖同 key 不减旧值、evict 减的是"当前插入条目"的字节数、remove/invalidate_range 完全不减
  - 位置: src/features/color/core/cache_manager.py:455
  - 证据:
    ```python
    with self._lock:
        # 如果已存在，移除旧的
        if key in self._cache:
            del self._cache[key]

        # 添加新的
        self._cache[key] = value
        self._stats.inserts += 1
        self._stats.total_bytes += estimated_bytes

        # 如果超出最大大小，移除最旧的
        while len(self._cache) > self._max_size:
            oldest_key, oldest_value = self._cache.popitem(last=False)
            self._stats.evictions += 1
            # 估算释放的内存（简化处理）
            self._stats.total_bytes = max(
                0, self._stats.total_bytes - estimated_bytes
            )
    ```
  - 影响: (1) 同 key 重复 put 时旧条目字节数未扣减，total_bytes 单调虚增；(2) 淘汰最旧条目时扣减的是**刚插入条目**的 estimated_bytes 而非被淘汰条目的，逐次产生随机偏差；(3) `remove()`（:500-504）与 `invalidate_range()`（:532-536）删除条目不减 total_bytes。`get_stats()` 暴露的 total_mb 因此不可信，任何基于该指标的未来决策（包括实现 AUD-0313）都会建立在错误数据上。
  - 建议: 缓存条目与字节同存（`OrderedDict[key] -> (value, bytes)`），evict/remove/覆盖时按对应条目字节扣减。
  - 置信度: 高

- [AUD-0315] [Major] GPUTextureCache 淘汰/清空只丢 Python 引用，无 release 或 deferred-release 回调，违背项目 GPU 资源释放架构
  - 位置: src/features/color/core/cache_manager.py:809
  - 证据:
    ```python
    class GPUTextureCache:
        """
        GPU 纹理缓存。

        缓存 GPU 纹理对象，避免重复创建。
        注意：不持有 GPU 资源锁，纹理生命周期由池管理。
        """
    ```
    其 `put`（:855）仅 `self._cache.put(key, texture, estimated_bytes)`，淘汰路径即 LRUCache 的 `self._cache.popitem(last=False)`（:467），无任何释放钩子。
  - 影响: 被淘汰/清空的纹理对象引用计数归零时，其底层 ModernGL/GL 资源会在**执行淘汰的线程**（可能是 UI 线程或任意 put 调用线程）被析构释放，而非按项目的 deferred-release 模式交由渲染线程统一释放；跨线程释放 GL 对象轻则泄漏、重则破坏上下文。注释声称"生命周期由池管理"，但本类既无 evict 回调也无与池的任何联动机制，该契约无代码保障。注：grep 确认该类当前无外部调用方，属待接线风险。
  - 建议: LRUCache 增加可选 `on_evict(value)` 回调；GPUTextureCache 注入"投递到渲染线程释放队列"的回调后再淘汰。
  - 置信度: 中（"无释放机制"为代码事实；实际是否泄漏取决于纹理对象析构行为与未来接线方式）

- [AUD-0316] [Suggestion] CacheManager._lock 定义后从未使用，复合操作（clear_all/update_config）非原子
  - 位置: src/features/color/core/cache_manager.py:1141
  - 证据:
    ```python
    # 线程锁
    self._lock: threading.Lock = threading.Lock()
    ```
    全类无第二处 `self._lock` 引用；`clear_all()`（:1246-1260）逐层调用各子缓存 clear，中间可穿插其他线程的 put。
  - 影响: 死代码；"清空所有缓存"期间各层状态不一致（部分层已清、部分层刚被并发 put），且 `register_memory_callback` 对 `_memory_callbacks` 的追加与遍历也无同步（list.append 原子性掩盖了竞态）。
  - 建议: 删除该锁或在 CacheManager 级复合操作中真正持有；回调列表改用受锁保护的副本遍历。
  - 置信度: 高

- [AUD-0317] [Suggestion] update_config/reset_stats 直接穿透访问子缓存私有 `_cache` 属性
  - 位置: src/features/color/core/cache_manager.py:1236
  - 证据:
    ```python
    def reset_stats(self) -> None:
        """重置所有统计"""
        self.decode_cache.proxy_cache.reset_stats()
        self.decode_cache.master_cache.reset_stats()
        self.render_plan_cache._cache.reset_stats()
        self.gpu_texture_cache._cache.reset_stats()
        self.timeline_preview_cache._cache.reset_stats()
        self.audio_waveform_cache._cache.reset_stats()
        self.thumbnail_cache._cache.reset_stats()
    ```
    `update_config`（:1358-1364）同样以 `xxx_cache._cache.max_size = ...` 方式写入。
  - 影响: 破坏封装；子缓存若重构内部结构（如 LRUCache 换实现）此处静默失效。DecodeFrameCache.get（:624-634）同样直接操作 `cache._lock/_cache/_stats`。
  - 建议: 为各子缓存补 `reset_stats()`/`set_max_size()` 公共方法。
  - 置信度: 高

- [AUD-0318] [Suggestion] RenderPlanCache 的 get/put 从不传质量参数，RenderPlanCacheKey.quality 恒为默认 "preview"，不同质量层级的渲染计划互相覆盖
  - 位置: src/features/color/core/cache_manager.py:738
  - 证据:
    ```python
    def get(self, frame_index: int, viewport_w: int = 1920, viewport_h: int = 1080) -> Optional[Any]:
        ...
        key = RenderPlanCacheKey(
            frame_index=frame_index,
            viewport_width=viewport_w,
            viewport_height=viewport_h,
        )
    ```
    键定义（:188）含 `quality: str = "preview"`，但 get/put 签名无 quality 形参。
  - 影响: proxy 与 master 两种质量层级的渲染计划共用同一键，后写入者覆盖前者，切质量时命中过期计划（渲染结果错误级别）或命中率假性下降。键类的设计意图（文档"缓存键设计：帧号 + 质量级别 + 分辨率 + 色彩空间"）未落地。
  - 建议: get/put 增加 quality 形参并纳入键。
  - 置信度: 中（缺陷为代码事实；实际影响取决于未来调用方是否区分质量）

- [AUD-0319] [Suggestion] MemoryPressureMonitor.start/stop 的 _is_running 为无锁 check-then-act，可能双线程启动
  - 位置: src/features/color/core/cache_manager.py:1560
  - 证据:
    ```python
    def start(self) -> None:
        """启动监控"""
        if self._is_running:
            return

        self._is_running = True
        self._stop_event.clear()
    ```
  - 影响: 两个线程并发 start 均可通过 `if self._is_running` 检查，产生两条监控线程，双倍频率触发 on_memory_pressure（叠加 AUD-0311 会加速清缓存）。
  - 建议: 用 Lock 保护 start/stop（与 CacheWarmer 的做法对齐，但注意避免复刻 AUD-0310 的重入错误）。
  - 置信度: 高（竞态存在是事实；触发窗口小，故仅 Suggestion）

### 通过项
- LRUCache 主体 get/put/contains/remove/clear/invalidate_range 均在 `threading.Lock` 保护下，单缓存级线程安全成立；stats 读取返回副本避免外部直接改写
- 全局 `get_cache_manager`/`reset_cache_manager` 使用 `_global_lock` 正确保护单例创建/销毁
- `CacheStatistics.hit_rate` 对 total==0 有除零保护
- 帧缓存键（FrameCacheKey）为 frozen dataclass，含 clip/frame/quality/尺寸/色彩空间，键设计完整（RenderPlanCacheKey 见 AUD-0318）
- `_warmup_loop` 对单帧解码异常有捕获并记日志、失败帧跳过继续，不会因单帧错误中止预热
- 无内嵌 GLSL，无浮点字面量类问题

---

## src/features/color/core/color_manager.py (行数: 497)

### 发现

- [AUD-0303] [Major] _convert_via_lcms 将 HDR float 图像 clamp 到 0..1 并经 8bit 中转；uint16 输入路径静默失败
  - 位置: src/features/color/core/color_manager.py:460
  - 证据:
    ```python
    is_float = np.issubdtype(image.dtype, np.floating)

    # BGR → RGB
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB) \
        if len(image.shape) == 3 else image

    if is_float:
        rgb = (np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)

    pil_img = Image.fromarray(rgb)
    ```
  - 影响: 项目内部为 HDR float16/32 管线，导出色彩空间转换时：(1) 所有 >1.0 的高光被 `np.clip(rgb, 0.0, 1.0)` 硬截断，HDR 亮度信息丢失；(2) 即便值域合法，转换在 uint8 中间表示上进行，gradient banding（8bit 量化误差经 LCMS 传递放大）。(3) uint16 三通道输入时 `Image.fromarray` 不支持 (H,W,3) uint16 会抛异常，被 `convert_for_export`（:340-342）捕获后**静默返回未转换图像**，导出目标色彩空间未被应用且仅有一条 error 日志。
  - 建议: float 路径改用 ImageCms 的高位深支持（uint16 中转）或保留 float 的 GPU/OCIO 路径；至少在 HDR 值 >1 时记录告警并对 uint16 输入显式支持或显式报错。
  - 置信度: 高（clamp/量化/静默降级均为代码事实）

- [AUD-0304] [Major] embed_icc 以默认参数重新编码 JPEG（quality 75），二次有损压缩且丢失 EXIF 等元数据
  - 位置: src/features/color/core/color_manager.py:365
  - 证据:
    ```python
    try:
        with Image.open(path) as pil_img:
            pil_img.save(path, icc_profile=icc_data)
        return True
    ```
  - 影响: 该函数在 `FileIO.save_image()` 成功后调用，对已落盘的最终成品再开再存：JPEG 未指定 quality，Pillow 默认 75，用户以高质量导出的 JPEG 被无声重压到 q75（画质不可逆损失）；同时仅传 `icc_profile`，EXIF/XMP 等元数据未复用即丢失。PNG/TIFF 路径无损但元数据同样丢失。
  - 建议: 打开时提取并回写 `exif`/`xmp` 等信息；JPEG 显式 `quality='keep'`（对已存在 JPEG）或使用可配置质量。
  - 置信度: 高

- [AUD-0305] [Major] apply_defaults_to_new_image 的 float 分支把 float16/float64 输入误走 /255 整数缩放路径
  - 位置: src/features/color/core/color_manager.py:424
  - 证据:
    ```python
    elif bit_depth == '32-bit float' and image.dtype != np.float32:
        if image.dtype == np.uint16:
            image = image.astype(np.float32) / 65535.0
        else:
            image = image.astype(np.float32) / 255.0
    ```
  - 影响: 条件只排除 `np.float32`。若新建图像画布为 float16（项目 HDR 内部格式之一）或 float64，`else` 分支将其再除以 255，画面亮度骤降 255 倍（近黑），且该函数返回值直接作为新图像数据，属于数据级错误。当前主要风险取决于新建画布是否会出现非 float32 浮点 dtype，但逻辑上对"任何非 float32 浮点"均处理错误。
  - 建议: 先判 `np.issubdtype(image.dtype, np.floating)` 并按已是线性 0..1 处理（仅做 `.astype(np.float32)`），整数 dtype 再走 /255、/65535。
  - 置信度: 中（代码缺陷确定；是否可达取决于上游新建画布的 dtype 惯例）

- [AUD-0306] [Major] ColorManager 单例 __new__ 无锁，并发首次实例化可产生双实例并重复执行昂贵初始化
  - 位置: src/features/color/core/color_manager.py:123
  - 证据:
    ```python
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    ```
  - 影响: 两个线程同时首次调用 `get_color_manager()` 时均观察到 `_instance is None`，各自创建实例并完整执行 `__init__`（`_init_profiles` 触发系统 ICC 目录扫描 + LittleCMS profile 创建）。`_initialized` 标志只防同一实例重复初始化，不防两个不同对象；先创建者被后创建者覆盖 `_instance`，持有旧引用的一方与单例状态（config_manager、_ocio_config）分裂，后续 reconfigure 只作用于其中一个。
  - 建议: 加类级锁双重检查（对照 cache_manager.get_cache_manager 的正确做法），或模块级惰性单例。
  - 置信度: 中（竞态成立取决于首次调用是否跨线程；导入器/后台加载器并存时常见）

- [AUD-0307] [Major] _get_pil_profile 不查 bundled profiles 目录，与 get_profile_for_space 的三级查找不一致，目标空间静默退化为 sRGB
  - 位置: src/features/color/core/color_manager.py:489
  - 证据:
    ```python
    def _get_pil_profile(self, color_space: str):
        """Return a PIL ImageCms profile object for *color_space*."""
        if color_space == 'sRGB':
            return self._srgb_profile or ImageCms.createProfile('sRGB')

        fpath = self._system_profiles.get(color_space)
        if fpath and os.path.isfile(fpath):
            ...
        # Graceful fallback
        return self._srgb_profile or ImageCms.createProfile('sRGB')
    ```
    而导出字节路径 `get_profile_for_space`（:303-312）会先查 `src/shared/color/profiles/` 的 bundled 文件。
  - 影响: 在未安装对应系统 ICC 的机器上（Windows/Linux 常见缺 Display P3、CMYK 配置），`convert_for_export` 的目标配置文件静默落到 sRGB——例如用户选择导出到 "CMYK (Coated FOGRA39)"，实际执行的是 X→sRGB 转换，输出色彩空间错误且无任何日志。嵌入 ICC（走 get_profile_for_space）与实际像素转换（走 _get_pil_profile）使用不同源，二者可能不一致：文件里嵌 A 空间 profile，像素却是按 sRGB 转的。
  - 建议: `_get_pil_profile` 复用与 `get_profile_for_space` 相同的三级查找（bundled → system → sRGB），理想上两者共用同一解析函数。
  - 置信度: 高（两条查找路径的差异为代码事实）

- [AUD-0308] [Suggestion] _scan_system_profiles 的 os.listdir 无异常保护，目录列举失败会中断构造
  - 位置: src/features/color/core/color_manager.py:170
  - 证据:
    ```python
    for profile_dir in dirs:
        if not os.path.isdir(profile_dir):
            continue
        for fname in os.listdir(profile_dir):
    ```
  - 影响: `isdir` 通过但列举失败（权限受限、竞态删除）时抛 OSError，沿 `_init_profiles` → `__init__` → `get_color_manager()` 直接传播到调用方，色彩管理整体不可用。本文件其余 IO 均有 try 保护，唯独此处裸奔。
  - 建议: 对每个目录的列举包 try/except 并记日志。
  - 置信度: 中（触发条件罕见，但防护缺失明确）

- [AUD-0309] [Suggestion] 多处宽泛 `except Exception: pass` 静默吞错且无日志
  - 位置: src/features/color/core/color_manager.py:218
  - 证据:
    ```python
    try:
        with Image.open(path) as pil_img:
            icc_data = pil_img.info.get('icc_profile')
            if icc_data:
                return self.identify_color_space(icc_data), icc_data
    except Exception:
        pass
    return None
    ```
    同模式还见于 `identify_color_space`（:252-253）、`_init_profiles`（:147-149）、`get_profile_for_space` 各读取点（:296-300, :310-312, :317-320）、`_get_pil_profile`（:492-494）。
  - 影响: 损坏 ICC、权限错误、Pillow 行为变化等均被无声吞掉，ICC 检测退化为"无 profile"，用户难以区分"确实没有 ICC"与"读取失败"，排障无迹可循。
  - 建议: 至少 `logger.debug/warning` 记录异常再降级。
  - 置信度: 高（事实明确，影响属可观测性）

### 通过项
- 线程安全：初始化完成后 `_srgb_profile`/`_system_profiles` 为只读；`reconfigure` 仅重置 `_ocio_config`（OCIO 当前无读取消费点，风险暂不成立；单例竞态另见 AUD-0306）
- GPU 资源生命周期：不涉及 OpenGL 资源
- 无内嵌 GLSL，无浮点字面量/循环边界类问题
- `.toos` 格式的 ICCP chunk 读取有完整异常回退（按无 ICC 处理，注释明确色彩标签 fallback 路径）
- 配置访问均有默认值兜底（'relative'、'sRGB' 等），缺配置不抛错
- `embed_icc` 对扩展名白名单校验（jpg/jpeg/png/tif/tiff）后才处理

---

## src/features/color/core/color_output.py (行数: 87)

### 发现

- [AUD-0301] [Suggestion] update() 四字段赋值非原子，跨线程直接读属性存在撕裂读可能
  - 位置: src/features/color/core/color_output.py:62
  - 证据:
    ```python
    def update(self, texture_id, width, height):
        """Update the cached ColorOutput with a new render result."""
        self._texture_id = texture_id
        self._width = width
        self._height = height
        self._dirty = False
        self._revision += 1
    ```
  - 影响: 类无任何锁。当前唯一写路径是 `image_engine._on_color_output_ready`（pyqtSlot，经 Qt 跨线程信号在接收者线程串行执行），消费者经 `color_output_changed` 信号同线程读取，故现状安全；但任何未来调用方若从渲染线程直接读 `texture_id`/`width`/`height`（文档鼓励"Consumed by the COLOR node"），可能读到新 id 配旧尺寸的组合。`revision += 1` 依赖 GIL 原子性尚可。
  - 建议: 类注释明确"仅限 ImageEngine 线程访问"，或用简单锁/一次性元组赋值（`self._state = (id, w, h, rev)`）保证读端一致快照。
  - 置信度: 低（现有调用链经信号串行化，风险仅存在于未来用法）

- [AUD-0302] [Suggestion] compute_proxy_size 缺少零尺寸/极端长宽比防御，int 截断可产生 0 维边
  - 位置: src/features/color/core/color_output.py:86
  - 证据:
    ```python
    if orig_w <= max_dim and orig_h <= max_dim:
        return orig_w, orig_h
    scale = min(max_dim / orig_w, max_dim / orig_h)
    return int(orig_w * scale), int(orig_h * scale)
    ```
  - 影响: `orig_w/orig_h` 为 0 时除零异常（当前上游 `_do_render_color_output` 已过滤 `img_w <= 0`，故为纵深防御缺失）；极端长宽比（如 40000×2 的全景图，scale=2048/40000=0.0512）时 `int(2*0.0512)==0`，返回 0 高度尺寸，下游创建 0 尺寸纹理/FBO 会失败。
  - 建议: 结果用 `max(1, int(round(...)))` 钳制，并断言输入 > 0。
  - 置信度: 低（0 输入已被上游挡住；极端长宽比在 4K 基准素材下罕见）

### 通过项
- GPU 资源生命周期：本类只缓存整数纹理 ID，纹理对象所有权在 AsyncRenderer/composite resource_pool（`output_node.py` 经 `release_texture_by_id` 释放，`async_renderer._process_garbage` 有 2 秒 deferred-release），ID 替换不构成泄漏
- 线程安全（现状）：写入经 pyqtSlot 信号在接收者线程串行化，`texture_id`/`is_dirty`/`revision` 的当前读写路径无并发
- 无 GLSL、无 clamp、无浮点字面量问题（纯尺寸计算）
- revision 单调递增、dirty 标记语义清晰，无绕过 undo/command 的模型写入（纯运行时缓存状态）

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-0310 |
| Major | 10 | AUD-0303, 0304, 0305, 0306, 0307, 0311, 0312, 0313, 0314, 0315 |
| Suggestion | 9 | AUD-0300, 0301, 0302, 0308, 0309, 0316, 0317, 0318, 0319 |

特别说明：`src/features/color/core/cache_manager.py` 的三个编排类（CacheManager/CacheWarmer/MemoryPressureMonitor）当前在全仓库无外部调用方（composite 使用独立的 render/cache_manager.py），其 Critical/Major 缺陷为"接线即触发"型；而 color_manager.py 的 AUD-0303/0304/0307 位于导出主链路，影响当前版本的真实输出画质与色彩正确性，建议优先处理。
