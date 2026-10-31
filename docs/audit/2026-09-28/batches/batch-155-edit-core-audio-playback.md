# Batch-155 静态审查报告 — Edit Core / audio_playback.py

- 审查对象: `src/features/edit/core/audio_playback.py`（4,855 行，全文通读）
- 审查日期: 2026-09-28
- 重点维度: 1 线程安全 / 6 异常与取消路径 / 8 性能模式（其余维度逐条过）
- 结论统计: Critical 0 / Major 9 / Suggestion 9

## src/features/edit/core/audio_playback.py (行数: 4855)

### 发现

- [AUD-15501] [Major] 全局 `_av_resampler_cache` 无界增长，且以 `id(self)` 作键存在复用污染
  - 位置: src/features/edit/core/audio_playback.py:143-163, 862, 1064-1075
  - 证据:
    ```python
    143  _av_resampler_cache: Dict[Tuple[int, int, int, int], Any] = {}
    ...
    152      key = (source_rate, target_rate, channels, instance_id)
    153      if key not in _av_resampler_cache:
    ...
    862          self._instance_id: int = id(self) & 0xFFFFFFFF
    ...
    1064      def close(self) -> None:
    1065          """关闭解码器。"""
    1066          if self._container:
    1067              self._container.close()
    ```
  - 影响: (1) `AudioDecoder.close()` 不清理 `_av_resampler_cache`，长时间剪辑会话反复 add/remove clip 使缓存条目只增不减（每个残留 AudioResampler 持有 FIR 历史）；(2) CPython `id()` 在对象释放后可复用，新解码器分配到同地址时拿到旧实例的残留滤波状态，clip 开头混入上一个 clip 的滤波尾，产生可闻杂音。
  - 建议: `close()` 中按 `_instance_id` 前缀清理缓存条目，或改用单调递增计数器（`itertools.count`）作 instance_id；对缓存设置容量上限。
  - 置信度: 中（泄漏确定；id 复用触发依赖分配模式，同尺寸对象顺序分配时概率不低）

- [AUD-15504] [Major] `AudioDecoder.open()` 失败路径不关闭容器，`is_open` 语义被破坏
  - 位置: src/features/edit/core/audio_playback.py:1027-1032, 1058-1062
  - 证据:
    ```python
    1027              audio_streams = [s for s in self._container.streams if s.type == 'audio']
    1028              if not audio_streams:
    1029                  logger.warning(f"No audio stream in {path}")
    1030                  return False
    1031
    1032              self._audio_stream = audio_streams[0]  # type: ignore
    ...
    1058          except Exception as e:
    1059              logger.error(f"Failed to open audio file: {path}, error: {e}")
    1060              self._container = None
    1061              self._audio_stream = None
    1062              return False
    ```
  - 影响: "无音频流"分支 return False 时 `self._container` 仍为非 None（文件句柄只靠 GC 释放，时机不确定），且 `is_open` 属性此后错误地返回 True；except 分支同样直接置 None 而不 `close()`，打开/关闭不对称。
  - 建议: 两个失败路径统一先 `self._container.close()`（try/except 包裹）再置 None，保证 `open() == False` ⟹ `is_open == False` 且句柄即时释放。
  - 置信度: 高

- [AUD-15508] [Major] 音频输出启动失败或设备中途丢失后，播放状态残留为 playing，无恢复也无通知
  - 位置: src/features/edit/core/audio_playback.py:3041-3043, 2716-2721, 3084
  - 证据:
    ```python
    3041              if not self._output.start():
    3042                  playback_trace("audio_thread.error", reason="output_start_failed")
    3043                  self._running = False
    ...
    2716          except RuntimeError as e:
    2717              self._is_playing = False
    2718              self._io_device = None
    ...
    3084                  if self._clock and self._mixer and self._output and self._output.is_playing:
    3085                      self._play_cycle()
    ```
  - 影响: `AudioPlaybackManager.play()` 已 emit `state_changed("playing")`（4385 行），此后 sink 启动失败或运行中设备拔出（write/bytes_free 捕获 RuntimeError 后置 `_is_playing=False`）只会让线程静默退出/空转：时钟停走、frame_sync 不再发射、视频冻结，UI 仍显示"播放中"，没有 state_changed/end_reached/错误信号，也没有设备恢复（QMediaDevices 无监听）。
  - 建议: 线程退出前或 write 失败时向 manager 发一个内部错误信号，由 manager 统一走 stop() + state_changed，并考虑监听 `QMediaDevices.audioOutputsChanged` 做设备热插拔重建。
  - 置信度: 高

- [AUD-15509] [Major] 设备格式回退后只同步时钟/帧率，解码器输出采样率与混音器采样率未同步，音频变调变速
  - 位置: src/features/edit/core/audio_playback.py:3046-3065
  - 证据:
    ```python
    3047                  device_rate = self._output._sample_rate
    3048                  device_channels = self._output._channels
    ...
    3054                      self._sample_rate = device_rate
    3055                      self._channels = device_channels
    3056                      if self._clock:
    3057                          self._clock.set_sample_rate(device_rate)
    3058                      self._samples_per_frame_exact = device_rate / self._fps
    ```
  - 影响: 解码器在 `add_track` 时以 `output_sample_rate=self._sample_rate`（如 48000）打开并重采样到 48k，`AudioMixer._sample_rate`（EQ/FX 系数）也仍是 48000；回退到 44.1k 设备后，48k 内容按 44.1k 消费 → 全部音频 ~8.6% 减速降调，EQ 中心频率整体偏移。若回退为单声道，`_prefill_ring_to` 向固定 2 声道的 ring 写单声道数据会持续失败（被 `_play_cycle` 的 except 吞掉），永久走直混兜底。
  - 建议: 回退时同步调用 `self._mixer.set_sample_rate(device_rate)`，并让所有解码器按新 `_output_sample_rate` 重开/重采样；ring 声道数与 `_channels` 对齐。
  - 置信度: 中（触发依赖 `isFormatSupported` 回退路径，代码逻辑本身确定不一致）

- [AUD-15510] [Major] 直混兜底路径的 `mix_samples` 未被 try 包裹，异常会杀死整个播放线程
  - 位置: src/features/edit/core/audio_playback.py:3260-3266, 3084-3085, 3214-3217
  - 证据:
    ```python
    3260              # ring 水位不足（underrun 边界）：直接 mix 兜底，避免静音断音。
    3261              samples, _direct_per_track = self._mixer.mix_samples(
    3262                  current_frame,
    3263                  self._fps,
    3264                  chunk_samples,
    3265                  self._output._channels,
    3266	          )
    ...
    3214	        try:
    3215	            self._prefill_ring()
    3216	        except Exception as e:
    3217	            logger.debug(f"_prefill_ring failed: {e}")
    ```
  - 影响: `_prefill_ring` 有 try/except 保护，但同一 `_play_cycle` 里 ring 欠载时的 direct-mix 调用没有；`run()` 的 `while self._running` 循环也没有外层 try。`mix_samples` 内任何未预期异常（如 `topological_sort_buses:329` 的 `float(send_level)` 对畸形 sends 抛 TypeError、DSP 插件抛错）会直接冲出 `run()`，播放线程终止且状态残留为 playing（与 AUD-15508 同类后果但触发源不同）。
  - 建议: `run()` 循环体包 try/except 记录并继续（跳过本 chunk），或至少给 direct-mix 分支与 prefill 相同的保护。
  - 置信度: 中

- [AUD-15512] [Major] 写入失败时 ring 中已读出的样本被直接丢弃，造成音频跳变
  - 位置: src/features/edit/core/audio_playback.py:3257, 3289-3311
  - 证据:
    ```python
    3257	            samples = self._ring.read(chunk_samples)
    ...
    3289	        written = self._output.write_samples(samples)
    ...
    3298	        elif written == 0:
    3299	            self._sample_chunk_fraction = chunk_fraction_before
    3300	            if not self._write_blocked_logged:
    ...
    3310	            self.msleep(5)
    3311	            return False
    ```
  - 影响: `write_samples` 返回 0（设备瞬断、缓冲意外满）时只回滚了 `_sample_chunk_fraction`，但样本已从 ring 消费且部分写入的尾差也不回补——这块音频永久丢失，出现一次可闻的跳变/卡顿；ring 欠载兜底分支同样先 `ring.clear()` 再写，失败即丢整块。
  - 建议: 写失败时把 `samples` 回写 ring 头部（`push_front` 类接口），或先 peek/write 成功后再 commit 读游标；`write_samples` 内部对部分写入的字节数做余量补写。
  - 置信度: 中（bytes_free 预检降低了触发概率，但部分写入/设备瞬断路径存在）

- [AUD-15513] [Major] scrub 预览 worker 与播放启动存在竞态窗口，可在播放中扰动解码器位置并复位 DSP
  - 位置: src/features/edit/core/audio_playback.py:3625-3634, 4232, 4269, 4370
  - 证据:
    ```python
    4232          if self._timeline_duration == 0 or self._clock.is_playing:
    4233              return False
    ...
    3628	                self._mixer.reset_dsp_states()
    3629	                samples, _scrub_per_track = self._mixer.mix_samples(
    3630	                    frame,
    ...
    3648	            if int(generation) != int(latest_generation):
    ...
    3656	                continue
    ```
  - 影响: `preview_scrub_audio` 只在入队时检查 `is_playing`，`play()` 也不递增 `_scrub_preview_generation`、不停止 worker。暂停期发出的预览任务在用户按下 Play 后仍会通过 generation 校验（generation 未变）并执行：`reset_dsp_states()` 清掉正在播放的混响尾/包络，且把各解码器 seek 到 scrub 帧——播放线程下一个 chunk 观察到帧错位再次 seek 回来，产生瞬时爆音/错位。worker 侧仅在 emit 之后由 `_on_scrub_preview_samples_ready:4269` 丢弃输出，破坏已经发生。
  - 建议: worker 在 mix 前增加时钟态检查（回调到 manager 或共享原子标志），或 `play()` 时递增 generation / 排空 worker 队列。
  - 置信度: 中（窗口小但真实存在，用户快速"刮擦后立即播放"可命中）

- [AUD-15515] [Major] `resume()` 无状态守卫，停止态调用会误发 `state_changed("playing")`
  - 位置: src/features/edit/core/audio_playback.py:4416-4428
  - 证据:
    ```python
    4416      def resume(self) -> None:
    4420	        # 恢复时钟
    4421	        self._clock.resume()
    4422
    4423	        # 恢复播放线程
    4424	        if self._playback_thread:
    4425	            self._playback_thread.resume()
    4426
    4427	        # 发射状态变化
    4428	        self.state_changed.emit(AudioPlaybackState.PLAYING.value)
    ```
  - 影响: `AudioClock.resume()` 对非 PAUSED 态是 no-op（735-737 行），但 manager 无条件 emit PLAYING。stop 之后或从未播放时调用 `resume()`，UI 显示"播放中"而实际无声、时钟不走，状态机与实际脱节（对照 `pause()` 在 4400 行有 `if not self._clock.is_playing: return` 守卫）。
  - 建议: 与 `pause()` 对称，先检查 `get_state()` 为 PAUSED 才继续，否则直接 return。
  - 置信度: 高

- [AUD-15517] [Major] `get_progress()` 在 duration==1 时除以零
  - 位置: src/features/edit/core/audio_playback.py:4650-4660
  - 证据:
    ```python
    4656	        if self._timeline_duration <= 0:
    4657	            return 0.0
    4658	        current_frame = self.get_current_frame()
    4659	        return current_frame / (self._timeline_duration - 1)
    ```
  - 影响: 守卫只挡了 `<= 0`，`_timeline_duration == 1`（单帧工程/单帧素材时间线）时分母为 0，UI 线程调用进度条更新直接抛 ZeroDivisionError。
  - 建议: `denominator = max(1, self._timeline_duration - 1)`，或 duration==1 时返回 0.0/1.0 特判。
  - 置信度: 高

- [AUD-15502] [Suggestion] 类型注解缺失/裸泛型（维度 9）
  - 位置: src/features/edit/core/audio_playback.py:287-290, 619, 937-938
  - 证据:
    ```python
    287  def topological_sort_buses(
    288      bus_ids: list,
    289      routing: dict,
    290  ) -> tuple[list, list]:
    ...
    619      def __iter__(self):
    ...
    937      @property
    938      def source_layout(self):
    ```
  - 影响: `bus_ids: list`/`routing: dict` 丢失元素类型；`TrackLevel.__iter__`、`AudioDecoder.source_layout`（应标 `-> Optional["ChannelLayout"]`）无返回注解，与模块 docstring "完整类型注解" 的自述不符。
  - 建议: 补 `List[str]`/`Dict[str, Any]` 与返回类型注解。
  - 置信度: 高

- [AUD-15503] [Suggestion] 多处宽泛/静默 `except`，其中 `ring.clear()` 失败被吞会掩盖重复播放旧样本的缺陷
  - 位置: src/features/edit/core/audio_playback.py:949-956, 1116-1117, 1788-1791, 2325-2327, 3166-3169, 3273-3275
  - 证据:
    ```python
    951              except (ValueError, Exception):
    952	                pass
    ...
    3166	        try:
    3167	            self._ring.clear()
    3168	        except Exception:
    3169	            pass
    ...
    2325	                        except Exception:
    2326	                            pass
    ```
  - 影响: `.clinerules` 禁止宽泛 except。`(ValueError, Exception)` 冗余等价于裸 Exception；LUFS 计量、meter reset、seek 期 resampler flush、以及 seek/欠载路径的 `ring.clear()` 全部静默吞错——尤其 ring.clear 若持续失败，旧样本残留会导致"seek 后重复播放旧段落"，而这正是该调用要防的 bug，却无任何日志。
  - 建议: 统一改为捕获具体异常并 `logger.warning/debug` 记录；`(ValueError, Exception)` 去掉冗余项。
  - 置信度: 高

- [AUD-15505] [Suggestion] mixer 锁内执行 `decoder.close()`（文件 IO），音频线程可能被 UI 操作拖住
  - 位置: src/features/edit/core/audio_playback.py:1510-1516, 1539-1543
  - 证据:
    ```python
    1510	        with QMutexLocker(self._mutex):
    1511	            if replace_existing or track_id not in self._decoders:
    1512	                old_decoders = self._decoders.get(track_id)
    1513	                if old_decoders is not None and replace_existing:
    1514	                    for old_decoder in self._iter_decoders(old_decoders):
    1515	                        old_decoder.close()
    ```
  - 影响: `add_track(replace_existing=True)`/`remove_track` 在持有 mixer `_mutex` 时关闭 PyAV 容器（可能 flush 缓冲、慢存储/网络盘下耗时不可控）；播放线程的 `mix_samples`（整个解码+混音周期都在同一把锁内）会被 UI 线程这次关闭操作阻塞，靠 200ms ring 水位吸收，磁盘慢时有 underrun 风险。
  - 建议: 锁内只做字典摘除，close 移到锁外执行。
  - 置信度: 中

- [AUD-15506] [Suggestion] mix 热路径存在逐块 import、逐块 solo 闭包重算与 ramp 分配（维度 8）
  - 位置: src/features/edit/core/audio_playback.py:2013-2019, 2083, 2128, 2151, 2208, 2412
  - 证据:
    ```python
    2014	            if has_solo:
    2015	                solo_ids = [tid for tid, st in self._tracks.items() if st.solo]
    2016	                all_track_ids = list(self._tracks.keys())
    2017	                audible_solo = compute_audible_solo_closure(
    2018	                    all_track_ids, self._track_routing, solo_ids,
    2019	                )
    ...
    2128	                    from src.features.edit.core.channel_layout import adapt_channels
    ...
    2208	                    from src.features.edit.core.audio_dsp import pan_gains_for_channels
    ```
  - 影响: 每个 ~40ms 混音块都要：重算 solo 可听闭包（图遍历+集合分配）、执行 5 处函数内 `from X import Y`（sys.modules 命中但仍有查找/调用开销）、为 automation ramp 新建多个 `np.array`。solo/routing 只在 set_* 时变化，闭包完全可以缓存失效式计算。
  - 建议: solo 闭包在 `set_track_solo`/`set_track_routing`/add/remove 时预计算缓存；模块级导入 adapt_channels/pan_gains_for_channels/balance_gains/AudioGraphRenderer 等热路径依赖。
  - 置信度: 高

- [AUD-15507] [Suggestion] AudioOutput 死代码：`_buffer`/`_buffer_mutex` 从未用于读写，`buffer_low`/`sync_error` 信号从未发射
  - 位置: src/features/edit/core/audio_playback.py:2528-2529, 2562-2564, 2632, 2676, 2861
  - 证据:
    ```python
    2528      samples_played = pyqtSignal(int)
    2529      buffer_low = pyqtSignal()
    ...
    2562	        # 缓冲区
    2563	        self._buffer: bytearray = bytearray()
    2564	        self._buffer_mutex: QMutex = QMutex()
    ...
    2861	      sync_error = pyqtSignal(float)
    ```
  - 影响: ring buffer 方案落地后 `_buffer` 只剩 `clear()` 调用（2632/2667/2676 行），`_buffer_mutex` 无人使用；`buffer_low`（AudioOutput）与 `sync_error`（AudioPlaybackThread，已在 4190 行连接）从不 emit，连接方永远收不到，误导维护者以为有欠载告警通道。
  - 建议: 删除死字段与死信号，或补上真实的欠载 emit（与 ring_stats 呼应）。
  - 置信度: 高

- [AUD-15511] [Suggestion] 反压分支的 `msleep(5)` 位于"仅首次"日志守卫内，后续阻塞周期不再睡眠
  - 位置: src/features/edit/core/audio_playback.py:3230-3241, 3298-3311
  - 证据:
    ```python
    3230	        if bytes_free < chunk_bytes:
    3231	            self._sample_chunk_fraction = chunk_fraction_before
    3232	            if not self._write_blocked_logged:
    3233	                self._write_blocked_logged = True
    3234	                playback_trace(
    ...
    3240	                self.msleep(5)
    3241	            return False
    ```
  - 影响: 首次阻塞记录日志并 sleep 5ms；`_write_blocked_logged` 置 True 后的连续阻塞轮次不再 sleep。当前靠主循环 `queue.get(timeout=0.01)` 提供了 ~10ms 节奏，实际空转有限，但代码意图（每次反压都退避）与实现不符，若日后循环结构变化会变成忙等烧 CPU。
  - 建议: 把 `self.msleep(5)` 移出 `if not self._write_blocked_logged`，只保留日志去重。
  - 置信度: 高

- [AUD-15514] [Suggestion] `stop()`/`quiesce_for_timeline_rebuild()` 从 UI 线程无界 `wait()` 音频线程
  - 位置: src/features/edit/core/audio_playback.py:4440-4443, 4018-4024
  - 证据:
    ```python
    4440	        if self._playback_thread:
    4441	            self._playback_thread.stop()
    4442	            self._playback_thread.wait()
    4443	            self._playback_thread = None
    ```
  - 影响: `stop()` 的 `wait()` 无超时。线程正在 `mix_samples` 内做 PyAV demux（慢盘/网络素材单块解码可超过百毫秒甚至秒级）时，UI 线程被卡住直到解码返回；`_on_frame_sync` 播放到终点触发的 `stop()` 也会在 UI 线程同步等待。`quiesce_for_timeline_rebuild` 至少有 1s 超时+告警的两段式。
  - 建议: 与 quiesce 一致采用有界 wait + 告警 + 延迟清理的策略。
  - 置信度: 中

- [AUD-15516] [Suggestion] `get_state()` 把"暂停在帧 0"误报为 STOPPED
  - 位置: src/features/edit/core/audio_playback.py:4643-4648
  - 证据:
    ```python
    4643	        if self._clock.is_playing:
    4644	            return AudioPlaybackState.PLAYING
    4645	        elif self._clock.get_state().elapsed_at_pause > 0:
    4646	            return AudioPlaybackState.PAUSED
    4647	        else:
    4648	            return AudioPlaybackState.STOPPED
    ```
  - 影响: `AudioClock.seek(0, fps)` 会把 `elapsed_at_pause` 置 0.0（759-760 行）；seek 回 0 后暂停的会话被判为 STOPPED，UI 状态图标/按钮态错误（应显示 PAUSED）。
  - 建议: 用 `AudioClockState.state` 字段而非 `elapsed_at_pause > 0` 推断 PAUSED。
  - 置信度: 高

- [AUD-15518] [Suggestion] `AudioVideoSynchronizer.set_sync_mode()` 重复调用 `setup()` 造成信号重复连接
  - 位置: src/features/edit/core/audio_playback.py:4760-4785
  - 证据:
    ```python
    4767	        if self._sync_mode == AudioSyncMode.AUDIO_MASTER:
    4768	            self._audio.frame_changed.connect(self._sync_video_to_audio)
    ...
    4777	    def set_sync_mode(self, mode: AudioSyncMode) -> None:
    ...
    4784	        self._sync_mode = mode
    4785	        self.setup()
    ```
  - 影响: `setup()` 每次 connect 都追加，从不 disconnect。同模式重复 set_sync_mode 后每帧 frame_changed 触发两次 `_sync_video_to_audio`（重复 video.seek）；切换模式时旧连接保留（槽内有模式守卫早退，仅浪费）。另注意 manager 侧 `set_video_controller`（4858 附近无）在 3857-3858 行也连接了 `controller.frame_changed → _on_video_frame_changed`，与 synchronizer 的 VIDEO_MASTER 直连 `video.frame_changed → audio.seek`（4773 行）叠加时同一帧触发两次 seek。
  - 建议: setup 前先 disconnect 旧连接，或用 `Qt.ConnectionType.UniqueConnection` 语义/保存连接句柄。
  - 置信度: 高

### 通过项

- 线程安全（AudioClock）：所有读写均在 QMutex 内，`get_state` 返回副本，跨线程一致。
- 线程安全（解码器状态）：`_last_decoded_frame`/`_sample_remainder`/`_fractional_offset`/demux 游标只在 mixer 锁内的 `mix_samples`→`decode_samples_for_frame` 路径变更，无锁外竞争。
- 线程安全（命令队列）：play/pause/seek/preroll 全部经线程安全 `queue.Queue` 串行到音频线程执行，seek/preroll 有 coalesce 且 FIFO 保序；UI 线程不从外部 seek 解码器（4477-4480 注释明确）。
- QAudioSink 线程亲和：播放路径 AudioOutput 在播放线程内创建/使用/销毁；scrub 输出在 manager（UI）线程创建并通过排队信号在同线程写，无跨线程设备调用。
- underrun 恢复：ring 水位不足时 direct-mix 兜底并清 ring 对齐游标，不会静音卡死。
- 数值正确性（采样格式）：int16/int32/int8/uint8/float64→float32 归一化路径齐全；输出前 `np.clip(-1,1)` 防 wrap；Int16/Int32/UInt8 编码换算正确。
- 数值正确性（重采样边界）：空数组、target_count<=0、非 C-contiguous 输入（`np.ascontiguousarray`）均有防护；flush 后实例从缓存移除避免 AVERROR_EOF 慢路径（220-226 行有明确注释与修复）。
- 数值正确性（声道映射）：优先容器真实布局（RMA-09，`source_layout`），失败回退安全截断/广播，与导出侧一致。
- 爆音防护：seek 后 `_pending_fade_in` + 64 样本线性 fade（ring 路径 apply_fade_in、直混路径 linspace），mute/自动化切换有 `_apply_declick_edges`。
- 信号防递归/节流：frame_sync 只在帧变化时 emit；电平信号 master+per-track 共用 30ms 节流时间戳，无每帧洪泛；`_on_frame_sync`/`_sync_video_to_audio` 有模式守卫，AUDIO_MASTER 下无 seek 回环。
- 状态一致性（play/pause）：`pause()` 有 is_playing 守卫；preroll→play 复用已 warm 的 sink；`play()` 区分 prepared/seek 两条路径；clock 与线程游标在 `_seek_audio_thread_position` 中成组复位。
- 取消/清理路径：`cleanup()` 先 stop 播放线程再 join scrub worker（有界 3s + 协作式无限等待并注释了 macOS QThread 析构 abort 风险）；`quiesce_for_timeline_rebuild` 在改图前先停线程，防 decoder close 与输出写竞争。
- 设备异常（写路径）：`write_samples`/`bytes_free`/`suspend` 捕获 RuntimeError 并置空设备引用，不崩溃（残留状态问题见 AUD-15508）。
- 输入验证：`set_sample_rate`/`set_timeline_fps`/`set_timeline_duration`/`AudioTrackState.__post_init__`/`set_speed`/`set_gain_db`/`set_fade` 均有范围校验；open 时 stream.rate/channels 有 fallback；duration 双路径（stream.time_base / 容器 μs）换算正确。
- 性能（混音零分配）：`_mix_scratch` 复用 acc/pad/bus 三类 scratch 缓冲，A3 懒分配、A2 `np.dot` 算 RMS、W11-gate LUFS 按需计算均已落实。
- 资源生命周期（mixer 缓存）：remove_track/unregister_bus_track 对 EQ/FX/clip 链/LUFS/automation/routing/transition/scratch 清理对称。
- Bus DAG：Kahn 拓扑排序 + 环检测，环内 Bus 排除并告警不崩溃；solo 可听闭包语义（RMA-04）正确处理普通轨/Bus 双向闭包。
