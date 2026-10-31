# 视频时域降噪与空域降噪实现计划

## 1. 文档目标

本计划用于在 Topos Color 中实现面向视频调色的时域降噪与空域降噪。

第一阶段目标是支持 Color 页面的视频处理流程，后续再考虑统一到 Edit、Composite 和导出管线。

目标不是复制 DaVinci Resolve 的闭源实现，而是独立实现接近的功能、控制方式、画面质量和 GPU 性能特征。

DaVinci 的公开说明表明：时域降噪利用多帧信息，在静止区域效果更好；空域降噪分析单帧，对运动区域更可靠。推荐的组合顺序通常是先时域，再用空域补足残余噪声。

参考：

- [Blackmagic Design - DaVinci Resolve Color](https://www.blackmagicdesign.com/uk/products/davinciresolve/color)
- [DaVinci Resolve Spatial NR Controls](https://documents.blackmagicdesign.com/SupportNotes/DaVinci_Resolve_17_New_Features_Guide.pdf)

## 2. 当前项目状态

当前图片降噪主要由以下模块实现：

- `src/features/composite/nodes/denoise_node.py`
- `src/features/composite/core/shaders.py`
- `src/features/composite/evaluators/filter_evaluator.py`
- `src/shared/ai_models/denoise_worker.py`

当前 GPU 降噪是单帧双边滤波，输入只有一张纹理；AI 降噪是单图 ONNX 推理并烘焙结果，都不具备视频时域降噪所需的历史帧、运动补偿和场景切换状态。

现有视频基础主要包括：

- `src/shared/video/frame_provider_service.py`
- `src/shared/video/color_frame_provider_adapter.py`
- `src/shared/video/video_texture_pool.py`
- `src/features/color/core/async_renderer.py`
- 现有 GPU 解码、YUV 上传和外部纹理零拷贝路径

时域降噪不应简单扩展为普通单帧节点。它需要作为视频渲染上下文中的专用阶段处理：

```text
视频解码
  -> 邻帧读取与缓存
  -> 亮度/色度分离
  -> 运动估计
  -> 时域对齐与累积
  -> 空域降噪
  -> 调色节点与其他效果
  -> 合成、显示或导出
```

## 3. 第一阶段范围

### 3.1 包含范围

- Color 页面视频调色。
- Preview、Paused、Export 三种处理质量。
- 亮度和色度独立降噪。
- GPU 预览路径。
- GPU 解码外部纹理路径。
- CPU 上传回退路径。
- 连续播放、seek、拖动时间线、逐帧和导出。
- 项目参数保存与恢复。

### 3.2 暂不包含

- DaVinci 内部算法的精确复刻。
- AI/神经网络降噪。
- 复杂相机噪声标定模型。
- 一开始同时改造所有 Edit、Composite 和 Color 效果节点。

## 4. 参数规格

### 4.1 时域参数

```text
temporal_enabled
temporal_frames       1 / 2 / 3
temporal_luma         0-100
temporal_chroma       0-100
temporal_motion       fast / balanced / accurate
temporal_threshold
temporal_blend
scene_cut_sensitivity
```

### 4.2 空域参数

```text
spatial_enabled
spatial_mode          fast / better / enhanced
spatial_radius        1-10
spatial_luma          0-100
spatial_chroma        0-100
spatial_threshold
detail_recovery
```

### 4.3 质量模式

| 模式 | 时域窗口 | 运动估计 | 空域质量 | 用途 |
|---|---|---|---|---|
| Preview | 前向 1-2 帧 | Fast | Fast | 实时播放 |
| Paused | 前后各 2-3 帧 | Balanced | Better | 暂停、逐帧预览 |
| Export | 前后各 3-5 帧 | Accurate | Enhanced | 最终导出 |

## 5. 阶段 M0：规格、素材和性能基线

### 任务 M0.1：建立测试素材

准备以下视频：

1. 固定机位、低照度、高 ISO。
2. 人物缓慢移动。
3. 快速横摇。
4. 前景物体经过背景。
5. 树叶、头发、细线等高频纹理。
6. 暗部彩色噪声。
7. 镜头硬切。
8. 闪光灯或曝光突变。
9. 遮挡与重新出现。
10. HDR 或 10-bit 视频。

### 任务 M0.2：记录现有基线

记录每个测试视频的：

- 解码耗时。
- GPU 上传耗时。
- 单帧渲染耗时。
- 播放 FPS。
- 丢帧数量。
- GPU 显存占用。
- 当前图片降噪耗时。

建议新增：

```text
tools/benchmark_video_denoise.py
tests/video_denoise/
docs/video_denoise_spec.md
```

### M0 验收标准

- 有可重复运行的性能基线。
- 有原始视频帧、当前降噪结果和参考结果。
- 有统一的测试硬件、分辨率和质量模式记录。
- 不修改现有图片降噪路径。

## 6. 阶段 M1：视频空域降噪 V1

### 任务 M1.1：分离图片与视频空域降噪

建议新建：

```text
src/shared/video/denoise/
    __init__.py
    params.py
    frame_types.py

src/features/color/core/
    video_spatial_denoise.py
    video_denoise_shaders.py
```

图片继续使用现有 `DenoiseNode`；视频使用新的 `VideoSpatialDenoiser`。

### 任务 M1.2：亮度/色度分离

处理流程：

```text
RGB/YUV
  -> Y、Cb、Cr
  -> Y 使用边缘保护滤波
  -> Cb/Cr 使用更强、较低分辨率滤波
  -> 重新合成
```

### 任务 M1.3：实现三个空域质量模式

#### Fast

- 小半径双边滤波。
- 预计算空间权重。
- 低成本亮度边缘判断。
- 用于实时播放。

#### Better

- 两阶段或可分离双边滤波。
- 独立亮度和色度阈值。
- 更准确的边缘保护。
- 用于暂停预览。

#### Enhanced

- 多尺度处理。
- 更强的细节保护。
- 局部方差和纹理判断。
- 用于导出。

### 任务 M1.4：首轮 GPU 优化

现有 shader 半径为 10 时最多约 441 次采样，并且每个样本都可能计算指数权重。需要逐步完成：

- 预计算空间权重。
- 减少 `exp()` 调用。
- 亮度和色度分开采样。
- 色度使用较低分辨率。
- 避免每帧重复创建 FBO 和纹理。
- 视硬件能力选择 Compute Shader。

### M1 验收标准

- 静止画面噪声明显下降。
- 边缘没有明显光晕。
- 不出现大面积塑料感。
- 色度噪声可以独立控制。
- 关闭降噪时画面和性能与原路径一致。
- Preview 模式中参数可以实时调整。

## 7. 阶段 M2：邻帧读取与缓存

### 任务 M2.1：扩展帧服务接口

在 `src/shared/video/frame_provider_service.py` 增加邻帧窗口接口：

```python
request_frame_window(
    clip_id,
    center_frame,
    before,
    after,
    quality,
    generation_id,
)
```

返回内容包括：

```text
center_frame
previous_frames
next_frames
missing_frames
frame_indices
```

同时增加异步预取：

```python
prefetch_frame_window(...)
```

### 任务 M2.2：增加时域缓存

新建 `TemporalFrameWindowCache`，缓存键必须包含：

```text
clip_id
frame_index
quality
generation_id
pixel_format
resolution
```

缓存策略：

| 场景 | 策略 |
|---|---|
| 连续播放 | 保留当前帧前后 3-5 帧 |
| 拖动时间线 | 清空历史并重新建窗 |
| 镜头切换 | 清空历史 |
| 导出 | 顺序读取、禁止跳帧 |
| Preview | 使用 proxy |
| Paused/Export | 使用 master |

### 任务 M2.3：处理丢帧和 seek

必须检查：

```text
current_frame_index == last_processed_frame_index + 1
```

如果不连续：

- Preview：重置历史。
- Paused：重新请求邻帧。
- Export：禁止跳帧，保证顺序处理。

### M2 验收标准

- 连续播放能稳定获得邻帧。
- 快速拖动不会使用旧历史。
- 切换视频文件后不会串帧。
- proxy 和 master 不会混用。
- 缓存大小受内存和显存预算控制。
- 缺帧时可以退化为空域降噪。

## 8. 阶段 M3：运动估计

### 任务 M3.1：先实现低成本块匹配

第一版使用亮度通道和低分辨率块匹配：

```text
当前帧 Y + 上一帧 Y
  -> 1/4 分辨率
  -> 局部搜索候选块
  -> motion_vector
  -> motion_confidence
  -> matching_error
```

### 任务 M3.2：多尺度运动估计

```text
原图
  -> 1/2 分辨率
  -> 1/4 分辨率
  -> 粗到细估计
```

### 任务 M3.3：遮挡和场景切换

检测以下情况：

- 当前像素在上一帧找不到对应位置。
- 对齐后的颜色差过大。
- 匹配置信度过低。
- 全帧亮度变化异常。
- 直方图发生突变。

低置信度区域降低历史权重；场景切换时清空历史。

### M3 验收标准

- 固定机位不会明显闪烁。
- 缓慢运动不会明显拖影。
- 快速运动不会出现大面积重影。
- 边缘不会出现双边。
- 镜头切换时历史被清空。
- 曝光变化不会被错误地当成普通运动累积。

## 9. 阶段 M4：时域降噪 V1

### 任务 M4.1：先实现单向时域降噪

Preview 模式使用因果模式：

```text
当前帧 + 前一帧
```

处理流程：

```text
上一帧
  -> 使用 motion vector warp
  -> 与当前帧比较
  -> 计算置信度
  -> 自适应混合
```

基础权重：

```text
history_weight =
    temporal_strength
    * motion_confidence
    * color_similarity
    * occlusion_confidence
```

运动越大、匹配越差，历史权重越低。

### 任务 M4.2：实现多帧时域降噪

Paused 和 Export 模式使用：

```text
前 1-3 帧
当前帧
后 1-3 帧
```

每一帧都需要：

1. 获取运动矢量。
2. Warp 到当前帧坐标。
3. 计算有效性和置信度。
4. 参与亮度/色度独立累积。
5. 根据噪声阈值决定权重。

### 任务 M4.3：噪声估计

第一版使用局部统计：

```text
局部均值
局部方差
高频残差
亮度区域
色度区域
```

后续再增加 ISO、相机元数据、阴影和高光相关模型。

### 任务 M4.4：渲染状态管理

在 `src/features/color/core/async_renderer.py` 中增加每个视频流独立的状态：

```python
TemporalDenoiseState(
    clip_id,
    last_frame_index,
    generation_id,
    history_textures,
    motion_fields,
    scene_id,
    parameters_hash,
)
```

以下情况必须重置：

- 视频切换。
- seek 或时间线跳跃。
- 当前帧号不连续。
- 参数改变。
- 分辨率改变。
- proxy/master 切换。
- 场景切换。
- OpenGL 上下文重建。

### M4 验收标准

- 静止区域噪声显著降低。
- 运动人物没有明显拖影。
- 运动物体边缘不冻结。
- 快速运动时自动降低历史权重。
- 第一帧不会出现黑帧或旧帧污染。
- 最后一帧不会等待不存在的后帧。
- seek 后不会带入 seek 前历史。
- 倒放和变速不会产生错误累积。

## 10. 阶段 M5：接入 Color 视频渲染管线

不要从图片 `on_denoise_requested()` 入口接入视频降噪。推荐在视频纹理准备完成后、调色节点执行前接入：

```text
_do_render_video_frame()
  -> 当前视频纹理
  -> VideoDenoisePipeline.process()
  -> _do_render(render_params)
```

GPU 外部纹理路径也要使用同一套管线：

```text
_do_render_video_gpu_frame()
  -> 外部视频纹理
  -> VideoDenoisePipeline.process()
```

CPU 上传、GPU 外部纹理和导出路径必须保持相同的参数语义。

### UI 建议

现有图片降噪面板增加模式：

```text
Still Image
Video Denoise
```

视频面板包含：

```text
Temporal
    Enable
    Frames
    Luma
    Chroma
    Motion Quality
    Threshold

Spatial
    Enable
    Mode
    Radius
    Luma
    Chroma
    Detail Recovery
```

增加：

```text
Preview Quality
Final Quality
Reset Temporal History
```

### 项目保存

视频降噪使用独立项目字段：

```json
{
  "video_denoise": {
    "enabled": true,
    "temporal": {},
    "spatial": {},
    "quality_mode": "preview"
  }
}
```

不能与现有图片降噪字段共用，避免旧项目参数冲突。

## 11. 阶段 M6：性能优化

### 必须遵守的原则

- 运动估计、warp、累积和滤波尽量全部在 GPU 完成。
- 不允许每帧 GPU 到 CPU 再回 GPU。
- 历史帧纹理使用纹理池。
- 运动估计使用低分辨率亮度图。
- 色度可以使用低分辨率处理。
- 不在播放热路径频繁创建 FBO。
- 不在每帧调用强制 GPU 同步。
- Preview、Paused 和 Export 使用不同质量等级。
- 运动估计和降噪资源使用对象池。

现有 `VideoTexturePool` 需要增加历史帧锁定或引用计数，避免历史纹理仍在采样时被回收。

### 性能指标

指标必须基于固定测试硬件记录：

| 指标 | Preview 目标 | Export 目标 |
|---|---:|---:|
| 1080p | 不低于实时播放 | 稳定输出 |
| 4K | 使用代理或自动降级 | 允许离线渲染 |
| GPU 回读 | 0 次 | 0 次 |
| 历史缓存命中率 | 90% 以上 | 尽量接近 100% |
| 场景切换伪影 | 0 次 | 0 次 |
| 额外播放卡顿 | 不明显增加 | 不适用 |

## 12. 阶段 M7：测试体系

### 单元测试

测试：

- 邻帧窗口计算。
- 缓存命中、淘汰和内存限制。
- seek 后状态重置。
- 帧号不连续处理。
- scene cut 重置。
- proxy/master 隔离。
- 参数变化导致历史失效。
- 缺帧时退化为空域降噪。

### 集成测试

测试：

- 正常连续播放。
- 快速拖动。
- 暂停逐帧。
- 倒放。
- 变速。
- 切换素材。
- 切换 proxy 和 master。
- 播放中修改参数。
- GPU 零拷贝路径。
- CPU 上传回退路径。
- 视频导出。

### 视觉验收

重点观察：

```text
静止区域：噪声是否下降
运动区域：是否拖影
边缘：是否出现双边
细节：头发、树叶、纹理是否被抹掉
色度：是否出现彩色拖尾
暗部：是否出现色块
高光：是否出现闪烁
切镜：是否串帧
```

建议保存：

```text
original
temporal_only
spatial_only
temporal_then_spatial
spatial_then_temporal
reference
```

## 13. 功能开关和回滚策略

初期必须增加：

```text
video_denoise_enabled
video_temporal_denoise_enabled
video_spatial_denoise_enabled
video_denoise_backend
video_denoise_debug_overlay
```

回退优先级：

```text
时域 + 空域
  -> 失败时使用空域
  -> 空域失败时使用原始视频帧
```

缓存错误、GPU 资源错误和邻帧缺失不能阻塞视频播放。

## 14. 里程碑和完成标准

| 里程碑 | 完成标准 |
|---|---|
| M0 | 有测试素材、参数规格和性能基线 |
| M1 | 视频空域降噪可以实时运行 |
| M2 | 邻帧读取、缓存和 seek 重置稳定 |
| M3 | 运动估计和置信度可视化正常 |
| M4 | 时域降噪无明显重影和拖影 |
| M5 | 时域 + 空域完成 Color 页面接入 |
| M6 | 播放、暂停和导出三条路径一致 |
| M7 | GPU 零拷贝、缓存和性能优化完成 |
| M8 | 项目保存、回退和自动化测试完成 |

## 15. 推荐实施顺序

```text
性能基线
  -> 视频空域降噪
  -> 邻帧缓存
  -> 运动估计
  -> 单向时域降噪
  -> 多帧时域降噪
  -> 时域 + 空域组合
  -> Color 管线接入
  -> 导出和性能优化
```

第一版不要同时实现 AI 降噪或完整高质量光流。先完成经典时域降噪、可实时空域降噪和稳定的视频状态管理，再逐步提高画质和性能。
