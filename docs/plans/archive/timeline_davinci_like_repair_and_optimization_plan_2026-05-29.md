# Timeline DaVinci-like 修复与二次优化计划

日期：2026-05-29

目标：在当前已经引入 `timeline_layout.py` / `timeline_interaction.py`、V/A 区域分割、轨道高度、linked drag 等改造之后，继续把时间线面板修到稳定可用，并逐步达到「85%-95% 视觉接近、核心体验接近 DaVinci Resolve」。

本计划基于 2026-05-29 当前代码状态和截图反馈：

- UI 视觉改善不明显。
- 视频/音频轨距离过大，中间空白过多。
- 左侧轨道头出现 `A2 A1` 这类编号混乱。
- linked 视频/音频素材拖动释放时可能失败。
- 用户感知为“现在连素材都无法移动”。
- `tools/capture_edit_page.py` 当前有语法错误，无法用于截图验收。

已确认并修过的第一批问题：

- linked move 使用了错误的 `MacroCommand(commands, description=...)` 构造方式，已改为 `MacroCommand.create("Move linked clips", commands)`。
- V1/A1 现在应贴近中间 splitter，而不是分别贴在各自区域顶部。
- Track header badge 改成类型内部索引，避免第一条音频轨显示为 A2。
- video/audio target track 独立高亮。
- `tests/edit` 已验证：`523 passed, 10 skipped`。

---

## 1. 当前状态判断

当前优化属于「架构拆了一半、视觉和交互尚未完成」阶段。也就是说，已经有一些正确方向：

- 有了 `TimelineLayoutEngine`。
- 有了 V/A area 和 splitter。
- `ClipItem.paint()` 已经不再直接 `setPos()`。
- 有 linked move 的原子命令雏形。
- 有一批 layout tests。

但仍然还不像 DaVinci，原因主要是：

| 问题 | 现状 | 后果 |
|---|---|---|
| 架构改造未闭环 | layout engine 有了，但 preview/drop/hit-test/辅助 item 还没有完全统一 | 局部仍可能错位 |
| 视觉层没真正重做 | clip、header、ruler、toolbar 仍大部分沿用旧绘制 | 看起来像旧 UI 加了几条线 |
| 交互状态机不完整 | `TimelineView` 和 `ClipItem` 都在处理 mouse event | 素材拖动可能被拦截或状态错乱 |
| target track 语义不清 | video/audio target 刚分开，插入/drop 尚需统一 | 拖入素材可能落点和预览不一致 |
| 测试偏 layout | linked drag 有基础测试，但真实鼠标拖拽/preview/drop 还缺测试 | UI bug 容易回归 |
| 截图工具不可用 | `capture_edit_page.py` 有 SyntaxError | 无法做视觉验收闭环 |

---

## 2. 最高优先级原则

接下来不要直接继续堆视觉效果。必须按下面顺序：

1. 先稳定：素材能移动、linked 音视频能同步、不会错轨。
2. 再统一：所有坐标、hit-test、preview、drop 都走同一套 layout engine。
3. 再视觉：轨道头、clip、标尺、toolbar、背景层次统一成 DaVinci-like。
4. 再性能：缩略图、波形、LOD、缓存。
5. 最后验收：截图、交互测试、性能基线。

---

## 3. P0：立即修复当前回归 bug

目标：让时间线恢复“能正常编辑”的最低稳定状态。

| 编号 | 任务 | 当前问题 | 文件 | 验收 |
|---|---|---|---|---|
| P0-1 | 修复 linked move 命令构造 | 错误构造 `MacroCommand` 会导致释放拖拽失败 | `src/features/edit/ui/timeline_view.py` | 拖动 linked 视频后，音频同步移动，命令不报错 |
| P0-2 | V1/A1 靠近 splitter | 视频和音频 clip 被各自区域顶部吸附，中间空白巨大 | `src/features/edit/ui/timeline_layout.py` | V1 底部贴近 splitter 上方，A1 顶部贴近 splitter 下方 |
| P0-3 | 修复 A1 显示成 A2 | 轨道头 badge 用全局视觉索引 | `src/features/edit/ui/timeline_items.py` | 第一条音频轨显示 A1 |
| P0-4 | video/audio target 独立 | 只有视频 target 高亮或 A target 混入 video target | `timeline_view.py` | V1 和 A1 可同时高亮为目标轨 |
| P0-5 | 增加 linked drag 回归测试 | 当前 bug 之前没有测试兜底 | `tests/edit/test_timeline_view_layout.py` | `_on_drag_finished()` 后 video/audio start 同步 |
| P0-6 | 检查真实鼠标拖拽链路 | 单元测试能过不代表鼠标事件没被拦截 | `timeline_view.py`、`timeline_items.py` | 手动拖动 clip，拖动中有 preview，释放后位置保留 |
| P0-7 | 修复截图工具语法错误 | 无法做 UI 验收 | `tools/capture_edit_page.py` | `python tools/capture_edit_page.py --help` 可运行 |

P0 完成标准：

- `tests/edit` 全绿。
- 用户能拖动视频 clip。
- linked audio 不上漂、不重叠、不释放失败。
- 截图工具能运行。

---

## 4. P1：统一坐标、命中、拖拽和 drop

目标：彻底避免“看起来在这里，实际命中在那里”的问题。

| 编号 | 任务 | 具体实现 | 验收 |
|---|---|---|---|
| P1-1 | 统一 track rect 查询 | 所有 track top/height 只从 `TimelineLayoutEngine.track_rect(track_id)` 获取 | `rg "TRACK_AREA_TOP.*track_index"` 不再出现在 track-bound item 逻辑里 |
| P1-2 | 统一 frame/x 查询 | 所有 frame/x 用 `engine.frame_to_x()` / `engine.x_to_frame()` | clip、ruler、playhead、snap 对齐 |
| P1-3 | 统一 hit-test | 新增或完善 `resolve_timeline_hit(scene_pos)` | ruler/splitter/header/clip/empty/track gap 区域明确 |
| P1-4 | 统一 drop target | 新增 `resolve_drop_target(scene_pos, media_type)` | preview 轨道就是最终 drop 轨道 |
| P1-5 | wrong-area 反馈 | 音频拖视频区、图片拖音频区要 invalid 或明确吸到目标轨 | 不再静默落错轨 |
| P1-6 | locked track 防护 | hit-test、preview、command 都检查 locked | 锁定轨不可 drop/move/trim |
| P1-7 | splitter 优先级 | splitter 命中优先于 track resize 和 clip drag | 拖 splitter 不误触 clip |
| P1-8 | empty area 行为 | 空白轨道区可框选/drop，非轨道空白区不创建 clip | 不会在下方黑区误建轨 |

建议新增数据结构：

```python
@dataclass(frozen=True)
class TimelineHit:
    area: Literal["ruler", "splitter", "track", "clip", "header", "empty", "invalid"]
    track_id: str | None = None
    clip_id: str | None = None
    frame: int | None = None
    reason: str = ""

@dataclass(frozen=True)
class TimelineDropTarget:
    frame: int
    track_id: str | None
    track_type: str | None
    is_valid: bool
    create_track: bool = False
    reason: str = ""
```

P1 完成标准：

- 鼠标在哪，系统命中的区域和用户看到的一致。
- preview 和最终 drop 一致。
- 视频/音频不会跨错区域。

---

## 5. P2：重做鼠标交互状态机

目标：避免 `TimelineView` 和 `ClipItem` 抢事件，导致素材不能移动。

当前风险：

- `TimelineView.mouseMoveEvent()` 处理 splitter、playhead、rubber band。
- `ClipItem.mouseMoveEvent()` 处理 clip move/trim/fade/slip/slide。
- 如果 `TimelineView` 的 `_interaction` 状态没有正确 idle，clip event 可能被吞。

建议路线：

| 编号 | 任务 | 具体实现 | 验收 |
|---|---|---|---|
| P2-1 | 明确事件所有权 | `TimelineView` 只决定全局区域；clip body/edge 交给 `ClipItem` 或统一迁移到 View | 鼠标事件不双重处理 |
| P2-2 | clip drag session | 建立 `ClipDragSession`，记录 primary、linked、selected group | 拖动中状态可追踪 |
| P2-3 | preview-only 原则 | mouse move 只改 preview，不提交 command | 拖动不会产生多条 undo |
| P2-4 | release 提交一次 | mouse release 生成一个 command 或 macro command | 撤销一次还原 |
| P2-5 | Esc cancel | 按 Esc 恢复所有 preview | model 不变 |
| P2-6 | drag threshold | 鼠标移动超过 3-5px 才进入 dragging | 点击选择不会误拖 |
| P2-7 | cursor state | hover edge、clip body、splitter、locked track 都显示正确 cursor | 手感专业 |

状态机：

```text
Idle
Selecting
DraggingClip
TrimmingLeft
TrimmingRight
DraggingPlayhead
DraggingRuler
ResizingArea
ResizingTrack
Panning
ContextMenu
```

硬规则：

- 同一时间只能有一个 interaction。
- 进入非 Idle 状态必须记录 source item 和 start pos。
- release/cancel 必须清理状态。
- timeline reload 时强制 cancel 当前 interaction。

---

## 6. P3：linked 音视频与多选移动重构

目标：音视频同步和多选移动像专业剪辑软件一样可信。

| 编号 | 任务 | 当前风险 | 修复 |
|---|---|---|---|
| P3-1 | MoveGroup 构建 | linked 和 selected 分散处理 | 构建统一 `MoveGroup` |
| P3-2 | group validate | 当前可能主 clip 成功、其他 selected 失败 | 先验证全组，失败则全部不提交 |
| P3-3 | live preview 全组移动 | 当前 linked preview 曾经不同步 | move 中所有 member 同步 x 变化，y 锁轨 |
| P3-4 | atomic command | linked 和 selected 都要一个 macro | undo 一次全部还原 |
| P3-5 | linked selection toggle | linked selection 开启时拖 video 带 audio；关闭时可单独移动 | 行为可控 |
| P3-6 | linked split/delete 策略 | 刀片/删除是否同步 linked 要明确 | 不出现半段音频或半段视频 |

建议结构：

```python
@dataclass(frozen=True)
class MoveGroupMember:
    clip_id: str
    track_id: str
    old_start: int
    new_start: int

@dataclass(frozen=True)
class MoveGroup:
    primary_clip_id: str
    members: tuple[MoveGroupMember, ...]
    delta_frames: int
```

验收：

- 拖视频，音频同步横移。
- 拖音频，视频同步横移。
- y 坐标始终由各自轨道决定。
- 多选有一个整体 invalid/valid 状态。
- undo 一次还原全部。

---

## 7. P4：真正做 DaVinci-like 视觉层

当前截图里的问题不是简单颜色不好，而是视觉层次没有被系统化重做。

### 7.1 背景与区域

| 元素 | 目标 |
|---|---|
| 面板背景 | 深灰，不纯黑 |
| 视频区 | 比音频区略亮 |
| 音频区 | 深绿灰/深灰，不突兀 |
| splitter | 中间有可见 6px hit area，视觉上 1-2px 线 |
| 空白区 | 不抢眼，但能看出是 track area |
| 网格线 | 主刻度/副刻度透明度不同 |

### 7.2 Track Header

| 细节 | 目标 |
|---|---|
| badge | V1/A1 小胶囊，类型内部编号 |
| target | V target 和 A target 独立高亮 |
| lock/mute/solo | 图标或紧凑按钮，不挤成灰块 |
| volume | 音频轨显示小型音量条 |
| height resize | 底部 handle hover 可见 |
| spacing | 左侧不要拥挤，按钮之间 2-4px |

### 7.3 Clip

| 视频 clip | 音频 clip |
|---|---|
| 蓝色主体 | 绿色主体 |
| filmstrip 缩略图 | waveform |
| 顶部 label bar | 顶部/底部 label bar |
| link icon | link icon |
| fx/speed badge | mute/gain badge |
| selected 橙色描边 | selected 橙色描边 |
| hover 边框 | hover 边框 |

### 7.4 Ruler / Playhead

| 元素 | 目标 |
|---|---|
| ruler | SMPTE 时间码，LOD 随 zoom 变化 |
| playhead | 红色三角 + 垂直线，贯穿 V/A 区 |
| scrub | 点击/拖动 ruler 响应稳定 |
| snap line | 吸附时出现清晰竖线 |

P4 完成标准：

- 截图第一眼不再像旧 UI。
- V/A 结构、标尺、clip、轨道头都有统一视觉语言。
- 不用解释，用户能看懂哪里是视频、哪里是音频、哪里能拖。

---

## 8. P5：素材预览、缩略图、波形

| 编号 | 任务 | 具体实现 | 验收 |
|---|---|---|---|
| P5-1 | 视频 filmstrip LOD | 宽 clip 显示多帧，窄 clip 隐藏缩略图 | 缩放不卡、不乱 |
| P5-2 | start/end thumbnail 模式 | 可选仅首尾帧 | 小项目观感清爽 |
| P5-3 | waveform 正确时间映射 | 用 audio seconds，不乱用 video fps | 修剪后波形对应素材 |
| P5-4 | 缓存 | thumbnail/waveform 缓存 key 包含 asset/range/height/zoom bucket | 滚动不卡 |
| P5-5 | loading/failed 状态 | 缩略图未生成、生成失败都有占位 | 不显示空白或假图 |

---

## 9. P6：截图验收工具修复

当前 `tools/capture_edit_page.py --help` 直接报：

```text
SyntaxError: name 'DEFAULT_CAPTURE_DELAY' is used prior to global declaration
```

必须修，因为 UI 优化没有截图验收就是盲改。

| 编号 | 任务 | 验收 |
|---|---|---|
| P6-1 | 修复 `global DEFAULT_CAPTURE_DELAY` 语法错误 | `python tools/capture_edit_page.py --help` 成功 |
| P6-2 | 增加 timeline-only 截图模式 | 可截 1365x768、1440x900、1920x1080 |
| P6-3 | 增加测试素材场景 | 空时间线、1V1A、多轨、不同轨高、长时间线 |
| P6-4 | 保存截图到固定目录 | `captures/timeline/日期/` |
| P6-5 | 增加视觉 checklist | 每张图检查 label、overlap、V/A split、playhead、ruler |

---

## 10. P7：测试矩阵

最低需要补齐这些测试：

| 测试 | 目的 |
|---|---|
| `test_linked_drag_finished_moves_video_and_audio_atomically` | 防止 linked move 再次失败 |
| `test_track_header_badge_uses_type_local_index` | 防止 A1 显示成 A2 |
| `test_video_and_audio_target_tracks_are_independent` | 防止 target 互相覆盖 |
| `test_v1_a1_are_adjacent_to_splitter_after_resize` | 防止中间空白再次变大 |
| `test_drag_preview_matches_final_drop_target` | 防止 preview/drop 不一致 |
| `test_audio_drop_on_video_area_invalid` | 防止音频落错区 |
| `test_video_drop_on_audio_area_invalid` | 防止视频落错区 |
| `test_locked_track_rejects_move_and_drop` | 防止锁轨无效 |
| `test_clip_drag_mouse_event_reaches_clip_item` | 防止 View 吞掉 clip drag |
| `test_capture_tool_help_runs` | 防止截图验收工具坏掉 |

每轮提交前至少跑：

```bash
.venv/bin/python -m pytest tests/edit -q
.venv/bin/python -m py_compile src/features/edit/ui/timeline_layout.py src/features/edit/ui/timeline_view.py src/features/edit/ui/timeline_items.py
```

---

## 11. 推荐执行顺序

| 顺序 | 阶段 | 为什么 |
|---|---|---|
| 1 | P0 | 先恢复能移动、能同步、视觉不明显错 |
| 2 | P6-1 | 修截图工具，后面每次视觉优化都能验收 |
| 3 | P1 | 坐标和 hit-test 不统一，后面都会反复炸 |
| 4 | P2 | 鼠标事件不稳，所有交互都会不可信 |
| 5 | P3 | linked/multi move 是剪辑软件核心手感 |
| 6 | P4 | 架构稳定后再做 DaVinci-like 视觉 |
| 7 | P5 | 缩略图/波形/缓存提升质感和性能 |
| 8 | P7 | 把每个修复沉淀成测试 |

---

## 12. 每阶段验收截图清单

### 空时间线

- 有 ruler。
- 有 V/A 区域。
- splitter 在中间。
- V1/A1 header 可见。
- 无大片无意义黑洞。

### 单个视频带音频

- V1 clip 和 A1 clip 起点对齐。
- V1 在 splitter 上方，A1 在 splitter 下方。
- linked icon 可见。
- 拖动视频时音频同步。

### 多轨

- V3/V2/V1 上到下，V1 贴 splitter。
- A1/A2/A3 上到下，A1 贴 splitter。
- badge 显示正确。
- 调轨高后不重叠。

### 长时间线

- 横向滚动标尺和 clip 对齐。
- 缩放时文字不溢出。
- 缩略图/波形不会卡死。

---

## 13. 明确不要做的事

- 不要继续在 `paint()` 里改 position。
- 不要在不同 item 里重复计算 track y。
- 不要让 preview 和 drop 分别走两套 target 逻辑。
- 不要拖动中提交 command。
- 不要让 linked move 分成多个 undo 步骤。
- 不要没有截图就宣称 UI 改好了。
- 不要先做花哨视觉，再修拖拽和命中。

---

## 14. 下一步最小任务包

如果你接着优化，建议从这个最小任务包开始：

1. 修 `tools/capture_edit_page.py` 语法错误。
2. 截一张当前修复后的 timeline 图。
3. 补 `test_track_header_badge_uses_type_local_index`。
4. 补 `test_video_and_audio_target_tracks_are_independent`。
5. 补 `test_clip_drag_mouse_event_reaches_clip_item`。
6. 统一 `resolve_drop_target()`。
7. 再开始重绘 track header 和 clip。

完成这组之后，再进入真正的 DaVinci-like 视觉精修会稳很多。
