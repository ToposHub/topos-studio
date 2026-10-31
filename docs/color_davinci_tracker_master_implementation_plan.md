# Topos Color：DaVinci 风格跟踪器完整实施计划

> 文档状态：规划中  
> 建立日期：2026-08-23  
> 目标模块：Color 页面 / 蒙版跟踪器  
> 技术栈：PyQt6 + QGraphicsView/QPainter + ModernGL + OpenCV + NumPy + SciPy

## 1. 需求边界与文档说明

### 1.1 用户真正提出的需求

用户希望在 Topos Color 中实现一个完整的 DaVinci 风格跟踪器：

1. 在画面上绘制圆形、矩形或曲线形状。
2. 指定形状对应的画面区域。
3. 向前、向后分析视频，使形状跟随目标移动。
4. 支持平移、水平/垂直倾斜、缩放、旋转、透视变化和 3D 摄像机跟踪。
5. 提供类似 DaVinci 的跟踪控制、曲线图、关键帧、失败修正和可视化反馈。
6. 所有 UI 绘制、状态、按钮、颜色、交互和验收标准都必须写清楚，不能依赖模型自行“看图猜界面”。

### 1.2 参考资料与约束的区别

- 用户提供的截图是 DaVinci Tracker 的视觉和交互参考，不是需要直接复制的源代码或产品规格文件。
- 项目根目录的 `AGENTS.md` 是 Agent 的开发规范，规定项目背景、代码约束和建议工作流，不是本功能的需求清单。
- 本文档是本功能的实施计划，后续每一阶段完成并通过验收后，才能开始下一阶段。

### 1.3 “完整能力”的目标定义

本项目无法获得 DaVinci 内部专有跟踪器的实现，因此“完整”定义为功能层面的可用对齐，而不是逐像素复刻 Blackmagic 的内部算法。目标分为三层：

| 层级 | 能力 | 交付要求 |
|---|---|---|
| L1 | 2D 点/区域跟踪 | 平移、缩放、旋转、水平/垂直倾斜、前后跟踪、置信度、失败标记 |
| L2 | 平面/透视跟踪 | 单应性、平面角点、遮挡恢复、手动修正、稳定结果 |
| L3 | 3D 摄像机跟踪 | 特征点云、相机运动、镜头参数、3D 参考平面、3D 结果应用 |

L1 是第一个可交付版本；L2 是专业可用版本；L3 是完整能力版本。L1、L2、L3 都必须经过各自的验收门槛，不能用“按钮存在”代替功能完成。

## 2. 当前项目可复用基础与主要缺口

### 2.1 已存在的基础

- [src/features/color/core/layer.py](../src/features/color/core/layer.py)：已有 `MaskShape`、`MaskGroup`、归一化坐标、Bezier 点、旋转、羽化、布尔运算、序列化。
- [src/features/color/ui/canvas.py](../src/features/color/ui/canvas.py)：已有 `MaskShapeItem`、Bezier 控制点、中心点、旋转点、尺寸点、画布叠加绘制和鼠标拖拽。
- [src/features/color/ui/mask_panel.py](../src/features/color/ui/mask_panel.py)：已有蒙版列表、选择、可见性、反转、布尔操作、群组和参数更新信号。
- [src/features/color/core/shader_compiler.py](../src/features/color/core/shader_compiler.py)：已有蒙版合成、羽化和调色渲染链。
- [src/features/color/core/async_renderer.py](../src/features/color/core/async_renderer.py)：已有异步 GPU 渲染和视频帧处理边界。
- [src/shared/keyframe.py](../src/shared/keyframe.py)：已有通用关键帧、插值和序列化能力。
- Color 页面已有 source frame、timeline frame、clip local frame 的映射，跟踪结果必须使用 source frame 作为视频分析坐标，同时保留 timeline frame 映射。
- `requirements.txt` / `pyproject.toml` 已包含 OpenCV、NumPy、SciPy，可先使用 OpenCV 实现传统跟踪器，不引入 AI 模型作为第一版前置条件。
- 项目已有大量 Color、视频、关键帧和 UI 测试，可按照现有测试分层补充跟踪测试。

### 2.2 主要缺口

1. 没有跟踪数据模型：当前蒙版几何是静态值，没有按帧保存变换。
2. 没有跟踪引擎：没有特征点选择、光流、区域匹配、单应性估计和 3D 求解。
3. 没有异步跟踪任务协议：没有取消、进度、代际 ID、陈旧结果丢弃和失败恢复。
4. 没有跟踪与蒙版的绑定关系：跟踪结果还不能驱动 `MaskShape` 或曲线控制点。
5. 没有 Tracker 专用 UI：现有蒙版面板和画布工具不能直接承载跟踪状态、曲线、轨迹点和错误标记。
6. 没有跟踪缓存和项目持久化格式。
7. 没有用于评估跟踪质量的固定测试素材、人工标注轨迹和指标。
8. 没有 3D 摄像机解算和 3D 参考平面数据结构。

## 3. 总体架构

### 3.1 建议新增模块

```text
src/features/color/tracking/
├── __init__.py
├── types.py                  # 所有不可变数据结构、枚举、协议
├── track_data.py             # 每帧结果、轨迹段、关键帧和质量信息
├── frame_sampler.py          # source frame 读取、缩略图/代理帧采样
├── feature_detector.py       # 特征点检测、网格分布和质量筛选
├── tracker_2d.py             # 点跟踪、区域跟踪、仿射/单应性估计
├── tracker_3d.py             # 相机内参、基础矩阵、PnP、相机轨迹
├── tracking_worker.py        # QThread/任务取消/进度/结果代际管理
├── tracking_store.py         # 工程保存、缓存、版本迁移
├── tracking_evaluator.py     # 跟踪结果到蒙版/节点/3D 平面的应用
└── tracking_service.py       # Color 页面使用的高层服务门面

src/features/color/ui/
├── tracker_panel.py          # Tracker 主面板
├── tracker_toolbar.py        # 目标模式、跟踪方向、播放控制
├── tracker_graph.py          # 轨迹曲线和关键帧图
├── tracker_status.py         # 进度、质量、错误和操作提示
└── tracker_overlay.py        # 画布中的搜索框、特征点、路径和 3D 叠加
```

### 3.2 现有模块的改造点

| 文件 | 改造内容 |
|---|---|
| `color/core/layer.py` | 为 `MaskShape` 增加 tracker binding，不把完整跟踪算法塞进数据类 |
| `color/ui/canvas.py` | 复用 `MaskShapeItem`，增加 Tracker overlay 图层和鼠标状态机 |
| `color/ui/mask_panel.py` | 保持蒙版列表职责，增加“发送到跟踪器/显示轨迹”入口，不把 Tracker 图表塞进此文件 |
| `color/core/async_renderer.py` | 只接收已经求值的几何结果，不在 GPU 渲染线程运行 OpenCV |
| `shared/keyframe.py` | 尽量复用通用插值；跟踪结果需要独立类型，避免把数千帧跟踪点伪装成普通 UI 关键帧 |
| Color controller | 负责选中形状、创建任务、取消任务、提交结果、触发重新渲染和自动保存 |
| 工程序列化 | 增加 tracker schema version、源素材 fingerprint、分析参数和结果版本 |

### 3.3 线程边界

```text
UI 线程
  ├─ 绘制 QGraphicsItem / 图表 / 状态
  ├─ 发起 TrackingRequest
  └─ 接收不可变 TrackingProgress / TrackingResult

Tracking worker 线程
  ├─ 读取 source frame
  ├─ OpenCV 特征检测、匹配、光流和几何求解
  ├─ 生成质量与失败信息
  └─ 响应 cancel token

渲染线程
  ├─ 接收当前 frame 的 resolved mask transform
  ├─ 运行 GPU 蒙版/调色链
  └─ 不调用 OpenCV、不触碰 Qt UI 对象
```

## 4. 分阶段实施路线

### 阶段 0：建立能力契约、素材和基线

**目的**：先确定“什么算完成”，避免后续只完成 UI 外壳。

**工作内容**：

- 建立本计划中的能力矩阵和术语：source frame、timeline frame、clip local frame、track frame。
- 准备四类固定测试素材：
  1. 只有平移的高对比度平面。
  2. 平移 + 缩放 + 旋转。
  3. 透视变化的平面。
  4. 遮挡、运动模糊、出画和重新出现。
- 为每个素材建立人工参考点或角点轨迹。
- 记录当前 Color 页面打开视频、切换帧、蒙版显示、保存和重新打开的基线。
- 确认代理帧分析和原始帧坐标之间的缩放矩阵。

**完成标准**：

- 能通过脚本稳定读取所有测试素材的指定 source frame。
- 能把 source frame 坐标转换到 viewer 坐标，再转换回归一化蒙版坐标。
- 播放、拖动、切换片段不会产生旧帧跟踪结果覆盖新片段的问题。
- 新增测试：`tests/color/test_tracking_frame_mapping.py`、`tests/color/test_tracking_fixture_manifest.py`。

**验证门**：未通过坐标和素材基线，不允许开始算法开发。

---

### 阶段 1：跟踪数据模型和持久化

**目的**：先让数据可保存、可重放、可撤销，再接算法。

**核心类型建议**：

```python
class TrackingMode(Enum):
    TRANSLATION = "translation"
    TRANSLATION_ROTATION_SCALE = "trs"
    AFFINE = "affine"
    HOMOGRAPHY = "homography"
    CAMERA_3D = "camera_3d"

@dataclass(frozen=True)
class TrackTransform:
    frame: int
    matrix: tuple[float, ...]       # 3x3 row-major
    translation: tuple[float, float]
    rotation: float
    scale: tuple[float, float]
    shear: tuple[float, float]
    confidence: float
    status: str                      # valid / weak / failed / manual

@dataclass(frozen=True)
class FeatureObservation:
    frame: int
    feature_id: int
    x: float
    y: float
    error: float
    confidence: float
    status: str

@dataclass
class TrackingBinding:
    shape_id: str
    source_frame_start: int
    source_frame_end: int
    reference_frame: int
    mode: TrackingMode
    search_region: tuple[float, float, float, float]
    pattern_region: tuple[float, float, float, float]
    transforms: dict[int, TrackTransform]
    observations: dict[int, tuple[FeatureObservation, ...]]
    manual_corrections: dict[int, TrackTransform]
    result_version: int
```

**必须考虑的字段**：

- 源素材 fingerprint、clip ID、source frame 范围、参考帧。
- 分析分辨率、代理比例、颜色转换版本、算法版本。
- 目标形状 ID、目标形状初始几何和绑定方式。
- 前向和后向结果是否存在。
- 手动修正帧不能被重新分析覆盖，除非用户明确选择“清除修正”。
- 结果版本和任务代际，防止旧任务写回。

**完成标准**：

- 空轨迹、完整轨迹、部分失败轨迹、含手动修正轨迹都能保存和加载。
- 旧工程没有 tracker 字段时正常加载。
- 保存后重新打开，形状、轨迹、当前帧和曲线选择状态一致。
- 对同一帧只允许一个最终结果，但要保留“自动结果”和“手动修正结果”的来源。

**测试**：

- `tests/color/test_tracking_data.py`
- `tests/color/test_tracking_serialization.py`
- `tests/color/test_tracking_schema_migration.py`

**验证门**：通过保存/加载和版本迁移测试，才能接 UI。

---

### 阶段 2：2D 特征检测、点跟踪和质量评估

**目的**：实现 L1 跟踪器的算法核心，不先追求复杂 UI。

**算法层次**：

1. 在 pattern region 中检测 Shi-Tomasi 或 ORB 特征点。
2. 使用金字塔 Lucas-Kanade 光流进行相邻帧跟踪。
3. 使用 forward-backward check 检查点是否回到原位置。
4. 使用 RANSAC 去除离群点。
5. 根据有效点数量、重投影误差、点分布、前后向误差计算置信度。
6. 使用网格分桶，避免特征点全部集中在目标的一个角落。
7. 当有效点不足时，进入 `weak` 或 `failed`，不能静默产生错误变换。

**变换模式**：

- Translation：至少 1 个稳定点，建议 4 个以上。
- Translation + Rotation + Scale：至少 2 个非重合点，建议 6 个以上。
- Affine：至少 3 个非共线点，建议 8 个以上。
- Homography：至少 4 个非共线点，建议 12 个以上。

**质量指标**：

```text
confidence =
    点数量分数
  × 空间分布分数
  × forward-backward 分数
  × RANSAC 内点比例
  × 重投影误差分数
```

阈值必须配置化，并显示在诊断信息中，不允许使用没有解释的魔法数字。

**完成标准**：

- 四类测试素材都能输出逐帧 `TrackTransform`。
- 纯平移素材不会产生明显旋转或缩放漂移。
- 目标被短暂遮挡时标记为 weak/failed，而不是继续输出无依据的轨迹。
- 跟踪方向前后结果对同一参考帧能够合并。
- 任意一个 worker 结果都带 task generation，取消或换片段后旧结果会被丢弃。

**测试**：

- `tests/color/test_tracker_2d_translation.py`
- `tests/color/test_tracker_2d_trs.py`
- `tests/color/test_tracker_confidence.py`
- `tests/color/test_tracker_failure_detection.py`
- `tests/color/test_tracker_cancel_and_generation.py`

**验证门**：先用离线脚本输出指标和轨迹 CSV/JSON，再接入实时 UI。

---

### 阶段 3：Tracker 任务服务、缓存和异步执行

**目的**：把跟踪算法变成 Color 页面可安全调用的服务。

**任务协议**：

```text
TrackingRequest
  ├─ session_id / clip_id / shape_id
  ├─ source_frame_start / source_frame_end
  ├─ reference_frame
  ├─ direction: forward / backward / both
  ├─ mode: translation / trs / affine / homography / 3d
  ├─ pattern_region / search_region
  ├─ quality: draft / normal / high
  └─ generation

TrackingProgress
  ├─ generation
  ├─ current_frame / total_frames
  ├─ valid_points / confidence
  └─ status_message

TrackingResult
  ├─ generation
  ├─ binding_id
  ├─ transforms / observations
  ├─ failures
  └─ diagnostics
```

**必须实现**：

- 开始、暂停、取消、清除结果、只重新跟踪失败段。
- 任务进度不能阻塞 UI；UI 只消费信号。
- 同一形状不能同时运行两个互相覆盖的任务。
- 更换 clip、source frame 范围或参考帧时，旧任务自动失效。
- 缓存 key 至少包含素材 fingerprint、帧范围、算法版本、参数和 pattern 区域。
- 预览分析优先使用代理帧，最终导出可以选择高质量重新分析。

**完成标准**：

- 4K 视频上 UI 可以继续拖动时间线，跟踪线程不冻结界面。
- 取消后 200 ms 内停止继续发布结果，旧结果不写回。
- 重新打开工程可优先加载缓存，不重复计算。
- 内存和线程在连续执行 20 次任务后无增长性泄漏。

**测试**：

- `tests/color/test_tracking_service.py`
- `tests/color/test_tracking_worker_lifecycle.py`
- `tests/color/test_tracking_cache.py`
- `tests/color/test_tracking_stale_result_guard.py`
- `tests/color/test_tracking_soak.py`

**验证门**：通过任务取消、换片段、连续任务和 UI 响应性测试后，才进入 Tracker UI。

---

### 阶段 4：画布 Tracker Overlay 与 DaVinci 风格交互

**目的**：实现截图中最重要的视觉反馈，让用户能看懂“正在跟踪什么”。

#### 4.1 画布图层顺序

从底到顶固定为：

```text
视频/调色结果
→ 蒙版实际边界
→ Tracker search region
→ Tracker pattern region
→ 特征点和方向线
→ 当前跟踪路径
→ 当前帧控制点、中心点、旋转点
→ 错误/弱跟踪标记
→ 鼠标 hover 提示
```

Tracker overlay 必须是独立 QGraphicsItem 图层，不能把跟踪点永久写入蒙版图形，否则会破坏蒙版编辑和撤销逻辑。

#### 4.2 颜色和线宽规格

所有颜色必须使用固定常量，不依赖系统主题自动变化：

| 元素 | 颜色 | 透明度 | 线宽 |
|---|---|---:|---:|
| 普通搜索框 | `#00D9A6` | 220 | 1 px |
| pattern 区域 | `#FFD43B` | 230 | 1 px |
| 当前形状 | `#FF8A00` | 255 | 1.5 px |
| 有效特征点 | `#36D7FF` | 240 | 1 px |
| 弱特征点 | `#FFB020` | 240 | 1 px |
| 失败特征点 | `#FF4D5E` | 240 | 1 px |
| 当前路径 | `#FFFFFF` | 210 | 1 px |
| 手动修正点 | `#C77DFF` | 255 | 2 px |
| 3D 轴 X/Y/Z | `#FF4D5E/#50E080/#4DA3FF` | 255 | 2 px |

线条使用 cosmetic pen，保证缩放 viewer 时仍保持 1 个物理像素。控制点使用 7 px～11 px 的屏幕尺寸，并设置 `ItemIgnoresTransformations`。

#### 4.3 交互状态机

```text
Idle
 ├─ 选择形状 → Selected
 ├─ 点击新建窗口 → Drawing
 └─ 点击 Tracker 工具 → TrackerReady

TrackerReady
 ├─ 拖动 pattern region → PatternEditing
 ├─ 拖动 search region → SearchEditing
 ├─ 点击 Track Forward → TrackingForward
 ├─ 点击 Track Backward → TrackingBackward
 └─ 按 Esc → Idle

TrackingForward / TrackingBackward
 ├─ 进度更新 → 保持状态
 ├─ 用户取消 → Cancelled → TrackerReady
 ├─ 失败 → Failed
 └─ 完成 → Tracked

Tracked
 ├─ 拖动当前帧控制点 → ManualCorrection
 ├─ 点击继续跟踪 → Tracking...
 ├─ 点击清除 → TrackerReady
 └─ 切换形状 → Selected
```

任何拖动操作都必须有：按下、拖动、释放三个明确阶段；拖动过程中只更新 overlay，释放时才提交模型和触发一次最终渲染。

#### 4.4 画布控件规格

- Pattern region：实线黄色矩形，四角 8 px 控制点；拖动内部移动，拖动边角缩放。
- Search region：虚线青绿色矩形，四角 8 px 控制点；必须始终包住 pattern region，否则显示红色边框并禁止开始跟踪。
- 特征点：有效点为实心青色圆点，弱点为橙色，失败点为红色叉号。
- 跟踪路径：从参考帧到当前帧绘制折线；超过 2000 点时按显示缩放抽样，但原始数据不能丢失。
- 当前帧：显示当前变换后的 pattern region 和目标中心十字。
- 失败帧：路径变红并在对应位置显示三角警告图标；点击警告图标跳转到该帧。
- 3D 模式：显示地面网格、X/Y/Z 三轴、相机锥体和参考平面；3D overlay 与 2D 蒙版 overlay 分层管理。

---

### 阶段 5：Tracker 主面板和截图级 UI

**目的**：实现明确、稳定、可操作的 DaVinci 风格 Tracker 面板。

#### 5.1 总体布局

Tracker 面板建议作为 Color 页面右侧 Dock，默认宽度 300 px，最小宽度 260 px，最大宽度 480 px。面板分为五个垂直区域：

```text
┌──────────────────────────────────────────────┐
│ Tracker 标题栏 + 当前形状 + 状态灯             │ 32 px
├──────────────────────────────────────────────┤
│ 模式/工具栏                                    │ 38 px
├──────────────────────────────────────────────┤
│ 跟踪控制区                                     │ 76 px
├──────────────────────────────────────────────┤
│ 参数与质量区                                   │ 150 px
├──────────────────────────────────────────────┤
│ 曲线图区                                       │ stretch
├──────────────────────────────────────────────┤
│ 底部操作栏                                     │ 34 px
└──────────────────────────────────────────────┘
```

背景使用 `#1A1A1A`，分隔线使用 `#343434`，文本使用 `#D8D8D8`，次要文本使用 `#8C8C8C`，选中/警告使用项目统一橙色 `#FF8A00`。

#### 5.2 标题栏

- 左侧文本：`跟踪器 - 窗口`。
- 右侧显示当前绑定形状名称，如 `Window 1`。
- 状态灯：
  - 灰色：无跟踪数据。
  - 青绿色：存在有效跟踪。
  - 黄色：存在 weak 帧。
  - 红色：当前帧跟踪失败。
- 右侧按钮：设置、帮助、更多操作。
- 标题栏高度 32 px，文字 11 px，字重 bold。

#### 5.3 模式工具栏

按从左到右固定排列：

1. 点跟踪图标。
2. 区域跟踪图标。
3. 平面/透视图标。
4. 3D 摄像机图标。
5. 目标选择下拉框。
6. `2D` / `3D` 模式切换。

每个按钮 28×28 px，图标 16×16 px。选中按钮背景为 `rgba(255,138,0,35)`，底部 2 px 橙色指示条。所有按钮必须有中文 tooltip、英文 fallback 和键盘焦点框。

#### 5.4 跟踪控制区

第一排为帧导航：

```text
|◀ 首帧|◀ 前一帧|■ 停止|▶ 后一帧|▶ 最后一帧|
```

第二排为跟踪动作：

```text
|向后跟踪|停止跟踪|向前跟踪|双向跟踪|重新跟踪失败帧|
```

按钮规范：

- 普通按钮 72×26 px。
- 主动作“向前跟踪”和“向后跟踪”使用青绿色文字。
- 取消/停止使用红色 hover 边框，但不能使用纯红色大面积填充。
- 运行时，播放控制只保留“暂停”和“取消”；所有会改变分析参数的控件禁用。
- 进度显示为 `Frame 132 / 480 · 74% · 8/12 points`。

#### 5.5 质量与参数区

使用两列网格，标签宽度 78 px，控件宽度自适应：

| 标签 | 控件 | 说明 |
|---|---|---|
| 跟踪类型 | 下拉框 | 平移 / 平移旋转缩放 / 仿射 / 透视 / 3D |
| 分析质量 | 下拉框 | 草稿 / 标准 / 高质量 |
| 特征点数 | 数值框 | 建议值和实际有效值分开显示 |
| 搜索范围 | 数值/拖动值 | 相对 pattern 的倍数 |
| 最低置信度 | 拖动值 | 低于此值标记 weak |
| 失败策略 | 下拉框 | 停止 / 保持上一帧 / 尝试重捕获 |
| 显示点 | 复选框 | 显示/隐藏特征点 |
| 显示路径 | 复选框 | 显示/隐藏轨迹 |
| 显示搜索框 | 复选框 | 显示/隐藏 pattern/search |

所有数值控件支持拖动调整、双击输入、右键恢复默认值。拖动时显示完整数值和单位，释放后提交一次参数变更。

#### 5.6 曲线图区

曲线区是截图中的核心，不允许用普通 `QChart` 默认样式代替。建议自定义 `QWidget` + `QPainter`，以保证视觉可控。

固定规格：

- 背景 `#111216`。
- 上方时间刻度区高度 22 px。
- 左侧数值刻度宽度 38 px。
- 主绘图区显示水平网格，每 25% 一条细线。
- 右侧滚动条宽度 10 px，滑块颜色 `#888888`。
- 底部时间轴显示帧号或 timecode，跟随 Color 当前帧。
- 当前帧竖线为 1 px 红色，顶部带 8 px 红色三角标。
- 曲线颜色固定：
  - 平移 X：`#00E0B0`。
  - 平移 Y：`#4DA3FF`。
  - 缩放：`#FF38A8`。
  - 旋转：`#FFD43B`。
  - 倾斜/透视：`#C77DFF`。
  - 3D 深度：`#FF9F43`。
- 当前曲线右下角显示最后一个值，保留两位小数。
- 有效关键帧为 6 px 菱形；手动修正帧为紫色菱形；失败帧为红色三角。
- 鼠标 hover 显示十字光标、帧号、参数名和值。
- 左键单击选择曲线/关键帧；Shift 多选；Ctrl/Cmd 点击取消选择。
- Alt/Option 拖动关键帧复制；Delete 删除手动修正；双击空白处新增手动修正点。
- 滚轮垂直缩放，Shift+滚轮水平缩放，按 `F` 适配全部曲线，按 `Home` 回到参考帧。

曲线图区必须支持“片段模式”和“帧模式”：

- 片段模式显示整个 clip 的轨迹。
- 帧模式显示当前参考帧附近的详细轨迹。

#### 5.7 底部操作栏

从左到右：

- 交互模式复选框。
- 显示 overlay 按钮。
- 选择/移动工具。
- 删除手动修正按钮。
- 重置当前帧按钮。
- 右侧下拉框：`云跟踪` / `本地跟踪` / `自动`。

如果没有云端能力，`云跟踪`必须显示为禁用态并附 tooltip，不得显示一个点击后无响应的假按钮。默认使用本地跟踪。

#### 5.8 空状态、运行态、成功态、失败态

- 空状态：曲线区显示“选择一个蒙版窗口，然后点击开始跟踪”，不能显示空白黑框。
- 运行态：显示进度、当前帧、有效点数、置信度；禁止修改算法参数。
- 成功态：显示绿色状态灯、有效帧范围、平均置信度和“重新跟踪/清除结果”。
- 部分失败：黄色状态灯，曲线断开，失败区间底部显示黄色阴影，可点击跳转。
- 当前帧失败：红色状态灯，viewer 上显示红色警告框和错误原因。

---

### 阶段 6：跟踪结果应用到蒙版和关键帧系统

**目的**：让跟踪结果真正改变蒙版位置，而不是只显示轨迹。

**应用规则**：

1. 以参考帧的蒙版几何为基准。
2. 当前帧先计算 `TrackTransform`。
3. 将变换应用到 `MaskShape` 的 center/size/rotation。
4. 对曲线蒙版，默认应用整体仿射/透视变换到所有 `pos/c1/c2`。
5. 不直接永久覆盖用户原始形状；使用 `base_geometry + tracking_transform` 求值。
6. 手动修正优先级高于自动跟踪结果。
7. 用户关闭 tracking binding 后，原始蒙版回到未跟踪状态。
8. 只在当前帧求值，不为每帧复制一份完整 MaskShape 对象。

**关键帧策略**：

- 跟踪结果是高密度的逐帧 transform 数据。
- 普通关键帧系统用于用户可编辑的稀疏修正点。
- 提供“将跟踪结果烘焙为关键帧”，并允许设置采样间隔。
- 烘焙前保留原始跟踪数据，撤销后可以恢复。

**完成标准**：

- 圆形、矩形和曲线蒙版都能随目标运动。
- 参考帧不会发生双重变换。
- 拖动时间线时 overlay、蒙版和调色结果始终使用同一 source frame。
- 手动修正后继续向前跟踪不会覆盖修正帧。
- 调色渲染线程只接收最终求值参数，不触碰 tracking store。

**测试**：

- `tests/color/test_tracking_mask_binding.py`
- `tests/color/test_tracking_curve_transform.py`
- `tests/color/test_tracking_manual_correction.py`
- `tests/color/test_tracking_keyframe_bake.py`
- `tests/color/test_tracking_source_timeline_frame_sync.py`

**验证门**：至少用一个真实视频完成“绘制形状 → 跟踪 → 拖动时间线 → 保存 → 重开”的闭环。

---

### 阶段 7：失败处理、遮挡、重捕获和专业交互

**目的**：从“能跟踪”提升到“可用于真实工作”。

**功能**：

- 失败帧自动停止或按用户策略保持上一帧。
- 在失败帧之前和之后寻找新的特征点并尝试重捕获。
- 用户调整 pattern/search region 后，从当前帧继续跟踪。
- 用户可把某帧标记为“手动修正”，修正点固定显示在曲线图中。
- 前向和后向结果可以在参考帧合并，冲突时显示冲突提示。
- 轨迹分段显示，失败段不使用虚线假装有效。
- 支持锁定某个特征点、删除坏点、重新检测特征点。
- 支持跟踪点排名：位置、误差、置信度、是否内点。
- 支持“只显示当前形状”“只显示当前轨迹”“显示所有轨迹”。

**完成标准**：

- 遮挡素材不会出现明显跳变而 UI 仍显示成功。
- 用户可以定位失败帧并在该帧修正后继续分析。
- 所有自动恢复行为都有状态文字和可撤销记录。
- 失败原因至少区分：特征点不足、遮挡、越界、图像模糊、几何求解失败。

---

### 阶段 8：平面/透视跟踪（L2）

**目的**：支持屏幕、墙面、纸张、车牌等平面目标。

**工作内容**：

- 以 RANSAC 单应性估计替代只使用 TRS 的变换。
- Viewer 中显示四角、边中点、透视方向和变形后的 pattern region。
- 支持锁定平面四角、手动拖动角点修正。
- 显示单应性矩阵稳定性、重投影误差、内点数量。
- 对极端透视和退化四边形给出明确错误。
- 允许将平面跟踪结果导出为 corner pin 数据。

**验收**：

- 透视测试素材四角平均误差达到项目设定阈值。
- 旋转到接近侧面时，系统能停止并标记不可靠，而不是生成爆炸矩阵。
- 输出应用到曲线蒙版时，所有控制点保持拓扑关系。

---

### 阶段 9：3D 摄像机跟踪（L3）

**目的**：实现截图中 3D 能力和可视化，不把 3D 当作 2D 按钮别名。

**工作内容**：

- 通过多帧特征匹配建立 2D tracks。
- 估计基础矩阵/本质矩阵，恢复相对相机运动。
- 支持已知/未知焦距、传感器尺寸和镜头畸变参数。
- 三角化稀疏 3D 点云并显示重投影误差。
- 计算相机轨迹、参考平面和世界坐标系。
- 显示 3D viewer overlay：网格、轴、相机锥体、点云、参考平面。
- 支持导出相机轨迹和参考平面，格式另行确定。
- 处理无视差、纯旋转、特征不足和尺度不确定性。

**3D UI 规格**：

- 3D 模式选中后，面板标题显示 `跟踪器 - 3D 摄像机`。
- 曲线图区可切换 `相机 X/Y/Z`、`旋转 X/Y/Z`、`焦距`、`重投影误差`。
- Viewer 右下角显示坐标轴图例，颜色固定为 X 红、Y 绿、Z 蓝。
- 点云默认白色，选中点橙色，低置信度点红色。
- 参考平面使用半透明青绿色网格，透明度 20%。
- 必须提供“设置地面”“设置原点”“设置尺度”三个明确动作。

**验收**：

- 纯旋转镜头和有明显平移的镜头分别有独立测试。
- 3D 结果必须有重投影误差和点数指标。
- 3D 失败时显示原因和可操作建议，不能只显示“跟踪失败”。

---

### 阶段 10：性能、导出和产品化验收

**目的**：保证功能在真实视频编辑环境中可持续使用。

**性能目标**：

- UI 线程在跟踪期间保持可交互，普通操作不超过 50 ms 卡顿。
- 预览跟踪使用代理帧，4K 原始帧不直接全部复制到 UI 线程。
- 轨迹图在 100,000 个点以内保持可操作；显示层可以抽样，数据层不能丢失。
- 连续 30 分钟跟踪和取消任务不发生内存持续增长。
- 渲染线程和跟踪线程不能竞争同一个 OpenGL context。

**产品化功能**：

- 工程保存和重新打开。
- 源视频替换后的 fingerprint 检查。
- 代理/原始结果切换。
- 结果版本迁移。
- 撤销/重做。
- 日志和诊断包导出。
- 一键导出跟踪轨迹 JSON/CSV。
- 自动化回归素材和阈值报告。

**最终验收**：

- L1、L2、L3 能力矩阵全部有测试证据。
- 所有 UI 状态可通过键盘操作和自动化测试触发。
- 失败、取消、换片段、关闭页面、重新打开工程均无崩溃和旧结果污染。
- Color 页面已有功能（蒙版、调色、播放、保存、导出）不回归。

## 5. 第一批实际代码文件建议

第一轮只实现阶段 0 和阶段 1，不要同时修改全部 UI：

```text
新增：
src/features/color/tracking/__init__.py
src/features/color/tracking/types.py
src/features/color/tracking/track_data.py
src/features/color/tracking/tracking_store.py
tests/color/test_tracking_data.py
tests/color/test_tracking_serialization.py
tests/color/test_tracking_frame_mapping.py

后续再新增：
src/features/color/tracking/frame_sampler.py
src/features/color/tracking/feature_detector.py
src/features/color/tracking/tracker_2d.py
src/features/color/tracking/tracking_worker.py
src/features/color/ui/tracker_panel.py
src/features/color/ui/tracker_graph.py
src/features/color/ui/tracker_overlay.py
```

第一轮不应修改 `shader_compiler.py`，因为数据模型和保存格式尚未稳定。第一轮也不应把 Tracking 逻辑直接写进 `MaskShape`，避免数据类变成算法和 UI 的耦合中心。

## 6. 每阶段统一验收模板

每次实施一个阶段后，必须提交以下证据，未完成的阶段不得标记为完成：

```text
阶段编号：
变更文件：
新增测试：
执行命令：
测试结果：
人工操作路径：
截图/录屏证据：
性能数据：
已知限制：
回滚方式：
```

建议的验证命令：

```bash
python -m pytest tests/color/test_tracking_data.py -q
python -m pytest tests/color/test_tracking_serialization.py -q
python -m pytest tests/color/test_tracking_frame_mapping.py -q
python -m pytest tests/color -q
```

接入 UI 后再增加：

```bash
python -m pytest tests/ui -q
python -m pytest tests/color tests/video tests/app -q
```

## 7. 开发顺序纪律

1. 一次只实现一个阶段。
2. 先写数据模型和测试，再写算法，再接 UI。
3. 先让离线结果正确，再追求实时速度。
4. 每个异步任务必须有取消、代际和陈旧结果保护。
5. 所有 UI 状态都必须有空态、运行态、成功态和失败态。
6. 所有跟踪结果都必须能解释：当前帧、有效点数、置信度、误差和失败原因。
7. 任何“看起来像 DaVinci”的按钮，在没有真实后端能力前必须显示禁用态或明确提示，不能做假功能。
8. 3D 跟踪必须独立验收，不能用 2D 变换结果伪装。
9. UI 线程不执行 OpenCV 和大型帧复制；渲染线程不操作 Qt 控件。
10. 每阶段完成后先验证，再继续下一阶段。

## 8. 当前推荐起点

下一步应只做 **阶段 0：能力契约、固定素材、坐标映射基线**，然后做 **阶段 1：跟踪数据模型和持久化**。这两阶段完成后，后续算法和 UI 才有稳定的接口。

当前不建议直接创建 `tracker_panel.py` 或在 `canvas.py` 中堆叠 DaVinci UI，因为如果帧坐标、source frame 映射和跟踪数据格式没有先固定，后续必然需要大规模返工。
