# Topos Color 时间线多轨遮挡裁剪优化计划

> 状态：O0–O8 已实施（2026-09-19），实施记录与基准结果见 `docs/timeline_occlusion_culling_implementation_2026-09-19.md`
>
> 建立日期：2026-09-19
>
> 目标：当上层视频已经完全不透明地覆盖画布时，避免解码、处理和合成被遮挡的下层视频；当上层缩放、移动或存在透明区域时，继续保留下层计算，确保预览与导出结果一致。

## 1. 结论摘要

用户提出的渲染模型是正确的，但当前代码尚未实现逐帧遮挡裁剪：

| 路径 | 当前行为 | 关键位置 |
|---|---|---|
| Edit CPU 预览 | 从下到上遍历所有可见图层，并请求每层帧 | `src/features/edit/core/timeline_preview_renderer.py` |
| Edit 导出 CPU 合成 | 对 `sorted_layers` 的每层执行取帧、调色、变换和合成 | `src/features/edit/core/timeline_export.py` 的 `TimelineExporter._composite_layers()` |
| Edit GPU 预览/导出 | 将所有可用图层作为 stack element 提交给 GPU compositor | `src/features/edit/core/edit_gpu_preview_renderer.py`、`src/features/edit/core/timeline_gpu_export_renderer.py`、`src/features/edit/core/timeline_metal_export_renderer.py` |
| Render Plan | 返回所有当前可见图层，没有输出遮挡分析结果 | `src/features/edit/core/timeline_render_plan.py` |

因此，当前即使上轨素材完全覆盖下轨，下轨通常仍会被请求和计算。此前修复的“上轨缩放后导出黑底”属于 Alpha 语义问题；本计划解决的是下一步的性能优化，两者必须分开验证。

## 2. 优化目标与非目标

### 2.1 目标

1. 对每个时间线帧，在请求视频帧之前完成保守的可见性分析。
2. 完全被确定性不透明上层覆盖的下层，不解码、不加载、不进入 GPU/CPU 合成队列。
3. 上层缩放、移动、旋转、裁剪、透明度变化、Alpha、转场或特殊混合模式出现时，不能错误裁掉下层。
4. 预览、CPU 回退、GPU 导出和批量导出使用同一份裁剪语义。
5. 通过 telemetry 证明减少的是实际解码/渲染工作，而不是只减少 Python 循环次数。

### 2.2 非目标

- 第一阶段不做复杂的任意形状遮罩求交，也不为了裁剪而引入 CPU 像素读回。
- 第一阶段不改变时间线的图层顺序、Alpha 合成、转场和混合模式语义。
- 第一阶段不把“GPU 使用率升高”当成成功标准；必须保留帧数、解码请求和耗时证据。
- 第一阶段不做素材级永久缓存；遮挡结果必须随当前帧、关键帧和转场状态变化。

## 3. 安全裁剪规则

### 3.1 可以作为“全画布遮挡层”的必要条件

只有同时满足以下条件，才允许停止计算它下面的图层：

| 条件 | 要求 | 原因 |
|---|---|---|
| 图层存在 | clip、asset、源帧计划有效且当前帧可见 | 无效层不能作为遮挡证据 |
| 时间状态 | clip 启用，未处于淡入/淡出或转场混合阶段 | 有效 opacity 可能小于 1 |
| 不透明度 | 当前帧有效 opacity `>= 1.0 - epsilon` | 否则下层仍会透出 |
| 源 Alpha | 源素材明确无 Alpha，或当前帧已证明 Alpha 满刻度 | 未知 Alpha 必须按保守策略保留下层 |
| 几何 | 变换为恒等，或变换后的覆盖区域完整覆盖输出画布 | 缩小、平移、旋转通常会露出下层 |
| Crop/Mask | 无裁剪、羽化、蒙版或其他透明边界 | 边界可能露出下层 |
| 混合模式 | `normal` | Multiply、Screen、Overlay 等需要下层像素 |
| 转场 | 不参与任何仍有效的转场 | 转场需要一侧或两侧的下层内容 |
| 效果 | 第一阶段只允许无视频效果；后续效果必须声明“不依赖背景”且有覆盖测试 | Blur、Glow、插件等可能改变覆盖范围或读取背景 |
| 图层类型 | 普通 video/image，不能是 adjustment、nested sequence 或未声明语义的 Composite Clip | 这些类型可能在渲染时产生额外内容或依赖上下文 |

> `epsilon` 建议统一为 `1e-6`，并在共享模块中定义，避免预览和导出各自使用不同阈值。

### 3.2 必须保留下层的情况

出现以下任一情况，当前阶段都应 fail-closed，即不做遮挡裁剪：

| 情况 | 处理 |
|---|---|
| 上层素材缩放小于 1、平移、旋转或自定义 Pivot | 保留下层；变换后的透明区域必须正常 over 到下层 |
| 上层有 Crop、Feather、Mask 或 Alpha | 保留下层 |
| opacity 小于 1，包含关键帧、淡入淡出和转场期间 | 保留下层 |
| 非 Normal blend mode | 保留下层 |
| 上层素材 Alpha/像素格式未知 | 保留下层 |
| adjustment clip | 保留下层，并按既有 post-composite 语义处理 |
| Cross Dissolve、Wipe、Dip To Color 或用户转场 | 保留转场涉及的全部图层 |
| 需要背景的效果或插件 | 保留下层；除非后续能力声明明确证明安全 |
| 当前帧的 Render Plan 或几何数据异常 | 保留下层，并记录诊断原因 |

### 3.3 “放大但仍覆盖全画布”的处理

缩放不等于一定要保留下层：如果上层放大后经过平移/旋转计算，实际覆盖区域仍完整包含整个输出画布，并且其它不透明条件全部满足，可以在后续阶段将其视为全画布遮挡层。

第一阶段建议只接受“恒等几何 + 无 Crop”的全画布情况，避免几何边界计算错误。第二阶段再增加矩形覆盖分析，并保留 1 像素安全膨胀或直接回退策略。

## 4. 推荐架构

### 4.1 新增共享的纯分析模块

建议新增：

```text
src/features/edit/core/timeline_occlusion.py
```

建议提供以下类型和函数：

```python
@dataclass(frozen=True)
class LayerCoverage:
    kind: Literal["full_frame", "rect", "unknown"]
    rect: tuple[int, int, int, int] | None = None
    opaque: bool = False
    reason: str = ""


@dataclass(frozen=True)
class OcclusionDecision:
    render_layers: tuple[ClipLayerInfo, ...]
    culled_layers: tuple[ClipLayerInfo, ...]
    occluder_clip_id: str | None = None
    reason: str = ""


def analyze_visible_layers(
    layers: tuple[ClipLayerInfo, ...],
    transitions: tuple[TransitionBlendInfo, ...],
    frame_idx: int,
    output_size: tuple[int, int],
    *,
    mode: Literal["preview", "export"],
) -> OcclusionDecision:
    ...
```

实现要求：

1. 这是纯 Python/几何分析，不请求解码帧、不创建 OpenGL/Metal 对象、不读取 GPU 像素。
2. 从上到下扫描图层，找到第一个可靠的 `full_frame + opaque + normal` 图层。
3. 保留该图层及其上方图层，裁掉它下方的图层。
4. 分析不确定时返回原始全部图层，不能猜测为不透明。
5. 返回稳定的 `reason`，例如 `full_frame_opaque_occluder`、`opacity_below_one`、`transform_not_identity`、`unknown_alpha`，供 telemetry 使用。
6. 不直接修改 `TimelineRenderPlan` 的不可变模型；输出一个新的渲染选择结果。

### 4.2 接入预览

目标调用链：

```text
TimelineRenderPlan.get_frame_plan(frame)
        ↓
timeline_occlusion.analyze_visible_layers(...)
        ↓
只对 render_layers 执行 _get_layer_frame()
        ↓
CPU preview / GPU preview
        ↓
ProgramViewer present
```

建议改动：

| 文件 | 改动 |
|---|---|
| `src/features/edit/core/timeline_preview_renderer.py` | 在进入 `_composite_layers` 的取帧循环之前调用共享分析；只遍历 `render_layers` |
| `src/features/edit/core/edit_gpu_preview_renderer.py` | 如果该路径直接消费 layer list，使用同一分析结果；不要在 GPU 侧重复推断 |
| `src/features/edit/core/timeline_render_plan.py` | 暂不改变 `FrameRenderPlan.layers` 的含义，避免影响已有调用方；后续可增加只读分析辅助 API |
| `src/features/edit/core/realtime_playback/` | 只接收已经裁剪后的渲染任务，不在调度器中复制遮挡判断 |

### 4.3 接入导出

目标调用链：

```text
TimelineExporter._render_frame_impl()
        ↓
frame_plan + timeline_occlusion.analyze_visible_layers(...)
        ↓
_composite_layers(render_layers, ...)
        ↓
CPU compositor / Metal compositor / OpenGL fallback
```

建议改动：

| 文件 | 改动 |
|---|---|
| `src/features/edit/core/timeline_export.py` | 在 `_composite_layers` 取帧前使用分析结果；禁止被裁掉的图层进入 `_get_layer_frame` |
| `src/features/edit/core/timeline_gpu_export_renderer.py` | 消费已经裁剪后的 layer elements；保留 renderer 自己的 capability fail-closed |
| `src/features/edit/core/timeline_metal_export_renderer.py` | 不重新实现裁剪规则，只执行共享模块产出的图层列表 |
| `src/app/controllers/deliver_controller.py` | 不增加第二套判断；单文件、视频导出和批量导出都通过 TimelineExporter 生效 |
| `src/features/deliver/core/` | 如果存在独立导出入口，确认它最终复用同一 TimelineExporter；若不复用，补接入测试 |

## 5. 分阶段实施计划

| 阶段 | 优先级 | 任务 | 主要文件 | 依赖 | 完成标准 |
|---|---:|---|---|---|---|
| O0 | P0 | 建立基线 telemetry：每帧总 layer 数、实际请求帧数、被裁 layer 数、裁剪原因、render wall time | `timeline_preview_renderer.py`、`timeline_export.py`、`render_diagnostics.py` 或既有 telemetry | 无 | 同一素材在优化前后可比较，默认关闭裁剪时结果不变 |
| O1 | P0 | 新建共享 `timeline_occlusion.py`，实现仅支持“恒等几何、完全不透明、Normal、无效果、无转场”的 full-frame culling | `src/features/edit/core/timeline_occlusion.py` | O0 的字段定义 | 纯函数单测覆盖全部安全/不安全条件 |
| O2 | P0 | 接入 CPU 预览，确保被遮挡层在 `_get_layer_frame` 之前被移除 | `timeline_preview_renderer.py` | O1 | 上轨全屏覆盖时下轨无 decode request；画面与未裁剪基线逐像素一致 |
| O3 | P0 | 接入 CPU 时间线导出和视频导出 | `timeline_export.py` | O1 | H.264、ProRes、Topos 和批量导出均保持画面一致；下轨请求数下降 |
| O4 | P1 | 接入 GPU preview/export 的 layer list，避免 GPU 路径重新读取被遮挡源 | `edit_gpu_preview_renderer.py`、`timeline_gpu_export_renderer.py`、`timeline_metal_export_renderer.py` | O2/O3 | CPU/GPU layer count 和裁剪原因一致；无错误回退 |
| O5 | P1 | 支持“放大后仍覆盖全画布”的保守矩形覆盖分析 | `timeline_occlusion.py` | O1–O4 | 旋转、奇数尺寸、边界 1 像素情况下不误裁；不确定则保守回退 |
| O6 | P1 | 支持矩形可见区域分析，为未来 ROI 解码/局部合成提供数据 | `timeline_occlusion.py`、frame provider 接口 | O5 | 只输出 region plan，不改变现有整帧语义；有独立 feature flag |
| O7 | P2 | 评估效果链和插件的遮挡安全能力声明 | Effect Catalog / export capability | O4 | 只有声明 `background_independent` 且测试通过的效果可参与高级裁剪 |
| O8 | P1 | 实机基准和开关收口 | `docs`、benchmark tools、配置项 | O2–O6 | 4K 多轨基准达到收益门槛；失败时可关闭并保留诊断 |

## 6. 测试计划

### 6.1 纯逻辑单测

新增建议文件：

```text
tests/edit/test_timeline_occlusion.py
```

至少覆盖：

| 用例 | 预期 |
|---|---|
| 两轨，上轨普通模式、恒等变换、opacity=1、无 Alpha | 下轨被裁掉 |
| 三轨，上轨全屏不透明 | 只保留最上层及其上方层 |
| 上轨 scale=0.5 | 下轨保留 |
| 上轨平移/旋转 | 下轨保留，第一阶段不做激进判断 |
| 上轨 opacity=0.999 | 下轨保留 |
| 上轨有 fade 或 opacity keyframe | 下轨保留 |
| 上轨有 crop/feather | 下轨保留 |
| 上轨源 Alpha 未知或存在 | 下轨保留 |
| Multiply/Screen 等非 Normal 模式 | 下轨保留 |
| Cross Dissolve/Wipe/Dip To Color | 相关层全部保留 |
| Adjustment Clip / nested sequence / Composite Clip | 下轨保留 |
| 上轨不可用或几何异常 | 返回全部层并记录 reason |
| 上轨放大且几何覆盖整个画布 | O5 前保守保留；O5 后才允许裁剪 |

### 6.2 解码与合成回归

使用 fake frame provider 统计请求：

1. 上轨全屏覆盖：下轨 `request_frame` 次数必须为 0。
2. 上轨缩小：下轨必须请求，且输出四角能看到下轨。
3. 上轨缩小后再导出：不能出现黑色边界；验证此前 Alpha 修复仍然有效。
4. 同一帧开启/关闭 culling，输出图像逐像素一致，允许的差异仅限浮点量化误差。
5. 取消、暂停、失败和资源清理路径均不因裁剪结果泄漏 decoder session。

### 6.3 三路一致性

| 对比 | 验收 |
|---|---|
| CPU preview vs CPU export | 同帧 PSNR/SSIM 达到现有合成基线；不出现黑边或缺层 |
| CPU preview vs GPU preview | 裁剪决策一致；视觉差异不超过既有 GPU 容差 |
| CPU export vs GPU export | 完成全屏遮挡、缩放露底、Alpha、转场四组对拍 |
| 单文件 vs 批量导出 | 同一 `.topos` 工程得到相同裁剪决策和输出画面 |

## 7. 性能基准与目标

所有结果需记录机器、素材 SHA、代理/原片、输出分辨率、FPS、编码设置和后端。建议每组预热 60 帧，正式统计 480 帧，至少运行 3 次取中位数。

| 基准组 | 时间线 | 观察重点 |
|---|---|---|
| B0 | 单轨 | 不应因分析器造成可测回归 |
| B1 | 两轨，上轨全屏不透明 | 下轨解码请求接近 0，验证最大收益 |
| B2 | 两轨，上轨缩小 50% | 下轨仍被请求，输出正确；验证不误裁 |
| B3 | 四轨，最上层全屏不透明 | 下面三轨均不解码 |
| B4 | 四轨，上层交替缩放/透明/转场 | 只在安全帧裁剪，决策随帧变化正确 |
| B5 | 4K 原片与 1080p proxy | 对比 CPU、GPU、解码和显存收益 |
| B6 | 单文件与批量导出 | 确认后台/批量路径没有旁路旧逻辑 |

建议验收门槛：

- B0 开启分析器后总播放/导出耗时回归不超过 1%。
- B1 两轨全屏遮挡时，下轨解码请求减少 90% 以上。
- B3 四轨全屏遮挡时，至少减少 75% 的下层视频帧请求。
- B2 缩小露底时，不得为了追求请求数下降而跳过下轨。
- 任何视觉错误、黑底、闪帧或错误缺层都高于性能收益，立即回退该帧或关闭高级裁剪。

## 8. 开关、诊断与回退

建议增加开发/诊断开关，而不是永久保留不可关闭的隐式优化：

```text
TOPOS_TIMELINE_OCCLUSION_CULL=0|1
TOPOS_TIMELINE_OCCLUSION_DEBUG=0|1
```

规则：

1. 默认开启 O1 的保守 full-frame culling，O5/O6 在独立验证完成前默认关闭。
2. `DEBUG=1` 时输出每帧：总层数、保留层数、裁剪层数、occluder clip id、reason、是否发生回退。
3. 发现任何画面不一致时，可只关闭 culling，不影响其它 GPU/导出优化。
4. 分析异常、模型字段缺失、Alpha 不明、几何 NaN/Inf 时只回退到“保留全部层”，不能回退成黑帧。

## 9. 主要风险与对应措施

| 风险 | 后果 | 防护 |
|---|---|---|
| 把未知 Alpha 当成不透明 | 下层错误消失 | Alpha 未知一律保留；必须有素材格式白名单 |
| 关键帧在帧间变化 | 某些帧正确、某些帧缺层 | 每帧分析有效 opacity/transform，不跨帧盲目缓存 |
| 转场只检查上层 | 转场阶段下层短暂消失 | 任何相关 transition 直接禁用裁剪 |
| GPU 与 CPU 各自判断 | 预览和导出不一致 | 只允许共享纯分析模块产出 layer list |
| 只减少合成、不减少解码 | 性能收益很小 | 将分析放在 `_get_layer_frame`/decode request 之前，并统计请求数 |
| ROI 过早实现 | 复杂度、资源和边界错误 | O1 只做全画布裁剪，ROI 放到 O6 独立阶段 |
| adjustment/effect 读取背景 | 画面改变 | 第一阶段排除全部 adjustment、效果和插件；能力声明后再逐项放行 |
| 批量导出绕过 TimelineExporter | 单文件正常、批量仍慢或不一致 | 增加单文件/批量同工程对拍和调用链检查 |

## 10. 推荐实施顺序

```text
O0 基线与 telemetry
  ↓
O1 共享纯分析模块 + 单测
  ↓
O2 CPU 预览接入
  ↓
O3 CPU/批量导出接入
  ↓
O4 GPU 预览/导出接入
  ↓
O5 保守矩形覆盖
  ↓
O6 ROI 数据契约（单独评审）
  ↓
O7 效果/插件能力声明
  ↓
O8 实机基准、默认开关和文档收口
```

每个阶段单独提交、单独回归；不要把 O5/O6 的复杂几何判断和 O1 的安全收益混在同一个提交中。

## 11. 最终验收清单

- [ ] 上轨全屏不透明时，下轨不再请求视频帧。
- [ ] 上轨缩小后，下轨仍参与计算，四角和露出区域正确。
- [ ] 上轨移动、旋转、裁剪、Alpha、opacity、转场和特殊混合模式没有被错误裁掉。
- [ ] 预览、导出、GPU 回退和批量导出使用同一裁剪决策。
- [ ] CPU/GPU 输出与未优化基线通过画面对拍。
- [ ] 取消、暂停、失败、关闭窗口和批量队列清理无资源泄漏。
- [ ] telemetry 能显示实际减少的 layer decode/render 工作。
- [ ] 4K 多轨基准达到门槛，且单轨基准无明显回归。
- [ ] 发现异常时可通过开关关闭裁剪，不影响正常导出。
- [ ] 文档、测试、基准结果和已知限制同步更新。

## 12. 关联代码与文档

- `src/features/edit/core/timeline_render_plan.py`
- `src/features/edit/core/timeline_preview_renderer.py`
- `src/features/edit/core/timeline_export.py`
- `src/features/edit/core/edit_gpu_preview_renderer.py`
- `src/features/edit/core/timeline_gpu_export_renderer.py`
- `src/features/edit/core/timeline_metal_export_renderer.py`
- `src/features/edit/core/realtime_playback/`
- `docs/professional_nle_realtime_playback_optimization_plan.md` 的“多轨可扩展性”章节
- `docs/edit_optimization_plan_2026-09-07.md`
