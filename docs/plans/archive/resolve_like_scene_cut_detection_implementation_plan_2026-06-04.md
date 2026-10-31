# Resolve-like 场景剪切探测实施计划

日期：2026-06-04

目标：在 Topos Color/Edit 中实现接近 DaVinci Resolve 体验的场景剪切探测功能。算法第一阶段不使用 AI，基于 PyAV/FFmpeg + OpenCV/Numpy 的传统帧差、直方图、边缘、亮度和时间序列规则实现；重点做到在媒体池素材上右键启动检测、打开独立 Scene Detect 弹窗、检测结果可预览、可逐个接受/拒绝、可标记和高亮疑似误判、可点击 `Add Cuts to Media Pool` 将所有接受的片段作为虚拟子片段加载到媒体池、可缓存、可使用代理素材加速、长素材性能可控，并正确处理闪光、快速运动、转场、黑场、VFR、掉帧和不同 fps 的精确映射。

适用范围：

- `src/shared/media/`
- `src/features/edit/core/`
- `src/features/edit/ui/`
- `src/features/edit/module.py`
- `src/app/controllers/edit_controller.py`
- `src/features/edit/core/project.py`
- `src/features/edit/core/serialization.py`
- `src/features/edit/core/project_serializer.py`
- `src/features/edit/locales/en_US.json`
- `src/features/edit/locales/zh_CN.json`
- `tests/media/`
- `tests/edit/`
- `tools/`

不在本计划范围内：

- 不接入 AI/ONNX 模型。
- 不改 Color 页 ModernGL/GLSL 调色核心。
- 页面布局、交互密度和信息层级高度接近 DaVinci Scene Detect，但不复制商标、专有图标或不可替代的像素级视觉资产。
- 不在检测阶段修改原素材文件。
- 不让检测任务阻塞 UI 线程。

---

## 1. 当前项目基础判断

| 能力 | 当前状态 | 对本功能的意义 |
|---|---|---|
| 视频解码 | 已有 `src/shared/media/ffmpeg_source.py` 和 PyAV/FFmpeg 探测 | 可以复用媒体元数据和帧读取能力 |
| OpenCV/Numpy | 启动脚本已检查 `cv2`，多处 AI/波形/矢量示波器使用 | 可以做非 AI 的图像差异计算 |
| 时间线模型 | `ClipInstance` 有 `source_start`、`source_duration`、`timeline_start`、`timeline_duration` | 检测结果可以映射到 clip 内源帧和时间线帧 |
| 命令系统 | 已有 `SplitClipCommand`、`MacroCommand`、`timeline_ops.split_clip` | 自动切片可以一次提交、一次撤销 |
| Marker | `TimelineMarker` 已存在 | 检测点可先落为 marker，再执行切片 |
| 代理/缓存 | 有 proxy、thumbnail、decode queue、disk proxy 服务 | 长素材检测可以优先用代理或低分辨率解码 |
| UI 架构 | Edit 页已有 media pool、source viewer、timeline panel | 可以添加检测面板/对话框/结果 review 流程 |
| 媒体池素材 | `MediaAsset` 已有 `metadata` 字典和 media pool 展示 | 可扩展为虚拟子片段，不需要真的切割文件 |

主要缺口：

- 没有独立的 scene cut detection 数据模型。
- 没有后台检测 worker、进度、取消和结果缓存。
- 没有结果 review UI。
- 没有 subclip/虚拟子片段模型，无法把检测结果作为多个媒体池条目显示。
- 当前 `split_clip` 需要检查是否完整保留 clip 的调色/合成/淡入淡出/动画参数。
- VFR/PTS 到 timeline frame 的映射需要专门建索引，不能只依赖浮点 fps。

---

## 2. 目标用户体验

入口：

| 入口 | 行为 |
|---|---|
| Media Pool 右键视频素材 | 对完整素材运行场景剪切探测，结果缓存到素材级分析；完成后可将接受的 cuts 添加为媒体池子片段 |
| Timeline clip 右键 | 只对当前 clip 的 source range 运行探测 |
| Timeline 菜单 | 对选中 clip 或当前播放头所在 clip 运行探测 |
| Scene Detect 弹窗底部右侧按钮 | `Add Cuts to Media Pool`，将接受的检测区间批量创建为虚拟子片段 |
| Review 面板中的 Apply | 将接受的检测点转换为 marker、timeline split 或 media pool subclips |

Resolve-like 结果预览：

1. 弹出或打开 `Scene Cut Detection` 面板。
2. 顶部显示进度、素材名、检测范围、采样策略、命中数量。
3. 中间显示候选列表，每一项包含 source timecode、timeline timecode、score、confidence、reason、状态。
4. 右侧或底部显示剪切点前后帧 A/B 预览。
5. 支持逐个接受/拒绝、全选高置信度、只接受 hard cut、忽略疑似转场、重新检测。
6. 疑似误判以 warning 状态高亮，例如 flash、fast_motion、fade、dissolve、black_gap。
7. 时间线上临时显示检测标记和竖线，高置信度和疑似误判使用不同颜色。
8. 用户确认后可选择：
   - Add Cuts to Media Pool
   - Add Markers Only
   - Split Selected Clip
   - Split Linked Video/Audio
   - Save Analysis Only

硬规则：

- 检测和预览生成必须后台执行。
- UI 只消费进度和结果，不直接解码整段视频。
- 自动切片前必须有 review 或明确确认。
- `Add Cuts to Media Pool` 不生成真实视频文件，创建虚拟子片段。
- 一次 Apply 必须是一个 undo step。
- linked audio/video 必须保持对应关系。

### 2.1 Scene Detect 弹窗页面设计

以下设计根据用户提供的 DaVinci Scene Detect 截图拆解，用于实现高度接近的专业检测窗口。实现时保持布局、信息层级和操作节奏接近，但使用 Topos 自有图标、文字和样式 token。

窗口整体：

| 区域 | 设计规格 |
|---|---|
| Window | 独立深色 `QDialog`，标题居中显示 `Scene Detect`，左上角关闭，右上角三点菜单 |
| 尺寸 | 默认 900x520 左右，允许拉伸；最小尺寸不低于 760x420 |
| 背景 | 深灰黑专业 NLE 风格，分区间用 1px 低对比线 |
| 布局比例 | 上方 preview 约 40%，中间 transport 约 12%，下方 graph/list 约 38%，底部 action bar 约 10% |

顶部三联预览：

| 子区域 | 内容 | 行为 |
|---|---|---|
| 左预览 | 当前候选 cut 前一帧或当前播放点前帧 | 点击候选时更新 |
| 中预览 | 当前候选 cut 点附近帧或过渡中间帧 | 用于观察变化 |
| 右预览 | 当前候选 cut 后一帧 | 点击候选时更新 |
| 分隔 | 三块预览之间用竖向细分割线 | 不使用卡片嵌套 |
| 图像适配 | 保持宽高比，黑边填充 | 不拉伸画面 |

中间播放控制条：

| 控件 | 行为 |
|---|---|
| 跳到上一检测点 | 选中上一候选 |
| 后退/前进单帧 | 当前检测点附近逐帧检查 |
| 播放/暂停 | 在检测范围内预览 |
| 跳到下一检测点 | 选中下一候选 |
| 剪刀按钮 | 手动在当前帧添加 cut |
| 列表按钮 | 显示/隐藏右侧 Scene 列表 |
| 迷你 scrubber | 跟随当前帧，红色或亮色 playhead |

下方检测图：

| 元素 | 设计规格 |
|---|---|
| Graph | 黑底横向图表，占下方左侧约 80% 宽度 |
| 绿色竖线 | 每个候选 cut 的检测峰值，高度表示 score/confidence |
| 紫色水平线 | 当前阈值线，调整 sensitivity 时上下移动 |
| 红色 playhead | 当前选中帧的竖线，上方带小三角 |
| 灰色范围条 | 顶部显示可见范围和缩放窗口 |
| Warning 候选 | flash/transition/fast motion 用橙色或虚线峰值，不默认 accepted |

右侧 Scene 列表：

| 列 | 内容 |
|---|---|
| Scene | 场景序号，从 1 开始，按接受后的片段顺序排列 |
| Frame | cut 所在 source frame |
| Start TC | 子片段起始 timecode |
| 可选列 | Confidence 或 Type，可通过三点菜单显示 |

列表行为：

- 当前行高亮。
- 点击行更新三联预览和 graph playhead。
- Accepted/Pending/Rejected 用图标或低对比状态展示。
- 右侧滚动条窄而低调。
- 默认按 frame 升序。

底部操作栏：

| 位置 | 控件 | 行为 |
|---|---|---|
| 左下 | `Auto Scene Detect` | 开始/重新自动检测 |
| 左下 | `Add` | 在当前 playhead 手动添加 cut |
| 左下 | `Delete` | 删除当前候选 cut |
| 中下 | Sensitivity slider | 调整阈值线，不遮挡图表 |
| 右下 | `Add Cuts to Media Pool` | 将所有 accepted cuts 创建为媒体池虚拟子片段 |

必须实现的交互细节：

- `Auto Scene Detect` 运行时显示进度并可取消。
- `Add` 添加的是候选 cut，不直接创建子片段。
- `Delete` 删除或 reject 当前候选，不修改原素材。
- `Add Cuts to Media Pool` 点击前校验至少有一个 accepted cut。
- 如果全部候选都是 pending，按钮应 disabled 或提示先接受候选。
- 添加完成后弹窗可保持打开，并在媒体池中选中新建的第一条子片段。

---

## 3. 目标架构

建议新增模块：

```text
src/features/edit/core/scene_cut/
  __init__.py
  models.py
  detector.py
  metrics.py
  filters.py
  frame_sampler.py
  cache.py
  timeline_mapping.py
  subclips.py
  apply_ops.py

src/features/edit/ui/
  scene_cut_dialog.py
  scene_cut_result_model.py
  scene_cut_preview_widget.py
  scene_cut_graph_widget.py
  scene_cut_timeline_overlay.py

tests/edit/
  test_scene_cut_models.py
  test_scene_cut_detector.py
  test_scene_cut_filters.py
  test_scene_cut_timeline_mapping.py
  test_scene_cut_subclips.py
  test_scene_cut_apply_ops.py
  test_scene_cut_cache.py

tools/
  generate_scene_cut_fixtures.py
  benchmark_scene_cut_detection.py
```

建议不要把检测逻辑写进 `timeline_view.py` 或 `media_pool_panel.py`。UI 只负责启动任务、展示结果和提交命令；检测和映射保持纯 Python 可测试。

---

## 4. 核心数据模型

建议在 `src/features/edit/core/scene_cut/models.py` 中定义：

```python
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class SceneCutKind(Enum):
    HARD_CUT = "hard_cut"
    SOFT_TRANSITION = "soft_transition"
    BLACK_GAP = "black_gap"
    FLASH = "flash"
    FAST_MOTION = "fast_motion"
    UNKNOWN = "unknown"


class SceneCutDecision(Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True)
class SceneCutSettings:
    sensitivity: float = 0.55
    min_scene_frames: int = 8
    sample_width: int = 256
    max_sample_height: int = 144
    use_proxy: bool = True
    detect_transitions: bool = True
    suppress_flash: bool = True
    suppress_fast_motion: bool = True
    suppress_black_fades: bool = True
    adaptive_threshold: bool = True
    start_source_frame: int = 0
    end_source_frame: int | None = None


@dataclass(frozen=True)
class FrameTimePoint:
    source_frame: int
    pts: int | None
    seconds: float
    duration_seconds: float | None = None
    is_keyframe: bool = False
    is_duplicate_time: bool = False


@dataclass(frozen=True)
class SceneCutMetrics:
    luma_delta: float
    hist_delta: float
    edge_delta: float
    color_delta: float
    black_score: float
    flash_score: float
    motion_score: float
    combined_score: float


@dataclass(frozen=True)
class SceneCutCandidate:
    source_frame: int
    pts: int | None
    seconds: float
    timeline_frame: int | None
    score: float
    confidence: float
    kind: SceneCutKind
    decision: SceneCutDecision = SceneCutDecision.PENDING
    metrics: SceneCutMetrics | None = None
    reasons: tuple[str, ...] = ()
    before_thumb_cache_key: str | None = None
    after_thumb_cache_key: str | None = None


@dataclass(frozen=True)
class SceneCutAnalysis:
    analysis_id: str
    asset_id: str
    source_path: str
    clip_id: str | None
    settings: SceneCutSettings
    media_fingerprint: str
    source_time_index: tuple[FrameTimePoint, ...]
    candidates: tuple[SceneCutCandidate, ...]
    created_at: float
    detector_version: str


@dataclass(frozen=True)
class SceneSubclipSpec:
    parent_asset_id: str
    source_path: str
    scene_index: int
    source_start: int
    source_duration: int
    start_seconds: float
    duration_seconds: float
    display_name: str
    start_timecode: str
    end_timecode: str
    thumbnail_source_frame: int
    detection_confidence: float
    detection_kind: SceneCutKind
```

设计原则：

- 候选点始终记录 `source_frame` 和 `seconds`。
- 如果从 timeline clip 启动，再额外记录 `timeline_frame`。
- VFR 情况下以 `FrameTimePoint` 的 PTS/seconds 为准，再映射到时间线。
- `decision` 属于 review 状态，不能改变检测分数本身。
- `detector_version` 进入缓存 key，算法变更后自动失效旧缓存。
- `SceneSubclipSpec` 是创建媒体池虚拟子片段的中间结果，不直接依赖 UI。
- 虚拟子片段应继续使用原始 `path`，在 `metadata` 中保存 `parent_asset_id`、`source_start`、`source_duration`、`scene_cut_analysis_id`、`scene_index`。

### 4.1 媒体池虚拟子片段模型

现有 `MediaAsset.media_type` 只允许 `image`、`video`、`audio`、`image_sequence`、`comp_clip`。为了实现 DaVinci-like 的 `Add Cuts to Media Pool`，建议第一版采用最小侵入方案：子片段仍然是 `media_type="video"` 的 `MediaAsset`，但通过 `metadata.is_subclip=True` 标识它是原素材的 source range view。

```python
metadata = {
    **parent_asset.metadata,
    "is_subclip": True,
    "parent_asset_id": parent_asset.asset_id,
    "parent_path": parent_asset.path,
    "source_start": spec.source_start,
    "source_duration": spec.source_duration,
    "source_end": spec.source_start + spec.source_duration,
    "start_seconds": spec.start_seconds,
    "duration_seconds": spec.duration_seconds,
    "scene_index": spec.scene_index,
    "scene_cut_analysis_id": analysis.analysis_id,
    "scene_cut_confidence": spec.detection_confidence,
    "scene_cut_kind": spec.detection_kind.value,
}
```

媒体池中的子片段：

| 字段 | 规则 |
|---|---|
| `asset_id` | 新 UUID，保证可独立拖拽和选择 |
| `path` | 与 parent asset 相同，不生成新文件 |
| `media_type` | 仍为 `video`，避免大量下游代码需要识别新类型 |
| `display_name` | 默认 `原素材名 Scene 001`、`原素材名 Scene 002` |
| `metadata.is_subclip` | `True`，用于 UI badge 和 timeline 插入时取 source range |
| `metadata.parent_asset_id` | 指向原素材，便于 relink/proxy/cache 复用 |
| `metadata.source_start/source_duration` | 子片段真实源范围 |
| `thumbnail_key` | 使用子片段起始帧或中间帧 |
| `proxy_path/proxy_status` | 继承 parent proxy 信息或通过 parent 解析 |

不建议第一版新增 `media_type="subclip"`，原因：

- `MediaAsset.__post_init__`、serializer、media pool filter、timeline insert、proxy 逻辑都要扩展。
- 子片段本质仍是 video asset 的 source range view。
- 用 `metadata.is_subclip` 可以快速兼容现有媒体池和时间线拖拽流程。

后续如果要更强类型，可以再引入 `SubclipAssetData` 或 `MediaAsset.kind`，但第一版以落地和兼容为优先。

---

## 5. 检测算法设计

第一阶段采用多指标融合，不使用 AI。

### 5.1 帧采样

| 策略 | 说明 |
|---|---|
| 默认逐帧采样 | 对当前 clip/source range 每帧解码，但每帧缩放到 `256x<=144` |
| 长素材粗扫 + 精扫 | 超过 30 分钟先按 2 帧步长粗扫，命中附近再回查逐帧 |
| 代理优先 | 如果 proxy 存在且时间映射可靠，优先读 proxy |
| keyframe 辅助 | 不以 keyframe 作为 cut 点，但可用于 seek 加速 |
| 预取窗口 | 后台顺序解码，不频繁 random seek |

### 5.2 单帧特征

每个采样帧生成轻量特征：

| 特征 | 用途 |
|---|---|
| resized RGB/BGR | 基础差异 |
| grayscale luma | 亮度变化、黑场 |
| HSV 或 Lab histogram | 色彩分布变化 |
| Sobel/Canny edge map | 构图/边缘变化 |
| mean/std luma | flash、fade、black |
| perceptual hash 可选 | 重复帧/强变化辅助 |

### 5.3 相邻帧指标

候选 score 建议由以下指标融合：

```text
combined =
  0.35 * normalized_luma_mad
+ 0.30 * histogram_chi_square
+ 0.20 * edge_delta
+ 0.10 * color_mean_delta
+ 0.05 * pHash_delta
```

必须做归一化和异常值防护：

- 使用 rolling median / MAD 估计当前片段的动态阈值。
- 不直接固定阈值判断所有素材。
- 对 10-bit/HDR 素材，检测前只做 downsample + normalized luma，不改变原素材。

### 5.4 候选判定

候选点满足：

```text
combined_score > adaptive_threshold
and distance_from_previous_cut >= min_scene_frames
and not suppressed_by_flash_guard
and not suppressed_by_transition_guard
```

阈值建议：

| sensitivity | 行为 |
|---|---|
| 0.35 | 保守，只取强 hard cut |
| 0.55 | 默认，兼顾召回和误判 |
| 0.75 | 激进，更多候选，需要人工 review |

### 5.5 误判抑制规则

| 场景 | 症状 | 处理 |
|---|---|---|
| 闪光 | 单帧或 1-2 帧亮度暴涨，前后构图相似 | 标记 `FLASH`，默认不自动接受 |
| 快速运动 | 连续多帧 edge/luma 都高，不是单点跃迁 | 标记 `FAST_MOTION`，降低 confidence |
| 转场/溶解 | 分数在 6-20 帧内缓慢升降 | 标记 `SOFT_TRANSITION`，不当作 hard cut 自动切 |
| 黑场/淡入淡出 | black_score 高，前后是黑 | 标记 `BLACK_GAP`，需要用户选择是否切 |
| 重复帧/掉帧 | PTS 异常或图像重复 | 不生成 cut，记录 diagnostics |
| 摄影机闪白 | luma 高但 hist/edge 前后相似 | flash guard suppress |

### 5.6 疑似误判高亮

候选结果新增 reasons：

```text
"high_score"
"low_confidence"
"flash_like"
"fast_motion_like"
"transition_like"
"black_gap"
"vfr_time_uncertain"
"proxy_mapping_approx"
```

UI 中：

- `confidence >= 0.85` 默认 accepted 可选。
- `0.55 <= confidence < 0.85` 默认 pending。
- `confidence < 0.55` 默认 pending 且 warning。
- `FLASH`、`FAST_MOTION`、`SOFT_TRANSITION` 默认 pending 或 rejected，不直接 split。

---

## 6. VFR、掉帧和不同 fps 映射

这是实现质量的关键点。不能只用 `frame / fps`。

### 6.1 建立源时间索引

在 `timeline_mapping.py` 中实现：

```python
@dataclass(frozen=True)
class SourceFrameTimeIndex:
    asset_id: str
    media_fingerprint: str
    points: tuple[FrameTimePoint, ...]

    def source_frame_to_seconds(self, source_frame: int) -> float: ...
    def seconds_to_nearest_source_frame(self, seconds: float) -> int: ...
```

来源：

- PyAV frame 的 `pts`
- video stream 的 `time_base`
- frame 的 `time` 或手动 `pts * time_base`
- 缺失 PTS 时回退 Rational fps，并记录 fallback reason

### 6.2 Clip source range 到 timeline frame

对于 timeline clip：

1. 检测返回素材源帧 `candidate.source_frame`。
2. 检查是否在 `clip.source_start <= frame < clip.source_end`。
3. 使用 `ClipInstance.source_to_timeline_frame()` 或 `timeline_frame_mapper` 映射。
4. 如果 clip 有 speed/reverse/fps metadata，优先走已有 mapper。
5. VFR 时以 seconds/PTS 做二次校验，误差超过 0.5 timeline frame 时标记 `vfr_time_uncertain`。

### 6.3 代理映射

代理素材可能帧率、time_base、GOP 与 master 不同。规则：

- 检测可以使用 proxy frame 内容，但结果必须映射回 master source time。
- 如果 proxy 与 master fps 完全一致，可用 frame index 映射。
- 如果不一致，必须用 seconds 映射。
- 如果无法确认代理映射，结果仍可 review，但默认不自动 split。

验收：

- 23.976 source 到 24 fps timeline 的 cut 不漂移超过 1 帧。
- 29.97 source 到 25 fps timeline 的 cut 不漂移超过 1 帧。
- VFR fixture 中候选点使用 PTS 映射，不能只按平均 fps。

---

## 7. 缓存设计

缓存位置建议：

```text
<project cache>/scene_cut/
  analyses/
    <cache_key>.json
  thumbs/
    <cache_key>_<source_frame>_before.jpg
    <cache_key>_<source_frame>_after.jpg
```

cache key 包含：

| 字段 | 原因 |
|---|---|
| source path | 区分文件 |
| file size | 文件变化失效 |
| mtime_ns | 文件变化失效 |
| asset_id | 项目内引用 |
| source range | clip 局部检测 |
| settings hash | 灵敏度/采样大小变化失效 |
| detector_version | 算法变化失效 |
| proxy fingerprint | 代理变化失效 |

缓存策略：

- 检测完整素材时可复用到 clip 局部。
- 检测 clip 局部时只缓存该 source range。
- review decision 可以单独保存为 project state 或 analysis override，避免算法结果被污染。
- 缩略图 LRU，避免长素材占满磁盘。

验收：

- 同一素材同一设置二次打开结果应在 1 秒内加载。
- 修改 sensitivity 后重新计算。
- 替换/Relink 文件后缓存失效。
- 代理重建后缓存失效或标记 proxy changed。

---

## 8. 分阶段实施计划

### P0：架构调研和契约确认

目标：确认检测入口、时间线切分、linked audio、marker 和缓存边界。

| 编号 | 任务 | 文件/范围 | 产物 | 验收 |
|---|---|---|---|---|
| P0-1 | Explore 当前 split/link/marker 调用链 | `timeline_view.py`、`commands.py`、`timeline_ops.py`、`timeline_v2.py` | 影响范围记录 | 明确自动切片应调用的命令入口 |
| P0-2 | 检查 `SplitClipCommand` 是否保留全部 clip 字段 | `commands.py`、`timeline_ops.py` | 字段保留清单 | color/composite/effects/fade/animated params 不丢失 |
| P0-3 | 检查 linked audio split 行为 | `timeline_view.py`、`commands.py` | linked split 策略 | 视频 split 后音频可同步 split |
| P0-4 | 确认 marker API | `TimelineMarker`、timeline update API | marker 写入策略 | 可添加、删除、撤销检测 markers |
| P0-5 | 确认 project cache 路径 | `src/shared/paths/`、project IO | cache 路径约定 | cache 不污染源码目录 |

验证方法：

```bash
rg -n "class SplitClipCommand|MacroCommand|TimelineMarker|with_clip_updated|linked_clip_id" src/features/edit
pytest -q tests/edit -k "split or timeline or marker"
```

### P1：检测核心纯逻辑

目标：实现无 UI、可单测的检测器。

| 编号 | 任务 | 文件 | 实现要点 | 验收 |
|---|---|---|---|---|
| P1-1 | 新增 scene_cut 包和 models | `scene_cut/models.py` | 定义 settings/candidate/analysis/metrics | dataclass 单测通过 |
| P1-2 | 实现 frame sampler | `scene_cut/frame_sampler.py` | PyAV 顺序解码、downsample、PTS 记录 | 可读取 fixture 并输出 frame/time |
| P1-3 | 实现 metrics | `scene_cut/metrics.py` | luma/hist/edge/color/black/flash/motion | 合成帧差异分数符合预期 |
| P1-4 | 实现 filters | `scene_cut/filters.py` | adaptive threshold、min distance、flash/transition guard | flash fixture 不误判 hard cut |
| P1-5 | 实现 detector | `scene_cut/detector.py` | 串联 sampler/metrics/filters | hard cut fixture 检出准确 |
| P1-6 | 增加 diagnostics | `SceneCutAnalysis` | 记录 decode path、fallback reason、frame count | 异常可定位 |

建议 fixtures：

| Fixture | 内容 | 预期 |
|---|---|---|
| hard_cut_24fps.mp4 | 红/蓝/绿纯色镜头切换 | 精确检出 cut |
| flash_no_cut.mp4 | 同一画面中间白闪 1 帧 | 标记 flash，不默认切 |
| fast_pan.mp4 | 连续快速运动 | 低 confidence 或 suppress |
| dissolve.mp4 | 12 帧溶解 | 标记 soft transition |
| black_gap.mp4 | 黑场间隔 | 标记 black gap |
| vfr_cut.mp4 | 不等 duration PTS | PTS 映射正确 |

验证方法：

```bash
pytest -q tests/edit/test_scene_cut_models.py
pytest -q tests/edit/test_scene_cut_detector.py
pytest -q tests/edit/test_scene_cut_filters.py
```

### P2：后台 worker、进度和取消

目标：检测任务不阻塞 UI，可取消，可显示进度。

| 编号 | 任务 | 文件 | 实现要点 | 验收 |
|---|---|---|---|---|
| P2-1 | 新增 worker | `scene_cut/detector.py` 或 `scene_cut/worker.py` | QThread/QRunnable 包装纯检测器 | UI 线程不阻塞 |
| P2-2 | 进度模型 | `models.py` | frame/current/total/phase/message | 每秒最多节流更新 10 次 |
| P2-3 | 取消 token | worker | 检测循环检查 cancel | 取消后资源释放 |
| P2-4 | 错误边界 | worker | PyAV/OpenCV 异常转为用户可读错误 | 不崩溃 |
| P2-5 | 并发限制 | controller/service | 同一 asset 同一设置不重复跑 | 重复启动复用任务或提示 |

验证方法：

```bash
pytest -q tests/edit/test_scene_cut_worker.py
python tools/benchmark_scene_cut_detection.py --fixture tests/fixtures/hard_cut_24fps.mp4 --cancel-after 1
```

手动验收：

- 运行检测时仍可拖动窗口、切换面板。
- Cancel 后 1 秒内停止进度。
- 关闭项目或窗口时 worker 不悬挂。

### P3：缓存、代理和长素材性能

目标：长素材可用，重复检测快速，优先使用代理加速。

| 编号 | 任务 | 文件 | 实现要点 | 验收 |
|---|---|---|---|---|
| P3-1 | 新增 cache key | `scene_cut/cache.py` | path/size/mtime/settings/version/proxy | 同设置命中缓存 |
| P3-2 | analysis JSON 存取 | `cache.py` | 结构化序列化，不 pickle | roundtrip 单测 |
| P3-3 | thumb cache | `cache.py`、UI preview | before/after 小图缓存 | review 打开不卡 |
| P3-4 | proxy resolver | `frame_sampler.py` | 优先使用 proxy，结果映射 master | proxy 检测结果可 split master clip |
| P3-5 | 长素材分块 | `detector.py` | chunk 级处理，释放临时帧 | 1 小时素材内存稳定 |
| P3-6 | benchmark 工具 | `tools/benchmark_scene_cut_detection.py` | 输出 fps、内存、命中数、缓存状态 | 可量化优化 |

性能目标：

| 场景 | 目标 |
|---|---|
| 1080p 10 分钟 H.264，有代理 | 检测速度 >= 8x realtime |
| 4K 10 分钟 H.264，无代理 | 检测速度 >= 2x realtime |
| 1 小时素材 | 内存增长 < 300 MB |
| 二次打开同缓存结果 | < 1 秒 |

验证方法：

```bash
pytest -q tests/edit/test_scene_cut_cache.py
python tools/benchmark_scene_cut_detection.py --media /path/to/sample.mp4 --use-proxy --json
```

### P4：结果 Review UI

目标：实现接近 Resolve 的检测结果预览和逐个接受/拒绝体验。

| 编号 | 任务 | 文件 | 实现要点 | 验收 |
|---|---|---|---|---|
| P4-1 | 新增 dialog/panel | `scene_cut_dialog.py` | QDialog 或 dock panel | 可从 clip/media pool 打开 |
| P4-2 | 结果 model | `scene_cut_result_model.py` | QAbstractTableModel，支持 decision edit | 列表状态稳定 |
| P4-3 | A/B preview | `scene_cut_preview_widget.py` | before/after thumb、timecode、score | 点击候选切换预览 |
| P4-4 | 批量操作 | dialog | accept high confidence/reject warnings/reset | 状态正确 |
| P4-5 | sensitivity rerun | dialog/controller | 修改设置重新检测，保留可匹配 decision 可选 | 不混淆旧结果 |
| P4-6 | keyboard flow | dialog | Enter accept、Delete reject、Up/Down 导航 | review 快速 |
| P4-7 | i18n | locale JSON | 中英文 key | 无硬编码中文/英文 |

UI 状态建议：

| 状态 | 颜色/语义 |
|---|---|
| accepted hard cut | 高亮确认色 |
| pending | 中性色 |
| rejected | 低对比/删除线 |
| warning | 橙色/黄色警示 |
| cached | 小型 cache indicator |

验证方法：

```bash
pytest -q tests/edit/test_scene_cut_result_model.py
python tools/capture_edit_page.py --scenario scene-cut-review
```

手动验收：

- 点击候选时，source viewer 或预览区显示 cut 前后帧。
- 连续按键 review 100 个候选不卡顿。
- 关闭再打开，decision 保留。

### P5：时间线 marker、overlay 和疑似误判高亮

目标：检测结果在时间线可见，但未确认前不破坏 timeline 数据。

| 编号 | 任务 | 文件 | 实现要点 | 验收 |
|---|---|---|---|---|
| P5-1 | 临时 overlay | `scene_cut_timeline_overlay.py`、`timeline_view.py` | 竖线/小标记，不写入 timeline | review 时可见 |
| P5-2 | marker 写入命令 | `apply_ops.py`、`commands.py` | accepted candidates -> TimelineMarker | 可撤销 |
| P5-3 | warning marker 样式 | overlay/items | flash/transition/black 不同样式 | 疑似误判清楚 |
| P5-4 | 与 zoom/scroll 同步 | timeline overlay | frame_to_x 使用 layout engine | 滚动缩放不漂移 |
| P5-5 | 清理生命周期 | timeline reload/project close | 移除临时 overlay | 不残留 |

验证方法：

```bash
pytest -q tests/edit/test_scene_cut_timeline_mapping.py
pytest -q tests/edit/test_scene_cut_markers.py
python tools/capture_edit_page.py --scenario scene-cut-overlay
```

### P6：Add Cuts to Media Pool 和虚拟子片段

目标：实现截图中右下角 `Add Cuts to Media Pool` 的主工作流。用户在 Scene Detect 弹窗中完成检测和 review 后，点击该按钮，所有 accepted cuts 生成一组媒体池虚拟子片段。子片段不写新视频文件，只引用原素材并保存 source range。

| 编号 | 任务 | 文件 | 实现要点 | 验收 |
|---|---|---|---|---|
| P6-1 | cut 点转 scene ranges | `scene_cut/subclips.py` | accepted cut frames -> 连续 scene ranges | N 个 cut 生成 N+1 个 scene |
| P6-2 | 子片段 spec 生成 | `subclips.py` | `SceneSubclipSpec` 包含 start/duration/timecode/name/thumb frame | spec 单测通过 |
| P6-3 | 扩展 project 添加 subclip | `project.py` | 新增 `import_subclip()` 或 `with_subclips_added()` 纯函数 | 不重复导入 parent 文件 |
| P6-4 | 媒体池 bin 插入 | `project.py`、`media_pool_panel.py` | 子片段加入当前 bin 或 parent asset 所在 bin | 媒体池立即显示 |
| P6-5 | 子片段缩略图 | thumbnail provider/cache | 使用 scene 起始帧或中间帧 | 每个 scene 有独立缩略图 |
| P6-6 | 拖入时间线 source range | `timeline_view.py`、insert command | 子片段拖入时使用 metadata.source_start/source_duration | 时间线 clip 只使用该 scene 范围 |
| P6-7 | 子片段 UI badge | `media_pool_panel.py` | 小型 `Scene`/`Subclip` 标识，不影响普通素材 | 用户能区分原素材和子片段 |
| P6-8 | 命名规则 | `subclips.py`、dialog | `ClipName Scene 001`，支持前缀/起始序号 | 名称稳定且不冲突 |
| P6-9 | 重复添加策略 | dialog/controller | 提示 Replace Existing / Append / Cancel | 不无声创建重复片段 |
| P6-10 | 撤销/清理策略 | project command 或 service | Add Cuts 是一个 project mutation，可删除本次生成的 subclips | 误操作可恢复 |

scene range 规则：

```text
输入：
  source_start = 0
  source_end = 1000
  accepted cuts = [100, 260, 700]

输出：
  Scene 001: [0, 100)
  Scene 002: [100, 260)
  Scene 003: [260, 700)
  Scene 004: [700, 1000)
```

如果从 timeline clip 启动：

- 子片段范围只覆盖当前 clip 的 `source_start/source_duration`。
- 子片段仍引用 parent asset 的 path。
- display name 可包含 timeline clip 名称。
- 如果 clip 有 speed/reverse，不建议第一版直接生成媒体池子片段，应提示“Scene cuts are based on original source range”，或仅对 source range 生成正常速度子片段。

添加到媒体池后的行为：

| 操作 | 期望 |
|---|---|
| 双击子片段 | Source Viewer 打开对应 source range |
| 拖入时间线 | 创建 `ClipInstance(source_start=子片段 source_start, source_duration=子片段 source_duration)` |
| 生成代理 | 复用 parent asset proxy，不重复生成完整代理 |
| Relink parent | 子片段跟随 parent path 或通过相同 path 重新 online |
| 删除子片段 | 只删除媒体池条目，不影响原素材和其他子片段 |
| 重新检测并 Replace | 删除旧 analysis_id 对应子片段，创建新一批 |

验证方法：

```bash
pytest -q tests/edit/test_scene_cut_subclips.py
pytest -q tests/edit/test_scene_cut_project_serialization.py
pytest -q tests/edit/test_scene_cut_media_pool_integration.py
```

手动验收：

- 在 Media Pool 原素材右键打开 Scene Detect。
- 点击 `Auto Scene Detect` 后接受 3 个 cut。
- 点击 `Add Cuts to Media Pool`。
- 媒体池中出现 4 个 `Scene 001-004` 子片段。
- 每个子片段拖入时间线后 source range 正确。
- 删除子片段不删除原素材。
- 保存项目再打开，子片段仍存在且可拖入时间线。

### P7：自动切片和 linked audio/video

目标：将 accepted candidates 安全转换为 timeline split。

| 编号 | 任务 | 文件 | 实现要点 | 验收 |
|---|---|---|---|---|
| P6-1 | apply ops 纯函数 | `scene_cut/apply_ops.py` | 输入 timeline/clip/candidates，输出 commands | 单测可覆盖 |
| P6-2 | split points 去重排序 | `apply_ops.py` | 去除 clip 外、重复、边界点 | 不生成非法 split |
| P6-3 | 从右到左 split | `apply_ops.py` | 避免前一次 split 改变后续 clip_id/source range | 多点切分正确 |
| P6-4 | linked audio split | `apply_ops.py`、commands | 视频和 linked audio 同一 macro | 一次撤销还原 |
| P6-5 | 保留 clip 属性 | `timeline_ops.py`/`commands.py` | effects/color/composite/fade/animated params | 切后属性不丢 |
| P6-6 | 冲突检测 | `apply_ops.py` | locked track、disabled clip、overlap、transition | 先报告，不半提交 |
| P6-7 | MacroCommand | `commands.py` | Apply Scene Cuts 一个 undo step | undo/redo 正确 |

关键规则：

- 分割点必须满足 `clip.timeline_start < frame < clip.timeline_end`。
- 不对 rejected/pending 自动切。
- transition-like 默认不切，除非用户明确接受。
- linked audio 如果 source/time mapping 不确定，应提示并允许只切视频或取消。

验证方法：

```bash
pytest -q tests/edit/test_scene_cut_apply_ops.py
pytest -q tests/edit -k "split or linked"
```

手动验收：

- 对一个 10 个 hard cut 的长 clip apply，时间线变成 11 个 clip。
- Undo 一次恢复成原始单 clip。
- linked audio 同步切成同数量片段，且时间线位置对齐。

### P8：VFR、掉帧、不同 fps 和复杂误判专项

目标：把专业场景中最容易翻车的素材类型变成明确测试。

| 编号 | 任务 | 场景 | 验收 |
|---|---|---|---|
| P7-1 | VFR fixture | 不等 PTS duration | cut timeline frame 误差 <= 1 |
| P7-2 | 掉帧/重复帧 | PTS gap、duplicate frame | 不误判 repeated frame 为 cut |
| P7-3 | 23.976 -> 24 | source/timeline fps 不同 | split 位置正确 |
| P7-4 | 29.97 -> 25 | NTSC 到 PAL timeline | split 位置正确 |
| P7-5 | 速度变化 clip | speed != 1.0 | source_to_timeline mapping 正确 |
| P7-6 | reverse clip | speed reverse 或 SpeedParams reverse | 明确支持或禁用并提示 |
| P7-7 | dissolve | soft transition | 标记 transition，不默认 hard cut |
| P7-8 | black fade | 淡出黑/淡入黑 | 标记 black/fade，不默认乱切 |
| P7-9 | flash | 单帧闪白/闪黑 | suppress 或 low confidence |
| P7-10 | action fast motion | 大幅运动无剪辑 | 降低 confidence |

验证方法：

```bash
pytest -q tests/edit/test_scene_cut_vfr_mapping.py
pytest -q tests/edit/test_scene_cut_false_positive_guards.py
```

### P9：项目集成、菜单和持久化

目标：功能进入真实工作流。

| 编号 | 任务 | 文件 | 验收 |
|---|---|---|---|
| P9-1 | Edit 模块菜单贡献 | `src/features/edit/module.py` | 菜单出现 Scene Cut Detection |
| P9-2 | Timeline clip 右键入口 | `timeline_view.py` 或 context menu 模块 | 选中 clip 可启动 |
| P9-3 | Media Pool 右键入口 | `media_pool_panel.py` | 视频素材可启动 |
| P9-4 | Controller/service | `edit_controller.py` 或 feature service | 统一启动 worker 和 dialog |
| P9-5 | Project 持久化 decision | project serializer | review 状态可保存 |
| P9-6 | Project 持久化 subclips | project serializer | 保存/加载后子片段保留 |
| P9-7 | i18n | locale JSON | 中英文 UI 正常 |
| P9-8 | 偏好设置 | preferences | 默认 sensitivity/use_proxy/min_scene_frames 可配置 |

验收方法：

```bash
pytest -q tests/edit/test_menu_contributions.py
pytest -q tests/edit/test_scene_cut_project_serialization.py
python tools/verify_i18n.py
```

### P10：截图、性能和回归验收

目标：可视化和性能都有证据。

| 编号 | 任务 | 文件 | 验收 |
|---|---|---|---|
| P10-1 | 截图场景 | `tools/capture_edit_page.py` | scene-cut-review/overlay/add-to-media-pool/apply-before-after |
| P10-2 | 性能 benchmark | `tools/benchmark_scene_cut_detection.py` | 输出 JSON 可比较 |
| P10-3 | 内存监控 | benchmark | 长素材内存稳定 |
| P10-4 | 热路径检查 | tests | UI 线程不解码、不阻塞 |
| P10-5 | 回归矩阵 | docs/test_acceptance_matrix | 加入 scene cut |

完整验收命令：

```bash
pytest -q \
  tests/edit/test_scene_cut_models.py \
  tests/edit/test_scene_cut_detector.py \
  tests/edit/test_scene_cut_filters.py \
  tests/edit/test_scene_cut_cache.py \
  tests/edit/test_scene_cut_timeline_mapping.py \
  tests/edit/test_scene_cut_subclips.py \
  tests/edit/test_scene_cut_apply_ops.py \
  tests/edit/test_scene_cut_result_model.py \
  tests/edit/test_scene_cut_false_positive_guards.py \
  tests/edit/test_scene_cut_vfr_mapping.py

python tools/benchmark_scene_cut_detection.py --fixture tests/fixtures/hard_cut_24fps.mp4 --json
python tools/capture_edit_page.py --scenario scene-cut-review
python tools/capture_edit_page.py --scenario scene-cut-overlay
python tools/capture_edit_page.py --scenario scene-cut-add-to-media-pool
```

---

## 9. 验收矩阵

| 验收项 | 验证方式 | 通过条件 |
|---|---|---|
| Hard cut 准确率 | synthetic fixtures | precision >= 0.95，recall >= 0.95 |
| Flash 抑制 | flash fixture | 不默认 accepted 为 hard cut |
| 快速运动抑制 | fast motion fixture | warning 或 confidence 降低 |
| 转场识别 | dissolve fixture | 标记 `SOFT_TRANSITION` |
| 黑场处理 | black gap fixture | 标记 `BLACK_GAP`，不乱切 |
| VFR 映射 | VFR fixture | timeline frame 误差 <= 1 |
| fps 映射 | 23.976/29.97 fixtures | split 位置误差 <= 1 |
| 代理加速 | proxy fixture | 使用 proxy 时结果映射 master |
| 长素材性能 | benchmark | 内存增长 < 300 MB，速度达标 |
| 缓存 | 二次检测 | 同设置 < 1 秒加载 |
| UI 非阻塞 | 手动 + worker test | 检测时 UI 可操作 |
| Review 体验 | 手动 + screenshot | 可逐个接受/拒绝，A/B 预览正确 |
| Add Cuts to Media Pool | manual + unit | accepted cuts 生成 N+1 个媒体池虚拟子片段 |
| 子片段拖入时间线 | manual + unit | timeline clip 使用 subclip source_start/source_duration |
| 子片段持久化 | serialization test | 保存/打开项目后 subclips 完整保留 |
| Marker | unit + manual | accepted candidates 可写 marker 且可撤销 |
| Split | unit + manual | 一次 Apply，一次 Undo 全部恢复 |
| linked audio | unit + manual | 视频/音频切点对齐 |
| 属性保留 | unit | split 后 color/composite/effects/fade 不丢 |

---

## 10. 风险和规避

| 风险 | 严重性 | 规避 |
|---|---|---|
| 误判过多导致用户不信任 | 高 | 默认保守，warning 不自动接受，提供 review |
| VFR 映射漂移 | 高 | 建 PTS index，测试 VFR fixture |
| 自动切片破坏 linked audio | 高 | apply 前做 linked validation，MacroCommand 原子提交 |
| 子片段重复创建污染媒体池 | 中高 | Add 时提供 Replace/Append/Cancel，analysis_id 可追踪 |
| 子片段 source range 拖入时间线失效 | 高 | timeline insert 必须优先读取 `metadata.is_subclip` range |
| split 丢失 clip 属性 | 高 | 字段保留单测，必要时修 `split_clip` |
| 长素材检测卡死 UI | 高 | 后台 worker + cancel + chunk |
| 缓存错误复用 | 中高 | fingerprint/settings/version/proxy 全进入 key |
| 代理映射不准 | 中高 | proxy mismatch 标记 warning，不默认 auto split |
| 结果 overlay 与 timeline 缩放漂移 | 中 | 使用统一 layout/frame_to_x |
| i18n/菜单硬编码 | 中 | locale keys + verify_i18n |

---

## 11. 推荐实施顺序

| 顺序 | 阶段 | 原因 |
|---|---|---|
| 1 | P0 | 先确认 split/link/marker 不会破坏现有时间线 |
| 2 | P1 | 先做纯检测逻辑，避免 UI 和算法混在一起 |
| 3 | P8 的 fixture 生成 | 早做复杂素材测试，避免后期返工 |
| 4 | P2 | 检测变成可取消后台任务 |
| 5 | P3 | 长素材和缓存先稳定，UI 才有意义 |
| 6 | P4 | Review UI 接入真实结果 |
| 7 | P5 | 时间线 overlay/marker |
| 8 | P6 | Add Cuts to Media Pool 是主工作流，先于 timeline split 落地 |
| 9 | P7 | 自动切片，风险最高，放在 subclip 和 mapping 稳定后 |
| 10 | P9/P10 | 菜单、持久化、截图、性能验收收尾 |

---

## 12. AI 接力 Prompt 模板

### Prompt 1：Explore 架构影响

```text
使用 Explore agent（thoroughness: very thorough）分析 Topos Color/Edit 场景剪切探测功能的集成点。

项目背景：
- PyQt6 + ModernGL + FFmpeg/PyAV + OpenCV
- Timeline 数据模型在 src/features/edit/core/timeline_v2.py
- 时间线命令在 src/features/edit/core/commands.py 和 timeline_ops.py
- UI 在 src/features/edit/ui/
- 必须遵循 .clinerules：强类型、UI 不阻塞、信号防递归、OpenGL 不在 UI 线程调用

任务：
1. 梳理 split clip、linked audio、marker、undo/redo 的调用链。
2. 找出自动应用 scene cut 时必须保护的 clip 字段。
3. 找出 Media Pool 右键、Timeline clip 右键、Edit 菜单的入口文件。
4. 输出建议新增模块和影响范围。

输出：
1. 文件级影响表
2. 数据流图（文本）
3. 风险清单
4. 第一阶段实现建议
```

### Prompt 2：实现纯检测核心

```text
在 Topos Color/Edit 中实现非 AI 的 scene cut detection 纯核心。

要求：
- 不写 UI。
- 不阻塞 UI 线程。
- 不调用 ModernGL/OpenGL。
- 所有函数完整类型注解。
- 使用 PyAV/FFmpeg 读取帧，OpenCV/Numpy 计算差异。
- 输出 SceneCutAnalysis 和 SceneCutCandidate。
- 支持 hard cut、flash、fast_motion、soft_transition、black_gap 标记。

参考计划：
- docs/resolve_like_scene_cut_detection_implementation_plan_2026-06-04.md

建议文件：
- src/features/edit/core/scene_cut/models.py
- src/features/edit/core/scene_cut/frame_sampler.py
- src/features/edit/core/scene_cut/metrics.py
- src/features/edit/core/scene_cut/filters.py
- src/features/edit/core/scene_cut/detector.py
- tests/edit/test_scene_cut_*.py

验收：
- synthetic hard cut fixture 能检出精确 cut。
- flash fixture 不默认 hard cut。
- dissolve fixture 标记 soft transition。
```

### Prompt 3：实现缓存和 VFR 映射

```text
为 Topos Color/Edit scene cut detection 实现缓存和时间映射。

要求：
- 缓存 key 包含 path、size、mtime_ns、asset_id、source range、settings hash、detector_version、proxy fingerprint。
- 使用 JSON 序列化 analysis，不使用 pickle。
- 建立 SourceFrameTimeIndex，优先使用 PyAV PTS/time_base。
- VFR 和不同 source/timeline fps 映射误差不能超过 1 帧。
- proxy 检测结果必须映射回 master source time。

文件：
- src/features/edit/core/scene_cut/cache.py
- src/features/edit/core/scene_cut/timeline_mapping.py
- tests/edit/test_scene_cut_cache.py
- tests/edit/test_scene_cut_vfr_mapping.py
```

### Prompt 4：实现 Review UI

```text
在 Topos Color/Edit 中实现 Scene Cut Detection review UI。

要求：
- PyQt6 UI，遵循 .clinerules：信号防递归、强类型、UI 不直接执行重检测 heavy work。
- 页面布局参考 docs/resolve_like_scene_cut_detection_implementation_plan_2026-06-04.md 的“Scene Detect 弹窗页面设计”。
- 顶部三联预览：cut 前帧、当前/中间帧、cut 后帧。
- 中间 transport bar：上一检测点、逐帧后退、播放/暂停、逐帧前进、下一检测点、剪刀、列表切换。
- 下方左侧 graph：黑底、绿色检测峰值、紫色阈值线、红色 playhead。
- 下方右侧 Scene 列表：Scene、Frame、Start TC。
- 底部左侧按钮：Auto Scene Detect、Add、Delete。
- 底部中间 sensitivity slider。
- 底部右侧主按钮：Add Cuts to Media Pool。
- 结果列表支持 accepted/rejected/pending。
- 点击候选显示三联 preview。
- 支持批量接受高置信度、拒绝 warnings、重新检测。
- 支持 i18n：en_US 和 zh_CN。
- 不在 UI 线程解码整段视频；缩略图来自缓存或后台生成。

文件：
- src/features/edit/ui/scene_cut_dialog.py
- src/features/edit/ui/scene_cut_result_model.py
- src/features/edit/ui/scene_cut_preview_widget.py
- src/features/edit/ui/scene_cut_graph_widget.py
- src/features/edit/locales/en_US.json
- src/features/edit/locales/zh_CN.json
- tests/edit/test_scene_cut_result_model.py
```

### Prompt 5：实现 Add Cuts to Media Pool

```text
实现 Topos Color/Edit Scene Detect 弹窗中的 Add Cuts to Media Pool 主工作流。

要求：
- 用户在 Media Pool 原素材右键启动 Scene Cut Detection。
- 弹窗 review 完成后，点击 Add Cuts to Media Pool。
- 所有 accepted cuts 转换为 N+1 个虚拟子片段。
- 子片段作为新的 MediaAsset 出现在媒体池中。
- 不生成真实视频文件，子片段 path 与 parent asset 相同。
- 子片段 media_type 仍为 video，用 metadata.is_subclip=True 标识。
- metadata 必须包含 parent_asset_id、source_start、source_duration、scene_index、scene_cut_analysis_id。
- 子片段拖入 timeline 时必须使用 metadata.source_start/source_duration。
- 保存项目再打开后，子片段仍存在且可拖入时间线。
- 重复添加同一 analysis 时提供 Replace/Append/Cancel 策略。

文件：
- src/features/edit/core/scene_cut/subclips.py
- src/features/edit/core/project.py
- src/features/edit/core/project_serializer.py
- src/features/edit/core/serialization.py
- src/features/edit/ui/media_pool_panel.py
- src/features/edit/ui/scene_cut_dialog.py
- tests/edit/test_scene_cut_subclips.py
- tests/edit/test_scene_cut_project_serialization.py
- tests/edit/test_scene_cut_media_pool_integration.py
```

### Prompt 6：实现自动 marker 和 split apply

```text
实现 Topos Color/Edit scene cut detection 的 Apply 操作。

要求：
- Accepted candidates 可以写入 TimelineMarker。
- Accepted hard cuts 可以自动 split selected clip。
- linked audio/video 必须同步 split。
- Apply 必须是一个 MacroCommand，一次撤销恢复全部。
- 不处理 rejected/pending。
- 对 locked track、clip 外边界、重复 cut、transition-like candidate 做保护。
- split 后必须保留 clip 的 effects、color_stack_id、composite_id、fade、animated_params、render_cache_id 等字段。

文件：
- src/features/edit/core/scene_cut/apply_ops.py
- src/features/edit/core/timeline_ops.py
- src/features/edit/core/commands.py
- tests/edit/test_scene_cut_apply_ops.py
```

### Prompt 7：Review 审查

```text
使用 Review skill 审查 scene cut detection 相关变更。

重点：
1. UI 线程是否可能阻塞。
2. PyQt 信号是否有防递归。
3. worker 生命周期和取消是否安全。
4. split 是否丢失 clip 字段。
5. linked audio/video 是否保持一致。
6. VFR/fps 映射是否可能漂移。
7. 缓存 key 是否可能错误复用旧结果。
8. 是否引入 OpenGL/ModernGL UI 线程调用。

文件：
- src/features/edit/core/scene_cut/
- src/features/edit/ui/scene_cut_*.py
- src/features/edit/core/commands.py
- src/features/edit/core/timeline_ops.py
```

---

## 13. Definition Of Done

功能完成必须同时满足：

1. Media Pool 和 Timeline clip 都能启动 Scene Cut Detection。
2. Media Pool 右键素材可打开独立 Scene Detect 弹窗。
3. Scene Detect 弹窗具备三联预览、transport bar、检测 graph、Scene/Frame/Start TC 列表、Auto Scene Detect/Add/Delete/Add Cuts to Media Pool。
4. 检测后台执行，支持进度、取消、错误提示。
5. 检测结果可逐个预览、接受、拒绝。
6. 疑似 flash、fast motion、transition、black gap 会高亮并默认不自动切。
7. 点击 `Add Cuts to Media Pool` 后，accepted cuts 生成 N+1 个媒体池虚拟子片段。
8. 子片段不生成真实视频文件，引用 parent asset path，并保存 source_start/source_duration。
9. 子片段可拖入时间线，timeline clip 使用正确 source range。
10. 子片段保存项目后可恢复。
11. 检测结果可写入 markers。
12. accepted hard cuts 可一次性 split clip。
13. linked audio/video 可同步切分。
14. Apply 是一个 undo step。
15. VFR、掉帧、23.976/29.97 到不同 timeline fps 的映射误差 <= 1 帧。
16. 同素材同设置二次打开使用缓存。
17. 长素材检测内存稳定，UI 不阻塞。
18. 所有新增核心函数完整类型注解。
19. 中英文 i18n 检查通过。
20. 相关 unit tests、截图验收、benchmark 均有记录。

---

## 14. 后续可选增强

第一版非 AI 达标后，可以考虑：

- 引入 TransNetV2/ONNX 作为可选高级检测模式。
- 自动根据误判 review 结果调整 per-project sensitivity。
- 在 timeline filmstrip 上直接显示 cut confidence heatmap。
- 支持批量检测多个素材并生成 bins。
- 支持 EDL/XML 导入已有 scene cut markers。
- 支持导出检测结果 JSON/CSV。
