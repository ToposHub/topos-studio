# 批次报告：batch-374 — src/shared/tracking//

- 编号区间：AUD-37400–AUD-37499（实际使用 AUD-37400–AUD-37412）
- 审查日期：2026-09-28
- 文件数：6（2,126 行），全部完整通读
- 汇总：Critical 0 · Major 2 · Suggestion 10

交叉验证说明（非本批次文件，只读核对）：`tracking_evaluator.capture_base_geometry`（几何快照来源）、
`canvas.py:1126 path.cubicTo(p1['c2'], p2['c1'], p2['pos'])`（曲线控制点约定）、
`tracking_service.py`（缓存调用与线程上下文）、`point_manager.py`（feature_id 语义）。
OpenCV 常量区域 NCC 行为已用项目 .venv（cv2 4.11.0）实测验证。

---

## src/shared/tracking/shape_mask.py (行数: 135)

### 发现

- [AUD-37400] [Suggestion] base_geometry 字段缺长度/类型校验，畸形持久化数据会让跟踪任务 IndexError
  - 位置: src/shared/tracking/shape_mask.py:67-69, 82-87, 106, 110-116
  - 证据:
    ```python
    center = geometry.get("center") or [0.5, 0.5]
    size = geometry.get("size") or [0.0, 0.0]
    cx = float(center[0]) * width
    cy = float(center[1]) * height
    ```
    ```python
    anchors = [p.get("pos") or [0.0, 0.0] for p in points]
    ...
    p0 = (float(anchors[i][0]) * width, float(anchors[i][1]) * height)
    ```
  - 影响: `base_geometry` 经 `TrackingBinding.to_dict()` 持久化、`from_dict()` 原样透传后作为
    `TrackRequest.shape_geometry` 进入跟踪工作线程；若工程文件字段被截断/手改（如 `center=[0.5]`、
    `points[i]` 缺 `pos` 或非 dict），`center[1]`/`p.get` 直接抛 IndexError。模块自身契约是
    「几何无效时返回 None，调用方退化为包围盒矩形」，但只校验了 type/空值，未校验字段形状，
    畸形数据走不到退化路径，跟踪任务以原始异常失败。
  - 建议: 对 center/size/pos/c1/c2 统一做 `len == 2` 与数值类型检查（可抽一个
    `_as_point(v) -> Optional[Tuple[float, float]]`），非法即按 None 退化。
  - 置信度: 高（代码事实确凿；触发依赖畸形持久化数据，概率低）

### 通过项
- 线程安全：纯函数（numpy/PIL 局部对象），无共享可变状态，可在工作线程直接调用
- 曲线控制点约定正确：`_curve_polygon` 取 `points[i]["c2"]`（出向）与 `points[i+1]["c1"]`（入向），
  与 canvas.py 渲染路径 `cubicTo(p1['c2'], p2['c1'], p2['pos'])` 一致（已核对源码）
- 采样粒度与端点处理：`u = t/_CURVE_SEGMENTS` 不含 u=1，段间无重复顶点；闭合取模 `(i+1)%n` 正确
- 退化保护：`_MIN_MASK_PIXELS`、rx/ry/hw/hh 下限检查存在
- GPU/GLSL/信号/HDR：无涉及（纯 CPU 光栅化）
- 性能：光栅化每任务一次，PIL 逐段 16 采样规模可控

---

## src/shared/tracking/stabilize.py (行数: 186)

### 发现
（无发现）

### 通过项
- 线程安全：全部纯函数，无共享状态
- 数值健壮性：`_field` 对 NaN/±inf 做 `nan_to_num` 清洗后才平滑/求逆；`affine_matrix` 用
  `_MIN_SCALE` 钳制退化缩放避免 `linalg.inv` 奇异；`invert_entry` 的 2x3 求逆公式 [A|t]→[A⁻¹|-A⁻¹t] 正确
- 分解正确性：`decompose_affine` 的 sy 投影式与行列式符号保留（det<0 时 sy 取负）经代数推导验证
  正确（M = R·S 下 c·(-sinθ)+d·cosθ = sy），镜像不会被 abs 吞掉
- ±180° 缝处理：rotation 先 `np.unwrap` 再平滑，避免 179°/-179° 平均成 ~0°
- 平滑窗口语义：按帧距（而非索引）扩展窗口，失败帧空洞不会跨时间涂抹；`smooth_series` 对
  window≤1/空输入返回拷贝（不别名）
- 键类型契约：docstring 明确 {frame_str: entry}，唯一调用方 composite_page.py 传入 JSON 字符串键，符合
- 异常路径：调用方（composite_page `_on_tracker_stabilize_requested`）以 try/except 包裹并给用户提示
- GPU/GLSL/信号/undo：无涉及

---

## src/shared/tracking/track_data.py (行数: 282)

### 发现

- [AUD-37401] [Suggestion] from_dict 枚举值不容错，未知 mode 会让整个工程加载失败
  - 位置: src/shared/tracking/track_data.py:255
  - 证据:
    ```python
    mode=TrackingMode(data.get("mode", "trs")),
    ```
  - 影响: `TrackingMode(...)` 对未知字符串抛 ValueError（types.py 的 `TrackStatus`/`FailureReason`
    反序列化同理，见 AUD-37412）。schema 版本机制在 TrackingStore 层（SCHEMA_VERSION），但枚举
    前向兼容缺口仍在：旧版本打开新版本保存的工程（新增枚举成员）时，单个绑定反序列化失败会
    阻断整个调色栈的跟踪数据加载。
  - 建议: 未知枚举值降级为默认模式并记 warning（或整体跳过该 binding），不要让单个字段炸掉加载。
  - 置信度: 中（崩溃路径确凿；触发需要跨版本工程文件）

- [AUD-37402] [Suggestion] locked/excluded 集合类型注解未参数化
  - 位置: src/shared/tracking/track_data.py:74-75
  - 证据:
    ```python
    locked_feature_ids: set = field(default_factory=set)
    excluded_feature_ids: set = field(default_factory=set)
    ```
  - 影响: 元素类型（int）未表达，调用方与序列化路径（`sorted(self.locked_feature_ids)`）依赖隐式约定，
    静态检查无法捕获混入 str 的回归。
  - 建议: 改为 `set[int]`（文件已有 `from __future__ import annotations`，无运行时成本）。
  - 置信度: 高

### 通过项
- 状态提交一致性：手动修正优先级正确——`transform_at` 手动优先；`apply_result` 不覆盖已修正帧
  （仅更新观测）；`clear_results` 默认保留修正；每次写入统一递增 `result_version` 并刷新 `modified_at`
- 手动修正语义：`set_manual_correction` 强制 status=MANUAL、confidence=1.0、failure_reason=NONE，合理
- 特征点管理：lock 优先于 exclude（`lock_feature` discard 排除集；`exclude_feature` 对锁定点直接返回），
  `is_feature_excluded` 双重判定与写入侧约束一致
- 序列化对称性：`to_dict`/`from_dict` 字段逐项核对对称（transforms/observations/manual_corrections
  的 str-int 键转换、集合排序列表化、base_geometry 透传）
- `status_at` 无数据按 FAILED 处理，不假有效
- 线程安全：可变模型，按 TrackingStore 声明的「只在 UI 线程读写」约定使用（契约在 store 层文档化）
- 输入验证：`__post_init__` 兜底生成 binding_id

---

## src/shared/tracking/tracker_2d.py (行数: 1151)

### 发现

- [AUD-37403] [Major] 参考模板为低纹理（近常量）时外观门控静默失效，且诊断恒报满分相似度
  - 位置: src/shared/tracking/tracker_2d.py:865-870, 883-889, 572
  - 证据:
    ```python
    if x1 - x0 < 12 or y1 - y0 < 12:
        return None                       # 模板过小 → 不设门控
    template = ref_gray[y0:y1, x0:x1]
    ```
    ```python
    if ref_template is None:
        return 1.0                        # "模板不可用时不设门控"
    ...
    score = cv2.matchTemplate(patch, template, cv2.TM_CCOEFF_NORMED)
    return float(score[0][0])
    ```
    门控判定：`if appearance < cfg.appearance_min_similarity:`（line 572）。
    实测（本项目 .venv，cv2 4.11.0）：`matchTemplate(全零图, 全零模板, TM_CCOEFF_NORMED)` 返回
    **1.0**（0/0 被规约为满分），非 NaN。
  - 影响: `_ref_template` 只做 ≥12px 尺寸检查，不做方差检查。pattern 区域取在天空/墙面/黑场等
    低纹理内容上时模板近似常量 → 任意 warp 结果与它的 NCC 都是 1.0 → v2 引入的外观门控
    （模块头与 types.py `TRACKING_ALGORITHM_VERSION=2` 声明的「遮挡/漂移假有效修复」）对这类
    素材完全不起作用，遮挡帧只要几何自洽仍可能被判 VALID；同时 `FrameDiagnostics.
    appearance_similarity` 恒为 1.0，面板显示具有误导性。（反向情形已验证安全：常量 patch vs
    纹理模板 → 0.0，门控正常拦截。）
  - 建议: `_ref_template` 增加模板方差/梯度能量下限（如 `cv2.Laplacian(t, CV_64F).var() < ε`
    视为模板不可用返回 None），让低纹理模板走显式「无门控」路径而非伪满分。
  - 置信度: 高（行为已实测复现；实际风险取决于低纹理 pattern 的出现频率）

- [AUD-37404] [Suggestion] 有效点不足失败路径漏传 ref_template，模板重捕获前每帧重复重建参考模板
  - 位置: src/shared/tracking/tracker_2d.py:518-527（对比 535-544 等其余调用点均传 `ref_template=ref_template`）
  - 证据:
    ```python
    if cur_pts_raw is None or int(valid.sum()) < mode.min_points:
        return self._fail_step(
            request=request, frame=frame, sharpness=sharpness,
            out_fraction=out_fraction, fb_weak=fb_pass_ratio < 0.5,
            solve_failed=False, tracked_count=int(tracked.sum()),
            fb_pass_ratio=fb_pass_ratio, allow_redetect=allow_redetect,
            allow_recapture=allow_recapture, ref_gray=ref_gray,
            anchor_pts=anchor_pts, cur_gray=cur_gray,
            prev_H=prev_H, pattern_ana=pattern_ana,
        )
    ```
    而 `_template_recapture` 收到 None 时：`ref_template = self._ref_template(ref_gray, pattern_ana)`
    （line 918-919）逐帧重算。
  - 影响: 该失败分支（长遮挡下最常见的点数不足路径）每次进入模板重捕获都重新裁剪参考模板并
    重建 3x3 矩阵；结果与传参路径等价（同参数重算），仅浪费计算，但掩盖了「调用点参数应一致」
    的约定，易在后续维护中引入分歧。
  - 建议: 补传 `ref_template=ref_template`，与其他五个调用点对齐。
  - 置信度: 高

- [AUD-37405] [Suggestion] anchor_H 方向注释与实际矩阵语义相反，误导维护
  - 位置: src/shared/tracking/tracker_2d.py:282-284（及 414-415 同样表述）
  - 证据:
    ```python
    # - anchor_H：锚帧坐标 → 参考帧坐标（重锚时为单位阵；
    #   每帧求解出「锚→当前」后左乘得到「参考→当前」）
    ```
    而实际组合（line 564）：`H_ref_cur = H_anchor_cur @ anchor_H`，且 line 417 取
    `anchor_H = step.transform.matrix_np`——模块头明确 matrix 语义为「参考帧坐标 → 当前帧坐标」。
    链式成立要求 anchor_H 为「参考帧 → 锚帧」；若按注释的「锚帧→参考帧」，(锚→当前)·(锚→参考)
    根本无法复合出「参考→当前」。`_template_recapture` 的 docstring（line 915「参考帧→锚帧」）
    与实现才是正确表述。
  - 影响: 纯注释错误（代码数学正确，已按 T·R·S 与共轭推导验证），但两处关键状态变量的方向
    文档写反，后续基于注释改代码极易引入方向翻转 bug。
  - 建议: 修正 line 282-284 与 414-415 注释为「参考帧坐标 → 锚帧坐标」。
  - 置信度: 高

- [AUD-37406] [Suggestion] fb_weak 阈值 0.5 为散落的魔法数字，违反 TrackerConfig 自身声明
  - 位置: src/shared/tracking/tracker_2d.py:521, 537, 555, 576, 606, 639
  - 证据:
    ```python
    out_fraction=out_fraction, fb_weak=fb_pass_ratio < 0.5,
    ```
    同文件 TrackerConfig docstring（line 64）：「阈值全部配置化，禁止魔法数字散落算法内部」。
  - 影响: FB 通过率阈值 0.5 影响 `_classify_failure` 是否归因 OCCLUSION，却不可配置、与
    `weak_confidence` 等其他阈值的调参入口不一致。
  - 建议: 在 TrackerConfig 增加 `fb_occlusion_min: float = 0.5` 并替换六处。
  - 置信度: 高

- [AUD-37407] [Suggestion] RANSAC 未播种，同输入的分析结果不保证可复现
  - 位置: src/shared/tracking/tracker_2d.py:962-978
  - 证据:
    ```python
    M, mask = cv2.estimateAffinePartial2D(
        src, dst, method=cv2.RANSAC,
        ransacReprojThreshold=cfg.ransac_reproj_threshold,
        confidence=cfg.ransac_confidence,
    )
    ```
    （`estimateAffine2D` / `findHomography` 同样未播种。）
  - 影响: cv2 的 RANSAC 使用线程本地 theRNG，状态随同线程内其它 cv2 调用推进；同一素材同一参数
    在不同会话/不同调用历史下可能产生不同内点集与矩阵。会话内缓存（tracking_cache）会掩盖该
    不确定性，但重分析、A/B 对比与回归测试不可复现，且缓存未命中时的结果漂移难以解释。
  - 建议: 在 `track()` 入口（或每次 `_estimate` 前）调用 `cv2.setRNGSeed(固定种子)`，代价可忽略。
  - 置信度: 中（RNG 状态推进行为依赖 OpenCV 版本；不可复现的触发条件未逐一实测）

- [AUD-37408] [Suggestion] 预测区域完全出画时失败原因被误判为 BLUR 而非 OUT_OF_BOUNDS
  - 位置: src/shared/tracking/tracker_2d.py:1071-1079, 1022-1031
  - 证据:
    ```python
    if x1 - x0 < 4 or y1 - y0 < 4:
        return 0.0            # 区域裁剪退化 → sharpness=0
    ```
    ```python
    if sharpness < cfg.blur_laplacian_min:
        return FailureReason.BLUR      # 分类顺序：BLUR 优先于 OUT_OF_BOUNDS
    if out_fraction > cfg.out_of_bounds_fraction:
        return FailureReason.OUT_OF_BOUNDS
    ```
  - 影响: 目标大幅出画时 `search_rect_pred` 裁剪后不足 4px，sharpness 恒 0 → 恒判 BLUR。
    仅影响诊断标签（失败即失败，状态不受影响），但面板会把「出画」误导为「模糊」，引导用户
    错误调参（去改模糊阈值/换质量）。
  - 建议: 裁剪退化时返回 NaN/None 并在 `_classify_failure` 中把该情形优先归 OUT_OF_BOUNDS，
    或对退化区域跳过 sharpness 判定。
  - 置信度: 中（触发需预测区域近乎完全出画）

- [AUD-37409] [Suggestion] 内部数据结构与参数存在弱类型注解
  - 位置: src/shared/tracking/tracker_2d.py:1142（`observations: Optional[list]`）、467-468
    （`ref_template: Optional[Tuple[np.ndarray, np.ndarray, tuple]]`、`criteria: tuple`）
  - 证据:
    ```python
    observations: Optional[list]
    ...
    ref_template: Optional[Tuple[np.ndarray, np.ndarray, tuple]],
    criteria: tuple,
    ```
  - 影响: 元素类型未表达，`_FrameStep.observations` 实为 `Optional[List[FeatureObservation]]`、
    criteria 实为 `Tuple[int, int, float]`；静态检查与阅读成本上升。
  - 建议: 参数化泛型（文件已有 `from __future__ import annotations`）。
  - 置信度: 高

### 通过项
- 异常与取消路径：cancel_token 每帧检查（getattr+默认 lambda 安全）；`owns_sampler` 时 sampler 在
  finally 中必定关闭；progress_cb 异常有隔离（try/except + debug 日志，未掩盖领域错误）
- 失败策略状态机：HOLD/STOP/RECAPTURE 判定基于 step 级状态（`step.transform.status`）而非写出的
  HELD 结果，held 帧不污染 `last_valid_ana`/`gap_since_valid`；HOLD 置信度衰减用 `abs(frame -
  last_valid_frame)`，与跟踪方向无关；STOP 在写出失败帧与诊断后终止
- 锚点链一致性：成功帧每帧重锚（anchor_H=本帧矩阵，避免求逆放大误差）、重检测/重捕获帧
  anchor_H 分别取本帧矩阵/prev_H/平移估计，组合 `H_anchor_cur @ anchor_H` 数学正确（已推导）；
  失败帧保留上次有效 prev_H 用于预测，点链仅在 cur_pts 非空时推进，空点链不可达（各路径
  保证 ≥ min_points ≥ 1）
- 外观门控 warp 方向正确：`M = inv(H_ref_cur @ T)` 配默认（非 INVERSE_MAP）warpPerspective 语义
  即 `patch(p) = cur(M_fwd(p))`，已按 OpenCV 文档约定推导验证
- 模板重捕获：平移估计 = 匹配中心 − 模板裁剪中心（line 939-940 展开等价），正确；本帧仍记失败、
  由后续帧管线验证；纹理模板对纯色帧实测 NCC=0.0，低于阈值正确拒绝重捕获
- 输出换算：`S @ H_ana @ S_inv` 共轭换算到原始分辨率，参考帧写单位阵 + VALID + confidence 1.0；
  HOMOGRAPHY 模式对参考帧单位阵预检稳定性，阈值不合理时整段显式 DEGENERATE 失败，不假有效
- 输入换算防御：`scale <= 0` 回退 1.0；`_to_source_observation` 对 inf 残差保透传
- LK/FB 实现细节：back-flow 仅对 tracked 子集运行，`fb_idx = np.where(tracked)[0][ok]` 索引回映射
  正确；`valid = fb_pass & in_frame` 且越界仅参与比例统计；运算符优先级
  `(1.0 - mean) if np.any(tracked) else 1.0` 语义正确
- 置信度五因子：权重归一（w_sum>0 防御）、误差 inf 时 `exp(-inf)=0`、单点（TRANSLATION）时
  spread=0 合理惩罚
- 线程安全：Tracker2D 无跨调用可变状态（config/detector 只读），可安全在工作线程实例化运行
- GPU/GLSL/HDR/信号：无涉及（无 Qt、无 ModernGL）
- 性能：逐帧 LK + 小图 NCC 的主循环开销与 4K 分析分辨率相称；整帧 matchTemplate 仅在失败
  重捕获窗口内触发

---

## src/shared/tracking/tracking_cache.py (行数: 131)

### 发现

- [AUD-37410] [Major] 缓存键未包含 shape_geometry，遮罩形状变化后返回陈旧跟踪结果
  - 位置: src/shared/tracking/tracking_cache.py:48-59, 63-75
  - 证据:
    ```python
    payload = {
        "fp": fingerprint,
        "fs": int(frame_start),
        "fe": int(frame_end),
        "ref": int(reference_frame),
        "dir": direction,
        "mode": mode,
        "q": quality,
        "alg": int(algorithm_version),
        "pattern": [round(float(v), 6) for v in pattern],
        "search": [round(float(v), 6) for v in search],
    }
    ```
    `key_for_request`（line 63-75）同样只取这些字段；而 `TrackRequest.shape_geometry`
    （tracker_2d.py:131-134）会实质改变结果——初始特征检测限定在遮罩形状内部
    （tracker_2d.py:252-258 `rasterize_shape_mask` → `detector.detect(..., mask=shape_mask)`）。
    调用方 tracking_service.start 用同一函数取键并直接回发缓存结果
    （tracking_service.py:148-160 `cached = self.cache.get(cache_key)` → `task_finished.emit`）。
  - 影响: 用户编辑曲线遮罩（锚点移动但外接框 pattern/search 不变，例如同包围盒内改形状）、
    素材未变（指纹相同）、帧范围/参考帧/模式/质量不变时，重新分析会命中旧键，直接返回按旧
    遮罩形状计算的跟踪结果——与当前遮罩内特征分布不符，且无任何失效信号。
  - 建议: `key_for_request` 将 `request.shape_geometry` 纳入键（如
    `json.dumps(shape_geometry, sort_keys=True)` 后哈希，None 与空分别编码）。
  - 置信度: 高（键构造、服务调用、检测结果依赖三处源码均已核对）

- [AUD-37411] [Suggestion] compute_media_fingerprint 在调用线程做容器探测，无超时保护
  - 位置: src/shared/tracking/tracking_cache.py:115-129
  - 证据:
    ```python
    try:
        import av

        with av.open(media_path) as container:
            streams = container.streams.video
    ```
    调用链：`TrackingService.start()`（调用方线程，通常为 UI 线程槽）→ `self._fingerprint(...)` →
    本函数（tracking_service.py:139, 249-253）。
  - 影响: av.open 需打开容器并读流头，本地文件通常毫秒级，但网络盘/慢速媒体/损坏容器时会阻塞
    调用线程（UI 线程卡顿）；`s.frames` 对部分容器为 None 时仅是指纹变弱（f-string 拼入 "None"），
    无崩溃。函数自身无共享状态、线程安全没问题，问题只在同步阻塞的调用位置。
  - 建议: 将指纹计算挪入 worker 预备阶段，或对本地文件走纯 os.stat 快路径、仅必要时探测容器。
  - 置信度: 中（阻塞时长依赖媒体与存储；UI 线程调用位置已核实）

### 通过项
- LRU 语义：get 命中 `move_to_end`、put 后按 `popitem(last=False)` 淘汰至容量，正确
- 键完整性的其余维度：算法版本（TRACKING_ALGORITHM_VERSION）纳入键，版本升级自动失效；
  浮点区域 round(…, 6) 规避抖动合理
- 线程契约：类 docstring 明示「只在 UI 线程访问」，实际写路径 `_on_finished` 为排队信号槽
  （UI 线程），一致
- 异常路径：`os.stat` OSError → 空串按未命中处理；av 探测失败有 noqa 注明的降级
  （"probe-failed"），未掩盖领域错误
- 指纹设计权衡（大小+mtime+容器元信息，不做内容哈希）已在 docstring 说明
- GPU/GLSL/HDR/信号/undo：无涉及

---

## src/shared/tracking/types.py (行数: 241)

### 发现

- [AUD-37412] [Suggestion] from_dict 必填键与枚举值不容错，损坏/新版本数据反序列化直接抛异常
  - 位置: src/shared/tracking/types.py:193-202
  - 证据:
    ```python
    return TrackTransform(
        frame=int(data["frame"]),
        matrix=tuple(float(v) for v in data["matrix"]),
        ...
        status=TrackStatus(data.get("status", "valid")),
        failure_reason=FailureReason(data.get("failure_reason", "none")),
    )
    ```
  - 影响: `data["frame"]`/`data["matrix"]` 缺失抛 KeyError；未知 status/failure_reason 字符串抛
    ValueError。TrackTransform 经 `TrackingBinding.from_dict` 进入工程加载路径，单条坏数据会阻断
    整个跟踪数据集的反序列化（与 AUD-37401 同根因，此处是逐帧条目粒度，坏一帧炸全库）。
    现有校验（matrix 长度 9、confidence 范围，line 116-120）是有效拦截，但错误以异常上抛而非降级。
  - 建议: 解析失败的单帧降级为 `TrackTransform.failed(frame, FailureReason.NONE)` 或跳过并计数，
    枚举未知值回退默认 + warning。
  - 置信度: 中（异常路径确凿；触发依赖损坏或跨版本持久化数据）

### 通过项
- 线程安全：frozen dataclass / Enum，不可变，可安全跨线程传递（模块契约达成）
- 构造校验：`__post_init__` 校验 matrix 9 元素与 confidence ∈ [0,1]（NaN 因比较链为 False 亦被拒）
- `from_matrix` 分解正确性：纯旋转时 shear_x = atan2(-sinθ, cosθ)+θ = 0（已推导）；sx 退化
  （<1e-12）时 rotation/shear 安全回退 0
- `failed()` 工厂：单位阵 + 零置信 + 指定原因，符合「失败必须有原因」约定
- `matrix_np` 每次新建数组，无共享可变状态泄漏
- 枚举查找表（min_points/recommended_points）覆盖全部成员，无 KeyError 路径
- GPU/GLSL/HDR/信号/undo：无涉及
