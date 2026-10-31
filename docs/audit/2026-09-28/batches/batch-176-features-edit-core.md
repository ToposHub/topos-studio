# batch-176 审查报告 — src/features/edit/core//

编号区间：AUD-17600–AUD-17699。九维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解。

---

## src/features/edit/core/recording_manager.py (行数: 1356)

### 发现

- [AUD-17600] [Critical] 磁盘耗尽安全终止路径在持有 `_buffer_lock` 时调用 `_finalize_take`，非重入锁同线程二次加锁 → 死锁挂死应用
  - 位置: src/features/edit/core/recording_manager.py:1079-1090（触发点 1089）、src/features/edit/core/recording_manager.py:1211（死锁点）
  - 证据:
    ```python
    # _on_audio_ready（1079-1090）——except 处理器位于 with 块内部
            else:
                with self._buffer_lock:
                    if self._pcm_buffer is None:
                        self._pcm_buffer = _ChunkedPcmBuffer(channels=channels)
                    try:
                        self._pcm_buffer.append(raw)
                    except DiskSpaceExhausted as e:
                        self._set_state(self.STATE_FAILED)
                        if self.on_error:
                            self.on_error(str(e))
                        self._finalize_take(save_partial=True)   # 仍在锁内
                        return
    ```
    ```python
    # _finalize_take（1211）——同线程再次获取同一把锁
        with self._buffer_lock:
            pcm = self._pcm_buffer
    ```
  - 影响: `threading.Lock` 不可重入。磁盘剩余空间低于 `_DISK_FAIL_BYTES`（长录音溢写场景的真实路径）时，`append` 抛 `DiskSpaceExhausted`，except 处理器在未释放 `_buffer_lock` 的情况下调用 `_finalize_take`，后者在 1211 行再次 `acquire` → 调用线程（readyRead 交付的主线程）永久阻塞，整个应用挂死，且注释中承诺的"安全终止并保留已录数据"完全无法发生。另注：1087-1088 的 `on_error` 回调同样在持锁状态下调用，若回调再进入 `stop_recording`→`_finalize_take` 也会经同一路径死锁。
  - 建议: 把 `DiskSpaceExhausted` 的处理移出 `with self._buffer_lock:` 块（先在锁内仅捕获并置标志/释放 pcm 引用，退出锁后再 `_set_state(FAILED)` + `on_error` + `_finalize_take`）；或将 `_buffer_lock` 改为 `threading.RLock` 并在回调/级联调用前统一约定"不持锁调用外部回调"。
  - 置信度: 高

- [AUD-17601] [Major] take 落盘（整段 WAV 流式写盘）在 UI/主线程同步执行，长 take 卡死界面
  - 位置: src/features/edit/core/recording_manager.py:1162、1164-1169、1220-1225
  - 证据:
    ```python
    # stop_recording（1162）：无 post-roll / 离线路径 → 同步落盘
        return self._finalize_take(save_partial=False)

    # post-roll 路径（1164-1169）：QTimer 回调 → 仍在主线程
    def _on_post_roll_finished(self) -> None:
        self._post_roll_timer = None
        if self._state != self.STATE_STOPPING:
            return
        self._finalize_take(save_partial=False)
    ```
    ```python
    # _finalize_take（1220-1225）：整段流式写盘 + bext 注入
            ok = write_take_blocks(
                take.file_path, pcm.iter_blocks(),
                channels=self._config.channels, ...
            )
    ```
  - 影响: readyRead/QTimer 均在主线程交付（QAudioSource 在主线程创建，auto connection 队列回主线程），`stop_recording()` 由 UI 按钮触发的同步路径与 `_on_post_roll_finished` 的定时器路径都在 UI 线程执行 `write_take_blocks` 的全量磁盘写。本模块自设 64MB 内存溢写 + 500MB 磁盘预警，明确支持长录音；48kHz/24bit mono 约 144KB/s，10 分钟 take 约 86MB、更长的 ADR session 可达数百 MB，慢盘/网络盘上 UI 冻结数秒以上。`_on_audio_ready` 内 `_spool_oldest` 的溢写小写入同样发生在主线程（有界小块，影响较小但同源）。
  - 建议: 将 `_finalize_take` 的落盘（`write_take_blocks` + bext 注入）移到后台线程/任务队列，主线程仅在完成后收 `on_take_completed`；期间状态置为落盘中（如新增 flushing 态）防止重复 stop。
  - 置信度: 高

- [AUD-17602] [Major] `_spool_oldest` 先 pop 后磁盘预检，预检抛出时该块既未落盘也未回内存 → 保留的 partial take 缺一段音频且时长虚高
  - 位置: src/features/edit/core/recording_manager.py:123-141、119
  - 证据:
    ```python
    def _spool_oldest(self) -> None:
        oldest = self._chunks.pop(0)
        self._memory_bytes -= oldest.nbytes
        self._check_disk_space(oldest.nbytes)      # 可能在此抛 DiskSpaceExhausted
        if self._spool_file is None:
            ...
        interleaved = np.ascontiguousarray(oldest.T)
        self._spool_file.write(interleaved.tobytes())  # 抛出时永远执行不到
        ...
        self._spooled_samples += oldest.shape[1]
    ```
    ```python
    # append（119）：total_samples 在 pop 后不回退
        self._total_samples += block.shape[1]
    ```
  - 影响: 磁盘预检失败（剩余空间低于阈值）时，`oldest` 已从 `_chunks` 弹出但未写入溢写文件，`iter_blocks` 再也读不到它；`_total_samples` 仍计入该块 → `take.duration_seconds = pcm.total_samples / sample_rate`（1219）比文件实际内容多出约一个设备回调块（几十毫秒级），与"安全终止并保留已录数据"（DiskSpaceExhausted 的文档承诺）不符，外部 DAW 按 bext TimeReference 对齐时出现缺口。
  - 建议: 将 `_check_disk_space` 前移到 `append` 入口（写入前预检、不动数据），或 pop 失败路径把块塞回 `_chunks` 头部并回退 `_memory_bytes`；同时在该失败分支从 `_total_samples` 扣除丢失块（或改为按实际产出块计数）。
  - 置信度: 高

- [AUD-17603] [Suggestion] `RecordingConfig` 无输入验证：`channels=0` 触发 ZeroDivisionError 被宽泛 except 吞成 warning，录音静默无声
  - 位置: src/features/edit/core/recording_manager.py:1055-1056、1099-1100、674
  - 证据:
    ```python
            if channels > 1:
                # PyAV/Qt 交错 → reshape 为 (channels, n)
                n = len(raw) // channels      # channels=0 → ZeroDivisionError
                raw = raw[:n * channels].reshape(n, channels).T
    ```
    ```python
        except Exception as e:
            logger.warning(f"Audio read failed: {e}")   # 每个回调静默吞掉
    ```
    ```python
    codec = "pcm_s24le" if bit_depth == 24 else "pcm_s16le"   # 8/32 位请求被静默降为 16bit
    ```
  - 影响: 配置来自用户设置/持久化（外部数据），`channels<=0`、`sample_rate<=0`、`bit_depth` 非 {16,24} 均不报错：前者让整个录音静默失败只在日志留 warning；后者把 32/8 位请求静默写成 16bit，交付规格与预期不符。违反"外部数据须验证"铁律。
  - 建议: 在 `RecordingConfig.__post_init__`（frozen dataclass 可用 `object.__setattr__`）或 `start_recording` 入口校验 channels∈{1,2}、sample_rate>0、bit_depth∈{16,24}（非法直接 `on_error` + 返回 False）；bit_depth 非法时显式失败而非静默选 s16le。
  - 置信度: 高（逻辑确定；channels=0 是否实际可达取决于上游设置面板，标中高）

- [AUD-17604] [Suggestion] `_on_audio_ready` 最外层宽泛 `except Exception` 掩盖音频处理链中的领域/程序错误
  - 位置: src/features/edit/core/recording_manager.py:1099-1100
  - 证据:
    ```python
        except Exception as e:
            logger.warning(f"Audio read failed: {e}")
    ```
  - 影响: 该 try 覆盖 readAll、numpy 变换、rolling/take 缓冲路由、电平计算与 on_level 回调；任何真实缺陷（含 AUD-17600 修复前死锁之外的属性竞态、回调内异常）都被降级为 warning 并无限重复，录音表面继续实际数据流已断，违反"不得宽泛 except 掩盖领域错误"。
  - 建议: 收窄 except 到预期 IO/转换异常（OSError/ValueError），并区分 `on_level` 回调异常（单独捕获）；对连续失败计数，超阈值转 `STATE_FAILED`。
  - 置信度: 中

- [AUD-17605] [Suggestion] 设备启动失败置 `STATE_FAILED` 后 `start_recording` 仍返回 True，返回值语义失真
  - 位置: src/features/edit/core/recording_manager.py:948-951、1034-1036
  - 证据:
    ```python
        else:
            self._begin_capture()      # 内部 _start_capture_device 失败 → STATE_FAILED
        return True
    ```
    ```python
        if not self._start_capture_device():
            self._set_state(self.STATE_FAILED)
    ```
  - 影响: 无 pre-roll 路径中 `_begin_capture` 同步把状态置 FAILED，但函数仍返回 True；仅依赖返回值的调用方会误认为已开始录音（有 on_error 回调兜底，但契约不一致）。
  - 建议: `return self._state != self.STATE_FAILED`（或提前探测设备并返回 False），文档同步说明。
  - 置信度: 中

- [AUD-17606] [Suggestion] `TakeInfo.status` 文档枚举与实际取值不一致（缺 "pending"/"completed_partial"）
  - 位置: src/features/edit/core/recording_manager.py:270、286、1228
  - 证据:
    ```python
        status: "recording" / "completed" / "failed" / "discarded"   # 文档（270）
    ...
        status: str = "pending"                                      # 默认值（286）
    ...
            take.status = "completed" if not save_partial else "completed_partial"  #（1228）
    ```
  - 影响: 下游（take sheet UI、导入逻辑、测试）若按文档枚举做 match/校验，会漏掉 "pending" 与 "completed_partial" 两个真实状态，产生未处理分支。
  - 建议: 更新 docstring 为完整枚举，或定义 `Status` 常量类（与 RecordingManager 的 STATE_* 风格一致）供下游引用。
  - 置信度: 高

- [AUD-17607] [Suggestion] `_finalize_take` 内 `stop()` 可能同步再入 `stateChanged` → 二次 `_finalize_take`，存在 take 重复归档/重复回调风险
  - 位置: src/features/edit/core/recording_manager.py:1102-1118、1202-1208
  - 证据:
    ```python
    # _on_source_state_changed
            if state == QAudio.State.StoppedState:
                err = self._audio_source.error() if self._audio_source else None
                if err is not None and err != QAudio.Error.NoErrorError:
                    ...
                    self._finalize_take(save_partial=True)
    ```
    ```python
    # _finalize_take 内部
        if self._audio_source is not None:
            try:
                self._audio_source.stop()      # 同线程直接连接 → 可能同步再入上方处理器
    ```
  - 影响: 若设备处于错误态时 error 未被显式 stop 复位（Qt 版本相关），`_finalize_take` 中的 `stop()` 会同步再入 `_on_source_state_changed` 并重入 `_finalize_take`：内层完成归档/回调后外层继续执行，`self._takes.append(take)`（1238）与 `on_take_completed`（1242-1243）对同一 take 各执行两次 → take 列表重复、控制器重复导入同一 WAV。
  - 建议: 为 `_finalize_take` 增加重入保护（`_finalizing` 标志或入口检查 `_current_take is None` 早退——当前 take 清空发生在末尾，覆盖不到中段重入）；断开 stateChanged 连接后再 stop。
  - 置信度: 低（取决于 Qt `stop()` 对 error 的复位语义，未实测；模式本身是真实风险）

### 通过项
- GPU 资源生命周期：纯音频模块，无 OpenGL/ModernGL 对象，不适用
- GLSL/HDR 正确性：无着色器；`write_take_blocks` 的 `np.clip(block, -1.0, 1.0)`（709）是 int16/24 交付格式所需（超出 ±1 会整数回绕），属有理由 clamp，不违反 HDR 铁律（本模块亦非 HDR 内部数据路径）
- 信号防递归：无 Qt 信号（回调式）；`is_recording` 含 STOPPING + `start_recording` 双重守卫（876）防 post-roll 期间重复 arm
- 状态提交一致性：discard 防二次落盘的二次防御（1181-1191）正确；cancel/discard/finalize 三路径均停定时器、停设备、清缓冲，无残留路径
- 取消路径：`cancel_recording`/`discard_current_take` 完整清理定时器、设备、缓冲并删除溢写临时文件（181-199）
- 性能模式：分块缓冲 O(1) 摊销追加、64MB 内存上限 + 磁盘溢写、流式落盘不整体物化（P0-07 落实）；`estimate_click_offset_ms` 用 valid 互相关且切片边界安全（valid 模式保证 peak_index+template.size ≤ signal.size）
- 原子落盘：临时文件 + `os.replace`（720），编码失败不覆盖已有 take
- 类型注解：完整（含回调 Optional[Callable[...]]）

---

## src/features/edit/core/render_cache.py (行数: 348)

### 发现

- [AUD-17610] [Suggestion] `RenderCacheSettings.from_dict` 对持久化外部数据零验证，非法类型/范围直接进入后续算术与缓存管理
  - 位置: src/features/edit/core/render_cache.py:49-64
  - 证据:
    ```python
    @staticmethod
    def from_dict(data: dict[str, Any]) -> RenderCacheSettings:
        return RenderCacheSettings(
            codec=data.get("codec", "topos_proxy"),
            ...
            auto_mode=data.get("auto_mode", "off"),
            max_size_gb=data.get("max_size_gb", 50.0),
            container=data.get("container", "mov"),
        )
    ```
  - 影响: 工程文件/手改 JSON 属外部数据：`max_size_gb` 为 null/字符串/负数时不报错，缓存清理时的容量比较与删除循环拿到非法值才崩溃；`custom_width/height` 非正数会流向编码几何。违反"外部数据须验证"铁律（下游未见补偿校验）。
  - 建议: `from_dict` 内做类型/范围钳制（float()/min-max、auto_mode 枚举白名单），非法值回退默认并记 warning。
  - 置信度: 中

- [AUD-17611] [Suggestion] `RenderCacheManifest.create_new` 生成 CWD 相对路径 `Cache/...`，状态检查与 worker 均按进程 CWD 解析
  - 位置: src/features/edit/core/render_cache.py:309
  - 证据:
    ```python
        return RenderCacheManifest(
            ...
            path=f"Cache/clip_{clip_id[:8]}_{cache_id[:8]}.{extension}",
    ```
  - 影响: 消费端（render_cache_status.py:145/210/265 的 `os.path.exists(manifest.path)`、render_cache_worker.py:395-398 的非绝对路径回退 `os.path.join("Cache", basename)`）都按当前工作目录解析；主流程 edit_controller 用绝对路径构造 manifest（已核实），但经 `create_new`（facade `create_render_cache_manifest_new` 暴露）创建的 manifest 在进程 CWD 变化后会被误判 missing → 状态条误红、触发不必要的重渲染。
  - 建议: `create_new` 增加可选 `cache_root`/基准目录参数拼绝对路径；或消费端统一经 project 根解析相对路径。
  - 置信度: 中

### 通过项
- 线程安全：纯数据模型 + 纯函数指纹构建（不可变 frozen dataclass，无共享可变状态）
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不适用（无 GL 对象、无着色器、无 Qt 信号）
- 状态提交一致性：指纹刻意不含全局 revision（注释说明 undo/redo 语义），`is_valid_for` 对 legacy 空 effect_signature/fingerprint 向后兼容（340-347）；`from_dict` 对 legacy ProRes/MOV 路径推断 codec/container（263-286）防旧缓存被静默重解释
- 异常路径：`_fingerprint_value` 对 to_dict 失败 repr 兜底（97-98）、`_asset_version` 捕获 OSError 置 -1（119-125），指纹永不阻塞编辑
- 性能模式：指纹用 mtime+size 代替整文件哈希（108）
- 类型注解：完整

---

## src/features/edit/core/render_cache_encoder.py (行数: 249)

### 发现

- [AUD-17612] [Suggestion] `close()` 异常路径不清理临时文件且 `_is_open`/编码器状态不复位；未 open 时 `write_frame*` 静默丢帧
  - 位置: src/features/edit/core/render_cache_encoder.py:169-191、123-124、148-149
  - 证据:
    ```python
        try:
            self._encoder.close()
            self._is_open = False
            ...
            os.replace(self._config.temp_path, self._config.output_path)
            ...
        except Exception as e:
            logger.error("Failed to finalize render cache: %s", e)
            return False          # temp 文件残留；_is_open 仍为 True
    ```
    ```python
        if not self._is_open or self._encoder is None:
            return                # 帧被静默丢弃，调用方无感知
    ```
  - 影响: close 失败（如 `os.replace` 跨设备失败、编码器 close 抛错）后 `.tmp` 文件留在缓存目录，反复失败的重渲染会累积残留（仅当调用方再调 abort 才清理）；同时 `_is_open` 保持 True，后续误用仍走"已打开"分支。`write_frame_bgr/write_frame` 在未 open 时静默 return，会把调用方的编码错误掩盖成"成功但空缓存"。
  - 建议: close 的 except 分支复位 `self._is_open = False` 并尝试删除 temp（或统一委托 abort 清理）；`write_frame*` 未 open 时 raise（或至少 logger.error），让调用方能感知。
  - 置信度: 高（代码路径确定，触发依赖失败场景）

- [AUD-17613] [Suggestion] `_parse_codec` 返回注解为裸 `tuple`，泛型参数缺失
  - 位置: src/features/edit/core/render_cache_encoder.py:219-220
  - 证据:
    ```python
        @staticmethod
        def _parse_codec(codec_str: str) -> tuple:
    ```
  - 影响: 类型检查（mypy/pyright strict）下丢失 (codec, profile, pix_fmt) 结构信息，调用点无法静态校验解包。
  - 建议: `-> tuple[str, str | None, str]`。
  - 置信度: 高

### 通过项
- 线程安全：后台渲染线程专用封装，无可变共享状态；open/write/close 生命周期由 worker 串行驱动
- GPU 资源生命周期：无 GL 对象；`abort()` 先关编码器（释放 PyAV 容器/文件描述符）再删临时文件，无句柄泄漏（199-217）
- 输入验证：未知 codec 显式 `ValueError` 拒绝静默回退（244-248）、VP8/9 显式重定向到实时录制器（222-228）、`write_frame` 几何不匹配显式 raise（152-156）——均为良好实践
- 原子写：temp → `os.replace` 原子换名，重渲染期间旧缓存始终可见（176-183）
- GLSL·HDR：h264_proxy 走 yuv420p 8bit 为文档化的用户显式选择（硬件路径），默认 topos_proxy/dnxhr 保持 10bit 4:2:2，非违规

---

## src/features/edit/core/render_cache_evaluator.py (行数: 120)

### 发现

（无发现）

### 通过项
- 通过（无逻辑缺陷）：纯函数打分器，无 Qt/GPU/IO/信号；`score_clip` 对 `effective_speed` 异常兜底为 1.0（67-69）、`animated_params` 类型检查（54-57）、`select_auto_cache_clips` 对生成器/None 输入与禁用 clip 处理健全（112-114）、排序稳定降序（119）
- 类型注解完整；权重/阈值常量与文档公式一致（31-36）

---

## src/features/edit/core/render_cache_status.py (行数: 346)

### 发现

- [AUD-17614] [Major] 缓存状态条每 500ms 在 UI 线程全量重建：每 clip 全量指纹（JSON 序列化 + sha256 + os.stat）× 每 clip 线性扫描全部 manifest（含逐个 from_dict）→ 大时间线周期性主线程开销
  - 位置: src/features/edit/core/render_cache_status.py:301-307（全 clip 遍历）、157（每 clip 指纹）、130-133（manifest 全量反向扫描）；触发频率证据 src/features/edit/ui/timeline_view.py:611-613、1544-1548
  - 证据:
    ```python
    # render_cache_status.py：每个 clip 都构建完整语义指纹
        current_fingerprint = build_render_cache_fingerprint(project, timeline_id, clip)
    ```
    ```python
    # render_cache_status.py：motion-graphic 共享查找逐个转换并比较全部 manifest
        manifests = getattr(project, "render_cache_manifests", {}) or {}
        for raw in reversed(tuple(manifests.values())):
            candidate = raw
            if isinstance(candidate, dict):
                candidate = RenderCacheManifest.from_dict(candidate)
    ```
    ```python
    # timeline_view.py:611-613（调用方，已实读核实）：500ms 周期定时器
        self._cache_indicator_timer = QTimer(self)
        self._cache_indicator_timer.setInterval(500)
        self._cache_indicator_timer.timeout.connect(self._update_cache_indicator)
    ```
  - 影响: `build_cache_indicator_segments` 对每条轨道每个 clip 调 `cache_status_for_clip`：需渲染的 clip 每次都要 `_fingerprint_value(clip)`（整 clip to_dict + graph + color stacks 的 JSON dumps + sha256，render_cache.py:176-203）、`_asset_version` 的 `os.stat`（render_cache.py:120）、`get_render_cache_for_clip` 对全部 manifest 的反向线性扫描（project.py:2946-2951，每个 dict 都 `from_dict` 重建）；motion-graphic 无命中时再叠加 `_shared_motion_graphic_manifest` 的 O(N) 扫描。复杂度 O(clips × manifests)，每 500ms 在 UI 线程执行一次；数百 clip + 数百 manifest 的工程上单次可达数十毫秒级，持续占用主线程，与 4K 实时基准冲突。
  - 建议: (1) `cache_status_for_clip` 结果按 (timeline_id, clip_id, 依赖 revision/指纹前缀) 缓存，仅在 revision 变化时重建；(2) `get_render_cache_for_clip` 一次预扫描构建 (timeline_id, clip_id) 索引供整轮复用，避免每 clip 重扫；(3) 状态条刷新改为脏标记驱动（编辑事件触发）而非 500ms 轮询，或轮询时仅对可视时间窗内的 clip 求值。
  - 置信度: 高（机制与 500ms 频率已核实；量级随工程规模增长）

### 通过项
- 线程安全：模型只读聚合函数，无共享可变状态；Qt 绘制留在 UI 层（模块自述且核实）
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不适用（无 GL 对象、无着色器、无 Qt 信号）
- 状态提交一致性：composite 分支校验时间线输出契约（分辨率/帧率，272-291，P2-17）、motion-graphic 分支校验录制时长/场景/编码/容器/预览尺寸/帧率（195-254），防止旧录制误报绿
- 异常路径：`_motion_graphic_range_cached` 捕获 ImportError/RuntimeError/TypeError/ValueError 回退 False（124-125）；getter 新签名 TypeError 回退旧两参签名（170-175）兼容测试壳
- 算法正确性：指示条事件扫描 + 相邻同状态段合并为 O(n log n)，半开区间语义正确（300-336）
- 鲁棒性：`_fps_token` 对 Fraction/str/float 三形态归一化（含 limit_denominator），None/非法输入返回 "" 不抛
- 类型注解：完整

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-17600 |
| Major | 3 | AUD-17601, AUD-17602, AUD-17614 |
| Suggestion | 9 | AUD-17603–17607, AUD-17610–17613 |

- Critical：AUD-17600 磁盘耗尽安全终止路径持锁调用 `_finalize_take` → 非重入锁同线程死锁，应用挂死。
- Major：AUD-17601 take 落盘在 UI 线程同步执行（长 take 卡死界面）；AUD-17602 溢写预检失败丢块且时长虚高；AUD-17614 缓存状态条 500ms UI 线程全量指纹+线性 manifest 扫描。
