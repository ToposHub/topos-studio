# 批次报告：batch-183 [P1] src/features/edit/core

编号区间：AUD-18300–AUD-18399
审查文件（3 个，2,959 行）：subtitle_renderer.py (582)、take_sheet.py (269)、timeline_clip_frame_provider.py (2,108)
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解

---

## src/features/edit/core/subtitle_renderer.py (行数: 582)

### 发现

- [AUD-18301] [Major] 全局字幕纹理缓存 `_GLOBAL_CACHE` 无锁，被 UI 绘制线程与导出线程并发读写
  - 位置: src/features/edit/core/subtitle_renderer.py:397-411（及调用方 src/features/edit/ui/program_viewer.py:2880、src/features/edit/core/timeline_export.py:5720-5741 → 6394）
  - 证据:
    ```python
    def get(self, key: SubtitleCacheKey) -> SubtitleTexture | None:
        if key in self._cache:
            self._order.remove(key)
            self._order.append(key)
            return self._cache[key]
        return None
    ```
    导出生产者线程（timeline_export.py:5736-5738 `producer = _threading.Thread(target=produce, ...)` 中 `self._render_frame(fi, config)` → `_burn_in_subtitles` → `render_subtitle_clip_to_rgba_cached`）与预览绘制线程（program_viewer.py:2880 paint 路径 `get_subtitle_texture_cached(clip, canvas_w, canvas_h, style=style)`）共用同一个模块级 `_GLOBAL_CACHE`（line 418），dict+list 均无任何锁。
  - 影响: 两线程同时 get 同一 key 时，后到者的 `self._order.remove(key)` 抛 ValueError（未捕获，击穿 paintEvent / 导出 produce 循环）；put 路径并发可产生重复/孤儿 order 条目，静默破坏 LRU 容量约束。导出烧录期间预览仍在绘制字幕时即可触发。
  - 建议: 为 `_SubtitleRenderCache` 增加 `threading.Lock`（get/put/clear 全覆盖），或改为每线程独立缓存实例。
  - 置信度: 中（并发前提是导出烧录与预览绘制同时活动；缓存对象本身无锁是确凿的）

- [AUD-18302] [Major] `_warp_rgba_to_canvas` 对旋转四边形外像素做边缘 clamp 采样，旋转字幕出现拖尾条纹伪影且与预览不一致
  - 位置: src/features/edit/core/subtitle_renderer.py:309-331
  - 证据:
    ```python
    # 双线性采样：把 (lx, ly) clamp 到 [0, tex_w-1]/[0, tex_h-1]，做 bilinear
    lx_c = np.clip(lx, 0.0, tex_w - 1.001)
    ly_c = np.clip(ly, 0.0, tex_h - 1.001)
    ...
    sampled = top_b * (1.0 - fy) + bot_b * fy
    # 写回 canvas 子区域（直接覆盖，因为源是 straight-alpha，混合由 painter 负责）
    canvas[y_lo:y_lo + sub_h, x_lo:x_lo + sub_w] = sampled
    ```
    扫描区域是旋转后 bbox 的 axis-aligned AABB（line 277-286），rotation≠0 时 AABB 内、变换四边形外的像素经反向映射落在纹理外，被 clamp 到最近边缘 texel 并直接覆盖写回。字形 bbox 边缘必然含不透明字形像素，故旋转后向 AABB 边界拖出不透明条纹。
  - 影响: 旋转字幕在全画布路径（导出烧录 `render_subtitle_clip_to_rgba_cached`）出现边缘像素条纹；而预览路径（program_viewer 用 QPainter 对紧凑纹理做变换，按四边形光栅化）无此伪影——烧录结果与预览不一致（违反模块头"像素级一致"承诺）。
  - 建议: 反向映射后先计算 inside mask（`0<=lx<=tex_w-1 and 0<=ly<=tex_h-1`），mask 外像素 alpha 置 0 再采样写回。
  - 置信度: 中（机制由数学确定；视觉程度取决于字形 bbox 边缘内容，rotation=0 时无影响）

- [AUD-18303] [Suggestion] `render_subtitle_clip_to_rgba` 中 4K 全画布分配为死存储，每次调用白付一次大额分配与清零
  - 位置: src/features/edit/core/subtitle_renderer.py:191（对照 209-218）
  - 证据:
    ```python
    # ── 2. 构造全画布透明 buffer，把 bbox 纹理贴到正确位置 ──
    canvas = np.zeros((canvas_h, canvas_w, 4), dtype=np.float32)
    ...
    out = _warp_rgba_to_canvas(
        bbox_rgba,
        ...
    )
    return out
    ```
    `_warp_rgba_to_canvas` 内部（line 248）自行 `np.zeros` 分配并返回自己的 canvas；line 191 的 `canvas` 从未被使用。
  - 影响: 4K 下每次调用浪费约 132 MB float32 的分配与清零（该函数为公开 API，src 内当前无调用方，但保留即隐患）。
  - 建议: 删除 line 191 的死分配。
  - 置信度: 高

- [AUD-18304] [Suggestion] 34 个样式字段清单在三个地方重复声明，且 `_clip_to_text_params` 用 `style[f]` 直接索引
  - 位置: src/features/edit/core/subtitle_renderer.py:82-92, 117-121（对照 src/features/edit/core/captions/model.py:140-157 的 STYLE_FIELDS）
  - 证据:
    ```python
    _STYLE_FIELD_SET: frozenset[str] = frozenset((
        "font_family", "font_size", "font_weight", "italic",
        ...
    ))
    for f in _PASSTHROUGH_FIELDS:
        if style is not None and f in _STYLE_FIELD_SET:
            params[f] = style[f]
    ```
    `_PASSTHROUGH_FIELDS`、`_STYLE_FIELD_SET` 与 captions.model.STYLE_FIELDS（有 `len==34` 断言）三处手工同步；且此处用 `style[f]` 直接索引，而 `_resolved_value`（line 223）用 `.get` 防御——防御风格不一致。当前 resolve_style 保证 34 键齐全，但任一处清单漂移即 KeyError。
  - 影响: 字段增删时清单漂移风险；部分 style 字典传入即 KeyError 崩溃。
  - 建议: `_STYLE_FIELD_SET = frozenset(STYLE_FIELDS)`（import captions.model），`params[f] = style.get(f, getattr(clip, f))`。
  - 置信度: 高（重复确凿；崩溃需漂移/部分字典才会发生）

### 通过项
- HDR/GLSL 正确性：输出 straight-alpha float32 全程无 0-1 clamp；composite_over 复用 TextNode._over，RGB 可 >1.0
- 信号防递归：纯核心模块无 Qt 控件/信号（不适用，已核查）
- 状态提交一致性：不涉及 undo/redo；缓存键含完整内容签名（clip_id+text+34 样式值+画布尺寸），样式/文本变化不会命中陈旧条目，`invalidate_cache` 仅为可选优化
- 输入验证：`_resolved_value` 对 style 缺键/None 回退 clip 扁平字段；`tex_w/tex_h = max(1, ...)` 防零尺寸；frames_to_srt_timecode 对 fps<=0 回退 24.0、负帧 clamp 0
- pivot 换算一致性：cached 路径经 `pivot_texture_local`（render_plan.py:41-64，已对照）与未缓存路径内联公式语义一致
- 类型注解：公开 API 均有完整类型注解

---

## src/features/edit/core/take_sheet.py (行数: 269)

### 发现

- [AUD-18310] [Suggestion] `build_take_sheet_from_project` 对持久化元数据做强类型转换但无逐条异常防御，与 `from_dict` 不一致
  - 位置: src/features/edit/core/take_sheet.py:238-257（对照 199-203）
  - 证据:
    ```python
    for asset_id, asset in assets.items():
        metadata = getattr(asset, "metadata", None) or {}
        if not metadata.get(KEY_IS_RECORDING):
            continue
        peak = metadata.get("peak_dbfs")
        entry = TakeSheetEntry(
            ...
            duration_seconds=float(metadata.get("duration", 0.0) or 0.0),
    ```
    而 `TakeSheet.from_dict`（line 199-203）对每条 `try/except (TypeError, ValueError)` 并 `logger.warning` 跳过。重建路径没有等价保护。
  - 影响: 工程包/元数据被手改或旧 schema 写入畸形值（如 duration=""、take_number="abc"）时，重开工程的重建流程抛 ValueError 中断整个 take sheet 恢复（`from_dict` 路径则只丢一条并告警）。
  - 建议: 重建循环内逐条 try/except，失败条目记 warning 跳过，与 from_dict 对齐；或抽取共享的 coerce 工具。
  - 置信度: 高（行为差异确凿；触发需外部畸形数据，故定 Suggestion）

### 通过项
- 线程安全：纯内存对象、模块声明不依赖 Qt，无跨线程共享状态
- GPU 资源生命周期：不涉及 GL 资源（不适用，已核查）
- 序列化往返：peak_dbfs 的 ±inf ↔ None 处理正确（to_dict:68-69 / from_dict:79,90）
- mark_best 同场次互斥逻辑正确（含 scene_name="" 全局查询语义）
- add_take 按 take_id 幂等更新已存在条目
- 最佳 take 持久化经 MarkBestTakeCommand（edit_controller.py:3043-3072，已核查）走 undo 栈，不产生双真值
- 类型注解：dataclass 与方法注解完整，__all__ 导出齐全

---

## src/features/edit/core/timeline_clip_frame_provider.py (行数: 2108)

### 发现

- [AUD-18320] [Major] `_pending_source_frames` 无界累积：scrub 期间后台解码帧仅在精确同帧再次请求时弹出，CPU 内存持续增长
  - 位置: src/features/edit/core/timeline_clip_frame_provider.py:913, 1154-1157（仅 2082 一处清理）
  - 证据:
    ```python
    # (asset, frame) -> 后台已解码的 DecodedFrame（等渲染线程上传）
    self._pending_source_frames: dict[tuple[str, int], Any] = {}
    ...
    pending = self._pending_source_frames.pop((asset_path, source_frame), None)
    ...
    with self._async_lock:
        self._pending_source_frames[(asset_path, frame)] = (
            decoded, matrix_tag, range_tag,
        )
    ```
    全文仅 4 处引用（202 定义 / 913 精确键 pop / 1155 写入 / 2082 release_video_sessions 清空）。最新目标 latest-wins 逐帧入 pending；scrub 前进后旧位置的键再也不会被 pop。
  - 影响: Composite 页长时间拖拽 scrub，每个浏览过的帧号滞留一张全尺寸解码帧（4K master rgb48le 约 49 MB/张、NV12 约 12 MB/张），仅换 provider key 或 release_video_sessions 才释放——无界 CPU 内存泄漏，长会话可耗尽内存。
  - 建议: pending 容量设上限（如按 asset 只保留最新 1-2 条，latest-wins 逐出旧帧并释放）；或定期清理未被请求的过期条目。
  - 置信度: 高

- [AUD-18321] [Major] 源纹理缓存"淘汰即释放"，返回中的 `_TexResult` 无引用保护——同图多素材求值时纹理名可在采样前被释放
  - 位置: src/features/edit/core/timeline_clip_frame_provider.py:524-529, 879-884, 1077-1079
  - 证据:
    ```python
    while len(self._source_cache_order) > self._SOURCE_CACHE_MAX:
        old_key = self._source_cache_order.pop(0)
        old_result = self._source_texture_cache.pop(old_key, None)
        if old_result and old_result.texture_id in self._owned_textures:
            self._release_owned_texture(old_result.texture_id)
    ```
    上传路径注释自认"the cache owns the texture lifetime: release happens only on eviction / replacement / explicit clear"；`_SOURCE_CACHE_MAX = 3`。`get_timeline_clip_texture` 把 texture_id 交给评估器后即失去引用；同一复合图内第 4 个不同素材 clip 上传会淘汰并释放第 1 个节点尚未采样（最终合成 draw 前）的纹理。
  - 影响: 含 ≥4 个不同视频素材输入的复合图（或多次求值交错）出现悬垂 texture_id 采样——黑帧/错色闪变，与注释 1584-1588 修复的同类闪变问题同根。模块 docstring 称第一阶段"只支持单个 clip 主输入"，故当前触发面受限。
  - 建议: 返回的 _TexResult 增加 lease/refcount（评估器持有时不淘汰），或缓存淘汰只移出账本、延迟到帧末 flush 再释放。
  - 置信度: 中（机制确凿；是否可现有路径构造多 clip 同图未验证，受"单 clip 主输入"阶段约束）

- [AUD-18322] [Major] YUV→RGB 转换输出纹理固定 8-bit（dtype='f1'），10/12-bit HDR 源在 IDT 前被量化
  - 位置: src/features/edit/core/timeline_clip_frame_provider.py:449, 1792（对照 1769-1774 已知 bit_depth）
  - 证据:
    ```python
    import moderngl
    output_tex = self._gl_context.texture((width, height), 4, dtype="f1")
    ...
    # _upload_planar_frame 内：
    output_tex = ctx.texture((width, height), 4, dtype='f1')
    ```
    `_upload_planar_frame` 已解析 bit_depth（line 1769-1774，P010 走 10-bit render）并写入 `_TexResult.bit_depth`，但渲染目标附件仍为 f1；`_import_deferred_gpu_frame` 同样对 bit_depth=10 的 VT 帧用 f1。下游 FX-M01（line 742-749）专门为保 HDR 用 f2 scratch，而源入口先把 PQ/HLG 10-bit 编码值量化到 8-bit。
  - 影响: HDR 源（P010/PQ/HLG、10-bit 母版）在 IDT 展开前损失 2-bit 精度，暗部渐变出现 banding，与"HDR 内部处理（float16/32）"基准不符（注：为量化损失而非 >1.0 clamp，PQ/HLG 编码值本身在 [0,1] 内）。
  - 建议: `dtype='f2' if bit_depth > 8 else 'f1'`（两处），与 FX-M01 的 scratch 精度策略对齐。
  - 置信度: 中

- [AUD-18323] [Suggestion] `_gl_thread_id` 每次 `get_timeline_clip_texture` 无条件覆写，provider 被两个 GL 线程先后复用时跨线程 clear 协议失真
  - 位置: src/features/edit/core/timeline_clip_frame_provider.py:625, 2050-2058
  - 证据:
    ```python
    # This method is the provider's GL/render-thread entry: pin the
    # thread identity for the cross-thread clear contract...
    self._gl_thread_id = threading.get_ident()
    self._drain_pending_source_cache_clear()
    ```
    若同一 provider 实例先后在两个 GL 线程上求值（facade.py:188 工厂与 CompositeFrameRenderer 复用场景），线程 ID 翻转后：旧 GL 线程的 clear_source_cache 被停靠给新线程代为释放（跨上下文释放纹理），且两线程可并发进入无锁的 cache/order 变更路径。
  - 影响: 多 GL 线程复用同一实例时释放到错误上下文（GL 错误/崩溃）或缓存记账竞态。当前 provider 按 renderer 实例隔离、线程固定，未见现实触发路径。
  - 建议: 首次进入时固化 `_gl_thread_id`（或换线程时强制先全量释放再重建），并在文档标注单线程约束。
  - 置信度: 低（多线程复用前提未在现有代码中确认）

- [AUD-18324] [Suggestion] deferred GPU 帧导入失败路径：调用方立即 `safe_release_gpu_frame` 抢先于 fence 延迟释放，违背"安全时机释放"契约
  - 位置: src/features/edit/core/timeline_clip_frame_provider.py:454-457, 494-499, 974-977
  - 证据:
    ```python
    draw_submitted = True
    if not external_pass.render(planes, gpu_frame, output_fbo, width, height):
        self._external_gpu_fallback_reason = "external_yuv_render_failed"
        return None
    ```
    finally 中 `draw_submitted=True` 时 `_enqueue_external_source_release(gpu_frame, bridge_release)` 走 glFenceSync 延迟释放；但调用方（line 977）随即 `safe_release_gpu_frame(gpu_frame)` → `frame.release.release()`（gpu_frame.py:532-533）立即执行。GpuFrameRelease exactly-once（gpu_frame.py:246-262，已核实无双重释放）使立即方胜出，fence 保护被架空。
  - 影响: 若 render 返回 False 前已提交部分采样 IOSurface 的 draw，CVPixelBuffer 在 GPU 仍可能读取时被回收——IOSurface 复用伪影。窗口窄（render 失败路径）。
  - 建议: 失败 return 前标记"已入队延迟释放"，调用方据此跳过 safe_release；或 render 失败路径在 enqueue 后直接返回哨兵区分。
  - 置信度: 中（契约违背确凿；实际劣化需 render 部分提交后失败）

- [AUD-18325] [Suggestion] `clear_debug_cache` 名不副实：实际释放全部 owned 纹理/源缓存/解码会话，且无 clear_source_cache 的跨线程停靠保护
  - 位置: src/features/edit/core/timeline_clip_frame_provider.py:2021-2034
  - 证据:
    ```python
    def clear_debug_cache(self) -> None:
        """清除缓存的调试占位符 texture。"""
        self._release_external_gpu_resources(wait=True)
        for tex in self._owned_textures.values():
            try:
                tex.release()
            except Exception:
                pass
    ```
    与 `clear_source_cache`（2036-2058）的"非 GL 线程停靠、渲染线程 drain"契约不同，此方法无任何线程防护；src 内当前无调用方（已 grep 核实），一旦被 UI 线程误用即离线程释放 ModernGL 纹理。
  - 影响: 潜在陷阱 API：语义（名）与行为（释放一切）不符，且绕过线程契约。
  - 建议: 重命名为 `release_all_resources` 并复用停靠协议，或删除（无调用方）。
  - 置信度: 高（语义与防护缺失确凿；现网无影响）

- [AUD-18326] [Suggestion] `_detect_source_color_space` 的 info=None 早退不写缓存，无 clip info 的素材在渲染线程逐帧重复探测
  - 位置: src/features/edit/core/timeline_clip_frame_provider.py:1256-1261
  - 证据:
    ```python
        try:
            info = service.get_clip_info(clip_id) if hasattr(service, "get_clip_info") else None
        except Exception:
            info = None
        if info is None:
            return "bt709", "bt709", "bt709", "limited"
    ```
    默认值直接 return，未经 `tags_cache[clip_id] = tags` 记忆化；顺序播放每帧源纹理缓存 miss 都重查一次 get_clip_info（成功路径才缓存，line 1338-1339）。
  - 影响: get_clip_info 不可用的素材在 4K 播放热路径上每帧多一次元数据调用（渲染线程）。
  - 建议: 早退默认值同样写入 tags_cache。
  - 置信度: 高

- [AUD-18327] [Suggestion] 上传路径宽泛 `except Exception` 且仅 logger.debug，真实 GL/解码异常根因不可诊断
  - 位置: src/features/edit/core/timeline_clip_frame_provider.py:1605-1607, 1895-1897
  - 证据:
    ```python
        except Exception as e:
            logger.debug(f"Frame upload failed: {e}")
            return None
    ...
        except Exception as e:
            logger.debug(f"Planar frame GPU upload failed: {e}")
            return None
    ```
  - 影响: 上游虽因返回 None 打 warning（1061-1067），但真正的异常类型/栈被 debug 级吞掉，违背"不得宽泛 except 掩盖领域错误"的项目铁律；线上排障只能看到"upload failed"表象。
  - 建议: 至少 logger.warning 带异常类型，或按预期异常类型收窄捕获。
  - 置信度: 高

- [AUD-18328] [Suggestion] 异步解码停止协议竞态：join=False 即弃引用，随后 `_ensure_async_decode_thread` 复位 stop 标志可"复活"未退出的旧 worker 并在清空后回写 pending
  - 位置: src/features/edit/core/timeline_clip_frame_provider.py:1115-1127, 1194-1206, 2079-2082
  - 证据:
    ```python
    # _ensure_async_decode_thread
    self._async_decode_stop = False
    ...
    # _stop_async_decode_thread(join=False，release_video_sessions 调用)
    if (join and thread is not None and ...):
        thread.join(timeout=timeout)
    self._async_decode_thread = None
    ...
    # release_video_sessions 清空后，旧 worker 仍可能执行：
    with self._async_lock:
        self._pending_source_frames[(asset_path, frame)] = (...)
    ```
    旧 worker 若正阻塞在 request_frame 内（join=False 不等待），ensure 因引用已置 None 创建新线程并把 `_async_decode_stop` 复位为 False；旧 worker 解码完成后 `while not self._async_decode_stop` 判定为"未停止"继续循环（僵尸双 worker），并把已释放会话时期的帧写回刚清空的 pending。
  - 影响: 反复换上下文可积累多个常驻 worker 线程；release 后 pending 重新出现陈旧条目（叠加 AUD-18320 的无界增长）。
  - 建议: worker 用世代号（generation counter）而非全局布尔 stop；弃引用前 join 或让旧 worker 检测自身线程对象已被替换。
  - 置信度: 中

### 通过项
- GPU 资源 deferred-release（IOSurface lease）：glFenceSync 入队 + `_drain_external_source_releases` 轮询释放，无 sync 支持时 glFinish 兜底，契约完整（294-396）
- 双重释放防护：GpuFrameRelease exactly-once（gpu_frame.py:246-262 已核实），导入失败路径 provider 延迟释放与调用方 safe_release 不会双重释放
- 跨线程源缓存 clear 停靠协议（clear_source_cache park + 渲染线程入口 drain）设计正确（2036-2073）
- 色彩管线：源 transfer/primaries/matrix/range 探测完整（PQ/HLG/linear/traw-log0 字符串与数字码双路径），PQ/HLG 强制 BT.2020，默认 bt709+limited 符合消费视频规范
- CPU→GL 原点翻转严格单边界：packed（1647/1663/1677）与 planar（1926-1931）各翻转一次，约定注释明确
- 输入验证：bit_depth 强制 coerce（0<x<=64）、帧尺寸/通道（3/4）校验、clip 元数据全部 getattr 防御、__new__ 构造的测试实例补建缓存字典
- 回退链不再落调试占位符（缺失媒体返回 None 并 warning，671-678），_create_debug_placeholder 为显式 API
- HDR packed 路径：uint16 → float32 归一化（f4 上传）与 rgb48 全范围 65535 处理正确
- keyframe 帧空间统一为 timeline_frame（FX-C05），与 Edit 预览/导出一致
- 取消路径：export 取消在上层队列处理；异步解码 stop 标志 + wakeup 事件（见 AUD-18328 的竞态残留）
- 信号防递归：无 Qt 控件/信号（不适用，已核查）
- 类型注解：公开方法与 _TexResult 字段注解完整（参数多用 Any 属跨模块 duck-type 边界，可接受）

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 5 | AUD-18301, AUD-18302, AUD-18320, AUD-18321, AUD-18322 |
| Suggestion | 9 | AUD-18303, AUD-18304, AUD-18310, AUD-18323, AUD-18324, AUD-18325, AUD-18326, AUD-18327, AUD-18328 |
