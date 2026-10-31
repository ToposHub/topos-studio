# batch-373 审查报告 — src/shared/tracking//

- 批次：batch-373 [P2]，编号区间 AUD-37300–AUD-37399
- 审查日期：2026-09-28
- 文件数：8（1,454 行）
- 结论汇总：Critical 0 / Major 4 / Suggestion 9
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失

---

## src/shared/tracking/__init__.py (行数: 18)

### 发现

（无）

### 通过项
- 通过（无逻辑）：纯 re-export 层，从 `corner_pin` 导出 `build_corner_pin_export` / `validate_corner_pin` / `write_corner_pin_export` 并在 `__all__` 声明，与包 docstring 声明的"纯 numpy/cv2/stdlib + src.shared"依赖约束一致。
- 无 Qt / GPU / 信号 / 状态提交逻辑，无风险面。

---

## src/shared/tracking/coordinate_map.py (行数: 141)

### 发现

- [AUD-37300] [Major] ClipFrameMapper.timeline_to_source_frame 边界闭区间与 TimelineClip 语义不一致，可返回越界 source frame
  - 位置: src/shared/tracking/coordinate_map.py:126
  - 证据:
    ```python
    def timeline_to_source_frame(self, timeline_frame: int) -> int:
        """timeline frame -> source frame。"""
        timeline_end = self.timeline_start + self.timeline_duration
        if not (self.timeline_start <= timeline_frame <= timeline_end):
            raise ValueError(
                f"Timeline frame {timeline_frame} outside clip range "
                f"[{self.timeline_start}, {timeline_end}]"
            )
        relative = timeline_frame - self.timeline_start
        return self.source_start + int(relative * self.speed)
    ```
  - 影响: docstring 自述"语义与 TimelineClip 一致"，但 `TimelineClip.timeline_to_source_frame`（src/features/edit/core/timeline_v2.py:373）用开区间 `timeline_frame < timeline_end`。本类在 `timeline_frame == timeline_start + timeline_duration`（TimelineClip 视为越界的那一帧）仍然放行：speed=1.0 时 `relative = source_duration`，返回 `source_start + source_duration` = `source_end + 1`，即一个超出片段源范围的帧号且不报错。下游 `tracking_evaluator.evaluate_geometry_at_timeline` 依赖该映射（幸有 `binding.contains_frame` 二次拦截），任何没有二次校验的调用方会拿到假帧号。
  - 建议: 把边界校验改为 `timeline_frame < self.timeline_start + self.timeline_duration`（与 TimelineClip 的开区间一致）；或在文档中显式声明差异并让 `clip_local_to_source_frame` 拒绝 `clip_local_frame >= timeline_duration`。
  - 置信度: 高

- [AUD-37301] [Suggestion] 归一化→像素方向的两个函数不做尺寸正数校验，与反向函数不对称
  - 位置: src/shared/tracking/coordinate_map.py:28
  - 证据:
    ```python
    def norm_point_to_pixel(x: float, y: float, width: int, height: int) -> Point:
        """归一化点 -> 像素点。"""
        return x * float(width), y * float(height)


    def pixel_point_to_norm(x: float, y: float, width: int, height: int) -> Point:
        """像素点 -> 归一化点。"""
        if width <= 0 or height <= 0:
            raise ValueError("帧尺寸必须为正")
    ```
  - 影响: `pixel_point_to_norm` / `pixel_region_to_norm` 校验尺寸为正，而 `norm_point_to_pixel` / `norm_region_to_pixels` 不校验；传入 0 或负尺寸会静默产出全 0 / 镜像坐标，错误在上游扩散后才暴露。
  - 建议: 两个正向函数补上同样的 `width <= 0 or height <= 0` 校验（或抽出公共断言）。
  - 置信度: 高

### 通过项
- 纯函数 + frozen dataclass，无共享可变状态，无线程安全问题。
- 无 GPU/OpenGL、无信号、无 GLSL 逻辑（维度不适用）。
- `ClipFrameMapper.__post_init__` 校验 source_end >= source_start 与 speed 非 0；帧号越界抛带上下文的 ValueError，异常路径清晰。
- `clamp_region_to_frame` / `region_contains` 边界处理正确（min/max 夹取、容差包含判定）。
- 类型注解完整。

---

## src/shared/tracking/corner_pin.py (行数: 354)

### 发现

- [AUD-37302] [Major] build_corner_pin_export 缺少矩阵有限性防护，单个坏帧的 ValueError 会让整次导出失败
  - 位置: src/shared/tracking/corner_pin.py:311
  - 证据:
    ```python
        if transform.status != TrackStatus.FAILED:
            corners = transform_corners(
                transform.matrix, pattern, source_width, source_height
            )
            validation = validate_corner_pin(corners, reference)
            entry["corners"] = [{"x": float(x), "y": float(y)} for x, y in corners]
    ```
    而同文件旧接口 `build_corner_pin`（105-113 行）先做防护再跳帧：
    ```python
        matrix = transform.matrix_np
        if not np.all(np.isfinite(matrix)):
            skipped.append(frame)
            continue
        corners = corner_positions(matrix, pattern_rect_px)
    ```
  - 影响: `transform_corners`（213 行）在矩阵含 NaN/Inf 或齐次除零时抛 `ValueError("单应性投影出现非有限值或齐次除零")`。tracker_2d 仅对 HOMOGRAPHY 模式做 `check_homography_stability` 有限性检查（tracker_2d.py:587-591），TRS/AFFINE 模式的 WEAK/VALID 变换未做该检查；一旦某一帧矩阵非有限，`build_corner_pin_export` / `write_corner_pin_export` 整体抛异常，导出全失败（color_controller 的调用点以 try/except 兜底成错误提示，但本应只跳过坏帧）。与旧接口"失败帧不输出坐标、不造假数据"的约定不一致。
  - 建议: 在调用 `transform_corners` 前增加 `np.all(np.isfinite(np.asarray(transform.matrix)))` 检查，非有限帧按 missing/failed 条目处理（`include_missing` 语义），与 `build_corner_pin` 的 skip 行为对齐。
  - 置信度: 高

- [AUD-37303] [Suggestion] export_corner_pin_json 不创建父目录，与 write_corner_pin_export 行为不一致
  - 位置: src/shared/tracking/corner_pin.py:158
  - 证据:
    ```python
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    ```
    对比 `write_corner_pin_export`（351-352 行）：
    ```python
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
    ```
  - 影响: 用户在导出对话框输入带新目录的路径时，旧入口直接 `FileNotFoundError`，新入口能成功；两个导出入口行为不一致。
  - 建议: `export_corner_pin_json` 同样先 `path.parent.mkdir(parents=True, exist_ok=True)`。
  - 置信度: 高

- [AUD-37304] [Suggestion] validate_corner_pin 在参考四角退化时报出误导性原因"透视过度"
  - 位置: src/shared/tracking/corner_pin.py:258
  - 证据:
    ```python
        try:
            ref_area = abs(polygon_signed_area(reference_corners))
        except (TypeError, ValueError):
            ref_area = 0.0
    area_ratio = area / ref_area if ref_area > 1e-12 else 0.0
    ...
    if reference_corners is not None and area_ratio < min_area_ratio:
        return CornerPinValidation(False, area, area_ratio, True, True, "透视过度，接近侧面")
    ```
  - 影响: 参考四角本身退化（面积为 0 / 格式错误）时 `area_ratio` 被置 0.0，诊断信息显示"透视过度，接近侧面"，把参考帧数据问题误报为当前帧透视问题，误导排障。
  - 建议: ref_area 退化时单独返回 `reason="参考四角退化"` 类原因，或在通过校验前先判 `ref_area > 1e-12`。
  - 置信度: 中

- [AUD-37305] [Suggestion] 公共导出函数的 binding 参数缺类型注解
  - 位置: src/shared/tracking/corner_pin.py:72
  - 证据:
    ```python
    def build_corner_pin(
        binding,
        pattern_rect_px: Rect,
        frame_start: Optional[int] = None,
        frame_end: Optional[int] = None,
    ) -> Dict:
    ```
  - 影响: `build_corner_pin` / `export_corner_pin_json` / `build_corner_pin_export` / `write_corner_pin_export` 的 `binding` 参数均无注解（同文件 `merge.py` 已示范应注为 `TrackingBinding`）；IDE/静态检查无法校验调用方传参，且 `path`/`output_path` 参数也未注解（应为 `Union[str, Path]`）。
  - 建议: 统一注解为 `binding: TrackingBinding`、`path: Union[str, Path]`。
  - 置信度: 高

### 通过项
- `corner_positions` / `transform_quad` 对非有限值与齐次除零（`|wz| < 1e-9`）都有防护，不输出爆炸坐标。
- 角序常量 `CORNER_ORDER` 固定 (TL, TR, BR, BL)，导出 JSON 含 `corner_order` / `frame_size` / `skipped`，失败帧不造假数据（与模块 docstring 约定一致）。
- `polygon_signed_area` / `polygon_is_convex` 形状校验（reshape (4,2)）正确；CCW 顶点序判定与角序约定一致（图像 y 向下坐标系中 TL→TR→BR→BL 叉积为正）。
- JSON 写入用 `ensure_ascii=False, indent=2`，坐标 round 4 位，无精度/clamp 问题；纯 CPU 模块，不涉及 GPU/HDR/信号维度。
- 无状态纯函数（除文件写入），无线程安全问题。

---

## src/shared/tracking/feature_detector.py (行数: 125)

### 发现

- [AUD-37306] [Major] 分桶均匀化后的全局截断按桶序而非响应序，会整桶丢弃点、破坏"均匀分布"目标
  - 位置: src/shared/tracking/feature_detector.py:121
  - 证据:
    ```python
        selected = [pt for bucket in buckets.values() for pt in bucket]
        selected_arr = np.asarray(selected, dtype=np.float32)
        if len(selected_arr) > cfg.max_features:
            selected_arr = selected_arr[: cfg.max_features]
        return selected_arr
    ```
  - 影响: `goodFeaturesToTrack` 按响应降序返回，分桶后 `buckets` 的插入序 = 首个强响应点所在桶先入。桶容量 12 × 48 桶 = 576 上限 > `max_features`=240；纹理丰富场景必然触发截断，此时排在前面的桶（强纹理区）整桶保留、后插入的桶（弱纹理区）被整桶丢弃。这与模块 docstring "保证几何求解的点位分布"的目标直接矛盾，单应性/仿射求解在弱纹理象限失去支撑点，降低几何约束质量。
  - 建议: 截断改为按桶配额（`max_features` 均摊到非空桶，逐桶轮转取点），或全局按响应排序后在桶配额约束下选择，保证每个非空桶至少贡献 1 个点。
  - 置信度: 高

- [AUD-37307] [Suggestion] mask 参数文档语义（替代 region）与实现（仍夹回 region 矩形）矛盾
  - 位置: src/shared/tracking/feature_detector.py:91
  - 证据:
    ```python
            mask: 与 gray 同尺寸的 uint8 掩码（255 = 可检测）。传入时
                替代 region 矩形掩码——跟踪复用 Mask 窗口形状时，特征
                点只落在遮罩形状内部；None 时退化为 region 矩形。
    ...
        points[:, 0] = np.clip(points[:, 0], x0, x1)
        points[:, 1] = np.clip(points[:, 1], y0, y1)
    ```
  - 影响: `tracker_2d.track`（tracker_2d.py:257）传入的 `shape_mask` 是全帧光栅化的形状掩码，与 `pattern_ana`（用户 pattern 区域）相互独立。当形状超出 pattern 矩形时，掩码内检测到的点被逐坐标夹到 region 边界上，多个点塌缩到同一条边，既违反"点在 mask 内"的文档承诺又产生退化几何。
  - 建议: 明确语义为 `mask ∩ region`：检测掩码取 `mask` 与 region 矩形的交集（`np.zeros` 帧上先填 region 再 `& mask`），或修正 docstring 并在 clip 前过滤掉 region 外的点（而非夹取）。
  - 置信度: 中

- [AUD-37308] [Suggestion] mask 仅校验尺寸与非空，未校验 dtype/通道数
  - 位置: src/shared/tracking/feature_detector.py:69
  - 证据:
    ```python
        if mask is not None and (
            mask.shape[:2] != (frame_h, frame_w) or not (mask > 0).any()
        ):
            # 形状掩码与灰度图尺寸不符或全空：退化为矩形，宁可比崩溃好
            mask = None
    ```
  - 影响: `shape[:2]` 匹配但通道数 > 1 或 dtype 为 bool/float 的掩码能通过该守卫，`cv2.goodFeaturesToTrack` 要求单通道 uint8，会抛出原始 `cv2.error` 而非按预期"退化为矩形"。当前唯一生产调用方 `rasterize_shape_mask` 返回 uint8（shape_mask.py:59），属外部输入加固缺口。
  - 建议: 守卫补充 `mask.ndim == 2 and mask.dtype == np.uint8`（不满足则转 uint8 或退化为矩形）。
  - 置信度: 中

### 通过项
- 区域夹取到帧内（`min/max`）且最小 8px 宽高短路返回 `(0,2)` 空数组，空结果形状约定一致。
- 掩码尺寸不符/全空时退化为矩形并注释理由，避免崩溃（外部数据验证维度基本通过，见 AUD-37308 的 dtype 缺口）。
- 参数全部集中在 `DetectorConfig`，无散落魔法数字；`detect` 无共享可变状态，实例可安全在单工作线程复用。
- 性能模式合理：oversample + 桶限流的复杂度为 O(N)。

---

## src/shared/tracking/frame_sampler.py (行数: 269)

### 发现

- [AUD-37309] [Suggestion] scale 只按宽度计算，代理尺寸各边独立取偶导致 y 向存在系统性映射误差
  - 位置: src/shared/tracking/frame_sampler.py:183
  - 证据:
    ```python
        src_w, src_h, _, _ = self.probe_info()
        h, w = gray.shape[:2]
        scale = (w / src_w) if src_w > 0 else 1.0

        sample = SampledFrame(
            frame_index=frame_index,
            ...
            scale=float(scale),
    ```
    配合 `_proxy_size`（236-249 行）：
    ```python
        scale = max_side / float(longest)
        return _even(int(round(src_w * scale))), _even(int(round(src_h * scale)))

    def _even(v: int) -> int:
        v = max(2, int(v))
        return v + (v % 2)
    ```
  - 影响: `SampledFrame.scale` 文档为"均匀缩放系数"，但 `_even` 对宽高各自独立向上取偶，两轴实际缩放比可差出 1 个代理像素；`scale` 只取 `w/src_w`，帧底部 y 坐标换算回原始分辨率时误差可达约 `src_h/proxy_h` 个源像素（4K→1080 代理约 2px），与 merge 的 4px 冲突阈值同量级，系统性偏向 y 向。
  - 建议: 保留各轴精确缩放（`SampledFrame` 存 `scale_x`/`scale_y`），或 `_proxy_size` 保证两轴使用同一缩放比并接受奇数边（swscale 支持奇数目标尺寸时）。
  - 置信度: 中

### 通过项
- 线程约定明确（docstring 声明仅跟踪工作线程使用）；LRU 缓存与解码器字典无跨线程共享，composite 页在 worker 内 `with FrameSampler(...)` 使用，符合约定。
- 资源生命周期正确：`close()` 关闭全部解码器、清缓存、置 `_closed`；`__enter__/__exit__` 上下文管理；关闭后再读取抛 `TrackingFrameError`。关闭失败的宽 `except Exception` 带 `noqa` 注释且 log（不吞领域错误，属合理的关闭路径防御）。
- 异常路径规范：`read_frame` 的 ValueError/Exception 分别转译为带上下文的 `TrackingFrameError`；负帧号、空 RGB、探测失败均有明确错误。
- HDR/高位深处理正确：uint16 按位深归一到 0-255，float 帧做有限值收集 + 1%/99% 分位拉伸 + `nan_to_num` + clip，最后统一 uint8（跟踪只需要亮度结构，合理，不涉及渲染 clamp 红线）。
- `media_fingerprint` 对文件不存在返回空串（OSError 捕获），路径/大小/mtime/头部字节组合合理。
- 输入验证：`source_path` 空串抛 ValueError；`cache_size` 至少为 1。

---

## src/shared/tracking/homography_stability.py (行数: 193)

### 发现

- [AUD-37310] [Suggestion] summarize_diagnostics 参数类型为 Dict[int, object]，靠 getattr 鸭子取值且重复求值
  - 位置: src/shared/tracking/homography_stability.py:160
  - 证据:
    ```python
    def summarize_diagnostics(diagnostics: Dict[int, object]) -> dict:
        """逐帧诊断汇总，供面板显示：内点数、重投影误差、条件数等。"""
        values = list(diagnostics.values())
        ...
        errors = [
            float(getattr(d, "reproj_error", 0.0)) for d in values
            if math.isfinite(float(getattr(d, "reproj_error", 0.0)))
        ]
    ```
  - 影响: 入参实为 `tracker_2d.FrameDiagnostics`（含 inlier_count/reproj_error/confidence/condition_number 字段），却注解为 `object` 并用 getattr 默认值兜底——字段拼错或类型不符时静默取 0，汇总面板显示假 0 值而非报错；`getattr` 在推导中对每个元素求值两次。
  - 建议: 注解改为 `Dict[int, "FrameDiagnostics"]`（TYPE_CHECKING 导入避免循环依赖），直接属性访问；`HomographyDiagnostics` 与 `FrameDiagnostics` 也可考虑共享 Protocol。
  - 置信度: 高

### 通过项
- "绝不输出爆炸矩阵"的防护链完整：非有限矩阵 → 条件数 LinAlgError 兜底 inf → 齐次除零（|w|<1e-9）→ 面积比上下限 → 凸性/顶点序，全部返回结构化诊断而非异常。
- `_is_convex_ccw` 用连续叉积同号判定（有向面积为正不足以排除凹形），实现正确；TL→TR→BR→BL 顶点序在 y 向下坐标系叉积为正，与角序约定一致。
- 参考面积 < 1.0 时拒绝（"pattern 区域过小"），输入验证充分。
- 阈值全部常量化并可被 TrackerConfig 覆盖，无魔法数字。
- 纯函数无状态，无线程安全/资源问题。

---

## src/shared/tracking/merge.py (行数: 204)

### 发现

- [AUD-37311] [Suggestion] merge_into_binding 无条件置 has_forward/has_backward=True，与实际方向无关
  - 位置: src/shared/tracking/merge.py:202
  - 证据:
    ```python
    for frame, transform in merge_result.transforms.items():
        binding.apply_result(
            transform, merge_result.observations.get(frame)
        )
    binding.has_forward = True
    binding.has_backward = True
    return len(merge_result.conflicts)
    ```
  - 影响: `merge_bidirectional` 不要求两个方向都有结果（backward.transforms 为空也能构造 MergeResult），合并后无论哪一方向缺数据都标记"前向/后向结果已存在"。字段语义（track_data.py:45）被放宽，UI/缓存若依赖它跳过重分析会漏跑方向；color_controller.py:908 的做法（`has_backward or TrackDirection.BACKWARD in directions`）才是按事实标记。当前该函数在生产代码中无调用点（仅测试使用），属潜在风险。
  - 建议: `merge_into_binding` 增加方向实参（forward_done/backward_done），或按 MergeResult 中参考帧前/后是否有帧推断。
  - 置信度: 中

### 通过项
- 合并语义正确：参考帧恒等矩阵（避免双重变换）；单侧覆盖直接采用；双侧覆盖用 pattern 四角平均偏差（`cv2.perspectiveTransform`）度量冲突并保留置信度高者；`deviation > threshold` 判定与 `MergeConflict` 记录供面板提示。
- `trajectory_segments` 正确处理缺帧断段（`frame != prev + 1`）与可用/失败分段，失败段不画假有效轨迹，与模块 docstring 约定一致。
- 合并不修改输入结果（不可变 TrackResult），写入绑定走 `binding.apply_result`，手动修正帧不被覆盖（track_data.apply_result 保证）。
- `_pick_observations` 空值安全；candidates 每方向至多一条（dict 键唯一），双元素解包不会越界。
- MergeResult/MergeConflict/TrackSegment 均 frozen dataclass，跨线程传递安全。

---

## src/shared/tracking/point_manager.py (行数: 150)

### 发现

- [AUD-37312] [Major] redetect_reference_points 重新从 0 编号 feature_id，锁定/排除集合仍指向旧 ID，语义错位
  - 位置: src/shared/tracking/point_manager.py:133
  - 证据:
    ```python
        obs = tuple(
            FeatureObservation(
                frame=binding.reference_frame,
                feature_id=int(i),
                x=float(p[0]) * inv,
                ...
        )
        binding.observations[binding.reference_frame] = obs
        binding.result_version += 1
    ```
    配合模块 docstring（8-9 行）："锁定/排除集合不受影响"、track_data.py:176 `lock_feature` 按 feature_id 存集合。
  - 影响: 重新检测后观测按新检测顺序从 0 重编号；此前用户锁定的 `locked_feature_ids` / 排除的 `excluded_feature_ids` 里的是旧编号，"不受影响"实际上让旧 ID 作用到任意的新点上（或指向不存在的点）。`active_observations` 的"锁定点永不被删"承诺随之失效——排除的可能是用户本想保留的物理点。当前该函数生产代码无调用（仅测试覆盖），属公共 API 的明确缺陷。
  - 建议: 重新检测前清空（或按用户确认保留）`locked_feature_ids` / `excluded_feature_ids`；或为特征点引入跨检测稳定的持久 ID（如坐标哈希）。
  - 置信度: 中

### 通过项
- `rank_observations` 评分权重参数化、`confidence` 夹取 [0,1]、`error=inf` 时 error_factor=0、排序键 `(-score, feature_id)` 稳定确定；位置因子对单点/共点集（max_radius=0）安全降级。
- `active_observations` 过滤逻辑正确：排除集中剔除、锁定集优先保留。
- `redetect_reference_points` 的资源路径正确：`FrameSampler` 在 finally 中 `close()`，异常时不泄漏解码器；坐标换算双向（归一化→源像素→分析坐标→检测结果→源像素）自洽。
- 纯函数部分无 Qt 依赖；`result_version` / `modified_at` 递增符合 track_data 写入约定。
- 类型注解完整（除 binding 为具体类 TrackingBinding 已注解）。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 4 | AUD-37300, AUD-37302, AUD-37306, AUD-37312 |
| Suggestion | 9 | AUD-37301, AUD-37303, AUD-37304, AUD-37305, AUD-37307, AUD-37308, AUD-37309, AUD-37310, AUD-37311 |
