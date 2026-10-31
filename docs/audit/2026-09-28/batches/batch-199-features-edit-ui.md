# 批次报告：batch-199 — src/features/edit/ui/edit_page.py

审查范围：AUD-19900–AUD-19999。文件 1 个，全文通读并逐维度审查；关键疑点已交叉核对
`src/features/edit/core/audio_playback.py`、`src/app/controllers/file_controller.py`、
`src/app/controllers/edit_controller.py`、`src/features/edit/ui/scene_cut_timeline_overlay.py`、
`src/features/edit/core/scene_cut/models.py`、`src/features/edit/core/timeline_v2.py`。

## src/features/edit/ui/edit_page.py (行数: 3,302)

### 发现

- [AUD-19900] [Major] 字幕轨语言设置调用不存在的方法 `_execute_subtitle_command`，功能必然失败
  - 位置: src/features/edit/ui/edit_page.py:3256
  - 证据:
    ```python
    from src.features.edit.core.captions.commands import UpdateSubtitleTrackMetadataCommand
    command = UpdateSubtitleTrackMetadataCommand.create(
        track_id, {"language": language.strip()},
    )
    self._execute_subtitle_command(command)
    ```
    全文件仅此一处出现 `_execute_subtitle_command`；该方法只定义于
    `src/app/controllers/edit_controller.py:8631`（`EditController` 类），且全仓库无任何
    `setattr(edit_page, ...)` 形式的动态注入。EditPage(QWidget) 无此方法亦无混入。
  - 影响: 用户从字幕轨头部菜单设置 BCP-47 语言并确认后，槽内抛
    `AttributeError: 'EditPage' object has no attribute '_execute_subtitle_command'`，
    命令未进 undo 栈、语言未写入模型、工程未标脏——该功能完全失效且无用户可见错误提示。
  - 建议: 在 EditPage 中实现或转发统一提交路径（如经 `TimelineView.execute_command`，
    与 EditController._execute_subtitle_command 相同的入口），或把该请求桥接到
    `_main_window` 上的既有 handler（同文件 `_on_speech_to_text_from_clip` 的模式）。
  - 置信度: 高

- [AUD-19901] [Major] 隐藏页面帧请求缺 `_preview_page_is_active()` 门控：Color 页播放可触发隐藏 Edit 预览渲染
  - 位置: src/features/edit/ui/edit_page.py:2474-2475（辅证 710、949-958）
  - 证据:
    ```python
    # set_timeline() 内，无页面激活判断：
    if previous_preview_signature is None or preview_changed or audio_changed:
        self._request_frame_at_playhead()   # → timeline_frame_requested.emit(frame)
    ```
    而外部传输路径在 Edit 隐藏时也会进入 set_timeline：
    ```python
    # start_timeline_audio_playback()，由 file_controller.py:5427 在 Color 页播放时调用
    if getattr(timeline, "timeline_id", None) != getattr(
        self._current_timeline, "timeline_id", None):
        ...
        self.set_timeline(timeline)
    ```
    同文件 `_on_playhead_changed` 对同一信号有明确门控与理由（2063-2073 行：
    "A hidden Edit page must NOT drive its own preview renderer ... the visible stutter
    on the Composite page"），但该门控未覆盖 set_timeline→`_request_frame_at_playhead`
    与 `_on_playback_state_for_audio`（710 行，state=="playing" 时无条件 emit）两处。
  - 影响: Color 页播放另一时间线的片段时，隐藏的 Edit 预览渲染器被唤醒解码同一内容，
    产生文件注释中自述的双重解码 + 渲染背压拖慢共享播放钟（可见卡顿）问题。
  - 建议: `request_current_timeline_frame` / `set_timeline` 的重渲请求与
    `_on_playback_state_for_audio` 的 start_frame emit 统一加 `_preview_page_is_active()`
    门控；on_enter 已有重渲逻辑可覆盖重新激活场景。
  - 置信度: 中（代码链路已验证；实际卡顿程度取决于 TimelinePreviewRenderer 的消费侧行为）

- [AUD-19902] [Major] 场景剪切候选叠加未绑定素材、未做源帧→时间线帧映射，标记位置错误/串轨
  - 位置: src/features/edit/ui/edit_page.py:2913-2919
  - 证据:
    ```python
    def _on_scene_cut_analysis_for_overlay(self, analysis: Any) -> None:
        """Store the latest detection candidates for the timeline overlay."""
        timeline_view = self._get_scene_cut_timeline_view()
        if timeline_view is None:
            return
        candidates = tuple(getattr(analysis, "candidates", ()) or ())
        timeline_view.set_scene_cut_candidates(candidates)
    ```
    `analysis.asset_id`（scene_cut_controller.py:68）被完全忽略；候选帧为"源媒体帧号"
    （scene_cut/models.py:570 `source_frame: Zero-based frame index in source media`），
    而叠加层直接以 `cand.source_frame` 作为时间线坐标建标记
    （scene_cut_timeline_overlay.py:669 `cand_id = str(cand.source_frame)`）。
  - 影响: 对媒体池任意素材跑场景检测，结果会叠加到当前时间线（即使该素材不在时间线上）；
    检测自时间线片段时，只要片段 source_start≠0 或素材被多轨复用，标记位置即与真实剪切点
    错位——功能展示错误信息。
  - 建议: 按 analysis.asset_id 过滤（仅当该素材出现在当前时间线时显示），并把
    source_frame 经 clip.source_start 映射为时间线帧；或明确该叠加仅在"整段素材=整段时间线"
    语义下启用。
  - 置信度: 高（edit_page 侧与 overlay 侧行为均已核实）

- [AUD-19903] [Suggestion] `create_timeline()` 为空壳：emit 空 timeline_id 并返回 ""，不创建任何时间线
  - 位置: src/features/edit/ui/edit_page.py:2544-2562
  - 证据:
    ```python
    def create_timeline(self, name: str, settings: Dict[str, Any]) -> str:
        """...
        Returns:
            Timeline ID
        """
        ...
        if 'fps' in settings:
            self._fps = float(settings['fps'])
        self.signal_hub.timeline_created.emit("", settings)
        return ""
    ```
    docstring 承诺返回 Timeline ID；实际仅副产地改 `_fps`。全仓库未发现调用方
    （实际创建走 `EditProject.create_timeline`）。
  - 影响: 潜在误用面：任何按契约调用者拿到空 ID；订阅 `timeline_created` 的组件会收到
    空串事件。当前为死代码，风险是契约性的。
  - 建议: 删除该 stub 或委托给 project 层真实创建并在成功后 emit 真实 ID。
  - 置信度: 高

- [AUD-19904] [Suggestion] `_update_audio_track_state` 死代码且穿透私有 `_mixer` 属性
  - 位置: src/features/edit/ui/edit_page.py:1900-1926（关键行 1924）
  - 证据:
    ```python
    self._audio_manager._mixer.set_track_state(track_id, new_state)
    ```
    全仓库（含本文件内部）无任何调用；其余音频同步均走 `audio_mgr` 公开 API
    （`set_track_strip_eq` / `set_track_routing` 等）。
  - 影响: 绕过管理器封装直接操作混音器内部；若保留且未来引擎重命名 `_mixer` 或置 None，
    将 AttributeError。与文件内其他路径的 API 纪律不一致。
  - 建议: 删除，或改为经 `audio_mgr` 公开接口并接入 `_sync_audio_tracks_from_timeline`
    的签名门控路径。
  - 置信度: 高

- [AUD-19905] [Suggestion] 字幕叠加路径多处静默吞异常（`except Exception: pass/return None`，无日志）
  - 位置: src/features/edit/ui/edit_page.py:2099-2104、2148-2149、2176-2177、2191-2193
  - 证据:
    ```python
    try:
        w = int(getattr(timeline, 'width', 1920) or 1920)
        h = int(getattr(timeline, 'height', 1080) or 1080)
        self._program_viewer.set_subtitle_canvas_size(w, h)
    except Exception:
        pass
    ...
    except Exception:          # _subtitle_graphic_overlay_metadata / _resolve_subtitle_overlay_pair
        return None
    ```
  - 影响: 字幕画布尺寸同步、图形模板解析、分层样式解析、字形缓存失效任一环节的真实
    领域错误（模型字段改名、注册表异常等）被完全掩盖，预览静默退化为旧渲染且无任何
    排查线索，违反 .clinerules"不得宽泛 except 掩盖领域错误"。
  - 建议: 至少统一 `logger.debug(..., exc_info=True)`；对 `resolve_style` /
    `template_parameters` 等领域函数的失败用窄化异常类型。
  - 置信度: 高

- [AUD-19906] [Suggestion] `set_fps`/`_frame_to_timecode` 缺 fps>0 防御校验
  - 位置: src/features/edit/ui/edit_page.py:2397-2416、2418-2433
  - 证据:
    ```python
    total_seconds = frame / fps
    ...
    frames = int(frame % fps)
    ...
    def set_fps(self, fps: float) -> None:
        ...
        self._fps = fps
    ```
  - 影响: fps=0（外部 settings 或畸形数据）会在每次播放头移动时抛 ZeroDivisionError。
    现状风险低：`set_timeline` 走模型 fps，模型已校验（timeline_v2.py:2975-2979）；
    但 `set_fps`/`create_timeline(settings)` 入口无校验。
  - 建议: `set_fps` 加 `fps > 0` 断言或回退默认帧率。
  - 置信度: 低（防御性；主路径已被模型层校验覆盖）

- [AUD-19907] [Suggestion] BCP-47 语言标签输入无任何校验（空串/任意文本直接入库）
  - 位置: src/features/edit/ui/edit_page.py:3246-3256
  - 证据:
    ```python
    language, accepted = QInputDialog.getText(
        self, "Caption Language", "BCP-47 language tag (e.g. zh-CN, en-US):",
        text=current,
    )
    if not accepted:
        return
    ...
    track_id, {"language": language.strip()},
    ```
    `language.strip()` 结果可为空串或任意非语言文本，无格式校验即进入命令与持久化。
  - 影响: 非法/空语言标签写入字幕轨元数据并最终进入 TTML 导出等下游。
  - 建议: 用简单 BCP-47 正则（如 `^[a-zA-Z]{2,3}(-[a-zA-Z0-9]+)*$`）校验，空值提示。
  - 置信度: 中（当前该路径还受 AUD-19900 阻断，但命令构造处的缺失独立存在）

- [AUD-19908] [Suggestion] 若干辅助函数参数/返回类型注解缺失
  - 位置: src/features/edit/ui/edit_page.py:2114、2152、2759
  - 证据:
    ```python
    def _subtitle_graphic_overlay_metadata(timeline, clip, frame: int) -> dict[str, Any] | None:
    def _resolve_subtitle_overlay_pair(timeline, clip):
    def _get_source_edit_params(self) -> tuple:
    ```
  - 影响: 与 .clinerules"所有函数有类型注解"约束不符；`-> tuple` 未参数化削弱可读性。
  - 建议: 补 `timeline: Any, clip: Any`（或精确模型类型）与 `tuple[Optional[str], Optional[int], Optional[int]]`。
  - 置信度: 高

### 通过项

- 线程安全：音频管理器在 UI 线程创建/调用（AudioPlaybackManager(parent=self)，内部线程自有）；
  WaveformExtractor 单例状态采用绑定方法接收者上下文连接（T-P1-71 修复形态正确，析构自动断连，
  edit_page.py:231-238、262-263）；scrub 合并用 QTimer 单发 + token 校验，均在 UI 线程。
- GPU 资源生命周期：本文件不直接触碰 OpenGL；清空节目画面走 viewer.clear_texture 回调（2453-2456）。
- GLSL·HDR 正确性：不涉及（无着色器/HDR 数据路径）。
- 信号防递归：scene cut overlay 开关经相等早退 + dialog 侧 blockSignals 约定（2882-2907）；
  transport 连接有 `_transport_connected` 一次性护栏（面板在 dock 装配期仅注入一次，已核对
  dock_layout_controller.py:422/433）。
- 状态提交一致性：音频图签名（AM-02/05/06/07、AT、P-clip-FX、master automation）字段覆盖完整，
  部分注册失败时 `active_track_ids != signature_track_ids` 会强制重建（1148-1165）；
  路由门（P0-02）阻断期仍同步时间基准；shutdown_audio_playback 幂等且与
  audio_playback.cleanup()（4662-4684，含 QThread join）语义匹配。
- 异常与取消路径：scrub start/finish 成对复位全部节流状态；on_leave 暂停播放并释放音频
  worker；音频各 set_* 容错降级均有窄化日志。
- 性能模式：scrub 4ms 合并 + latest-only、音频试听 60ms 节流、签名门控避免整图重建、
  `_set_thumbnail_playback` 让位策略，均合理。
- 输入验证：字幕导出目标路径有扩展名归一与文件名清洗（3151-3173）；`_get_source_edit_params`
  校验 In/Out 区间（2772-2777）。
