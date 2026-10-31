# 视频降噪实现规格（M0-M7 第一版完成状态）

实现依据：`docs/video_denoise_implementation_plan.md`。
本文档记录第一版（V1）的最终架构、参数语义、性能基线与已知边界。

## 1. 模块地图

```
src/shared/video/denoise/               纯 CPU 侧（可独立单测）
    params.py                           参数 dataclass + 质量模式裁剪 + 序列化
    frame_types.py                      FrameWindow + TemporalFrameWindowCache
    window_provider.py                  暂停态邻帧窗口提供器（UI 去抖调度 +
                                        后台取帧 → RGBA f16 转换 → Qt 信号交付）

src/features/color/core/                GPU 侧（渲染线程专用）
    video_denoise_shaders.py            GLSL 源码（空域三模式/运动估计/时域累积/
                                        调试叠加）
    video_denoise_gl.py                 QuadRenderer + RenderSurfaceCache
    video_spatial_denoise.py            VideoSpatialDenoiser
    video_temporal_denoise.py           VideoTemporalDenoiser + TemporalDenoiseState
                                        （因果模式 + 窗口重放 + debug_overlay）
    video_denoise_pipeline.py           VideoDenoisePipeline（时域→空域编排 +
                                        开关/回退/调试叠加）

src/shared/video/frame_provider_service.py
    request_frame_window / prefetch_frame_window   邻帧窗口接口（M2）

src/features/color/core/async_renderer.py
    _do_render_impl 中 source_tex 就绪后、raw_view 前接入（M5）
    SET_VIDEO_DENOISE / RESET_VIDEO_DENOISE_HISTORY /
    SET_VIDEO_DENOISE_WINDOW 命令
```

## 2. 渲染管线接入点

CPU packed 上传路径与 GPU 外部纹理（VT 零拷贝）路径在 `_do_render_impl`
的 source_tex 选择处汇合（YUV→RGB 已完成，RGBA16F 视口分辨率），在此统一
执行 `VideoDenoisePipeline.process()`：

```
视频纹理准备（CPU 上传 / VT 零拷贝 + YUV→RGB）
  -> 时域降噪（运动估计 + warp + 置信度累积）
  -> 空域降噪（fast / better / enhanced）
  -> raw_view / 调色节点 / 输出
```

质量模式映射：播放中 `preview`；暂停/静态预览/调色刷新 `paused`；
导出 `export`（参数裁剪见 params.for_quality）。

## 3. 算法要点

- 权重全部用有理近似 `1/(1+k·x²)` 替代 `exp()`；空间/值域 sigma 随强度自适应。
- 空域三模式：
  - fast：单 pass 联合双边（半径≤4，49 tap 上限）
  - better：可分离两 pass（半径≤6，色度仅第二 pass 过滤）
  - enhanced：1/2 降采样 → 可分离双边 → 上采样 + 残差能量自适应细节恢复
- 运动估计：1/4 分辨率亮度（16 tap box 降采样），**5×5 块 SAD 搜索**
  ±3/±4/±5（fast/balanced/accurate）+ **抛物线亚像素细化** +
  **3×3 运动场平滑（置信度取最小值）**，输出 UV 空间运动矢量 +
  噪声底标定置信度。
  **G-P1（2026-08-24 伪影修复）**：早期 3×3 支撑整数搜索在缓慢平移
  （1/4 res 上 <1px/帧）下运动场分裂（一半像素 0px、一半 ±4px 整数
  步长），std≈4px 且置信度虚高（0.98）→ 双向错位历史高置信融合 =
  涂抹/重影伪影。5×5 支撑 + 亚像素细化 + 平滑后 std≈0.5px。
- 时域累积（因果，Preview）：history ping-pong，warp 历史与当前帧按
  `strength × blend × confidence × similarity × ramp` 混合；亮度/色度独立；
  **相似度噪声自适应**（半衰减点 = m_tol·σ，阈值 0→2σ/1→5σ）；
  亮度权重上限 0.85 / 色度 0.90；逐像素场景切换检测（亮度差超阈值
  直接放弃历史）。
- 时域窗口模式（Paused）：UI 侧 `VideoDenoiseWindowProvider` 在暂停/seek
  落定/逐帧/参数变化后（300ms 去抖）后台请求邻帧窗口（master 质量，
  前后各 2 帧），CPU 转换为归一化 RGBA float16（宽度 ≤2560 控制显存），
  经 `SET_VIDEO_DENOISE_WINDOW` 投递渲染器；渲染器在 paused 质量且
  窗口 center 帧号与当前帧一致时懒上传纹理并走窗口重放，否则回退因果
  历史。重放时**邻帧轮半权重、当前帧轮全权重**（邻帧为降采样上传，
  高频先天偏软，当前帧细节必须保占比）。换素材/卸载清除窗口，在途
  请求被 token 作废。
  **G-P0 关键约束（黑屏修复 2026-08-24）**：窗口取帧必须用独立
  ClipSession（`open_clip(share_by_path=False)`），绝不能复用播放会话——
  共享 master 解码器时，逐帧 seek 取邻帧会与播放帧请求竞争（H.264
  实测播放帧供给恶化 8 倍），导致播放黑屏（声音正常）。播放开始时
  UI 侧作废在途取帧请求（`_on_video_play_state_changed`）。
- 调试叠加（video_denoise_debug_overlay）：面板复选框或 config 开启，
  在时域输出上叠加最近一次运动置信度场——青=高置信（正在累积历史）、
  红=低置信（运动/遮挡）、黄色增量随运动矢量模长增大。
- 状态机 `TemporalDenoiseState` 重置条件（全部实现并有单测）：
  换素材 / 世代变化（seek）/ 帧号跳变 / 播放方向反转 / 参数变化 /
  分辨率变化；同帧重绘（调色调整）复用历史不重置。

## 4. 性能基线（RX 6900 XT，2026-08-23，工具 tools/benchmark_video_denoise.py）

| 阶段 | 1080p | 4K 原生 |
|---|---:|---:|
| temporal（热纹理，管线内实际状态） | 0.57 ms | 2.35 ms |
| temporal（新上传纹理，含首采样惩罚） | 6.5-10 ms | 31 ms |
| spatial fast | 0.86 ms | 3.5 ms |
| spatial better | 0.31 ms | 1.3 ms |
| spatial enhanced | 0.21 ms | 0.7 ms |
| 时域+空域组合（新纹理） | 5.7-6.0 ms | 20-23 ms |
| 显存占用（表面缓存） | 82 MB | 439 MB |

说明：
- 真实播放中，VT 零拷贝路径的源纹理由 GPU YUV→RGB pass 产出（已驻留），
  降噪读它接近"热纹理"成本；CPU packed 上传路径会承担部分首采样惩罚。
- 300 帧浸泡：表面数有界（7→8 后稳定）、显存恒定、0 次意外重置、无泄漏。
- 9 个测试素材（H264/H265/ProRes 422/4444/DNxHR 12-bit，2K/4K）三轮跑分全部通过。

## 5. 功能开关与回退

config.json：
```
video_denoise_enabled / video_temporal_denoise_enabled /
video_spatial_denoise_enabled / video_denoise_backend /
video_denoise_debug_overlay（面板复选框实时切换）
```
回退链：时域+空域 → 仅空域 → 原始帧。任何 GL 异常直通原始帧，不阻塞播放；
管线持续报错（60s 内 >30 次）自动禁用。

## 6. 项目持久化

顶层独立字段 `video_denoise`（与图片降噪 source_bake 完全隔离）：
```json
{"video_denoise": {"temporal": {...}, "spatial": {...}}}
```
UI：Denoise 面板新增"视频降噪"模式（时域/空域分组实时调整，非破坏，
附重置时域历史与调试叠加开关）。

## 7. 测试体系

- 54 项单测/集成测试（tests/video_denoise/）：参数、帧窗口、状态机、
  GPU 算法（standalone GL）、真实渲染线程集成（AsyncRenderer 全链路：
  命令队列/GL mutex/播放降噪/seek 重置/暂停窗口消费与回退）、
  邻帧窗口提供器（转换/调度语义）。
- tools/benchmark_video_denoise.py：真实素材基准（fresh/warm 两模式）。

## 8. 已知边界（后续迭代）

1. 暂停态邻帧窗口仅覆盖单视频（video_controller）路径；Color 时间线
   clip 的 source 帧映射与时间线去抖作为后续迭代。
2. Preview 模式按计划裁剪为 fast 空域（半径≤4）；better/enhanced 在
   paused/export 生效。
3. 噪声估计为强度驱动的启发式（M4.3 局部统计版可后续加入）。
4. Edit/Deliver 页导出管线尚未接入同一管线（Color 页 color_output 路径已生效）。
5. TemporalFrameWindowCache（frame_types.py）面向后续时间线多 clip 场景，
   当前生产路径使用 window_provider 的即时取帧方案。
