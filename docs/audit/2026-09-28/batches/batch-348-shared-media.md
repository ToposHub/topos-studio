# 批次报告：batch-348 — src/shared/media/

- 审查日期：2026-09-28
- 编号区间：AUD-34800–AUD-34899（实际使用 AUD-34800–AUD-34815）
- 审查范围：7 个文件，2,801 行，九维度（线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性 / 异常与取消 / 输入验证 / 性能 / 类型注解）
- 汇总：**Critical 0 / Major 2 / Suggestion 14**

严重度校准依据（跨库核对）：
- `classify_render_frame_path` / `get_frame_render_path` 仅被 `src/shared/media/__init__.py` re-export 与 tests 引用，无生产调用点（grep 全库确认）→ 诊断类缺陷定为 Suggestion。
- `frame_cache.py` 的 `FrameCache` 无生产导入方（`playback_clock` 使用 `cache.py` 的 `DualFrameCache`），但属 `src.shared.media` 公共导出 API。
- `DecodeSurfaceLease` 的在树适配器 `macos_vt/topos_surface.py` 的 `release_surface` 重复调用为良性（幂等置 `leased=False`），双释放风险针对第三方/其他平台适配器。

---

## src/shared/media/format_registry.py (行数: 222)

### 发现

- [AUD-34800] [Suggestion] `.dng` 双重注册为视频与图像，`get_media_type` 恒返回 'video'
  - 位置: src/shared/media/format_registry.py:43, 82, 192-194
  - 证据:
    ```python
        '.dng',                    # CinemaDNG (also in IMAGE_EXTENSIONS for stills)
    ```
    ```python
        ext = _get_ext(path)
        if ext in VIDEO_EXTENSIONS:
            return 'video'
    ```
  - 影响: `is_video('.dng')` 与 `is_image('.dng')` 同时为 True；`get_media_type` 因先查 VIDEO，把所有单张 DNG 照片（RAW stills）归类为 'video'，IMAGE 分支对 `.dng` 不可达。依赖 `get_media_type` 分流的调用方会把静帧 DNG 误导入视频路径。
  - 建议: 明确优先级策略（如 CinemaDNG 序列判视频、单文件判图像需目录上下文），或在文档/返回值中显式标注该歧义；至少让 `get_media_type` 的判定顺序与注释一致。
  - 置信度: 高（行为确定）；影响取决于各调用方使用哪个 API，标中。

### 通过项
- 输入验证：`is_toos_image` 按内容 magic（TPIM）判定且 OSError 安全返回 False，符合 spec §13
- 类型注解完整（frozenset[str]、返回 bool/str）
- 纯常量注册表 + 无副作用函数，无线程/GPU/信号/持久化维度问题
- `ALL_MEDIA_EXTENSIONS` 并集完整覆盖五类扩展名集合

---

## src/shared/media/frame.py (行数: 481)

### 发现

- [AUD-34801] [Suggestion] `has_alpha` 名称启发式对未注册格式误判，且含永不可达的死分支
  - 位置: src/shared/media/frame.py:404
  - 证据:
    ```python
        fmt = self.format_info
        if fmt:
            return fmt.has_alpha
        # 根据格式名称推断
        return 'a' in self.pixel_format.lower() or self.pixel_format in ('rgba', 'bgra', 'rgba64', 'rgba64le')
    ```
  - 影响: 走到 fallback 时（格式不在 PIXEL_FORMATS 中），任何名字含 'a' 的未注册格式（如 FFmpeg 的 'gray'、'pal8'）会被判为含 Alpha；后半句 `in ('rgba', ...)` 列出的名字全部已注册，属于死分支。
  - 建议: fallback 改为白名单匹配（如 `pixel_format.startswith(('rgba','bgra','yuva','gbrap'))`），删除死分支。
  - 置信度: 高（逻辑确定）；实际触发需未注册格式名，标中。

- [AUD-34802] [Suggestion] `__post_init__` 从 planes[0] 推导宽高，对每平面 W/2×H/2 的 CFA/TRAW 格式得出半尺寸
  - 位置: src/shared/media/frame.py:357-360（对照 216-220 的格式定义注释）
  - 证据:
    ```python
                h, w = self.planes[0].shape[:2]
                object.__setattr__(self, 'width', w)
                object.__setattr__(self, 'height', h)
    ```
    ```python
        # TRAW（Topos RAW，v1.6 批 1）：Bayer CFA 4 相位平面 R/Gr/Gb/B——
        # 每平面均为 W/2×H/2（(4,2,0) 的双维减半语义，非色度下采样）。
    ```
  - 影响: 构造 `topos_cfa12le`/`topos_cfa16le` 的 DecodedFrame 且未显式传 width/height 时，帧尺寸被记为真实分辨率的一半，下游缩放/显示/缓存计量全部失真。现有解码器若总是显式传尺寸则不触发。
  - 建议: 在 `__post_init__` 中对 planes 数量/格式为 CFA 类（4 相位平面）时禁止从 plane0 推导尺寸，或直接要求此类格式显式传 width/height。
  - 置信度: 中（代码路径确定，是否可达取决于解码器构造习惯）。

- [AUD-34803] [Suggestion] `to_rgb` 对 BGR 系返回负 stride 的 flip 视图
  - 位置: src/shared/media/frame.py:463-465
  - 证据:
    ```python
            elif self.pixel_format in ('bgr24', 'bgra', 'bgra64'):
                # BGR -> RGB
                return np.flip(self.data[..., :3], axis=-1)
    ```
  - 影响: `np.flip` 返回负 stride 视图且非 C 连续；若结果直接送 `Texture.write()` 等 GPU 上传路径，可能报错或产生错乱行序（ModernGL 通常要求连续缓冲）。通道翻转而非行翻转即可避免。
  - 建议: 改为 `self.data[..., ::-1]` 仍为视图但仅通道维负 stride 时同样需注意；稳妥做法返回 `np.ascontiguousarray(...)` 或通道切片翻转 `data[..., :3][..., ::-1]` 前确认消费方约定。
  - 置信度: 低（未发现当前生产调用链把该结果直传 GL）。

- [AUD-34804] [Suggestion] 非 frozen dataclass 上使用 `object.__setattr__`，并冗余局部 `import numpy`
  - 位置: src/shared/media/frame.py:288, 344-350, 458
  - 证据:
    ```python
        if self.dtype is None:
            if self.data is not None:
                import numpy as np
                object.__setattr__(self, 'dtype', self.data.dtype)
            elif self.planes:
                import numpy as np
                object.__setattr__(self, 'dtype', self.planes[0].dtype)
    ```
  - 影响: `DecodedFrame` 未声明 frozen，`object.__setattr__` 无必要且绕过 dataclass 语义，误导读者以为有不可变约束；局部 `import numpy as np` 与模块顶层导入重复（顶部已 `import numpy as np`，局部名遮蔽）。
  - 建议: 改为普通赋值 `self.dtype = ...`，删除冗余局部导入。
  - 置信度: 高。

### 通过项
- 子采样除数逻辑核对正确（4:4:4/4:2:2/4:2:0/4:1:1 的宽高除数分支）
- PIXEL_FORMATS 注册表与 Topos Video Codec 2.0 / TRAW 枚举注释一致（抽查 topos_yuva*、topos_cfa*、gbrph16le 语义）
- `bit_depth` 推断链（fmt → plane_infos → dtype → 名称正则 → 8）顺序合理，float32 正确映射 32
- 无 GPU 资源持有、无信号、无持久化维度
- 类型注解完整（Optional/tuple/property 返回类型）

---

## src/shared/media/frame_cache.py (行数: 614)

### 发现

- [AUD-34805] [Major] `put()` 更新已存在键时完全绕过内存上限与驱逐检查
  - 位置: src/shared/media/frame_cache.py:309-328
  - 证据:
    ```python
            # 检查是否已存在
            if key in self._entries:
                # 更新现有条目
                old_entry = self._entries[key]
                self._current_memory -= old_entry.memory_size
                self._current_memory += memory_size

                entry = FrameCacheEntry(
                    frame=frame,
                    frame_index=frame.frame_index,
                    is_master=is_master,
                    memory_size=memory_size
                )
                self._entries[key] = entry
    ```
  - 影响: 文档契约"如果内存超限，会触发 LRU 驱逐"在更新路径不成立：同 key 反复放入更大帧（如代理→母版切换、reformat 后回填）时 `_current_memory` 可持续超过 `_max_memory` 且不会触发任何驱逐，内存预算失效。缓解因素：当前全库未发现该类的生产导入方（`playback_clock` 用 `cache.py` 的 `DualFrameCache`），但它是 `src.shared.media` 的公共导出 API。
  - 建议: 更新路径在加入新尺寸后同样执行 `while` 驱逐循环（至少在超限时驱逐其他条目或拒绝更新），或明确文档化"更新路径不受上限约束"并由调用方保证。
  - 置信度: 高（行为确定；暴露面低）。

- [AUD-34806] [Suggestion] `_evict_one` 驱逐原因统计为事后猜测，类目可能记错
  - 位置: src/shared/media/frame_cache.py:581-585
  - 证据:
    ```python
        # 记录驱逐原因
        if self._current_memory > self._max_memory:
            self._stats['evictions_for_memory_limit'] += 1
        elif len(self._entries) >= self._max_entries:
            self._stats['evictions_for_entry_limit'] += 1
    ```
  - 影响: 原因在驱逐之后按"当前还超不超"推断：同时超内存与条数时只记内存；批量驱逐的中间若干次可能两个条件都不满足而漏记，`evictions_for_memory_limit/entry_limit` 与真实触发原因不符，监控数据失真。
  - 建议: 由 `put()` 的驱逐循环把触发原因（memory/entries）作为参数传入 `_evict_one` 记录。
  - 置信度: 高。

- [AUD-34807] [Suggestion] `get_oldest_entries` 文档与实现不符；`_evict_one` 每次驱逐全表扫描
  - 位置: src/shared/media/frame_cache.py:529-534, 561-569
  - 证据:
    ```python
            Returns:
                最旧的条目列表（按 last_used_time 排序）
    ```
    ```python
        with self._lock:
            # OrderedDict 保持插入顺序，前面的是最旧的
            entries = list(self._entries.values())
            return entries[:count]
    ```
    ```python
        for key, entry in self._entries.items():
            idle = entry.idle_time()
    ```
  - 影响: 实现返回的是 OrderedDict 序（因 get 时 move_to_end 实为 LRU 访问序），注释却说"插入顺序"，docstring 又说按 last_used_time 排序——三者不一致，维护者易误用。`_evict_one` 每次 O(n) 扫描，`put()` 驱逐循环最坏 O(n·k)；max_entries=1000 时可接受但接近上限的稳态插入有可感知开销。
  - 建议: 统一 docstring/注释/实现语义（LRU 访问序）；`_evict_one` 可直接取 OrderedDict 首个满足 idle 条件的条目，避免每次全表找最小 last_used_time。
  - 置信度: 高。

### 通过项
- 线程安全：所有公开方法（get/get_entry/put/evict_*/clear/get_stats/get_memory_info/has_frame/__len__/__contains__）均在 `RLock` 内操作 `_entries` 与 `_current_memory`
- 异常路径无宽泛 except；统计读取均在锁内
- `time.monotonic` 用于 LRU 时序（不受系统时钟跳变影响）
- 无 GPU 资源持有、无信号、无持久化维度
- 类型注解完整

---

## src/shared/media/frame_path.py (行数: 516)

### 发现

- [AUD-34808] [Major] `release()` 回调抛异常时状态不落位，重试将重复调用 `release_callback`（双释放风险）
  - 位置: src/shared/media/frame_path.py:183-186（对照 201-206 `abort()` 的 try/finally）
  - 证据:
    ```python
            if self.release_callback is not None:
                self.release_callback()
            self._views = None
            self._state = DecodeSurfaceState.RELEASED
    ```
    而 `abort()` 为：
    ```python
            try:
                if self.release_callback is not None:
                    self.release_callback()
            finally:
                self._views = None
                self._state = DecodeSurfaceState.RELEASED
    ```
  - 影响: 若适配器的 `release_callback` 抛异常（部分释放后失败），lease 停留在 UNMAPPED/FENCED；调用方按协议重试 `release()` 会再次执行回调——对资源型回调（GPU ring slot、D3D11 shared handle、CVPixelBuffer ref）构成双释放/使用后释放风险。在树适配器 `topos_surface.release_surface` 幂等故良性，但该类是平台中立契约，第三方后端不受保护。`fence()`（156-165 行）回调异常同样不落位，重试会二次插 fence。
  - 建议: `release()`/`fence()`/`unmap()` 对回调采用与 `abort()` 一致的 try/finally（或失败即标记 RELEASED 并记日志），保证状态机在任何回调异常下仍单向前进。
  - 置信度: 中（协议级风险确定；实际危害取决于适配器回调实现）。

- [AUD-34809] [Suggestion] DECODING 状态无异常恢复出口，异常路径可永久卡死 lease
  - 位置: src/shared/media/frame_path.py:137-150, 197-200
  - 证据:
    ```python
    def end_decode(self) -> None:
        with self._lock:
            self._require(DecodeSurfaceState.DECODING)
            self._state = DecodeSurfaceState.MAPPED

    def unmap(self) -> None:
        with self._lock:
            self._require(DecodeSurfaceState.MAPPED)
    ```
    ```python
            if self._state is DecodeSurfaceState.RELEASED:
                return
            self._require(DecodeSurfaceState.ACQUIRED)
    ```
  - 影响: 若调用方在 `begin_decode` 后异常且未调 `end_decode`，lease 停在 DECODING：`unmap` 要求 MAPPED、`abort` 要求 ACQUIRED、`release` 要求 UNMAPPED/FENCED，全部拒绝——槽位泄漏直到池 close 报错。在树唯一调用方 `topos_binding.py:1064-1077` 已用 try/finally 包裹 `begin/end_decode`，故为协议健壮性缺口而非现行缺陷。
  - 建议: 为 `abort()` 增加"DECODING 亦可中止"出口（视图已由本类管理，回调释放前先作废 `_views`），或在协议文档中强制调用方 finally 化 end_decode。
  - 置信度: 高（状态机事实）；影响标低（在树调用方正确）。

- [AUD-34810] [Suggestion] GBR/浮点平面 RGB 格式被归类为 YUV 渲染路径；`classify_decode_path` 不产出 CVMETAL 变体
  - 位置: src/shared/media/frame_path.py:443-449, 401-407（gpu_frame.py:499 `get_frame_render_path` 仅识别 component=="RGBA" 的同类缺陷一并归入本条）
  - 证据:
    ```python
    if decode_path.is_zero_copy:
        is_rgb = 'rgb' in pixel_format.lower() or 'rgba' in pixel_format.lower()
        try:
            return RenderFramePath.qualify_external(graphics_api, is_rgb)
    ```
    ```python
    if zero_copy_available:
        if decoder_type == 'videotoolbox':
            return DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY
    ```
  - 影响: 平面序 GBR 名（gbrp*/gbrap*/topos_gbrap10a12）与浮点 `gbrph16le` 不含 'rgb' 子串 → external 帧被标为 `*_EXTERNAL_YUV`，CPU 平面路径标为 `PLANAR_YUV_R16*_UPLOAD`（且 float16 会被标成 R16UI 整数路径）；与模块验收标准"HUD/日志可以准确展示当前路径"不符。`classify_decode_path` 也永不返回 Phase 5 的 `MACOS_VT_CVMETAL_ZERO_COPY`，Metal 零拷贝解码无法通过该工厂表达。当前无生产调用点（仅 tests + re-export），故仅诊断/遥测失真，不影响像素。
  - 建议: is_rgb 判定改为 `('rgb' in name) or name.startswith(('gbr', 'topos_gbr'))`；为 classify_decode_path 增加 graphics_api 参数或独立的 CVMetal 判定入口；浮点平面格式映射到专用路径值。
  - 置信度: 高（分类逻辑确定）；影响低（无生产消费方）。

### 通过项
- 状态机输入验证严格：`__post_init__` 校验 backend 非空、memory_type ∈ {0,1}；`_require` 全程显式 `DecodeSurfaceProtocolError`
- GPU 生命周期契约正确：memory_type=1（GPU）强制 fence 后才可 release（179-182 行）
- `RenderFramePath.from_serialized` 未知值显式 ValueError（符合 .clinerules:11），旧值别名透明映射且别名表置于模块级避免 Enum 成员化
- `try_from_serialized` 遥测安全解析与严格解析职责分离清晰
- `views` 属性在 unmap 后拒绝访问，防止 use-after-unmap
- 类型注解完整；锁（RLock）覆盖全部状态迁移

---

## src/shared/media/gop_index.py (行数: 407)

### 发现

- [AUD-34811] [Suggestion] `build_gop_index` 宽泛 `except Exception` 仅 debug 级日志
  - 位置: src/shared/media/gop_index.py:297-299
  - 证据:
    ```python
        except Exception as e:  # noqa: BLE001 - 索引构建绝不向调用方抛异常
        logger.debug("gop index build failed for %s: %s", path, e)
        return None
    ```
  - 影响: "绝不抛异常 + 回退无索引路径"是有意设计且注释已声明，但失败只留 debug 日志：生产环境（默认 INFO 级）中索引持续构建失败（如损坏容器、av 版本不兼容）完全不可见，seek 性能退化无从排查，与 .clinerules"宽泛 except 不得掩盖领域错误"精神相悖。
  - 建议: 首次失败升 warning（或聚合限速告警），保留 debug 级去重；至少区分 ImportError（环境缺 av）与容器解析错误分别记录。
  - 置信度: 高（代码事实）；影响标中。

- [AUD-34812] [Suggestion] `save_gop_index` 的 identity `setdefault` 为死代码；identity 不匹配时仍写永久 miss 的缓存文件
  - 位置: src/shared/media/gop_index.py:316-324
  - 证据:
    ```python
        if index.identity is None:
            index.identity = key
        payload = index.to_dict()
        # 身份字段冗余进内容，加载时可二次校验
        payload.setdefault("identity", {
            "source_path": key.source_path,
            "source_mtime_ns": key.source_mtime_ns,
            "source_size": key.source_size,
        })
    ```
  - 影响: `index.identity` 已在上方被赋值为 key（非 None 时本就存在），`to_dict()` 必然写出 "identity" 键，`setdefault` 永不生效（死代码，注释 misleading）。若调用方传入 identity 与 key 不一致的 index，文件会按 key 的 hash 落盘但内容 identity 不同 → `load_gop_index` 永远校验失败且该文件永不清理。
  - 建议: 删除 setdefault；开头显式校验 `index.identity` 与 key 一致（不一致直接 return False 或覆盖为 key）。
  - 置信度: 高（死代码确定）；影响低。

### 通过项
- 持久化正确性：mkstemp + os.replace 原子写，异常路径清理临时文件，模块级 `_save_lock` 防同进程交错写
- 加载校验完整：schema 版本、keyframe 升序、Fraction 字段类型、`ZeroDivisionError`（fps_den/tb_den 为 0）均捕获后返回 None
- 身份失效设计：mtime_ns+size 入 hash 与内容双重校验，源文件变化自动失效
- `frame_to_pts` 舍入规则与 `ffmpeg_source._frame_to_pts`（1798-1803 行）逐位一致（Fraction + 1/2 截断），核对通过
- `get_or_build_gop_index` 大文件护栏（8GB）与 force 重建语义清晰
- 线程模型克制（模块内不起线程，职责归调用方），无 GPU/信号维度

---

## src/shared/media/gpu_frame.py (行数: 533)

### 发现

- [AUD-34813] [Suggestion] `GpuDecodedFrame` 未声明 frozen，违背模块自述的线程安全设计原则
  - 位置: src/shared/media/gpu_frame.py:16, 293-294
  - 证据:
    ```python
    - 数据类 frozen=True 保证线程安全（planes 不可变）
    ```
    ```python
    @dataclass
    class GpuDecodedFrame:
    ```
  - 影响: 模块"设计原则"与类文档都声称 frozen=True 保证线程安全，但类是普通可变 dataclass，且 `planes: Sequence[GpuFramePlane]` 可传入 list 并在发布后被生产者线程修改——解码线程发布帧、渲染线程消费帧的场景下存在数据竞争窗口（GpuFramePlane/GpuPlaneResource 本身 frozen，缓解了单平面内部被改的风险）。
  - 建议: 加 `frozen=True`（release 字段已是独立可变对象，不受影响），或修改文档移除 frozen 声明并规定 planes 必须传不可变 tuple。
  - 置信度: 高（声明与代码不一致确定）；实际竞争未证实，风险标中。

- [AUD-34814] [Suggestion] `destructor_policy="enqueue"` 未实现，GC 时静默降级为泄漏告警
  - 位置: src/shared/media/gpu_frame.py:421-426
  - 证据:
    ```python
        elif policy == "enqueue":
            # Future: enqueue_gpu_release(self.release) when render-thread queue exists
            logger.warning(
                f"GpuDecodedFrame.__del__: frame={self.frame_index} "
                f"enqueue policy not yet implemented, falling back to warn."
            )
    ```
  - 影响: 生产者若设置 policy="enqueue"（文档承诺"__del__ 把 release 请求投递到全局 release queue"），实际行为是泄漏 + 每次告警，资源回收承诺不成立；策略值成为脚枪。grep 全库当前无其他文件使用该策略值，属预留接口未兑现。
  - 建议: 在实现队列前拒绝该策略值（__post_init__ 或 GpuFrameRelease 构造时抛 ValueError），或把文档降级为"计划中"。
  - 置信度: 高（代码事实）；影响低（无使用方）。

- [AUD-34815] [Suggestion] `normalize_color_matrix` 未知串直通与 range 归一化行为不一致；数值 9 映射 'bt2020' 而非 'bt2020nc'；VALID_* 集合未被强制
  - 位置: src/shared/media/gpu_frame.py:49-61, 63-70, 90-93, 112-113
  - 证据:
    ```python
        '9': 'bt2020',
    ```
    ```python
        if isinstance(value, str):
            return _COLOR_MATRIX_ALIASES.get(value.lower().strip(), value.lower().strip())

        return 'bt709'
    ```
    ```python
        if isinstance(value, str):
            return _COLOR_RANGE_ALIASES.get(value.lower().strip(), 'limited')
    ```
  - 影响: matrix 对未知串原样直通（下游 `kernels_video._MATRIX_COEFFS` 会显式报错兜底，但 GL 侧消费方若无校验则以任意键参与查找），range 却静默默认 'limited'——同一模块两种失败语义；FFmpeg AVCOL_SPC_BT2020_NCL=9 归一为 'bt2020' 而非表内已有的 'bt2020nc'（转换系数恰好相同故暂无像素误差，但字典键分裂）；模块定义的 `VALID_COLOR_MATRICES/VALID_COLOR_RANGES` 无任何强制使用点。另 `normalize_color_matrix(value)`/`normalize_color_range(value)` 参数缺类型注解（Union[str, int, None]）。
  - 建议: 统一未知值策略（直通但记 debug，或归 'unknown'）；数值 9 改映射 'bt2020nc'；让两个 VALID_* 集合真正承担校验或删除；补参数注解。
  - 置信度: 高（行为确定）；色彩影响标低（下游 Metal 路径有二次校验）。

### 通过项
- `GpuFrameRelease` exactly-once 正确：锁内置位、锁外执行回调（防死锁），`release_when_fenced` 未确认 fence 时宁可不释放（符合"宁可延迟回收"契约）
- `GpuFramePlane.__post_init__` 强制非 GL 后端携带 GpuPlaneResource（P2-03 契约的输入验证）
- `GpuDecodedFrame.__post_init__` 完整性校验（zero_copy 需 planes、CPU 路径禁 zero_copy、尺寸正数、duration 非负、设备一致性）
- `validate_for_device` 契约失败返回机器可读串，device mismatch 判定双向（帧级 + plane 级）
- `__del__` 的 destructor_policy 分级（warn_only 默认）正确规避 GC 线程调用 GL 的铁律，`hasattr(self, 'release')` 防半初始化
- `safe_release_gpu_frame` 广泛用于 async_renderer/video_frame_node/file_controller，类型分发安全
- 无信号防递归维度；类型注解总体完整（GpuApi/TextureTarget/PlaneComponent Literal 化）

---

## src/shared/media/gpu_frame_handle.py (行数: 28)

### 发现

（无）

### 通过项
- 通过（无逻辑）：纯 `@dataclass` 字段定义（TimelineFrameGPUHandle，9 个字段），无方法无行为；已标注 DORMANT(2026-08-19 审计) 且无生产入口；`release_callback: Optional[Callable]` 注解完整

---

## 附：编号使用清单

| 编号 | 文件 | 严重度 |
|------|------|--------|
| AUD-34800 | format_registry.py | Suggestion |
| AUD-34801 | frame.py | Suggestion |
| AUD-34802 | frame.py | Suggestion |
| AUD-34803 | frame.py | Suggestion |
| AUD-34804 | frame.py | Suggestion |
| AUD-34805 | frame_cache.py | Major |
| AUD-34806 | frame_cache.py | Suggestion |
| AUD-34807 | frame_cache.py | Suggestion |
| AUD-34808 | frame_path.py | Major |
| AUD-34809 | frame_path.py | Suggestion |
| AUD-34810 | frame_path.py | Suggestion |
| AUD-34811 | gop_index.py | Suggestion |
| AUD-34812 | gop_index.py | Suggestion |
| AUD-34813 | gpu_frame.py | Suggestion |
| AUD-34814 | gpu_frame.py | Suggestion |
| AUD-34815 | gpu_frame.py | Suggestion |
