# batch-076 审查报告 — src/features/composite/nodes

审查日期：2026-09-28。编号区间：AUD-7600–AUD-7699。
两文件全文通读（video_frame_node.py 1–2805 行，vignette_node.py 1–110 行）。
关键跨文件事实已核实：`_gc_resources` 全文件仅出现于声明与 finalizer 注册（无 append）；`_texture_cache` 无任何写入点；`_get_cache_key` 无调用者；`_effect_*` 输出契约被 base_evaluator/cache_manager 及 60+ 节点采用（全库既有模式）。

## src/features/composite/nodes/video_frame_node.py (行数: 2805)

### 发现

- [AUD-7601] [Critical] `_frame_cache` 缓存条目与共享转换 scratch 缓冲对象别名，缓存命中会上屏错误帧/撕裂帧
  - 位置: src/features/composite/nodes/video_frame_node.py:1037-1046, 1095, 1484, 1837-1838, 1862-1874
  - 证据:
    ```python
    buf_attr = '_bg_rgba_buffer' if background else '_rgba_buffer'
    buf = getattr(self, buf_attr, None)
    if buf is None or buf.shape[0] != h or buf.shape[1] != w:
        buf = np.zeros((h, w, 4), dtype=np.float32)
        setattr(self, buf_attr, buf)
    else:
        buf[:] = 0  # Clear alpha
    result = buf
    ...
    return np.ascontiguousarray(result)   # 缓冲本身 C 连续 → 返回同一对象
    ```
    随后 `self._frame_cache[frame_num] = frame_data`（1484）把该共享缓冲对象按帧号入缓存；`upload_frame` 经 `frame_data = self._frame_cache.get(frame_number)`（1838）与 present-latest 扫描（1862-1874）取出上传。
  - 影响: 同尺寸下所有 packed CPU 帧共用同一个 `_rgba_buffer`/`_bg_rgba_buffer` 对象，`_frame_cache` 中多个帧号指向同一缓冲，内容恒为“最后一次解码的帧”。后果：(a) 邻域缓存命中/present-latest 上屏的是最近解码的邻居帧而非请求帧；(b) 短循环（循环长度 ≤ cache 窗口）导出第二遍精确命中缓存时输出的是循环末帧像素——最终成片渲染错误；(c) 渲染线程在 `np.flipud(frame_data)` 拷贝期间，worker 可在无锁状态下复写同一 `_bg_rgba_buffer`（转换不持 `_cache_lock`），产生上下撕裂的混合帧。注释只隔离了线程间（双缓冲）串帧，未覆盖帧间别名。planar 帧路径（`_PlanarFrameData` 每次独立数组）不受影响，故平时 ProRes planar 预览不暴露。
  - 建议: 入缓存前深拷贝（`result.copy()`）或改为按帧分配缓冲池；至少在 `upload_frame` 取出后立刻在 `_cache_lock` 内快照拷贝，并在 `_convert_decoded_frame` 写缓冲期间持锁。
  - 置信度: 高

- [AUD-7602] [Major] `_clear_cache` 对 `_vt_programs` 的 `(prog, vao)` 元组调用 `.release()`，AttributeError 被空 except 吞掉，VT program/VAO 从不释放
  - 位置: src/features/composite/nodes/video_frame_node.py:1649-1654, 2322-2328
  - 证据:
    ```python
    for obj in self._vt_programs.values():
        try:
            obj.release()
        except Exception:
            pass
    self._vt_programs.clear()
    ```
    而写入侧 `self._vt_programs['_ibo'] = ibo`（Buffer）、`self._vt_programs[key] = (prog, vao)`（元组，无 `release` 属性）。
  - 影响: 每次 `_clear_cache`（每次换源/重开视频，见 `_open_video_via_service` → `_clear_cache`）泄漏 VT 转换 shader program 与 VAO 各一份（'2d'/'rect' 两个键），随会话内换源次数无界累积；同时是“宽泛 except 掩盖领域错误”铁律的直接违例——清理逻辑静默失效且无任何日志。
  - 建议: 按 isinstance 分派释放（元组则分别 `prog.release()`、`vao.release()`），或统一存 dataclass；except 至少记 debug 日志。
  - 置信度: 高

- [AUD-7603] [Major] GL 资源释放遗漏：mid FBO 附件纹理从不释放、尺寸变化路径直接丢弃 FBO 不 release、flip program/VBO/IBO 不释放
  - 位置: src/features/composite/nodes/video_frame_node.py:2052-2057, 2069-2072, 1669-1680, 1681-1688, 2113
  - 证据:
    ```python
    # _ensure_gpu_target 尺寸变化：旧 FBO 对象不 release 直接丢弃
    self._gpu_texture.release()
    self._gpu_texture = None
    self._gpu_fbo = None
    self._gpu_mid_fbo = None
    ...
    if self._gpu_mid_fbo is None:
        mid_tex = ctx.texture((w, h), 4, dtype='f4')
        mid_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self._gpu_mid_fbo = ctx.framebuffer(mid_tex)   # mid_tex 无独立引用
    ```
    `_clear_cache` 中仅 `self._gpu_fbo.release()` / `self._gpu_mid_fbo.release()`（ModernGL Framebuffer.release 不释放附件纹理）；`self._flip_vao.release()` 后 `self._flip_vbo = None; self._flip_prog = None` 均不 release，flip IBO（2113 行局部变量，仅被 VAO 引用）全文件无释放点。
  - 影响: 每次 planar 路径换源/尺寸变化/清缓存泄漏一张 f4 中转纹理（4K 约 132MB VRAM）与 FBO 对象；另泄漏 flip program/VBO/IBO。长时间会话反复换源可耗尽 VRAM 导致渲染失败/崩溃。`mid_tex` 仅靠 FBO 附件引用存续，`_ensure_gpu_target` 换尺寸路径连 FBO 带纹理整体丢弃。
  - 建议: 为 mid 纹理保留显式引用（`self._gpu_mid_tex`）并在 FBO 释放后释放；尺寸变化路径先 `release()` 旧 FBO；`_clear_cache` 补 `self._flip_prog.release()`、`self._flip_vbo.release()` 并保存/释放 flip IBO。
  - 置信度: 高（ModernGL Framebuffer.release 不释放附件为其文档语义）

- [AUD-7604] [Major] `_upload_frame_gpu_vt` 绑定成功后至 pin 前的异常路径释放 gpu_frame 但不释放 bridge 租约，IOSurfaceGLBridge ring 槽位泄漏
  - 位置: src/features/composite/nodes/video_frame_node.py:2393, 2429-2448, 2458-2464
  - 证据:
    ```python
    planes, bridge_release = result
    ...
    except Exception as e:
        logger.warning(f"VT zero-copy upload error: {e}")
        self._vt_fail_count += 1
        if self._vt_fail_count >= 3:
            self._vt_gpu_disabled = True
        safe_release_gpu_frame(gpu_frame)
        return False
    ```
    `bind_pixel_buffer` 成功后的显式失败分支（2403/2410/2423）都成对释放 `bridge_release`，但 `glUseProgram`/`prog['u_color_range']`/渲染段（2429-2448）抛异常落入总 except 时只释放 `gpu_frame`。
  - 影响: bridge ring_size=4，每次泄漏占用一个槽位；连续 3 次即触发 `_vt_fail_count >= 3` 永久禁用 VT 路径（功能降级），且外部纹理句柄泄漏。uniform 名缺失（如 shader 变体不一致）即 KeyError 进入此路径。
  - 建议: 在 except 分支补 `bridge_release.release()`（以局部标志区分是否已取得租约），或用 try/finally 结构化成对释放。
  - 置信度: 高

- [AUD-7605] [Major] GC 后备释放机制是死代码：`_gc_resources` 永远为空列表，finalizer 恒空转
  - 位置: src/features/composite/nodes/video_frame_node.py:349-355, 2784-2805
  - 证据:
    ```python
    # weakref.finalize 后备释放机制
    # GC 时确保 GPU 纹理和 MediaSource 释放
    self._gc_resources: List[Any] = []
    self._finalizer = weakref.finalize(
        self,
        self._cleanup_on_gc,
        self._gc_resources
    )
    ```
    全文件（含 grep 核实）无任何 `_gc_resources.append(...)`；`_cleanup_on_gc` 遍历空列表。
  - 影响: 注释宣称的“GC 时确保 GPU 纹理和 MediaSource 释放”完全不成立。节点若未显式 `cleanup()`（图未正确反注册、异常路径丢弃节点）即被 GC，其 `_gpu_texture`、FBO、program、`_source`（FFmpeg 句柄）全部泄漏——文档化的安全网给出虚假保障。
  - 建议: 移除该机制（GL 对象本就不应从 GC 线程释放），改由节点图在渲染线程统一 deferred-release 兜底；或至少删掉误导性注释，避免后续维护者依赖此安全网。
  - 置信度: 高

- [AUD-7606] [Suggestion] `_vt_cpu_ok`/`_vt_cpu_miss` 跨线程读写无锁，与 `_frame_cache` 的锁纪律不一致
  - 位置: src/features/composite/nodes/video_frame_node.py:943-950, 1839, 1870-1871, 1637-1638
  - 证据:
    ```python
    # worker 线程（_decode_frame），无锁：
    miss = self._vt_cpu_miss.get(frame_number, 0) + 1
    ...
    self._vt_cpu_miss[frame_number] = miss
    if (not attached) or miss >= 2:
        ...
        self._vt_cpu_ok.add(frame_number)
    ```
    渲染线程 `upload_frame` 无锁读 `frame_number not in self._vt_cpu_ok`（1839、1870）；`_clear_cache` 却在 `_cache_lock` 内 clear 这两个容器（1637-1638）。
  - 影响: GIL 下单操作原子，但“清空后 worker 补写陈旧放行标记”等逻辑竞态可让门控短暂误放行 CPU 帧，出现一次 VT/CPU 颜色混显（正是该门控要防止的现象）。低概率、影响一帧。
  - 建议: 这两个容器的读写统一纳入 `_cache_lock`（或改用与 `_media_generation` 同代的版本化集合）。
  - 置信度: 高（竞态窗口存在）/ 影响评估为中

- [AUD-7607] [Suggestion] 死代码：`_texture_cache` 从未写入，`_get_cache_key` 无调用者
  - 位置: src/features/composite/nodes/video_frame_node.py:307, 1622-1625, 1662-1666, 1704-1719
  - 证据:
    ```python
    # 声明与淘汰/清理逻辑齐备，但全文件无 _texture_cache[...] = 赋值：
    self._texture_cache: Dict[int, moderngl.Texture] = {}
    ...
    if frame_num in self._texture_cache:
        tex = self._texture_cache.pop(frame_num)
    ```
    `_get_cache_key` 同理仅定义（1704），grep 全 src 无调用。
  - 影响: 维护者会误以为存在 GPU 纹理帧缓存及其淘汰保证；`_ensure_gpu_target`/`upload_frame` 尺寸变化时对空缓存的遍历释放是无效代码。
  - 建议: 删除 `_texture_cache`、`_get_cache_key` 及相关清理分支，或补上真正入缓存的路径。
  - 置信度: 高

- [AUD-7608] [Suggestion] `from_dict` 将 `_meta` 外部数据未经校验直写只读参数，None/异常类型可流入解码请求
  - 位置: src/features/composite/nodes/video_frame_node.py:2765-2778
  - 证据:
    ```python
    node.parameters["fps"] = meta.get("fps", 0.0)
    node.parameters["total_frames"] = meta.get("total_frames", 0)
    ...
    node.parameters["bit_depth"] = meta.get("bit_depth", 8)
    ```
    调用方 `self.parameters.get("fps", 24.0)`（713、876）对“键存在但值为 None/字符串”不生效，原值直接传给 `VideoContext.request_frame_for_node`。
  - 影响: 损坏或手改工程文件（外部数据，铁律要求验证）中的非数值 `_meta` 会在首次求值时以 `None` fps 触发服务层算术异常，节点静默无输出（被上层 except 吞掉后回退 fallback）。
  - 建议: 恢复时按 ParamDef 类型/范围钳制（复用 `_get_param_clamped`/`_get_param_int` 语义），非法值回退默认。
  - 置信度: 中（需外部文件损坏触发；机制本身确定）

- [AUD-7609] [Suggestion] 类型注解与实际存储不符：`_preload_target` 三元组、`_frame_cache` 混存 `_PlanarFrameData`
  - 位置: src/features/composite/nodes/video_frame_node.py:302, 317, 1268, 1484
  - 证据:
    ```python
    self._frame_cache: Dict[int, np.ndarray] = {}
    ...
    self._preload_target: Optional[Tuple[int, str]] = None
    ...
    self._preload_target = (int(center_frame), render_mode, scrub)   # 实为 3 元组
    ...
    self._frame_cache[frame_num] = frame_data   # frame_data 可为 _PlanarFrameData
    ```
  - 影响: worker 侧只能靠 `target[2] if len(target) > 2`（1300）防御性取值；类型检查器与后续维护者会得到错误契约。
  - 建议: 注解改为 `Optional[Tuple[int, str, bool]]` 与 `Dict[int, Union[np.ndarray, _PlanarFrameData]]`。
  - 置信度: 高

- [AUD-7610] [Suggestion] 服务开片路径直接改写 `self.parameters[...]` 绕过 `set_parameter` 版本记账；`_video_metadata` 在 total_frames==0 的新片上残留旧片元数据
  - 位置: src/features/composite/nodes/video_frame_node.py:452-459, 461-462
  - 证据:
    ```python
    self._clip_id = clip_id
    self.parameters["clip_id"] = clip_id
    self.parameters["fps"] = clip_info.fps
    ...
    if clip_info.total_frames > 0:
        ... self._video_metadata = VideoMetadata(...)
    ```
    基类 `set_parameter`（base_node.py:643）会 bump `param_revision`/dirty/输出修订；直写不触发。`_video_metadata` 仅在 total_frames>0 时重建，开片前未清空。
  - 影响: (a) 元数据参数变更不进修订链，依赖参数快照的下游缓存/撤销对比看不到变化；(b) 换到 total_frames==0 的素材时沿用上一支片的 fps/transfer/primaries → 帧号换算与 `_detect_color_space` 用错色彩元数据。
  - 建议: 开片起始处置 `self._video_metadata = None`；隐藏参数更新走 `set_parameter` 或在注释中明确豁免理由。
  - 置信度: 中

- [AUD-7611] [Suggestion] `_flip_blit` 吞掉渲染异常后 `_upload_frame_gpu` 仍返回 True，翻转失败时以陈旧内容冒充新帧
  - 位置: src/features/composite/nodes/video_frame_node.py:2041-2042, 2097-2125
  - 证据:
    ```python
    self._flip_blit(ctx)
    return True
    ...
    except Exception as e:
        logger.warning(f"flip blit failed: {e}")
    ```
  - 影响: flip program 编译失败/绑定异常时 `_gpu_fbo` 保留上一帧内容，但 `_revision += 1`、`_current_frame` 前进——下游按修订号视为新帧，播放中表现为“画面偶发停在同帧且缓存判定已更新”。
  - 建议: `_flip_blit` 返回 bool，失败时 `_upload_frame_gpu` 返回 False 走持帧回退。
  - 置信度: 中

### 通过项
- GLSL/HDR 正确性：内联 shader 数字字面量均带 `.0`（2075-2095、2213-2299）；VT/planar 转换刻意不 clamp 高光保留 HDR；BT.601/709/2020 系数与 limited/full 展开语义正确且与 CPU 路径一致；YUV 矩阵/范围标签映射（731-750、799-816）与 `_convert_decoded_frame` 的判定一致。
- 信号防递归：文件不涉及 Qt 信号/控件，无递归风险（不适用）。
- 线程安全（主体结构）：`_frame_cache`/`_media_generation`/`_async_fail_counts` 读写全程持 `_cache_lock`；`_vt_pending` 判定与清槽在同一 `_vt_lock` 临界区（防 use-after-release）；GL 纹理/FBO/program 仅渲染线程触碰，后台线程强制 `release_textures=False`；直连回退模式禁用后台预取避免 libav 单实例重入崩溃；渲染/后台各持独立转换缓冲隔离线程间串帧。
- 异常与取消路径（主体）：service open 失败分类（unsupported 会话级跳过 / transient 3 次熔断）防止重试风暴；VT bind/init 失败 3 次熔断回退 CPU；交互式解码被取代（superseded）不计失败避免误触同步兜底；`_decode_and_store` 的 stale VT 条目覆盖/代际过期均显式释放 CVPixelBuffer。
- 输入验证（参数侧）：PARAM_DEFS 全部带 min/max；`_get_param_int`/`_get_param_clamped` 在回退路径统一钳制；`upload_frame` 对尺寸变化重建纹理；`_apply_frame_override` 对 remap 异常回退原帧号。
- 性能模式：播放/拖拽判定驱动的邻域抑制、present-latest、VT 交互式 coarse、单槽 latest-wins 预取目标、纹理 buffer-protocol 直写省整帧 tobytes、planar GPU 直上省 CPU 转换——均有明确注释依据与实测说明。
- 持久化：`to_dict`/`from_dict` 往返保存 `_meta` 只读元数据与 `is_fallback`，重开时由 probe 刷新。
- 上下文管理器与 `cleanup()`：先 join 后台线程再关源清缓存，顺序正确（除发现 AUD-7603/7605 所列遗漏项）。

## src/features/composite/nodes/vignette_node.py (行数: 110)

### 发现

- [AUD-7612] [Suggestion] `_do_evaluate` 返回值与注解 `Dict[str, TextureResult]` 不符：混入 `_effect_*` 非纹理键且 `texture_id=-1` 占位
  - 位置: src/features/composite/nodes/vignette_node.py:81-110
  - 证据:
    ```python
    ) -> Dict[str, TextureResult]:
        ...
        return {
            "result": TextureResult(texture_id=-1, ...),
            "_effect_type": "vignette",
            "_effect_input": image,
            "_effect_params": {...},
        }
    ```
  - 影响: 与全库 60+ 节点共享的 `_effect_*` 声明式契约一致（base_evaluator/cache_manager 消费），机制本身成立；但注解欺骗类型检查器，`texture_id=-1` 的占位契约只存在于约定中，新调用方易误当真实纹理使用。
  - 建议: 引入 `EffectResult` 类型别名或在基类定义该返回协议，统一注解。
  - 置信度: 高（不一致确定；为全库既有模式，非本文件独有缺陷）

- [AUD-7613] [Suggestion] `cache_to_disk` 参数定义并初始化但节点内从未使用（死参数）
  - 位置: src/features/composite/nodes/vignette_node.py:48, 66
  - 证据:
    ```python
    ParamDef("cache_to_disk", "bool", False, group="Cache"),
    ...
    "cache_to_disk": False,
    ```
    全文件无任何读取（grep 核实 composite 下也无该节点相关消费）。
  - 影响: UI 会展示无效的 Cache 控件，用户操作无任何效果；序列化携带冗余字段。
  - 建议: 删除该参数，或接入节点级磁盘缓存机制。
  - 置信度: 高

### 通过项
- 线程安全：无可变共享状态、无后台线程，纯函数式求值。
- GPU 资源生命周期：不创建/持有任何 GL 对象（texture_id=-1 占位，效果合并由渲染器执行）。
- GLSL/HDR 正确性：乘性 exposure 语义，factor∈[0,1] 只做增益衰减，不对 HDR 数据做无理由 clamp；Python 镜像 `vignette_factor` 的参数钳制（radius∈[0.05,1]、softness≤radius）与 `_effect_params` 传出的钳制值一致。
- 信号防递归：不适用（无 Qt 信号）。
- 异常与取消路径：无 I/O 与外部调用，输入缺失（image None/invalid）正确返回空。
- 输入验证：amount/radius/softness/roundness 四参数在传出前全部 min/max 钳制，容忍 parameters 缺省。
- 性能模式：无逐像素 CPU 计算；`vignette_factor` 仅为 shader 镜像文档函数。
- 状态提交一致性：无可变状态参数（除死参数 cache_to_disk），bbox 透传输入，无 undo/revision 风险点。
- 类型注解：公开方法均有完整注解（`if __name__ != "__main__"` 的 BBox 导入守卫配合字符串注解，无 NameError 风险）。

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-7601 |
| Major | 4 | AUD-7602, AUD-7603, AUD-7604, AUD-7605 |
| Suggestion | 8 | AUD-7606 – AUD-7613 |
