# Batch-006 静态审查报告 — src/features/color/core/image_engine.py

审查日期：2026-09-28 · 审查方式：全文只读（4,730 行）· 问题区间：AUD-0600–AUD-0619

---

## src/features/color/core/image_engine.py (行数: 4,730)

### 发现

- [AUD-0600] [Critical] undo/redo 恢复图像后既不重新上传 GPU 纹理也不重置蒙版脏标记，视口继续渲染撤销前的旧图
  - 位置: src/features/color/core/image_engine.py:4194-4225
  - 证据:
    ```python
    image = state.get('image')
    if image is not None:
        self.img_full_original = image.copy()
        low = FileIO._resize_to_fit(image, FileIO.LOW_RES_SIZE)
        high = FileIO._resize_to_fit(image, FileIO.HIGH_RES_SIZE)
        self.img_low = low
        self.img_high = high
    ...
    # 重新渲染
    self.render_pipeline()
    ```
    `render_pipeline` 的 RENDER_FRAME 参数（2915-2947）不含任何图像数据，渲染线程只消费此前 UPLOAD_IMAGE 上传的纹理；`_restore_state` 全程无 `renderer.submit(RenderCommand.UPLOAD_IMAGE, ...)`，也未对任何层置 `layer.mask_dirty = True`。
  - 影响: 对 crop/resize/rotate/flip/apply 等破坏性操作执行 undo/redo 后，GPU 主纹理与蒙版纹理仍是旧内容/旧尺寸（`_update_mask_textures` 因 mask_dirty 未置位直接跳过），视口显示与 `img_full_original` 不一致——渲染错误；同时 `img_low/img_high` 直接赋 `FileIO._resize_to_fit` 的 uint8 结果，未走 `normalize_to_float32`，破坏了文件内其他路径「img_high 为 float32 0..1」的不变量（对照 set_image:780-781）。`set_reference_image` 也未同步，split 参考纹理同样过期。
  - 建议: `_restore_state` 恢复图像后走与 `set_image` 一致的归一化 + UPLOAD_IMAGE 提交（或直接调用 `set_image(image, high, low, is_new_file=False)` 前先手动写回 `img_full_original`），并对所有层置 `mask_dirty = True`、刷新 reference。
  - 置信度: 高

- [AUD-0601] [Major] resize_image 从不更新 img_full_original，全分辨率导出与坐标基准滞留旧尺寸
  - 位置: src/features/color/core/image_engine.py:4230-4237（对照 set_image:691-699、rotate_image:4247）
  - 证据:
    ```python
    def resize_image(self, width, height):
        if self.img_full_original is None: return
        self.history.push_state(self.img_full_original, self.layers, self.active_layer_index)
        res = cv2.resize(self.img_full_original, (width, height), interpolation=cv2.INTER_LANCZOS4)
        ...
        self.set_image(res, high, low, is_new_file=False)
    ```
    而 set_image 中仅 `if is_video:` / `if is_new_file:` 两个分支会写 `self.img_full_original = full.copy()`；rotate_image 与 `_apply_flip_state` 都在调用 set_image 前显式 `self.img_full_original = res.copy()`（4247、4274），resize_image 漏掉了这一步。
  - 影响: 缩放后 `img_full_original` 仍是旧分辨率：`get_display_image`/全分辨率渲染（2952-2966 直接以 img_full_original 为源）输出旧尺寸图像；`render_pipeline` 中 `img_full_original` 与 `img_high` 形状不一致触发 eff_visible_rect 缩放换算（2791-2799），UV/效果缩放全部错位。
  - 建议: 在 set_image 调用前补 `self.img_full_original = res.copy()`，与 rotate/flip 路径对齐（或让 set_image 在 is_new_file=False 时也写回 full）。
  - 置信度: 高

- [AUD-0602] [Major] apply_crop 不更新 img_source_full，随后的 flip/reset_flip 用未裁剪源图重建，裁剪结果被静默丢弃
  - 位置: src/features/color/core/image_engine.py:4293-4357（对照 _apply_flip_state:4261-4275）
  - 证据:
    ```python
    # apply_crop 末尾：
    self.img_full_original = final
    self.set_image(final, high, low, is_new_file=False)
    # _apply_flip_state：
    def _apply_flip_state(self):
        if self.img_source_full is None: return
        res = self.img_source_full.copy()
        if self._flip_h and self._flip_v: res = cv2.flip(res, -1)
        ...
    ```
    apply_crop 只写 `img_full_original`，`img_source_full` 保持未裁剪的原图；flip 操作完全从 `img_source_full` 重建。
  - 影响: 裁剪后再水平/垂直翻转（或 set_flip_state 恢复）会从原图重建，裁剪结果凭空消失——用户工作丢失；本文件内无任何路径重新应用 `self.crop_params`。
  - 建议: apply_crop 后同步 `self.img_source_full = final.copy()`（与 rotate 语义一致），或让 flip 基于当前 `img_full_original` 而非 source。
  - 置信度: 中（若外部模块有基于 crop_params 的重应用逻辑则不成立，但本文件内未见）

- [AUD-0603] [Major] render_cached_image 临时替换 img_full_original 无 finally 恢复；嵌套事件循环期间被并发读取
  - 位置: src/features/color/core/image_engine.py:4051-4061, 4091-4093
  - 证据:
    ```python
    orig_full = self.img_full_original
    self.img_full_original = (self.img_high * 255).astype(np.uint8) if self.img_high.dtype == np.float32 else self.img_high
    result = self.render_full_resolution()
    self.img_full_original = orig_full
    ...
    except Exception as e:
        logger.error(f"Export Render Error: {e}")
        return None
    ```
    恢复语句不在 finally 中；`render_full_resolution` 内部 `loop.exec()`（995）会处理任意排队事件（定时器、信号槽），且 `request_full_resolution` 在提交异常时会重新 raise（912-915）。
  - 影响: 一旦渲染抛异常，`img_full_original` 永久滞留为低分辨率 8 位代理，后续所有全分辨率导出/显示全部降质；即使正常路径，嵌套事件循环期间任何读取引擎状态的处理者（scope、另一次渲染）都会看到被换掉的代理图。
  - 建议: 用 try/finally 包裹恢复；渲染期间设置重入保护（复用 `_full_res_render_active` 或独立标志）拒绝其他读取者。
  - 置信度: 高

- [AUD-0604] [Major] render_cached_image 将 float 图像 `(img_high * 255).astype(np.uint8)` 无 clip，HDR 值按模 256 回绕
  - 位置: src/features/color/core/image_engine.py:4055
  - 证据:
    ```python
    self.img_full_original = (self.img_high * 255).astype(np.uint8) if self.img_high.dtype == np.float32 else self.img_high
    ```
    对照同文件 `convert_to_uint8`（775-778）：`return (np.clip(img, 0, 1) * 255).astype(np.uint8)`。`normalize_to_float32`（768-773）对 float 输入原样保留（"Assume already 0-1 or HDR"），EXR 等 HDR 源可 >1.0。
  - 影响: HDR 值（如 2.0 → 510）经 astype(uint8) 回绕为 254 之类的错误像素，导出图像出现亮度反转伪影；这是「HDR 数据无理由 clamp/截断」审查维度的直接违例（此处连 clamp 都没有，是更糟的回绕）。
  - 建议: 先 `np.clip(img_high, 0.0, 1.0)` 再乘 255，与 convert_to_uint8 统一。
  - 置信度: 中（需要 float HDR 源进入 img_high 且走 grab_viewport_image/render_cached_image 路径）

- [AUD-0605] [Major] bake_layer 全分辨率数据不分数值类型一律除以 255，uint16/float HDR 源烘焙结果数值错误
  - 位置: src/features/color/core/image_engine.py:4399-4404
  - 证据:
    ```python
    if self.img_full_original is not None:
        # Convert uint8 BGR to float32 RGB for GPU upload
        full_rgb = cv2.cvtColor(self.img_full_original, cv2.COLOR_BGR2RGB)
        full_rgba = cv2.cvtColor(full_rgb, cv2.COLOR_RGB2RGBA)
        full_res_image = np.ascontiguousarray(full_rgba.astype(np.float32) / 255.0)
    ```
    注释假定 uint8，但 `img_full_original` 可为 uint16（RAW 重解码路径 1979 直接赋值）或 float（set_image 保存 float EXR 源，697-699）。对照 `_do_render_full_res`（3077-3085）对 uint8/uint16/float 分别 /255、/65535、原样处理。
  - 影响: uint16 源烘焙结果整体过曝约 257 倍、float HDR 源缩小 255 倍——bake 写入 EXR 与 GPU 纹理的数据错误，且后续渲染持续消费该错误缓存。
  - 建议: 复用与 `_do_render_full_res` 相同的 dtype 分支归一化逻辑。
  - 置信度: 中（取决于 RAW/EXR 源是否以非 uint8 进入 img_full_original；代码路径存在）

- [AUD-0606] [Major] request_sam_decode 跨线程信号发射可变列表（同一文件其他发射点都做了 .copy()）
  - 位置: src/features/color/core/image_engine.py:1769-1779（对照 1799-1801、1848-1850）
  - 证据:
    ```python
    self._sam_points.append((x, y))
    self._sam_labels.append(label)
    logger.info(f"sig_process_sam_decode with {len(self._sam_points)} points")
    self.sig_process_sam_decode.emit(self._sam_points, self._sam_labels)
    ```
    而 `undo_sam_point`/`remove_sam_point` 均发射 `self._sam_points.copy(), self._sam_labels.copy()`。该信号连接到 AI 工作线程的槽（228-230），列表按引用跨线程传递。
  - 影响: 工作线程在后台遍历/消费 `_sam_points` 时，UI 线程下一次点击继续 append 同一列表——数据竞争，可能解码出多余点、几何错乱，或在实现遍历时崩溃。
  - 建议: 与其他两个发射点一致，发射前复制：`emit(list(self._sam_points), list(self._sam_labels))`。
  - 置信度: 高

- [AUD-0607] [Major] _do_render_full_res 异常路径泄漏 GPU 纹理：3211 行之前创建的纹理未入 garbage 清单，mask/aux 纹理仅在正常路径释放
  - 位置: src/features/color/core/image_engine.py:3058, 3086-3091, 3163-3181, 3211, 3281-3378, 3404-3434, 3444-3463
  - 证据:
    ```python
    texture_garbage = []            # 3058
    input_tex = ctx.texture((w, h), 3, data=input_data.tobytes(), dtype='f4')   # 3086
    raw_dev_tex = ctx.texture((w, h), 4, dtype='f4')                             # 3090
    ...
    texture_garbage = [input_tex, raw_dev_tex, ping_tex, pong_tex]               # 3211 才登记
    ...
    # Release mask texture
    if mask_tex is not self.white_tex:
        self.fb_manager.release_temp_texture(mask_tex)                           # 3377-3378 仅正常路径
    ```
    `raw_lut_tex_full`（3181）与 `dummy_matte`（3434）同样只在成功路径 release；`prog_raw`/`prog_pass` 创建后任何路径都不 release。
  - 影响: 3086-3211 之间任一异常（如 RAW_VIEW_FRAGMENT 编译失败、_load_lut_sync 内部以外的 GL 错误）使 `input_tex`/`raw_dev_tex` 泄漏；循环中渲染异常使 fb_manager 临时纹理（mask/curves/hsl/warper/lut）脱离池管理；finally 的 bare except 再把问题吞掉。反复失败导出会累积显存占用，违背 deferred-release 项目约定（此处甚至连即时释放都没做到）。
  - 建议: 创建后立即 append 到 texture_garbage；mask/aux 纹理纳入同一清单或 try/finally；`prog_raw`/`prog_pass` 显式 release。
  - 置信度: 高

- [AUD-0608] [Major] bake 进行中执行 unbake 会被异步 EXR 保存完成信号"复活"，且复活后的 bake 无 digest 记录、永不自动失效
  - 位置: src/features/color/core/image_engine.py:4424-4454, 4642-4690, 4509-4523
  - 证据:
    ```python
    # unbake_layer → _unbake_layer_internal：删文件、清纹理、pop digest，无任何 pending-bake 取消
    # 随后 _on_baked_save_finished（保存线程完成后）：
    layer.is_baked = True
    layer.baked_file_name = baked_file_name
    digest = self._pending_bake_digests.pop(layer.id, None)
    if digest is not None:
        self._bake_prefix_digests[layer.id] = digest
    # _validate_baked_prefixes：
    recorded = self._bake_prefix_digests.get(layer.id)
    if recorded is None:
        continue   # 无台账 → 永不失效
    ```
  - 影响: 用户在 bake 的 EXR 落盘窗口内点击解冻：解冻清理生效后，`_on_baked_save_finished` 无条件重新置 is_baked=True、重传纹理、重渲染（用户操作被静默回滚）；更糟的是此时 `_pending_bake_digests` 已被 unbake 消费掉（或从未登记），复活层没有 digest，P0-COL-04 的前缀失效校验对其永久失效——下层任何修改都不会再自动解冻，渲染持续使用过期前缀。
  - 建议: unbake 时同时 `self._pending_bake_digests.pop(layer.id, None)` 并设置一次性"bake 取消"标志；`_on_baked_save_finished` 在写状态前检查该层是否仍处于待 bake 状态（或在 unbake 后保留墓碑标记拒绝复活）。
  - 置信度: 中（时间窗为 EXR 异步保存时长，大图下并不窄）

- [AUD-0609] [Major] _layer_render_data / _create_layer_snapshot 把 ndarray（curves_lut、hsl_curve_lut、warper_params、ai_matte）按引用传给渲染线程，破坏"不可变 DTO"承诺
  - 位置: src/features/color/core/image_engine.py:69-90, 4705-4729
  - 证据:
    ```python
    "curves_lut": layer.curves_lut,
    "hsl_curve_lut": layer.hsl_curve_lut,
    "warper_params": layer.warper_params,
    ...
    # _create_layer_snapshot 同样：
    'curves_lut': layer.curves_lut,
    'warper_params': layer.warper_params,
    'ai_matte': layer.ai_matte,
    ```
    docstring 声称 "Build the immutable layer DTO consumed by the render thread"，且同 dict 中 `grading_params.copy()`、`params.copy()` 等都做了拷贝，唯独三个 ndarray 字段共享引用。
  - 影响: 渲染线程对 DTO 做 `.tobytes()`/上传时，若 UI 线程对面板编辑触发对同一 ndarray 的原地写（本文件外行为，无法排除），将产生撕裂的 LUT/网格/matte 数据——跨线程共享可变状态，属线程安全维度的明确违例。
  - 建议: ndarray 字段按需浅拷贝（`np.array(x, copy=True)` 或约定消费端只读并在写入端强制替换而非原地改）。
  - 置信度: 中（取决于文件外部是否存在原地修改；本文件内 apply_curves 等是整体替换，风险较低）

- [AUD-0610] [Major] apply_changes 借道 set_image(is_new_file=True) 实现烘焙，触发"新文件"语义的一揽子副作用
  - 位置: src/features/color/core/image_engine.py:1027-1036（对照 set_image:697-712）
  - 证据:
    ```python
    u8 = full_res_result if full_res_result.dtype == np.uint8 else np.clip(full_res_result * 255, 0, 255).astype(np.uint8)
    low = FileIO._resize_to_fit(u8, FileIO.LOW_RES_SIZE)
    high = FileIO._resize_to_fit(u8, FileIO.HIGH_RES_SIZE)
    self.set_image(u8, high, low)      # is_new_file 默认 True
    ```
    set_image 的 is_new_file 分支会：`self.current_file_path = ... path`（此处为 None）、清空 `ai_plugin_result_cache`（705）、重置 crop_params（706-709）、重探测 sRGB（path=None → 按 sRGB 处理）。
  - 影响: 应用更改后：工程失去源文件引用（RAW 面板 `if not self.current_file_path: return` 直接失效、离线检测受影响）；AI 插件结果的 CPU 持久化缓存被清空（保存工程时 AI 结果可能丢失，GPU 纹理仍在但与持久层不一致）；色彩管理被静默切回 sRGB 直通口径。
  - 建议: 为"烘焙到当前会话"提供独立的内部路径（更新图像 + UPLOAD_IMAGE + `_source_epoch += 1`），不复用 set_image 的新文件语义；或给 set_image 增加 `preserve_session` 参数。
  - 置信度: 中（部分重置或属预期"扁平化"行为，但 AI 缓存清空与文件引用丢失很难解释为预期）

- [AUD-0611] [Major] _on_raw_update_finished 重解码替换源图像内容但不递增 _source_epoch，bake 前缀失效校验漏掉该路径
  - 位置: src/features/color/core/image_engine.py:1953-1999（对照 set_image:682-683、_compute_prefix_digest:4492）
  - 证据:
    ```python
    # set_image:
    # P0-COL-04：源图替换使所有 bake 前缀过期（参与前缀摘要）
    self._source_epoch += 1
    # _on_raw_update_finished（直接替换像素，绕过 set_image）：
    self._last_applied_raw_params = applied_params.copy()
    for layer in self.layers:
        layer.mask_dirty = True
    self.img_full_original = full
    ```
    digest 以 `src_epoch={self._source_epoch}` 为盐（4492），但该路径不改 epoch。
  - 影响: RAW 参数重解码（WB 'auto' 或非交互变更触发的后台 decode）把新的白平衡/曝光烘焙进源图像后，既有 baked 层的记录摘要仍然匹配——校验不失效，渲染继续用旧像素的 bake，出现"调了白平衡但画面一半新一半旧"的前缀不一致。
  - 建议: `_on_raw_update_finished` 中同样 `self._source_epoch += 1`（或在摘要中纳入图像内容哈希）。
  - 置信度: 高

- [AUD-0612] [Suggestion] on_raw_params_changed 就地变异调用方 params dict 并将同一引用跨线程传给 RAW worker
  - 位置: src/features/color/core/image_engine.py:1855-1882, 1943-1947
  - 证据:
    ```python
    params['user_wb'] = curr_wb          # 变异调用方对象
    ...
    QMetaObject.invokeMethod(self.raw_worker, "decode",
                           Qt.ConnectionType.QueuedConnection,
                           Q_ARG(str, self.current_file_path),
                           Q_ARG(dict, params))   # 引用跨线程
    ```
    对照 set_image:757 对 user_wb 专门 `list(...)` "防止引用污染"的意识，此处未做隔离。
  - 影响: RawPanel 若复用/继续修改同一 dict，工作线程读到中途状态；engine 持有的 `current_raw_params` 与调用方共享可变结构。
  - 建议: 入口 `params = dict(params)` 拷贝后再写入 user_wb，传给 worker 前再浅拷贝。
  - 置信度: 中

- [AUD-0613] [Suggestion] cs_map/gamma_map 大字面量在 4 处重复定义且每次渲染调用都重建
  - 位置: src/features/color/core/image_engine.py:445-456, 1896-1905, 2843-2852, 3113-3122
  - 证据: 四段几乎相同的 `"sRGB": 0, "Rec.709": 1, ...` / `"Linear": 0, "sRGB": 1, ...` 字典字面量；`render_pipeline` 每帧（含 playback）都执行 2843-2852 的重建。
  - 影响: 代码重复（改一处漏三处的正确性风险）；每次渲染的常量字典重建属无谓开销（绝对值小，仅记录）。
  - 建议: 提升为模块级常量 `COLOR_SPACE_MAP` / `GAMMA_MAP`，四点共用。
  - 置信度: 高

- [AUD-0614] [Suggestion] remove_layer 不清理 ai_plugin_result_cache / _bake_prefix_digests / _pending_bake_digests 中的残留条目
  - 位置: src/features/color/core/image_engine.py:1075-1093（对照 240-246）
  - 证据:
    ```python
    self.layers.pop(index)
    self.active_layer_index = max(0, min(self.active_layer_index, len(self.layers) - 1))
    if self.renderer:
        self.renderer.submit(RenderCommand.REMOVE_LAYER, layer_id)
    ```
    GPU 侧清理了，CPU 侧三个以 layer.id/ (layer_id, instance_id) 为键的缓存未清。
  - 影响: 删除带 AI 结果或 bake 台账的图层后，CPU 数组与 digest 残留（显式内存滞留；`_on_layer_baked` 对已删层 return 时 `_pending_bake_digests` 同样残留）。
  - 建议: remove_layer 中 pop 该 layer.id 相关的全部 CPU 侧键。
  - 置信度: 高

- [AUD-0615] [Suggestion] 裸 except / 宽泛 except 吞领域错误
  - 位置: src/features/color/core/image_engine.py:747-752, 3444-3447, 3450-3463, 3749-3751
  - 证据:
    ```python
    try:
        import rawpy
        with rawpy.imread(path) as raw:
            ...
    except:
        self.current_raw_metadata["camera_wb"] = [2.0, 1.0, 1.5, 1.0]   # 裸 except
    ...
    except Exception as e:
        logger.error(f"-Res Render Error: {e}")
        import traceback
        traceback.print_exc()                                            # 全分辨率渲染失败被吞，无 error_occurred 上报
    ```
  - 影响: 裸 except 连 KeyboardInterrupt/SystemExit 一并吞掉；全分辨率渲染失败仅打印控制台，UI 层无 `error_occurred` 通知，用户只看到导出静默变 None。
  - 建议: 至少 `except Exception`；全分辨率失败路径 emit error_occurred。
  - 置信度: 高

- [AUD-0616] [Suggestion] prog_raw / prog_pass 每次全分辨率渲染新建 program 且从不显式 release
  - 位置: src/features/color/core/image_engine.py:3096, 3194-3206
  - 证据:
    ```python
    prog_raw = ctx.program(vertex_shader=PASSTHROUGH_VERTEX, fragment_shader=RAW_VIEW_FRAGMENT)
    ...
    prog_pass = self.gl_manager.ctx.program(
        vertex_shader=PASSTHROUGH_VERTEX,
        fragment_shader=""" ... """
    )
    ```
    两者均未进入任何释放路径（pipeline 管理的 prog_comp 除外）。
  - 影响: 依赖 CPython 引用计数在任意时机触发 ModernGL 对象析构，与项目 deferred-release 约定不符；重复全分辨率导出期间存在 GL 对象生命周期不可控的窗口。
  - 建议: 缓存为实例级 program（与 passthrough_prog 同模式）或在 finally 中显式 release。
  - 置信度: 高

- [AUD-0617] [Suggestion] scope 读回路径在拿不到 GL mutex 时仍读回并缓存黑帧
  - 位置: src/features/color/core/image_engine.py:3963-3973（render_full_image_for_scope:4012-4025 同模式）
  - 证据:
    ```python
    self._scope_fbo.use(); self.gl_manager.ctx.scissor = None; self._scope_fbo.clear(0, 0, 0, 1)
    self._draw_background_tex_id(...)   # 内部 tryLock 失败时返回 False，不绘制
    data = self._scope_fbo.read(...)
    ...
    self.cached_thumbnail = (np.clip(res_bgr_float, 0, 1) * 255).astype(np.uint8)
    return res_bgr_float
    ```
  - 影响: 渲染线程持锁期间 scope 读回纯黑帧并覆盖 `cached_thumbnail`，示波器/缩略图闪黑。此外 `_scope_fbo/_scope_tex` 的创建与读回本身未持 gl_manager.mutex。
  - 建议: 检查 `_draw_background_tex_id` 返回值，False 时直接返回上一次结果/None；scope FBO 操作纳入 mutex 保护。
  - 置信度: 中

- [AUD-0618] [Suggestion] _validate_baked_prefixes 在每次渲染（含 playback 帧）对每个 baked 层做全前缀 md5 摘要
  - 位置: src/features/color/core/image_engine.py:2728, 4501-4533
  - 证据:
    ```python
    # render_pipeline 入口无条件执行：
    self._validate_baked_prefixes()
    # 内部对每个 baked 层：
    current = self._compute_prefix_digest(
        [_layer_render_data(l) for l in self.layers[:j + 1]])
    ```
    `_canonical_digest_part` 对 ndarray（curves_lut 等）做 `tobytes()` 哈希。
  - 影响: 存在 baked 层时，视频播放每帧都为 0..j 层构建 DTO 并哈希全部 LUT 字节——4K 实时基准下的无谓 CPU 开销。
  - 建议: 摘要结果按「层内容版本号」缓存（层上维护 revision，参数变更时递增），或至少对 playback/grading 原因跳过校验。
  - 置信度: 中（开销随层数/LUT 大小线性放大，绝对值未测）

- [AUD-0619] [Suggestion] 大量公开方法缺少类型注解
  - 位置: src/features/color/core/image_engine.py:668, 805, 1027, 1063-1204, 1389, 1854, 2001, 2698（部分参数有默认值但无注解）等
  - 证据: 如 `def set_image(self, full, high, low, is_new_file=True, path=None, is_video=False, video_path=None):`、`def apply_changes(self):`、`def grade_image(self, lift, gamma, gain, exp, ...):`。
  - 影响: 与 `.clinerules` 强类型约束不一致；仅记录，不改行为。
  - 建议: 按项目规范补齐参数/返回类型注解。
  - 置信度: 高

### 通过项
- 线程安全：paint/render 循环已有 `_in_draw_background` 防递归护栏（2733-2741）；`shutdown_worker_threads` 的协作取消 + terminate 兜底 + 进程级引用防 QThread 析构 Abort（342-385）实现正确；`_layer_render_data` 对 dict 类字段做了拷贝；bake 异步保存经信号回 UI 线程（queued）。
- GPU 资源生命周期：`_do_render_full_res` 正常路径的 ping/pong/input/raw_dev/aux 纹理与 FBO 均在 finally 释放；`_generate_full_res_matte`/`_apply_matte_postprocess`/`_blur_matte_pass` 的 fb_manager 临时纹理借还配对正确（正常路径）；`discard_changes/reset_all_layers` 主动提交 CLEAR_AUX_TEX 清理陈旧 GPU 槽位。
- GLSL/HDR 正确性（内嵌字符串）：3196-3205 与 4149-4160 两段内嵌 fragment shader 无缺 `.0` 字面量、无循环、无动态注入；mask 蒙版 clamp(0,1) 属语义正确范围。
- 信号防递归：`set_film_grain_params` 等参数入口只更新后统一走 render_pipeline，无成环连接；`_baked_save_finished` 仅在 renderer 首建时连接一次（603 守卫）。
- 取消路径：RawUpdateWorker 的 `_cancel_event` 抑制过期结果（119-128）；`request/cancel_full_resolution` 的请求注册/遗忘配对（2972-2985）；`render_full_resolution` 超时兜底 + 信号断连清理（996-1007）。
- 输入验证：`_path_transfer_is_linear` 全包 try/except 且失败回退保守值（137-157）；`_tracking_frame_context` 对 provider 结果做类型/区间防御（2469-2485）；`load_reference_still` 校验空图像（1263）。
- 确定性：grain 种子完全由参数+帧号决定（GRAIN_ANIMATION_STEP 黄金比例步进），回放/重渲可复现（31-47, 1525-1553）。

---

## 统计
- Critical: 1（AUD-0600）
- Major: 11（AUD-0601 ~ AUD-0611）
- Suggestion: 8（AUD-0612 ~ AUD-0619）
