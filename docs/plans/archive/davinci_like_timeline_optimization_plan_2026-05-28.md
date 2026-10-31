# DaVinci-like 时间线面板优化计划表

日期：2026-05-28

目标：把 Topos Color/Edit 当前时间线面板提升到「85%-95% 视觉接近 DaVinci Resolve、核心体验接近专业 NLE」的水平，同时优先修复当前最影响使用的音视频轨道错位和布局稳定性问题。

适用范围：

- `src/features/edit/ui/timeline_panel.py`
- `src/features/edit/ui/timeline_view.py`
- `src/features/edit/ui/timeline_items.py`
- `src/features/edit/core/timeline_v2.py`
- `src/features/edit/core/timeline_ops.py`
- `src/features/edit/core/commands.py`
- `src/features/edit/core/audio_waveform.py`
- `src/shared/media/thumbnail_provider.py`
- 相关测试：`tests/edit/`、`tools/capture_edit_page.py`

不在本计划范围内：

- 不修改调色页核心渲染管线。
- 不修改 ModernGL/GLSL 图像处理核心。
- 不做 DaVinci 商标、图标、专有布局的像素级复制；只复刻专业剪辑体验、视觉层次和交互逻辑。

---

## 1. 总体目标

| 目标 | 当前问题 | 优化方向 | 验收标准 |
|---|---|---|---|
| 专业时间线观感 | 当前面板像原型工具，轨道、clip、标尺、工具栏层级弱 | 建立 DaVinci-like 深色主题、专业轨道头、清晰视频/音频分区、精细 clip 绘制 | 第一眼能看出视频区、音频区、播放头、标尺、轨道头和素材状态 |
| 稳定拖拽体验 | 音频素材拖拽时向上偏移，和视频片段重叠 | 统一坐标系统，音频轨道 Y 坐标只由音频区域和轨道索引决定 | 拖动视频 clip 时，链接音频始终停留在 A 区对应轨道，不出现上漂 |
| 轨道高度可调 | 当前轨道高度基本依赖固定常量，视频/音频高度混用风险高 | 增加轨道高度模型、拖拽手柄、最小/最大高度、布局重算 | 用户可拖动单轨高度，也可拖动视频/音频区域分割线 |
| 视频/音频区域分割 | 当前轨道顺序是线性堆叠，缺少明确上下区域 | 默认中线分割，上半部分视频、下半部分音频，支持拖动中间分割线 | 时间线中间有可识别分割线，拖动后 V 区/A 区大小同步更新 |
| 核心体验接近 NLE | 选择、拖拽、吸附、缩放、修剪等状态易互相干扰 | 明确交互状态机和命令提交边界 | 拖拽预览、释放提交、撤销重做、链接移动行为一致 |

---

## 2. P0 阶段：先修复音视频拖拽错位和区域布局

优先级最高。此阶段不追求视觉精修，先把坐标系统和轨道区域修稳。

| 编号 | 任务 | 当前风险/症状 | 实施要点 | 涉及文件 | 验收标准 |
|---|---|---|---|---|---|
| P0-1 | 建立唯一时间线几何布局契约 | `ClipItem.paint()`、拖拽预览、轨道命中测试可能各自计算 Y；混合视频/音频高度时容易错位 | 在 `TimelineView` 内集中提供 `_track_top_for_id()`、`_track_top_for_index()`、`_track_height_for()`、`_track_index_at_y()`、`_frame_to_x()`、`_x_to_frame()` | `timeline_view.py`、`timeline_items.py` | 所有 clip/header/ruler/grid 使用同一套坐标，缩放只影响 X，不影响 Y |
| P0-2 | 禁止 item 在 paint 中改变 scene position | 绘制过程中 `setPos()` 会造成残影、跳动、Y 漂移 | `ClipItem.paint()` 只画本地内容，不再修正 `setPos()`；位置更新统一由 `TimelineView._layout_track_items()` 完成 | `timeline_items.py` | 拖动时没有突然跳轨、旧位置残影明显减少 |
| P0-3 | 修复音视频链接拖拽对齐 | 当前视频拖拽后，链接音频可能用错误 track index 或固定高度计算，导致向上偏移并与视频重叠 | 拖拽时建立 `MoveGroup`：主 clip、linked clip、选中组统一用 `delta_frames`；Y 坐标不跟鼠标自由漂移，只锁定到各自轨道 | `timeline_view.py`、`timeline_items.py`、`commands.py` | 视频 clip 横向拖动时，音频 clip 同步横移但始终留在 A1/A2 区域 |
| P0-4 | 引入视频区/音频区双区域布局 | 当前 `video_tracks + audio_tracks` 线性堆叠，缺少区域高度概念 | 新增 `TimelineLayoutState`：`video_area_height`、`audio_area_height`、`splitter_y`、`track_heights`；默认分割线在时间线轨道区域中点 | `timeline_view.py` | 默认上半部分只放视频轨，下半部分只放音频轨 |
| P0-5 | 中间分割线可拖拽 | 用户要求视频和音频分割线在中间并可调整区域大小 | 增加 `TimelineAreaSplitterItem` 或在 `TimelineView` 中绘制/命中 6px splitter；拖拽时更新 video/audio area 高度；限制最小区域高度 | `timeline_view.py`、`timeline_items.py` | 鼠标靠近中线变成上下调整光标，拖动后 V/A 区比例变化 |
| P0-6 | 单轨高度可调 | 用户要求轨道高度可调 | 每个 `TrackHeaderItem` 和 track body 底部增加 resize handle；更新对应 track height；重新布局 header、clip、grid、sceneRect | `timeline_items.py`、`timeline_view.py` | 拖动 V1/A1 下边缘可改变该轨高度，clip 高度同步变化 |
| P0-7 | 轨道命中测试重写 | Drop/drag 可能把音频识别到视频轨，或把 ruler 区识别成 V1 | `_track_index_at_y()` 根据区域和 cumulative track top 判断；ruler/header/空白区返回 `None` 或 `-1` | `timeline_view.py` | 在标尺上拖拽不误判为 V1；A 区命中只返回音频轨 |
| P0-8 | 拖拽预览锁轨 | 拖拽预览可能跟随鼠标 Y 自由移动 | 预览 item 的 Y 来自目标轨 top；目标轨只在跨越有效轨道中心线时变化 | `timeline_view.py`、`timeline_items.py` | 预览不会半截压到两条轨之间，也不会盖住错误媒体类型区域 |
| P0-9 | 添加布局回归测试 | 这种 bug 很容易后续反复出现 | 增加纯函数/轻量 Qt 测试：V 区/A 区 top、track height、clip scene rect、linked drag 后 y 不变 | `tests/edit/test_timeline_view_layout.py` | 测试覆盖音视频 linked clip 拖拽后 Y 坐标仍正确 |

P0 交付结果：时间线还不一定最漂亮，但视频与音频轨道边界明确，拖拽不再上漂重叠，轨道高度和中间分割线具备基础可调能力。

---

## 3. P1 阶段：DaVinci-like 基础视觉骨架

| 编号 | 任务 | 设计目标 | 实施要点 | 涉及文件 | 验收标准 |
|---|---|---|---|---|---|
| P1-1 | 重做时间线背景层级 | 达到 DaVinci 风格的深灰层次，而不是一整块黑色 | 面板背景、标尺背景、轨道背景、空白区、网格线分别使用不同灰阶 | `edit_style.py`、`timeline_view.py` | 肉眼可分辨 panel/ruler/track/empty area |
| P1-2 | 重做顶部工具栏 | 当前工具按钮存在但密度、层次不够专业 | 工具组分区：选择/修剪/刀片/吸附/链接/标记/缩放/音量；按钮尺寸统一，激活态清晰 | `timeline_panel.py` | 工具栏接近 DaVinci 的紧凑专业工具条 |
| P1-3 | 重做时间码显示 | 当前时间码在底部/工具区存在感不稳定 | 左侧大号 SMPTE timecode，等宽字体，格式 `01:00:00:00`，可点击输入 | `timeline_panel.py` | 时间码位置清晰，不随内容跳动 |
| P1-4 | 重做时间标尺 | 当前标尺刻度弱，缩放层级不足 | 动态 LOD：帧、秒、10秒、分钟刻度按缩放显示；主刻度长线，副刻度短线 | `timeline_items.py` | 缩放时刻度不会挤成一团，clip 左边缘与刻度对齐 |
| P1-5 | 播放头专业化 | 当前播放头较粗糙 | 顶部红色三角、垂直 1-2px 红线、轻微阴影，高 z-order 覆盖 clip | `timeline_items.py` | 播放头在任何轨道高度下完整贯穿 V/A 区 |
| P1-6 | 网格线与区域线 | 当前背景线不够指导剪辑 | 主刻度垂直线贯穿 V/A 区，轨道分隔线细且低对比 | `timeline_view.py` | 用户可以准确感知时间和轨道边界 |

P1 交付结果：时间线第一眼明显更像专业 NLE，层次、标尺、播放头、工具栏达到可用的 DaVinci-like 基础风格。

---

## 4. P2 阶段：轨道头和轨道管理

| 编号 | 任务 | 设计目标 | 实施要点 | 涉及文件 | 验收标准 |
|---|---|---|---|---|---|
| P2-1 | 重做 TrackHeaderItem | 左侧轨道头接近 DaVinci：V1/A1、锁定、目标轨、静音/独奏等清晰 | 分为编号区、名称区、控制区；视频显示 V1，音频显示 A1；增加 hover/active 状态 | `timeline_items.py` | 轨道头不再像简单按钮堆叠 |
| P2-2 | 视频/音频目标轨 | 专业剪辑软件需要明确 source patch/target track | 视频目标轨与音频目标轨分别维护；选中目标轨时左侧或边缘高亮 | `timeline_view.py` | V1/A1 可以分别设为目标轨 |
| P2-3 | 轨道锁定 | 锁定轨道不可拖入、不可移动、不可修剪 | 命中测试和 command 执行前检查 track.locked | `timeline_view.py`、`commands.py` | 锁定轨拖拽时显示 invalid preview |
| P2-4 | 音频 M/S 控制 | 音频区要有 mute/solo 状态 | Header 按钮状态映射到 `EditTrack.muted/solo`，样式用红/绿激活态 | `timeline_items.py`、`commands.py` | M/S 状态可见、可撤销 |
| P2-5 | 轨道高度预设 | 方便快速切换小/中/大轨道 | 增加右键菜单或快捷入口：Small/Medium/Large/Auto | `timeline_items.py`、`timeline_view.py` | 用户可快速恢复合理高度 |
| P2-6 | 区域折叠/展开 | 长时间线需要隐藏视频或音频区 | 支持视频区/音频区折叠，分割线保留 | `timeline_view.py` | 折叠后不破坏 clip 数据和拖拽命中 |

P2 交付结果：轨道头开始具备真实剪辑软件的控制能力，视频/音频区域和轨道状态都能被用户清楚理解。

---

## 5. P3 阶段：Clip 绘制精修

| 编号 | 任务 | 当前问题 | 实施要点 | 涉及文件 | 验收标准 |
|---|---|---|---|---|---|
| P3-1 | 视频 clip 外观 | 当前视频 clip 边框、文字、缩略图混乱 | 蓝色主体、顶部标签栏、1px 高光、选中橙色描边、圆角 2-3px | `timeline_items.py` | 视频 clip 观感接近 DaVinci 的蓝色片段 |
| P3-2 | 音频 clip 外观 | 当前音频波形和背景不够专业 | 绿色主体、波形浅绿色、中心线对称、clip 名称在底部或顶部标签区 | `timeline_items.py`、`audio_waveform.py` | 音频 clip 不再像一块简单绿条 |
| P3-3 | 缩略图策略 | 当前缩略图密集且容易变形 | 支持 `filmstrip` 和 `start_end` 两种模式；默认 DaVinci-like filmstrip，缩小时自动隐藏 | `timeline_items.py`、`thumbnail_provider.py` | 缩略图不拉伸，缩放后仍清晰 |
| P3-4 | 文本防溢出 | 文件名可能压出 clip 或被缩略图盖住 | elide 文本，宽度不足时隐藏；文字下方半透明暗底 | `timeline_items.py` | 窄 clip 不出现文字溢出 |
| P3-5 | 链接图标 | 音视频链接状态不明显 | clip 左下角绘制小链接图标；断开链接后隐藏/变灰 | `timeline_items.py` | 用户能看出视频与音频是否 linked |
| P3-6 | 修剪手柄 | 当前修剪状态不够明显 | hover 到 clip 左右边缘显示 trim cursor 和高亮 handle | `timeline_items.py` | 用户能准确知道是否进入 trim 模式 |
| P3-7 | LOD 绘制 | 长时间线绘制容易卡 | 按 clip 屏幕宽度切换：纯色、文本、缩略图、完整信息 | `timeline_items.py` | 缩放和滚动保持流畅 |

P3 交付结果：clip 的质感、信息密度、选中态、hover 态和媒体特征显著接近专业剪辑软件。

---

## 6. P4 阶段：核心剪辑交互体验

| 编号 | 任务 | 目标 | 实施要点 | 涉及文件 | 验收标准 |
|---|---|---|---|---|---|
| P4-1 | 交互状态机 | 防止选择、拖拽、修剪、框选互相抢状态 | 引入 `InteractionState`：Idle/Selecting/Dragging/Trimming/Splitting/Panning/Resizing | `timeline_view.py` | 同一时间只有一个主交互状态 |
| P4-2 | linked move 宏命令 | 视频和音频要一起移动、一起撤销 | 主 clip 和 linked clip 用一个 `MacroCommand` 提交 | `commands.py`、`timeline_view.py` | 一次撤销同时还原音视频位置 |
| P4-3 | 吸附 | 核心体验接近 DaVinci | 吸附到播放头、clip 起止点、标记、相邻剪辑；显示吸附线 | `timeline_ops.py`、`timeline_view.py` | 拖拽接近目标时有稳定吸附反馈 |
| P4-4 | 框选/多选 | 专业时间线必须支持批量操作 | Rubber band 只选择轨道区内 clip，Shift/Cmd 追加选择 | `timeline_view.py` | 多选后拖动保持相对距离 |
| P4-5 | 修剪左/右边界 | clip 基础编辑能力 | Trim left/right 区分 source_start、timeline_start、duration | `timeline_items.py`、`commands.py` | 修剪不会改变未选中 linked clip 的错误 Y |
| P4-6 | 刀片工具 | 接近 NLE 标准工具 | Blade 在播放头或点击位置 split，linked clip 同步 split 可选 | `timeline_view.py`、`timeline_ops.py` | 分割后视频音频仍对应 |
| P4-7 | 手型平移/滚轮缩放 | 时间线浏览手感 | Option/Alt + wheel 横向缩放，Shift + wheel 横向滚动，普通 wheel 垂直/横向按平台规范 | `timeline_view.py` | 缩放中心稳定在鼠标位置附近 |

P4 交付结果：时间线从“能摆 clip”提升到“能进行基础剪辑”，并且 linked audio/video 行为稳定。

---

## 7. P5 阶段：音频波形、缩略图和缓存性能

| 编号 | 任务 | 目标 | 实施要点 | 涉及文件 | 验收标准 |
|---|---|---|---|---|---|
| P5-1 | 波形时间映射修正 | 波形显示必须对应 source range | 明确 timeline frame、source frame、audio seconds 的换算；避免简单用视频 fps 代替音频时间基 | `audio_waveform.py`、`timeline_items.py` | 修剪 audio 后波形显示对应修剪后的片段 |
| P5-2 | 波形缓存 | 滚动/缩放不能重复计算 | 按 asset_id + source range + zoom bucket 缓存 pixmap/path | `audio_waveform.py` | 同一 clip 重绘不重新抽取音频 |
| P5-3 | 缩略图缓存 | 视频 filmstrip 流畅 | 按 asset_id + frame + target_height 缓存 QPixmap；后台生成，UI 线程只消费 | `thumbnail_provider.py`、`timeline_items.py` | 滚动时不会明显掉帧 |
| P5-4 | 可见区域绘制 | 长时间线必须只画可见内容 | 根据 viewport visible rect 只更新/请求可见 clip 的缩略图和波形 | `timeline_view.py` | 100 个 clip 时滚动仍可用 |
| P5-5 | 资源释放 | 避免长时间编辑内存上涨 | 缓存 LRU、timeline unload 时释放无用 pixmap | `thumbnail_provider.py`、`audio_waveform.py` | 重复打开项目内存不持续增长 |

P5 交付结果：视觉素材加载更像专业软件，波形和缩略图在正确时间范围内显示，并且不会因为大量素材拖慢时间线。

---

## 8. P6 阶段：验收、截图对比和防回归

| 编号 | 任务 | 目标 | 实施要点 | 涉及文件 | 验收标准 |
|---|---|---|---|---|---|
| P6-1 | 自动截图验收 | 每次改 UI 都能看见实际效果 | 扩展 `tools/capture_edit_page.py`，生成空时间线、单视频带音频、多轨、多高度截图 | `tools/capture_edit_page.py` | 产出固定尺寸截图用于人工/自动对比 |
| P6-2 | 布局单测 | 防止音频上漂复发 | 测试分割线、track top、clip scene rect、linked drag y-lock | `tests/edit/` | P0 bug 有明确测试覆盖 |
| P6-3 | 交互集成测试 | 验证拖拽/修剪/撤销 | 使用 Qt 测试或轻量模拟调用 command | `tests/edit/` | linked move 撤销/重做通过 |
| P6-4 | 性能基线 | 保证视觉升级不把时间线拖慢 | 记录 10/50/100 clips 的布局和重绘耗时 | `tests/edit/`、`tools/` | 重绘耗时在可接受范围内 |
| P6-5 | DaVinci-like 视觉对照清单 | 防止优化跑偏 | 用截图逐项检查：标尺、播放头、轨道头、clip、分割线、缩放、音频区 | `docs/` | 达到 85%-95% 主观视觉接近 |

P6 交付结果：不靠感觉收尾，有截图、有测试、有性能基线，后续继续加功能不容易把基础布局打坏。

---

## 9. 推荐实施顺序

| 顺序 | 阶段 | 原因 | 预计风险 |
|---|---|---|---|
| 1 | P0-1 到 P0-3 | 当前最大 bug 是音视频拖拽错位，必须先修坐标源头 | 中等，涉及现有 item 布局习惯 |
| 2 | P0-4 到 P0-8 | 视频/音频中线分区和轨道高度是整体布局基础 | 中高，需要避免破坏 drop/drag 命中 |
| 3 | P0-9 + P6-2 | 先把 bug 写成测试，后面视觉重构才敢动 | 低 |
| 4 | P1 | 视觉骨架依赖稳定坐标系统 | 中 |
| 5 | P2 | 轨道头依赖稳定 track layout 和 area split | 中 |
| 6 | P3 | clip 精修依赖稳定 item geometry | 中 |
| 7 | P4 | 交互体验会触碰 command 和 linked clip 逻辑 | 高 |
| 8 | P5/P6 | 性能、缓存、截图验收收尾 | 中 |

---

## 10. P0 详细技术设计

### 10.1 时间线布局状态

建议在 `TimelineView` 内部新增一个轻量状态对象：

```python
@dataclass
class TimelineLayoutState:
    track_header_width: int
    ruler_height: int
    video_area_height: int
    audio_area_height: int
    splitter_height: int
    track_heights: dict[str, int]
```

关键规则：

- 视频轨只在 video area 内布局。
- 音频轨只在 audio area 内布局。
- 分割线位于 video area 和 audio area 中间。
- 默认 `video_area_height == audio_area_height`，也就是分割线在轨道区中间。
- 当面板高度改变时，保持用户设置过的比例；没有用户设置时继续 50/50。
- 每条轨道都有独立高度，缺省值来自 `EDIT_SIZES["track_h_video"]` / `EDIT_SIZES["track_h_audio"]`。

### 10.2 坐标函数必须成为唯一入口

建议统一为：

```python
def _ordered_video_tracks(self) -> tuple[EditTrack, ...]: ...
def _ordered_audio_tracks(self) -> tuple[EditTrack, ...]: ...
def _track_height_for(self, track: EditTrack) -> int: ...
def _video_area_top(self) -> float: ...
def _audio_area_top(self) -> float: ...
def _splitter_rect(self) -> QRectF: ...
def _track_top_for_id(self, track_id: str) -> float: ...
def _track_rect_for_id(self, track_id: str) -> QRectF: ...
def _track_at_scene_y(self, y: float, media_type: str | None = None) -> EditTrack | None: ...
def _frame_to_x(self, frame: int) -> float: ...
def _x_to_frame(self, x: float) -> int: ...
```

任何地方禁止再写：

```python
TRACK_AREA_TOP + track_index * (TRACK_HEIGHT + TRACK_GAP)
```

除非它只存在于旧代码注释或将被删除的兼容层里。

### 10.3 linked audio/video 拖拽规则

拖拽视频 clip 时：

1. 找到主 clip。
2. 如果 `linked_clip_id` 存在，找到链接音频 clip。
3. 两者组成一个 move group。
4. 鼠标横向移动只计算 `delta_frames`。
5. 主 clip 的 Y 仍来自视频轨。
6. linked audio 的 Y 仍来自音频轨。
7. 释放鼠标后用一个 `MacroCommand` 提交所有 `MoveClipCommand`。

拖拽音频 clip 时同理：

- linked video 不会被拖到音频区。
- linked video 的 Y 始终来自视频轨。
- 如果用户明确允许解除链接或单独移动，必须通过 modifier 或右键命令，不作为默认行为。

### 10.4 分割线拖拽规则

分割线行为：

- 默认在可用 track area 的 50%。
- 命中区域建议 6px 到 8px。
- 鼠标 hover 显示 `SizeVerCursor`。
- 拖动时实时更新 `video_area_height` 和 `audio_area_height`。
- 最小视频区高度：至少容纳 1 条视频轨的最小高度。
- 最小音频区高度：至少容纳 1 条音频轨的最小高度。
- 拖动结束后保存到用户偏好或项目 UI state。

### 10.5 单轨高度拖拽规则

轨道 resize handle：

- 每条轨道底部 4px-6px 命中区域。
- hover 显示上下调整光标。
- 拖动只修改该 track 的高度。
- 高度范围建议：
  - 视频轨：`36px - 160px`
  - 音频轨：`28px - 120px`
- 双击 handle 恢复默认高度。

---

## 11. 视觉规格建议

| 元素 | 建议规格 |
|---|---|
| 面板背景 | `#15161A` |
| 标尺背景 | `#191A1F` |
| 轨道背景 | `#1B1D22` |
| 轨道交替背景 | `#202229` |
| 网格主线 | `rgba(255,255,255,0.12)` |
| 网格副线 | `rgba(255,255,255,0.05)` |
| 视频 clip | `#2F6F9F` / 深蓝青 |
| 音频 clip | `#2F8A57` / 深绿 |
| 选中描边 | `#E29227` |
| 播放头 | `#FF4D3D` |
| 分割线 | `#0F1013` 背景 + `#3A3D45` 细线 |
| 文字 | `#D6D8DE` |
| 次级文字 | `#8E929A` |

---

## 12. 最小可交付版本

如果只做一个最短闭环版本，建议只做以下项目：

| 必做项 | 交付结果 |
|---|---|
| P0-1 坐标统一 | 所有元素不再各算各的 |
| P0-2 paint 不改位置 | 拖拽残影和跳动明显减少 |
| P0-3 linked drag 修复 | 音频不会向上偏移并压住视频 |
| P0-4/P0-5 视频音频中线分割 | 默认上视频、下音频，分割线可拖 |
| P0-6 单轨高度可调 | 轨道高度可调 |
| P0-9 布局测试 | 防止最大 bug 回归 |
| P1-1/P1-4/P1-5 | 背景、标尺、播放头达到专业基本观感 |

完成这 7 项后，时间线面板会从「简陋且容易错位」进入「结构稳定、视觉明显接近专业剪辑软件」的阶段。

---

## 13. 85%-95% 接近 DaVinci 的明确验收定义

这里的「85%-95%」不是像素级复制，而是用户一眼看到和实际操作时，都感觉这是一个成熟 NLE 时间线，而不是原型控件。

| 维度 | 85% 标准 | 95% 标准 | 不要求 |
|---|---|---|---|
| 视觉层级 | 深色专业面板、清晰标尺、V/A 分区、clip 有缩略图/波形/选中态 | 工具栏、轨道头、clip、播放头、滚动条、分割线都有统一质感和细节状态 | 不复制 DaVinci 商标、专有图标、完全相同像素 |
| 布局稳定性 | 缩放、拖拽、调整轨道高度时不跳动、不错位 | 复杂多轨、多选、linked clip、滚动中仍稳定 | 不要求一次覆盖 Fairlight/Fusion 全部高级能力 |
| 核心剪辑 | 选择、移动、linked move、修剪、刀片、吸附、撤销重做可用 | ripple/roll/slip/slide 等高级修剪具备稳定预览和提交 | 不要求一次实现所有 DaVinci 快捷键 |
| 媒体表现 | 视频 filmstrip、音频波形、clip label、link icon 正常 | 缩略图/波形有缓存、LOD、空白/失败态和异步加载态 | 不要求所有 codec 的极限性能 |
| 操作手感 | 鼠标命中区、光标、hover、拖拽预览清晰 | 每个操作都有 preview、invalid state、snap feedback 和 rollback | 不要求完全复刻 DaVinci 内部状态机 |
| 工程质量 | 坐标系统统一，P0 bug 有测试 | 视觉截图、交互测试、性能基线完整 | 不追求一次性大重写所有 Edit 模块 |

---

## 14. 必须提前防范的问题总清单

这些是专业时间线最容易出 bug 的地方，应该在实现和测试里逐项覆盖。

| 类型 | 问题 | 典型表现 | 预防方案 |
|---|---|---|---|
| 坐标 | X/Y 坐标由多个类重复计算 | clip 跳轨、音频上漂、缩放后错位 | `TimelineView` 成为唯一 geometry owner |
| 绘制 | `paint()` 修改 item 位置或尺寸 | 残影、闪烁、拖动卡顿 | paint 只绘制，不改变 scene state |
| 高度 | 视频轨/音频轨使用同一固定高度 | A 区 clip 压到 V 区 | 每条 track 存独立高度 |
| 分区 | 视频/音频线性堆叠，无区域边界 | 音频 drop 到视频区，视觉混乱 | `video_area` / `audio_area` 双区域布局 |
| linked | linked audio/video 只移动主 clip | 音画不同步、撤销只撤一半 | `MoveGroup` + `MacroCommand` |
| drop | 外部素材 drop 时先按鼠标 Y 自由定位 | drop preview 和最终 clip 不一致 | drop preview 也走统一 track hit test |
| selection | 多选和 linked selection 混乱 | 只选视频却误移动音频，或反过来 | 明确 linked selection toggle 和 modifier |
| trim | 修剪混淆 source/timeline duration | clip 时长错误、波形错位 | 分离 timeline frame/source frame/audio seconds |
| undo | 拖拽过程产生大量 command | 撤销需要按很多次，或者状态错乱 | 拖拽中只 preview，释放时提交一次 command |
| snapping | snap 点太多或阈值不随缩放 | 拖动粘滞、不听手 | snap threshold 用屏幕像素换算 frame |
| waveform | 音频波形使用视频 fps 推导 | 波形和声音内容对不上 | 音频时间基独立管理 |
| thumbnail | 缩略图同步解码 | 拖动/缩放卡死 | 后台生成，UI 线程只画缓存 |
| sceneRect | scene 尺寸不随内容/区域变化 | 滚动条错误，空白区截断 | 每次布局统一更新 scene rect |
| scrollbar | 横向滚动和 header/ruler 不同步 | 标尺和 clip 位置错开 | header 固定，ruler/track 同一 scroll origin |
| DPI | Retina/高 DPI 下线条模糊 | 1px 线发虚、图标糊 | 使用 device pixel ratio aware pixmap/cache |
| i18n | 中文文件名/轨道名截断异常 | 文字重叠、乱码 | elide text，字体 fallback，保留 UTF-8 |

---

## 15. 推荐模块拆分

当前 `TimelineView` 和 `timeline_items.py` 已经承担很多职责。要达到接近 DaVinci 的复杂度，建议逐步拆出以下内部模块。可以先放在 `src/features/edit/ui/`，不急着建立过度抽象。

| 模块 | 责任 | 可先实现为 |
|---|---|---|
| `timeline_layout.py` | frame/x、track/y、区域分割、track 高度、sceneRect 计算 | 纯 Python 类，便于单测 |
| `timeline_interaction.py` | Idle/Dragging/Trimming/Resizing/Panning 状态机 | `TimelineInteractionState` + helper |
| `timeline_paint_style.py` | DaVinci-like 颜色、尺寸、pen/brush 工厂 | 对 `edit_style.py` 的补充 |
| `timeline_hit_test.py` | clip edge、track header button、splitter、empty area 命中 | 可先是 `TimelineView` 的私有方法 |
| `timeline_drag_group.py` | linked clip、多选 clip、preview rect、合法性验证 | dataclass |
| `timeline_cache_policy.py` | 缩略图/波形/LOD 请求策略 | 后期再拆 |

第一阶段可以不创建所有文件，但要按这个职责边界写代码，避免再把绘制、命中、命令、缓存全塞在一个方法里。

---

## 16. 详细数据模型补充

| 数据 | 建议字段 | 用途 | 持久化建议 |
|---|---|---|---|
| 时间线 UI 布局 | `video_area_ratio`、`track_heights`、`collapsed_areas` | 恢复用户调整过的 V/A 区比例和轨道高度 | 项目 UI state 或用户偏好 |
| Track 显示状态 | `height`、`is_target`、`is_expanded`、`display_index` | 轨道高度、目标轨、折叠状态 | 项目 UI state |
| Clip UI 缓存键 | `asset_id`、`source_start`、`source_duration`、`height`、`zoom_bucket` | 缩略图/波形缓存 | 运行时缓存，不进项目文件 |
| Interaction session | `state`、`start_pos`、`current_pos`、`move_group`、`snap_result` | 拖拽/修剪过程中的临时状态 | 不持久化 |
| Linked group | `primary_clip_id`、`linked_clip_ids`、`selection_policy` | 音视频同步移动和多选 | clip model 已有 linked id，必要时扩展 group |

实现原则：

- 项目业务模型保持纯数据，不依赖 PyQt。
- UI 布局状态不要污染剪辑语义模型，优先放 project UI state。
- 运行时 interaction state 不进 undo stack。
- command stack 只记录用户最终操作，不记录鼠标移动过程。

---

## 17. 交互状态机细化

| 状态 | 进入条件 | 鼠标移动 | 鼠标释放 | 允许的光标/反馈 |
|---|---|---|---|---|
| `Idle` | 默认 | hover hit test | 无 | 普通、手型、左右 trim、上下 resize |
| `Selecting` | 空白区按下或 Shift/Cmd 选择 | 更新 rubber band | 更新 selection | 半透明选择框 |
| `DraggingClip` | clip body 按下并超过阈值 | 更新 move group preview、snap、invalid overlay | 提交 `MacroCommand` | 移动光标、snap line |
| `TrimmingLeft` | clip 左边缘按下 | 更新左 trim preview | 提交 `TrimClipCommand` 或 linked trim | 左 trim 光标 |
| `TrimmingRight` | clip 右边缘按下 | 更新右 trim preview | 提交 trim | 右 trim 光标 |
| `BladeHover` | blade 工具移动 | 显示切割预览线 | 点击执行 split | blade 光标 |
| `Panning` | hand 工具或中键 | 移动 viewport | 停止 pan | 抓手光标 |
| `Zooming` | zoom 工具或快捷滚轮 | 以鼠标点缩放 | 停止 | 放大镜光标 |
| `ResizingTrack` | track resize handle | 更新单轨高度 | 保存高度 | 上下 resize |
| `ResizingArea` | V/A splitter | 更新视频/音频区域比例 | 保存比例 | 上下 resize |
| `ContextMenu` | 右键 | 无 | 执行动作 | 菜单 |

状态机硬规则：

- 进入非 Idle 状态时记录 session id，避免异步更新误改旧状态。
- 拖拽过程中不直接改 model，只改 preview item 或临时 item geometry。
- 释放鼠标才执行 command。
- Esc 取消当前 interaction，并恢复所有 preview。
- 如果拖拽过程中时间线模型被外部刷新，取消当前 interaction，避免提交到旧 clip。

---

## 18. 鼠标命中与交互细节

| 区域 | 命中规则 | 反馈 | 常见 bug 防护 |
|---|---|---|---|
| 标尺 | y 在 ruler rect 内 | 点击/拖动移动 playhead | 不允许返回 V1 track |
| 播放头头部 | 三角区域扩大 4px 命中 | 拖动 playhead | 不和 clip drag 抢事件 |
| track header | x 小于 header width | 轨道选择/目标轨/按钮 | header 事件不传给 clip area |
| clip body | clip rect 内，非边缘 | 选择/拖拽 | Y 锁定到 track |
| clip 左边缘 | 左 6px-8px | trim cursor | 窄 clip 时边缘区不超过 clip 一半 |
| clip 右边缘 | 右 6px-8px | trim cursor | 同上 |
| fade handle | 视频 clip 上角区域 | fade cursor/handle | 只有足够宽时显示 |
| splitter | V/A 中线 6px-8px | 上下 resize | 不误触发 track resize |
| track resize | 每轨底部 4px-6px | 上下 resize | splitter 优先级高于单轨 resize |
| 空白 track 区 | 有效 track rect 内无 clip | 选择框/drop | drop 按媒体类型找目标轨 |
| 空白非轨道区 | track 区之外 | 无或 pan | 不创建 clip、不改变 selection |

---

## 19. 轨道系统细化

| 功能 | 细节要求 | 验收 |
|---|---|---|
| V/A 默认分区 | 时间线轨道区域默认 50/50；上方视频、下方音频 | 新建项目时分割线在中间 |
| 多视频轨 | V1、V2、V3 按专业习惯垂直排列；建议 V1 靠近分割线，上方是 V2/V3 | V1/A1 贴近中线，方便 linked clip 对齐 |
| 多音频轨 | A1、A2、A3 在下半区排列；A1 靠近分割线 | linked audio 默认落 A1 |
| 轨道高度 | 每轨可单独调；双击恢复默认；右键预设 small/medium/large | 高度改变后 clip/header/grid 同步 |
| 区域高度 | 拖中线调整 V/A 可视空间；不改变 clip 数据 | 分割线拖动只影响布局 |
| 区域滚动 | 如果 V 区轨道过多，可考虑区域内滚动或整体垂直滚动 | 不会把音频轨挤到视频区 |
| 轨道目标 | 视频目标轨和音频目标轨分开 | drop video/audio 时落入对应目标轨 |
| 轨道锁定 | locked track 禁止 drop/move/trim/delete | invalid overlay 清楚 |
| 轨道启用 | 视频 track enable/disable，音频 mute/solo | 状态影响预览/播放计划 |
| 轨道命名 | 支持重命名，默认 `视频 1` / `音频 1` | 中文/英文都不溢出 |

关于轨道排序：

- 视觉上推荐：视频区从上到下为 V3、V2、V1，V1 靠近分割线；音频区从上到下为 A1、A2、A3。
- 如果当前模型是 `video_tracks` 顺序 V1,V2,V3，需要在 layout 层决定是否反转显示，不要改业务数据顺序。
- linked video/audio 最好默认在中线两侧相邻：V1 在分割线上方，A1 在分割线下方。

---

## 20. Clip 内容和状态细节

| 状态/内容 | 视频 Clip | 音频 Clip | 验收 |
|---|---|---|---|
| 默认 | 蓝色主体 + filmstrip + 文件名 | 绿色主体 + waveform + 文件名 | 不同媒体类型一眼可分 |
| selected | 橙色 2px 描边，z-order 提升 | 同左 | 多选时每个都有清楚边框 |
| hover | 边框微亮，trim edge 可见 | 同左 | hover 不改变布局 |
| linked | 左下角 link icon 或细线标记 | 同左 | linked/unlinked 可区分 |
| disabled | 降低透明度/灰化 | 降低透明度/灰化 | 不与 selected 混淆 |
| locked track | clip 变暗，禁止操作 | clip 变暗 | 鼠标反馈为禁用或无操作 |
| muted audio | waveform/clip 降饱和或显示 M 状态 | 适用 | mute 状态可见 |
| offline media | 红色/斜线/缺失标识 | 同左 | 离线素材不假装正常 |
| proxy media | 小 proxy badge | 小 proxy badge | 使用代理时可提示 |
| speed changed | `1.25x` badge | `1.25x` badge | 非 1.0x 才显示 |
| effects | 小 fx badge | 小 fx badge | 有效果时可识别 |
| transition | clip 边缘叠加 transition block | 音频淡入淡出/交叉淡化 | 不遮住主 label |
| very narrow | 只画色块和选中框 | 只画色块 | 不显示溢出文字 |

绘制层级建议：

1. clip shadow/底色。
2. clip rounded path。
3. thumbnail/waveform。
4. 顶部 label bar。
5. 文件名和状态 badge。
6. link/effect/speed icon。
7. trim/fade handle。
8. selected/hover border。

---

## 21. 时间标尺、播放头和导航细节

| 项目 | 细节 | 验收 |
|---|---|---|
| 起始时间码 | 支持默认 `01:00:00:00` 或项目设置；内部仍可 frame 0 | UI 显示和计算不混乱 |
| 标尺 LOD | 高 zoom 显示帧，中 zoom 显示秒，低 zoom 显示 10s/分钟 | 任意 zoom 不拥挤 |
| 主/副刻度 | 主刻度长、颜色更亮；副刻度短、透明 | 类似专业 NLE 标尺 |
| 当前时间高亮 | 播放头所在标尺位置或标签高亮 | scrub 时反馈清楚 |
| 播放头拖动 | 标尺或头部可拖动；clip 区点击可选是否移动 | 不误触 clip drag |
| playhead follow | 播放时可选跟随、居中、分页 | 播放不突然迷路 |
| frame step | 左右箭头逐帧，Shift 多帧 | 帧进退准确 |
| Home/End | 跳转起止 | 时间码同步 |
| JKL | J/K/L shuttle 基础支持 | 速度显示和播放状态同步 |

---

## 22. 拖拽、Drop 和媒体插入细节

| 场景 | 期望行为 | 关键边界 |
|---|---|---|
| 从媒体池拖视频带音频 | 视频落目标 V 轨，音频落目标 A 轨，二者 linked，同 start | 如果 A 轨不存在，自动创建或提示 |
| 从媒体池拖纯音频 | 只能落音频区/音频目标轨 | 拖到视频区时 preview 自动吸到 A 目标轨或显示 invalid |
| 从媒体池拖图片 | 落视频轨，使用默认 still duration | 不创建音频 |
| 拖动已存在 linked 视频 | 音频同步横移，Y 保持 A 轨 | 不允许音频上漂 |
| 拖动已存在 linked 音频 | 视频同步横移，Y 保持 V 轨 | 不允许视频下漂 |
| 拖动 selected 多 clip | 所有 selected 保持相对时间差 | 任一 clip 碰撞则整体 invalid |
| 跨轨拖动 | 只有媒体类型兼容轨可作为目标 | 视频不能进 A，音频不能进 V |
| 拖到锁定轨 | invalid preview | 不提交 command |
| 拖到已有 clip 重叠 | 按当前 edit mode 决定 overwrite/insert/deny | MVP 可先 deny，并显示红色 preview |
| 拖出轨道区 | 保持最后合法轨或 invalid | 不产生半悬空 clip |
| Esc 取消 | 所有 preview 消失，model 不变 | command stack 不增加 |

---

## 23. 剪辑操作细节

| 操作 | MVP | 进阶 | 必测边界 |
|---|---|---|---|
| Move | 单 clip、linked clip、多选移动 | ripple move、insert move | 碰撞、锁轨、负时间、linked y-lock |
| Trim left | 改 `timeline_start`、`timeline_duration`、`source_start` | ripple trim left | 最小时长、source 起点、linked 行为 |
| Trim right | 改 `timeline_duration`、`source_duration` | ripple trim right | 最小时长、source 终点 |
| Blade | 点击位置 split | linked split、全轨切割 | split 在 clip 外、clip 边缘、locked track |
| Delete | 删除 selected clips | ripple delete | linked delete 策略、undo |
| Ripple delete | 关闭空隙 | 多轨同步 ripple | linked 音画同步 |
| Roll trim | 相邻两个 clip 边界移动，总时长不变 | snap 到 frame/marker | source 边界 |
| Slip | clip 位置不变，source range 变化 | slip preview thumbnails | source 起止 |
| Slide | clip source 不变，左右邻接 clip 调整 | slide preview | 相邻 clip 边界 |
| Fade | 视频/音频 fade handle | 曲线/淡化类型 | fade 长度不能超过 clip |
| Transition | clip 边缘添加 transition | transition duration trim | 两侧媒体余量 |

MVP 先把 Move/Trim/Blade/Delete 做稳；Ripple/Roll/Slip/Slide 可以分阶段，但计划里要预留状态和 command 边界。

---

## 24. 右键菜单和快捷键细节

| 区域 | 右键菜单 | 快捷键 |
|---|---|---|
| Clip | Delete、Split、Link/Unlink、Enable/Disable、Open in Color、Open in Composite、Show in Media Pool、Clip Properties | Delete、Cmd/Ctrl+C/V、B split、D enable |
| Video clip | Add Transition、Add Fade、Use Shared Grade、Make Local Grade Copy | 根据现有快捷系统接入 |
| Audio clip | Mute Clip、Normalize、Show Waveform、Audio Gain | M mute selected 可选 |
| Track header | Add Track、Delete Track、Rename、Lock、Set Target、Track Height、Reset Height | 无或少量 |
| Splitter | Reset 50/50、Video Larger、Audio Larger | 双击重置 50/50 |
| Ruler | Add Marker、Go to Timecode、Set In/Out | M marker，I/O in out |
| Empty track area | Paste、Add Track、Select All on Track | Cmd/Ctrl+A |

快捷键原则：

- 不要一次性抢太多全局快捷键，先接入现有 `shortcut_manager`。
- 编辑页获得焦点时才响应时间线快捷键。
- 文本输入状态下不触发剪辑快捷键。
- 所有快捷键都要有菜单或 tooltip 暗示，避免变成隐藏功能。

---

## 25. 滚动、缩放和视口细节

| 项目 | 行为 | 关键实现 |
|---|---|---|
| 横向滚动 | ruler 和 clip 同步，track header 固定 | QGraphicsView scene x 一致 |
| 垂直滚动 | 视频/音频区域和 header 同步 | header item 与 track body 同一 y |
| Zoom in/out | 以鼠标所在 frame 为中心 | 缩放前后保持 frame under mouse 不变 |
| Fit timeline | 全部内容适配横向宽度 | 计算有效 duration |
| Zoom slider | 和 wheel zoom 同步 | 防递归锁 |
| 最小 zoom | 不挤爆标尺文字 | LOD 隐藏细节 |
| 最大 zoom | 单帧足够宽 | frame tick 清晰 |
| 滚动条风格 | 深色、细、低干扰 | 与面板主题一致 |
| 空时间线 | 仍显示 10s 或项目默认时长标尺 | sceneRect 不为 0 |

---

## 26. 性能预算

| 场景 | 目标 | 说明 |
|---|---|---|
| 10 个 clips | 交互无感卡顿 | 每次 layout/update < 8ms |
| 50 个 clips | 拖动和滚动稳定 | 可见区域重绘优先 |
| 100 个 clips | 可用，不明显冻结 | LOD、缓存、异步请求必须生效 |
| 缩放 | 不同步解码缩略图 | 缩放时先画色块，延迟补图 |
| 波形 | 不在 paint 中提取 | paint 只消费 cache/path |
| 缩略图 | 不在 UI 线程解码视频 | 后台 worker + signal |
| 内存 | 缓存有上限 | LRU 清理 |

明确禁止：

- 在 `paint()` 中读视频文件、抽波形、请求阻塞 IO。
- 在鼠标移动中重建整个 scene。
- 每一帧拖动都提交 command。
- 未调用 `prepareGeometryChange()` 就改 bounding rect 相关数据。

---

## 27. 测试和截图验收矩阵

| 测试类型 | 用例 | 验收点 |
|---|---|---|
| 布局单测 | 1V1A 默认 | 分割线 50/50，V1/A1 在中线两侧 |
| 布局单测 | 3V4A 不同高度 | 每条 track top 不重叠 |
| 布局单测 | 调整分割线 | video/audio area 高度正确 |
| 布局单测 | 调整单轨高度 | clip/header/grid 同步 |
| 交互测试 | linked video drag | audio y 不变，start 同步 |
| 交互测试 | linked audio drag | video y 不变，start 同步 |
| 交互测试 | drag to locked track | invalid，不提交 |
| 交互测试 | drag to wrong media area | invalid 或自动吸到正确目标轨 |
| 交互测试 | Esc cancel | model 不变 |
| 命令测试 | linked move undo/redo | 一次撤销还原音视频 |
| 命令测试 | trim undo/redo | source/timeline 字段正确 |
| 绘制截图 | empty timeline | 标尺、V/A 区、分割线可见 |
| 绘制截图 | one video with audio | V1/A1 linked clip 对齐 |
| 绘制截图 | many clips | 无文字溢出、无重叠 |
| 绘制截图 | narrow zoom | LOD 生效 |
| 绘制截图 | high zoom | 帧刻度/缩略图清晰 |
| 性能测试 | 100 clips scroll | 无明显卡死 |

截图建议固定尺寸：

- `1365x768`：接近用户截图。
- `1440x900`：常见桌面。
- `1920x1080`：标准大屏。
- `2560x1440`：高分辨率。

---

## 28. 失败态和边界态设计

| 场景 | UI 表现 | 行为 |
|---|---|---|
| 媒体离线 | clip 红色斜线/Offline 标签 | 不能预览但可移动/删除 |
| 缩略图未生成 | 显示色块和 loading shimmer/占位纹理 | 后台生成后刷新 |
| 波形未生成 | 显示中心线和淡色占位 | 后台生成后刷新 |
| 缩略图生成失败 | 显示 fallback icon | 不反复无限请求 |
| 波形生成失败 | 显示平直线和 warning badge | 可手动 retry |
| 轨道太窄 | 隐藏文字/图标，只保留色块 | 不挤压布局 |
| clip 太短 | 隐藏 label，仅保留 selected border | 不显示半个字 |
| 素材 duration 缺失 | 使用默认 duration 并标记 | 不创建 0 宽 clip |
| drop 无目标轨 | 自动创建或提示 | 不静默失败 |
| 命令执行失败 | 回滚 preview，显示状态提示 | 不留下半移动 item |

---

## 29. 用户偏好和项目保存

| 设置 | 默认 | 保存位置建议 |
|---|---|---|
| V/A 区比例 | 50/50 | 项目 UI state |
| 单轨高度 | 视频/音频默认高度 | 项目 UI state |
| clip 缩略图模式 | filmstrip | 用户偏好 |
| audio waveform 显示 | 开 | 用户偏好 |
| snap 开关 | 开 | 用户偏好或项目 state |
| linked selection | 开 | 用户偏好 |
| playhead follow | 关或按项目设置 | 用户偏好 |
| 起始时间码 | `01:00:00:00` | 项目设置 |
| timeline zoom | 适配内容或默认 | 项目 UI state |

注意：

- 项目文件语义数据和 UI state 要分开，避免布局偏好影响导出/渲染。
- 用户偏好负责全局习惯，项目 UI state 负责打开同一个项目时恢复现场。

---

## 30. 分阶段交付版本建议

| 版本 | 内容 | 用户感受 |
|---|---|---|
| V0.1 修 bug | P0-1 到 P0-3 | 音频不再上漂，linked 拖拽可信 |
| V0.2 布局 | P0-4 到 P0-9 | 有中线分区，轨道高度可调 |
| V0.3 视觉骨架 | P1 | 第一眼像专业时间线 |
| V0.4 轨道头 | P2 | 左侧 V/A 控制区像 NLE |
| V0.5 Clip 精修 | P3 | 视频缩略图、音频波形、label 质感上来 |
| V0.6 核心剪辑 | P4 MVP | 能稳定移动、修剪、切割、撤销 |
| V0.7 媒体缓存 | P5 | 长时间线仍流畅 |
| V0.8 验收收尾 | P6 + 截图矩阵 | 达到 85%-95% 主观目标 |

每个版本都应该可以单独合并，不要等全部做完才验证。

---

## 31. 实施时的硬性工程约束

- 先写/补布局测试，再大改坐标。
- 所有坐标换算都通过 `TimelineView` 或新布局模块。
- `QGraphicsItem.paint()` 禁止改变 item 的 scene position。
- 拖拽期间只做 preview，不修改 timeline model。
- 鼠标释放时才提交 command。
- linked audio/video 操作必须是一个可撤销的宏命令。
- 任何 track height、area split、zoom 变化后必须统一调用 layout refresh。
- 任何 bounding rect 变化前必须 `prepareGeometryChange()`。
- thumbnail/waveform 只异步生成，paint 只消费缓存。
- 每次完成 P0/P1/P3 任一阶段，都要跑截图工具做视觉确认。

---

## 32. 最终验收清单

完成优化后，用下面清单判断是否真的接近 DaVinci-like：

| 检查项 | 通过标准 |
|---|---|
| 打开空项目 | 时间线不是黑洞，有标尺、V/A 区、中线、轨道头 |
| 拖入一个带音频视频 | V1/A1 两个 clip 起点一致，位于中线两侧 |
| 横向拖动视频 | 音频同步横移，不上漂、不重叠视频 |
| 横向拖动音频 | 视频同步横移，不下漂、不重叠音频 |
| 调整中线 | 视频区/音频区大小改变，clip 不错位 |
| 调整轨道高度 | header、clip、grid、waveform 同步变化 |
| 缩放时间线 | 标尺和 clip 左边缘始终对齐 |
| 多选移动 | 相对位置保持，撤销一次全部恢复 |
| 修剪 clip | source/timeline 映射正确，波形/缩略图更新 |
| 刀片切割 | linked video/audio 可同步切割 |
| 锁定轨道 | 不能误移动/删除/放入素材 |
| 文字显示 | 中文文件名不溢出，不盖住其他 UI |
| 100 clips | 滚动/缩放仍可用 |
| 截图对比 | 和 DaVinci 时间线在布局、密度、层次上明显接近 |

如果以上清单全部通过，就可以认为时间线达到了「85%-95% 视觉接近、核心体验接近 DaVinci」的目标。

---

## 33. 当前代码审计发现的具体问题

本节基于当前代码快速审计，不是泛泛推测。下面这些问题应该全部进入后续实现计划。

| 编号 | 位置 | 当前问题 | 用户可见症状 | 严重级别 | 修复阶段 |
|---|---|---|---|---|---|
| BUG-01 | `timeline_items.py` / `ClipItem.paint()` | `paint()` 内部计算 `expected_x/expected_y` 并调用 `setPos()`，且 Y 使用 `TRACK_AREA_TOP + track_index * (track_height + gap)` | 绘制时反向改位置，混合轨高时音频可能上漂、残影、跳动 | Critical | P0 |
| BUG-02 | `timeline_view.py` / `_layout_track_items()` vs `ClipItem.paint()` | `TimelineView` 已经按累计高度布局，但 `ClipItem.paint()` 又用固定公式覆盖 | 布局系统互相打架，轨道高度变化后 clip 回到错误 Y | Critical | P0 |
| BUG-03 | `timeline_view.py` / `_on_clip_moved()` | live drag 只更新当前拖动 item，不更新 linked audio/video item | 拖动过程中音频不同步，释放后才跳到位置，手感像 bug | Critical | P0 |
| BUG-04 | `timeline_view.py` / `_on_drag_finished()` | linked video/audio 是两个独立 `MoveClipCommand` 顺序执行，不是一个原子 `MacroCommand` | 撤销要分两次；中间状态会 emit 两次 timeline changed；失败时可能半提交 | Critical | P0/P4 |
| BUG-05 | `timeline_view.py` / `_move_selected_clips()` | 多选移动在主 clip 已经提交后才移动其他 selected clip，且 invalid clip 被跳过 | 多选移动可能只移动一部分，违背 NLE 的 group move 预期 | High | P4 |
| BUG-06 | `timeline_view.py` / `_target_track_id` | 只有一个 target track id，不区分 video target 和 audio target | 设置 A1 为目标后，视频插入/高亮可能混乱；无法像 DaVinci 那样 V/A 独立 patch | High | P2 |
| BUG-07 | `timeline_view.py` / `get_target_audio_track_id()` | 音频目标轨总是第一条音频轨，忽略用户在 header 中选中的 audio target | 用户点选 A2 目标轨后，音频仍可能落到 A1 | High | P2 |
| BUG-08 | `timeline_view.py` / `_find_target_track()` | 音频 drop 到视频区时 `adjusted_index < 0` 会被强制改成 0 | 音频拖到错误区域可能静默吸到 A1，而不是给出 invalid/明确反馈 | High | P0/P2 |
| BUG-09 | `timeline_view.py` / `drawBackground()` | 视频/音频分割线只是“最后一条视频轨后面的线”，不是面板中间的可拖动区域分割线 | 无法满足“默认中间分割，上视频下音频，可拖动调比例”的目标 | Critical | P0 |
| BUG-10 | `timeline_view.py` / track height | 轨道高度存在 `TrackHeaderItem` 运行时状态里，`load_timeline()` 重新创建 header 后恢复默认高度 | 用户调好的轨道高度在刷新/重载/命令后容易丢失 | High | P0/P2 |
| BUG-11 | `timeline_view.py` / `_track_index_at_y()` | track 下面空白区返回 `len(tracks)`，调用方可能解释成“新轨道” | 在时间线空白处 drop/drag 行为不明确，可能误创建轨道 | Medium | P0 |
| BUG-12 | `timeline_view.py` / `_validate_drag_position()` | 只按 `track_index` 查 `timeline.all_tracks`，没有同时校验媒体类型、锁定状态、区域状态 | 预览可能显示合法，但 drop 时又换轨；锁轨/wrong media area 防护不足 | High | P0 |
| BUG-13 | `timeline_view.py` / gap indicators | `_update_gap_indicators()` 里局部使用 `TRACK_HEIGHT` / `track_h_audio`，没有统一走 `_track_height_for()` | 自定义轨高后 gap indicator 高度可能不对 | Medium | P2 |
| BUG-14 | `timeline_items.py` / `DragPreviewItem._update_geometry()` | preview 内部仍使用 `TRACK_AREA_TOP + track_index * (TRACK_HEIGHT + gap)` | preview 初次显示/内部 bounding 可能与最终落点不同 | High | P0 |
| BUG-15 | `timeline_items.py` / `RollHandleItem`、`SnapIndicatorItem` 等 | 多个 item 仍有固定 track height / fixed y estimate | 可变轨高、V/A 分区后辅助线和手柄可能错位 | High | P0/P4 |
| BUG-16 | `timeline_view.py` / `FullViewportUpdate` | 用全量刷新缓解残影，但根因仍是绘制时改几何和状态冲突 | 时间线复杂后性能变差，拖拽仍可能闪 | Medium | P5 |
| BUG-17 | `tests/edit/test_timeline_view_layout.py` | 现有测试只覆盖线性 video+audio 累计高度，没有覆盖中线分区、linked drag、可变轨高、target audio | 当前最大 bug 没有足够回归测试 | High | P6 |
| BUG-18 | `timeline_v2.py` / `EditTrack` | 轨道业务模型没有 UI height/area state；这是合理的，但当前也没有单独 UI state 层承接 | UI 状态难持久化，命令刷新后高度/区域比例容易重置 | Medium | P0/P29 |
| BUG-19 | `timeline_view.py` / drop flow | drag preview、drop target、最终 `_add_asset_as_clip()` 分散在多个路径，且 target track 选择逻辑重复 | preview 和 drop 结果可能不一致 | High | P0 |
| BUG-20 | `timeline_view.py` / `load_timeline()` | 每次 load 会清空 item、重建 header，并默认把第一条视频轨设为 target | 用户选择的 target track、高度、hover/selection UI 状态可能被重置 | Medium | P2/P6 |

---

## 34. 代码审计后新增的 P0/P1/P2 修复任务

| 编号 | 新增任务 | 原因 | 具体做法 | 验收 |
|---|---|---|---|---|
| P0-10 | 删除所有 paint 内几何同步 | `ClipItem.paint()` 当前直接导致坐标系统冲突 | 从 `ClipItem.paint()` 移除 `setPos()`；任何 item 的 `paint()` 都只读状态、只绘制 | `rg "def paint" timeline_items.py` 后确认 paint 中没有 `setPos()` |
| P0-11 | 建立 `TimelineLayoutEngine` 纯布局层 | 现在布局散落在 view、item、preview 中 | 抽出 frame/x、track/y、area split、track rect、scene rect 计算 | 纯单测覆盖所有布局函数 |
| P0-12 | 建立 `TimelineAreaLayout` | 当前没有真正的视频/音频区域 | 明确 `video_area_rect`、`audio_area_rect`、`splitter_rect`，默认 50/50 | V1/A1 位于分割线两侧 |
| P0-13 | 建立 `TrackVisualStateStore` | 高度只存在 header，刷新会丢 | 用 `track_id -> height`、`video_area_ratio`、`audio_area_ratio` 管 UI 状态 | load/reload 后高度保持 |
| P0-14 | 统一 drag preview 和 final drop | preview 和 drop target 分散，容易不一致 | 新增 `resolve_drop_target(pos, asset_type)`，preview/drop 都调用 | preview 位置就是最终落点 |
| P0-15 | linked live preview | 拖动时 linked clip 不动，用户感知非常差 | `_on_clip_moved()` 计算 move group，更新主 clip 和 linked clip 的 preview start | 拖动过程中 V/A clip 始终同步横移 |
| P0-16 | linked move 原子提交 | 现在两个 command 分开执行 | 主 clip、linked clip、selected clip 全部收集成一个 `MacroCommand` | 撤销一次还原全部 |
| P0-17 | wrong-area invalid feedback | 音频拖到视频区会静默映射到 A1 | hit test 区分 `video_area/audio_area/empty_area`；不匹配时显示 invalid 或吸附到明确 target | 不再悄悄落错轨 |
| P1-7 | 固定 header 和时间区同步策略 | 专业时间线要求 header 不跟横向滚动跑 | 明确 header x 固定、track body/ruler 水平滚动、垂直位置同步 | 横向滚动时 V1/A1 header 固定 |
| P2-7 | 分离 video/audio target | 单一 target id 会导致 V/A 插入混乱 | 新增 `_target_video_track_id`、`_target_audio_track_id`；header 点击按 track type 更新 | V1/A2 可同时作为目标 |
| P2-8 | target header 状态恢复 | load 后总是第一条 video target | target id 存 UI state；若轨道被删再 fallback | reload 后目标轨不乱跳 |
| P2-9 | 可变轨高影响所有辅助 item | gap/snap/roll/playhead/preview 都要同步 | 所有辅助 item 接收 track rect 或 area height，不再自算固定高度 | 改轨高后所有线和手柄对齐 |

---

## 35. 当前代码中的坐标系统冲突详解

现在最核心的问题不是“某一行算错”，而是存在三个坐标来源：

| 来源 | 当前职责 | 问题 |
|---|---|---|
| `TimelineView._layout_track_items()` | 用累计轨高摆放 header、clip、transition、gap、roll | 方向正确，但不是唯一来源 |
| `ClipItem.paint()` / item `_update_geometry()` | item 自己按 `track_index * height` 改位置 | 会覆盖 view 的累计布局 |
| `DragPreviewItem` / `RollHandleItem` / `SnapIndicatorItem` 等 | 用固定 `TRACK_HEIGHT + TRACK_GAP` 估算 | 可变轨高和 V/A 分区后必然错位 |

必须改成：

```text
TimelineLayoutEngine
  -> 计算 track rect / clip rect / area rect / splitter rect
TimelineView
  -> 调用 layout engine，把 scene position 分发给 item
QGraphicsItem
  -> 只持有 local width/height，只负责 paint local rect
```

禁用模式：

```text
ClipItem.paint() -> setPos()
DragPreviewItem._update_geometry() -> fixed TRACK_HEIGHT formula
RollHandleItem._update_geometry() -> fixed TRACK_HEIGHT formula
任何 item 自己通过 track_index 推导全局 y
```

允许模式：

```text
TimelineView.layout_items()
TimelineView.update_drag_preview()
TimelineView.update_playhead_span()
```

---

## 36. 当前 linked audio/video 逻辑缺口

| 环节 | 当前状态 | 问题 | 计划修复 |
|---|---|---|---|
| 创建 | `_add_asset_as_clip()` 会创建互相 linked 的 video/audio clip | 基础关系存在 | 保留 |
| live drag | `_on_clip_moved()` 只动当前 item | 拖动中音画不同步 | 引入 `MoveGroupPreview` |
| release | `_on_drag_finished()` 先移动主 clip，再移动 linked clip | 不原子，撤销不一致 | 用一个 `MacroCommand` |
| multi-select | `_move_selected_clips()` 在主 clip 后单独处理其他选择 | 部分 selected 可移动，部分不动 | group validate 先于提交 |
| validation | 主 clip 和 linked clip 分别 validate | 没有整体事务概念 | `validate_move_group()` |
| undo | 两个独立 command | 需要两次撤销 | 一个 command |
| target track | linked clip 不应跨类型轨 | 当前主要依赖原 track | 加 `track_type` 和 area lock |

新增数据结构建议：

```python
@dataclass(frozen=True)
class MoveGroupMember:
    clip_id: str
    track_id: str
    old_start: int
    new_start: int
    locked_y: float

@dataclass(frozen=True)
class MoveGroup:
    primary_clip_id: str
    members: tuple[MoveGroupMember, ...]
    delta_frames: int
```

操作流程：

1. mouse press：建立候选 selection group。
2. mouse move：根据 delta_frames 生成 `MoveGroupPreview`。
3. validate：所有 member 都合法才显示 valid。
4. paint preview：每个 member 的 y 来自自己的 track rect。
5. mouse release：生成一个 `MacroCommand`。
6. undo：一次还原全部。

---

## 37. 当前 drop/insert 逻辑需要统一的地方

代码里有多条路径会解析 drop target：

- `dragMoveEvent`
- `dropEvent`
- `_handle_external_file_drop`
- `_update_drag_preview`
- `_find_target_track`
- `_get_track_and_frame_from_pos`

风险是：preview 看到的轨道、最终 drop 的轨道、自动创建的轨道可能不是同一个决策。

建议新增统一结果对象：

```python
@dataclass(frozen=True)
class TimelineDropTarget:
    frame: int
    track_id: str | None
    track_type: str
    area: Literal["video", "audio", "splitter", "empty", "ruler", "invalid"]
    create_track: bool
    is_valid: bool
    reason: str = ""
```

所有路径统一调用：

```python
resolve_drop_target(scene_pos, asset_media_type) -> TimelineDropTarget
```

验收：

- 纯音频拖到视频区：显示 invalid 或明确吸到 audio target，不静默创建错误轨。
- 视频带音频拖到空时间线：创建 V1/A1，且 V/A 在中线两侧。
- 图片拖到音频区：invalid，不进入 A 轨。
- 拖到 ruler：invalid，不创建 clip。
- 拖到 splitter：invalid，不创建 clip。

---

## 38. 当前测试覆盖缺口和新增测试表

现有 `tests/edit/test_timeline_view_layout.py` 已经覆盖了基本累计轨高、ruler hit test、素材 duration/fps，但距离当前目标还缺很多关键测试。

| 新测试 | 覆盖 bug | 要断言什么 |
|---|---|---|
| `test_clip_paint_does_not_change_position` | BUG-01/02 | 手动设置 item pos 后调用 paint，不应触发 scene pos 变化 |
| `test_audio_video_area_default_split_is_centered` | BUG-09 | video/audio area 默认 50/50 |
| `test_v1_a1_adjacent_to_splitter` | 轨道排序 | V1 在 splitter 上方，A1 在 splitter 下方 |
| `test_area_splitter_resize_preserves_clip_track_y` | P0-5 | 拖动 splitter 后 clip 仍在对应 track |
| `test_track_height_persists_after_load_timeline` | BUG-10 | 改高度后 reload/incremental update 不丢 |
| `test_linked_drag_live_preview_updates_audio_y_locked` | BUG-03 | video live drag 时 audio x 变、y 不变 |
| `test_linked_move_uses_single_macro_command` | BUG-04 | command stack 增加一次，undo 一次还原两者 |
| `test_multi_selected_move_is_all_or_nothing` | BUG-05 | 任何 selected invalid 时全组不提交 |
| `test_audio_target_track_is_independent_from_video_target` | BUG-06/07 | V1/A2 可同时作为 target |
| `test_audio_drop_on_video_area_is_invalid` | BUG-08 | 不静默映射到 A1 |
| `test_gap_indicator_uses_custom_track_height` | BUG-13 | 改轨高后 gap item 高度等于 track height |
| `test_drag_preview_uses_layout_engine_y` | BUG-14 | preview y 等于 layout track rect top |
| `test_ruler_splitter_empty_area_hit_test` | BUG-11/12 | ruler/splitter/empty 区域返回明确 area |
| `test_full_reload_preserves_ui_state` | BUG-20 | target track、高度、split ratio 不被重置 |

---

## 39. 更细的实施检查清单

### P0 代码改造检查

- [ ] 建立 `TimelineLayoutEngine` 或等价私有布局层。
- [ ] `TimelineView` 增加真实 V/A area rect 和 splitter rect。
- [ ] 删除 `ClipItem.paint()` 内所有 `setPos()`。
- [ ] 删除/弃用全局 `TimelineCoordinateManager.track_to_y()` 对 track-bound item 的使用。
- [ ] `ClipItem`、`TrackHeaderItem`、`DragPreviewItem` 不再自己按 `track_index` 算全局 y。
- [ ] drag preview 和 final drop 使用同一个 `resolve_drop_target()`。
- [ ] linked live preview 同步更新。
- [ ] linked move release 使用单个 `MacroCommand`。
- [ ] multi-selection move 改为全组 validate 后一次提交。
- [ ] wrong media area、locked track、ruler、splitter 都显示 invalid preview。
- [ ] track height 存入 UI state store。
- [ ] area split ratio 存入 UI state store。

### P1/P2 代码改造检查

- [ ] 分离 `_target_video_track_id` 和 `_target_audio_track_id`。
- [ ] `get_target_track_id()` 改成按 media type 获取。
- [ ] target header 高亮可以同时显示 V target 和 A target。
- [ ] `drawBackground()` 使用 area rect，而不是最后视频轨后画线。
- [ ] track header 高度、clip 高度、grid 高度、gap 高度统一来自 layout engine。
- [ ] 加 track height preset 和 reset。
- [ ] splitter 双击 reset 50/50。
- [ ] reload/incremental update 保留 UI state。

### P3/P4 代码改造检查

- [ ] clip label 不溢出。
- [ ] video thumbnail 和 audio waveform 都有 LOD。
- [ ] trim handle 命中不与 fade handle、clip body 冲突。
- [ ] Esc 可以取消 drag/trim/resize。
- [ ] snap indicator 高度覆盖实际 V/A track area。
- [ ] undo/redo 后所有 item 重新从 model + layout state 得到位置。

---

## 40. 计划表中必须明确的“不做会继续像简陋原型”的细节

| 细节 | 为什么重要 | 不做的后果 |
|---|---|---|
| V1/A1 贴近中线 | 专业 NLE 默认音视频 linked clip 上下相邻，方便同步观察 | 音视频距离太远，视觉上不像剪辑时间线 |
| header 固定宽度和分区 | 用户需要稳定看到 V1/A1、M/S/L、锁定状态 | 左侧像临时按钮，不像轨道控制区 |
| clip label bar | 缩略图复杂时文字需要保护 | 文件名看不清，显得脏乱 |
| 选中橙色描边 | DaVinci-like 的关键视觉反馈 | 用户不知道选中了什么 |
| invalid preview | 专业软件不会静默失败 | 用户以为拖进去了，释放后消失或落错轨 |
| snap line | 剪辑手感的核心 | 拖拽没有“贴边”反馈 |
| all-or-nothing group move | 多选和 linked 操作必须可信 | 素材越多越容易音画错位 |
| UI state persistence | 用户调好的布局必须记住 | 每次操作后高度/分割线重置，体验很差 |
| 截图验收 | UI 优化必须看图说话 | 代码通过但视觉仍然简陋 |

---

## 41. 更新后的最高优先级执行顺序

基于代码审计，建议把实际开工顺序改成下面这样：

| 顺序 | 任务 | 原因 |
|---|---|---|
| 1 | 写布局/paint 不改位置/linked drag 的回归测试 | 先把当前最大 bug 固定下来 |
| 2 | 删除 `ClipItem.paint()` 内 `setPos()`，让 `TimelineView` 成为唯一坐标 owner | 这是音频上漂和残影的根因之一 |
| 3 | 抽 `TimelineLayoutEngine`，保留现有线性布局行为 | 先不改视觉，只统一来源 |
| 4 | 引入 V/A area 和 splitter rect，默认 50/50 | 满足用户要求的视频/音频中线分割 |
| 5 | 改所有 item/preview/helper 使用 layout engine 的 y/height | 清掉固定 `TRACK_HEIGHT` 隐患 |
| 6 | 建立 linked live preview 和 atomic move group | 修复拖动体验和撤销一致性 |
| 7 | 分离 video/audio target track | 防止插入、drop、header 高亮混乱 |
| 8 | 持久化 track height 和 area ratio UI state | 防止刷新后布局丢失 |
| 9 | 做 P1 视觉骨架和 P2 轨道头 | 在稳定布局上做 DaVinci-like 外观 |
| 10 | 做 P3/P4/P5/P6 | 精修 clip、交互、性能和验收 |
