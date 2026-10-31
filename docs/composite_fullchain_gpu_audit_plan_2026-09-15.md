# 合成（Composite）页面全链路 GPU 覆盖审计与优化计划表

- 日期：2026-09-15
- 环境：Windows 11 / AMD Radeon RX 6800 / standalone GL 3.3（默认）与 4.6（对照）
- 素材：真实调色工程素材（桌面「截图」文件夹）
  - 4K h264 25fps（`2620吉利调色1.mp4`，3840×2160）
  - 1080p h264（`260616五菱调色2.mov`）
  - 4K ProRes 4444 12bit（`1.mov`）
  - 从上述视频抽取的真实帧 PNG（4K×4 帧 / 1080p×4 帧）+ ProRes 帧 EXR
- 测量入口：生产代码路径 `CompositeFrameRenderer.render_comp_clip_frame`（与 Edit 时间线 comp_clip 预览/导出完全同一条链路），预览 = `srgb_u8` 读回、导出 = `linear_f32` 读回
- 基准脚本：`scripts/bench_composite_nodes_gpu_coverage.py`（逐节点）、`scripts/bench_composite_readback_micro.py`（读回微基准）
- 原始数据：`tests/media/results/composite_nodes_bench_2026-09-15.json`（GL3.3 全量）、`composite_nodes_bench_gl460_2026-09-15.json`（GL4.6 对照子集）

> 结论速览：**合成页面自身的实时预览已经做到 GPU→GPU 零读回**（viewer glBlitFramebuffer 直通），
> ~70 个 GLSL 效果程序绝大多数全 GPU；真正的"全链路 GPU"断点集中在：
> ① Edit 时间线 comp_clip 路径的 CPU 读回往返（4K 下每帧 ~0.5s），
> ② 无头上下文 GL 3.3 导致 compute shader 全禁用，
> ③ 六类 CPU 节点（AI/deflicker/RIFE/paint/particle/源解码），
> ④ 导出末端的 linear f4 读回。下面按优先级给出问题清单与修复路线。

---

## 一、全链路 GPU 覆盖现状矩阵

| 链路 | 现状 | 实测（真实素材） | 断点 |
|---|---|---|---|
| 合成页面交互预览（页面内 viewer） | ✅ GPU→GPU 直通上屏，零 CPU 读回 | —（无读回） | 无 |
| 效果节点求值（Filter/Color/Keyer/Merge/Channel/Transform/Mask） | ✅ fragment/compute shader 全 GPU | 1080p +2~93ms/节点 | 无（少数节点见问题 #9） |
| Edit 时间线 comp_clip 预览 | ❌ GPU 求值 → CPU 读回(uint8) → LRU 缓存 → Edit 预览合成器再上传 GPU | 1080p 基线 18.2ms/帧；**4K 基线 509ms/帧** | 读回往返 + 4K 分块路径（#1/#6） |
| 导出（comp_clip / CLI topos_render） | ❌ GPU 求值 → linear f4 全帧读回 → CPU 编码 | 1080p ~58ms/帧读回；4K ~500ms/帧 | #1/#7 |
| 视频源解码（video_frame 节点，Windows） | ❌ CPU 软解（VT 硬解仅 macOS） | 4K h264 205ms/帧、4K ProRes 253ms/帧、1080p 69ms/帧 | #8 |
| AI 节点 | ❌ 每次求值 2×全帧 f4 读回 + EXR 磁盘缓存 + onnxruntime | 透传固定开销 1080p ~25ms / 4K ~785ms | #2/#5 |
| deflicker / RIFE / particle / paint / text | ❌ CPU（读回/numpy/栅格化） | 见第三节 | #3/#4/#9 |

---

## 二、问题清单（按优先级，均为本次真实素材实测发现）

### P0-1 【最大性能断点】>2048px 输出走分块路径并被强制 linear_f4 读回，4K 预览每帧 ~500ms

- 位置：`src/features/composite/render/composite_export_renderer.py:205-211`
  （`if plan.needs_tiling ...: self._readback_output = "linear_f32"`，docstring 自述
  "tiled renders always use linear_f32"）；`TILE_THRESHOLD = 2048`（:94）
- 现象（cProfile 实测，4K 基线预览 492ms/帧）：
  - 每帧切 4 个 2048² 瓦片，每瓦片一次 `read_into` f4 同步读回（67MB/片，共 268MB/帧）≈ 298ms
  - numpy `.copy` 拼接 ≈ 66ms、`_stitch_tile` ≈ 52ms、`ctx.finish()`×4 ≈ 27ms、cvtColor ≈ 14ms
  - **GPU 求值本身仅 ~5ms/帧**；读回与拼接占 >90%
- 对照：1080p（低于阈值）走整帧 srgb_u8 路径，基线仅 18.2ms/帧；裸 f1 读回微基准 4K 仅 ~15ms
- 修复方向：
  1. **按实际资源动态选择预览策略，而不是无条件禁用分块**：当目标预览尺寸、最大纹理尺寸、当前可用 VRAM、图中峰值临时纹理和 in-flight 帧预算允许时，走整帧 GPU 路径；4K 一张 linear f4 纹理约 132MB，必须把输入、输出、ping-pong、缓存和其他 Edit 任务都计入预算；
  2. 不能整帧时，分块预览也必须保持 GPU 内拼接，并使用 `srgb_u8/RGBA8` 输出或异步 PBO 旁路读回；禁止逐瓦片 `linear_f32` 同步读回、numpy 拼接后再上传；
  3. 预览目标尺寸应独立于源/交付分辨率：按 viewer 像素尺寸和 Auto/Full/Half/Quarter 质量档选择，只有用户明确要求全分辨率时才申请完整 4K 输出；
  4. 记录本帧选择整帧/瓦片/降分辨率的原因、临时纹理峰值、读回字节和降级状态，避免“看似预览很快但实际上悄悄降低质量”。
- 预期收益：4K comp_clip 预览 ~500ms → ~20-30ms/帧（>15×）

### P0-2 无头 GL 上下文默认 3.3，compute shader 全禁用（大半径效果 fragment 回退）

- 位置：`composite_export_renderer.py:360` `moderngl.create_standalone_context()` 未传 `require`
- 对照：交互页面请求 GL 4.5（`src/main.py:182` QSurfaceFormat）；本机 standalone 实测 4.6 可用
- 实测影响（1080p 预览，GL3.3 → GL4.6）：
  | 节点 | GL3.3 | GL4.6 | 提升 |
  |---|---|---|---|
  | blur(radius 24) | 31.4ms | 16.1ms | ~2.0× |
  | defocus(size 24) | 21.1ms | 11.0ms | ~1.9× |
  | denoise | 20.2ms | 11.0ms | ~1.8× |
  | sharpen | 23.5ms | 12.9ms | ~1.8× |
- 修复：建立 capability negotiation，而非硬编码把所有无头上下文设为 `require=430`：Windows/Linux 优先请求计算着色器所需版本并用真实 dispatch probe 验证；macOS 保持 GL 4.1 fragment 回退；失败时记录 `compute_unavailable`、驱动/版本和具体节点 fallback，不能让无头导出整体不可用。
- 影响面：Edit 时间线 comp_clip、CLI 渲染（`scripts/topos_render.py`）、并行导出 worker——交互合成页面不受影响

### P0-3 AI 节点在无模型/无 mask 时返回 None，整个图无输出（黑帧）

- 实测：`ai_generate / ai_inpaint / ai_expand / ai_denoise` 在本机（无 onnx 模型）求值返回空 →
  `render_comp_clip_frame` 返回 None → Edit 时间线该 comp_clip 全黑/丢帧；而 `ai_remove / ai_gen_expand` 走透传正常
- 位置：`src/features/composite/evaluators/base_evaluator.py:1282+`（`_evaluate_ai_node` 分支差异）
- 修复：AI 基础设施不可用/输入不完整时统一走输入透传（与 ai_remove 一致），绝不让单节点吞掉整图输出

### P1-4 `migrate_output_format_mode` 用未解码 image_input 的 width=0 播种 → Output 静默 1×1

- 位置：`src/features/composite/core/node_engine.py:2496-2504`（从 `image_input` 的隐藏参数
  `width/height` 播种，而这两个参数**解码前默认 0**，`isinstance(0, int)` 通过）；
  `output_node.py:257` `max(1, 0)` → 1×1
- 触发：加载输出节点未显式 custom 的老图（migration 在 `from_dict` 时运行，早于任何解码）
- 实测复现：省略输出节点宽高的图 → 输出 (1,1,3)
- 修复：播种时校验 `w > 0 and h > 0`，否则跳过该节点继续找 / 保留 OutputNode 自身 1920×1080 默认

### P1-5 paint 节点空蒙版上传每帧失败（dtype/size 双错）

- 位置：`src/features/composite/evaluators/filter_evaluator.py:867`
  `mask_tex.write(np.zeros((h, w, 4), dtype=np.float32).tobytes())`
- 实测日志：`_moderngl.Error: data size mismatch 132710400 != 66355200`（f4 数据写入 f1 纹理，
  4K 下每帧报错；被 except 吞掉但每帧浪费一次 132MB numpy 分配 + tobytes）
- 附带：paint 无笔画（passthrough）在 4K 预览 71ms/帧——空状态应零成本直通
- 修复：按目标纹理 dtype/大小构造 zeros（`dtype="f1"`、h×w×4 字节），或空笔画直接返回输入

### P1-6 4K 视频瓦片路径纹理池耗尽 + 瓦片静默丢弃

- 实测日志（`video_frame_4k_h264`，中位 1042ms/最大 5118ms）：
  - `Could only free 0 bytes, needed 236879872`（持续增长，池 2GB 但可释放集为空）
  - `glActiveTexture err=1282（无效操作）` → `Tile 0/2 produced no output, skipping`
- 位置：瓦片+解码纹理叠加超过 `GPUResourcePool(max_memory_mb=2048)` 可回收集合；
  释放后的纹理仍被绑定导致 GL 1282
- 修复：瓦片路径按预算预排布纹理；释放前确保解绑；瓦片失败必须显式报错而非 skip

### P1-7 Edit 时间线 comp_clip 的 GPU→CPU→GPU 往返

- 链路：worker 线程 GL 求值+读回 uint8（`timeline_preview_renderer.py:2659`）→ `_comp_clip_cache` →
  Edit 预览合成器再上传为轨纹理 → GPU 合成
- 已有可复用机制：合成页面 viewer 的共享纹理直通（`test_async_present_readback.py:258`
  "timeline passthrough texture" 契约）与 `pbo_readback.py` 异步读回
- 修复方向：先冻结 `CompositeGpuFrameHandle` 契约，再实现共享纹理直通。契约必须包含 backend/device、texture/surface、尺寸、pixel format、color space、alpha/premult 语义、frame/generation、fence、lease/release callback 和 fallback reason；之后才把 comp_clip 求值结果以共享纹理（同 share-group GL texture / WGL-DX interop / D3D11 surface 链）直通给 `EditGpuPreviewRenderer` 轨纹理槽。读回只留给缓存缩略图、显式 CPU encoder 或诊断旁路。
- 预期收益：叠加 P0-1 后，4K comp_clip 预览总成本从 ~500ms+二次上传 → 单次 GPU 拷贝

### P2-8 video_frame 源解码在 Windows 全软解 + Provider 打开失败降级

- 实测：4K h264 205ms/帧、4K ProRes4444 253ms/帧（25fps 预算 40ms，离线可接受、实时不可行）；
  `VideoFrameProviderService failed ... Failed to open clip`（4K mp4）→ 每次走 direct source 降级并打错误日志
- 修复方向：接入既有 `src/shared/media/windows_hw/` D3D11/DXVA 解码基建（W-B01 一侧）；
  Provider 打开失败需区分"格式不支持"与"偶发失败"并修正打开参数

### P2-9 CPU 节点实测热点（非 bug，GPU 化机会）

| 节点 | 1080p 预览 | 4K 预览 | 主因 |
|---|---|---|---|
| deflicker | 401ms | 3324ms | 全帧读回 + np.sort 时域窗口（且负帧号内部报错 `need at least one array to stack`） |
| frame_interpolate（无 RIFE 模型→线性混合回退） | 352ms | 2537ms | 双帧 f4 读回 + CPU 混合 + 上传 |
| particle(count 500) | 282ms | 802ms | Python 逐粒子模拟 + 逐粒子 splat 栅格 |
| paint（空笔画） | 25~31ms | 71ms(一次修复前) | 见 P1-5 |
| text | 36ms（内容哈希命中后 ~0） | — | QPainter 栅格 + numpy 合成（offscreen 下字体可用） |
| AI 透传固定开销 | 9~25ms | 785ms | 2× f4 全帧读回（缓存命中也逃不掉） |

- deflicker 修复：时域窗口改为 GPU 环形纹理组（shader 取 5 帧排序/百分位），或至少把读回降到 f1；
  同时修负帧号边界
- RIFE 回退：线性混合可在 GPU 用一个双纹理 shader 完成（模型路径除外）
- particle：模拟留 CPU（粒子数少时够快）但 splat 改 instanced point-sprite / compute
- AI 读回延迟：可在全帧读回前用缓存键判等，命中则跳过 2× 读回；缓存键不得只依赖可复用的 `texture_id`，应至少包含输入资源 generation/revision、节点参数、模型/权重版本、分辨率、像素格式、color space、时间点和上游图 revision，防止纹理复用导致错误命中

### P2-10 其它

- 导出线性读回 1080p 恒 ~58ms/帧：`_readback` 里每帧新建临时纹理+FBO（`composite_export_renderer.py:797-812`）+
  `ctx.finish()`；应优先按交付 codec 分流：硬编路径走 GPU encoder surface、零 CPU 回读；必须走 CPU/Topos 平面编码的兼容路径使用池化纹理/FBO + 有界 PBO 双/三缓冲异步流水线。两条路径都要记录 CPU upload/download bytes，禁止把隐式回读称为全 GPU。
- `VideoFrameProviderService` 降级日志噪音：见 P2-8
- 交互页面 16ms 防抖 + 分辨率档（full/half/quarter/auto）+ proxy_scale/LOD 已到位，无需改动

---

## 三、逐节点实测数据（真实素材，中位数 ms/帧）

### 1080p（基线：冷 299ms / 预览 18.2 / 导出读回 57.9）

| 节点 | 预览 | 导出 | | 节点 | 预览 | 导出 |
|---|---|---|---|---|---|---|
| grade | 30.8 | 69.3 | | keyer | 74.0 | 71.5 |
| color_correct | 31.6 | 57.9 | | chroma_keyer | 51.1 | 66.2 |
| cs_transform | 19.4 | 57.4 | | luma_keyer | 30.5 | 76.0 |
| huecorrect | 30.4 | 68.7 | | difference_keyer | 78.6 | 74.1 |
| lut | 29.4 | 61.9 | | light_wrap | 63.7 | 70.4 |
| topos_color | 20.4 | 80.6 | | blend | 76.5 | 77.5 |
| blur(r24) | 31.4 | 74.6 | | deep_merge | 92.3 | 71.5 |
| glow | 22.1 | 80.3 | | channel_merge | 110.9 | 60.6 |
| denoise | 20.2 | 77.2 | | transform | 15.1 | 56.5 |
| sharpen | 23.5 | 63.8 | | reformat | 30.0 | 72.5 |
| erode_dilate(s20) | 28.3 | 75.9 | | crop | 29.3 | 60.0 |
| defocus(s24) | 21.1 | 63.8 | | cornerpin | 17.4 | 50.1 |
| edge_detect | 21.5 | 48.4 | | mask | 21.5 | 58.0 |
| relight | 67.3 | 57.7 | | roto_shape | 11.0 | 64.0 |
| fog | 52.1 | 65.4 | | depth_matte | 38.7 | 56.0 |
| godrays | 63.1 | 76.2 | | text | 35.7 | 60.8 |
| chromatic_aberration | 32.6 | 69.2 | | paint | 31.4 | 49.7 |
| vignette | 31.3 | 85.6 | | particle(500) | 282.0 | 60.4 |
| lens_flare | 40.5 | 76.8 | | frame_interpolate | 352.1 | 67.0 |
| lens_distortion | 41.2 | 67.9 | | deflicker | 401.0 | 80.9 |
| shuffle | 25.8 | 79.4 | | ai_remove(透传) | 25.1 | 66.3 |
| copy_channel | 66.3 | 61.9 | | ai_gen_expand(透传) | 39.4 | 57.9 |
| set_alpha | 88.8 | 75.2 | | noise | 6.3 | 65.1 |
| invert | 35.6 | 58.6 | | ramp | 8.6 | 69.9 |
| clamp | 39.5 | 74.0 | | constant | 9.5 | 58.6 |
| normalize | 38.3 | 84.1 | | compare | 41.4 | 66.4 |

（ai_generate/ai_inpaint/ai_expand/ai_denoise：无模型 → 返回 None，见 P0-3）

### 4K（基线：冷 1472ms / 预览 509 / 导出 500 —— 均为分块 f4 读回路径，见 P0-1）

- 效果节点预览 477~932ms，其中读回+拼接 ~470ms 恒定开销，效果本身增量与 1080p 一致外推
- 重灾区：deflicker 3324、frame_interpolate 2537、deep_merge 932、channel_merge 853、
  blend 852、paint 835、particle 802、ai_remove 785
- reformat（输出降到 1080p）仅 243ms —— 反证瓶颈在读回分辨率而非求值

### 视频源解码（video_frame 节点，含全链路渲染）

| 素材 | 中位 | 最大 |
|---|---|---|
| 4K h264（吉利调色1） | 1042.6ms | 5118.2ms（P1-6 瓦片池耗尽） |
| 1080p h264（五菱调色2） | 79.7ms | 109.4ms |
| 4K ProRes 4444（1.mov） | 911.2ms | 983.1ms |

---

## 四、建议实施顺序

| 批次 | 项 | 预期收益 | 风险 |
|---|---|---|---|
| 第 1 批（一行级修复） | P0-2 capability negotiation；P1-4 播种守卫；P1-5 paint zeros 修正 | 大半径效果 ~2×；老图不再 1×1；paint 每帧报错消失 | 极低 |
| 第 2 批（预览快速路径） | P0-1 按显存/目标尺寸选择整帧 GPU 或 GPU 内分块拼接 | 4K comp_clip 预览 ~500ms → ~25ms | 中（需验证 4K+ 超大图内存） |
| 第 3 批（正确性） | P0-3 AI 透传兜底；P2-9 deflicker 负帧；瓦片失败显式报错 | 消灭黑帧/静默降级 | 低 |
| 第 4 批（解码与直通） | P2-8 D3D11/DXVA 接入 video_frame；P1-7 comp_clip 纹理直通 | 4K 视频源 205ms → 硬解；预览再省一次上传 | 高（跨模块） |
| 第 5 批（CPU 节点 GPU 化） | deflicker GPU 时域窗口；RIFE 线性回退 shader 化；particle splat instancing；AI 读回延迟 | 4K 重灾区 3.3s/2.5s/0.8s → 数十 ms | 中 |

---

## 五、复现方式

```bash
# 1) 素材抽取（真实工程视频 → 帧序列）
#    ffmpeg -i "2620吉利调色1.mp4" -vf "select='eq(n,2)+eq(n,8)+eq(n,14)+eq(n,20)'" -fps_mode passthrough real4k_f%d.png
#    （1080p / ProRes EXR 同理，输出到 %TEMP%/topos_comp_bench/）

# 2) 逐节点基准（默认 GL3.3；--gl-require 460 出对照）
.venv/Scripts/python.exe scripts/bench_composite_nodes_gpu_coverage.py --res both

# 3) 读回微基准
.venv/Scripts/python.exe scripts/bench_composite_readback_micro.py

# 4) FLAT/None 输出复核
.venv/Scripts/python.exe scripts/bench_composite_flatcheck.py edge_detect transform matte
```

注：bench 的 "preview" 即 Edit 时间线 comp_clip 预览路径（srgb_u8），
"export" 即导出路径（linear_f32）；合成页面自身交互预览无读回，不在本表。

---

## 六、补充优化项与验收门槛

本节补强前述实测问题，确保局部修复能真正传递到 Edit 时间线和 Deliver，并在大工程、低显存、跨后端和后台任务并发时仍然成立。

### P0-4 统一 Composite 性能 trace 与逐段 GPU 时间

- 为每一个 `comp_clip` 帧使用稳定 `trace_id`，串起 graph build、source decode、CPU upload、GPU import、每个 node/pass、tile、readback、CPU stitch、cache、Edit composite、present 与 export encode；CPU wall time 与 GPU timestamp 分列记录。
- 每帧至少记录：frame/generation、quality、target size、backend/device、node/pass 数、整帧/瓦片决策、CPU/GPU 时间、CPU upload/download bytes、surface map、纹理/FBO/tile pool 峰值、队列深度、cache hit 与 fallback reason。
- 验收：同一帧从 Composite 到 Edit Viewer 或 encoder 可关联；没有 GPU timestamp 的后端必须标记 `gpu_time_unavailable`，不能填 0；任何“全 GPU”场景均可由 trace 证明无 CPU upload/download。

### P0-5 输出与跨上下文资源契约

- 新增 `CompositeGpuFrameHandle`（名称可调整，但语义必须固定）。它不是裸 GL texture name，而是带所有权和同步信息的帧句柄。
- 字段至少包含：backend/device 或 adapter identity、texture/surface、width/height、pixel format/bit depth、scene/display color space、range、straight/premult alpha、frame/timestamp/generation、producer fence、lease/release callback、可用性和 fallback reason。
- Consumer 未确认 fence 前 Producer 不得回收资源；generation 失效的句柄不能 present；上下文重建、设备丢失、跨 adapter、share-group 不成立时显式走复制或 CPU fallback，不能传递失效 texture id。
- 验收：随机 seek、关闭工程、GPU context reset、Viewer 重建和满队列压力后，lease 全部归零；没有旧帧闪回、提前释放、GL error 或无理由读回。

### P0-6 基准分层、统计纪律与像素语义

- `preview` 细分为 Composite 页面 GPU→GPU present、comp_clip→Edit GPU direct、comp_clip→CPU fallback、降分辨率 preview；不得混为一项“预览”。
- 每项报告 cold、warm、参数改变后的 partial miss、连续 10 分钟播放、随机 seek、与 proxy/cache/export/GIF/AI 并发六种状态；固定 warmup、样本帧数和重复次数，报告 p50/p95/p99/max 与资源峰值。
- 基准 artifact 必须带 commit、脚本版本、媒体 hash、机器、驱动、后端和环境变量；同机 p95 退化 ≥5% 或超出场景预算必须有原因、豁免或回滚。
- 预览 RGBA8/sRGB 只可位于显示边界，不能参与后续 linear 合成；cache key 必须包含格式、color space、range、HDR transfer 和 alpha 语义。8/10/12-bit、420/422/444、HDR、full/limited、straight/premult alpha 必须与 CPU reference 对拍。
- **强制真实视频门：** 每次修复 Composite、Comp Clip、缓存、GPU 互操作、解码或导出后，必须用 `tests/test_videos` 中受影响的真实视频复跑功能与性能；synthetic/golden fixture 只能补充，不能替代。结果有错误、黑帧、错帧、色彩/alpha/PTS 回归、未解释的 p95 退化或未执行真实视频测试时，该项不可标记完成。

### P0-7 TOPOS 优先的 Composite 缓存基准与默认策略

- **默认方向：** Composite 的可持久化磁盘缓存优先使用自研 TOPOS，而不是把通用中间编码当作性能基准。连续、密集的时间范围使用带 `TPIC` sample entry 的 Topos MOV；稀疏帧、局部重算、随机访问、定格/关键帧和需要逐帧原子发布的缓存使用 `.toos`（TPIM envelope）Topos 图片序列。
- 不对短生命周期的 GPU node cache、present slot、临时 tile 或同帧中间纹理编码落盘；它们保持 GPU resource pool。磁盘 cache 只保存跨播放、跨 seek、跨进程或交付可复用的结果。
- 将 Topos 视频与 TOOS 序列纳入所有 Composite 基准的**第一对照组**：先测 Topos，再与原始媒体、DNxHR/其他兼容中间格式或 CPU image sequence 比较。报告必须拆分 render、Topos 准备/编码、磁盘写入、打开、顺序/随机 decode、GPU import 与 cache hit，不能把 codec-only 数字冒充 comp_clip 体验。
- 每个 Topos cache artifact 写入 manifest：codec/profile、video/TOOS layout、width/height、fps/time range、frame numbering、input/graph revision、色彩元数据、alpha mode/bit depth、质量档、文件 hash、生成状态和 codec build identity。任意字段不匹配即失效，绝不错误命中。
- 验收：Topos MOV 和 TOOS 均以带 alpha、HDR/linear、8/10/12-bit、连续/稀疏帧场景通过 round-trip；每个项目给出体积、encode/decode p50/p95、seek、首帧、磁盘 I/O、CPU/GPU copy 和视觉/alpha 对拍数据。

### P1-8 图级脏区、ROI 与增量求值

- 在 render plan 中传播 node bbox/ROI、滤镜扩张半径和 transform 影响区；参数或上游输入变化时只失效受影响下游区域。需要全局信息的节点必须声明 full-frame/full-window 依赖，不能错误局部化。
- 对静态 node 输出、LUT、文本位图、mask、无参数变化 branch 和中间 composite 建立 GPU 缓存；key 包含输入 revision、参数、帧/时间、ROI、格式和 backend。
- 验收：局部 paint、文本、mask 点移动和 transform 的纹理写入面积/节点重算数明显低于全帧；ROI 边缘与全帧 reference 无接缝差异。

### P1-9 Pass 融合、pipeline cache 与资源池生命周期

- 对可安全组合的 color transform、简单 blend、geometry 和 point effect 做 pass fusion；不能改变精度、blend 顺序或采样语义。
- shader/program 编译、uniform layout、VAO/quad、FBO 和临时纹理全部移出播放帧，使用有界 cache、版本化失效和失败诊断。
- 实施图级 resource lifetime analysis：不重叠 transient 可以 alias；持久 cache、present slot、decode surface、tile、AI tensor 和 export surface 分预算。资源只在相关 GPU fence 完成后重用，release 前解绑。
- 验收：warm hit 与 partial miss 分开测量；GPU timestamp 有净收益；program/texture 创建次数不随播放线性增长；4K 多节点/双 comp_clip/并发 export 下无静默 tile 丢失和 VRAM 线性增长。

### P1-10 异步 upload/readback、预览输出与前台 QoS

- CPU 输入图像、图片序列和不支持零拷贝的 decode 使用有界 PBO/upload ring；只保留最新 generation 输入。CPU 旁路 readback 使用双/三缓冲 PBO，消费者取最新已完成帧，不同步等待当前帧。
- 统一优先级：`latest scrub > playhead > 即将播放 > comp_clip cache fill > thumbnail/GIF > proxy/render cache > AI/后台分析 > export`。所有 graph rebuild、tile、source decode、AI、PBO 和 cache 任务都携带 project/sequence/generation，可取消并释放 lease。
- 验收：同步 `read_into`、`glFinish` 和每帧临时 FBO/纹理创建有审计并持续下降；GPU direct 路径 readback count 为 0；连续 scrub 10 秒后队列在一个帧周期内收敛，播放启动后后台任务自动降权。

### P1-11 TOPOS Composite 缓存分层、Alpha 与 I/O 管线

- 新增 `CompositeCacheStorageProfile`（名称可调整）：`gpu_ephemeral`、`topos_video`、`toos_sequence` 三层由同一个 cache manifest 和失效图管理。默认磁盘 profile 为 TOPOS；仅在原生库不可用、用户显式选择兼容格式、或目标 profile/像素格式明确不支持时进入可诊断 fallback。
- 选择规则先以可测阈值冻结：连续帧段和顺序播放热点优先 `topos_video`；非连续帧、ROI/脏区重算、需要独立删除/重建的帧优先 `toos_sequence`。阈值必须由 P0-7 的实际 I/O、seek 和文件系统基准决定，不能拍脑袋固定。
- 两种格式都以 **保留 alpha 为默认语义**：当图中含 alpha 时，选用支持 alpha 的 Topos profile，并把 straight/premult、alpha bit depth 与合成色彩空间写入 manifest；不得因 cache layout 切换而压平透明度或把预乘语义改变。
- 写入采用临时目录/文件、帧级或段级校验、manifest 最后原子发布；取消、磁盘满、进程崩溃、缺帧、codec 版本变化和 graph revision 变化必须安全清理或重建。TOOS 序列缺帧策略须显式为 error/hold/skip，不能默默把旧帧当新帧。
- 验收：相同 Composite 图在 Topos MOV、TOOS、GPU warm cache 之间的帧、时间、色彩和 alpha 结果一致；随机 seek/局部失效只重建必要 TOOS 帧，连续播放优先命中视频段；cache 写入不会抢占 playhead 预算。

### P2-11 时间域、AI、Paint、Text 与 Particle

| 节点 | 补充优化 | 验收 |
|---|---|---|
| Deflicker / temporal | 声明前后帧窗口、temporal cache 与 warmup；GPU ring texture + compute/reduction 或经验证近似；cut/反放/seek/负帧/上游变化时重置 | 无跨镜头残影；实时与最终档分别报告资源和质量。 |
| RIFE 回退 | 缺模型时使用双纹理 GPU 线性混合；模型路径独立管理 model session、显存、batch、取消与交付质量 | 透传/回退不再全帧 CPU 往返；seek 后时间语义正确。 |
| AI | 在所有全帧读回之前命中带 revision 的 cache；模型/mask/input 不完整时零成本输入透传；低优先级且可取消 | 不黑帧；cache hit 不读回；模型版本变化不会误命中。 |
| Paint | 空笔画 passthrough；有笔画时 dirty tile/mask cache，长期评估 GPU stroke/SDF/instanced brush | 空状态零分配；局部笔画不触发整帧 QPainter/上传。 |
| Text | 字形/layout/位图按内容、样式、分辨率和 DPR 缓存；动画只更新变化属性 | 文本未变不栅格化；缩放和 alpha 语义正确。 |
| Particle | 确定性 seed、时间 checkpoint；splat 用 instanced point sprite 或 compute，CPU 保留小规模兼容回退 | 同帧/seed 可复现，preview/export 一致，seek 不漂移。 |

### P2-12 视频源、硬件解码和导出 surface

- `VideoFrameProviderService` 失败要区分路径、封装、codec、参数、硬件能力与临时错误；不能每帧退回 direct source 并刷日志。
- Windows 以 D3D11 surface 为视频 GPU 交付模型，接入前验证 decoder texture、renderer 和 encoder 的 adapter LUID 一致；跨 adapter 必须记录 copy 或拒绝零拷贝声明。macOS 的 VideoToolbox/CVPixelBuffer/IOSurface 和软件 decode upload 同样采用统一 GPU frame/lease 语义。
- 支持 GPU encode 的交付优先把 Composite 输出转成 NV12/P010 surface 并直接提交硬编；TOPOS 是 Composite 磁盘缓存的首选原生编码：连续 cache 段写 Topos MOV，独立帧 cache 写 TOOS。当前必须 CPU 可见的 Topos 编码路径使用池化纹理/FBO + 有界 PBO encode queue、优先 planar/alpha 直通，禁止同步 `ctx.finish()`；未来若 Topos encoder 接入 GPU surface，仍以同一 manifest/alpha/trace 契约替换该实现。
- 验收：H.264/H.265/ProRes/DNxHR/Topos、8/10-bit、alpha、硬/软解组合都有 capability 报告、实际 trace 与安全 fallback；输出帧数、PTS、HDR、alpha、range、metadata、取消和抽帧对拍全部通过。

### P3-13 并发、长跑、故障注入与用户可解释性

- 组合压力场景：4K 双 comp_clip + Edit 播放；播放 + Topos proxy；播放 + Topos MOV/TOOS Composite cache 生成；播放 + Deliver；5,000 素材媒体池 + 1,000 GIF；AI 分析 + scrub；低显存/低磁盘空间。
- 至少覆盖 30 分钟连续播放与 1 小时 cache/export 循环，记录 RAM、VRAM、句柄、线程、surface lease、队列、温度/降频（可用时）及 p99。
- 注入 GL/D3D device loss、context reset、PBO/encoder/decoder failure、磁盘满、模型缺失和取消；确保没有黑帧、死锁、半成品或资源泄漏。
- 建立按 node/backend 的能力表：`GPU direct`、`GPU with transfer`、`CPU fallback`、`unavailable`，并在 HUD/support bundle 显示 backend、source、cache、preview quality、fallback 和热点；不收集用户素材内容或路径。

## 七、跨团队并行实施边界

本专项可与 `topos_color_professional_nle_performance_master_plan_2026-09-15.md` 并行推进，但以下边界必须保持清晰。

| 工作包 | Composite 团队可独立推进 | 需要联合设计 / 唯一 DRI |
|---|---|---|
| 节点与局部性能 | P0-2、P0-3、P1-4、P1-5、P1-6、P1-8～P1-10、P2-11 | 改变 Edit/Export 输出格式、所有权或资源预算即转为联合项。 |
| 基准与测试 | 逐节点 timing、ROI、cache、精度、soak 基准与 golden tests | trace schema 必须接入总计划的统一 event schema。 |
| comp_clip GPU 直通 | 先实现 `CompositeGpuFrameHandle` producer 与契约测试 | P1-7 必须由 Edit/GPU 基础设施负责人统一 consumer、fence、present slot 与 fallback。 |
| Windows 视频链 | 补 `video_frame` contract 和测试素材 | P2-12 的 D3D11 decode→render→encode 由 Windows GPU 负责人统一 device、LUID、surface 和 trail。 |
| 导出与 TOPOS 缓存 | Composite 团队可实现 TOOS/MOV cache producer、manifest、原子发布和 PBO 兼容路径 | GPU encode surface、Deliver/Quick Export、Topos profile/plane 直通、全局 cache 复用由 export/codec 负责人统一。 |
| 系统资源与 UI | 上报资源、QoS 请求和 Composite 降级状态 | 全局 worker/VRAM/disk budget、GIF/thumbnail/AI/代理调度由性能平台负责人统一。 |

联合项开发规则：先以契约测试冻结 handle、event 和 fallback schema；共享 `src/features/edit/core/**`、`src/shared/gpu/**`、`src/shared/media/windows_hw/**`、`src/shared/export/**` 的项只保留一个实施分支/DRI，另一团队用 mock/adapter 开发；合并前同时跑 Composite golden、Edit 预览、Deliver、GPU trail 与 lease 测试；性能报告只能比较同一 commit、同一机器和同一素材集。

## 八、更新后的实施顺序与完成定义

| 批次 | 范围 | 合并前硬门 |
|---|---|---|
| 0：量化、契约与 TOPOS 基线 | P0-4～P0-7、`CompositeGpuFrameHandle` schema、基准协议 | trace/lease/precision contract tests 通过；Topos MOV/TOOS cache 基线可复现。 |
| 1：低风险正确性与能力 | P0-2、P0-3、P1-4、P1-5、Provider 错误分类 | 无黑帧、1×1、空 paint 分配或静默 tile skip；GL4.1/4.3+/fallback 都可用。 |
| 2：消除 4K 读回断点 | P0-1、P1-10 与基础显存预算 | 4K preview 的 GPU/CPU 段耗时、VRAM 和质量档可证；显存不足安全降级。 |
| 3：图增量和 GPU 直通 | P1-7、P1-8、P1-9 | Edit comp_clip direct 路径 readback count 为 0；seek/context reset/关闭工程无 lease 泄漏。 |
| 4：解码、TOPOS 缓存、时域和重节点 | P1-11、P2-11、P2-12 | Topos cache 的 alpha/色彩/seek/部分失效、硬/软解、temporal node 的正确性、资源预算和 fallback 矩阵通过。 |
| 5：交付和压力闭环 | P3-13 | GPU trail、输出验证、长跑/故障注入和并发交互预算通过。 |

一个条目只有同时满足以下条件才可标为完成：

1. 有 correctness、fallback、资源生命周期和性能测试；
2. 同机 cold/warm/long-run 的 p50、p95、p99、max 与资源峰值达到预算；
3. 对宣布的 GPU direct 路径，trace/`ExportGpuTrail` 证明 `cpu_upload_bytes == 0` 且 `cpu_download_bytes == 0`；未知必须明确标识；
4. 画质对拍覆盖色彩、HDR、range、alpha、边缘、时间域窗口和转场边界；Topos MOV 与 TOOS cache 不得改变 alpha/预乘语义或 metadata；
5. GPU 不可用、设备丢失、内存压力、模型缺失、取消和关闭工程均安全恢复；
6. 前台 Edit 播放不会因 cache、proxy、GIF、AI 或 export 并行而超过交互预算；
7. 结果 JSON、报告、commit、机器和驱动信息可追溯，用户界面的能力/降级说明同步更新。

---

## 九、Comp Clip 在 Edit 时间线流畅播放专项

### 9.1 当前热路径审计结论

Comp Clip 在 Composite 页面内可以 GPU→GPU 上屏，但进入 Edit 时间线后尚未继承这条路径。当前代码中存在以下已确认断点，必须先消除它们，才有资格把 Comp Clip 称为“实时可播放”。

| 编号 | 当前事实 | 性能/稳定性影响 | 优先修复方向 |
|---|---|---|---|
| TL-C01 | `PreviewRenderWorker._try_gpu_render()` 对 `comp_clip` / `composite_id` 明确返回 false；现有测试也把“GPU path skips comp_clip”当成旧行为 | Comp Clip 强制离开 Edit GPU realtime 路径并进入 CPU preview fallback | 把该测试改成迁移门：已支持的 Comp Clip 必须生成 GPU handle 并提交 `TimelineGpuCompositor`；不支持项由 engine hold/rebuffer，而不是静默 CPU 合成。 |
| TL-C02 | `CompositeFrameRenderer.render_comp_clip_frame()` 固定调用 `render_and_readback()`，返回 NumPy；preview 是 sRGB uint8 readback | GPU 求值后立即读回 CPU，Edit 再上传为轨道纹理，4K 成本和 P0-1 的瓦片问题叠加 | 新增不读回的 GPU 输出 API，返回 P0-5 的 `CompositeGpuFrameHandle`；CPU image 仅保留缩略图、诊断和明确 fallback。 |
| TL-C03 | Preview 为避免纹理累计会逐帧 `CacheManager.clear()` | 每帧清掉静态 branch、LUT、mask、文本和中间结果，无法实现连续播放的 GPU warm cache | 以 graph revision、frame dependency、ROI 与显存预算驱动选择性缓存/驱逐，禁止每帧全清。 |
| TL-C04 | `_comp_clip_cache` 只按 60 个 NumPy 帧计数，key 主要是 graph/frame/caption；不是字节级真 LRU | 4K RGB/BGRA 帧可快速占用数 GB；cache hit 不移动顺序；图 revision/质量/输出规格变化需要更严格键 | 替换为 GPU frame cache + byte-budgeted CPU fallback + TOPOS disk cache 三层；cache key/manifest 含 graph revision、quality、size、color/alpha、时间映射。 |
| TL-C05 | Comp Clip 通过独立共享 GL context，使用全局 GL mutex，并在每帧 makeCurrent/doneCurrent | Comp 求值可能串行阻塞其他 Edit GPU 工作；上下文切换和竞争引发卡顿尖峰 | 由统一的 GPU render scheduler / shared command lane 调度；直接 texture 路径与 Edit present slot 采用 fence/lease，不在播放帧反复创建或切换上下文。 |
| TL-C06 | 已移除“跨 context 直接复用纹理名”的 blanket disable；Timeline input provider 现在只对带 `macos_vt_pixel_buffer` 标记的延迟 CVPixelBuffer 启用当前 Composite GL context 导入 | VT 硬解帧可在 Comp Clip graph 前完成 IOSurface→GL plane→GPU YUV→RGB，避免回到 CPU 平面上传；不支持的 backend/格式仍需安全回退 | 继续用 P0-5 resource contract 做真实 viewer/硬件对拍；保持桥接/着色器 capability gate、GL fence lease 和有界 CPU/planar fallback，禁止接受未声明 context sharing 的外部 texture name。 |
| TL-C07 | Comp Clip preview 没有面向 viewer 尺寸/deadline 的显式输出规格和专用预取队列 | 小窗口仍可能按 graph 原尺寸求值；下一帧不能提前准备，重节点造成连续 miss | 将输出尺寸、quality、deadline、播放方向和 generation 纳入 Comp Clip render request，复用 realtime frame ring / deadline 调度。 |

### 9.2 目标播放链路

```text
Edit playback engine
  └─ CompClipScheduler (generation + deadline + direction + QoS)
       ├─ GPU warm cache / Topos disk cache hit ──────────┐
       ├─ Comp graph incremental evaluation (GPU)         │
       │    └─ source frames: external GPU / bounded upload│
       └─ CompositeGpuFrameHandle + fence + lease ────────┤
                                                          ▼
TimelineGpuCompositor → EditGpuPreviewRenderer → present slot → Viewer
```

实时模式的正常路径不得生成 `QImage` 或 NumPy 全帧。GPU miss 由播放 engine 按 deadline
决定 hold、rebuffer、降低预览档或读取已经完成的 TOPOS cache；只有用户明确选择 CPU
fallback 或当前 backend 不支持时才走 CPU，而且 HUD 必须显示原因。

### 9.3 新增工作项

| ID | 工作项 | 实施要点 | 验收 |
|---|---|---|---|
| TL-P0-01 | 建立 Comp Clip 时间线基线 | 冻结单/双 Comp Clip、1080p/2K/4K、静态/动画/视频源/alpha/temporal 节点、冷/热/seek 的项目矩阵；沿 TL-C01～C07 分段采样 | 每个场景报告首帧、稳态 fps、p50/p95/p99、GPU/CPU 时间、copy bytes、队列、VRAM/RAM、cache hit 与 fallback。 |
| TL-P0-02 | GPU direct 契约测试 | 以 `CompositeGpuFrameHandle` 测 producer/consumer、颜色/alpha、generation、fence、lease、device/context reset 和关闭工程 | 已支持 backend 的 comp_clip 不触发 readback/upload；未支持时有明确 fallback reason，不出现旧帧或提前释放。 |
| TL-P0-03 | 迁移旧 GPU-skip 测试 | 将“Comp Clip GPU path 必然跳过”的测试替换为 capability-gated 期望；保留 CPU fallback 测试但不再把它当成功路径 | GPU 可用样例提交至 `TimelineGpuCompositor`，其 `readback_count` 与 `cpu_qimage_count` 都为 0。 |
| TL-P1-01 | Comp Clip GPU 输出 API | `CompositeFrameRenderer.render_comp_clip_gpu()` 只渲染/返回 GPU handle；`TimelineClipFrameProvider` 在当前 Composite GL 线程导入延迟 VT/CVPixelBuffer，并用 GPU YUV→RGB pass 生成 graph 输入；CPU API 继续服务导出/缩略图/诊断 | 合成片段进入 Edit 的 GPU 入口；仍需用真实 viewer、有效 Composite graph 和 `tests/test_videos` 证明 4K 连续播放没有 CPU readback/upload，并完成 alpha/色彩对拍。 |
| TL-P1-02 | 统一 GPU 调度和上下文所有权 | 用 shared render lane/command queue 取代播放帧中的独立 context 切换与粗粒度全局 mutex；保留兼容后端 adapter | Comp Clip、普通视频、转场和 present 不互相死锁；GPU queue/fence 等待可量化，p99 不出现由 context thrash 导致的尖峰。 |
| TL-P1-03 | 编译与输入复用 | GraphSnapshot、TimelineClipFrameProvider、decoder session、外部 GPU source frame 在 graph revision 未变时跨帧复用；输入帧随 generation/lease 生命周期释放 | 同一 Comp Clip 连续 120 帧不重复反序列化图、不重开 decode session、不重复 upload 静态输入。 |
| TL-P1-04 | 依赖感知 GPU node cache | 静态 branch、LUT、text、mask 与静态输入保持 cache；frame-dependent/temporal node 按窗口缓存；按 VRAM byte budget LRU 和 fence 回收，替代每帧全清；调度器与评估器必须共享同一帧依赖策略 | 热播放的静态图实际 node GPU time 与纹理分配次数下降；编辑某个节点只失效其下游。 |
| TL-P1-05 | Comp Clip 专用 deadline/预取 | 根据播放方向预渲当前后 2–6 帧（由帧预算与节点成本自适应）；scrub 只保留最新帧；cache fill 只能在 playhead 有余量时执行 | 25/30fps 场景下一帧 ready rate、miss 和 rebuffer 可报告；连续 seek 后过期任务一个帧周期内取消。 |
| TL-P1-06 | 自适应预览 quality ladder | render request 显式携带 viewer target size、Full/Half/Quarter/Auto、deadline 和质量档；依节点成本选择 output scale、blur/denoise/AI temporal 档或已完成 cache | 降级动作可见且可逆；不改变最终 Deliver；画质、输出尺寸、色彩与 alpha 语义写入 handle/cache key。 |
| TL-P1-07 | 多 Comp Clip 图层和转场 | 同帧多个 Comp Clip 先各自产生 GPU handle，再由时间线 GPU compositor 做一次统一 blend/transition；不得分别 present 或读回 | 双 Comp Clip、Comp Clip 与普通视频、Comp Clip 转场保持一帧一次 present，alpha/混合/转场边界对拍通过。 |
| TL-P2-01 | TOPOS Comp Clip playback cache | 复用 P1-11：连续热点片段预渲成 Topos MOV，非连续/局部失效帧写 TOOS；engine 优先消费已完成 cache，后台生成遵循 QoS | graph revision 未变时第二次播放 cache hit；局部编辑只重建必要 TOOS 帧；Topos cache 带 alpha，且不会抢占前台播放。 |
| TL-P2-02 | Cache 分层与内存上限 | GPU warm cache（帧/节点）、byte-budgeted CPU fallback cache、Topos disk cache 三层都有独立预算、hit/miss/eviction 指标和优先级；GPU 资源池预算必须统计 active、pooled 与 fence 延迟释放的驻留字节 | 4K 60 帧不再因按“帧数”而无界占内存；内存压力下先驱逐低优先级，再降档/重建，不能 OOM。 |
| TL-P2-03 | 时间映射与时域正确性 | Comp Clip 的 source/timeline frame、变速、倒放、freeze、嵌套时间线、caption/关键帧输入都进入 cache key 和 temporal reset 规则 | 持续播放、跳转、倒放和编辑后无错误 cache 命中、错帧、跨镜头残影或音画漂移。 |
| TL-P2-04 | 透明/颜色专业路径 | 让 external GPU frame、Topos MOV、TOOS、GPU handle 和 timeline blend 共同消费一份 color/alpha contract | HDR、10/12-bit、premult/straight、透明边缘、matte 和转场在 preview/cache/export 一致。 |
| TL-P3-01 | 交互与并发保护 | Comp Clip 预渲/Topos cache/AI/缩略图/GIF/Deliver 都走全局 QoS；最新 scrub/playhead 始终优先 | 同时生成 cache、滚动 Effects、后台 AI 和 Deliver 时，Comp Clip 播放交互 p95 保持预算内。 |
| TL-P3-02 | 长跑与故障注入 | 跑 30 分钟多 Comp Clip 播放、1 小时 cache 循环；注入 context/device reset、cache 损坏、Topos 库不可用、磁盘满、取消、低显存 | 不黑帧、不死锁、不残留半成品/lease；恢复后有可诊断状态和安全 fallback。 |

### 9.4 推荐推进顺序

1. **先测与改断点：** TL-P0-01～03，随后 TL-P1-01。先消除 GPU skip 与强制 readback，才能验证后续缓存的真实收益。
2. **再稳定连续播放：** TL-P1-02～07。统一 GPU lane、静态/时域缓存、预取和 Auto quality，保证正常播放不会堆积。
3. **再让第二次播放更快：** TL-P2-01～04。TOPOS MOV/TOOS 作为 Comp Clip 持久缓存，按连续性和局部失效选择格式。
4. **最后压测：** TL-P3-01～02。只有在系统后台任务与故障场景下仍满足 p95/p99，才把 Comp Clip 设为默认 GPU realtime 能力。

### 9.5 时间线 Comp Clip 的完成定义

- 目标 GPU/backend 上，单和双 Comp Clip 不再触发 CPU 全帧读回/重传，`cpu_upload_bytes == 0`、`cpu_download_bytes == 0`，并有 fence/lease trace 佐证。
- 25/30fps 的目标测试工程稳定满足帧预算；超预算时是可解释的 hold/rebuffer/quality action，绝不静默退到 CPU 全帧合成。
- 图、缓存、播放、TOPOS MOV/TOOS 和 Deliver 在同一 revision 下帧号、时间映射、色彩、HDR、alpha 和转场边界一致。
- Comp Clip 编辑后只使依赖节点、帧范围与缓存失效；静态内容不因播放帧号变化重复完整计算。
- 多 Comp Clip、普通视频、标题/字幕、转场、代理、GIF、AI 和后台导出并发时，前台播放与输入响应仍符合总计划的 p95 预算。

### 9.6 `tests/test_videos` 真实视频强制复验协议

每个功能修复完成后，实施团队必须在**同一提交、同一目标机**上执行与变更相匹配的真实视频回归；不能只跑 mock、单元测试或从视频抽取的静态 PNG。测试素材根目录固定为 `tests/test_videos`，媒体属性以 `topos_test_media_manifest.json` 为准。

| 变更范围 | 必跑真实视频最小集 | 必须验证 |
|---|---|---|
| Comp Clip GPU 直通、timeline GPU、转场/混合 | 至少各一条 2K/4K H.264/H.265，加一条 Topos MOV；有 alpha 的图必须再加 manifest 标记的 Topos/ProRes 4444 样本 | 播放、seek、暂停/恢复、双层/转场、GPU trace、readback/upload bytes、frame/alpha/色彩对拍。 |
| 视频输入、硬/软解、GPU import | 2K/4K H.264、H.265、ProRes、DNxHR 与 Topos 的受影响规格 | decoder/backend/fallback、首帧、顺序/随机 seek、10 分钟播放、错误日志、资源峰值。 |
| TOPOS MOV/TOOS cache、编码或 manifest | `2K_topos*`、`4K_topos*`，以及按 manifest 选择的 alpha/high-bit-depth 对照素材 | cache 冷/热/局部失效、写入/取消/重开、顺序与随机读、色彩/alpha/PTS/metadata、体积和 I/O。 |
| Composite 节点/时域/AI/ROI | 至少一个 2K 和一个 4K 真实视频作为 graph source，另加含透明素材的项目 | 正常帧、cut/倒放/seek、无黑帧、时间映射、画质对拍、p50/p95/p99、VRAM/RAM/queue。 |
| 导出/Deliver | 受影响输入规格 + Topos cache 命中和未命中两种工程 | 输出帧数、PTS、音频时长、HDR/range、alpha、抽帧对拍、GPU trail、取消和原子文件语义。 |

每次复验的结果必须生成可追溯 artifact（JSON + 简短 Markdown）：commit、测试命令、素材相对路径与 manifest/hash、机器/驱动/backend、冷/热/seek/长跑 p50-p95-p99-max、CPU/GPU 时间、RAM/VRAM、cache hit、CPU upload/download bytes、fallback/error。与最近同机基线比较；发现错误先修功能，性能 p95 退化 ≥5% 或超过预算时必须定位、获准豁免或回滚。由于硬件/codec 不可用而无法执行时，报告必须写 `skipped` 和具体原因，且不得宣称该组合已经通过。

## 十、当前实现进度（持续更新）

本节记录已经落地的缓存/播放基础设施，避免把“契约或测试已具备”误写成“真实素材全链路完成”。

最近批次：`3ab8825c` 修复源帧纹理缓存命中不刷新淘汰顺序的问题，`TimelineClipFrameProvider` 现在对顺序播放、倒放和拖拽复用统一维护真正的 LRU；帧提供器回归 60 项、`tests/test_videos` 真实素材门 11 项通过。此前 `6eff1b68` 已补齐缓存条目对 depth/AOV 辅助 GPU 纹理的实际释放；结合 `fab35e94` 后，预算统计与淘汰释放都统一覆盖 active、pooled、deferred release 以及缓存条目拥有的主纹理/depth/AOV。TL-P1-03 的静态节点/纹理分层与真实 4K/多消费者统计、TL-P2-02 的真实 4K 长跑/低显存/并发压测仍待执行。

| 项目 | 状态 | 已落地内容 | 尚未完成的硬门 |
|---|---|---|---|
| P2-18 语义指纹 | ✅ 第一阶段 | 指纹覆盖时间线输出规格、片段/效果、合成图、调色栈、源文件版本和缓存设置；状态查询、缓存创建与 Motion Graphic 读取会核对 | 复杂多依赖图的增量失效、真实视频/Topos/TOOS 生产端贯通、跨进程重开实测 |
| P2-18 撤销/重做复用 | ✅ 基础能力 | 带指纹的 VALID 历史版本按当前指纹精确选择；编辑后不会误命中，撤销回原状态可重新使用旧缓存 | 与 TOOS 局部帧缓存、时间域窗口、跨 timeline duplicate 的共享策略联合验收 |
| P2-18 版本边界 | ✅ 基础能力 | 每个时间线/片段最多保留 8 个历史 manifest；淘汰记录会由控制器检查引用后清理对应文件；旧无指纹项目保持兼容 | 依据真实磁盘/播放基准调整上限，并加入全局磁盘配额和后台 GC 压测 |
| TL-P2-02 CPU fallback 帧缓存 | ✅ 基础能力 | Comp Clip 的 CPU 兼容帧缓存改为 OrderedDict 真 LRU，并以 256 MiB 字节预算 + 60 帧数量上限双重约束；命中会刷新淘汰顺序，清理同步维护字节计数 | GPU direct 与 Topos/TOOS 三层缓存仍需统一预算、压力注入和真实 4K 长跑验证 |
| TL-P2-03 Comp Clip 时间映射键 | ✅ 基础能力 | CPU fallback cache key 纳入 source frame、timeline frame、caption 输入和 graph 输出尺寸，避免移动/变速/时间关键帧复用错帧 | 倒放、freeze、嵌套时间线和 temporal window reset 仍需与 GPU/Topos/TOOS 路径联合验收 |
| TL-P1-05 实时预取与前台 QoS | ✅ 基础能力 | 同一媒体源游标按 clip 串行访问；前台精确解码优先于后台预取；预取请求 latest-only、方向变化可取消；seek、source switch、配置变化会以 generation/取消事件阻止旧帧回填 | 仍需接入真实硬解/Topos 解码，测量 25/30/60fps 的首帧、ready rate、miss、rebuffer 与 p95；全局 QoS 还需与 cache/GIF/AI/Deliver 联合压测 |
| RP-P1-14 播放预算恢复 | ✅ 基础能力 | back-pressure 期间保留真正的 drop 间隔，再周期性放行探测帧；探测帧回到预算内可自动解除压力，避免阈值为 1 时每帧忙循环或永久冻结 | 仍需按真实 25/30/60fps 工程校准探测间隔，并把 decode/upload/effect/present 分段耗时纳入预算决策 |
| TL-P1-01 统一 GPU 输出入口 | ✅ 接入阶段 | `CompositeGraphRenderService.render_gpu()` 已接入 `CompositeFrameRenderer.render_comp_clip_gpu()`，返回带 lease/fence/颜色与 alpha 契约的 `CompositeGpuFrameHandle`；`TimelineClipFrameProvider` 已在 Composite 当前 GL 线程完成延迟 VT/CVPixelBuffer 导入与 GPU YUV→RGB，源表面在 GL fence 后释放；不可用结果保留 fallback reason 并写入 telemetry | 仍需把真实 viewer/共享 context consumer 接入并以 `tests/test_videos` + 有效 Composite graph 验证跨 context fence、lease、readback/upload bytes 与实际 FPS；当前 192 项合成/时间线/VT 回归和真实素材门 11 项未覆盖完整硬件零拷贝性能结论 |
| TL-P1-02 GPU 上下文所有权 | ✅ 基础修正 | 多轨 GPU 解码仍保留普通媒体并行；Comp Clip GPU producer 不再进入通用解码线程池，改在持有共享 `QOpenGLContext` 的 worker 线程执行；同一多层解码批次只获取一次 context lease，批内 producer 复用当前 context；Edit GPU presenter 的每个 task 也加入同一 shared GPU command lane，结束后统一释放；呈现队列采用最新交互帧优先，后台 cache-fill 只能合并/拒绝，不能淘汰交互任务，并在淘汰时正确平衡 queue task 计数和源帧 lease；seek/项目切换会推进 renderer generation，立即清理等待队列中的旧代任务，提交前与执行前再次拦截竞态旧帧，避免旧源帧 lease 进入 GL；播放 deadline 已从 engine 贯通至 `EditRenderTask`/compositor/backend，提交前与取队列后均拒绝过期任务并释放资源，累计 `deadline_drops` 可观测 | 仍需把跨帧 shared render lane/command queue 的长期持有策略升级为可观测的统一 scheduler（统一优先级、取消和 deadline 仲裁），并在真实多 Comp Clip 场景测量 context switch、fence wait 与 p99；正在执行中的旧 task 仍需依靠 generation-tagged output 在呈现层丢弃 |
| TL-P1-03 跨帧输入复用与请求合并 | ✅ 部分落地 | `CompositeFrameRenderer` 已跨帧复用 `GraphSnapshot`、`TimelineClipFrameProvider` 与视频会话；`VideoFrameProviderService.request_frame()` 在会话锁内二次检查缓存，同一素材同一帧的并发 miss 只保留一次 seek/decode；`prefetch()` 逐帧复查缓存，避免前台先填缓存后被旧 miss 列表再次解码；`TimelineClipFrameProvider` 的源帧纹理缓存命中会刷新 MRU 顺序，顺序播放、倒放与拖拽不会因旧插入顺序提前淘汰热帧；`NodeGraph.to_dict()` 输出 `graph_revision`/`topology_revision`，快照缓存以廉价 revision token 判断原地更新，旧 payload 回退 `modified_at`，不在每帧深哈希整张图 | 仍需把静态节点/纹理 cache 按 graph revision 与依赖关系进一步细分，并完成真实 4K/多消费者 120 帧分配次数、decode 次数与 GPU copy 统计 |
| TL-P1-04 帧依赖策略一致性 | ✅ 基础修正 | `RenderScheduler._snapshot_node_needs_frame()` 已与评估器对齐：时间范围、表达式、关键帧、能力表声明、动态源以及节点类 `params_require_frame_cache()` 均会触发脏节点；覆盖逐字字幕动画和时间范围边界回归测试，避免 dirty-skip 在进入精确 cache key 前复用旧帧 | 依赖感知 GPU node cache 的 VRAM/fence 分层、temporal 窗口和真实 4K/长跑收益仍未完成；需继续按 TL-P1-04 验收 |
| TL-P2-02 GPU 驻留显存预算 | ✅ 基础修正 | `GPUResourcePool._check_budget()` 不再只看 active texture；新增 resident bytes，统一纳入 active、pooled 复用纹理和 fence 未完成的 deferred release；统计面板同步显示驻留 MiB 与真实预算占用，防止连续播放/分辨率切换时池内纹理绕过预算累积 | 仍需与 GPU warm cache、CPU fallback、Topos/TOOS 三层共享预算，并完成真实 4K 长跑、低显存和并发压力验证 |
| TL-P2-02 缓存辅助通道释放 | ✅ 基础修正 | `CacheManager._release_entry()` 现在按 `owned_texture_ids` 权威集合逐一归还主颜色、depth 和 AOV 纹理，并去重/过滤无效 ID；修复“预算已计入但淘汰后仍驻留”的 GPU 泄漏，覆盖 deep compositing 与 Scene3D 多通道结果 | 仍需在真实 3D/AOV 长跑与 context reset 场景核对驱动显存曲线、fence 回收和跨层预算 |
| T-03 平面缓存直通 | ✅ 接口阶段 | `DecodedFrame` 平面结果可直接交给 Topos 编码器；尺寸不符会显式失败，packed BGR 仍走兼容路径 | 当前 headless Comp Clip 生产端仍主要输出 BGR；需用 `tests/test_videos` 真实素材完成平面产出与 encode/decode 基准 |
| Comp Clip GPU 直通 | ✅ 契约/接入阶段 | GPU handle、调度、合成片段消费和状态桥接已有分批提交；延迟 VT 帧不再被 provider 直接丢弃，而是在当前 Composite GL context 经 IOSurface bridge 和 GPU YUV→RGB pass 进入 graph；导入失败会释放源帧并回退 | 尚未满足“Edit 时间线真实播放 readback/upload 为 0”的完成定义；需用真实 Composite graph + `tests/test_videos` 验证硬解导入、fence/lease、画面/色彩/alpha 和实际 FPS |

本节对应提交：`c1f19b86`（平面编码接口）、`99e0c2da`（语义指纹）、`822bd030`（撤销/重做历史版本复用与有界保留）、`3f4736e5`（Comp Clip CPU fallback 字节预算 LRU）、`c9efa0ae`（Comp Clip 时间映射键）、`3107a22d`（前台优先解码、可取消预取与 generation 守卫）、`db60e22a`（可恢复的播放预算 back-pressure）、`26032dff`（统一 Composite GPU 输出入口）、`5fb0c893`（GPU service asset context 透传修正）、`e78ece4d`（并发同帧 decode miss 合并）、`fbe31379`（预取逐帧复查缓存）、`35a89b30`（Comp Clip GPU producer 线程归属修正）、`928fa97f`（批内复用 Comp Clip GPU context lease）、`97e1a66d`（Edit GPU presenter 纳入 shared GPU command lane）、`e9373be5`（GPU 呈现队列最新帧合并与 QoS）、`9003e17e`（renderer generation fence 与旧代 GPU 队列淘汰）、`ead7eabf`（graph revision-aware 快照复用）、`45b450a2`（GPU deadline 贯通与过期任务淘汰）、`0cc9e375`（Comp Clip 延迟 VT/CVPixelBuffer 在当前 GL 线程导入与 GPU YUV→RGB）、`2720255f`（调度器与评估器的帧依赖缓存策略对齐）、`6eff1b68`（缓存淘汰释放主纹理、depth 与 AOV 辅助纹理）、`3ab8825c`（源帧纹理缓存命中刷新 LRU）。本批帧提供器回归 60 项和真实素材门 11 项通过；此前本轮缓存/资源池/合成关键路径 98 项、合成/时间线/VT 回归 192 项也已通过。该真实门覆盖素材打开、解码、source policy、seek 和 AV sync，但尚未覆盖有效 Composite graph 的硬解 GPU 导入与零拷贝性能，因此不能替代第 9.6 节完整性能结论。

## 十一、隐藏缺陷复查批次（2026-09-16，分支 fix-ui）

对上述全部已落地批次做了一次交叉审查（近 40 个提交的主审 + 两路独立深读：缓存体系、调度/锁序），修复以下被既有测试与文档记录掩盖的缺陷。除注明外均有新增回归测试锁定。

| 修复 | 严重性 | 根因与修复 | 回归测试 |
|---|---|---|---|
| `ExternalYuvToRgbPass` 双 sampler 未指派纹理单元 | critical（错色） | `0cc9e375` 的 GPU YUV→RGB pass 只把平面 `glBindTexture` 到单元 0/1，从未写 sampler uniform；ModernGL sampler 默认全 0，`u_uv_plane` 实际采样 Y 平面，pass "成功"但色度全错。且 `u_uv_component_mode` 检查的是 Y 平面的 `GL_LUMINANCE_ALPHA`（该格式属于 UV 平面，见 bridge 平面格式表），legacy LA profile 下 V 通道取到 U。修复：对齐 Edit 渲染器既有写法（moderngl 不写 sampler2DRect uniform，必须原生 `glUniform1i`），LA 判断改读 UV 平面 | `tests/video/test_external_yuv_renderer.py` 3 项新增 |
| source 类节点缓存条目泄漏 IDT/裁剪纹理 | critical（显存无界增长） | `timeline_clip_input` 等不在 `CACHE_OWNED_TYPES`，`_collect_owned_texture_ids` 返回空集，但评估器对 source 输出每帧新分配 IDT 线性化与 tile/ROI 裁剪池纹理；帧键条目被 `retain_frame` 逐帧淘汰时 `_release_entry` 释放 0 个纹理（1080p RGBA16F 约 16MB/帧/源节点，播放期间单调增长）。修复：`base_evaluator` 在裁剪/IDT 前捕获输出纹理基线，把"新出现的纹理 id"差集并入 entry 的 owned 集合（与 bypass 分支 `_transient_texture_ids_to_release` 同一口径）；provider 源纹理不在差集内，不会被误归还 | 既有 tile/IDT 套件回归 |
| GPU 池预算检查先于池复用 | major（稳态性能回归） | `fab35e94` 把 `_check_budget` 改为 resident 口径后仍在复用尝试**之前**执行：播放稳态 resident 常已贴预算，成本淘汰恰好销毁可复用的同尺寸大纹理，随后必走同尺寸新建（1080p f2 单次 10-16ms，每帧徒增）。修复：复用（不增加驻留字节）提前，仅新建路径检查预算并淘汰 | `tests/video/test_gpu_resource_pool.py` 新增"贴预算复用不逐出"用例 |
| 前台缓存命中被预取解码阻塞 | major（优先级反转回归） | `e78ece4d` 把命中检查收进 `decode_lock` 后，"本可立即命中"的前台请求要排在持锁中的后台预取解码之后（最坏一整次 master 解码）。修复：①命中快路径回锁外（`dict.get` GIL 下原子）；②`ClipSession.decode_waiters` 计数 + 预取每帧解码前检查让路（进行中的一帧有界完成，后续帧终止本轮扫描） | `tests/media/test_video_frame_provider_service.py` 2 项新增 |
| RuntimeError 兜底泄漏 GPU 租约 | major | `35a89b30` 后 comp job 内联解码先产出结果，executor `submit/result` 抛 RuntimeError 时旧兜底整表重解，已产出的 comp GPU handle/VT 缓冲租约既不释放也不消费（泄漏 + comp 图双重解码）。修复：兜底前逐个 `release_layer_data` 归还 ok=True 的结果再串行重解 | 既有 comp 套件回归 |
| 指纹校验击穿 MG 复制片段共享缓存 | major | `99e0c2da` 后共享 manifest（同场景另一 ClipInstance 录制）携带原 clip 的指纹，与本 clip 必然不等，`_get_motion_graphic_static_frame` 的共享回退对带指纹 manifest 恒失效，复制 MG 片段退回慢路径。修复：与 `render_cache_status` 一致加 `manifest_is_shared` 豁免 | 既有 MG 缓存套件回归 |
| UI 线程 `clear_source_cache` 竞态 | major（pre-existing） | 素材切换时 UI 线程直接清 `_source_texture_cache/_source_cache_order` 并 `tex.release()`：与渲染线程 LRU 变更交错可产生漏项/重复项（击穿 `_SOURCE_CACHE_MAX` 预算），且 ModernGL 释放在非 GL 线程属未定义行为。修复：provider 记录 GL 线程身份，跨线程 clear 挂起为标志、由渲染线程入口（`get_timeline_clip_texture`）drain；同线程调用行为不变 | 既有 provider 60 项回归 |
| `prepare_window_async` 同请求早返回破坏 latest-only | minor | A→B→A 时第二次 A 命中"同请求早返回"，拿到的是已被 B 置 cancel、注定 False 的 A future，worker 实际执行更旧的 B，最新意图被静默丢弃。修复：cancel 已置位时不再早返回，落入 pending 替换逻辑 | `tests/edit/test_timeline_decode_scheduler.py` 1 项新增 |
| prefetch 尾部 `evict_old_frames` 锁外竞态 | minor | `sorted(cache.keys())` 与锁内插入并发可抛 `dictionary changed size during iteration`。修复：移入 `decode_lock` 内执行 | 同批 provider 测试 |
| `GLContextManager.instance()` 无锁懒初始化 | minor | `97e1a66d` 后渲染线程与 UI/GL 生产者线程可能并发首触，双实例会把全局 GPU lane 拆成两把锁。修复：双重检查加锁 | — |
| `get_available_budget_mb` 口径不一致 | minor | 仍用 active-only，与 resident 准入口径矛盾（当前无调用方）。修复：对齐 resident | — |
| MasterLimiter 对拍测试恒失败 | test-bug | NumPy 2 弱提升：`float / float32 数组` 从 float64 变 float32，测试参考的增益递归被 `np.float32` 标量拖入 float32 精度，1e-9 对拍恒失败（实现本身显式 float64 更精确，非 DSP 回归）。修复：参考显式 float64 + 增益状态 <1e-9、输出 <1e-6 双断言 | `tests/edit/test_audio_dsp_vectorization.py` |

已知未修（记录在案，均非本批回归）：①`97e1a66d` 整帧 presenter 持全局 GL lane 属既定权衡，统一 scheduler 归 TL-P1-02 剩余项；②`test_deflicker_real_video_gate` 全量运行段错误、`test_regression_image_video` 独立崩溃、`test_audio_waveform` 全量偶发崩溃与 `test_workspace_presets` 尾部 `FF.F`+SIGABRT（2026-09-16 P1-05 批全量对拍时新观察）均在干净 HEAD 同位复现（Qt 对象析构竞态，单跑通过或与顺序无关），与性能批次无关，另行立项；③`_fingerprint_value` 的 `repr` 兜底遇不可序列化对象会产生含地址的非确定指纹（条件触发，暂无生产命中）；④`RenderCacheManifest.is_valid_for(render_fingerprint=)` 无生产调用方，校验逻辑三处内联（重构项）。

边界与诚实声明：sampler 修复消除了错色的确定性根因，但"硬解导入色彩正确"仍需按 §9.6 用真实素材实机对拍后方可计入零拷贝验收；source 节点 owned 差集修复了 IDT/裁剪两类已证泄漏，`image_input` 等自管源节点的基础渲染纹理归属未在本批重审。
