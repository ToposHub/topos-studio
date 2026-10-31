# batch-180 审查报告：src/features/edit/core/scene_cut//

批次：batch-180 [P1]；编号区间：AUD-18000–AUD-18099；审查日期：2026-09-28。
文件：metrics.py (957 行)、models.py (1,312 行)，均已完整通读。
说明：本目录为纯计算/数据模型层，无 OpenGL、无 PyQt 信号、无 undo 栈直接交互；
GPU 资源生命周期与信号防递归两维度不适用（在通过项中注明）。

---

## src/features/edit/core/scene_cut/metrics.py (行数: 957)

### 发现

- [AUD-18001] [Major] 自适应阈值 k 值公式与设计注释不符，灵敏度后半区间调节失效
  - 位置: src/features/edit/core/scene_cut/metrics.py:755-761
  - 证据:
    ```python
    # 基准 k 值：sensitivity=0.55 时 k=5
    k_base = 5.0
    k_range = 3.0  # k 变化范围

    # 线性映射：sensitivity 0.35 -> k=8, 0.75 -> k=2
    normalized_sensitivity = (self._sensitivity - 0.35) / (0.75 - 0.35)
    k = k_base + k_range * (1 - normalized_sensitivity)
    ```
  - 影响: 按公式实测：s=0.35→k=8（符合）、s=0.55→k=6.5（注释说 5）、s=0.75→k=5（注释说 2）。
    即注释给出的三个锚点中两个与代码不符。满足全部锚点应为 k = 5 + 6*(0.5-normalized)。
    后果：灵敏度从 0.55 调到 0.75 时阈值仅从 median+6.5*MAD 降到 median+5*MAD，
    而设计意图是降到 median+2*MAD——高灵敏度档的召回提升远低于设计，UI 灵敏度滑条后半段几乎无效。
  - 建议: 修正映射为 k = k_base + 2*k_range*(0.5 - normalized_sensitivity)，或以代码为准更新注释并确认设计意图。
  - 置信度: 高（两处注释互相印证，公式只命中一个锚点，公式与设计说明必有一错；判定代码偏离设计）

- [AUD-18002] [Suggestion] calculate_with_history 的 window_size 与 prev_edge_delta 参数完全未使用
  - 位置: src/features/edit/core/scene_cut/metrics.py:271-313
  - 证据:
    ```python
    def calculate_with_history(
        self,
        frame_prev: np.ndarray,
        frame_curr: np.ndarray,
        prev_luma_delta: float | None = None,
        prev_edge_delta: float | None = None,
        window_size: int = 5,
    ) -> SceneCutMetrics:
    ```
    函数体只读取 prev_luma_delta（300 行比较一次）；prev_edge_delta 仅参与 297 行的 None 判断，值从未被使用；
    window_size 在函数体内没有任何引用。
  - 影响: API 误导调用方以为存在滚动窗口历史逻辑；死参数掩盖了实际仅做单点闪关抑制的事实。
  - 建议: 删除未用参数，或补全设计中的窗口逻辑。
  - 置信度: 高

- [AUD-18003] [Suggestion] 帧输入未校验 dtype/通道数，非 uint8-BGR 帧产生 NaN 指标或 cv2 异常
  - 位置: src/features/edit/core/scene_cut/metrics.py:229-237（仅校验 None 与 shape）；167-170（calcHist）；159（cvtColor）
  - 证据:
    ```python
    if frame_prev is None or frame_curr is None:
        return self._create_zero_metrics()

    if frame_prev.shape != frame_curr.shape:
        ...
    hist = cv2.calcHist(
        [frame], [ch], None, [self._hist_bins], [0, 256]
    ).flatten()
    channel_hists.append(hist / np.sum(hist))
    ```
  - 影响: 本项目 HDR 内部数据为 float16/32。若 float 帧流入：calcHist 的 [0,256] 对 float 图直接按值分箱，
    值域外的 float 数据直方图全 0 → np.sum(hist)==0 → 除法得 NaN → 下游 SceneCutMetrics.__post_init__
    在很远的地方抛出难定位的 ValueError；4 通道 BGRA 帧则让 cv2.cvtColor(COLOR_BGR2GRAY) 直接抛 cv2.error。
  - 建议: 在 calculate() 入口校验 frame.ndim==3 且 frame.dtype==np.uint8（或显式转换），错误时抛带上下文的 TypeError。
  - 置信度: 中（取决于上游是否保证 uint8 BGR 代理帧，本文件内无此保证）

- [AUD-18004] [Suggestion] extract_frame_features 内 try: import cv2 为死代码
  - 位置: src/features/edit/core/scene_cut/metrics.py:879-882
  - 证据:
    ```python
    try:
        import cv2
    except ImportError:
        raise RuntimeError("OpenCV is not installed")
    ```
  - 影响: 模块顶部第 35 行已 `import cv2`，若未安装模块加载即失败，此分支永不可达；徒增阅读负担。
  - 建议: 删除该 try/except。
  - 置信度: 高

- [AUD-18005] [Suggestion] Sobel 边缘量纲在两条路径下不一致，粗扫与精扫分数不可比
  - 位置: src/features/edit/core/scene_cut/metrics.py:452-460 与 895-896、933
  - 证据:
    ```python
    # _compute_sobel_edge（精扫路径）
    sobel_x = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    ...
    magnitude = np.clip(magnitude, 0, 255)

    # extract_frame_features（粗扫路径）
    sobel = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    edges_mean = float(np.mean(np.abs(sobel)))   # 无幅值合成、无归一化，ksize=3 时单方向值域可达 ±1020
    ...
    edge_diff = abs(features_prev["edges_mean"] - features_curr["edges_mean"]) / 255.0
    ```
  - 影响: 精扫用 sqrt(sx²+sy²) 且 clip 到 255；粗扫用单方向 |sx| 均值且未做 Sobel 缩放，
    /255.0 的归一化基准不同，compare_frame_features 的"简化差异"与 combined_score 尺度不对应。
  - 建议: 粗扫复用 _compute_sobel_edge 或统一为梯度幅值后再归一化。
  - 置信度: 中（粗扫定位为"快速估算"，但量纲漂移会影响两阶段阈值衔接）

- [AUD-18006] [Suggestion] AdaptiveThresholdCalculator 构造参数无验证，window_size<10 时自适应阈值永不生效
  - 位置: src/features/edit/core/scene_cut/metrics.py:686-699、727-731
  - 证据:
    ```python
    def __init__(self, window_size: int = 30, sensitivity: float = 0.55) -> None:
        self._window_size = window_size
        self._sensitivity = sensitivity
    ...
    if len(self._score_buffer) >= 10:
        self._calculate_threshold()
    else:
        # 窗口未满，使用初始阈值
        self._current_threshold = 0.3
    ```
  - 影响: 硬编码的 10 与 window_size 无关联：传入 window_size<=10 时 _calculate_threshold 永不被调用，
    阈值恒为 0.3 且无任何告警；sensitivity 越出 [0.35,0.75] 时 normalized_sensitivity 线性外推，k 值跑出设计区间。
  - 建议: 构造时校验 window_size>=10、0.35<=sensitivity<=0.75，否则抛 ValueError。
  - 置信度: 高

- [AUD-18007] [Suggestion] 单侧黑场分数公式方向疑似反转，且中间分支在默认阈值下不可达
  - 位置: src/features/edit/core/scene_cut/metrics.py:527-534
  - 证据:
    ```python
    # 如果只有一帧是黑，分数为中等
    if prev_is_black or curr_is_black:
        black_frame_luma = min(luma_prev, luma_curr)
        content_frame_luma = max(luma_prev, luma_curr)

        transition_score = black_frame_luma / self._black_luma_threshold
        return float(np.clip(transition_score, 0.0, 0.5))
    ```
  - 影响: black_frame_luma 越黑（越接近 0）分数越低：纯黑帧得 0.0 分，恰在阈值边缘（9.9）反而得 ~0.5 分，
    与"越黑越像黑场过渡"的意图相反。且该分支上限 0.5 < 默认 black_score_threshold=0.7
    （models.py:103，filters.py:340 消费），单侧黑过渡永远不会被抑制，分支实际失效。
  - 建议: 改为 (threshold - black_frame_luma)/threshold 方向并复核 0.5 上限与消费阈值的配套关系。
  - 置信度: 中（无法从本文件确定 0.5 上限是否为刻意设计，但"越黑分越低"与注释意图矛盾）

### 通过项
- 线程安全：单实例由 detector.py:336 在检测任务内创建使用，可变缓存（_cached_frame/_cached_features）无跨线程共享假设；无锁即无假共享问题
- GPU 资源生命周期：不适用（纯 CPU/OpenCV 计算，无 ModernGL 对象）
- GLSL/HDR 正确性：不适用（无着色器；所有分数均显式归一并 np.clip 到 [0,1]）
- 信号防递归：不适用（无 PyQt 依赖）
- 缓存正确性：_features_for 按 ndarray 对象身份（is）命中且持有强引用，避免对象地址复用导致的误命中，并提供 reset_cache（metrics.py:186-210）
- 数值健壮性：_chi_square_distance 对除零有 1e-10 下限防护（metrics.py:399-401）；组合权重常量与 _calculate_combined_score 一致（0.35+0.30+0.20+0.10）
- 异常路径：calculate() 对 None/形状不匹配做防御并记录 warning 后返回零值指标，不会向调用方抛裸异常
- 序列工具：calculate_metrics_sequence 正确复用同一 calculator 使相邻帧缓存命中

---

## src/features/edit/core/scene_cut/models.py (行数: 1,312)

### 发现

- [AUD-18010] [Major] SceneCutSettings.to_hash() 遗漏 5 个误报抑制阈值字段，缓存键不随设置变化失效
  - 位置: src/features/edit/core/scene_cut/models.py:137-151（字段定义 101-106；cache.py:226 消费 settings.to_hash()）
  - 证据:
    ```python
    def to_hash(self) -> str:
        data = (
            f"{self.sensitivity:.4f}"
            f"|{self.min_scene_frames}"
            f"|{self.sample_width}x{self.max_sample_height}"
            f"|{self.use_proxy}"
            ...
            f"|{self.start_source_frame}-{self.end_source_frame or 'end'}"
        )
        return hashlib.sha256(data.encode()).hexdigest()[:16]
    ```
    而字段定义（101-106 行）为：
    ```python
    rolling_window_size: int = 30
    black_score_threshold: float = 0.7
    motion_score_threshold: float = 0.5
    flash_score_threshold: float = 0.3
    transition_frame_threshold: int = 12
    ```
  - 影响: filters.py:254/340/344/417/711 直接用这 5 个字段控制误报抑制与转场合并，改变它们会实质改变检测结果；
    但 generate_cache_key（cache.py:180-226）与 SceneCutAnalysis.to_cache_key（本文件 926-940）都以 settings.to_hash() 为键，
    修改这些阈值后仍命中旧缓存，静默返回按旧阈值计算的候选——缓存结果与当前设置不一致。
  - 建议: to_hash() 补齐全部 17 个字段（或改用 to_dict() 的规范化序列做哈希），并加断言测试防止字段再次遗漏。
  - 置信度: 高

- [AUD-18011] [Major] with_updated_candidate / with_all_decisions 重建时丢失 proxy_fingerprint
  - 位置: src/features/edit/core/scene_cut/models.py:874-886、896-908（字段定义 809）
  - 证据:
    ```python
    return SceneCutAnalysis(
        analysis_id=self.analysis_id,
        ...
        candidates=new_candidates,
        created_at=self.created_at,
        detector_version=self.detector_version,
        diagnostics=self.diagnostics,
    )
    ```
    两个方法的实参列表均无 proxy_fingerprint=self.proxy_fingerprint（而 to_dict/from_dict/to_cache_key 均包含该字段，
    to_cache_key:930 以 `self.proxy_fingerprint or "no-proxy"` 参与键计算）。
  - 影响: 用户 accept/reject 决策后派生的分析（scene_cut_controller.py:541 经 with_all_decisions）proxy 指纹变为 None；
    其 to_cache_key() 从 "<proxy_fp>" 漂移为 "no-proxy"，与 generate_cache_key 生成的正规键不一致——派生结果一旦经
    SceneCutCache.save_analysis（cache.py:380 以 to_cache_key 为存储键）写回，会落到错误键位，可能与真正的无代理分析键
    冲突造成缓存污染。
  - 建议: 两处重建补传 proxy_fingerprint；可加 dataclasses.replace 复制以避免逐字段遗漏。
  - 置信度: 高

- [AUD-18012] [Major] timecode_to_frames 未处理 drop-frame 与小数帧率，与 _frames_to_timecode 的 DF 修复不对称
  - 位置: src/features/edit/core/scene_cut/models.py:1285-1311
  - 证据:
    ```python
    parts = timecode.split(":")
    if len(parts) != 4:
        raise ValueError(f"Invalid timecode format: {timecode}")

    hours, minutes, seconds, frames = [int(p) for p in parts]

    total_frames = (
        hours * int(fps) * 3600
        + minutes * int(fps) * 60
        + seconds * int(fps)
        + frames
    )
    ```
  - 影响: 同文件 _frames_to_timecode（1275-1282）已按 P0-05 审计修复委托 interchange 的 DF 双向实现，
    但反向转换仍是旧副本：int(fps) 截断使 29.97→29、23.976→23（连 NDF 也算错）；29.97 DF 时间码反算帧号
    每分钟偏差 2 帧、1 小时偏差 108 帧；字段越界（ff>=fps、mm>=60）不报错。该函数经
    scene_cut/__init__.py:216 导出为公共 API，与 interchange.py:148 的精确逆运算版本并存，调用方极易用错。
  - 建议: 与 _frames_to_timecode 对称地委托 interchange.timecode_to_frames（带 is_drop_frame_rate(fps)）。
  - 置信度: 高（代码事实确定；仓库内未见直接调用点，实际影响面取决于外部/未来调用）

- [AUD-18013] [Major] generate_scene_subclips 硬编码首场景起始秒为 0.0，范围分析的时间元数据错误
  - 位置: src/features/edit/core/scene_cut/models.py:1186-1203、1205-1207
  - 证据:
    ```python
    if not accepted:
        return (
            SceneSubclipSpec(
                ...
                source_start=start_frame,
                source_duration=end_frame - start_frame,
                start_seconds=0.0,
    ...
    specs: list[SceneSubclipSpec] = []
    prev_frame = start_frame
    prev_seconds = 0.0
    ```
    而 SceneCutCandidate.seconds 文档定义为 "Time in seconds from media start"（绝对时间，572 行），
    start_frame 来自 analysis.settings.start_source_frame（with_range 支持任意非零起点，175-199 行）。
  - 影响: 对 start_source_frame=1000 的范围分析，首场景 start_seconds 被写成 0.0 而非该帧的真实秒数；
    duration=cut.seconds-0.0 把 0..起点之间的时长也算进首场景，SceneSubclipSpec.to_metadata（1068-1095）
    写入媒体池的 start/end/duration_seconds 与 start/end_timecode（按帧计算，正确）互相矛盾。
  - 建议: 用 analysis.source_time_index.source_frame_to_seconds(start_frame)（无索引时按 fps 折算）初始化 prev_seconds，
    无 cuts 分支同理。
  - 置信度: 中（scene_cut_controller.py:334 当前传全片统计调用，若调用链只支持全片分析则未触发；
    但 with_range 与 start_source_frame 验证使非零起点为受支持的合法状态）

- [AUD-18014] [Suggestion] diagnostics 为可变 dict，破坏 frozen 不可变约定且在派生副本间共享引用
  - 位置: src/features/edit/core/scene_cut/models.py:808、886、907
  - 证据:
    ```python
    diagnostics: dict[str, Any] | None = None
    ...
            detector_version=self.detector_version,
            diagnostics=self.diagnostics,   # with_updated_candidate / with_all_decisions 均直接传同一对象
        )
    ```
  - 影响: frozen dataclass 的不可变承诺被可变字段穿透：任何持有者原地修改 diagnostics 会同时影响历史版本与所有派生版本，
    跨线程读写存在竞争。
  - 建议: 派生时浅拷贝 dict（或包装为 Mapping 代理 / 冻结结构）。
  - 置信度: 高（事实确定，实际风险取决于是否被并发读写）

- [AUD-18015] [Suggestion] SceneCutMetrics.combined_score 文档公式与实现不符（多写了 0.05*motion 项）
  - 位置: src/features/edit/core/scene_cut/models.py:493-494（实现见 metrics.py:642-647）
  - 证据:
    ```python
    combined_score: Weighted combination of metrics.
        Formula: 0.35*luma + 0.30*hist + 0.20*edge + 0.10*color + 0.05*motion
    ```
    而 metrics.py 实现为 0.35/0.30/0.20/0.10 四项，无 motion（且 metrics.py:52-53 注明 pHash 的 0.05 已并入 color）。
  - 影响: 文档权重和为 1.0、实现为 0.95，误导后续维护与阈值调参。
  - 建议: 更新 docstring 与实现一致。
  - 置信度: 高

- [AUD-18016] [Suggestion] 反序列化不校验 points 的排序与秒单调性，二分查找对乱序数据静默给出错误映射
  - 位置: src/features/edit/core/scene_cut/models.py:462-474（from_dict）；329-383（source_frame_to_seconds 二分查找）
  - 证据:
    ```python
    points = tuple(
        FrameTimePoint.from_dict(p)
        for p in data.get("points", [])
    )
    ```
    类文档（313 行）声称 "points: Tuple of frame time points, sorted by source_frame"，
    但 from_dict 不验证排序；source_frame_to_seconds/get_point_for_frame 均为二分查找。
  - 影响: 缓存 JSON 属磁盘外部数据（铁律要求验证）。损坏或手改的缓存加载后，帧↔秒映射静默错位，
    VFR 素材的剪切点时间错误且无任何告警。
  - 建议: from_dict 后做一次 O(n) 排序校验（source_frame 严格递增、seconds 非递减），失败抛 ValueError。
  - 置信度: 中（正常生产路径会排序；风险在外部数据边界）

### 通过项
- 线程安全：全部数据类 frozen=True，字段不可变（diagnostics 除外，见 AUD-18014）；无共享可变状态的方法
- GPU 资源生命周期：不适用（纯数据模型，无 ModernGL/GL 对象）
- GLSL/HDR 正确性：不适用（无着色器代码；[0,1] 值域校验完整）
- 信号防递归：不适用（无 PyQt 依赖，符合模块自述 "No PyQt dependencies"）
- 输入验证：__post_init__ 覆盖全面——sensitivity/min_scene_frames/sample_width/max_sample_height/起止范围（108-135）、
  source_frame/seconds/score/confidence（603-612）、八项指标 [0,1] 区间（506-519，NaN 因比较失败同样被拒）、
  子剪辑 13 项约束（1035-1056）；from_dict 对必需键缺失抛 KeyError、对非法枚举值抛 ValueError（显式失败，无静默吞错）
- 复制方法完整性：SceneCutCandidate 的 with_decision/with_timeline_frame/with_reasons 全部 12 字段无遗漏（616-665）；
  SceneCutSettings.with_sensitivity/with_range 全部 17 字段无遗漏（153-199）
- 查询算法：source_frame_to_seconds 的二分+插值+两端外推逻辑正确（假定有序输入）；seconds_to_nearest_source_frame
  下界搜索与邻点比较正确；get_candidate_at_frame 线性查找适合候选量级
- 序列化往返：to_dict/from_dict 字段一一对应（除 AUD-18010/18011 所述哈希与派生遗漏外无漂移）；
  to_cache_key 与 cache.generate_cache_key 的键结构逐字段一致（media_fp|range|settings_hash|detector_version|proxy|clip）
- 编号辅助：generate_analysis_id 基于 sha256(asset_id|timestamp|version) 截断 16 位，碰撞概率可忽略
