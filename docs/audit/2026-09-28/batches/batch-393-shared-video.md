# 批次报告：batch-393 [P2] src/shared/video//

- 编号区间：AUD-39300–AUD-39317
- 审查文件（4 个，2,739 行）：pipeline_decision.py / pixel_copy_counters.py / playback_clock.py / playback_frame_trace.py
- 统计：Critical 1 / Major 3 / Suggestion 13

---

## src/shared/video/pipeline_decision.py (行数: 622)

### 发现

- [AUD-39300] [Suggestion] fallback_to_v2_or_fail 的 fatal+allow_fallback 分支消息仍写 "Fallback to v2 allowed"，与 should_fallback 恒为 False 矛盾
  - 位置: src/shared/video/pipeline_decision.py:513-517
  - 证据:
    ```python
    should_fallback = False
    ...
    else:
        message = (
            f"Fallback to v2 allowed "
            f"(reason={reason.name}, is_fatal={is_fatal})"
        )
    ```
  - 影响: v2 已移除、决策结果永不回退，但该分支（AUTO+致命+allow_fallback）产出的日志/UI 消息宣称 "Fallback to v2 allowed"，误导排障与遥测消费方，让人以为发生了 v2 回退。
  - 建议: 消息改为 "staying v3 (v2 removed)" 一类与决策一致的说法。
  - 置信度: 高

- [AUD-39301] [Suggestion] resolve_video_pipeline_mode 在 AUTO+fatal 分支返回 reason="fallback"、fallback_reason=force_reason，__str__ 会输出 "(fallback from auto …)" 而实际并未 fallback
  - 位置: src/shared/video/pipeline_decision.py:394-401, 162-167
  - 证据:
    ```python
    return VideoPipelineDecision(
        requested_mode=requested_mode,
        effective_mode=VideoPipelineEffectiveMode.V3,
        allow_fallback=True,
        reason="fallback",
        fallback_reason=force_reason,
        message=f"Fatal issue kept on v3 (v2 removed, reason: {force_reason.name})",
    )
    ...
    if self.fallback_reason != VideoPipelineFallbackReason.NONE:
        return (
            f"Video pipeline: {self.effective_mode.value} "
            f"(fallback from {self.requested_mode.value}, ...)"
        )
    ```
  - 影响: effective_mode 恒为 V3 且不回退，但 `str(decision)`/`to_log_dict()` 呈现为"发生了 fallback"，日志与 UI 口径失真。
  - 建议: fatal-kept-on-v3 场景将 fallback_reason 置 NONE（或为 __str__ 增加区分"记录的 fatal 原因"与"实际回退"）。
  - 置信度: 高

- [AUD-39302] [Suggestion] should_allow_fallback_for_reason 在 AUTO+致命+allow_fallback 时返回 True（暗示可回退 v2），与 v2 移除后的现实矛盾；且生产代码无调用者
  - 位置: src/shared/video/pipeline_decision.py:291-295
  - 证据:
    ```python
    # AUTO 模式：只有致命错误才 fallback
    if decision.requested_mode == VideoPipelineMode.AUTO:
        if not decision.allow_fallback:
            return False
        return is_fatal_fallback_reason(reason)
    ```
  - 影响: 该函数语义仍是"允许回退 v2"，而 fallback_to_v2_or_fail 已硬编码 should_fallback=False；两处口径不一致。全库检索仅 `src/shared/video/__init__.py` re-export，无实际调用，属遗留死接口。
  - 建议: 删除或改名为"是否记录 fatal 事件"语义，与统一入口对齐。
  - 置信度: 高

- [AUD-39303] [Suggestion] 未使用导入 dataclasses.field 与 enum.auto
  - 位置: src/shared/video/pipeline_decision.py:85-86
  - 证据:
    ```python
    from dataclasses import dataclass, field
    from enum import Enum, auto
    ```
  - 影响: 两个符号在文件内均未使用，增加阅读噪声。
  - 建议: 删除 field 与 auto 导入。
  - 置信度: 高

### 通过项
- 输入验证：video_pipeline_mode 非法值经 try/except ValueError 回落 AUTO（:335-341）
- 决策单一入口 resolve_video_pipeline_mode，requested/effective 模式分离清晰
- fallback 事件经 log_fallback 统一记录，reason 词表封闭（FATAL/NON_FATAL 集合覆盖枚举）
- v2 请求映射为 v3 并告警，不静默
- 无线程共享可变状态、无 GPU 资源、无 Qt 信号，信号防递归维度不适用
- 类型注解完整（公开 API 均有返回类型）

---

## src/shared/video/pixel_copy_counters.py (行数: 88)

### 发现

- [AUD-39304] [Suggestion] add_pixel_copy 对负数字节静默忽略，与 docstring "负数/未知 kind 被拒绝" 不符（未知 kind 抛 ValueError，负数直接 return）
  - 位置: src/shared/video/pixel_copy_counters.py:58-68
  - 证据:
    ```python
    def add_pixel_copy(kind: str, nbytes: int) -> None:
        """累加一次拷贝的字节数（线程安全；负数/未知 kind 被拒绝）。...
        """
        if kind not in _bytes:
            raise ValueError(f"unknown copy kind: {kind!r}")
        nbytes = int(nbytes)
        if nbytes <= 0:
            return
    ```
  - 影响: "零拷贝为 0 只能由真实计数证明（fail-closed）"的设计下，调用点误传负数（如 signed 差值下溢）会被静默吞掉，削弱审计口径；docstring 与行为漂移。
  - 建议: 负数 raise ValueError（或至少 logger.warning），nbytes==0 保持静默跳过；同步修正 docstring。
  - 置信度: 高

### 通过项
- 线程安全：所有读写都在 threading.Lock 内，snapshot 返回副本
- 快照键形状固定（COPY_KINDS 常量），消费方无需判空
- reset 语义明确（benchmark 会话前清零）
- 类型注解完整；无 GPU 资源、无信号、无状态提交维度

---

## src/shared/video/playback_clock.py (行数: 1439)

### 发现

- [AUD-39305] [Critical] 使用 os.path.isfile 但未 import os——磁盘代理 READY 路径抛 NameError，导致 SourceManager.open() 整体失败
  - 位置: src/shared/video/playback_clock.py:365
  - 证据:
    ```python
    if self._disk_proxy_info is not None and self._disk_proxy_info.is_ready:
        proxy_path = self._disk_proxy_info.proxy_path
        if proxy_path and os.path.isfile(proxy_path):
    ```
    文件头部导入仅有：
    ```python
    import logging
    import threading
    import time
    ```
  - 影响: 一旦传入 is_ready 的 disk_proxy_info 且 proxy_path 非空，`os` 求值即 NameError，被 open() 外层 `except Exception`（:513）捕获后 close() 并返回 False——整个媒体源打开失败而非仅跳过磁盘代理（调用方 session.py:471 会直接 raise RuntimeError）。现网 src/ 暂无传 disk_proxy_info 的调用点（仅测试覆盖 MISSING 路径故未暴露），但该参数是 SourceManager 公开 API 且有 READY 语义测试，任何接线即必现。
  - 建议: 文件头部 `import os`；并补一条 READY 磁盘代理路径的回归测试。
  - 置信度: 高

- [AUD-39306] [Major] 共享源（无需代理）时 master/proxy 双游标各自记账，顺序播放免 seek 优化会读到并缓存错误的 proxy 帧
  - 位置: src/shared/video/playback_clock.py:479, 686-702, 719（对照 611, 741-742）
  - 证据:
    ```python
    # 无代理时 proxy 与 master 共享同一个 MediaSource（:479）
    self._proxy_source = self._master_source
    ...
    # get_proxy_frame 顺序分支免 seek（:686-692, :702）
    is_sequential = frame_index == self._proxy_current_frame + 1
    if is_sequential:
        seek_ms = 0.0
    else:
        active_source.seek_frame(frame_index)
    ...
    frame = active_source.read_next_frame()
    ...
    self._cache.put_proxy(frame_index, frame)
    ```
    而 get_master_frame 只更新 `_master_current_frame`（:611）、seek_master 只挪 master 游标（:741-742），均不同步 `_proxy_current_frame`。
  - 影响: `_proxy_current_frame == 5` 时先 get_master_frame(20)（或 seek_master(30)），共享解码器位置被挪走；随后 get_proxy_frame(6) 判定为"顺序下一帧"免 seek，read_next_frame 实际返回第 21 帧，却被当作第 6 帧放入 proxy 缓存并上屏——预览错帧 + 缓存污染。master 方向因 read_frame 内部自校验游标（ffmpeg_source.py:1200 / topos_source.py:967 会自动补 seek）而幸免，proxy 的 read_next_frame 路径无任何保护。
  - 建议: 共享源场景（`_proxy_source is _master_source`）下让 get_master_frame/seek_master 同步维护 `_proxy_current_frame`（或反之），或对共享源禁用顺序免 seek 优化。
  - 置信度: 高（逻辑必然；触发需 master 与 proxy 读交错，暂停→scrub 即可满足）

- [AUD-39307] [Major] FrameScheduler 未适配 V-P0-09"未知时长"：total_frames=0 时预读被整体禁用
  - 位置: src/shared/video/playback_clock.py:1364, 1404（对照 1021-1023, 1040）
  - 证据:
    ```python
    end = min(current + self._prefetch_lookahead, self._clock.total_frames - 1)
    ...
    if next_frame >= self._clock.total_frames:
        return None
    ```
    PlaybackClock 将 0/None 规范化为 `_total_frames=None`（未知、不设上界），但 `total_frames` property 对外仍返回 0（:1040）。
  - 影响: 时长未知的素材（容器无 nb_frames）经 `total_frames or 0` 构造（session.py:473-474、video_playback.py:2838-2840）后，`schedule_next_frame` 因 `next_frame >= 0` 恒真永远返回 None，`get_prefetch_range` end=-1，预读调度完全失效，与 PlaybackClock 里"未知时长不做上界 clamp"的设计意图相悖。
  - 建议: FrameScheduler 内部区分 `clock._total_frames is None`（未知→按 lookahead 封顶）与已知上界；或 property 暴露 Optional 并分别处理。
  - 置信度: 高（代码逻辑确定；实际触发依赖存在时长未知的素材）

- [AUD-39308] [Suggestion] should_drop_frame/get_lag 仍用旧的全量 `clock_time × speed` 公式，与 V-P0-02 速率锚点公式不一致
  - 位置: src/shared/video/playback_clock.py:1266, 1277（对照 tick 的锚点公式 :1185-1189）
  - 证据:
    ```python
    def should_drop_frame(self) -> bool:
        ideal_frame = int(self._state.clock_time * self._fps * self._state.speed)
        lag = ideal_frame - self._state.current_frame
        return lag >= self._drop_threshold
    ```
  - 影响: tick() 已改为锚点公式（正是为了修"把新 speed 乘到旧 speed 累计的 clock_time 上"的跳变缺陷），这两个方法保留旧公式：中段改速或 seek 后 lag/丢帧判断与 tick 实际调度失真。当前全库无外部调用者，属潜伏不一致。
  - 建议: 复用 `anchor_frame + (clock_time - anchor_clock_time) * fps * speed` 公式，或直接删除这两个无调用者的方法。
  - 置信度: 高

- [AUD-39309] [Suggestion] seek_proxy 只 seek `_proxy_source`，不处理磁盘代理激活（`_proxy_source is None`）的场景；且全库无调用者
  - 位置: src/shared/video/playback_clock.py:748-768（对照 :673 `active_source = self._disk_proxy_source or self._proxy_source`）
  - 证据:
    ```python
    def seek_proxy(self, frame_index: int) -> bool:
        if not self._is_open or self._proxy_source is None:
            return False
        with self._decode_lock:
            try:
                self._proxy_source.seek_frame(frame_index)
    ```
  - 影响: 磁盘代理就绪时 `_proxy_source` 为 None（open() 跳过内存代理创建），seek_proxy 恒返回 False 且不 seek 真正生效的 `_disk_proxy_source`。当前无调用者，属遗留死接口，一旦被调用即语义错误。
  - 建议: 改为 seek `self._disk_proxy_source or self._proxy_source`，或删除。
  - 置信度: 高

- [AUD-39310] [Suggestion] `hasattr(frame, 'proxy_resolution')` 恒为 False——DecodedFrame 无该字段，磁盘代理分辨率标注永不生效（死代码）
  - 位置: src/shared/video/playback_clock.py:714-715
  - 证据:
    ```python
    if is_from_disk_proxy and self._disk_proxy_info is not None and hasattr(frame, 'proxy_resolution'):
        object.__setattr__(frame, 'proxy_resolution', self._disk_proxy_info.proxy_resolution)
    ```
  - 影响: src/shared/media/frame.py 的 DecodedFrame 未定义 `proxy_resolution` 字段，且全库对该属性的写入都在同样的 hasattr 守卫之后（frame_provider_service.py:2133-2135 同款）——属性永远不存在，setattr 永不执行，下游拿不到磁盘代理分辨率信息。
  - 建议: 去掉 hasattr 前置条件直接 setattr（或写入 frame.extra 字典）。
  - 置信度: 高

- [AUD-39311] [Suggestion] fps=0 时 start()/seek_to_frame()/get_time_for_frame() 除零崩溃（构造器只保护了 _frame_duration）
  - 位置: src/shared/video/playback_clock.py:1025, 1082, 1130, 1255
  - 证据:
    ```python
    self._frame_duration: float = 1.0 / fps if fps > 0 else 0.04
    ...
    self._state.clock_time = self._state.current_frame / self._fps   # start()
    self._state.clock_time = frame / self._fps                       # seek_to_frame()
    return frame / self._fps                                         # get_time_for_frame()
    ```
  - 影响: video_playback.py:2835 构造前有 `if fps:` 防护，但 session.py:471-477 直接用 `self._metadata.fps.to_fps()` 构造，元数据异常（fps=0）时 ZeroDivisionError 使会话创建崩溃。
  - 建议: 构造器对 fps<=0 直接 raise ValueError，或四处除法统一走受保护的 `_frame_duration`。
  - 置信度: 高（逻辑）；触发依赖异常元数据

- [AUD-39312] [Suggestion] tick(dt) 显式传入 dt 时仍写 `last_frame_time = time.time()`，dt 与 wall-clock 时间源混用
  - 位置: src/shared/video/playback_clock.py:1170-1178
  - 证据:
    ```python
    if dt is None:
        current_time = time.time()
        elapsed = current_time - self._state.last_frame_time
    else:
        elapsed = dt
    self._state.clock_time += elapsed
    self._state.last_frame_time = time.time()
    ```
  - 影响: 若同一时钟交替使用 dt 模式与 None 模式，dt 模式写入的 wall-clock 时间戳会让下一次 None 模式的 elapsed 产生跳变。模块自身注释强调"避免时间源混用导致漂移"，此处未遵守。
  - 建议: 仅在 dt is None 分支更新 last_frame_time。
  - 置信度: 中（需交替调用模式才触发，当前调用方是否混用未逐一核实）

- [AUD-39313] [Suggestion] tick() 的 lag<=0 分支内 `if ideal_frame > self._state.current_frame` 恒为假（死代码）
  - 位置: src/shared/video/playback_clock.py:1195-1202
  - 证据:
    ```python
    lag = ideal_frame - self._state.current_frame
    if lag <= 0:
        # 已经超前或刚好，不跳帧
        # 可能是因为 seek 或速度变化
        if ideal_frame > self._state.current_frame:
            self._state.current_frame = ideal_frame
        return self._state.current_frame
    ```
  - 影响: lag<=0 蕴含 ideal_frame <= current_frame，内层条件永不成立，注释描述的"seek/变速后前跳"行为并不存在，误导后续维护。
  - 建议: 删除内层死分支或修正注释。
  - 置信度: 高

- [AUD-39314] [Suggestion] 未使用导入 numpy（np）与 CachePolicy
  - 位置: src/shared/video/playback_clock.py:63, 82
  - 证据:
    ```python
    import numpy as np
    ...
    from src.shared.media.cache import DualFrameCache, CachePolicy
    ```
  - 影响: 文件内无 `np.` 使用、无 CachePolicy 使用；numpy 导入还增加模块加载开销。
  - 建议: 删除两个未使用导入。
  - 置信度: 高

### 通过项
- 线程安全：源操作全部经 `_decode_lock`（RLock）串行化，close() 先取本地引用再关闭，正确处理 master/proxy 共享同一源时的二次 close（:520-537）
- 异常路径：open() 失败统一 close()+返回 False；get_master_frame/get_proxy_frame 解码异常捕获后返回 None 不扩散；close() 对各源 close 异常分别告警
- 磁盘代理打开失败/被拒时回落内存代理，`_disk_proxy_info.with_status(FAILED)` 状态推进正确（:381-402）
- 代理分辨率计算对缺失 viewport 有 VIEWPORT_THRESHOLD 兜底，避免静默全分辨率播放（:801）
- 工厂签名兼容：TypeError 逐级降级重试（target_size→path），自定义工厂路径有默认 hwaccel 状态
- P2-1 约束：get_master_frame/get_proxy_frame 入口调用 _assert_no_sync_decode_in_playback 防播放态同步解码
- 性能监控埋点（cache hit/seek/read/decode 时间）覆盖缓存命中与未命中两条路径
- PlaybackClock 速率锚点（V-P0-02）在 set_speed/seek/start 三处均重置，tick 主路径公式正确；未知时长（V-P0-09）上界处理正确
- 类型注解完整

---

## src/shared/video/playback_frame_trace.py (行数: 590)

### 发现

- [AUD-39315] [Major] is_full_gpu 的 decode 白名单与全库实际产出的 decode_path 词表不符，GPU/硬解帧被误判为非全 GPU
  - 位置: src/shared/video/playback_frame_trace.py:159
  - 证据:
    ```python
    and decode_paths <= {'macos_vt_zero_copy', 'hw_decode_planar', 'gpu_direct', 'gpu'}
    ```
    而实际写入 trace.decode_path 的值包括（timeline_preview_renderer.py:2615-2631, :1934；本模块 docstring :27）：
    ```python
    decode_paths.add('comp_gpu_direct')        # GPU 纹理句柄，零 CPU 回读
    decode_paths.add('videotoolbox_planar')    # VideoToolbox 硬解 planar
    trace_info['decode_path'] = ','.join(...)  # 还有 'composite_cache_blit'
    decode_path='macos_vt_iosurface_zero_copy' # 模块自己的示例值
    ```
  - 影响: 'comp_gpu_direct'（纯 GPU 句柄）与 'videotoolbox_planar'（硬解）都不在白名单内；白名单里的 'hw_decode_planar' 全库无产出点。结果是零拷贝/硬解路径的帧被计入 `cpu_fallback_frames`/`fallback_frames`，`gpu_frames` 低估——该摘要正是 benchmark/HUD 判定"全 GPU 占比"的 KPI，口径失真直接误导性能结论。
  - 建议: 以 frame_trace_schema 的统一词表为准维护白名单（纳入 'comp_gpu_direct'、'videotoolbox_planar'、'macos_vt_iosurface_zero_copy'，移除无产出的 'hw_decode_planar'），并加词表锁定测试。
  - 置信度: 高

- [AUD-39316] [Suggestion] get_summary 空会话返回的键形状与非空分支不一致，但注释声称"键形状一致（消费方无需判空）"
  - 位置: src/shared/video/playback_frame_trace.py:334-350（对照非空分支 :383-410）
  - 证据:
    ```python
    if not traces:
        return {
            'total_frames': 0,
            # 空会话也保持键形状一致（消费方无需判空）
            'decode_saturation': {...}, 'transition_frames': {...},
            'gpu_time': {...}, 'pixel_copy_bytes': {...},
        }
    ```
    非空分支另有 page/gpu_frames/cpu_fallback_frames/fallback_reasons/decode_avg_ms/deadline 等约 17 个键。
  - 影响: 直接按非空形状索引（如 `summary['gpu_frames']`）在空会话时 KeyError。现网调用方（timeline_panel.py:2094 用 .get、edit_benchmark_service.py:157 有 try 防护）恰好没踩中，但注释承诺的契约是假的。
  - 建议: 空会话走同一个键构造函数填默认值，兑现注释承诺。
  - 置信度: 高

- [AUD-39317] [Suggestion] export_json 中 get_summary 与 get_traces 是两次独立加锁调用，summary 与 traces 可能不是同一快照
  - 位置: src/shared/video/playback_frame_trace.py:541-549
  - 证据:
    ```python
    def export_json(self, last_n: Optional[int] = None) -> str:
        summary = self.get_summary(last_n)
        traces = self.get_traces(last_n)
    ```
  - 影响: 两次调用之间若有 record()/annotate()，导出的 summary 统计与 traces 明细不一致（计数对不上），削弱导出报告的证据链一致性。
  - 建议: 在单次持锁内取 `traces = list(...)` 后基于该列表本地计算 summary。
  - 置信度: 高（逻辑）；实际影响小（导出时机通常静止）

### 通过项
- 线程安全：FrameTraceCollector 所有状态访问均在 RLock 内；全局单例双检锁 + reset 正确（get_trace_collector/reset_trace_collector）
- GPU 时间语义（P0-03）：gpu_time_unavailable 默认 True，摘要区分 measured/unavailable，禁止用 0.0 冒充，_summarize_gpu_time 过滤 `gpu_submit_ms > 0`
- 异常路径：record/annotate 中 evaluate_deadline_miss 失败被窄捕获且不丢 trace（注释说明理由，符合"遥测 best-effort"而非掩盖领域错误）
- deadline 归因（P1-06）：幂等（已归因不覆盖），段优先级与 SEGMENT_BUDGET_RATIOS 键一致（decode/import_upload/composite 均存在），无 deadline 时返回空不凭空套预算
- annotate 的 trace_id 合并窗口（最近 128 帧）语义在 docstring 明示，extra dict 逐键合并不覆盖整字典
- to_compact_dict 省略默认值后强制补齐 frame_index/trace_id/decode_path/render_path/fallback_reason 关键字段
- 分位数索引计算有边界钳制（p95_idx max/min clamp），除零均有 `if traces`/`if measured` 防护
- 输入验证：record/annotate 对空 trace_id（旁路帧）显式返回 False
- 类型注解完整；多轨道逗号拼接 decode_path 在 is_full_gpu 中正确按段拆分判断

---

## 附注

- 编号使用：AUD-39300–AUD-39317，全部位于任务书区间 AUD-39300–AUD-39399 内。
- 交叉验证文件（只读）：src/shared/media/frame.py、ffmpeg_source.py、topos_source.py、cache.py、frame_trace_schema.py、proxy.py、session.py、video_playback.py、frame_provider_service.py、timeline_preview_renderer.py、timeline_clip_frame_provider.py、timeline_panel.py、edit_benchmark_service.py、tests/media/test_disk_proxy_integration.py。
