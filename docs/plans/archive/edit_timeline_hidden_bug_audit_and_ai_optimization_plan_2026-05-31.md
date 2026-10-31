# Edit Timeline Hidden Bug Audit and AI Optimization Plan

审计日期：2026-05-31  
项目：Topos Color  
范围：剪辑页时间线 UI、轨道创建与轨道头、视频/声音素材移动、片段剪辑、多轨播放、Edit 页 GPU 解码渲染链路。

## 0. 审计结论

时间线已经有比较完整的骨架：不可变时间线模型、命令栈、轨道布局引擎、ClipItem/TrackHeaderItem、TimelinePreviewRenderer、EditGpuDecodeManager、EditGpuPreviewRenderer 都已经存在。

但目前仍有几类高风险隐藏 BUG：

1. 命令栈 undo/redo 状态不完整，多个轨道头命令、删除轨道、重命名、RippleDelete、Slip、MacroCommand 会出现撤销无效或撤销丢状态。
2. A/V 联动不是严格原子操作，视频素材插入后音频可能被自动挪到其他时间点，导致刚导入就音画不同步；视频成功、音频失败时也可能留下半成品。
3. 跨轨拖拽、多选/联动拖拽路径存在错误命令选择，拖到已有轨道时可能误创建新轨道。
4. speed 字段在代码里同时被当成 float 和 SpeedParams 使用，split/trim/slip/ripple 路径容易崩溃或算错源帧。
5. 轨道创建/移动/删除/重排时没有保留 volume，轨道音量会静默重置。
6. Blade、Split All、轨道头开关、音量拖动等 UI 交互存在“看似能用但模型不同步/撤销不对”的问题。
7. GPU 全链路尚未稳定打通：组件存在且有连接入口，但实际应用会大量回落 CPU/QImage；GPU 成功路径绕开 TimelinePreviewRenderer 的 pending/cache/generation 管理，存在 stale frame、重复跳过请求、纹理竞争和测试误判风险。

建议先修 P0/P1（数据一致性和命令栈），再修 P2/P3（交互和轨道），最后打通 P4/P5（GPU 全链路与播放同步）。不要先做视觉微调。

## 1. 重点文件

| 模块 | 文件 | 重点 |
|---|---|---|
| 时间线数据模型 | `src/features/edit/core/timeline_v2.py` | `ClipInstance`、`EditTrack`、`TimelineSequence` |
| 时间线操作函数 | `src/features/edit/core/timeline_ops.py` | split/trim/move/slip/slide/ripple 等纯操作 |
| 命令栈 | `src/features/edit/core/commands.py` | undo/redo、MacroCommand、轨道命令、剪辑命令 |
| 轨道布局 | `src/features/edit/ui/timeline_layout.py` | track hit test、drop target、轨道显示顺序 |
| 时间线主 UI | `src/features/edit/ui/timeline_view.py` | 拖拽、剪辑、drop、轨道头回调、选择、增量刷新 |
| 时间线图元 | `src/features/edit/ui/timeline_items.py` | ClipItem、TrackHeaderItem、鼠标事件、波形/缩略图 |
| 时间线面板 | `src/features/edit/ui/timeline_panel.py` | 播放控制、时间线 View 容器 |
| Edit 页协调 | `src/features/edit/ui/edit_page.py` | playhead -> frame request |
| Program Viewer | `src/features/edit/ui/program_viewer.py` | QImage 上传、共享纹理显示 |
| CPU/GPU 预览控制 | `src/features/edit/core/timeline_preview_renderer.py` | CPU 合成、GPU 提交、缓存、generation |
| GPU 解码管理 | `src/features/edit/core/edit_gpu_decode_manager.py` | VT zero-copy / planar / CPU fallback |
| GPU 预览渲染 | `src/features/edit/core/edit_gpu_preview_renderer.py` | shared GL context、YUV->RGB、GPU 合成 |
| 应用接线 | `src/app/controllers/file_controller.py` | TimelinePreviewRenderer 初始化、GPU pipeline 初始化 |

## 2. 隐藏 BUG 清单

| 编号 | 严重级别 | 现象/风险 | 证据位置 | 建议修复 |
|---|---:|---|---|---|
| B01 | Critical | `MacroCommand.apply()` 执行子命令时会处理 `(timeline, updated_command)`，但没有把 updated 子命令保存回 Macro。依赖 apply 时写入 undo 状态的子命令，在 Macro undo 时会丢状态。 | `commands.py` `MacroCommand.apply` | MacroCommand 返回 `(new_timeline, updated_macro)`；CommandStack 推入 updated_macro；添加 linked trim、batch delete、ripple delete 的 undo 测试。 |
| B02 | Critical | `SetTrackMuteCommand`、`SetTrackSoloCommand`、`SetTrackLockCommand`、`SetTrackVolumeCommand`、`RenameTrackCommand`、`DeleteTrackCommand` apply 中记录 old 值但没有写回命令对象，undo 多数会 no-op 或警告。 | `commands.py` track command classes | 全部改成返回 `(timeline, updated_command)`；或用非 frozen 命令并统一约束。优先保持现有 CommandStack 的 tuple 协议。 |
| B03 | Critical | `SlipClipCommand.apply()` 注释说返回 updated command，但实际只返回 timeline。undo 需要 `old_source_start`，缺失时会失败。 | `commands.py` `SlipClipCommand.apply` | 返回 updated command，并测试直接创建命令未传 old 值时的 undo。 |
| B04 | Critical | `RippleDeleteCommand.apply()` 创建了 updated_cmd 但返回的是 timeline，undo 拿不到 `_deleted_clip/_affected_moves`；undo 内部移动逻辑也有可能 no-op。 | `commands.py` `RippleDeleteCommand` | 返回 `(timeline, updated_cmd)`；重写 undo 为“先恢复被删 clip，再按记录反向移动 affected clips”。 |
| B05 | Critical | `SplitClipCommand.apply()` 使用 `clip.speed.speed`，但项目里很多 ClipInstance 的 speed 是 float；会在 split 时崩溃。 | `commands.py` `SplitClipCommand`; `timeline_v2.py` speed 字段 | 引入 `get_effective_speed(clip)` 单一 helper，所有 split/trim/source mapping 使用同一 API。 |
| B06 | Critical | `timeline_ops.py` 多处用 `clip.speed` 参与乘法，如果 speed 是 SpeedParams 会类型错误；如果是变速曲线则源帧计算不正确。 | `timeline_ops.py` split/trim/slip/slide | 把 `ClipInstance.timeline_to_source_frame()` 设为唯一源帧映射入口；操作层只处理 timeline/source 边界，不直接乘 speed。 |
| B07 | Critical | 轨道重排/新增/移动到新轨道/删除恢复时手动重建 `EditTrack`，没有传 `volume`，导致轨道音量重置为 1.0。 | `commands.py` `AddTrackCommand`、`MoveTrackCommand`、`DeleteTrackCommand.undo`、`MoveClipToTrackCommand` | 新增 `EditTrack.with_index(index)`；禁止散落手写 EditTrack 重建；补 volume 保留测试。 |
| B08 | Critical | 添加视频素材时，视频 clip 先添加，音频 clip 后添加。音频如果找不到同起点空位会被 `_find_free_frame_on_track()` 移到其他帧，linked clip 立即音画不同步；音频失败时视频会留下半成品。 | `timeline_view.py` `_add_asset_as_clip` | 插入 A/V 必须构造一个 MacroCommand，先全量验证同起点可插入，再一次提交；失败时不改 timeline。 |
| B09 | Critical | 多选/联动拖拽到已有轨道时，部分路径使用 `MoveClipToTrackCommand.create_for_new_track()`，可能误创建新轨道，而不是移动到目标已有轨道。 | `timeline_view.py` `_on_drag_finished` | 对 `new_track_id` 分支明确区分 existing/new pseudo track；existing 必须用 `MoveClipToExistingTrackCommand`。 |
| B10 | Critical | GPU 成功路径由 `EditGpuPreviewRenderer.frame_ready` 直接连到 ProgramViewer `set_shared_texture`，没有回到 `TimelinePreviewRenderer._on_frame_rendered`。`_pending_frame` 不清、cache 不写、generation 不校验，可能 stale frame 或同帧请求被永久跳过。 | `file_controller.py` `_init_gpu_pipeline`; `timeline_preview_renderer.py` `_try_gpu_render` | GPU renderer signal 改为 `(frame, generation, tex_id, w, h)`，统一进入 TimelinePreviewRenderer，再由 `frame_ready(frame, result)` 输出。 |
| B11 | Critical | GPU final texture 是 render thread 持有的 `_composite_tex`，UI 线程直接显示共享 tex_id；下一帧复用同一纹理时可能与 UI paintGL 竞争。 | `edit_gpu_preview_renderer.py` `_composite_tracks`; `program_viewer.py` `set_shared_texture` | 改成双缓冲/三缓冲 output texture 池，加 GL fence 或显式生命周期 ACK；UI 不应显示正被下一帧写入的纹理。 |
| B12 | High | `TimelinePreviewRenderer` CPU 路径仍以 numpy/cv2/QImage 为主，`_render_frame()` 注释写着 MVP 禁用 GPU；ProgramViewer `set_frame()` 也把 QImage 作为 primary path。 | `timeline_preview_renderer.py` `_render_frame`; `program_viewer.py` `set_frame` | 明确 CPU fallback 和 GPU path 的优先级；GPU path 通过统一信号返回 texture tuple；CPU path 仅作为 fallback 且有可观测指标。 |
| B13 | High | `test_edit_gpu_pipeline.py` 把 `EditGpuDecodeManager.get_frame()` 第二返回值当 bool，但现在返回字符串 `'gpu'/'planar'/'cpu'`；`if is_gpu:` 对 `'cpu'` 也为 True，可能误判。 | `tests/edit/test_edit_gpu_pipeline.py` | 测试改成 `frame_type == "gpu"`；提交 task 时带 `frame_type`；输出判定分别统计 gpu/planar/cpu。 |
| B14 | High | Blade 点击没有按 y 命中轨道，只要某帧落在任意 track clip 内，就分割第一个命中的 clip；点击某轨可能切到别的轨。 | `timeline_view.py` `_handle_blade_click` | 使用 `TimelineLayoutEngine.resolve_timeline_hit()`，只切当前 hit track 的 clip；Alt/Shift 才执行 all tracks。 |
| B15 | High | `_split_all_at_playhead()` 循环直接执行多个 SplitClipCommand，不是 Macro。一次 Split All 需要多次 undo，也可能半成功。 | `timeline_view.py` `_split_all_at_playhead` | 构造 MacroCommand；预验证所有可切片段后一次执行。 |
| B16 | High | TrackHeaderItem 在点击 mute/solo/lock 时先改本地 `_is_*` 再 emit；如果命令失败或 undo 状态失败，UI 会和模型不同步。 | `timeline_items.py` `TrackHeaderItem` | header 只发 request；模型成功后由 timeline incremental update 回填 UI；失败时不改视觉。 |
| B17 | High | 轨道音量拖动每次移动都执行 `SetTrackVolumeCommand`，undo 栈会被大量细碎命令污染。 | `timeline_view.py` `_on_track_volume_changed`; `timeline_items.py` volume drag | 音量拖动期间只 live preview，mouse release 时提交一个 command；或 CommandStack 支持 coalescing。 |
| B18 | High | `_find_target_track()` 在媒体类型和鼠标所在轨道不匹配时，会静默返回第一个同类型未锁轨道，用户把视频拖到音频区也可能插到某个视频轨。 | `timeline_view.py` `_find_target_track`; `timeline_layout.py` `resolve_drop_target` | Drop target 错类型应显示 invalid；只有明确拖到 create zone 或 target header 时才自动选轨。 |
| B19 | High | `get_target_audio_track_id()` 可能在 getter 中自动创建轨道，有副作用；查询目标轨道会改变 timeline 和 undo 栈。 | `timeline_view.py` `get_target_audio_track_id` | 拆分为 `get_target_audio_track_id()` 与 `ensure_target_audio_track()`；只有用户操作提交阶段允许创建。 |
| B20 | High | `_validate_move_group()` 按 `m.track_id` 分组，如果 member 已准备移动到新轨/目标轨，可能按旧轨验证，交叉轨道碰撞验证不完整。 | `timeline_view.py` `_validate_move_group` | MoveGroupMember 增加 `target_track_id`；验证时按目标轨分组。 |
| B21 | High | `MoveClipToExistingTrackCommand.apply()` 没有保留 source track volume；同类 reindex 也有 volume 丢失。 | `commands.py` `MoveClipToExistingTrackCommand` | 使用 track copy helper，禁止遗漏字段。 |
| B22 | High | `TimelineRenderPlan` 能返回多层，但旧 EditPage `_get_clip_at_frame()` 仍只取 topmost，且旧 clip-based frame request 仍存在；有双路径混淆。 | `edit_page.py` `_get_clip_at_frame`, `_request_frame_at_playhead` | 删除或降级旧 clip-based path；EditPage 只负责 `timeline_frame_requested(frame)`，渲染计划由 TimelinePreviewRenderer 统一处理。 |
| B23 | High | `EditGpuPreviewRenderer._composite_tracks()` 多轨叠加只支持 opacity，未处理 transform/crop/composite/effects/transition；CPU path 也多为基础 alpha blend。 | `timeline_render_plan.py`; `timeline_preview_renderer.py`; `edit_gpu_preview_renderer.py` | 将 `ClipLayerInfo.transform/crop` 映射进 GPU shader；transition 作为 render task 数据进入 GPU renderer。 |
| B24 | Medium | `TimelineLayoutEngine._default_track_for_type("video")` 返回 `self._video_tracks[0]`，但视频视觉顺序是 reversed。默认目标轨道和视觉 V1/Vn 可能不一致。 | `timeline_layout.py` | 明确定义 V1 是靠近 splitter 还是顶部；默认 target 与轨道头高亮保持一致。 |
| B25 | Medium | 同一个 ClipItem 处理鼠标事件，TimelineView 也维护 InteractionSession/drag session，事件所有权分裂，容易出现拖拽状态清理不一致。 | `timeline_items.py`; `timeline_view.py` | 收敛为 TimelineView 主导的交互状态机；ClipItem 只做 hit/paint/轻量 signal。 |
| B26 | Medium | `_current_drag_session` 在部分正常 same-track MoveGroup 分支未及时清理，可能导致 layout/update 跳过或预览状态残留。 | `timeline_view.py` `_on_drag_finished` | 在进入任何 command execute 前统一 clear drag session；异常路径也 finally 清理。 |
| B27 | Medium | `TimelinePreviewRenderer.request_render()` 缓存 key 只有 frame，没有包含 sequence generation、viewport size、preview quality、color/effect state。 | `timeline_preview_renderer.py` | cache key 改为 `(generation, frame, viewport_size, quality, render_hash)`；sequence/project 改动清 cache。 |
| B28 | Medium | reverse playback 逻辑疑似双重计算 `elapsed_at_pause`，倒放可能跳帧或跳到错误位置。 | `edit_playback_controller.py` `tick` | 用 playhead anchor + direction + monotonic delta 重写；加 forward/reverse 单元测试。 |
| B29 | Medium | 音频播放/混音链路和视频播放 clock 未看到统一 master clock，多轨 audio mute/solo/volume 未必驱动实际声音。 | `audio_playback.py`; `edit_playback_controller.py`; `timeline_render_plan.py` | 建立 playback coordinator：audio clock 或 video clock 作为 master，timeline audio render plan 支持多轨混音。 |
| B30 | Medium | CPU planar 转 BGR 使用简化 cv2 转换，limited/full range、BT.601/709/2020、10/12/16bit 精度可能不准。 | `timeline_preview_renderer.py` `_convert_planar_to_bgr` | CPU fallback 也要使用统一 color metadata；专业格式优先走 GPU YUV shader。 |

## 3. 优化总路线

执行顺序必须先保证模型和命令可靠，再优化交互，最后打通 GPU 链路。否则 GPU/播放优化会被底层 timeline 状态错误反复污染。

| 阶段 | 目标 | 文件 | 产出 | 验证 |
|---|---|---|---|---|
| P0 | 建立回归保护和基线 | `tests/edit/` | 新增失败复现测试，先标记 expected fail 或直接修复 | `pytest tests/edit/test_timeline_* tests/edit/test_edit_page_playback_preview.py` |
| P1 | 修复命令栈 undo/redo 和字段保留 | `commands.py`, `timeline_v2.py` | CommandStack/MacroCommand 协议稳定，轨道 volume 不丢 | 新增 track command undo、macro undo、volume preserve 测试 |
| P2 | 统一 speed/source frame 模型 | `timeline_v2.py`, `timeline_ops.py`, `commands.py`, `timeline_render_plan.py` | 所有 split/trim/slip/render 使用同一源帧映射 | float speed 和 SpeedParams 两套测试均通过 |
| P3 | 修复 A/V 插入与移动原子性 | `timeline_view.py`, `commands.py` | linked video/audio 插入、移动、trim 全部同帧同 undo | UI 级拖拽测试 + model command 测试 |
| P4 | 修复轨道头与轨道创建/删除/移动 | `timeline_items.py`, `timeline_view.py`, `commands.py` | header 只发请求，模型成功回填 UI；轨道 getter 无副作用 | mute/solo/lock/volume/rename/delete undo 测试 |
| P5 | 修复剪辑工具交互 | `timeline_view.py`, `timeline_items.py` | blade hit 正确、Split All 原子、drag session 清理一致 | pytest-qt 鼠标事件测试 |
| P6 | 打通 GPU render result 回流 | `timeline_preview_renderer.py`, `edit_gpu_preview_renderer.py`, `file_controller.py`, `program_viewer.py` | GPU result 统一回 TimelinePreviewRenderer，pending/cache/generation 正常 | GPU pipeline bench + fake GPU signal 测试 |
| P7 | 完善多轨 GPU 合成和播放同步 | `timeline_render_plan.py`, `edit_gpu_preview_renderer.py`, `edit_playback_controller.py`, `audio_playback.py` | 多视频轨 opacity/transform/crop/transition 可播放，audio/video sync | 多轨黄金帧测试、播放 drift 测试 |
| P8 | 性能、可观测性和验收矩阵 | `docs/`, `tests/`, logging | 帧路径指标、decode/render path overlay、benchmark | 1080p/4K/6K 样片性能记录 |

## 4. 详细实施步骤

### P0. 回归测试先行

1. 新建 `tests/edit/test_timeline_command_regressions.py`。
2. 覆盖以下最小场景：
   - mute/solo/lock/volume/rename/delete track apply 后 undo 能恢复。
   - Add/Move/Delete track 后原 track volume 不变。
   - MacroCommand 包含需要 updated_command 的子命令时，undo 成功。
   - SplitClipCommand 同时支持 `speed=1.0` 和 `speed=SpeedParams(speed=1.0)`。
   - RippleDelete 删除后 undo 恢复 clip 和后续 clip 位置。
3. 新建 `tests/edit/test_timeline_linked_av_regressions.py`。
4. 覆盖：
   - 插入带音频视频素材时，video/audio linked clip 同起点。
   - 音频目标轨道有重叠时，整个插入失败或选择明确新轨，不能悄悄错位。
   - 视频插入成功但音频失败时，timeline 不留下半成品。
5. 新建/补充 `tests/edit/test_edit_gpu_pipeline_integration.py`。
6. 覆盖：
   - GPU renderer 成功返回后，TimelinePreviewRenderer `_pending_frame` 清空。
   - generation 不匹配的 GPU frame 不显示。
   - `EditGpuDecodeManager.get_frame()` 的第二返回值按字符串处理，不再当 bool。

验证命令：

```bash
python3 -m pytest tests/edit/test_timeline_command_regressions.py -q
python3 -m pytest tests/edit/test_timeline_linked_av_regressions.py -q
python3 -m pytest tests/edit/test_edit_page_playback_preview.py tests/edit/test_timeline_preview_renderer.py -q
```

### P1. 命令栈和字段保留

1. 在 `commands.py` 增加统一 helper：
   - `copy_track_with(track, **changes) -> EditTrack`
   - `copy_clip_with(clip, **changes) -> ClipInstance`
   - 或优先给 `EditTrack`/`ClipInstance` 增加 `with_index()`、`with_name()`、`with_volume()` 等不可变复制方法。
2. 搜索所有 `EditTrack(` 手写重建，逐处替换为 helper，必须保留：
   - `track_id`
   - `name`
   - `track_type`
   - `index`
   - `clips`
   - `transitions`
   - `is_enabled`
   - `is_locked`
   - `is_muted`
   - `is_solo`
   - `volume`
3. 搜索所有 `ClipInstance(` 手写重建，逐处替换为 helper，必须保留：
   - `speed`
   - `linked_clip_id`
   - `transform`
   - `crop`
   - `opacity`
   - `effects`
   - `is_enabled`
   - `fade_in_duration`
   - `fade_out_duration`
   - `animated_params`
   - `color_stack_id`
   - `color_shared`
   - `composite_id`
4. 修改以下命令 apply 返回 updated command：
   - `SetTrackMuteCommand`
   - `SetTrackSoloCommand`
   - `SetTrackLockCommand`
   - `SetTrackVolumeCommand`
   - `RenameTrackCommand`
   - `DeleteTrackCommand`
   - `SlipClipCommand`
   - `RippleDeleteCommand`
   - 任何 undo 依赖 `_old_*` 或 `_deleted_*` 的 command。
5. 修改 `MacroCommand.apply()`：
   - 每个子命令执行后，如果返回 `(timeline, updated_cmd)`，把 updated_cmd 放进新列表。
   - 最终返回 `(current_timeline, updated_macro)`。
   - 如果中途失败，不应修改原 timeline 或推入 undo stack。
6. 修改 `CommandStack.execute()` 测试，确保普通 command 和 MacroCommand 都按同一协议入栈。

验证方法：

```bash
python3 -m pytest tests/edit/test_timeline_command_regressions.py -q
python3 -m pytest tests/edit/test_timeline_track_operations.py -q
python3 -m py_compile src/features/edit/core/commands.py src/features/edit/core/timeline_v2.py
```

人工验证：

1. 新建 V1/A1/V2/A2。
2. 设置 A1 volume=0.35。
3. 新增、移动、删除任意轨道并 undo/redo。
4. 确认 A1 volume 始终为 0.35。
5. mute/solo/lock/rename/delete track 均能一次 undo 恢复。

### P2. 统一 speed 和源帧映射

1. 在 `timeline_v2.py` 建立稳定 API：
   - `ClipInstance.effective_speed() -> float`
   - `ClipInstance.timeline_frame_to_source_frame(timeline_frame: int) -> int`
   - `ClipInstance.timeline_delta_to_source_delta(delta_frames: int) -> int`
2. `SpeedParams` 支持时，所有变速曲线、反向、ramp 都在模型 API 内处理。
3. `timeline_ops.py` 禁止直接 `clip.speed * delta`，全部改用模型 API。
4. `commands.py` `SplitClipCommand` 禁止 `clip.speed.speed`，改用 `clip.timeline_delta_to_source_delta(split_offset)`。
5. `timeline_render_plan.py` 的 `calculate_source_frame()` 改为调用 ClipInstance API。
6. 补充测试：
   - float speed 1.0 / 0.5 / 2.0 split 后左右 source range 正确。
   - SpeedParams constant speed split 后左右 source range 正确。
   - reverse speed 或 ramp 未完整支持时，明确抛 `NotImplementedError` 或降级，不允许静默算错。

验证命令：

```bash
python3 -m pytest tests/edit/test_timeline_ops.py tests/edit/test_timeline_render_plan.py -q
```

### P3. A/V 插入、移动、trim 原子性

1. 重写 `TimelineView._add_asset_as_clip()`：
   - 先解析 asset media type 和可用 audio/video stream。
   - 先生成 plan，不立即执行 command。
   - 验证 video target track、audio target track、起点 frame、duration、overlap、lock 状态。
   - 对带音频视频素材，video/audio 必须同起点；如果目标 audio 轨重叠，优先明确创建新 audio track 或拒绝，不允许自动错位。
   - 用一个 MacroCommand 提交 video clip + audio clip + linked ids。
2. 增加专门命令：
   - `InsertLinkedAssetCommand` 或 `AddLinkedClipsCommand`
   - 负责创建两个 clip、互设 linked_clip_id、可选创建音频轨。
3. 修改 `_find_target_track()`：
   - media type 与 hit track type 不一致时返回 invalid，不静默 fallback。
   - fallback 只能用于“没有命中任何轨道但有当前 target track”的明确场景。
4. 修改 `get_target_audio_track_id()`：
   - 去掉创建轨道副作用。
   - 创建轨道只允许在 `ensure_target_audio_track()` 内，并且作为 command 的一部分。
5. 修改 `_on_drag_finished()`：
   - MoveGroupMember 带 `target_track_id` 和 `create_track`。
   - 拖到已有轨道用 `MoveClipToExistingTrackCommand`。
   - 拖到 pseudo/new zone 才用 `MoveClipToTrackCommand.create_for_new_track()`。
   - 命令执行前统一预验证所有 member，不允许 macro 执行一半才失败。
6. linked trim：
   - left/right trim 对 linked clip 的 source delta 和 timeline delta 必须同一单位。
   - 如果 linked clip 不满足边界，整个 trim 拒绝并还原视觉。

验证命令：

```bash
python3 -m pytest tests/edit/test_timeline_linked_av_regressions.py -q
python3 -m pytest tests/edit/test_timeline_drag_sync.py tests/edit/test_timeline_interactions.py -q
```

人工验证：

1. 把带音频的视频拖到 V1/A1 的空白处，确认 V/A 同帧起点，linked 高亮正确。
2. 在 A1 同位置放已有音频，再拖同一个视频到 V1；确认要么新建 A2 且同帧，要么明确拒绝，不允许 A1 音频自动错位。
3. 选中 linked video/audio，拖到已有 V2/A2，确认不新增轨道。
4. 选中 linked video/audio，拖到轨道边缘 create zone，确认只新增需要的新轨道。
5. undo 一次恢复整个 linked move。

### P4. 轨道头和轨道创建

1. `TrackHeaderItem` 的 mute/solo/lock/target/volume：
   - click/drag 时只 emit request。
   - 不直接修改 `_is_muted/_is_solo/_is_locked/_volume`。
   - TimelineView 执行 command 成功后，通过 `update_timeline_incremental()` 或全量 reload 更新 header。
2. 音量：
   - mouse move 只更新 temporary preview 和 tooltip。
   - mouse release 提交一个 `SetTrackVolumeCommand`。
   - 支持 command coalescing：连续 volume 命令同 track 在 500ms 内合并。
3. Delete track：
   - 删除有 clip 的轨道前已有确认框，但 command 必须保存完整 track dict。
   - undo 恢复原 index、clips、transitions、volume、mute/solo/lock。
4. Move track：
   - 明确 video 视觉上下与 index 的关系。
   - UI 文案“上/下”转换为 model index 时统一走 layout engine。
5. Add track：
   - `position` 使用 type-local index，不用混合 all_tracks index。
   - 插入后自动 target 新轨道，并保持 header 高亮。

验证：

```bash
python3 -m pytest tests/edit/test_timeline_track_operations.py tests/edit/test_timeline_command_regressions.py -q
```

人工验证：

1. 快速点击 mute/solo/lock，然后 undo/redo。
2. 拖 volume 5 秒，undo 栈只增加 1 条或少量合并命令。
3. 删除含 clip 的轨道后 undo，clip、transition、volume 全恢复。

### P5. 剪辑工具和 UI 交互

1. Blade：
   - `_handle_blade_click()` 使用 layout engine hit test。
   - 普通点击只切命中轨道 clip。
   - Shift/Alt 或菜单命令才 Split All。
2. Split All：
   - 先收集所有可切 clip。
   - 过滤 locked track、disabled clip、clip 边界。
   - 用 MacroCommand 一次提交。
3. Drag session：
   - `_current_drag_session` 在 `try/finally` 中清理。
   - 命令执行失败时还原所有 dragged ClipItem 的位置/尺寸。
4. 增量更新：
   - `_clip_has_changed()` 增加所有 ClipInstance 字段比较，特别是 animated_params、composite_id、fade、color。
   - 对 linked/macro/cross-track 变更，必要时强制 full reload。
5. 交互状态：
   - ClipItem 不再直接决定复杂 drag 行为，只发 `pressed/moved/released` 意图。
   - TimelineView 的 InteractionSession 决定 mode、snap、validation、command。

验证：

```bash
python3 -m pytest tests/edit/test_timeline_interactions.py tests/edit/test_timeline_drag_sync.py -q
```

人工验证：

1. Blade 点击 V2 的 clip，不应切 V1/A1。
2. Split All 后 undo 一次全部恢复。
3. 拖拽失败或重叠时，视觉立即回原位。
4. 锁定轨道上的 clip 可选中但不可移动/trim/split。

### P6. GPU 全链路打通

当前判断：尚未稳定打通。

已存在：

1. `EditGpuDecodeManager` 支持三路径：
   - macOS VT zero-copy -> `GpuDecodedFrame`
   - CPU planar YUV -> GPU uploader
   - CPU packed RGB fallback
2. `EditGpuPreviewRenderer` 支持 shared GL context、YUV shader、track FBO、多轨 opacity blend。
3. `file_controller.py` 会在 ProgramViewer GL 初始化后尝试 `_init_gpu_pipeline()`。
4. ProgramViewer 有 `set_shared_texture()`。

关键缺口：

1. `TimelinePreviewRenderer._render_frame()` 仍明确是 QImage MVP fallback。
2. GPU 成功路径没有发回 `TimelinePreviewRenderer.frame_ready(frame, result)`，而是直接让 GPU renderer signal 到 ProgramViewer。
3. GPU signal 缺少 frame/generation，无法丢弃 stale result。
4. `_pending_frame` 和 cache 不会在 GPU 成功后更新。
5. GPU output texture 没有双缓冲/同步，UI 和 render thread 可能竞争。
6. 多轨 GPU 合成未覆盖 transform/crop/transition/effects。
7. 测试脚本对 `get_frame()` 返回值理解错误，可能误报。

实施步骤：

1. 修改 `EditGpuPreviewRenderer.frame_ready`：
   - 从 `(tex_id, width, height)` 改为 `(frame, generation_id, tex_id, width, height)`。
2. `TimelinePreviewRenderer.set_gpu_renderer()` 连接 GPU renderer signal 到自身 `_on_gpu_frame_ready()`。
3. 新增 `_on_gpu_frame_ready(frame, generation, tex_id, width, height)`：
   - 校验 generation。
   - 清 `_pending_frame`。
   - 写入 cache，cache value 为 `("shared_texture", tex_id, width, height, generation)` 或 dataclass。
   - 发 `frame_ready.emit(frame, (tex_id, width, height))`。
4. `file_controller.py` 不再把 `gpu_renderer.frame_ready` 直接连接到 ProgramViewer。
5. `ProgramViewerWidget.set_frame()`：
   - texture tuple 应调用 `set_shared_texture()`，不是普通 `set_texture()`。
   - CPU fallback QImage 保持 `set_qimage()`。
6. Output texture 池：
   - `EditGpuPreviewRenderer` 维护 2-3 个 composite textures。
   - 提交给 UI 的 texture 标记 in-flight。
   - UI paint 后发 ACK 或下一帧延迟回收；简单版本可三缓冲并延迟 N 帧复用。
7. fallback 策略：
   - GPU 初始化失败：明确 logger + metric `render_path=cpu_qimage`。
   - 某一 layer 是 CPU packed：允许整帧 CPU fallback，或者上传 packed RGB 到 GPU 后继续合成；不要 silent black。
8. 测试修复：
   - `tests/edit/test_edit_gpu_pipeline.py` 使用 `frame_type == "gpu"`。
   - task layer 必须包含 `frame_type`。
   - 分别统计 gpu/planar/cpu/fallback。
9. 添加可观测日志：
   - 每帧记录低频 sampled metrics：`decode_path`, `render_path`, `layers`, `gpu_submit_ms`, `gpu_render_ms`, `qimage_upload_ms`。
   - ProgramViewer 可选 debug overlay：GPU zero-copy / planar GPU upload / CPU QImage。

验证命令：

```bash
python3 -m pytest tests/edit/test_timeline_preview_renderer.py tests/edit/test_edit_page_playback_preview.py -q
python3 tests/edit/test_edit_gpu_pipeline.py --frames 60
```

人工验证：

1. 打开 H264/H265 样片，移动 playhead，overlay 应显示 GPU zero-copy 或明确 CPU fallback 原因。
2. 连续 scrub，画面不能闪回旧帧。
3. 同一帧重复请求不会因为 `_pending_frame` 卡住。
4. 关闭项目/切换 timeline 时，旧 GPU frame 不应覆盖新 timeline。
5. 多轨叠加 V1+V2，V2 opacity=0.5，应看到正确混合。

### P7. 多轨播放和音视频同步

1. `TimelineRenderPlan`：
   - 确认 video track order 与 UI 显示一致。
   - solo/mute 规则明确：video mute 是否隐藏视频，audio mute 是否静音。
   - 输出稳定的 layer order：bottom -> top。
2. `EditGpuPreviewRenderer`：
   - Layer shader 支持 transform/crop/opacity。
   - Transition task 支持 cross dissolve、dip to color。
   - 不支持的 effect/composite 明确 fallback 或跳过并显示 warning。
3. `EditPlaybackController`：
   - 重写 forward/reverse tick：使用 anchor frame + monotonic delta。
   - 支持 dropped frame 策略，不堆积 render queue。
4. Audio：
   - 建立 timeline audio render plan。
   - 多轨混音支持 track volume、clip gain、mute、solo。
   - 选定 master clock：优先 audio clock；无音频时 video clock。
   - 视频 frame request 以 master clock 推导 timeline frame。
5. Scrub：
   - scrub 时只取最近请求，取消旧请求。
   - ProgramViewer 不显示比当前 playhead 更旧的 frame。

验证：

```bash
python3 -m pytest tests/edit/test_timeline_render_plan.py tests/edit/test_edit_playback_controller.py -q
```

人工验收矩阵：

| 场景 | 期望 |
|---|---|
| V1 单轨播放 | 正常解码、播放头和画面同步 |
| V1+V2 重叠 | V2 按视觉上层覆盖 V1 |
| V2 opacity 50% | GPU/CPU 路径结果一致 |
| V1 muted | 如果定义为 video mute，画面隐藏；如果只影响 audio，UI 文案需区分 |
| A1 muted | 声音静音，视频不受影响 |
| A1 volume 35% | 实际音量下降，undo/redo 正常 |
| linked video/audio 移动 | 播放全程音画同步 |
| reverse playback | 不跳错帧、不双倍回退 |

## 5. 推荐 AI 任务拆分

不要让 AI 一次修完整时间线。按下面顺序开任务，每个任务都要求先跑相关测试。

| 任务 | Prompt 摘要 | 验收 |
|---|---|---|
| T1 | 修复 `commands.py` 中 track command / MacroCommand undo 状态保存，并保留 `EditTrack.volume` | command regression tests pass |
| T2 | 统一 speed/source mapping，修复 SplitClipCommand 和 timeline_ops | speed/split/render plan tests pass |
| T3 | 重构 linked A/V asset insertion 为原子 command，禁止自动错位 | linked AV insertion tests pass |
| T4 | 修复 cross-track drag existing/new track 命令选择和 MoveGroup validation | drag sync tests pass |
| T5 | 修复 TrackHeaderItem 乐观 UI 更新和 volume command coalescing | track header UI tests pass |
| T6 | 修复 blade hit test、Split All Macro、drag session cleanup | interaction tests pass |
| T7 | 改造 GPU frame_ready 回流 TimelinePreviewRenderer，修复 pending/cache/generation | preview renderer tests pass |
| T8 | 增加 GPU output texture double/triple buffering 和 pipeline benchmark 修复 | GPU bench 有可靠结果 |
| T9 | 多轨 GPU transform/crop/transition 和 playback audio sync | golden frame + playback tests pass |

## 6. 每个 AI 任务必须遵守的约束

1. 修改核心渲染/时间线模块前，先读 `.clinerules`。
2. PyQt UI 不直接调用跨线程 OpenGL API。
3. 所有 GLSL 数字常量使用 `1.0` 形式，循环边界必须可编译为常量。
4. 不手写丢字段的 dataclass 重建；优先用 `with_*` helper。
5. 所有 command 必须满足：
   - apply 成功后 redo 可重放。
   - undo 一次恢复完整旧状态。
   - MacroCommand undo 一次恢复全部子操作。
6. 所有 linked A/V 操作必须满足：
   - 要么全部成功，要么全部不变。
   - video/audio timeline_start 必须保持同步，除非用户明确 unlink。
7. GPU 优化必须有 CPU fallback，但 fallback 必须可观测，不能静默。

## 7. 建议新增测试文件

| 文件 | 内容 |
|---|---|
| `tests/edit/test_timeline_command_regressions.py` | 命令栈、undo/redo、volume preserve、MacroCommand |
| `tests/edit/test_timeline_linked_av_regressions.py` | linked A/V 插入、移动、trim 原子性 |
| `tests/edit/test_timeline_track_header_ui.py` | mute/solo/lock/volume header UI 与模型同步 |
| `tests/edit/test_timeline_blade_split.py` | blade hit test、Split All macro |
| `tests/edit/test_timeline_gpu_preview_state.py` | GPU result generation/pending/cache 回流 |
| `tests/edit/test_edit_playback_controller.py` | forward/reverse playback tick、drop frame、clock |
| `tests/edit/test_timeline_gpu_golden_frames.py` | CPU/GPU 多轨合成黄金帧对比 |

## 8. 最终验收标准

### 功能验收

1. 新建项目后可创建/删除/重命名/移动视频和音频轨道。
2. 轨道 mute/solo/lock/volume 支持 undo/redo，UI 和模型一致。
3. 带音频视频素材插入后，视频和音频 linked 且同帧。
4. linked clip 移动、trim、删除、split 均可一次 undo。
5. 跨轨移动到已有轨道不会误创建新轨道。
6. 拖到 create zone 才创建新轨道。
7. Blade 只切命中轨道；Split All 一次 undo 全恢复。
8. 多轨视频播放按轨道上下关系正确合成。
9. 音频 mute/solo/volume 影响真实播放。
10. reverse playback 不跳错帧。

### GPU 验收

1. H264/H265 macOS VT zero-copy 样片显示 `decode_path=gpu_zero_copy`。
2. ProRes/DNxHR 显示 `decode_path=cpu_planar` 且 `render_path=gpu_yuv_upload`。
3. 普通 CPU fallback 显示 `render_path=cpu_qimage` 和明确原因。
4. GPU 成功帧经过 TimelinePreviewRenderer generation 校验。
5. 快速 scrub 不显示旧 timeline/旧 generation 的画面。
6. 4K 单轨播放目标：稳定接近项目目标帧率；如果达不到，benchmark 输出 decode/render/upload 分项耗时。
7. 多轨 opacity 合成 CPU/GPU golden frame 差异在设定阈值内。

## 9. 推荐执行命令

基础静态检查：

```bash
python3 -m py_compile \
  src/features/edit/core/timeline_v2.py \
  src/features/edit/core/timeline_ops.py \
  src/features/edit/core/commands.py \
  src/features/edit/ui/timeline_view.py \
  src/features/edit/ui/timeline_items.py \
  src/features/edit/core/timeline_preview_renderer.py \
  src/features/edit/core/edit_gpu_decode_manager.py \
  src/features/edit/core/edit_gpu_preview_renderer.py \
  src/features/edit/core/edit_playback_controller.py
```

时间线核心：

```bash
python3 -m pytest tests/edit/test_timeline_ops.py tests/edit/test_timeline_render_plan.py -q
python3 -m pytest tests/edit/test_timeline_command_regressions.py -q
python3 -m pytest tests/edit/test_timeline_linked_av_regressions.py -q
```

UI/交互：

```bash
python3 -m pytest tests/edit/test_timeline_interactions.py tests/edit/test_timeline_drag_sync.py -q
python3 -m pytest tests/edit/test_edit_page_playback_preview.py tests/edit/test_timeline_preview_renderer.py -q
```

GPU：

```bash
python3 tests/edit/test_edit_gpu_pipeline.py --frames 60
python3 -m pytest tests/edit/test_timeline_gpu_preview_state.py -q
```

## 10. 建议交给 AI 的首个任务 Prompt

```text
请在 Topos Color 项目中修复 Edit 时间线命令栈的 undo/redo 状态问题。

项目背景：
- PyQt6 + ModernGL + GLSL 视频/图像编辑器。
- 时间线模型在 src/features/edit/core/timeline_v2.py。
- 命令栈在 src/features/edit/core/commands.py。
- 严格遵守 .clinerules。

任务范围：
1. 修复 MacroCommand.apply，使其保留所有返回 updated_command 的子命令，并返回 updated_macro。
2. 修复 SetTrackMute/Solo/Lock/Volume、RenameTrack、DeleteTrack、SlipClip、RippleDelete 等 apply 中旧状态未写回的问题。
3. 新增 copy helper，避免手写 EditTrack/ClipInstance 重建时丢字段，特别是 EditTrack.volume。
4. 新增 tests/edit/test_timeline_command_regressions.py，覆盖 track command undo、volume preserve、MacroCommand undo。

约束：
- 不改 UI 外观。
- 不碰 GPU 渲染。
- 不重构无关模块。
- 所有新增测试必须能单独运行。

验证：
python3 -m pytest tests/edit/test_timeline_command_regressions.py -q
python3 -m py_compile src/features/edit/core/commands.py src/features/edit/core/timeline_v2.py
```

