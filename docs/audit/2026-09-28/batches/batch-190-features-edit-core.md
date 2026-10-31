# batch-190 审查报告 — src/features/edit/core

审查日期：2026-09-28。文件均已 Read 通读（timeline_render_plan.py 全 1,593 行、timeline_thumbnail_scheduler.py 全 366 行），并对关键结论核对了消费方（timeline_preview_renderer.py / timeline_export.py / timeline_view.py）与模型层（timeline_v2.py）。编号区间 AUD-19000–AUD-19099。

## src/features/edit/core/timeline_render_plan.py (行数: 1,593)

### 发现

- [AUD-19000] [Critical] TOPOS 直通导出判定漏检 `animated_params` 关键帧，opacity/transform 动画在直通成品中静默丢失
  - 位置: src/features/edit/core/timeline_render_plan.py:1425-1434
  - 证据:
    ```python
    if getattr(clip, "transform", None) != TransformParams():
        return None, "non_identity_transform"
    if getattr(clip, "crop", None) != CropParams():
        return None, "non_identity_crop"
    opacity = getattr(clip, "opacity", 1.0)
    try:
        if abs(float(opacity) - 1.0) > 1e-6:
            return None, "non_identity_opacity"
    ```
    而本文件自己的渲染路径按关键帧求值（timeline_render_plan.py:910-943）：
    ```python
    transform_tuple = (
        float(clip.get_param_at_frame("transform.translate_x", sample_frame)),
        ...
    animated_opacity = float(clip.get_param_at_frame("opacity", sample_frame))
    ```
    且 `ClipInstance.get_param_at_frame` 中关键帧优先于静态值（timeline_v2.py:770-778 `if param_name in self.animated_params: return ...get_value_at_frame(frame)`；`with_keyframe_added` 仅更新 `animated_params`，不改静态 `opacity`/`transform` 字段，timeline_v2.py:794-833）。全函数（1323-1593 行）没有任何 `animated_params` 检查。
  - 影响: 一个带 opacity 淡入关键帧（或 transform 位移动画）但静态 `opacity == 1.0`、静态 `transform == TransformParams()` 的单一 TOPOS clip 会通过 eligibility 判定，导出走 `_export_direct_topos` 逐帧直通复制源帧——成品完全丢失动画，属静默的导出渲染错误（用户无任何提示，仅走了快路径）。
  - 建议: eligibility 中增加 `if getattr(clip, "animated_params", None): return None, "animated_params_present"`（至少对渲染语义相关的 `opacity` 与 `transform.*` 键判定）。
  - 置信度: 高

- [AUD-19001] [Major] 转场一侧 clip 被禁用时，转场覆盖帧整轨无任何图层输出（应为启用侧回落普通渲染）
  - 位置: src/features/edit/core/timeline_render_plan.py:828-862（叠加 1019-1023）
  - 证据:
    ```python
    if transition is not None:
        trans_info = self._compute_transition_blend(transition, track, frame)
        if trans_info is not None:
            transitions.append(trans_info)
            ...
            if left_clip is not None:
                left_layer = self._create_layer_info_for_clip(...)
    ...
    else:
        # 无转场：查找覆盖该帧的单个 clip
        clip = self._find_clip_at_frame(track, frame)
    ```
    而 `_compute_transition_blend` 任一侧禁用即整体返回 None（1019-1023 行）：
    ```python
    if (left_clip is not None and not left_clip.is_enabled) or (
        right_clip is not None and not right_clip.is_enabled
    ):
        return None
    ```
    `trans_info is None` 时既不追加 layers 也不回落 `_find_clip_at_frame`。模型层各 `with_*` 方法无条件保留 `transitions=self.transitions`（timeline_v2.py:2494/2514/2547/2566/2608），禁用 clip 不会级联删除转场，该状态可达。
  - 影响: 用户禁用转场一侧 clip 后，启用侧 clip 在转场区间帧（如 centered transition 的左半段，帧仍在启用 clip 体内）渲染为空——画面黑帧/露出下层轨道，直到转场结束帧。逐帧预览与导出均受影响。
  - 建议: `trans_info is None` 时回落到无转场分支（`_find_clip_at_frame` + `_create_layer_info_for_clip`），或 `_compute_transition_blend` 在一侧禁用时降级为仅启用侧的普通层而非返回 None。
  - 置信度: 高

- [AUD-19002] [Major] 宽泛 `except Exception` 掩盖 `project.get_asset_by_id` 真实错误并误报拒绝原因
  - 位置: src/features/edit/core/timeline_render_plan.py:1441-1447
  - 证据:
    ```python
    asset = None
    if project is not None:
        try:
            asset = project.get_asset_by_id(clip.asset_id)
        except Exception:
            asset = None
    if asset is None or str(getattr(asset, "media_type", "")) != "video":
        return None, "non_video_clip"
    ```
  - 影响: `get_asset_by_id` 内部的任何 bug（KeyError、状态损坏等）被静默吞掉并降级为 "non_video_clip" 资格拒绝：导出静默放弃直通快路径走慢路径，日志中的 reason 码误导排查方向。违反铁律"不得宽泛 except 掩盖领域错误"。（对照：1475-1476 行的 probe 补全 `except Exception` 带 `noqa: BLE001` 与 fail-closed 注释，属可接受；此处无任何记录。）
  - 建议: 收窄为 project API 声明的领域异常；无法收窄时至少 `logger.debug(..., exc_info=True)` 记录，并使用独立 reason 码（如 `asset_lookup_failed`）区分"资产确实不是 video"。
  - 置信度: 高

- [AUD-19003] [Suggestion] 类内缓存（轨道排序 clip / solo / decode stream key）无失效钩子，正确性完全依赖消费者整体重建实例
  - 位置: src/features/edit/core/timeline_render_plan.py:592-619、679-697、699-725
  - 证据: `clear_cache()`（614-619 行）在 src/ 全库无调用者（仅定义处一处命中）；缓存键为 `track.track_id`：
    ```python
    def _get_sorted_clips(self, track: EditTrack) -> tuple[ClipInstance, ...]:
        if track.track_id not in self._track_clip_cache:
            sorted_clips = tuple(sorted(track.clips, key=lambda c: c.timeline_start))
            self._track_clip_cache[track.track_id] = sorted_clips
        return self._track_clip_cache[track.track_id]
    ```
    当前正确性依赖 timeline_preview_renderer.py:10042/10095/10128/10284 在序列变化时置 `_render_plan = None` 重建整个实例。同文件 `_transition_index`（438-444 行）已示范了更稳的"按 transitions 元组身份失效"模式。
  - 影响: 公共核心 API 的隐性契约——任何长期持有实例、且在模型（不可变快照）演进后继续调用的消费者都会拿到过期 clip 列表/solo 状态/decode key，渲染旧内容。属潜在风险（现有两个消费者均重建）。
  - 建议: 缓存键升级为内容签名（如 `track.clips` 元组身份 + solo 标志），与 `_TransitionIndex` 一致；或在类 docstring 明示"模型变化必须重建/clear_cache"。
  - 置信度: 高（机制确定，现有消费者恰好规避）

- [AUD-19004] [Suggestion] `_find_clip_at_frame` 二分查找隐含"轨道 clip 互不重叠"前置条件，重叠时覆盖帧可能漏检
  - 位置: src/features/edit/core/timeline_render_plan.py:976-989
  - 证据:
    ```python
    while left < right:
        mid = (left + right) // 2
        clip = sorted_clips[mid]
        if frame < clip.timeline_start:
            right = mid
        elif frame >= clip.timeline_end:
            left = mid + 1
        else:
            return clip
    ```
    经典区间 bisect：若同轨出现重叠（如 A=[0,10)、B=[2,3)，frame=5 → mid 命中 B，`frame >= B.timeline_end` → left=2 → 返回 None，漏掉覆盖该帧的 A）。
  - 影响: 仅当模型允许同轨重叠（如拖拽/修整中间态未归一化）时触发——覆盖帧被当作空隙。若模型已严格保证不重叠则无影响。
  - 建议: 在方法 docstring 声明前置条件；或二分失败后对相邻小窗口线性兜底。
  - 置信度: 低（未证实模型允许重叠）

- [AUD-19005] [Suggestion] dip_to_color 的 `blend_color` 未做类型转换与数值域验证
  - 位置: src/features/edit/core/timeline_render_plan.py:1050-1065
  - 证据:
    ```python
    if isinstance(color_params, dict):
        blend_color = (
            color_params.get("r", 0.0), ...
    elif isinstance(color_params, (list, tuple)) and len(color_params) >= 3:
        blend_color = tuple(color_params[:3])
    ```
    dict 分支不 `float()`、不验证 0.0-1.0 域；list/tuple 分支原样保留，若外部参数以 0-255 整数表示（如 `[255, 0, 0]`），下游按 0-1 混合将得到过曝/纯白转场。`transition.params` 属外部可持久化数据。
  - 影响: 非法/异构颜色参数不报错，直接产生视觉错误的 Dip To Color（违反"外部数据须验证"）。
  - 建议: 统一 `float()` 转换并验证域（>1.0 时按 0-255 归一化或拒绝并回退默认黑）。
  - 置信度: 中（取决于参数生产方格式，未证实存在 0-255 写入者）

- [AUD-19006] [Suggestion] fade/opacity 有效值计算在两处重复实现，存在漂移风险
  - 位置: src/features/edit/core/timeline_render_plan.py:930-943 与 1114-1127
  - 证据: `_create_layer_info_for_clip` 与 `_get_clip_effective_opacity` 各自维护同一段 `fade_in/fade_out + animated_opacity` 逻辑（逐行几乎相同，仅一处额外 `max(0.0, min(1.0, ...))` 收尾位置不同）。
  - 影响: 未来修 fade 边界（如首帧 fade=0）只改一处会导致转场层与普通层透明度不一致。
  - 建议: 提取模块级 `compute_effective_opacity(clip, sample_frame)` 共享。
  - 置信度: 高

- [AUD-19007] [Suggestion] `ClipLayerInfo.transform` 使用裸 `tuple` 注解，未表达 8 元素结构
  - 位置: src/features/edit/core/timeline_render_plan.py:143
  - 证据: `transform: tuple  # translate_x, translate_y, scale_x, scale_y, rotation, pivot_mode, pivot_x, pivot_y`——注释承载类型信息；相邻 `crop` 已用 `tuple[float, float, float, float, float]`。
  - 影响: 消费者（渲染器）无法静态校验元素数与类型，pivot_mode 的 str/枚举类型不明确。
  - 建议: `tuple[float, float, float, float, float, str, float, float]`（与 TransformParams 字段类型对齐）。
  - 置信度: 高

### 通过项

- GPU 资源生命周期：纯 CPU 计划模块，不接触 ModernGL/纹理/缓冲。
- GLSL·HDR 正确性：无着色器代码；浮点字面量均显式 `.0`（ease 曲线 512-519、aspect/fps 容差 1539-1548）；模块不对 HDR 像素数据做 clamp（opacity/blend 的 0-1 clamp 属语义必需范围）。
- 信号防递归：不依赖 PyQt，无信号连接。
- 状态提交一致性：纯查询接口，无 undo/redo/revision/持久化职责。
- `_TransitionIndex` 二分 + 前缀最大 end 剪枝：算法与历史线性扫描语义一致（含重叠转场按声明序取首个），`prefix_max_end` 单调性保证回扫终止正确；空转场表、frame 早于首个 start、无候选（`prefix_max_end[p] <= frame`）三类边界均已正确短路（469-485 行）。
- WeakKeyDictionary 索引缓存：EditTrack 为无 slots 的 frozen dataclass（timeline_v2.py:2168-2169），可弱引用；`transitions` 元组替换即重建，失效正确（438-444 行）。
- 线程安全：不可变数据类；已知消费者要么持 QMutex 使用（timeline_preview_renderer.py:10402-10411），要么每次导出新建实例（timeline_export.py:2812）。类级 `_TRANSITION_INDEX_CACHE` 无锁，并发最坏重复构建索引，无功能影响。
- 直通判定输入验证（除 AUD-19000/19002 外）：未识别 planar/CFA 格式、源 fps 未知、源帧数不足、尺寸/宽高比不符、位深与 metadata 矛盾均 fail-closed 拒绝（1499-1556 行）；除法均有 `max(...,1)` / `not fps_den` 保护；CFA RAW 车道对位深/尺寸不一致严格拒绝（1489-1532 行）。
- 异常与取消路径：转场边界帧采样 `calculate_source_frame` 抛 ValueError 时回退 `clip.source_start`（1037/1047 行）；`_clips_share_forward_decode_cursor` 捕获 ValueError 收敛为 False（749-750 行），均为窄域捕获。
- `calculate_transition_blend`：duration<=0 防护、progress 钳制 0-1、曲线字典分支默认 linear，正确。
- `get_transition_at_frame`/`is_track_renderable`/`find_clips_in_range` 等查询接口：边界（frame<首区间、track_id 未命中）返回 None/(0,0)/空元组，一致。

## src/features/edit/core/timeline_thumbnail_scheduler.py (行数: 366)

### 发现

- [AUD-19008] [Major] `_completed_cache` 缩略图缓存无容量上限，且 `clear_cache` 在全库无调用者
  - 位置: src/features/edit/core/timeline_thumbnail_scheduler.py:183、356-366
  - 证据:
    ```python
    self._completed_cache: dict[tuple[str, int, int], object] = {}  # (asset_id, frame, height) -> pixmap
    ...
    def cache_thumbnail(self, asset_id: str, source_frame: int, height: int, pixmap: object) -> None:
        """Cache a completed thumbnail."""
        self._completed_cache[(asset_id, source_frame, height)] = pixmap
    ```
    模块内仅 `clear_cache()` 可清空，而 `_thumb_scheduler.clear_cache` 在 src/ 全库无调用（grep 无命中）；消费者 timeline_view.py:2821 经 `cache_thumbnail` 写入、2847 行把该缓存当 ClipItem 的 "L2 cache" 读取。
  - 影响: 缓存条目按 (资产, 帧, 高度) 只增不减——长会话下多资产胶片缩略图（每条 64px 高 pixmap）持续累积，无 LRU/数量/字节上限，构成明确的内存增长风险（Topos 长时间编辑会话场景）。
  - 建议: 改用 OrderedDict 实现 LRU 上限（参照同目录 timeline_render_plan 的 `_frame_cache` 模式），或提供按 asset 失效接口并在时间线重建时调用。
  - 置信度: 高（机制确定；实际增长速率取决于缩略图管线活跃度）

- [AUD-19009] [Suggestion] viewport 派发路径为死代码：`_on_batch_callback` 从未被触发、`_debounce_pending` 从未被读写、`update_viewport` 全库无调用者
  - 位置: src/features/edit/core/timeline_thumbnail_scheduler.py:184-185、191-193、209-245
  - 证据:
    ```python
    self._on_batch_callback: Callable[[list[TimelineThumbnailJob]], None] | None = None
    self._debounce_pending: bool = False
    ...
    def update_viewport(self, visible_clips: list[dict], ...) -> list[TimelineThumbnailJob]:
    ```
    消费方 timeline_view.py:836 `set_batch_callback(self._on_thumbnail_batch_ready)` 之后调度器内部从不调用该回调；timeline_view.py:2750-2757 的 `_submit_viewport_thumbnails` docstring 明言 "The former scheduler also decoded individual frames for the same clips, doubling disk/CPU work"——缩略图已改由 ClipItem 密度感知管线负责。模块头 docstring（19-34 行）的 "Typical usage" 仍描述这套已废弃的集成方式。
  - 影响: 误导后续维护者按 docstring 接线（重新引入双路解码）；`TimelineThumbnailJob`/`ThumbnailBatch`/`compute_visible_source_frames` 整条路径当前无产品调用。
  - 建议: 删除派发路径（保留 generation/is_job_valid/cache L2 职责），或更新模块 docstring 说明现状，避免死代码复活。
  - 置信度: 高

- [AUD-19010] [Suggestion] `update_viewport` 的 `_pending_jobs` 以 clip_id 为键，同 clip 的 filmstrip 多 job 只保留最后一个
  - 位置: src/features/edit/core/timeline_thumbnail_scheduler.py:243
  - 证据:
    ```python
    # Store pending
    self._pending_jobs = {j.clip_id: j for j in jobs}
    ```
    而 `_compute_clip_jobs` filmstrip 模式每 clip 最多生成 `THUMBNAIL_MAX_PER_VISIBLE_CLIP`(20) 个 job（305-335 行）。
  - 影响: pending 集合语义失真（注释声称 clip_id -> job）；当前路径无调用者，属潜伏缺陷，一旦复活即现。
  - 建议: 键改为 `(clip_id, source_frame)` 或存 list。
  - 置信度: 高

- [AUD-19011] [Suggestion] `_compute_clip_jobs` 未验证 `source_duration <= 0`，start_end/filmstrip 模式可生成越界源帧 job
  - 位置: src/features/edit/core/timeline_thumbnail_scheduler.py:297、317
  - 证据:
    ```python
    source_frame=source_start + source_duration - 1,   # start_end 末帧
    ...
    frame = source_start + int(t * (source_duration - 1))  # filmstrip
    ```
    `source_duration` 仅经 `.get("source_duration", 1)` 取默认，显式传入 0 时不被拦截（`visible_width <= 0` 检查只看像素）→ 生成 `source_start - 1` 的解码请求。对照同模块 `compute_visible_source_frames` 129 行有 `if clip_total_px <= 0 or source_duration <= 0` 防护。
  - 影响: 越界帧解码请求下发到 decode worker（无效帧/异常路径）；当前路径未接线，风险潜在。
  - 建议: 入口处 `source_duration = max(1, int(source_duration))` 或提前返回。
  - 置信度: 高（缺陷确定，路径当前未激活）

- [AUD-19012] [Suggestion] `update_viewport` 的 `viewport_width` 参数未被使用
  - 位置: src/features/edit/core/timeline_thumbnail_scheduler.py:209-227
  - 证据: 函数签名接收 `viewport_width: int`，函数体（229-245 行）从未引用该参数；调用方文档亦要求传入。
  - 影响: 死参数误导调用者，掩盖"密度只由 clip 可见宽度决定"的真实行为。
  - 建议: 删除参数或纳入密度计算。
  - 置信度: 高

- [AUD-19013] [Suggestion] 同模块两处 `thumb_w` 常量算法不一致（整数 `16 // 9` vs 浮点 `16.0/9.0`）
  - 位置: src/features/edit/core/timeline_thumbnail_scheduler.py:141 与 267
  - 证据:
    ```python
    thumb_w = max(48, THUMBNAIL_DEFAULT_HEIGHT * 16 // 9)      # = 113 (int)
    ...
    aspect_ratio = 16.0 / 9.0
    thumb_w = max(48, THUMBNAIL_DEFAULT_HEIGHT * aspect_ratio) # = 113.78 (float)
    ```
  - 影响: `compute_visible_source_frames` 与 `_compute_clip_jobs` 对同一 clip 在边界宽度处会得出不同的缩略图数量，两条路径结果不收敛；也违反"浮点字面量显式 .0"的字面一致性（141 行整型运算）。
  - 建议: 提取模块级常量 `THUMBNAIL_DEFAULT_WIDTH = 114`（或统一为一个浮点表达式）。
  - 置信度: 高

### 通过项

- generation 失效语义：`update_viewport`/`invalidate` 递增 generation、`set_playback_active(True)` 清 pending，`is_job_valid` 严格相等比较，stale job 丢弃正确；消费方（timeline_view.py:2800/2818）在异步回调中先验后用，模式正确。
- 输入验证（`compute_visible_source_frames`）：`clip_total_px <= 0` / `source_duration <= 0` 防护、采样帧 `max(source_start, min(frame, source_start + source_duration - 1))` 双向钳制（128-153 行），正确。
- 线程安全：实例状态为 dict/int 基本操作；消费方在 Qt 信号回调（UI 线程）调用 `is_job_valid`/`cache_thumbnail`，未见跨线程裸共享。
- GPU 资源生命周期：不接触 ModernGL；pixmap 生命周期由外部管理（本模块仅持有引用）。
- 信号防递归：无 PyQt 依赖、无信号。
- 状态提交一致性：无 undo/redo/持久化职责，纯调度状态机。
- 异常与取消路径：stale 结果经 `is_job_valid` 丢弃；`set_playback_active` 正确释放 pending，符合 playback 优先级设计。
