# Topos Color 视频批量导出面板实施计划

> 文档类型：UI / 交互 / 架构实施计划  
> 目标模块：Deliver / Video Export  
> 当前状态：设计阶段  
> 适用技术栈：PyQt6 + 现有 Timeline Exporter + 现有 Deliver ExportQueue

## 1. 目标与边界

本计划设计的是一个完整的视频 Deliver 批量导出面板，不是图片批量导出面板的改版。

视频批量导出的最小任务单位是：

```text
一个项目 / 一条时间线 / 一个时间范围 / 一组视频和音频参数 / 一个输出文件
```

例如同一条时间线可以生成多个视频任务：

```text
Timeline 1 + Web H.264 1080p       -> Timeline1_Web_1080p.mp4
Timeline 1 + Mobile H.264 1080x1920 -> Timeline1_Mobile.mp4
Timeline 1 + Master ProRes 422      -> Timeline1_Master.mov
```

### 1.1 必须满足的目标

| 目标 | 要求 |
|---|---|
| 视频导出独立性 | 不直接复用图片批量导出页面、图片任务模型或图片输出选项 |
| Deliver 工作流 | 以时间线、范围、视频编码、音频和交付预设为核心 |
| 页面结构 | 批量导出面板默认显示在 Deliver 页面内容的下方 |
| Dock 行为 | 面板可以从 Deliver 页面下方拆出，浮动或停靠到主窗口任意区域 |
| 启动方式 | 支持“导出当前视频”和“加入视频队列”两条路径 |
| 队列状态 | 支持等待、渲染、暂停、取消、完成、失败、重试 |
| 参数快照 | 任务加入队列后，不能因用户修改当前 UI 控件而被隐式改变 |
| 线程安全 | 视频渲染不能阻塞 UI；UI 不得直接调用 OpenGL/ModernGL |
| 可恢复性 | 应用重启后恢复等待中、失败和未完成的视频任务 |

### 1.2 明确不做的事情

| 不属于本功能 | 处理方式 |
|---|---|
| 当前图片逐张输出 | 保留现有 Image Export / 图片批量导出流程 |
| 图片质量、JPEG、PNG 单张参数 | 不放入 Video Deliver 面板 |
| 把每一帧当成一个队列任务 | 不允许；PNG/TIFF Sequence 是一个视频任务 |
| 直接复制图片批量队列表格 | 只能借鉴通用的状态显示和删除操作 |
| 首期并行渲染多个视频 | 默认串行；GPU 和解码资源必须可控 |

## 2. 现有代码基础与边界

### 2.1 可以复用的部分

| 现有位置 | 用途 | 复用方式 |
|---|---|---|
| `src/features/deliver/ui/deliver_page.py` | 当前 Deliver 页面 | 重构为视频 Deliver 工作区容器，不继续扩展单一长表单 |
| `src/features/deliver/core/export_queue.py` | 视频导出队列 | 作为状态机、持久化、重试和队列信号基础 |
| `src/features/edit/core/timeline_export.py` | Timeline 导出配置和导出器 | 作为视频任务的真实配置和渲染入口 |
| `src/features/deliver/module.py` | Deliver 页面和面板注册 | 增加视频批量导出面板 descriptor / action |
| `src/app/controllers/dock_layout_controller.py` | Dock 创建、停靠和布局恢复 | 接入底部默认嵌入与可拆卸 Dock |
| `src/shared/panels/registry.py` | 面板注册和菜单过滤 | 注册稳定的 `deliver.batch_export` 面板 ID |
| `src/features/deliver/locales/*.json` | 国际化 | 所有新 UI 文案进入 i18n |

### 2.2 不应直接复用的部分

| 现有位置 | 原因 |
|---|---|
| `src/shared/export/batch_queue_store.py` | 这是图片批量导出队列存储，不包含时间线、帧范围、音频和视频编码语义 |
| `src/shared/export/export_panel.py` | 面向图片/静态图像输出，不能作为视频 Deliver UI |
| `src/shared/export/quick_export_dialog.py` | 可以借鉴视频字段，但它是单任务快速导出对话框，不是批量视频面板 |
| 图片批量导出文件名逻辑 | 视频文件需要时间线、预设、分辨率、编码器和冲突处理语义 |

### 2.3 当前实现需要先处理的问题

1. 当前 `DeliverPage` 是一个纵向单任务表单，导出按钮直接创建一个 `ExportWorker`，还没有把单任务执行过程接入 `ExportQueue`。
2. `ExportQueue` 的 runner 接口是同步回调形式。长时间视频导出必须放入专用 Worker/QThread，不能在 UI 线程直接调用 runner。
3. 当前 Deliver Dock 位于主窗口右侧。批量面板不能通过把 `QDockWidget` 嵌套进另一个 `QDockWidget` 的方式实现。需要使用“页面内嵌入 + 外部 QDockWidget 包装器”的可拆卸面板机制。
4. `deliver_page.py` 的 `_on_resolution_changed()` 使用了 Python 字符串不支持的 `.contains()`，在重构页面时应改为安全的索引判断或 `"Custom" in text`。

## 3. 总体 UI 信息架构

### 3.1 页面层级

```text
MainWindow
└── Deliver Dock（当前已有的 Deliver 页面容器）
    └── DeliverWorkspace
        ├── VideoDeliverSettingsPanel       # 上方：创建一个视频任务
        ├── VerticalSplitter                 # 可拖动分隔线
        └── VideoBatchExportPanel            # 下方：视频队列和任务状态
```

默认状态下，批量面板必须位于 Deliver 页面内容的下方，而不是默认出现在右侧，也不能默认显示为图片导出 Dock。

### 3.2 页面内嵌和浮动机制

批量面板需要同时满足两个互相冲突的要求：

1. 默认在 Deliver 页面下方。
2. 可以浮动或停靠到主窗口任意区域。

采用以下机制解决：

```text
嵌入状态：
DeliverWorkspace
└── QSplitter(Qt.Vertical)
    ├── VideoDeliverSettingsPanel
    └── VideoBatchExportPanel

浮动/外部停靠状态：
MainWindow
└── QDockWidget(objectName="dockVideoBatchExport")
    └── VideoBatchExportPanel
```

实现要求：

| 行为 | 设计 |
|---|---|
| 默认 | `VideoBatchExportPanel` 放在 Deliver 页面下方，初始高度约为 300–380 px |
| 拆出 | 从面板标题栏拖出，或菜单选择“浮动批量导出面板” |
| 浮动 | 用 `QDockWidget` 包装面板，允许独立窗口移动和调整大小 |
| 停靠 | `allowedAreas=AllDockWidgetAreas`，允许停靠左、右、上、下 |
| 回到 Deliver 下方 | 提供“停靠到 Deliver 页面下方”命令，将面板从 QDockWidget 取回 splitter |
| 关闭 | 关闭后只隐藏面板，不销毁队列和任务状态 |
| 恢复 | 布局恢复时优先恢复用户上次的浮动/停靠状态；无旧状态时回退到 Deliver 下方 |
| 禁止 | 不创建嵌套的 QDockWidget，不把 QDockWidget 直接放进 `dock_deliver` 的 widget 树 |

建议新增一个轻量的 `DetachablePanelHost` 或 `VideoBatchExportDockController`，负责：

- 保存当前嵌入/浮动状态
- 从 `QSplitter` 移出和放回面板
- 创建和销毁 QDockWidget 包装器
- 保存 dock geometry、dock area、tabified 状态
- 处理 Deliver 页面切换时的显示策略

## 4. 详细 UI 布局规格

### 4.1 Deliver 页面上方：视频任务设置区

上方区域用于“创建任务”，不是用于展示整个队列。推荐使用横向三列布局，在窄宽度时切换为滚动纵向布局。

```text
┌─────────────────────────────────────────────────────────────┐
│ 视频 Deliver 设置                                            │
├──────────────┬──────────────────────────────┬───────────────┤
│ 来源与范围    │ 视频设置                      │ 音频与交付     │
│              │                              │               │
│ 当前项目      │ 容器：MP4 / MOV / MKV / MXF  │ 输出目录       │
│ 时间线        │ 编码：H.264 / HEVC / ProRes  │ 文件名模板     │
│ 导出范围      │ Profile                      │ 覆盖策略       │
│ In / Out      │ 分辨率                       │ [预览文件名]   │
│              │ 帧率                         │               │
│              │ 质量 / 码率                   │ [添加到队列]   │
│              │ 色彩 / Alpha                  │ [添加并开始]   │
└──────────────┴──────────────────────────────┴───────────────┘
```

初始宽度和约束：

| 区域 | 初始比例 | 最小宽度 | 说明 |
|---|---:|---:|---|
| 来源与范围 | 24% | 250 px | 只放时间线和范围，不放编码参数 |
| 视频设置 | 46% | 390 px | 主要参数区，宽度不足时允许滚动 |
| 音频与交付 | 30% | 300 px | 输出、音频、字幕和动作 |
| 上方设置区 | 55–65% 可用高度 | 360 px | 由上下 splitter 控制 |
| 下方队列区 | 35–45% 可用高度 | 260 px | 至少能显示 3 个任务行 |

### 4.2 来源与范围卡片

控件顺序固定如下，方便非多模态模型按结构实现：

```text
QGroupBox("来源与范围")
├── QLabel("项目") + QLabel(project_name)
├── QLabel("时间线") + QComboBox(timeline_selector)
├── QLabel("范围") + QComboBox(range_selector)
│   ├── Entire Timeline
│   ├── In / Out
│   ├── Selected Clips
│   └── Custom Frames
├── custom_range_row（默认隐藏）
│   ├── QLineEdit / QSpinBox(start_frame)
│   └── QLineEdit / QSpinBox(end_frame)
├── QLabel("时长") + QLabel(duration_summary)
└── QLabel("帧数") + QLabel(frame_count_summary)
```

绘制规则：

- GroupBox 标题使用 12 px 半粗体，颜色 `#E8E8E8`。
- 标签宽度统一为 82–96 px，右对齐，避免表单控件左边缘不齐。
- 时间线名称过长时使用 `QFontMetrics.elidedText()`，tooltip 显示完整名称。
- 范围错误使用黄色提示条，不只依赖红色边框。
- 没有时间线时，所有输出按钮 disabled，并显示明确的空状态。

### 4.3 视频设置卡片

```text
QGroupBox("视频")
├── 容器                 QComboBox
├── 编码器               QComboBox
├── Profile              QComboBox / QLineEdit
├── 分辨率               QComboBox
│   ├── Timeline Resolution
│   ├── 1920 x 1080
│   ├── 3840 x 2160
│   └── Custom
├── 帧率                 QComboBox
├── 扫描方式             QComboBox（Progressive / Interlaced，如支持）
├── 质量模式             QComboBox（Auto / CRF / Bitrate）
├── 质量值               QSlider + QSpinBox 或 QComboBox
├── 关键帧间隔           QSpinBox / Auto
├── 硬件加速             QCheckBox + capability hint
├── 色彩空间             QComboBox
├── 位深                 QComboBox
└── Alpha               QCheckBox（仅支持的编码器显示）
```

动态显示规则：

| 条件 | UI 行为 |
|---|---|
| 选择 H.264 / HEVC | 显示 CRF、Profile、Level、硬件加速 |
| 选择 ProRes | 显示 ProRes Profile，隐藏 CRF，显示 Alpha 能力 |
| 选择 DNxHR | 显示 DNxHR Profile，切换 MXF/MOV 兼容提示 |
| 选择图像序列 | 显示序列前缀、起始编号、位深、透明度；仍然是一个视频任务 |
| 输出分辨率高于当前缓存 | 显示“将执行完整离线渲染”提示 |
| HDR / 10-bit | 显示输入、处理和输出色彩空间摘要，不能静默降为 8-bit |

### 4.4 音频、字幕和交付卡片

```text
QGroupBox("音频与交付")
├── 导出音频             QCheckBox
├── 音频编码             QComboBox
├── 音频采样率           QComboBox
├── 音频码率             QComboBox
├── 字幕                 QComboBox（关闭 / 烧录 / 独立文件）
├── 字幕轨道             QComboBox（字幕打开时显示）
├── 输出目录             QLineEdit + QPushButton("浏览")
├── 文件名模板           QLineEdit
├── 冲突策略             QComboBox
│   ├── 询问
│   ├── 覆盖
│   ├── 自动编号
│   └── 跳过
├── 文件名预览           QLabel / QPlainTextEdit(read-only)
├── 可用空间状态         QLabel
└── [添加到视频队列]     Primary QPushButton
```

文件名模板默认值：

```text
{project}_{timeline}_{preset}_{resolution}
```

支持变量：

```text
{project} {timeline} {preset} {codec} {resolution} {fps} {date} {index}
```

文件名预览至少显示两行完整示例，并在输出路径不可写、文件已存在或文件名为空时立即提示。

### 4.5 预设栏

页面最上方增加横向预设栏，使用可点击的 preset card，而不是单个复杂下拉框：

```text
┌──────────────┬──────────────┬──────────────┬──────────────┐
│ Web H.264    │ Mobile       │ Master       │ 自定义        │
│ MP4 1080p    │ H.264 1080x1920│ ProRes 422  │ 当前设置      │
│ [选择]       │ [选择]        │ [选择]       │ [编辑]        │
└──────────────┴──────────────┴──────────────┴──────────────┘
```

卡片绘制要求：

- 未选中：背景 `#20232A`，边框 `#3A3F48`。
- hover：边框变为 `#D47A2C`，不要改变整体布局尺寸。
- 选中：左上角绘制 16×16 勾选标记，背景使用低透明度橙色。
- 卡片最小宽度 150 px，高度 64–72 px。
- 窄窗口时改为 `QScrollArea` 横向滚动，不强行压缩文字。

## 5. 下方视频批量队列面板

### 5.1 面板标题栏

下方区域必须明显标注它是“视频渲染队列”，避免用户误认为图片批量导出。

```text
┌─────────────────────────────────────────────────────────────┐
│ 视频渲染队列   3 个任务 · 1 个渲染中 · 预计剩余 08:42          │
│ [开始队列] [暂停] [取消当前] [清除已完成] [更多 ▾]              │
└─────────────────────────────────────────────────────────────┘
```

标题栏控件：

| 控件 | 行为 |
|---|---|
| 开始队列 | 从第一个 pending 任务开始串行导出 |
| 暂停 | 当前视频完成后暂停，不强杀当前编码器 |
| 取消当前 | 向当前 Worker 发出协作式取消请求 |
| 清除已完成 | 只删除 done 任务，不影响 pending/running |
| 更多 | 重试失败、导出日志、停靠位置、回到 Deliver 下方 |
| 关闭按钮 | 只隐藏面板，不清空队列 |

### 5.2 任务列表

建议使用 `QTreeView + QAbstractItemModel`，不要为每个任务创建大量独立 QWidget。这样可以支持大量任务和稳定刷新。

```text
列 1：复选框 / 状态图标
列 2：来源
列 3：预设
列 4：输出文件
列 5：视频规格
列 6：音频
列 7：进度
列 8：状态
列 9：操作
```

示例：

```text
☐  ●  Timeline 1   Web H.264    Timeline1_Web.mp4
       1920x1080 / 25fps / AAC   ███████░░ 68%  渲染中  [取消]

☐  ✓  Timeline 1   Master       Timeline1_Master.mov
       3840x2160 / 25fps / PCM   100%           已完成  [打开]

☐  !  Timeline 2   Mobile       Timeline2_Mobile.mp4
       1080x1920 / 30fps / AAC   --             失败    [重试]
```

### 5.3 任务行绘制规则

如果使用自定义 `QStyledItemDelegate`，必须按以下顺序绘制：

1. 绘制选中背景。
2. 绘制状态图标和状态色。
3. 绘制主标题，超过宽度时省略。
4. 绘制次级信息：分辨率、FPS、编码器、音频。
5. 绘制进度条。
6. 绘制操作按钮区域。
7. 绘制行底部分隔线。

颜色约定：

| 状态 | 颜色 | 说明 |
|---|---|---|
| pending | `#9AA0A6` | 等待中 |
| running | `#4EA1FF` | 渲染中 |
| done | `#51B56A` | 已完成 |
| failed | `#E05A5A` | 失败 |
| cancelled | `#D59A43` | 已取消 |
| paused | `#C8A951` | 队列暂停 |

进度条要求：

- 高度 6–8 px，圆角 3 px。
- 进度 0% 时仍绘制轨道，不显示空白。
- running 使用蓝色；done 使用绿色；failed 不显示假进度，显示错误图标。
- 进度刷新只更新模型对应行，不重建整个列表。

### 5.4 空状态

没有任务时不要显示空白区域，显示视频语义明确的空状态：

```text
              尚未添加视频导出任务
       在上方选择时间线和视频预设，然后点击
                 “添加到视频队列”
```

空状态不使用图片图标，使用胶片/时间线/视频帧相关的矢量图标或现有视频编辑图标。

## 6. 关键交互流程

### 6.1 添加单个视频任务

```text
选择时间线
    ↓
选择范围
    ↓
选择视频预设或自定义编码
    ↓
选择音频、字幕和输出路径
    ↓
执行 Preflight 检查
    ↓
生成文件名预览
    ↓
点击“添加到视频队列”
    ↓
写入 ExportJob 配置快照
```

### 6.2 同一时间线生成多个视频

1. 选择一条时间线。
2. 勾选多个视频预设。
3. 每个预设生成一个独立视频任务。
4. 按文件名模板展开不同输出文件名。
5. 一次性展示所有任务的冲突和能力错误。

### 6.3 修改已加入队列的任务

任务加入队列后，当前设置区的变化不能影响它。用户需要通过任务行菜单选择：

```text
编辑任务 -> 将任务配置载入编辑区 -> 修改 -> 更新任务快照
```

running 任务不能编辑，只能取消后重试或复制为新任务。

### 6.4 进入 Deliver 页面

进入 Deliver 页面时：

- 上方设置区加载当前活动时间线。
- 下方队列加载持久化任务。
- 如果队列正在运行，不能因为切换页面而停止。
- 如果 Deliver feature 被禁用，不创建页面和面板。
- 如果项目没有时间线，显示明确的“请先创建时间线”状态。

## 7. 视频任务数据模型

建议扩展现有 `ExportJob`，但不要让 UI 控件对象进入序列化数据。

```python
VideoExportJob = {
    "schema_version": 1,
    "job_id": "uuid",
    "project_id": "stable-project-id",
    "project_revision": 12,
    "timeline_id": "timeline-id",
    "timeline_name": "Timeline 1",
    "source_kind": "timeline",
    "range_type": "entire | in_out | selected | custom",
    "start_frame": 0,
    "end_frame": 1200,
    "preset_id": "web_h264",
    "video_config": {
        "container": "mp4",
        "codec": "h264",
        "profile": "high",
        "resolution": [1920, 1080],
        "fps": 25.0,
        "quality_mode": "crf",
        "quality_value": 20,
        "pixel_format": "yuv420p",
        "color_space": "rec709",
        "bit_depth": 8,
        "alpha": False
    },
    "audio_config": {
        "include_audio": True,
        "codec": "aac",
        "sample_rate": 48000,
        "bitrate": 192
    },
    "subtitle_config": {
        "mode": "off",
        "track_id": None
    },
    "output_path": "/path/to/output.mp4",
    "overwrite_policy": "ask",
    "status": "pending",
    "progress": 0.0,
    "error": "",
    "retry_count": 0
}
```

项目 revision 变化时必须有明确策略：

- 相同 revision：允许直接恢复。
- revision 变化但时间线仍存在：提示用户确认后继续。
- 时间线不存在：标记为 `needs_relink`，不能静默失败。

## 8. 队列和线程架构

```text
VideoBatchExportPanel
        │ signal / command
        ▼
VideoBatchExportController
        │
        ├── ExportQueue（状态、顺序、持久化）
        └── VideoJobRunner（每次只运行一个任务）
                │
                └── QThread / QObject Worker
                        │
                        └── TimelineExporter / FFmpeg / Render Service
```

规则：

1. `VideoBatchExportPanel` 只负责控件、模型刷新和用户命令。
2. `ExportQueue` 不持有 Qt 控件。
3. `VideoJobRunner` 负责把一个 `VideoExportJob` 转换为现有 `TimelineExportConfig`。
4. Worker 在后台线程执行；进度通过 Qt signal 回到 UI。
5. UI 线程不得直接调用 ModernGL、OpenGL readback 或同步解码。
6. 取消、失败、窗口关闭都必须释放 Worker、线程和临时媒体资源。
7. 首期保持串行执行，不为了“批量”而同时跑多个 GPU 视频任务。

推荐信号：

```text
job_added(job_id)
job_started(job_id)
job_progress(job_id, progress, current_frame, total_frames)
job_status_changed(job_id, status)
job_completed(job_id, output_path)
job_failed(job_id, error_code, message)
job_cancelled(job_id)
queue_paused()
queue_resumed()
queue_drained()
```

## 9. 文件和模块实施表

| 阶段 | 文件 | 具体工作 | 验收标准 |
|---|---|---|---|
| P0 | `src/features/deliver/ui/video_export_settings_panel.py` | 从 `DeliverPage` 拆出视频任务设置区 | 单独可实例化、所有控件有类型注解、无导出副作用 |
| P0 | `src/features/deliver/ui/video_batch_export_panel.py` | 实现下方视频队列面板和任务列表 model/delegate | 空状态、任务行、进度、失败和操作状态正确 |
| P0 | `src/features/deliver/ui/deliver_page.py` | 改为上下 `QSplitter` 工作区 | 批量面板默认位于 Deliver 页面下方 |
| P0 | `src/features/deliver/core/video_export_job.py` | 定义视频任务快照、序列化、校验 | 不包含 QWidget、可 JSON round-trip |
| P0 | `src/features/deliver/core/video_job_runner.py` | 将一个任务接入现有 TimelineExporter | 导出在后台线程运行，UI 不阻塞 |
| P0 | `src/features/deliver/core/export_queue.py` | 补齐异步 runner 或 controller 适配 | 串行、暂停、取消、重试、恢复可测试 |
| P0 | `src/features/deliver/core/video_export_preflight.py` | 检查时间线、范围、编码器、路径、磁盘空间和能力 | 错误在开始前可解释显示 |
| P1 | `src/features/deliver/ui/detachable_panel_host.py` | 实现嵌入 / 浮动 / 任意区域停靠 | 面板可从底部拆出并回到 Deliver 下方 |
| P1 | `src/app/controllers/dock_layout_controller.py` | 接入 dock geometry、area、浮动状态保存 | 重启后恢复上次位置，旧布局不崩溃 |
| P1 | `src/features/deliver/module.py` | 注册 `deliver.batch_export`、菜单和快捷操作 | Window > Panels > Deliver 中可显示/隐藏 |
| P1 | `src/shared/panels/registry.py` | 如有必要，增加可拆卸嵌入面板 host 能力 | 不通过嵌套 QDockWidget 绕过 registry |
| P1 | `src/features/deliver/locales/zh_CN.json` | 增加中文文案 | 新增 UI 无硬编码中文字符串 |
| P1 | `src/features/deliver/locales/en_US.json` | 增加英文文案 | 中英文切换布局不溢出 |
| P1 | `tests/deliver/test_video_export_job.py` | 测试任务快照、校验、版本和 JSON | schema round-trip、错误字段可定位 |
| P1 | `tests/deliver/test_video_batch_export_panel.py` | Qt UI 测试 | 添加、编辑、删除、重试、空状态正确 |
| P1 | `tests/deliver/test_video_batch_export_dock.py` | 浮动/停靠/回底部测试 | 不丢任务、不重复创建 dock |
| P1 | `tests/deliver/test_video_export_preflight.py` | Preflight 测试 | 离线素材、路径冲突、编码能力错误可解释 |
| P2 | `docs` / project schema | 保存预设和项目 revision 兼容策略 | 老项目可打开，未知字段保留 |

## 10. UI 绘制和实现要求

### 10.1 不依赖截图的实现规则

代码实现必须以控件树、尺寸、状态和颜色规则为依据，不依赖模型理解截图。

每个区域都要明确：

- objectName
- 最小宽度/高度
- layout margins
- layout spacing
- 字体大小
- 文本省略规则
- enabled/disabled 条件
- 空状态和错误状态
- 键盘焦点顺序

建议 objectName：

```text
DeliverWorkspace
VideoDeliverSettingsPanel
VideoPresetBar
VideoSourceSection
VideoSettingsSection
AudioDeliverySection
VideoBatchExportPanel
VideoQueueToolbar
VideoQueueView
VideoQueueEmptyState
VideoQueueProgressDelegate
DetachableVideoBatchExportHost
```

### 10.2 颜色和层级

沿用 Topos 暗色 UI，但视频队列的状态色必须独立于图片导出颜色：

```text
主背景：       #17191F
面板背景：     #20232A
输入框：       #252932
边框：         #3A3F48
普通文本：     #E6E8EB
次级文本：     #9AA0A6
主操作橙：     #D47A2C
视频运行蓝：   #4EA1FF
完成绿：       #51B56A
失败红：       #E05A5A
警告黄：       #D59A43
```

不要把“正在渲染”使用图片批量导出的同一套文案或图标。状态文本应明确使用“视频渲染中”“视频任务失败”等视频语义。

### 10.3 适配窗口尺寸

| 窗口条件 | 行为 |
|---|---|
| 宽度 >= 1280 | 上方设置区三列布局 |
| 900–1279 | 视频设置和音频交付合并为两列或 Tab |
| < 900 | 上方使用纵向滚动区域，下方队列保持最小可见高度 |
| 面板浮动宽度 < 520 | 任务列表隐藏次级列，保留来源、文件名、状态和进度 |
| 面板浮动高度 < 260 | 隐藏估算详情，只保留队列工具栏和任务行 |
| 中文长文本 | 所有列使用 elide，tooltip 显示完整内容 |

## 11. Preflight 检查

点击“添加到视频队列”前检查：

| 检查 | 失败行为 |
|---|---|
| 当前项目存在 | 阻止添加，提示打开或创建项目 |
| 时间线存在 | 阻止添加，定位到时间线选择框 |
| 范围合法 | 阻止添加，标记 start/end 控件 |
| 素材在线 | 显示离线素材列表，允许取消或继续（按策略） |
| 容器和编码器兼容 | 阻止添加并解释可用组合 |
| 音频编码兼容 | 自动修正前必须显示提示 |
| 输出目录可写 | 阻止添加 |
| 文件名不为空 | 阻止添加 |
| 磁盘空间足够 | 警告或阻止，显示估算空间和剩余空间 |
| 色彩/位深路径可用 | 不能静默降级，显示能力警告 |

Preflight 结果应显示在设置区底部：

```text
✓ 时间线和范围有效
✓ 输出目录可写
⚠ 预计输出 3.8 GB，磁盘剩余 4.2 GB
```

## 12. 测试和验收清单

### 12.1 UI 验收

- Deliver 页面首次打开时，视频批量面板位于页面下方。
- 拖动上下 splitter 后，设置区和队列区都不会被压成不可用高度。
- 面板可浮动。
- 浮动面板可停靠到左、右、上、下区域。
- 面板可通过“回到 Deliver 下方”恢复默认位置。
- 关闭面板不会删除任务。
- 重启应用后，面板位置、大小和任务状态可恢复。
- 切换到 Color、Edit、Composite 页面时，不泄漏 Deliver 专属面板。
- Deliver feature disabled 时，不创建 Deliver 页面、队列和 dock。

### 12.2 视频任务验收

- 同一时间线可以通过多个预设生成多个视频任务。
- 每个任务具有独立的容器、编码器、分辨率、FPS、音频和输出路径。
- 修改当前设置不会修改已加入队列的任务。
- 一个 PNG/TIFF Sequence 只产生一个视频交付任务。
- 任务按队列顺序串行执行。
- 当前任务可以取消，后续任务仍保持 pending。
- 失败任务显示错误原因并可以重试。
- 应用崩溃或重启后，stale running 任务恢复为 pending 或 needs_recovery。
- 视频导出不阻塞 UI，不在 UI 线程调用 GPU。

### 12.3 视觉验收

- 任务表格在 3 个、20 个、100 个任务时都能滚动和刷新。
- 进度条不跳动、不因整表重建而闪烁。
- 中文、英文、长文件名不溢出。
- 失败和警告不仅依靠颜色，还包含图标和文本。
- 空状态明确表达“视频导出队列为空”。
- 下方区域标题明确写“视频渲染队列”，不能出现“图片批量导出”等文案。

## 13. 推荐实施顺序

```text
第 1 步：拆分 DeliverPage 的设置区和队列区
    ↓
第 2 步：定义 VideoExportJob 和序列化 schema
    ↓
第 3 步：把单个 VideoExportWorker 接入 ExportQueue
    ↓
第 4 步：实现视频队列列表、状态和进度绘制
    ↓
第 5 步：加入视频预设、多任务生成和文件名模板
    ↓
第 6 步：实现 Preflight 和错误展示
    ↓
第 7 步：实现底部嵌入 / 浮动 / 任意区域停靠
    ↓
第 8 步：补齐布局持久化、国际化和测试
```

第一阶段完成后，用户应该能够：

1. 在 Deliver 页面选择当前时间线。
2. 选择一个或多个视频预设。
3. 配置范围、编码、音频和输出路径。
4. 在 Deliver 页面下方看到多个视频任务。
5. 启动串行视频渲染队列。
6. 暂停、取消、重试和查看失败原因。
7. 将队列面板浮动或停靠到任意区域。

这个阶段不需要先实现图片导出兼容，也不需要复制图片批量导出的 UI。视频 Deliver 面板应该以自己的任务模型、自己的字段、自己的状态图标和自己的预检流程独立完成。
