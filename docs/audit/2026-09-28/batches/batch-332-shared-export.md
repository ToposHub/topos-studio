# 批次报告：batch-332 [P2] src/shared/export//

编号区间：AUD-33200–AUD-33299。审查日期：2026-09-28。
共 7 个文件、2,532 行，全部逐行通读；关键契约（MediaSource Protocol、
GpuDecodedFrame/GpuFrameRelease、VideoEncoderConfig 字段单位、timeline_export
调用方）已交叉核对源码。

---

## src/shared/export/export_stage_telemetry.py (行数: 415)

### 发现

- [AUD-33200] [Suggestion] 阶段 backend 聚合取最后一个采样（last-wins），可能掩盖导出中途的后端切换
  - 位置: src/shared/export/export_stage_telemetry.py:386
  - 证据:
    ```python
    stages.append(StageTelemetry(
        name=name,
        backend=samples[-1]["backend"],
        sample_count=len(samples),
    ```
  - 影响: 同一阶段内若前段采样为 CPU 后端、末次采样为 GPU 后端（或反之），
    报告只呈现最后一次的 backend；前者可让 `stage_backend_cpu` 阻断项漏判
    （fail-open 方向），full_gpu 声明依据被弱化。
  - 建议: 聚合时收集去重后的 backend 集合，多于一个时输出
    `stage_backend_mixed:<name>` blocker（fail-closed），或按 sample 计数分桶。
  - 置信度: 中（代码事实确定；触发前提是同阶段 backend 漂移且无 fallback 标记）

- [AUD-33201] [Suggestion] full_gpu 上传门豁免方向存疑：cpu_to_gpu_upload 阶段被采样时正的 upload 字节不产生 blocker
  - 位置: src/shared/export/export_stage_telemetry.py:195-198
  - 证据:
    ```python
    upload_exempt = by_name.get("cpu_to_gpu_upload")
    if upload_exempt is None or upload_exempt.sample_count == 0:
        if self.cpu_upload_bytes > 0:
            blockers.append("cpu_upload_bytes_positive")
    ```
  - 影响: 只要管线登记过 cpu_to_gpu_upload 阶段采样，总 cpu_upload_bytes>0
    也不会阻断 full_gpu——与模块头“像素 upload/download 为 0（或该阶段显式
    豁免）”的硬规则存在张力，属潜在 gate 绕过。当前 src/ 内没有任何调用方
    记录该阶段（已 grep 验证），故为潜在而非现行缺陷。
  - 建议: 将豁免改为显式白名单/标记（如 stage 记录 native mapped-surface
    豁免位），而非“阶段存在即豁免”；或在该阶段存在且字节为正时输出独立
    blocker 词表。
  - 置信度: 中（逻辑事实高；设计意图以模块头文档为准，可能是有意为之）

- [AUD-33202] [Suggestion] set_native_encode_stage_stats 接受任意对象且无结构校验，序列化错误延迟到导出结束才暴露
  - 位置: src/shared/export/export_stage_telemetry.py:345-348
  - 证据:
    ```python
    def set_native_encode_stage_stats(self, stats: Any) -> None:
        """记录 native 编码阶段统计（T-P1-11；TOPOS 编码路径调用一次）。"""
        with self._lock:
            self._native_encode_stage_stats = stats
    ```
  - 影响: 若 stats 含非 JSON 可序列化对象（如 numpy 标量），
    `ExportStageTelemetryReport.to_json()` 在导出收尾时才抛异常；当前唯一
    调用方（timeline_export.py T-P1-11 步骤 5）已用 try/except 包裹收集，
    影响受限。
  - 建议: 收紧签名至 `Optional[Dict[str, Any]]` 并在写入时做一次
    json.dumps 冒烟校验或白名单字段校验。
  - 置信度: 中

### 通过项
- 线程安全：所有可变状态（_samples/_stage_fallbacks/_attributions/_extra_*）均在 threading.Lock 内读写，snapshot 在锁内完成聚合，无锁外读
- GPU 资源生命周期：本模块纯数据聚合，不持有 GL/Qt 资源，与声明一致
- 输入验证：阶段词表、backend 非空、负时长/负字节均显式拒绝并抛 ValueError
- 异常与取消路径：无吞异常的宽泛 except；导出中断时 recorder 直接丢弃即可，无资源需清理
- fallback first-wins 语义实现正确（root cause 与段级 reason 均 setdefault/None 检查）
- 归因缓冲有界（上限 64，超限丢弃而非无界增长）
- 类型注解完整（dataclass 字段与公共方法均有注解）
- 无 GLSL/HDR 数据处理、无信号递归面、无 undo/redo 状态，维度不适用

---

## src/shared/export/frame_provider.py (行数: 499)

### 发现

- [AUD-33203] [Major] read_frame_at 调用 MediaSource Protocol 之外的 read_next_frame，且不校验返回帧号
  - 位置: src/shared/export/frame_provider.py:448
  - 证据:
    ```python
    # 保存当前位置
    saved_frame = self._current_frame

    # 定位并解码
    self._source.seek_frame(frame_index)
    frame = self._source.read_next_frame()

    # 恢复位置（如果需要）
    if saved_frame != frame_index:
        self._source.seek_frame(saved_frame)
    ```
  - 影响: 构造函数契约声明 source“必须实现 MediaSource Protocol”
    （frame_provider.py:116），但该 Protocol（src/shared/media/source.py）并
    不包含 read_next_frame——仅 FFmpegMediaSource 等具体实现额外提供。对
    严格按 Protocol 实现的源，read_frame_at 直接 AttributeError；同时
    `__iter__` 路径有 frame_index 不匹配告警（line 213-218），此路径完全没有
    等价校验，seek 不精确的源会静默返回错误帧。另恢复逻辑在
    read_next_frame 抛异常时不会执行（无 try/finally），源位置遗留污染。
  - 建议: 改用 Protocol 内的 `read_frame(frame_index)`（其内部已处理
    seek+顺序优化）；返回前校验 `frame.frame_index == frame_index`；
    用 try/finally 包裹恢复 seek。
  - 置信度: 高（Protocol 成员缺失与调用点均已核对源码；模块在 src/ 内无
    内部调用方，仅经包 __init__ 公开导出，实际触发面有限）

- [AUD-33204] [Suggestion] _probe_metadata 宽泛 except Exception 吞掉探测期领域错误后继续运行
  - 位置: src/shared/export/frame_provider.py:151-162
  - 证据:
    ```python
    try:
        self._metadata = self._source.probe()
        logger.debug(...)
    except Exception as e:
        logger.warning(f"Failed to probe metadata: {e}")
        # 继续执行，某些情况下可能不需要完整元数据
    ```
  - 影响: 违反 .clinerules“不得宽泛 except 掩盖领域错误”：MediaSourceError
    及实现层 bug（AttributeError 等）一律降级为 warning。end_frame 显式
    给定时导出会继续，但 metadata=None 使 get_duration_seconds() 以帧数
    冒充秒数返回（line 256-258，单位错误）、estimate_output_size() 返回 0，
    下游进度/容量估算系统性失真；_validate_range 的越界钳制也被跳过。
  - 建议: 只捕获 MediaSourceError（或 probe 文档声明的异常集）；metadata
    缺失时对依赖元数据的方法显式抛错而非返回错误单位的数值。
  - 置信度: 高

### 通过项
- 确定性迭代主路径（__iter__）正确：逐帧 read_frame、帧号不匹配有告警、解码异常记日志后 re-raise 不吞
- 输入验证：start/end 负数与倒序范围显式拒绝；end_frame 越界钳制到 total_frames 并告警；seek_to_frame/read_frame_at 范围校验
- get_progress 除零已守卫（start==end 返回 1.0）
- 线程安全：按单导出线程使用契约设计，无可变共享状态跨线程
- 无 GPU/GL 资源、无信号递归面、无 undo/redo 状态，维度不适用
- 类型注解完整

---

## src/shared/export/gif_encoder.py (行数: 212)

### 发现

- [AUD-33205] [Major] close() 将全部已落盘帧重新载入内存，违背模块声明的“不在 RAM 保留完整动画”设计目标
  - 位置: src/shared/export/gif_encoder.py:126-144
  - 证据:
    ```python
    images: list[Image.Image] = []
    partial_path = self._partial_output_path()
    try:
        for index in range(self._frame_count):
            frame_path = os.path.join(self._frame_dir, f"frame_{index:08d}.png")
            with Image.open(frame_path) as source:
                images.append(source.copy())

        first, *rest = images
        first.save(
    ```
  - 影响: 模块 docstring（line 6-7）明确“spool 到临时目录以避免长导出把
    完整动画驻留 RAM”，但 close() 一次性把所有帧以解码图像形式装回
    `images` 列表。4K 帧（3840x2160 P 模式约 8.3MB/帧）× 数百帧可达数 GB，
    长导出收尾时突发内存峰值甚至 OOM——恰是 spool 设计要防的场景。
  - 建议: 用生成器逐帧追加（Pillow append_images 接受可迭代对象），或分
    批 save；至少在 open() 时按 config 分辨率×预估帧数给出上限保护。
  - 置信度: 高（代码路径确定；Pillow append_images 支持惰性迭代）

- [AUD-33206] [Suggestion] 浮点帧范围启发式（max<=1.0 即视为归一化 [0,1]）在边界数据上会产生错误缩放
  - 位置: src/shared/export/gif_encoder.py:193-196
  - 证据:
    ```python
    finite = np.nan_to_num(array, nan=0.0, posinf=1.0, neginf=0.0)
    if float(np.max(finite)) <= 1.0:
        finite = finite * 255.0
    array = np.clip(finite, 0.0, 255.0).astype(np.uint8)
    ```
  - 影响: 极暗的 0-255 量程浮点帧（max<1.0）被错误 ×255 放大（噪声放大）；
    归一化 HDR 数据中单点高光 >1.0 时跳过缩放整帧变暗；posinf→1.0 也隐含
    “归一化量程”假设。GIF 为 8-bit SDR 输出，量化本身合规，但量程判定应
    来自显式元数据而非 max 启发式。
  - 建议: 由 frame 携带显式 range 标记（如 pixel_format/元数据字段）决定
    缩放；启发式仅作缺失时 fallback 并记录告警。
  - 置信度: 中（触发依赖上游传入未 tone-map 的浮点帧）

### 通过项
- 输入验证：color_count 2..256、fps 正数且 ≤100（GIF 厘秒表示能力）、HxWx3/4 形状、帧尺寸与 config 一致均显式校验
- 异常与取消路径：close 失败时清理 .part 与 spool 目录并复位 _is_open 后 re-raise；abort 幂等；空帧 close 显式报错
- 发布原子性：临时 .part 写入后 os.replace 原子落盘
- add_audio_stream 显式拒绝音频，契约清晰
- 线程安全：单导出线程使用，无跨线程共享状态
- 无 GL/GLSL 路径、无信号递归面、无 undo/redo 状态，维度不适用
- 类型注解完整

---

## src/shared/export/gpu_benchmark_report.py (行数: 219)

### 发现

- [AUD-33207] [Suggestion] _atomic_write_text 写入失败时遗留 .tmp 临时文件（delete=False 且无异常清理）
  - 位置: src/shared/export/gpu_benchmark_report.py:211-218
  - 证据:
    ```python
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    temporary.replace(path)
    ```
  - 影响: write 抛异常（磁盘满/权限）时句柄关闭但 .tmp 文件留在报告目录；
    反复失败的基准运行会累积垃圾文件（隐藏文件，不易察觉）。
  - 建议: try/except 中删除 temporary 再 re-raise；写入后补 flush+fsync
    再 replace，保证崩溃时报告文件一致性。
  - 置信度: 高

### 通过项
- fail-closed 判定：is_full_gpu 完全委托 gpu_trail.full_gpu_claim_reason()，报告不可手写 full_gpu（validate 校验 full_gpu 与 claim reason 互斥）
- 输入验证：schema 必填字段校验、environment.system==Darwin 强制、inputs 非空
- 异常路径：ffprobe/ffmpeg 探测失败均转机器可读 reason（available:false）而非崩溃；subprocess 均带 timeout（20s/5s）
- 性能模式：sha256 分块流式读取（1MiB）避免整媒体文件入内存
- 线程安全：纯数据类，无共享可变状态
- 类型注解完整
- 无 GL/GLSL、无信号、无 undo/redo 面，维度不适用

---

## src/shared/export/gpu_encode_surface.py (行数: 173)

### 发现

- [AUD-33208] [Suggestion] submit() 回调返回 False（文档化失败信号）时不自动 release，lease 释放依赖调用方纪律
  - 位置: src/shared/export/gpu_encode_surface.py:115-128
  - 证据:
    ```python
    try:
        return self._submit_callback(self)
    except Exception:
        # The encoder did not accept ownership. The caller can still retry
        # a documented fallback, but this surface must not leak.
        self.release()
        raise
    ```
  - 影响: 异常路径有自动 release 保护，但 docstring 规定回调可“返回
    False 加诊断”表示未接受所有权——该返回值路径不触发 release，表面已
    `_submitted=True`，调用方若忘记手动 release()，底层 GpuFrameRelease
    （GPU 帧寿命）泄漏。文档仅以“release 安全且幂等”间接提示。
  - 建议: submit() 对 falsy 返回值自动 release 并原样返回该值，或在返回
    类型上显式封装 (accepted, diagnostic) 迫使调用方处理。
  - 置信度: 中（契约事实清晰；是否泄漏取决于调用方实现）

- [AUD-33209] [Suggestion] descriptor.planes 声明为 Sequence（可变），frozen dataclass 不保证深层不可变
  - 位置: src/shared/export/gpu_encode_surface.py:34
  - 证据:
    ```python
    backend: GpuEncodeBackend
    pixel_format: str
    width: int
    height: int
    bit_depth: int
    device_identity: str
    planes: Sequence[GpuFramePlane]
    ```
  - 影响: 类注释称“Immutable metadata”，但直接构造者可传 list 并在交付
    后增删 plane，跨线程读 descriptor.planes 可能看到变动；模块自身工厂
    from_decoded_frame 已用 tuple（line 164），仅外部直构路径受影响。
  - 建议: 类型改为 `Tuple[GpuFramePlane, ...]`，或 __post_init__ 中
    `tuple(self.planes)` 固化。
  - 置信度: 高（类型事实）；影响低

### 通过项
- 线程安全：submitted/released 状态锁保护；submit exactly-once（重复提交抛 RuntimeError）；release 幂等且先置位再调 lease（已核对 GpuFrameRelease.release 为锁内 exactly-once，gpu_frame.py:247-265）
- GPU 资源生命周期：submit 异常自动 release 防泄漏；release 后禁止 submit；lease 释放回调在锁外调用避免死锁
- 输入验证：__post_init__ 校验尺寸/位深(8,10,12,16)/device_identity/planes 非空；validate 校验 backend 一致性、device identity 一致性、plane.api==metal
- from_decoded_frame 对 frame.validate_for_device 的 fail-cause 先行校验，拒绝时返回机器可读 reason
- 无 Qt/GL 直接调用（纯契约层）；无信号递归、undo/redo 面，维度不适用
- 类型注解完整

---

## src/shared/export/gpu_trail.py (行数: 369)

### 发现

- [AUD-33210] [Suggestion] record_stage 与 record_stage_sample 混用同名段时守卫不对称，stage 登记证据被样本聚合静默覆盖
  - 位置: src/shared/export/gpu_trail.py:252-259（对照 303-309、319-339）
  - 证据:
    ```python
    def record_stage(self, stage: TrailStage) -> None:
        """登记一个段。同名重复登记视为缺陷……"""
        with self._lock:
            if stage.name in self._stages:
                raise ValueError(f"trail 段重复登记: {stage.name}")
            self._stages[stage.name] = stage
    ```
    而 record_stage_sample 只检查 `if name in self._stages`（line 304），
    snapshot 中样本聚合后写 `materialized[name] = TrailStage(...)`（line 328）
    覆盖 _stages 同名条目。
  - 影响: “同名重复登记视为缺陷”的守卫只单向生效：先 record_stage_sample
    再 record_stage(同名) 不报错，snapshot 时 record_stage 提交的
    gpu_wait_ms/note 等证据被样本聚合静默丢弃，而其字节数仍计入总数——
    审计证据与计数口径不一致。
  - 建议: record_stage 同样检查 `name in self._samples` 并抛错，保持双向
    互斥；或 snapshot 检测双源同名时抛出缺陷异常。
  - 置信度: 高（代码路径确定）；实际触发需同一条 trail 混用两种登记方式（低概率）

### 通过项
- 线程安全：登记/计数/快照全部在 threading.Lock 内，snapshot 返回 frozen 只读视图
- fail-closed 判定完整：像素驻留四段缺失、GPU 时间未测（None≠0）、上传/回读字节为正、fallback reason 四类 blocker 全覆盖
- 输入验证：TrailStage.__post_init__ 拒绝空名/空 backend/负时长/负字节/sample_count<1；from_dict 拒绝未知字段、schema_version 不匹配、重复段名
- 字节计数单向递增语义正确（record_stage 与样本字节无双重累计，已核对 snapshot 求和路径 line 340-345）
- 无 Qt/GL 依赖与声明一致；无信号、undo/redo 面；类型注解完整

---

## src/shared/export/presets.py (行数: 645)

### 发现

- [AUD-33211] [Suggestion] ProRes PROXY/LT 预设声明 bit_depth=8 + yuv422p，与 Apple ProRes 全系 10-bit 规格不符
  - 位置: src/shared/export/presets.py:127-142（LT 同 144-159）
  - 证据:
    ```python
    ProResProfile.PROXY: VideoEncoderConfig(
        codec='prores',
        profile='proxy',
        pix_fmt='yuv422p',
        bit_depth=8,
    ```
  - 影响: Apple ProRes 所有档位（含 Proxy/LT）均为 10-bit 422 编码；预设
    强制 8-bit 像素格式使专业 NLE 交互链路在进入编码器前即损失 2-bit 精度
    （渐变 banding 风险）。同仓库 frame_provider.py:312 注释亦自认
    “8-bit ProRes 通常不存在”，预设与该认知矛盾。
  - 建议: PROXY/LT 改为 pix_fmt='yuv422p10le'、bit_depth=10，与其他四档
    对齐。
  - 置信度: 中（规格事实高；实际画质影响取决于上游管线是否已 8-bit 化）

- [AUD-33212] [Suggestion] get_dnxhr_profile_for_resolution 的 width/height 参数完全未使用，与 docstring 承诺不符
  - 位置: src/shared/export/presets.py:497-521
  - 证据:
    ```python
    def get_dnxhr_profile_for_resolution(
        width: int,
        height: int,
        quality_level: str = 'hq'
    ) -> DNxHRProfile:
        ...
        profile_map = {
            'lb': DNxHRProfile.LB,
            ...
        }
        return profile_map.get(quality_level, DNxHRProfile.HQ)
    ```
  - 影响: 函数名与 docstring 声称“基于分辨率推荐”，实际只做 quality_level
    查表；调用方传入分辨率被静默忽略，接口语义误导（且经包 __init__ 公开
    导出）。
  - 建议: 实现分辨率分档逻辑（如 <HD 强制 LB/SQ、>=UHD 建议 HQX），或删
    除参数并重命名为 get_dnxhr_profile_for_quality。
  - 置信度: 高

- [AUD-33213] [Suggestion] get_recommended_preset('vfx', is_hdr=True) 返回 BT709/SDR 色彩标签的 ProRes 4444 XQ，HDR 用例元数据误标
  - 位置: src/shared/export/presets.py:630-634（预设定义 212-227）
  - 证据:
    ```python
    # VFX/compositing
    if use_case == 'vfx':
        if is_hdr:
            return PRORES_PRESETS[ProResProfile.RGBA_XQ]
        return PRORES_PRESETS[ProResProfile.RGBA]
    ```
    RGBA_XQ 预设固定 `color_space=ColorSpace.BT709,
    color_transfer=ColorTransfer.BT709`（line 217-219）。
  - 影响: HDR VFX 交付输出被写死 BT709/SDR 色彩标签，下游 NLE/播放器按
    SDR 解释 PQ/HLG 内容，颜色管理链路出错（模块 docstring 自述 4444 XQ
    用于“HDR mastering”）。
  - 建议: is_hdr 分支改用继承源色彩元数据的配置（或 BT2020/PQ 标签），
    不复用固定 BT709 预设。
  - 置信度: 中

- [AUD-33214] [Suggestion] ContainerFormat 导入未使用
  - 位置: src/shared/export/presets.py:57-63
  - 证据:
    ```python
    from .encoder import (
        VideoEncoderConfig,
        ContainerFormat,
        ColorSpace,
        ColorTransfer,
        ColorPrimaries,
    )
    ```
  - 影响: 文件内无任何 ContainerFormat 引用（container 字段用的是字符串
    'mov'/'mxf'/'mp4'/'mkv'），死导入增加阅读噪音；也暴露 container 字段
    绕过枚举约束、拼错不报错的弱点。
  - 建议: 删除导入，或让预设改用 ContainerFormat 枚举值获得拼写检查。
  - 置信度: 高

### 通过项
- 预设字段单位与 VideoEncoderConfig 定义一致（target_bitrate/max_bitrate 均为 Mbps，encoder.py:118-119 已核对，无 bps/Mbps 混淆）
- DNxHR 各档位 bit_depth/pix_fmt 映射正确（LB/SQ/HQ 8-bit yuv422p，HQX/444 10-bit）
- HDR10/HLG 预设的 x265-params 与 color 元数据字段自洽（BT2020_NCL + SMPTE2084/HLG）
- 纯静态配置模块：无线程/GPU/信号/undo 面；get_* 辅助函数对未知键的降级（默认 HQ/STANDARD）不会抛错
- 类型注解完整

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 2 | AUD-33203, AUD-33205 |
| Suggestion | 13 | AUD-33200–33202, 33204, 33206–33214 |

共 15 项发现，编号 AUD-33200–AUD-33214。
