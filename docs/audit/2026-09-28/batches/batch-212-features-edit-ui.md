# 批次报告：batch-212 — src/features/edit/ui//

任务书：docs/audit/2026-09-28/assignments/batch-212.md
编号区间：AUD-21200–AUD-21299
审查文件：1 个

---

## src/features/edit/ui/timeline_layout.py (行数: 872)

纯布局引擎（无 Qt 信号发射、无 OpenGL、无线程），核心问题集中在：字幕轨道存在时视频滚动偏移被绕过、持久化恢复不校验外部数据、以及一批死代码/验证缺口。以下所有跨文件结论（timeline_view.py 行为）均已用只读 grep/sed 验证。

### 发现

- [AUD-21200] [Major] 存在字幕轨道时 video_scroll_offset 被完全忽略，溢出场景下视频滚动条变死条、轨道绘制进标尺区
  - 位置: src/features/edit/ui/timeline_layout.py:338-348
  - 证据:
    ```python
    video_ordered = list(reversed(self._video_tracks))
    full_video_stack_h = self._stack_height(video_ordered)
    if self._subtitle_tracks:
        y = self._area.subtitle_splitter_rect.bottom()
    elif full_video_stack_h <= self._area.video_area.height():
        y = self._area.splitter_rect.top() - full_video_stack_h
    else:
        max_scroll = max(0.0, full_video_stack_h - self._area.video_area.height())
        offset = max_scroll if self._state.video_scroll_offset < 0 else self._state.video_scroll_offset
        offset = max(0.0, min(max_scroll, float(offset)))
        y = self._area.video_area.top() - offset
    ```
  - 影响: 只要 `self._subtitle_tracks` 非空就走第一分支，`video_scroll_offset`（含 -1 底部对齐语义和滚动条写入值）不再参与布局。而 `scroll_range_for_area("video")`（本文件 374-387 行）仍按 `stack - video_area.height()` 报告非零 max_scroll——当字幕+视频栈总高超过 `max_video_h = usable - MIN_AREA_HEIGHT`（`_compute_areas` 276-278 行，viewport≈600px 时约 7 条默认 82px 视频轨 + 2 条字幕轨即触发）时，timeline_view.py:1483 `can_scroll = maximum > minimum` 会启用视频滚动条，用户拖动只改 state 不改布局（死滚动条）；同时 `_compute_areas` 294 行 `subtitle_top = va_splitter_top - video_group_h` 会小于 `TRACK_AREA_TOP`，字幕/最上方视频轨被画到标尺之上且无任何滚动手段可见，`track_at_y_with_area` 对该区域返回 "ruler"，命中判定与可见内容矛盾。
  - 建议: 字幕分支也应套用与无字幕分支相同的 offset 钳制逻辑（以 `subtitle_splitter_rect.bottom()` 为锚点向下滚动，或改为组内各自滚动）；并在 `_compute_areas` 中钳制 `subtitle_top >= TRACK_AREA_TOP`（或按可用高度截断 group）。
  - 置信度: 高（分支逻辑直接可见；溢出阈值由常量推算）

- [AUD-21201] [Major] TimelineLayoutState.from_dict 不校验外部持久化数据，损坏的工程文件可在加载时崩溃、越界比率直接破坏布局
  - 位置: src/features/edit/ui/timeline_layout.py:113-124
  - 证据:
    ```python
    @classmethod
    def from_dict(cls, data: dict | None) -> "TimelineLayoutState":
        if not data:
            return cls()
        return cls(
            video_area_ratio=float(data.get("video_area_ratio", 0.5)),
            subtitle_area_ratio=float(data.get("subtitle_area_ratio", SUBTITLE_AREA_RATIO)),
            track_heights=dict(data.get("track_heights") or {}),
            video_scroll_offset=int(data.get("video_scroll_offset", -1)),
            ...
    ```
  - 影响: 数据源是工程持久化 JSON（timeline_view.py:2486/2497 `layout_state()` → UIState → 磁盘 → `set_layout_state` → `from_dict`），属外部输入。字段为字符串（如 `"abc"`）时 `float()/int()` 抛 ValueError/TypeError，工程加载流程崩溃；`video_area_ratio=7.5` 这类越界值不钳制（对比 `set_splitter_ratio` 605-609 行有 0.1..0.9 钳制），`usable * ratio` 使视频区远超视口；`track_heights` 值为 str 时在 `_height_for`（389-400 行）`min/max` 比较处 TypeError。违反 .clinerules「外部数据须验证」。
  - 建议: from_dict 逐字段 try/except (TypeError, ValueError) 回退默认值；比率恢复到 [0.1,0.9]/[0.05,0.6]；track_heights 逐值 `int()` 转换并丢弃非法项。
  - 置信度: 高

- [AUD-21202] [Suggestion] subtitle_area_ratio 为死状态：被写入、持久化、参与重配置签名，但布局计算从不读取
  - 位置: src/features/edit/ui/timeline_layout.py:611-615
  - 证据:
    ```python
    def set_subtitle_splitter_ratio(self, ratio: float) -> None:
        """P10.3: Set subtitle area ratio (fraction of non-video remainder)."""
        ratio = max(0.05, min(0.6, ratio))
        self._state.subtitle_area_ratio = ratio
        self._dirty = True
    ```
    （`_compute_areas` 243-314 行中字幕区高度只由 `_stack_height` + `usable * 0.4` 上限推导，全程未读 `self._state.subtitle_area_ratio`；`subtitle_splitter_ratio_from_y` 617-630 行同样只写不读。）
  - 影响: 唯一调用链是 timeline_view.py:4768-4772 `_handle_splitter_drag` 的 `"subtitle"` 分支，但 `_splitter_drag_kind` 只会被赋值为 `"video"`（timeline_view.py:646/4757），且 `_handle_splitter_press` 对 S/V 分隔条直接 `return False`——该分支不可达。结果：`to_dict` 持久化一个恢复后无任何效果的值（误导性持久化），`_configure_layout_engine` 签名（timeline_view.py:1078）包含它导致无意义重配置；`subtitle_splitter_ratio_from_y` 中 `2.0 * SPLITTER_HEIGHT` 与 `_compute_areas` 的单 SPLITTER_HEIGHT 口径也不一致。
  - 建议: 删除 `set_subtitle_splitter_ratio`/`subtitle_splitter_ratio_from_y` 与 state 字段（含 to_dict/from_dict 键），或恢复其在 `_compute_areas` 中的作用；保留则需在注释中明确标注"仅持久化占位"。
  - 置信度: 高

- [AUD-21203] [Suggestion] resolve_timeline_hit 未检查 S/V 分隔条，且实现顺序与文档化优先级矛盾
  - 位置: src/features/edit/ui/timeline_layout.py:705-721
  - 证据:
    ```python
    Priority order: splitter > track > empty > ruler > header > invalid.
    ...
    # 1. Ruler area (above track area)
    if scene_y < TRACK_AREA_TOP:
        ...
        return TimelineHit(area="ruler", frame=frame)

    # 2. Splitter (highest priority in track zone)
    sr = self._area.splitter_rect
    if sr.top() <= scene_y < sr.bottom():
    ```
  - 影响: docstring 声明 splitter 最高优先，实现却先判 ruler；且只查 V/A `splitter_rect`，未查 `subtitle_splitter_rect`（对比 `is_in_splitter` 582-592 行两者都查），S/V 分隔条命中会落到 "empty"/"between tracks"，与 `is_in_splitter`/`is_in_subtitle_splitter` 口径不一致。当前生产代码未调用此 API（仅 tests/edit/test_timeline_hit_and_drop.py 使用），属潜在隐患。
  - 建议: 对齐 `is_in_splitter` 的判定（先两个 splitter 再 ruler/track），并修正 docstring。
  - 置信度: 高

- [AUD-21204] [Suggestion] set_pixels_per_frame/configure 不拒绝 pixels_per_frame ≤ 0，x_to_frame 将除零崩溃
  - 位置: src/features/edit/ui/timeline_layout.py:212-217, 568-570
  - 证据:
    ```python
    def set_pixels_per_frame(self, ppf: float) -> None:
        if self._pixels_per_frame != ppf:
            self._pixels_per_frame = ppf
    ...
    def x_to_frame(self, x: float) -> int:
        """Convert scene X to frame number (rounded to nearest frame)."""
        return round((x - TRACK_HEADER_WIDTH) / self._pixels_per_frame)
    ```
  - 影响: ppf=0 时 `x_to_frame` 抛 ZeroDivisionError。现有唯一生产调用方 timeline_view 经 `set_zoom` 钳制（MIN_ZOOM=0.01，timeline_view.py:365/2275），暂不触发；但本模块自我定位为"独立、完全可测试的单一事实源"，任何新调用方传入 0/负值即崩溃。负 ppf 也不被拒绝，会产生镜像坐标系。
  - 建议: `set_pixels_per_frame`/`configure` 中 `if ppf <= 0: return`（或 raise ValueError）。
  - 置信度: 高（缺口确定，触发路径为潜在）

- [AUD-21205] [Suggestion] track_at_y 在矩形循环内反复重建 track_id 集合，鼠标移动热路径 O(N×M)
  - 位置: src/features/edit/ui/timeline_layout.py:471-478
  - 证据:
    ```python
    for tid, rect in self._track_rects.items():
        if rect.top() <= scene_y < rect.bottom():
            if area == "video" and tid in {t.track_id for t in self._video_tracks}:
                return tid
            if area == "audio" and tid in {t.track_id for t in self._audio_tracks}:
                return tid
            if area == "subtitle" and tid in {t.track_id for t in self._subtitle_tracks}:
                return tid
    ```
  - 影响: 每个命中的矩形都构造一次集合；该函数服务于鼠标移动命中测试，轨道数增多时在拖拽/悬停期间产生不必要的重复分配。正确做法可直接调用 `track_at_y_with_area` 已返回的 track_id（本函数 468 行已调用它却丢弃其 track_id 结果，再自己重算一遍）。
  - 建议: `tid, area = self.track_at_y_with_area(scene_y); return tid`，或把三个 id 集合提到循环外。
  - 置信度: 高

- [AUD-21206] [Suggestion] configure() 就地修改调用方 TrackInfo 并持有外部可变引用，绕过 _dirty 缓存的风险由调用方承担
  - 位置: src/features/edit/ui/timeline_layout.py:194-207
  - 证据:
    ```python
    self._tracks = tracks
    ...
    self._state = state
    ...
    # Fill default heights
    for t in self._tracks:
        if t.default_height == 0:
            if t.track_type == "audio":
                t.default_height = DEFAULT_AUDIO_TRACK_HEIGHT
    ```
  - 影响: 引擎直接改写入参对象的字段（副作用），并保存调用方的 list/state 引用——外部直接改 `state.track_heights[...]` 或 tracks 内容不会置 `_dirty`，缓存矩形可能与真实状态不一致（陈旧布局）。当前调用方 timeline_view.py:1060-1068 每次同步都新建 TrackInfo，故为潜在风险而非现行缺陷。
  - 建议: 在 configure 内 `tracks = list(tracks)` 并复制 TrackInfo（或文档化"传入后所有权归引擎、外部不得再改"）。
  - 置信度: 高（行为确定，现行无触发）

- [AUD-21207] [Suggestion] clip_id_at 注解 "callable | None" 不是合法类型；多处 dict 注解未参数化
  - 位置: src/features/edit/ui/timeline_layout.py:693, 102, 114
  - 证据:
    ```python
    clip_id_at: "callable | None" = None,
    ...
    def to_dict(self) -> dict:
    ...
    def from_dict(cls, data: dict | None) -> "TimelineLayoutState":
    ```
  - 影响: `callable` 是内置函数而非类型，字符串注解虽不运行时报错，但 mypy/pyright 会报 invalid type，IDE 补全与签名文档失真；`-> dict` / `dict | None` 未参数化，弱化本模块"强类型"约定。
  - 建议: `Callable[[float, float, str], Optional[str]]`（typing 导入），dict 注解参数化为 `dict[str, object]`。
  - 置信度: 高

- [AUD-21208] [Suggestion] resolve_drop_target 将一切未知 media_type 静默映射为 audio
  - 位置: src/features/edit/ui/timeline_layout.py:773
  - 证据:
    ```python
    desired_type = "video" if media_type in ("video", "image", "comp_clip", "motion_graphic") else "audio"
    ```
  - 影响: 传入拼写错误或新增类型（如 `"subtitle"`、`"vidoe"`）不会报错，而是被当作音频轨目标——拖放会静默吸附到 A 轨，属静默错误行为（.clinerules 输入验证精神）。
  - 建议: 显式枚举合法值，未知值返回 `is_valid=False, reason="Unsupported media type"` 或 raise ValueError。
  - 置信度: 中（当前调用方传值均合法，风险为演进型）

### 通过项

- 线程安全：纯计算模块，无 Qt 信号、无线程/锁交互，仅 UI 线程使用，无数据竞争面
- GPU 资源生命周期：不涉及任何 ModernGL/OpenGL 资源
- GLSL·HDR 正确性：不涉及着色器与 HDR 数据
- 信号防递归：本模块不发射 Qt 信号，无递归风险
- 异常与取消路径：无宽泛 except 掩盖领域错误（全文件无 except）；只读查询对未知 track_id 返回安全默认值（track_rect→空 QRectF、track_top→TRACK_AREA_TOP）
- frame↔X 映射一致性：frame_to_x/x_to_frame/frames_to_width 与 TRACK_HEADER_WIDTH 偏移互逆一致，zoom 只影响 X 不影响 Y（符合设计规则）
- 缓存策略：_ensure_computed 的 dirty 标记 + 矩形缓存模式正确，set_viewport_size/set_state/set_track_height 均正确置脏
- 高度钳制：_height_for 按轨道类型分别钳制到 MIN/MAX 区间，字幕轨遵循 SUB-P1-06 紧凑区间
