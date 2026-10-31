# Topos Color 时间线显示与交互优化计划

日期：2026-06-05

目标：参考 DaVinci Resolve 时间线的视觉层级与核心剪辑手感，系统优化 Topos Color 当前时间线显示。重点解决轨道头显示弱、片段分割不明显、时间刻度条稀疏、播放头不能吸附到片段边界、缩略图放大后模糊/马赛克、音频波形和片段信息密度不足等问题。

本计划用于交给其它 AI 或开发者分阶段执行。不要一次性重写整个时间线；应优先复用现有结构，把已经存在但未完全接入的能力补齐。

---

## 0. 当前截图诊断

| 区域 | 当前 Topos Color 现象 | DaVinci 参考现象 | 用户感知问题 | 优先级 |
|---|---|---|---|---|
| 轨道头 | 左侧 V1/A1 很窄，只有小胶囊编号和几个小按钮；轨道名称、片段数量、锁定/可见/静音等层级不清 | 左侧轨道头宽、分层清楚，V1/A1 编号、轨道名、锁定/选择/目标/胶片/音频按钮都有固定位置 | 不像专业剪辑软件，轨道状态难识别 | P0 |
| 片段分割 | 相邻片段之间仅靠很细的边界和轻微颜色差；视频缩略图连续铺开后边界弱 | 片段之间有清楚的 1px-2px 暗缝/亮边，clip 名称栏和边框明确 | 很难看出哪里是切点，尤其缩小时 | P0 |
| 时间刻度 | 刻度文字稀疏，主副刻度层级弱；缩放后刻度密度不稳定 | 根据缩放动态显示帧/秒/分钟层级，主刻度、副刻度、网格线对齐 | 定位时间不准，专业感不足 | P0 |
| 播放头 | 红线可见，但点击/拖动播放头无法吸附到片段分割处 | 吸附开启时，播放头、片段、标记、切点互相吸附，有明显吸附线/提示 | 对齐剪辑点困难 | P0 |
| 视频缩略图 | 固定几张图铺在片段内；放大时间线后原图被拉伸，变马赛克 | 根据可见宽度和轨道高度重新采样，filmstrip 密度随缩放变化 | 放大后信息反而更糊 | P0 |
| 音频显示 | 当前 A1 基本是绿色条，波形弱或不可见 | 音频片段内部有清楚的波形，能辅助剪切 | 无法通过音频波形定位对白/节奏 | P1 |
| 时间线背景 | 网格线、轨道区、空白区、分割线层级偏弱 | 轨道区、空白区、标尺区、片段区灰阶分明 | 视觉扁平，剪辑定位费眼 | P1 |
| 缩放/滚动 | 顶部缩放控件有，但与缩略图/刻度 LOD 没有形成完整联动 | 缩放影响刻度、缩略图密度、clip 细节显示 | 放大/缩小不是“信息层级变化” | P1 |

---

## 0.1 给非多模态 AI 的 DaVinci 参考文字规格

下面是对参考图的纯文字拆解。后续如果把任务交给不能看图的 AI，应直接附上本节，避免它只理解成“改深色主题”。

### 0.1.1 整体布局

| 区域 | 参考规格 |
|---|---|
| 顶部工具栏 | 高度约 52px，深黑灰背景；图标按钮横向紧凑排列，按钮之间按功能组用 1px 竖线分隔；激活工具图标为红色或浅色，未激活为灰色 |
| 左上 timecode 面板 | 位于轨道头列顶部、时间刻度条左侧；宽度与轨道头列一致，高度约 40px；显示大号 `HH:MM:SS:FF`，如 `01:00:02:15`；字体为粗体等宽，浅灰白；背景比轨道头略亮 |
| 时间刻度条 | 位于 clip 区顶部，和左上 timecode 面板同一行；背景深灰；主刻度带文字，副刻度只画短线；播放头红色三角压在刻度条下沿 |
| 左侧轨道头列 | 宽度约 260px；横向滚动时保持冻结；每条轨道头与右侧轨道 body 高度完全一致 |
| 轨道内容区 | V 区在上，A 区在下；轨道之间用 1px-2px 深色分割线；空白区域仍显示低对比网格 |
| 时间线内容原点 | frame 0 / timeline start 必须位于轨道头列右边，也就是 clip 内容区的左边界；不能从窗口最左侧或轨道头内部开始 |
| 滚动条 | 底部水平滚动条只覆盖右侧 clip 内容区，左端与轨道头右边界对齐；右侧垂直滚动条覆盖轨道内容高度；滑块偏细，灰色，不抢视觉焦点 |

### 0.1.2 视频 clip 外观

| 组成 | 参考规格 |
|---|---|
| 主体颜色 | 中深蓝，建议 `#2F6F94` 到 `#2B5F83`；不要过亮，不要纯蓝 |
| 外框 | 普通状态 1px 暗蓝/黑边；hover 可变浅蓝灰；selected 使用 1px-2px 高亮蓝或橙色描边 |
| 片段间隔 | 相邻 clip 之间必须有清楚的 1px-2px 深色竖缝；即使两个 clip 同色也能看到切点 |
| 缩略图区 | 缩略图铺在视频 clip 上半部分或主体区域；保持 16:9，不拉伸变形；可裁切居中填充，但不能水平/垂直强行拉伸 |
| 标签栏 | clip 底部或顶部有一条深蓝标签栏，高度约 18px-22px；文件名用白色/浅灰小号等宽或半粗字体；窄片段自动省略 |
| 链接图标 | 文件名前方可有一个小链条图标，表示视频音频 linked；图标为浅青/白色，大小约 10px-12px |
| 速度标识 | 如果速度不是 1.0x，在标签栏右侧显示 `1.0x`/`0.5x` 等；不要和文件名重叠 |
| 圆角 | 轻微圆角 2px-3px；不要大圆角卡片风 |
| 信息 LOD | 宽度小于 40px 只显示色块和边界；40px-120px 显示短标签；大于 120px 显示缩略图、文件名、链接图标 |

### 0.1.3 音频 clip 外观

| 组成 | 参考规格 |
|---|---|
| 主体颜色 | 中深绿，建议 `#2F8A55` 或 `#2F7D4C`；与视频蓝形成明显区分 |
| 波形 | 浅绿/近白绿色波形，建议 `#B6F2C8`；波形应占据音频 clip 主要高度，不能只是一条细线 |
| 波形密度 | 随 zoom 变化重新采样；放大时波形细节增加，缩小时聚合成 peaks |
| 标签栏 | 底部或顶部 16px-18px 深绿标签栏，显示链接图标 + 文件名；文字颜色浅灰白 |
| 片段间隔 | 相邻音频 clip 也必须有深色竖缝，切点和视频切点视觉对齐 |
| M/S 状态影响 | 静音 clip 可降低透明度或加斜纹；solo 不直接改变 clip 主体，主要在轨道头显示 |

### 0.1.4 轨道头外观

| 组成 | 视频轨规格 | 音频轨规格 |
|---|---|---|
| 编号 badge | 红褐/深红小矩形，文字 `V1`/`V2`，大小约 32x20px，圆角 3px | 红褐/深红小矩形，文字 `A1`/`A2`，大小约 32x20px，圆角 3px |
| 轨道名称 | `视频 1` 或 `Video 1`，位于 badge 下方/右侧，浅灰文字 | `音频 1` 或 `Audio 1`，浅灰文字 |
| 片段数量 | 名称下方显示 `3 个片段` / `3 clips`，更小字号，灰色 | 同左 |
| 控制按钮 | lock、target/source patch、filmstrip/visibility 等，排列在 badge 右侧 | lock、target/source patch、M、S、音量值等 |
| 激活状态 | target/selected 轨道左侧或顶部有蓝色/橙色高亮线 | M/S 激活时按钮背景更亮，M 可红灰，S 可绿灰 |
| 背景 | 轨道头背景为 `#17181D` 到 `#1D1E24`，比内容区略亮；每条轨道底部有 1px 分隔线 | 同左 |
| 对齐 | 轨道头高度必须和右侧 clip 轨道高度一致；V1 靠近视频 clip，A1 靠近音频 clip | 同左 |

### 0.1.5 左上 timecode 面板规格

| 项目 | 规格 |
|---|---|
| 位置 | 时间线左上角，位于轨道头列顶部，左边贴窗口边，右边接时间刻度条 |
| 尺寸 | 宽度等于 track header width；高度等于 ruler/header top band，高度建议 40px-52px |
| 内容 | 当前播放头 timecode，格式固定为 `HH:MM:SS:FF`，例如 `01:00:02:15` |
| 字体 | 等宽粗体，字号 20px-24px；颜色 `#D8D9DE`；数字居中或偏左 72px 位置，参考 DaVinci 大号时间码 |
| 背景 | `#17181D` 或 `#15161A`；底部 1px 分隔线 `#292B31` |
| 行为 | 播放头移动、播放、scrub、输入 frame/timecode 时实时更新；点击 timecode 可进入编辑或弹出跳转输入框 |
| 边界 | 不应和工具栏混在一起；它属于 timeline header，不属于顶部全局 toolbar |

### 0.1.6 时间线原点与底部水平滚动条规格

| 项目 | 规格 |
|---|---|
| 内容原点 | 时间线 frame 0 的 x 坐标必须等于 `track_header_width`，也就是轨道头列右侧边界 |
| ruler 起点 | 时间刻度条的第一个时间码刻度从轨道头右侧开始，不覆盖左上 timecode 面板 |
| clip 起点 | 所有 clip、marker、playhead、grid line 的 x 坐标都以右侧内容区为坐标原点；禁止在轨道头区域绘制 clip 内容 |
| 水平滚动条左端 | 底部水平滚动条左端应与轨道头右边界对齐，像 DaVinci 一样只滚动右侧时间线内容 |
| 水平滚动条不覆盖区域 | 轨道头列底部不应出现可拖动的水平滚动条滑块；那里可以保留为空、状态区或固定背景 |
| 垂直滚动条 | 右侧垂直滚动条滚动 V/A 轨道内容，轨道头应随垂直滚动同步，但不随水平滚动移动 |
| 坐标统一 | `frame_to_x(0)` 应返回 `track_header_width`；`x_to_frame(track_header_width)` 应返回 0 |
| 验收 | 横向滚动到最左时，第一个 clip 左边缘、ruler 的 `00:00:00:00`、播放头 frame 0 都贴在轨道头右边界 |

---

## 1. 现有代码基础与建议改动范围

| 模块 | 当前职责 | 本次优化重点 |
|---|---|---|
| `src/features/edit/ui/timeline_view.py` | 时间线主视图、场景管理、播放头、拖放、吸附、布局调度 | 接入统一吸附到播放头拖动；统一刷新 ruler/grid/thumbnail；完善可见区域调度 |
| `src/features/edit/ui/timeline_items.py` | `ClipItem`、`TrackHeaderItem`、`PlayheadItem`、`SnapIndicatorItem` 等绘制 | 重做轨道头、clip 边界、标签栏、缩略图绘制、波形显示、播放头细节 |
| `src/features/edit/ui/timeline_layout.py` | `TimelineLayoutEngine`，统一坐标、track rect、hit/drop target | 继续作为唯一坐标源；不要在 item 或 view 中重新手算 track y |
| `src/features/edit/core/timeline_ops.py` | timeline 操作和 `SnapManager` | 复用 `SnapManager`，补齐播放头/ruler scrubbing 吸附、拖拽边界吸附和显示反馈 |
| `src/features/edit/core/timeline_thumbnail_scheduler.py` | 可见区域缩略图调度 | 将其真正接入 `TimelineView` 和 `ClipItem`，避免 paint 内临时请求导致固定缩略图被放大 |
| `src/shared/media/thumbnail_provider.py` | 视频帧缩略图生成与缓存 | 增加目标尺寸、frame list、LRU、过期请求丢弃策略，确保放大时重新解码清晰帧 |
| `src/features/edit/core/audio_waveform.py` | 音频波形抽取与缓存 | 让音频片段显示 source range 对应波形，并按 zoom bucket 缓存 |
| `tests/edit/`、`tests/media/` | 时间线布局、缩略图、波形、命令测试 | 增加截图/像素/行为回归测试，防止视觉和吸附回退 |

核心原则：

1. `TimelineLayoutEngine` 是坐标唯一来源。任何 clip、header、grid、playhead、drop preview 都不能自己手算轨道 Y。
2. `paint()` 只负责绘制，不能发起大量解码、不能修改 scene position、不能频繁触发请求。
3. 缩略图和波形必须 viewport-aware、zoom-aware、height-aware。
4. 吸附阈值应使用像素阈值，再换算成帧；不要固定“6 帧”，否则缩放后手感会变。
5. 视觉优化必须有截图验收，不只看单元测试。

---

## 2. 总体里程碑

| 阶段 | 名称 | 目标 | 建议耗时 | 完成标志 |
|---|---|---|---|---|
| P0 | 时间线可读性急救 | 轨道头、片段边界、时间刻度、播放头吸附、缩略图清晰度先变可用 | 2-4 天 | 用户能清楚看见轨道/切点/时间/播放头；放大后缩略图不糊 |
| P1 | DaVinci-like 视觉骨架 | 统一深色层级、clip 信息结构、音频波形、背景网格 | 3-5 天 | 时间线第一眼接近专业 NLE |
| P2 | 交互和吸附完整化 | 播放头、clip 拖拽、trim、drop 都使用统一 SnapManager | 3-5 天 | 片段边界、播放头、标记、转场吸附一致 |
| P3 | 缩略图/波形性能 | 后台解码、可见区域调度、LOD、缓存、取消过期请求 | 4-7 天 | 50-100 个 clip 滚动/缩放仍流畅 |
| P4 | 轨道管理增强 | 轨道高度预设、锁定/目标/可见/静音/独奏状态清晰 | 3-5 天 | 左侧轨道头能承担真实剪辑控制 |
| P5 | 验收与防回归 | 自动截图、布局测试、吸附测试、性能基线 | 2-4 天 | 后续改动不容易把时间线显示改坏 |

---

## 3. P0 详细计划：最高优先级问题

### P0-A：重做轨道头显示

| 编号 | 任务 | 当前问题 | 实现建议 | 涉及文件 | 验收标准 |
|---|---|---|---|---|---|
| P0-A1 | 增宽轨道头有效信息区 | 当前轨道头窄，V1/A1 和按钮挤在一起 | 将 track header 视觉宽度固定为 180-260px 可配置；内容区分为编号列、名称/统计列、控制按钮列 | `timeline_items.py`、`timeline_layout.py`、`edit_style.py` | V1/A1、轨道名称、按钮不重叠；窗口缩小时按钮仍不挤出 |
| P0-A2 | 轨道头分层 | 当前只有小编号和几个图标，缺少 DaVinci 的轨道身份 | 视频轨：显示 `V1`、轨道名、片段数；音频轨：显示 `A1`、轨道名、片段数/音量 | `TrackHeaderItem` | 轨道头能一眼看出类型、编号、当前状态 |
| P0-A3 | 控制按钮规范化 | 锁定/可见/静音/独奏图标小且状态弱 | 每个按钮使用固定 18x18 或 20x20 命中区域；hover、active、disabled 三态；音频为 M/S，视频为 lock/target/visibility | `TrackHeaderItem._draw_buttons()` | 鼠标移动到按钮上有 hover；激活态颜色明确 |
| P0-A4 | 目标轨高亮 | 当前 target track 高亮弱 | 左侧 3px 橙色边条 + V1/A1 badge 高亮 + track body 背景微亮 | `TrackHeaderItem`、`TimelineView._layout_track_items()` | V1/A1 是否为目标轨不需要猜 |
| P0-A5 | 轨道高度同步 | header 高度和 body/clip 高度必须严格一致 | header bounding rect 高度从 `TimelineLayoutEngine.track_rect(track_id).height()` 读取 | `timeline_view.py`、`timeline_items.py` | 改变轨道高度后，header、clip、背景线完全对齐 |

建议轨道头布局：

```text
┌──────────────────────────────┐
│ V1 │ Video 1        3 clips  │
│    │ [lock] [target] [film]  │
├──────────────────────────────┤
│ A1 │ Audio 1        3 clips  │
│    │ [lock] [target] [M] [S] │
└──────────────────────────────┘
```

### P0-B：让片段分割明显

| 编号 | 任务 | 当前问题 | 实现建议 | 涉及文件 | 验收标准 |
|---|---|---|---|---|---|
| P0-B1 | 片段之间增加暗缝 | 相邻片段边界不明显 | 每个 clip 内缩 1px 绘制内容，clip 之间保留 scene 背景暗缝；或在 clip 左右边缘画 1px 深色 separator | `ClipItem.paint()` | 三个相邻视频片段的切点在缩放 0.4x 时仍清楚 |
| P0-B2 | 片段顶部标签栏固定 | 文件名贴在缩略图上，边界和信息混在一起 | 视频 clip 顶部固定 18-22px 深蓝标签栏；音频 clip 底部或顶部固定 16-18px 深绿标签栏 | `ClipItem.paint()` | 文件名不会被缩略图干扰，切点处标签栏中断明显 |
| P0-B3 | 选中/hover/普通状态 | 当前选中态和边界层级不足 | 普通：1px 深色外框；hover：1px 浅蓝灰；selected：2px 橙色描边，内部不挤压内容 | `ClipItem.paint()` | 选中的 clip 在复杂缩略图下仍明显 |
| P0-B4 | 切点强调线 | 切点是剪辑核心对象 | 对每个 clip 左边缘画 1px 高亮、右边缘画 1px 阴影；相邻 clip 形成“双边界” | `ClipItem._draw_*` | 播放头拖到切点附近时，切点和吸附线能对齐 |
| P0-B5 | 窄片段 LOD | 窄片段文字/图标会挤爆 | 宽度 < 36px 只画颜色和边界；36-90px 画短名；>90px 画完整标签和图标 | `ClipItem._calculate_thumbnail_mode()`、`paint()` | 缩小后不出现文字溢出或图标堆叠 |

### P0-C：优化时间刻度条

| 编号 | 任务 | 当前问题 | 实现建议 | 涉及文件 | 验收标准 |
|---|---|---|---|---|---|
| P0-C1 | 动态刻度 LOD | 当前刻度稀疏，缩放后不自然 | 以“目标主刻度间距 90-140px、副刻度 10-20px”为原则，根据 fps/ppf 选择刻度单位 | `TimecodeRulerItem`、`timeline_view.py` | 缩放时主刻度不会挤，也不会空 |
| P0-C2 | SMPTE/秒格式切换 | 参考图显示 `01:00:02:00`，当前显示类似 `05:00` | 默认用 SMPTE `HH:MM:SS:FF`；低缩放时可显示 `MM:SS`；项目起始 timecode 可配置 | `TimecodeRulerItem`、`timeline_panel.py` | 与项目 fps 对应，帧号不漂 |
| P0-C3 | 主副刻度视觉层级 | 主副刻度线长度、颜色差异弱 | 主刻度长线 10-12px + label；中刻度 6px；小刻度 3px；主网格线贯穿轨道区但低透明 | `TimecodeRulerItem.paint()`、`TimelineView._draw_vertical_time_markers()` | 用户能通过刻度判断当前大致时间 |
| P0-C4 | 可见区域只画可见刻度 | 长时间线不能全量画 | 根据 viewport visible rect 计算 start/end frame，只绘制可见范围外加一屏 buffer | `TimecodeRulerItem` | 长 timeline 横向滚动不卡 |
| P0-C5 | 刻度与 clip 边界对齐 | ruler、grid、clip x 必须同源 | ruler 使用 `TimelineLayoutEngine.frame_to_x()` 或同等 ppf/header offset，不单独维护偏移 | `timeline_layout.py`、`TimecodeRulerItem` | clip 左边缘、网格线、播放头、刻度 frame 完全重合 |
| P0-C6 | 时间线起点在轨道头右侧 | 如果 frame 0 从窗口最左侧开始，会和轨道头/timecode 面板混在一起 | 明确 `frame_to_x(0) == TRACK_HEADER_WIDTH`；ruler、clip、playhead、marker、grid 全部使用同一内容区原点 | `timeline_layout.py`、`timeline_view.py`、`TimecodeRulerItem` | 横向滚动到最左时，`00:00:00:00` 与 clip 起点都在轨道头右边 |

建议刻度选择算法：

```python
candidate_steps = [
    1, fps // 2, fps, fps * 2, fps * 5, fps * 10,
    fps * 30, fps * 60, fps * 300
]
main_step = first step where step * pixels_per_frame >= 90
minor_step = choose divisor of main_step that gives 10-20 px spacing
```

#### P0-C 技术规格：时间刻度条绘制必须达到的细节

这一节是给实现者的硬规格。时间刻度条不只是显示几个数字，它必须承担“时间定位、切点对齐、播放头拖动、缩放反馈”的核心职责。

##### 1. 几何和对齐

| 项目 | 规格 |
|---|---|
| ruler 高度 | 建议 36px-42px；如果顶部工具栏较高，ruler 仍保持紧凑，不做大块留白 |
| ruler 左边界 | 必须从 `TRACK_HEADER_WIDTH` 开始，即轨道头右边界；左上角区域留给大号 timecode 面板 |
| ruler 右边界 | 覆盖当前可见时间线内容区宽度，可比 viewport 右侧多一屏 buffer，避免滚动时露白 |
| frame 0 x | `frame_to_x(0) == TRACK_HEADER_WIDTH` |
| tick x | 所有 tick 的 x 必须来自统一坐标函数：`x = frame_to_x(frame)` |
| clip/grid/playhead 对齐 | 主刻度线、网格线、clip 左右边界、播放头红线在同一 frame 上必须像素对齐 |
| 横向滚动 | ruler 内容随右侧时间线内容水平滚动；左上 timecode 面板不随水平滚动 |
| 垂直位置 | ruler 位于 clip 轨道上方，底部 1px 分隔线与轨道内容区顶部对齐 |

##### 2. 视觉层级

| 元素 | 建议颜色/尺寸 | 说明 |
|---|---|---|
| ruler 背景 | `#15161A` 或 `#17181D` | 比 clip 内容区略亮，比工具栏略低调 |
| ruler 底部分隔线 | `#292B31` 1px | 与轨道内容区分开 |
| 主刻度线 | `#555A64`，高度 11px-13px | 带 timecode label |
| 中刻度线 | `#3E424B`，高度 7px-8px | 不带文字 |
| 小刻度线 | `#30343B`，高度 3px-4px | 高 zoom 时显示 |
| 主网格线 | `rgba(90, 95, 105, 0.22)` 1px | 从 ruler 底部贯穿轨道区，低对比 |
| 副网格线 | `rgba(70, 74, 82, 0.12)` 1px | 只在中高 zoom 显示；低 zoom 隐藏 |
| label 文字 | `#AEB3BD`，10px-11px，等宽字体 | 放在主刻度右侧 4px 或居中，不能重叠 |
| hover/scrub 区域 | 鼠标 hover 时可轻微亮底 | 不要像按钮一样突兀 |

##### 3. 刻度层级和 LOD

| 缩放状态 | pixels_per_frame 大致范围 | 主刻度 | 中/小刻度 | label 格式 |
|---|---:|---|---|---|
| 极低 zoom | `< 0.08` | 5 分钟或 10 分钟 | 1 分钟 | `HH:MM:SS` 或 `MM:SS` |
| 低 zoom | `0.08 - 0.25` | 1 分钟 | 10 秒 / 30 秒 | `HH:MM:SS` |
| 中低 zoom | `0.25 - 1.0` | 10 秒 | 1 秒 / 5 秒 | `HH:MM:SS` |
| 中 zoom | `1.0 - 4.0` | 1 秒或 2 秒 | 半秒 / 帧组 | `HH:MM:SS:FF` |
| 高 zoom | `4.0 - 12.0` | 10 帧或 1 秒 | 1 帧 / 5 帧 | `SS:FF` 或 `HH:MM:SS:FF` |
| 极高 zoom | `> 12.0` | 1 帧 / 5 帧 | 每帧小刻度 | `FF` 或 `SS:FF` |

实际实现不要硬绑上表的 ppf 边界，应使用“像素间距目标”动态选择：

```python
TARGET_MAJOR_MIN_PX = 90
TARGET_MAJOR_MAX_PX = 150
TARGET_MINOR_MIN_PX = 10
TARGET_MINOR_MAX_PX = 24

candidate_major_steps = [
    1, 2, 5, 10, 12, 15, 24, 25, 30,       # frame-level, depending fps
    fps // 2, fps, fps * 2, fps * 5,
    fps * 10, fps * 30, fps * 60,
    fps * 300, fps * 600,
]
major_step = first step where step * pixels_per_frame >= TARGET_MAJOR_MIN_PX
minor_step = largest divisor/sub-step where minor_step * pixels_per_frame >= TARGET_MINOR_MIN_PX
```

注意：

1. `fps` 可能是 23.976、24、25、29.97、30、50、59.94、60；内部应使用 timeline frame 整数，label 格式化时再按 fps 显示。
2. 对 23.976/29.97 drop-frame timecode 如果项目未来支持，应单独处理；当前可先使用 non-drop SMPTE，但文档和代码要留扩展点。
3. label 最小间距不能低于 72px；如果两个 label 的 bounding rect 重叠，跳过后一个 label，但仍可画 tick。

##### 4. Timecode 格式化

| 场景 | 格式 |
|---|---|
| 左上主 timecode | 永远使用 `HH:MM:SS:FF` |
| ruler 低 zoom | `HH:MM:SS` 或 `MM:SS`，避免文字拥挤 |
| ruler 中高 zoom | `HH:MM:SS:FF` |
| 极高 zoom | 可显示 `SS:FF` 或每秒主 label + 每帧小 tick |
| 负帧 | 默认不显示负帧；如果未来支持 pre-roll，应显示 `-HH:MM:SS:FF` |
| 起始 timecode | 支持 sequence start timecode，例如 DaVinci 常见 `01:00:00:00`；不要硬编码从 `00:00:00:00` 开始 |

建议接口：

```python
def format_ruler_timecode(frame: int, fps: float, start_timecode_frame: int, zoom_level: str) -> str:
    display_frame = start_timecode_frame + frame
    ...
```

##### 5. 可见区域裁剪

绘制时禁止遍历整条时间线。必须从 viewport 反算可见 frame 范围：

```python
visible_left = viewport_scene_rect.left()
visible_right = viewport_scene_rect.right()
start_frame = max(0, x_to_frame(visible_left) - major_step * 2)
end_frame = x_to_frame(visible_right) + major_step * 2
first_major = floor(start_frame / major_step) * major_step
```

验收：

1. 1 小时 timeline、1000 个 clip 时，横向滚动 ruler 不明显卡顿。
2. `paint()` 中不能对全 timeline duration 做逐帧循环。
3. 只画可见范围加 buffer，buffer 约 1-2 个主刻度间距。

##### 6. 播放头和 ruler 交互

| 行为 | 规格 |
|---|---|
| 点击 ruler | 播放头跳到点击 frame；开启吸附时吸附到最近 clip 边界/marker |
| 拖动 ruler | 进入 scrub 状态，播放头实时跟随；可选降低预览刷新频率，但红线必须实时 |
| Shift + 拖动 | 临时禁用吸附 |
| 鼠标样式 | hover ruler 使用水平定位/播放头相关 cursor，拖动时保持一致 |
| 吸附反馈 | 吸附时显示 `SnapIndicatorItem` 垂直线，线与 tick/grid/playhead 使用同一 x |
| 双击 ruler | 可选：添加 marker 或弹出跳转输入，具体按现有交互规范决定 |

##### 7. 与底部水平滚动条的关系

1. ruler 的横向滚动范围必须与底部水平滚动条控制的内容范围一致。
2. 底部水平滚动条左端与轨道头右边界对齐时，ruler 的内容起点也必须同样对齐。
3. 横向滚动到最左时，`start_timecode` label、frame 0 playhead、第一条 grid line 都贴在内容区左边界。
4. 横向滚动时，左上 timecode 和轨道头不动，ruler tick/label 向左或向右移动。

##### 8. 测试清单

| 测试 | 断言 |
|---|---|
| `test_ruler_origin_aligns_with_track_header` | `frame_to_x(0) == TRACK_HEADER_WIDTH`，第一个 label 不进入轨道头列 |
| `test_ruler_major_tick_spacing_lod` | 0.1x、0.5x、1x、4x、12x 下主刻度间距在 90px-150px |
| `test_ruler_labels_do_not_overlap` | 相邻 label bounding rect 不重叠 |
| `test_ruler_visible_range_only` | 1 小时 timeline 绘制 tick 数量与 viewport 宽度相关，不与总时长线性相关 |
| `test_ruler_playhead_snap_to_clip_edge` | 点击/拖动到 clip edge 附近时播放头吸附 |
| `test_ruler_shift_drag_disables_snap` | Shift 拖动时不吸附 |
| `test_ruler_grid_clip_playhead_alignment` | 同一 frame 的 tick、grid、clip edge、playhead x 坐标一致 |

### P0-D：播放头吸附到片段分割处

| 编号 | 任务 | 当前问题 | 实现建议 | 涉及文件 | 验收标准 |
|---|---|---|---|---|---|
| P0-D1 | 播放头拖动接入 SnapManager | 现有 clip 拖拽吸附有雏形，但 ruler/playhead scrubbing 未完整吸附 | `_on_ruler_clicked()`、`_on_ruler_dragged()`、`set_playhead()` 可选择调用 `_snap_frame()` | `timeline_view.py`、`timeline_ops.py` | 开启吸附后，播放头拖到 clip 起止点 10px 内自动贴齐 |
| P0-D2 | 吸附阈值用像素 | 固定帧阈值在不同 zoom 下手感不同 | `SnapConfig.threshold_pixels=8-12`，通过 `pixels_per_frame` 转帧 | `SnapManager.find_nearest()` | 缩放 0.4x、1x、4x 手感一致 |
| P0-D3 | Shift 临时禁用吸附 | 专业剪辑常见需求 | scrubbing/drag 时检测 Shift，临时使用 `with_temporary_disable_all()` | `timeline_view.py` | 按住 Shift 拖播放头不吸附 |
| P0-D4 | 吸附反馈线 | 当前吸附成功反馈弱 | SnapIndicator 显示黄色/橙色垂直线，顶部 label 显示目标类型：Cut/Playhead/Marker | `SnapIndicatorItem` | 用户能看见吸附到了哪个边界 |
| P0-D5 | 上/下一个切点导航 | 已有 jump previous/next boundary 类方法，应暴露快捷键/按钮 | 绑定上/下切点快捷键；按钮 tooltip 本地化 | `timeline_panel.py`、`timeline_view.py` | 可一键跳到前/后 clip 分割处 |

播放头吸附规则：

| 吸附点类型 | 默认启用 | 优先级 |
|---|---:|---:|
| 当前轨道/所有轨道 clip start/end | 是 | 1 |
| 播放头原位置 | 拖 clip 时启用；拖播放头时排除自己 | 2 |
| marker | 是 | 3 |
| transition start/center/end | 是 | 4 |
| sequence start | 是 | 5 |

### P0-E：缩略图不再固定几张、不再放大马赛克

| 编号 | 任务 | 当前问题 | 实现建议 | 涉及文件 | 验收标准 |
|---|---|---|---|---|---|
| P0-E1 | 缩略图请求从 paint 中移出 | paint 内请求容易重复和不可控 | `TimelineView` 根据可见 clip + zoom + track height 调度请求；`ClipItem` 只消费缓存 | `timeline_view.py`、`ClipItem`、`timeline_thumbnail_scheduler.py` | 滚动/缩放不会在 paint 中连续发起大量请求 |
| P0-E2 | target_height 跟随轨道高度 | 当前放大后可能使用低分辨率旧图 | cache key 包含 `target_height`、`source_frame`、`asset_id`、`mtime_ns`；轨道高度变化后重新取更高图 | `thumbnail_provider.py` | 轨道高度从 48 放到 96 后，缩略图重新清晰 |
| P0-E3 | filmstrip 密度跟随 zoom | 固定几张图放大后间距过大且糊 | 每个可见 cell 宽度约 72-120px，根据 visible width 计算需要的 source frames | `timeline_thumbnail_scheduler.py` | 放大 timeline 后缩略图数量增加，而不是拉伸旧图 |
| P0-E4 | 禁止超过 1.25x 放大缓存图 | 缓存图小于目标太多时宁愿显示 loading/占位，也不要强行放大成马赛克 | `ClipItem._draw_single_thumbnail_at()` | 不出现大块马赛克缩略图 |
| P0-E5 | 过期请求丢弃 | 快速缩放/滚动时旧请求回来会覆盖新图 | 使用 generation/token；worker 回来后检查当前 request key | `TimelineThumbnailScheduler`、`ThumbnailProvider` | 快速滚轮缩放后不会闪回旧尺寸缩略图 |
| P0-E6 | 多级缓存 | 同一帧不同高度可复用但不能混淆 | L1：当前 viewport pixmap；L2：FrameThumbnailKey；L3：磁盘可选 | `thumbnail_provider.py` | 反复滚动同一区域命中缓存 |

缩略图 LOD 建议：

| clip 屏幕宽度 | 显示模式 | 行为 |
|---:|---|---|
| < 40px | none | 只画颜色、边界、标签最小信息 |
| 40-120px | single | 显示首帧或代表帧 |
| 120-260px | start_end | 显示首尾帧，中间保留标签 |
| > 260px | filmstrip | 按可见区域采样多帧 |
| 播放中 | cached-only | 不发新解码任务，只显示已有缓存 |

### P0-F：左上角增加 DaVinci-like 大号 timecode 面板

当前 Topos 的 timecode 更像底部状态栏信息，不像专业剪辑软件的主定位信息。参考 DaVinci，应在时间线左上角、轨道头列顶部增加醒目的当前播放头 timecode。

| 编号 | 任务 | 当前问题 | 实现建议 | 涉及文件 | 验收标准 |
|---|---|---|---|---|---|
| P0-F1 | 新增左上 timecode 区 | 当前左上角缺少大号当前时间码，定位信息弱 | 在 timeline header 左侧增加 `TimelineTimecodeDisplay` 或复用 panel label；位置在 track header column 顶部，宽度等于 track header width | `timeline_panel.py`、`timeline_view.py`、`edit_style.py` | 左上显示 `HH:MM:SS:FF`，视觉接近 DaVinci |
| P0-F2 | timecode 与播放头同步 | 播放头移动时只更新红线，timecode 存在感不够 | `set_playhead()`、播放回调、ruler scrub、跳转输入都统一触发 timecode label 更新 | `timeline_panel.py`、`timeline_view.py` | 拖动播放头时 timecode 实时更新，无明显延迟 |
| P0-F3 | 支持点击输入跳转 | 专业软件中 timecode 是定位入口 | 点击 timecode 后进入编辑态，输入 `01:00:02:15` 或 frame number 后跳转播放头 | `timeline_panel.py` | 输入合法 timecode 后播放头跳到对应帧 |
| P0-F4 | 与 ruler 对齐 | 左上 timecode 区应是 ruler 的左侧配套区域 | timecode 面板高度与 ruler band 一致，底部分隔线与 ruler 底线对齐 | `timeline_view.py`、`TimecodeRulerItem` | 左上 timecode 面板和右侧时间刻度条在同一水平带 |
| P0-F5 | 避免底部状态重复抢焦点 | 底部状态栏可保留 zoom/duration，但主 timecode 应在左上 | 底部只保留辅助信息；主 timecode 使用更大字号和更高对比 | `timeline_panel.py` | 用户第一眼看左上即可知道当前时间 |

左上 timecode UI 建议：

```text
┌──────────────────────────────┬───────────────────────────────────────┐
│          01:00:02:15          │ 01:00:00:00   |   01:00:02:00   | ... │
├──────────────────────────────┼───────────────────────────────────────┤
│ V2  [lock] [target] [film]    │                                       │
│ 视频 2 / 0 个片段             │                                       │
├──────────────────────────────┼───────────────────────────────────────┤
│ V1  [lock] [target] [film]    │ video clips                            │
└──────────────────────────────┴───────────────────────────────────────┘
```

### P0-G：底部水平滚动条与轨道头右边界对齐

| 编号 | 任务 | 当前问题 | 实现建议 | 涉及文件 | 验收标准 |
|---|---|---|---|---|---|
| P0-G1 | 水平滚动条只覆盖内容区 | 默认 QGraphicsView 水平滚动条可能从整个 viewport 最左侧开始，视觉上会穿过轨道头区域 | 使用 viewport margin、外层布局拆分，或自定义 overlay scrollbar，使水平滚动条左端从 `TRACK_HEADER_WIDTH` 后开始 | `timeline_view.py`、`timeline_panel.py` | 底部滑块左端和轨道头右边界对齐 |
| P0-G2 | 轨道头水平冻结 | 横向滚动时轨道头不应跟着 clip 内容移动 | header item 的 x 跟随 visible scene left，或拆成独立固定 header view；clip/ruler 继续随 horizontal scroll 移动 | `timeline_view.py`、`timeline_items.py` | 横向滚动后 V1/A1 仍固定在左侧 |
| P0-G3 | timecode 面板固定 | 左上 timecode 面板属于轨道头/header，不应随横向滚动移动 | 将 timecode 面板放在固定 header 区，或每次 horizontal scroll 同步其 x | `timeline_panel.py`、`timeline_view.py` | 横向滚动后左上 timecode 仍固定 |
| P0-G4 | 内容区原点一致 | 滚动条对齐只是视觉，底层坐标也必须一致 | `frame_to_x(0)`、ruler label、clip start、playhead frame 0 全部使用 `TRACK_HEADER_WIDTH` 偏移 | `timeline_layout.py`、`timeline_view.py` | frame 0 不会画进轨道头区域 |
| P0-G5 | 底部左侧固定区 | 轨道头底部滚动条左侧空位要有设计，不要裸露错位 | 左侧固定区可显示小状态背景，或与轨道头背景同色，底部边线与水平滚动条齐平 | `timeline_panel.py`、`edit_style.py` | 视觉上像 DaVinci：左侧固定、右侧滚动 |

实现路线建议：

1. 最稳妥路线：把 timeline 分成固定左列和可滚动右侧内容区，左列包含左上 timecode 和 track headers，右侧包含 ruler、clip scene、水平滚动条。
2. 较小改动路线：保留单个 `QGraphicsView`，但通过 `setViewportMargins(TRACK_HEADER_WIDTH, 0, 0, 0)` 或 overlay item 固定轨道头，同时自定义底部 scrollbar 的 geometry。
3. 不推荐路线：让默认 QGraphicsView scrollbar 继续从窗口最左侧开始，然后只在视觉上遮挡左侧。这样容易造成 hit-test、frame 0、ruler 和 clip 起点错位。

---

## 4. P1：视觉骨架和专业信息层级

| 编号 | 任务 | 实现建议 | 涉及文件 | 验收标准 |
|---|---|---|---|---|
| P1-1 | 统一时间线色板 | 定义 panel/ruler/header/track/empty/grid/clip/border/accent 颜色 token，避免全黑或一片蓝绿 | `edit_style.py` | 截图中至少能分辨 5 个层级：toolbar、ruler、header、track body、clip |
| P1-2 | 背景网格优化 | 主刻度垂直线贯穿轨道区，副刻度只在 ruler 内或低透明 | `TimelineView.drawBackground()`、`_draw_vertical_time_markers()` | 网格辅助定位但不抢 clip |
| P1-3 | 视频/音频区域边界 | V/A 分割线更像 DaVinci 中间边界 | splitter 6px 命中区，视觉线 1-2px，hover 变亮 | `timeline_view.py` | 用户能看出上视频下音频 |
| P1-4 | Clip 内信息分区 | label、缩略图、badge、speed、linked icon 各有固定区域 | `ClipItem.paint()` | 文件名、1.0x、link、badge 不互相覆盖 |
| P1-5 | 音频波形可读 | A1 不能只是绿条 | 使用 source range 映射后的波形，双声道上下或叠加绘制，颜色浅绿 | `audio_waveform.py`、`ClipItem._draw_waveform()` | 参考 DaVinci 第二图，音频片段中能看出波形 |
| P1-6 | 播放头专业化 | 顶部红色倒三角 + 1-2px 线 + 高 z-order | `PlayheadItem` | 播放头贯穿所有可见轨道，不被 clip 遮挡 |
| P1-7 | 底部状态栏降级为辅助信息 | 主 timecode 移到左上后，底部不应重复抢焦点 | 底部保留 zoom、duration、clip count、缓存状态等辅助信息，字号小于左上 timecode | `timeline_panel.py` | 底部状态栏稳定，不抖动，不和主 timecode 竞争 |

---

## 5. P2：吸附和剪辑交互统一化

| 编号 | 任务 | 当前风险 | 实现建议 | 验收标准 |
|---|---|---|---|---|
| P2-1 | 一个 SnapManager 服务 | 旧函数 `find_snap_points()` 和增强 `SnapManager` 并存，容易逻辑分叉 | `TimelineView` 持有一个 `_snap_manager`，timeline 改变、playhead 改变、marker 改变时统一更新 | 所有吸附来源一致 |
| P2-2 | 拖 clip 吸附到切点 | 片段拖动可能已部分实现，但需排除自身和 linked group | 拖动开始时 `exclude_clip_ids` 包含选中组/linked group；对 primary delta 做吸附后同步全组 | 拖片段靠近相邻切点会贴齐 |
| P2-3 | trim 吸附 | 修剪边界也要吸附 | 左右 trim handle 使用边界 frame 参与 snap，注意 source_start/source_end 限制 | trim 到相邻切点不会越界 |
| P2-4 | drop preview 吸附 | 从媒体池拖入时 preview 和最终 drop 必须一致 | `resolve_drop_target()` 后再 snap frame；preview item 显示 snapped frame | 拖入素材靠近切点会预览吸附 |
| P2-5 | 播放头吸附 | 用户本次明确提出 | ruler click/drag、playhead drag 都使用 snap，Shift 临时禁用 | 播放头能吸附到片段分割处 |
| P2-6 | 吸附线 label | 只显示线不够明确 | SnapIndicator 根据 `SnapPointType` 显示 `Cut`、`Marker`、`Playhead`、`Start` | 用户知道吸附对象 |
| P2-7 | Snap toggle 状态 | 工具栏磁铁按钮要控制所有交互 | 一个 `_snap_enabled` 状态，按钮、快捷键、菜单同步 | 关闭吸附后所有交互都不吸 |

---

## 6. P3：缩略图与波形性能架构

### P3-A：缩略图调度链路

```text
TimelineView viewport/zoom/track-height changed
  -> collect visible video ClipItem/ClipInstance
  -> TimelineThumbnailScheduler.update_viewport(...)
  -> batch jobs grouped by asset
  -> ThumbnailProvider worker decodes QImage at target_height
  -> main thread converts QImage to QPixmap
  -> cache by FrameThumbnailKey
  -> ClipItem.update() draws cached pixmaps only
```

| 编号 | 任务 | 实现建议 | 验收标准 |
|---|---|---|---|
| P3-A1 | 收集 visible clips | 根据 `viewport().rect()` 映射 scene rect，只收集可见视频 clip | 视口外 clip 不发缩略图请求 |
| P3-A2 | 计算 visible source frames | 使用 clip 的 `source_start/source_duration/timeline_start/timeline_duration/speed` | 修剪后显示的是修剪范围内的图 |
| P3-A3 | batch decode | 同一 asset 多帧合并，减少反复打开 VideoCapture | 打开同一视频次数显著下降 |
| P3-A4 | LRU 限制 | pixmap 缓存按内存估算淘汰，不只按数量 | 长时间滚动内存不持续涨 |
| P3-A5 | 播放时降级 | 播放时不启动新 decode，只用 cached-only | 播放不被缩略图解码拖慢 |
| P3-A6 | HiDPI 支持 | QPixmap devicePixelRatio 正确处理 | Retina 屏上缩略图不糊 |

### P3-B：波形调度链路

| 编号 | 任务 | 实现建议 | 验收标准 |
|---|---|---|---|
| P3-B1 | source range 映射 | waveform cache key 包含 asset_id、source_start、source_end、speed、channels、zoom bucket | trim 后波形对应片段范围 |
| P3-B2 | zoom bucket | 不为每个 ppf 生成新波形，只按 bucket 缓存 | 缩放不会无限生成缓存 |
| P3-B3 | 双声道绘制 | 轨道高度足够时显示上下声道；高度低时混合显示 | A1 高轨时像 DaVinci 一样清楚 |
| P3-B4 | 占位状态 | 未生成时显示低对比 placeholder，不要空白 | 用户知道波形正在加载 |

---

## 7. P4：轨道管理增强

| 编号 | 任务 | 实现建议 | 涉及文件 | 验收标准 |
|---|---|---|---|---|
| P4-1 | 轨道高度预设 | Small/Medium/Large/Expanded 四档；可右键轨道头设置 | `TrackHeaderItem`、`timeline_view.py` | 切换预设后 header/clip/waveform/thumbnail 同步 |
| P4-2 | 轨道锁定完整化 | 锁定后不可移动、trim、drop、删除 | hit-test、command 执行前都检查 | 锁定轨道上交互显示 invalid |
| P4-3 | 轨道可见/启用状态 | 视频轨眼睛按钮控制可见，音频 M/S 控制播放状态 | 数据模型加字段或接入已有字段 | 状态可保存/撤销 |
| P4-4 | 轨道右键菜单 | Rename、Add Track Above/Below、Delete Empty Track、Track Height | `TrackHeaderItem.contextMenuEvent()` | 轨道管理不依赖隐藏入口 |
| P4-5 | 轨道头冻结 | 横向滚动时左侧轨道头不应随内容一起滚走 | QGraphicsView 可用 overlay header 或将 header x 绑定 visible left | 横向滚动后 V1/A1 仍在左侧 |

---

## 8. P5：测试与验收

| 类别 | 测试项 | 建议文件 | 验收标准 |
|---|---|---|---|
| 布局测试 | V1/A1 header 与 clip body 高度和 y 完全一致 | `tests/edit/test_timeline_view_layout.py` | 多轨、多高度、窗口 resize 均通过 |
| 片段边界测试 | 相邻 clip 的 scene rect 之间有可见 separator | `tests/edit/test_timeline_clip_visual_boundaries.py` | 像素或几何断言通过 |
| ruler 测试 | 不同 ppf 下主刻度间距在 90-140px | `tests/edit/test_timeline_ruler_lod.py` | 0.2x、1x、4x 均合理 |
| 播放头吸附测试 | ruler drag 到 clip edge 附近会 snap | `tests/edit/test_timeline_playhead_snap.py` | Shift 禁用吸附也通过 |
| clip 拖拽吸附测试 | 拖动 clip 到另一 clip 边界附近贴齐 | `tests/edit/test_timeline_command_regressions.py` | undo 一次还原 |
| 缩略图测试 | zoom/track height 改变后 request key 改变 | `tests/edit/test_timeline_thumbnail_scheduler.py`、`tests/media/test_thumbnail_cache_key.py` | 不复用低清旧图 |
| 波形测试 | trim 后 waveform range key 改变 | `tests/edit/test_audio_waveform.py` | 波形范围正确 |
| 截图验收 | 生成 Topos 当前/优化后同尺寸截图 | `tools/capture_edit_page.py` | 人眼对比清楚：轨道头、切点、刻度、缩略图改善 |
| 性能基线 | 10/50/100 clip 缩放滚动耗时 | `tools/timeline_preview.py` 或新增 benchmark | UI 不明显卡顿 |

---

## 9. 推荐执行顺序

| 顺序 | 任务组 | 为什么先做 |
|---:|---|---|
| 1 | P0-A 轨道头 | 用户明确说最大问题是轨道头；且它影响整体专业感 |
| 2 | P0-B 片段边界 | 片段分割是剪辑时间线最核心可读性 |
| 3 | P0-C 时间刻度 | ruler/grid/playhead 是所有对齐行为的视觉基础 |
| 4 | P0-D 播放头吸附 | 用户明确提出，且现有 SnapManager 可复用，收益高 |
| 5 | P0-E 缩略图清晰度 | 放大后马赛克是明显体验问题，需要架构性修 |
| 6 | P1 音频波形和背景层级 | 让时间线更接近 DaVinci 的完整观感 |
| 7 | P2/P3 完整交互和性能 | 在视觉可读后再做更深交互，避免返工 |
| 8 | P5 测试截图闭环 | 每阶段都应补一点测试，最后统一验收 |

---

## 10. 给其它 AI 的执行 Prompt 模板

### 10.1 轨道头优化 Prompt

```text
请在 Topos Color 项目中优化 Edit 时间线轨道头显示。

项目技术栈：PyQt6 + QGraphicsView 时间线，核心文件：
- /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/edit/ui/timeline_items.py
- /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/edit/ui/timeline_view.py
- /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/edit/ui/timeline_layout.py

必须遵守：
1. TimelineLayoutEngine 是唯一坐标来源，不要在 item 里重新手算 track y。
2. paint() 只绘制，不修改 scene position，不发起重型任务。
3. 轨道头 header 高度必须与 track_rect(track_id).height() 完全一致。
4. 视频轨显示 V1/V2，音频轨显示 A1/A2，按钮状态有 hover/active/disabled。

任务：
重做 TrackHeaderItem，使其接近 DaVinci 的轨道头信息层级：编号、轨道名、片段数、锁定、目标轨、视频可见/音频 M/S。实现后添加或更新布局测试。
```

### 10.2 片段边界与 clip 外观 Prompt

```text
请优化 Topos Color 时间线 ClipItem 绘制，让相邻片段切点更明显。

重点文件：
- /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/edit/ui/timeline_items.py

任务：
1. 视频 clip 顶部增加固定标签栏，音频 clip 增加清楚标签/波形区域。
2. 相邻片段之间必须有清楚的 1px-2px 暗缝或边界线。
3. hover、selected、普通状态边框要不同。
4. 窄片段使用 LOD，避免文字/图标溢出。
5. 不要在 paint() 里 setPos，不要发起缩略图解码。

DaVinci-like 文字规格：
- 视频 clip 主体为中深蓝，轻微 2px-3px 圆角，普通状态 1px 暗边，selected 1px-2px 高亮边。
- 视频 clip 内部有缩略图区域和固定标签栏；标签栏高度约 18px-22px，深蓝底，浅灰/白色文件名。
- 相邻 clip 之间必须有 1px-2px 深色竖缝，切点在 0.4x zoom 下也能看清。
- 音频 clip 主体为中深绿，内部显示浅绿色波形，底部或顶部有深绿标签栏。
- 窄片段使用 LOD：小于 40px 只显示色块和边界；40px-120px 显示短标签；大于 120px 显示缩略图、文件名、链接图标。

验收：
在 0.4x zoom 下，三个相邻片段的分割处仍清晰可见；选中 clip 橙色边框明显；文件名不覆盖缩略图。
```

### 10.3 播放头吸附 Prompt

```text
请为 Topos Color 时间线播放头增加吸附到片段分割处的能力。

重点文件：
- /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/edit/ui/timeline_view.py
- /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/edit/core/timeline_ops.py
- /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/edit/ui/timeline_items.py

现有基础：
timeline_ops.py 中已有 SnapManager/SnapConfig/SnapPointType。
timeline_view.py 中已有 _snap_frame、_get_all_snap_points、_on_ruler_clicked、_on_ruler_dragged、set_playhead。

任务：
1. ruler click/drag 和 playhead drag 都接入 SnapManager。
2. 吸附阈值使用像素阈值并根据 pixels_per_frame 转换。
3. Shift 拖动临时禁用吸附。
4. 吸附成功时显示 SnapIndicator，并标明 Cut/Marker/Playhead 等类型。
5. 添加测试：播放头拖到 clip start/end 附近会吸附，Shift 不吸附。
```

### 10.4 缩略图清晰度 Prompt

```text
请修复 Topos Color 时间线缩略图放大后马赛克的问题。

重点文件：
- /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/edit/ui/timeline_view.py
- /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/edit/ui/timeline_items.py
- /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/edit/core/timeline_thumbnail_scheduler.py
- /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/shared/media/thumbnail_provider.py

任务：
1. 缩略图请求由 TimelineView 根据可见区域和 zoom 调度，ClipItem 只画缓存。
2. cache key 必须包含 asset_id/path mtime/source_frame/target_height。
3. 放大 timeline 或增加轨道高度时，应请求更高 target_height 的缩略图，而不是拉伸旧图。
4. filmstrip 缩略图数量跟随可见宽度变化。
5. 快速滚动/缩放时旧 generation 的结果必须丢弃。
6. 播放中只显示缓存，不启动新 decode。

验收：
同一片段从 0.4x 放大到 2x 后，缩略图数量增加且清晰，不出现旧低清图被拉伸成马赛克。
```

### 10.5 轨道头与左上 timecode 外观 Prompt

```text
请把 Topos Color 时间线轨道头和左上 timecode 区改得更接近 DaVinci Resolve。注意：你不能看参考图，所以请严格按下面文字规格实现。

重点文件：
- /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/edit/ui/timeline_panel.py
- /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/edit/ui/timeline_view.py
- /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/edit/ui/timeline_items.py
- /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/edit/ui/timeline_layout.py
- /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/edit/ui/edit_style.py

轨道头规格：
1. 左侧轨道头列宽度约 220px-260px，横向滚动时保持在左侧。
2. 每条轨道头高度必须与右侧 track body 完全一致，使用 TimelineLayoutEngine.track_rect(track_id)。
3. 视频轨显示红褐色小 badge：V1/V2；音频轨显示 A1/A2。badge 约 32x20px，圆角 3px。
4. badge 附近显示轨道名：视频 1/音频 1 或 Video 1/Audio 1；下方小字显示 `3 个片段` 或 `3 clips`。
5. 视频轨按钮包含 lock、target/source patch、visibility/filmstrip；音频轨按钮包含 lock、target/source patch、M、S、音量值。
6. 激活 target 轨左侧显示 3px 高亮线，badge 或按钮也有更亮状态。
7. 背景使用深灰 `#17181D` 到 `#1D1E24`，每条轨道底部 1px 分隔线。

左上 timecode 规格：
1. 在时间线左上角新增大号 timecode 面板，位置在轨道头列顶部、时间刻度条左侧。
2. 面板宽度等于轨道头列宽度，高度等于 ruler/header band，高度约 40px-52px。
3. 显示当前播放头时间码，格式固定 `HH:MM:SS:FF`，例如 `01:00:02:15`。
4. 字体为等宽粗体，字号 20px-24px，颜色 `#D8D9DE`，背景 `#15161A` 或 `#17181D`。
5. 播放、拖动播放头、ruler scrub、跳转后必须实时更新。
6. 点击 timecode 可以进入编辑或弹出跳转输入框，输入合法 timecode 后设置播放头。

时间线原点和滚动条规格：
1. 时间线 frame 0 必须从轨道头右边界开始，不能从窗口最左侧开始。
2. `frame_to_x(0)` 应等于 `track_header_width`，`x_to_frame(track_header_width)` 应等于 0。
3. 时间刻度条、clip、marker、playhead、grid line 都必须使用同一个内容区 x 原点。
4. 底部水平滚动条左端必须与轨道头右边界对齐，只滚动右侧时间线内容区。
5. 轨道头列和左上 timecode 不随水平滚动移动；垂直滚动时轨道头与右侧轨道同步。

验收：
左上 timecode 和右侧时间刻度条在同一水平带；V1/A1 轨道头和右侧 clip 轨道高度完全对齐；轨道状态不用看图标 tooltip 也能基本识别。
```

### 10.6 时间刻度条绘制 Prompt

```text
请详细优化 Topos Color 时间线的 TimecodeRulerItem / 时间刻度条绘制，使其接近 DaVinci Resolve 的专业时间线 ruler。

重点文件：
- /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/edit/ui/timeline_view.py
- /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/edit/ui/timeline_items.py
- /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/edit/ui/timeline_layout.py
- /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/edit/ui/edit_style.py

硬性要求：
1. ruler 左边界必须从轨道头右边界开始，`frame_to_x(0) == TRACK_HEADER_WIDTH`。
2. ruler、clip、grid、marker、playhead 必须使用同一个 frame/x 坐标换算。
3. 主刻度目标间距 90px-150px，副刻度目标间距 10px-24px，根据 `pixels_per_frame` 和 fps 动态选择。
4. 低 zoom 显示分钟/秒级 label，高 zoom 显示 `HH:MM:SS:FF` 或帧级 tick。
5. label 不能重叠；如果会重叠，跳过部分 label，但仍可画 tick。
6. 只绘制 viewport 可见范围加 1-2 个主刻度 buffer，禁止遍历整条 timeline。
7. 主刻度线、中刻度线、小刻度线颜色和高度不同；主网格线低透明贯穿轨道区。
8. 点击/拖动 ruler 移动播放头，开启吸附时吸附 clip 边界/marker；Shift 临时禁用吸附。
9. 横向滚动时 ruler 内容随右侧时间线移动，左上 timecode 和轨道头固定。

建议测试：
- `test_ruler_origin_aligns_with_track_header`
- `test_ruler_major_tick_spacing_lod`
- `test_ruler_labels_do_not_overlap`
- `test_ruler_visible_range_only`
- `test_ruler_grid_clip_playhead_alignment`
- `test_ruler_playhead_snap_to_clip_edge`
```

---

## 11. 最终视觉验收清单

| 检查项 | 通过标准 |
|---|---|
| 轨道头 | V1/A1、名称、按钮、状态清晰；轨道高度变化后仍对齐 |
| 左上 timecode | 轨道头列顶部显示大号 `HH:MM:SS:FF`，播放头移动时实时更新，和右侧 ruler 对齐 |
| 时间线原点 | frame 0、ruler 第一个刻度、playhead frame 0、clip 左边缘都从轨道头右边界开始 |
| 底部水平滚动条 | 滚动条左端与轨道头右边界对齐，只滚动右侧时间线内容，轨道头/timecode 不随水平滚动移动 |
| 片段分割 | 相邻 clip 起止边界在缩小和放大下都清楚 |
| 时间刻度 | 缩放时主副刻度密度合理，文字不重叠 |
| 播放头 | 红色三角和竖线清楚，贯穿轨道区，可吸附到切点 |
| 吸附反馈 | 吸附时有线和类型提示，Shift 可禁用 |
| 缩略图 | 放大后重新采样，不拉伸旧低清图 |
| 音频波形 | A1/A2 内波形可读，trim 后对应正确源范围 |
| 滚动缩放 | 50 个 clip 下滚动和缩放不明显卡顿 |
| 回归测试 | 布局、吸附、缩略图 key、波形 range 测试通过 |

---

## 12. 不建议做的事

1. 不要像素级复制 DaVinci 的商标、图标或专有 UI；只参考专业剪辑体验。
2. 不要推倒重写整个 `TimelineView`，当前已有 `TimelineLayoutEngine` 和测试基础。
3. 不要在 `ClipItem.paint()` 中启动视频解码或大量请求。
4. 不要使用固定帧数作为吸附阈值。
5. 不要只改颜色而不改信息层级；这会让 UI 仍然像原型。
6. 不要只做单张截图验收；必须覆盖不同 zoom、不同轨道高度、多个相邻 clip。
