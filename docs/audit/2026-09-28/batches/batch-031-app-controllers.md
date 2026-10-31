# batch-031 审查报告 [P1] src/app/controllers//

- 任务书: docs/audit/2026-09-28/assignments/batch-031.md
- 编号区间: AUD-3100–AUD-3199（本报告使用 AUD-3100–AUD-3113）
- 审查日期: 2026-09-28
- 文件已全量通读（1–3601 行，分多次 Read，无跳读）。

## src/app/controllers/page_controller.py (行数: 3,600)

PageManagementMixin（混入 MainWindow，见 src/app/main_window.py:333），负责页面切换、页面上下文协调、viewport 恢复、Composite/Color 进入绑定与播放传输条接线。

### 发现

- [AUD-3100] [Major] Composite 时间线传输条 13 个槽闭包每次进入页面重复连接且从不拆除旧连接
  - 位置: src/app/controllers/page_controller.py:1400-1424（配合 1211-1243 的部分拆除、543-545 的每次 enter 调用）
  - 证据:
    ```python
    for signal_name, slot in control_connections:
        signal = getattr(bar, signal_name, None)
        if signal is None or slot is None:
            continue
        while True:
            try:
                    signal.disconnect(slot)
            except TypeError:
                break
        signal.connect(slot)
    ```
    `control_connections` 中的 `_on_seek/_on_play/...` 是本次调用新建的闭包（1260-1398 行），对从未连接过的新闭包 `disconnect` 必然立即抛 TypeError，while 循环恒空转后 `connect`。函数开头只拆除并清空 `frame_slot/state_slot/kf_state_slot`（1214-1235 行），13 个 transport 闭包既不存储也不拆除。`bar`（`composite_viewer.video_control_bar`，composite_viewport.py:1332 一次性创建）与 viewer 跨页面切换持久存在，而 `enter_page_hook` 每次进入 Composite 都调用本函数（543-545 行）。
  - 影响: 第 N 次进入时间线-backed Composite 后，播放/暂停/seek/scrub 等按钮信号连接了 N 套闭包。`_on_play` 是 toggle 语义，同一按钮点击连续触发两次时：第一次 `pause()`，第二次见 `is_playing=False` 又 `play()`——播放按钮表现为"无法暂停"。且 `enabled=False` 分支（1237-1243 行）提前 return，不拆除旧 transport 闭包，与 `_configure_composite_comp_clip_controls` 的 13 个闭包并存时（时间线/comp-clip 两条路径交替进入），`controller.seek` 与 `page.comp_seek` 双重触发，信号连接无界累积。
  - 建议: 仿照同文件 comp-clip 路径的做法（1584-1599 行 `_comp_clip_bar_slots` 的先存后拆），把这 13 个 (signal_name, slot) 存入 `self._composite_controls_transport_slots`，在每次接线前与 `enabled=False` 分支统一 disconnect。
  - 置信度: 高

- [AUD-3101] [Major] `_show_color_timeline_clip_static_preview` 重试耗尽后进入不设上限的 100ms 无限重试环
  - 位置: src/app/controllers/page_controller.py:2389-2416
  - 证据:
    ```python
    elif not rendered:
        # ... exact_seek 尝试 ...
        QTimer.singleShot(
            100,
            lambda: self._show_color_timeline_clip_static_preview(
                context,
                retry_count=retry_count,
                token=token,
            ),
        )
    ```
    正常重试链（2380-2388 行）以 `retry_count + 1` 递增并有 `< 8` 上限；耗尽后的 `elif` 分支用相同 `retry_count` 重新调度自身。注释声称该重试"only for legacy controllers which do not implement the exact-seek interface"，但代码并未检查 `exact_seek` 是否存在——实现了 `request_static_preview_frame` 却持续返回 False 的控制器（解码器卡死/损坏文件）会走 `elif`，而该分支里的 `exact_seek` 调用不改变 `rendered`，于是每 100ms 一次 `seek_to_frame(source_frame, force_display=True)` 永远循环。
  - 影响: 静态预览永远失败时形成 10Hz 强制 seek 风暴，持续施压解码 worker 与显示路径，仅在新预览请求（token 变化，2256-2260 行）或开始播放（2271-2273 行）时才被打断。
  - 建议: 该分支仅在 `exact_seek` 不可用（真 legacy）时保留重试，并给遗留重试同样设置硬上限；实现了精确 seek 的控制器应在一次 `force_display` seek 后放弃并记日志。
  - 置信度: 高（控制流确定；触发依赖控制器持续返回 False）

- [AUD-3102] [Major] Edit→Color 切换路径在 UI 线程同步执行 PyAV 容器探测
  - 位置: src/app/controllers/page_controller.py:3072-3085（`_update_color_monitor_for_asset`，由 `_handle_enter_color_with_context`:1972 调用）
  - 证据:
    ```python
    if not (transfer or primaries):
        path = getattr(asset, "path", None)
        if path:
            from src.shared.media.factory import MediaSourceFactory
            meta = MediaSourceFactory.probe_video_metadata(path)
            if meta is not None:
                transfer = transfer or getattr(meta, "color_transfer", None)
    ```
    `probe_video_metadata` 委托 `probe_video_metadata_cached`（factory.py:705-718），首次未命中时同步打开容器（PyAV demux open）。
  - 影响: 资产 metadata 缺失 color tag 时（每个文件首次打开），页面切换在 UI 线程阻塞于磁盘/网络 I/O 与 demux 打开，4K/网络媒体可卡顿数百毫秒到数秒，违背实时页面切换体验。写回 asset.metadata 缓存只能保证每文件一次。
  - 建议: 探测移至后台线程（复用既有异步探测基础设施），就绪后经信号回填 `canvas.update_color_monitor` 与 `asset.metadata`；首次未命中时先用占位/默认 tag 显示。
  - 置信度: 高（同步调用链已核实）

- [AUD-3103] [Major] 播放头跟随的视频轨分层约定在两条路径中相互矛盾
  - 位置: src/app/controllers/page_controller.py:3044-3048（`_switch_to_color_following_edit_playhead`） vs 1760-1764（`_find_clip_under_composite_playhead`）
  - 证据:
    ```python
    # 3044-3048: 注释 "the first video track covering the frame wins"
    clip = None
    for track in timeline.video_tracks:
        clip = track.get_clip_at(playhead)
        if clip is not None:
            break
    ```
    ```python
    # 1760-1764: 注释 "the highest video-track index wins; mirrors the edit preview's layering"
    video_tracks = sorted(
        getattr(timeline, "video_tracks", ()) or (),
        key=lambda t: getattr(t, "index", 0),
        reverse=True,
    )
    ```
  - 影响: 多层视频轨在同一帧重叠时，普通 Edit→Color 页签切换按 `video_tracks` 自然顺序取第一个命中（若自然顺序为升 index 即最底层），而 Composite 播放跟随取最上层 clip。同一帧经不同入口会打开不同 clip 调色/合成，且与"edit preview 的分层"二选一必有一处不一致，存在调错片段的风险。另注意 3044 路径未过滤 `is_enabled`（1767 行的跟随路径有过滤）。
  - 建议: 抽出单一 topmost-enabled-clip 解析工具（含 `is_enabled` 过滤与排序约定），两条路径共用。
  - 置信度: 中（两处约定矛盾确凿；`video_tracks` 自然顺序语义在本文件外，未最终确认）

- [AUD-3104] [Major] `_apply_composite_preview_color_space` 用宽泛 except 将色彩管线失败降级为 DEBUG 日志
  - 位置: src/app/controllers/page_controller.py:880-884
  - 证据:
    ```python
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug(
            f"preview color-space apply failed (non-fatal): {e}"
        )
    ```
  - 影响: try 块覆盖 descriptor 构建、HDR 源探测、tonemap 配置与 `set_display_space` 整条预览 ODT 链（820-879 行）。任何领域错误（配置损坏、枚举映射失败、viewer 拒绝参数）都被静默降为 debug 级，Viewer 以错误 transfer/gamut 显示 HDR 内容而无任何用户可见诊断，违反"不得宽泛 except 掩盖领域错误"。
  - 建议: 区分 ImportError（旧版 viewer 回退，可 debug）与领域异常（至少 warning 带堆栈）；`set_display_space` 失败时回退到 transfer-only 路径并记录。
  - 置信度: 高

- [AUD-3105] [Suggestion] HLG 源在旧版 Viewer 回退路径被静默映射为 sRGB
  - 位置: src/app/controllers/page_controller.py:873-879
  - 证据:
    ```python
    transfer_map = {
        ColorTransfer.LINEAR: 0, ColorTransfer.SRGB: 1,
        ColorTransfer.BT709: 2, ColorTransfer.PQ: 3,
    }
    self.composite_viewer.set_display_transfer(
        transfer_map.get(desc.transfer, 1)
    )
    ```
    `ColorTransfer.HLG = 'arib-std-b67'` 存在（src/shared/color/types.py:85），map 无该键，缺省取 1（sRGB）。
  - 影响: 无 `set_display_space` 的旧版 CompositeViewer 下，HLG HDR 内容按 sRGB transfer 显示且无 tonemap，预览色彩错误且无提示（仅影响旧版回退路径）。
  - 建议: transfer_map 覆盖 HLG 或缺省时记 warning 并按 BT709 近似。
  - 置信度: 高

- [AUD-3106] [Suggestion] `leave_page_hook`/`enter_page_hook` 中两处 `except Exception: pass` 完全吞掉失败
  - 位置: src/app/controllers/page_controller.py:416-420、473-479
  - 证据:
    ```python
    try:
        if hasattr(self.composite_page, "comp_pause"):
            self.composite_page.comp_pause()
    except Exception:
        pass
    ```
    ```python
    try:
        from src.shared.feature_flags import get_work_mode
        work_mode = get_work_mode(getattr(self, "config_manager", None))
        if hasattr(self.composite_page, "set_image_color_mode"):
            self.composite_page.set_image_color_mode(work_mode == "image")
    except Exception:
        pass
    ```
  - 影响: 离开 Composite 时 `comp_pause` 失败被静默，comp-clip 播放定时器可能残留并在隐藏页面上继续推进合成帧；`set_image_color_mode` 失败静默则 Image Color 模式不生效且无诊断。
  - 建议: 两处至少 `logger.warning(..., exc_info=True)`；comp_pause 失败时考虑直接停掉 `composite_page` 的播放定时器。
  - 置信度: 高

- [AUD-3107] [Suggestion] UI 线程调用 `gl_viewport.makeCurrent()` 后未配对 `doneCurrent()`
  - 位置: src/app/controllers/page_controller.py:322-332（`_restore_viewport_after_page_switch`）
  - 证据:
    ```python
    if hasattr(self.canvas, 'gl_viewport') and self.canvas.gl_viewport and self.canvas.gl_viewport.isVisible():
        self.canvas.gl_viewport.show()
        self.canvas.gl_viewport.makeCurrent()

        # Trigger LOD update
        self.canvas._update_lod()
    ```
    其后没有任何 GL 调用，也没有 `doneCurrent()`；`update()`/`scene.invalidate(BackgroundLayer)` 触发的重绘在 paint 事件中会自行 makeCurrent。
  - 影响: 与"UI 线程不得操作 OpenGL"铁律相悖的冗余上下文操作；上下文在 UI 线程保持 current，若异步渲染线程共享该上下文存在争用风险（GLCanvasViewport 为 QOpenGLWidget，见 src/features/color/ui/gl_viewport.py:14）。
  - 建议: 删除显式 `makeCurrent()`（或配对 `doneCurrent()`），保留 update/invalidate 即可。
  - 置信度: 中（Qt 层面合法，但按项目铁律与冗余性判断为缺陷）

- [AUD-3108] [Suggestion] 大量跨模块私有成员读写，封装破坏
  - 位置: src/app/controllers/page_controller.py:1130、601-606、1248、1500-1509、1371/1381、327 等
  - 证据:
    ```python
    self.composite_page._current_composite_id = "standalone_composite"   # 1130
    tex_id/w/h 来自 self.composite_page._last_texture_id / _last_texture_width ...  # 601-606
    clip_start = int(getattr(page, "_comp_clip_timeline_start", 0) or 0)  # 1248
    out_tl = getattr(controller, "_playback_range_end", None)            # 1371
    self.canvas._update_lod()                                            # 327
    ```
  - 影响: 控制层直接改写页面/控制器私有状态，重构（重命名、改为属性）极易静默破坏；`_on_mark_in/_on_mark_out` 读取控制器私有 `_playback_range_*` 而非公开 `playback_range`，语义脆弱。
  - 建议: 在 CompositePage/EditPlaybackController 上提供公开访问器（如 `last_texture_info()`、`playback_range` 元组），控制层只走公开接口。
  - 置信度: 高

- [AUD-3109] [Suggestion] 死代码：两个无调用方的私有辅助方法（约 100 行）
  - 位置: src/app/controllers/page_controller.py:1835-1882（`_should_block_implicit_composite_entry`）、2451-2499（`_show_color_timeline_clip_immediate_thumbnail_preview`）
  - 证据: 全仓 grep（`grep -rn ... src/ --include="*.py"`）两方法仅有定义处一个命中，无任何调用点（含 main_window.py 与各 mixin 宿主）。
  - 影响: 不可达逻辑（后者含 thumbnail 订阅管理整套流程）增加维护成本并误导后续修改。
  - 建议: 删除，或接回预定调用点（若属未完成功能应注明 TODO 与计划批次）。
  - 置信度: 高

- [AUD-3110] [Suggestion] 隐式 Optional 参数与缺失类型注解
  - 位置: src/app/controllers/page_controller.py:2973、3481、3555（`clip_id: str = None` / `timeline_id: str = None`）；130（`switch_to_page(self, page_name, force: bool = False)` 无 page_name 与返回注解）；358、438（leave/enter_page_hook 参数无注解）；1503-1564（comp-clip 闭包 `_on_seek(frame)`、`_on_loop(enabled)` 等参数无注解）
  - 证据:
    ```python
    def switch_from_edit_to_color(self, clip_id: str = None) -> None:
    ```
    ```python
    def switch_to_page(self, page_name, force: bool = False):
    ```
  - 影响: 与 .clinerules 强类型约束不符；隐式 Optional 在严格检查下报错。
  - 建议: 改为 `Optional[str]`/`str | None`，补齐参数与返回注解。
  - 置信度: 高

- [AUD-3111] [Suggestion] `switch_to_page_with_context` 局部导入的 `PageType` 未使用
  - 位置: src/app/controllers/page_controller.py:1897-1900
  - 证据:
    ```python
    # Import PageType locally to avoid circular imports at module level
    from src.app.main_window import PageType

    target_page = context.target_page.value
    ```
    函数体其余部分未再引用 `PageType`（对比 1951 行的导入在 1953 行有使用）。
  - 影响: 冗余导入与误导性注释（暗示本函数需要 PageType，实际不需要）。
  - 建议: 删除该导入。
  - 置信度: 高

- [AUD-3112] [Suggestion] Composite 自动绑定的播放头兜底把音频轨 clip 也作为候选
  - 位置: src/app/controllers/page_controller.py:936-942（`_auto_bind_composite_to_selected_clip`）
  - 证据:
    ```python
    if clip is None:
        for candidate_track in tuple(timeline.video_tracks) + tuple(timeline.audio_tracks):
            candidate_clip = candidate_track.get_clip_at(timeline_frame)
            if candidate_clip is not None:
                track = candidate_track
                clip = candidate_clip
                break
    ```
  - 影响: 播放头下无视频 clip 时会绑定音频 clip；通常因无 composite_id 而回退空工作区（表现为跳过），但与 `_find_clip_under_composite_playhead` 只查视频轨的约定不一致，若音频 clip 意外带 composite_id/asset 元数据将打开错误合成。
  - 建议: 兜底仅遍历 `video_tracks`（可复用 AUD-3103 建议的统一解析工具）。
  - 置信度: 中

- [AUD-3113] [Suggestion] `switch_to_page` 调用 `enter_page_hook` 无异常隔离，失败时页面停在半切换状态
  - 位置: src/app/controllers/page_controller.py:284-286（对比 3590-3596 的 `_safe_switch_to_page`）
  - 证据:
    ```python
    # Hooks for entering page
    self._page_switch_from_page = from_page
    self.enter_page_hook(page_name)
    ```
    此时 `current_page` 已更新（241 行）、布局已恢复，但 hook 内未包裹的调用（如 592 行 `self.composite_page.on_enter(...)`、550-559 行 adapter 路径）抛异常会传播到 Qt 信号层，`update_all_dock_titlebars`、viewport 恢复定时器及 `switch_to_page_with_context` 的后续 `_handle_enter_*_with_context` 全部被跳过（后者仅 `_contextual_page_switch_in_progress` 标志有 finally 复位，1906-1910 行）。
  - 影响: enter 钩子任一未防护路径失败时 UI 停在半切换状态（页面标识已变但内容/上下文未初始化），且无统一日志。
  - 建议: `enter_page_hook(page_name)` 调用包 try/except 并 `logger.exception`，或统一走 `_safe_switch_to_page` 的隔离模式。
  - 置信度: 中

### 通过项

- 信号防递归：调色栈 hydration 前后正确设置/清除 `_color_stack_hydration_in_progress` 状态锁（2220-2224），`_run_color_stack_hydration_step` 保存并恢复先前状态支持嵌套（2756-2761）；brush/picker 的程序化复位仅在状态实际变化时发信号，语义即"关闭工具"，无递归风险。
- GPU 资源生命周期：本文件不直接创建/释放 GL 对象；texture id/尺寸/revision 仅作传递，color_output 无效或脏时统一走 `request_color_output()`（550-559、571-579）。
- GLSL·HDR 正确性：文件内无 GLSL 代码；HDR 预览参数字面量规范（`target_peak = 100.0`，861-864 行），HDR 源探测覆盖 PQ/HLG tag（846-859 行）。
- 状态提交一致性：切换前调用 `_flush_active_grading_stack_before_switch`（2014-2016、2217-2219）与 `_flush_composite_to_edit_before_leave`（421-425）；`open_timeline_clip_in_color` 在 `_ensure_clip_has_color_stack` 改写不可变时间线后重新取 clip/stack（3259-3265），继承性 clip_local stack 的重绑定判定完整（3239-3252）。
- 异常与取消路径（除 AUD-3101/3104/3106 外）：`_show_color_timeline_clip_static_preview` 的 token 失效检查防止旧请求复活（2256-2260）；`_disarm_color_clip_switch_guard` 在各中止路径显式撤防避免黑屏（2262-2282、2418-2434）；`_restore_current_composite_workspace` 全程 try/except 且失败路径返回 False 走降级。
- 输入验证：`open_timeline_clip_in_color` 对 project/timeline/clip/asset/媒体类型逐级校验并提前返回（3140-3168）；`_create_standalone_image_color_session` 拒绝视频文件并抛出文档化异常（2783-2798）；`_metadata_int` 对外部 metadata 做类型容错（3292-3301）。
- 线程安全（除 AUD-3107 外）：全部状态访问在 UI 线程；渲染触发统一走 `engine.render_pipeline`/`request_color_output` 异步管线，无跨线程共享可变状态。
- 性能模式（除 AUD-3102 外）：调试日志统一经 `_emit_color_clip_debug` 门控（98-105）；clip strip 刷新用 80ms 单发防抖定时器并有完整清理（3412-3479）；`_configure_composite_*` 复用单一 viewer/bar 实例避免重复构建。
