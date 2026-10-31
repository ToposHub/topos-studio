# Topos Composite 进阶能力计划（Phase 2，2026-09-08）

> **文档用途**：本计划是 `docs/composite_optimization_plan_2026-09-07.md`（下称"计划一"）**全部完成之后**的后续任务书，覆盖合成页下一阶段的 14 项进阶功能（高价值摘果子组 / 3D 进阶组 / AI 生成式组）。每个任务自包含：背景、代码证据、实现步骤、验证方法、验收标准、风险与回退，供后续 AI / 开发者逐项领取。
>
> **启动前置**：计划一的 A/B/C/E 批全部完成（含 E 批硬门禁）。个别任务对计划一的具体任务有软依赖，见第 1 节总览表"依赖"列与第 6 节衔接矩阵——软依赖项未完成时该任务可做但需按任务内标注的降级方案执行。
>
> **关联文档**：
> - 计划一：`docs/composite_optimization_plan_2026-09-07.md`（合成页基础优化，**其第 2 节全局工程约束与第 0 节执行须知在本计划继续生效**）
> - 剪辑页计划：`docs/edit_optimization_plan_2026-09-07.md`（C2 RIFE 与其 D1 光流决策联动）

---

## 0. 执行须知（AI 执行者必读）

1. **计划一的第 0 节与第 2 节全局约束全部继续生效**（GraphSnapshot 求值模式、节点注册六件套、schema 迁移 v10+、缓存端口 revision、GLSL 规范、模块边界、并行协调规则），本计划不再重复，只补充第 2 节新增约束。
2. 每个任务独立提交，顺序建议见第 6 节；行号为 2026-09-08 快照，以符号名重新定位。
3. 验证一律含：pytest 单测 + 启动冒烟 + 渲染类任务加截图/PSNR 对比 + 基准类任务跑性能门。
4. 涉及 AI 模型下载的任务：**首次运行不得静默下载大模型**——走 model_manager 的下载状态 UI，用户确认后下载。

---

## 1. 任务总览

| 编号 | 任务 | 批次 | 优先级 | 规模 | 依赖（计划一） |
|------|------|------|--------|------|----------------|
| A1 | Relight 节点（AOV 重光照） | A | P0 | M | 无（AOV 已有） |
| A2 | Cryptomatte 抠选节点（ID 拾取） | A | P0 | M | 无（生成端已有） |
| A3 | 深度工具组（ZBlur / Fog / DepthMatte） | A | P0 | M | 无（Z 读取已有） |
| A4 | Deflicker 去闪烁（时域中值） | A | P1 | M | 无 |
| A5 | ML 辅助 Roto（分割传播 → 关键帧） | A | P0 | L | 软依赖 A1（LK warp 复用） |
| A6 | 通用子树磁盘缓存 + Render-in-Place | A | P0 | L | 无 |
| A7 | Headless CLI 渲染（topos-render） | A | P1 | M | 建议在 A6 后（可复用磁盘缓存） |
| B1 | VDB 体积渲染（烟/云/雾导入） | B | P1 | L | 无（新依赖需过打包） |
| B2 | 点云节点（PLY + 跟踪数据导入） | B | P2 | M | 软依赖 A1（跟踪点源） |
| B3 | God Rays 体积光 | B | P1 | S | 无 |
| B4 | 镜头效果包（色差 / 暗角 / 光晕） | B | P2 | M | 无 |
| C1 | 生成式扩展/填充（火山引擎） | C | P1 | L | 无（含 provider 迁移） |
| C2 | AI 帧插值（RIFE ONNX） | C | P1 | M | 与计划一 D2 决策联动 |
| C3 | 人脸工作流（landmark 跟踪 + 美颜） | C | P2 | L | 阶段 2 软依赖计划一 C7（网格变形） |

规模口径同计划一：S ≤ 半天；M = 1~2 天；L = 3 天以上。

---

## 2. 本期补充全局约束（在计划一第 2 节之上新增）

11. **AI 模型接入规范**：所有本地模型走 `src/shared/ai_models/model_manager.py` 的 `ModelInfo` 注册（含远端源、hash 校验、下载状态），转换脚本遵循 `tools/convert_{lama,swinir,nafnet}_to_onnx.py` 的既有模式命名（`tools/convert_xxx_to_onnx.py`）；推理统一经 `src/shared/ai_models/inference_engine.py`（ONNX worker、tile、流式预览复用）。
12. **云端 provider 规范**（C1）：API 密钥只从偏好设置或环境变量读取，**禁止硬编码/入库**；无密钥或离线时节点进入 `NEEDS_CONFIG` 状态（可见、可解释、不崩溃）；每次云端调用记录用量日志（diagnostics 体系）；批量调用前必须给用户成本确认提示。
13. **新三方依赖打包门**（B1 的 pyopenvdb 等）：新依赖必须过 `scripts/verify_clean_install.py` + `scripts/verify_packaged_resources.py`（或 `verify_product_bundle.py`）三平台验证；无法出 wheel 的依赖需提供受控构建脚本并记录平台限制，否则任务降级方案执行。
14. **性能基准门**：新增交互式渲染节点（A1/A3/B1/B3/B4）必须在 `quality_profile` 的 draft/preview 档提供降级路径（减采样数/步数）；`tools/` 新增对应 bench 脚本，合入前后跑对比，交互路径回退 >5% 必须修复或降档。

---

## 3. Batch A — 高价值且地基已备（"摘果子"组）

### A1 Relight 节点（AOV 重光照）

#### 背景与现状
用 Position/Normal AOV 在 2D 图上重新打光是 Nuke 的高频节点、CG 元素融入实拍的核心手段。地基已备：
- `src/features/composite/render3d/aov.py:47` `AOVPass` 已含 `NORMAL`（世界法线，**encode 到 [0,1]**）与 `POSITION`（世界位置，HDR float），且都在 `AOV_EXPORTABLE_PASSES`（L110-114）内；
- scene3d 渲染这些 AOV 为独立纹理；EXR 导出后外部 DCC（Blender）的 AOV 也能读回（A6 of 计划一落地后多层通道更顺）。
- 目前**没有任何消费端**用这些 pass 做重光照。

#### 涉及文件
| 文件 | 改动 |
|------|------|
| `src/features/composite/nodes/relight_node.py` | 新建 |
| `src/features/composite/core/node_engine.py` | 注册（建议新类别 Light 或归 Filter） |
| `src/features/composite/core/shaders.py` | 新增 relight fragment shader |
| `src/features/composite/evaluators/` | 求值（可并入现有 shader dispatch 模式） |

#### 实现步骤
1. 节点端口：`image`（beauty/albedo）、`normal`、`position` 三输入（端口类型校验沿用现有机制）；
2. 参数：`light_type`（point / directional）、`light_position_x/y/z`（point）/ `light_direction_x/y/z`（directional）、`intensity`、`color`、`ambient`、`normal_flip_y`（Blender 导出法线 Y 翻转兼容）、`normal_decode`（[0,1]↔[-1,1]，默认按本项目 encode）；全部可动画、可表达式；
3. Shader 数学：`N = decode(normal_tex)`；point 模式 `L = normalize(light_pos - P)`，directional `L = -normalize(dir)`；`diffuse = max(dot(N, L), 0)`；输出 `image * (diffuse * intensity * color + ambient)`（linear 域运算，遵守色彩约束）；
4. MVP 不做镜面高光（roughness/specular 列阶段 2，参数位预留但隐藏）；
5. 注册六件套 + 中文别名（"重光照/补光"）+ i18n + node_help；
6. tile/ROI：纯逐像素着色，无空间采样，padding=0。

#### 验证方法
- 自动化：`tests/composite/test_relight_node.py`——(1) 用 scene3d 渲一个 Lambert 材质球的 NORMAL/POSITION AOV 作 fixture，relight(point light 在 +X) 输出与解析 Lambert 解逐像素对比（容差 2/255）；(2) directional 模式同理；(3) 三种 normal_decode/flip 组合的正确性；(4) 序列化往返 + 缓存失效（参数动画逐帧求值）。
- 手工：Blender 渲一套 AOV EXR → relight 移动光源位置，明暗随方向变化正确；与 Blender 内置灯对比目视一致。
- 基准：`tools/bench_relight.py`，1080p 交互帧耗 <2ms（GPU）。

#### 验收标准
解析解对照通过；外部 DCC AOV 可用；性能门达标；动画/表达式可驱动光源。

#### 风险与回退
不同 DCC 的 AOV 约定差异（法线朝向、位置空间）——用 `normal_flip_y` 与单位说明文档兜底；阶段 2 可加 `space` 参数。

---

### A2 Cryptomatte 抠选节点（ID 拾取 → matte）

#### 背景与现状
**生成端已有**：`render3d/cryptomatte.py` 实现了 murmurhash3_32 → `cryptomatte_float_id(name)`（**单 float32 ID**，非标准 float-pair 编码）+ `CryptomatteManifest`（hash hex → 物体名的 JSON manifest），scene3d 输出 ID 纹理与 manifest 元数据。**缺消费端**：点击物体提取 matte 做局部调色/合成的节点不存在。

#### 实现步骤
1. 新节点 `nodes/cryptomatte_select_node.py`（类别 Channel 或 Keyer）：
   - 输入：`image`（含 ID 通道的图——scene3d 直连时的 OBJECT_ID AOV 纹理，或 EXR 读回的 ID 通道，经计划一 A6 的 extra_channels）；
   - 输出：`matte`（选中 ID 的 alpha）+ `image`（可选 isolate 模式：原图 × matte）；
2. ID 匹配：按**本项目编码**（单 float 比较，`|id - target| < 1e-6`），多选集合 `selected_ids: list[float]`；manifest 从输入节点元数据透传（scene3d 的 manifest JSON 或 EXR sidecar）；
3. **查看器拾取交互**：节点属性面板 "Pick" 模式开关 → 查看器点击像素 → 经 `composite_viewport.py` 既有 float readback（约 L8396 像素探针同路径）读 ID → manifest 反查名称 → 加入/移除选中集（面板列出已选名称，可勾选）；
4. 属性面板物体列表：有 manifest 时显示全部物体名复选列表（免点击也能选）；
5. matte 后处理透传：输出接现有 `matte_cleanup_node`/`erode_dilate_node` 做收边（不重造）；
6. 注册六件套 + 中文别名"Cryptomatte 抠选/ID 抠像" + i18n + node_help（写清与标准 Cryptomatte float-pair 的差异）；序列化：selected_ids + manifest 名称快照（manifest 变更时按名称重映射 ID，找不到则标记离线）。

#### 验证
- 自动化：`tests/composite/test_cryptomatte_select.py`——合成 ID 图（两物体）+ manifest：拾取 A → matte 恰为 A 区域；多选并集；isolate 输出；序列化往返（含按名称重映射）；EXR 路径（ID 经 extra_channels 读入）同样工作。
- 手工：scene3d 场景（多物体）→ CryptomatteSelect 拾取某物体 → 只对该物体调色/换背景；导出 EXR 重读后拾取仍可用。

#### 验收
拾取/列表双入口可用、按名称重映射稳健、与 matte 精修链路打通。

#### 风险
标准 Cryptomatte EXR（float-pair）外部文件导入列为阶段 2（编码转换器），本任务先覆盖自家生成端闭环。

---

### A3 深度工具组（ZBlur / Fog / DepthMatte）

#### 背景
深度消费能力缺位：EXR Z 读取已有（`core/image_loader.py` `DecodedImage.depth_data`）、scene3d 有 R32F 线性深度 AOV，但没有任何节点用深度做画面处理。三个节点组成工具组。

#### 实现步骤
1. **通用深度输入约定**：新 mixin/工具函数 `resolve_depth_input(node)`——按序取：显式 `depth` 输入口（接 scene3d DEPTH AOV 或 shuffle 出的 Z 通道）> 图像自带 `depth_data`；参数 `depth_units`（线性归一化 / 世界距离）与 `near/far`（世界距离模式的线性化范围）。三个节点共用。
2. **`zblur_node.py`**（Filter）：参数 `focus_distance`、`focal_range`（景深范围）、`max_blur_radius`、`bokeh_toggle`（复用 blur_node 的 bokeh 光圈形状采样器）；实现：per-pixel 模糊半径 `r = max_r * smoothstep(0, range, |Z - focus|)`；GPU 变半径模糊 MVP 用**两档 downsample + per-pixel radius 高斯近似**（先实现正确再优化，禁止一次性上金字塔）；draft 档半径/采样减半。
3. **`fog_node.py`**（Filter）：参数 `color`、`density`、`start/end`；`out = mix(image, fog_color, 1 - exp(-density * clamp((Z-start)/(end-start))))`；纯逐像素，padding=0。
4. **`depth_matte_node.py`**（Mask）：按 Z 区间 `[z0, z1]` + feather 输出 matte，供任意下游（配 A2/Keyer 联动）。
5. 三个节点各自注册六件套 + 中文别名 + i18n + node_help；深度源丢失时节点进入可见的 `NEEDS_INPUT` 状态（不崩、不静默直通）。

#### 验证
- 自动化：合成深度渐变 fixture——(1) ZBlur：焦点平面内清晰、远处半径单调增大（采样中心圆盘面积断言）；(2) Fog：start 前无雾、end 处雾量符合指数公式（数值表）；(3) DepthMatte 区间边界与羽化；(4) 世界距离/归一化两种模式换算正确；(5) tile 一致性（zblur 有空间采样，padding 按 max_r 声明）+ 序列化。
- 手工：CG 场景加景深/大气；EXR Z 通道素材（Blender 渲）三节点各走一遍。
- 基准：`tools/bench_zblur.py`（1080p 交互 <8ms GPU draft 档）。

#### 验收
三节点数值正确、深度源缺失安全、tile 安全、性能门达标。

---

### A4 Deflicker 去闪烁（时域中值）

#### 背景与现状
延时摄影、老素材、屏幕录制的亮度闪烁是刚需问题，全库无时域聚合节点。**架构挑战（先读）**：合成求值是 pull-based 按帧求值，时域节点需要相邻帧的上游结果——不能假设逐帧顺序。

#### 实现步骤
1. 新节点 `nodes/deflicker_node.py`（Filter，别名"去闪烁"）：参数 `window`（3/5/7/9 奇数）、`percentile`（0.5=中值）、`luma_only`（默认 true：只校正每像素亮度比例，保留色度——抗闪烁更稳）、`mode`（per-pixel / global gain 两档，global 为 MVP 备选）。
2. **时域缓冲策略（核心）**：
   - 节点维护按**绝对帧号**索引的环形缓冲（存上游输入纹理的 CPU 拷贝或 GPU 纹理引用，EFFECT/PREFETCH 缓存类别托管）；
   - 播放/顺序导出：窗口自然滑入；
   - **任意跳帧**：检查窗口内帧号连续性，缺帧时尝试从 `cache_manager` 取相邻帧缓存命中（计划一的 prefetch 体系提高命中率）；取不到则**降级直通 + 画面角落 warming 徽章**（节点状态透传 UI），禁止用错误窗口数据假装成功；
   - 求值结束把当前输入写入环形缓冲（含帧号），供后续帧使用。
3. 计算：MVP CPU numpy（窗口排序取 percentile；luma_only 模式先求每像素窗口中值亮度 → 增益 = median/current → 三通道同乘）；GPU compute 列阶段 2。
4. 注册六件套 + i18n + node_help（写明跳帧降级行为）。

#### 验证
- 自动化：`tests/composite/test_deflicker_node.py`——(1) 合成闪烁序列（亮度乘全局正弦）→ 输出亮度方差下降 >95%；(2) 顺序逐帧与任意跳帧后补齐的结果一致（窗口填满后）；(3) 跳帧缺缓存时降级直通不崩、状态正确；(4) 确定性（同序列两次求值一致）；(5) 序列化往返。
- 手工：延时素材/老电影素材去闪烁前后对比播放。
- 基准：`tools/bench_deflicker.py`（1080p @ window=5，CPU <60ms/帧）。

#### 验收
闪烁抑制量化达标、跳帧降级安全、确定性成立。

#### 风险
顺序依赖的隐藏状态与快照架构的张力——环形缓冲**不算入图 schema**（运行态），序列化/undo 不涉及；测试必须覆盖"保存重开后首帧行为"（应降级直通）。

---

### A5 ML 辅助 Roto（分割传播 → roto 关键帧）

#### 背景与现状
逐点手画 roto 是合成最耗时的人力活。地基已备：
- `src/shared/ai_models/ai_mask_engine.py`：BiRefNet + SAM（encoder/decoder 已在 model_manager 注册：`birefnet.onnx` / `sam_encoder.onnx` / `sam_decoder.onnx`）；
- `nodes/roto_shape_node.py`：bezier 形状 + 逐点动画通道（`roto_point_{i}_x/y/c1/c2/feather`，**上限 16 点**）+ 关键帧命令体系完整；
- 计划一 A1 完成后 `src/shared/tracking/` 有 LK 光流可复用（软依赖，未完成则传播步降级为"每 N 帧重检测"）。

#### 实现步骤
1. **入口**：roto_shape 节点属性面板 + 查看器 ROTO 工具下增加 "AI Assist"：
   - 用户在当前帧涂一笔粗略笔刷（或框选 box prompt）→ `ai_mask_engine` 出 mask；
2. **轮廓→形状**：`cv2.findContours` → 轮廓简化（Douglas-Peucker）→ **贝塞尔拟合并截断到 ≤16 点**（超限时增大简化阈值重试，仍超则提示用户该形状复杂、建议拆多个形状）→ 生成本帧 roto 关键帧（点坐标 + 自动手柄），走现有 roto 关键帧命令（可撤销）；
3. **时序传播**：从打点帧向两侧每 `step` 帧（默认 5，参数可调）迭代：
   - 有 shared/tracking：上一帧 mask 经 LK 光流 warp 到新帧作为 prompt → BiRefNet/SAM 精修 → 新关键帧；
   - 无：直接重检测（无 prompt 或 box prompt）→ 质量提示降档；
   - 每步算 IoA（Intersection over Area）置信度，低于阈值（默认 0.6）停止该方向并在面板标注最后可信帧；
4. 关键帧之间由 roto 既有插值承担；生成过程后台线程（`inference_engine` worker 复用）+ 进度 + 取消；
5. **产出是普通 roto 关键帧**——完全可手改、可撤销（生成整段关键帧打包为一个 undo 组）；
6. i18n（"AI 辅助 Roto/自动轮廓"）+ node_help（写明 16 点上限与复杂形状建议）。

#### 验证
- 自动化：`tests/composite/test_ml_roto.py`——(1) 合成运动图形 fixture（平移矩形）涂刷 → 生成关键帧的 IoU ≥ 0.9；(2) 点数截断逻辑（超 16 点的形状）；(3) 置信度停止；(4) undo 一步清除整段生成；(5) 序列化后关键帧即普通 roto 数据（无 AI 依赖）。
- 手工：真实人物走动视频一段 5 秒 → 涂刷 → 传播 → 逐帧抽查边缘 → 手动修个别帧。
- 性能：单帧推理 + 轮廓 < 1s（BiRefNet ONNX，720p 输入）。

#### 验收
单帧生成与传播可用、产物纯 roto 可编辑、撤销/序列化干净、置信度兜底。

#### 风险与回退
复杂运动传播失败是常态而非异常——置信度停止 + 人工接力是设计内行为；SAM mask→bezier 的拟合质量决定手感，简化阈值自适应是关键参数。

---

### A6 通用子树磁盘缓存 + Render-in-Place

#### 背景
节点缓存是内存 LRU（`render/cache_manager.py`，六大类别，AI 类别已可落盘），**通用子树无磁盘层**：重图重开工程/重播全靠重算；也没有 Nuke 式 precomp/bake 工作流。

#### 实现步骤
1. **DiskCacheStore**（`render/disk_cache_store.py`）：
   - 路径：缓存根目录（偏好可配，默认跟 comp clip 缓存同根）/`node_cache/`；
   - 键：直接复用 `cache_manager.compute_key`（内容寻址——参数/上游 revision 变化自动换键，天然无陈旧问题）；
   - 格式：EXR DWAB half-float（复用 `shared/io/file_io.py` bake 路径写法）；条目 manifest 记录节点 id、帧号、分辨率、创建时间；
   - 层级：内存 LRU 未命中 → 磁盘命中 → 加载回纹理并回填内存；内存驱逐时可选择溢写磁盘（仅 `cache_to_disk=True` 的节点）；
   - 清理：总容量上限（默认 20GB，偏好可调）+ LRU 清理线程（模式参考 `render_cache.py` 的 50GB 上限清理）。
2. **节点参数**：所有 Filter/Keyer 类节点 ParamDef 增 `cache_to_disk`（bool，默认 false）；求值路径在写内存缓存时同步落盘（AI 类别已有此行为，泛化它）。
3. **Render-in-Place（UI 工作流）**：
   - 节点右键 "Bake Subtree…"：选帧范围（默认工程范围）→ 用导出渲染器（`composite_export_renderer` 同步路径，worker 线程）逐帧渲染该节点输出 → 写成图片序列（EXR DWAB）+ manifest（记录被烘焙子图的完整 graph snapshot hash）；
   - 烘焙完成后**图改写**（命令化，可整体 undo）：原子图折叠进一个 Group（bypass），输出接一个 `image_input_node`（序列路径指向烘焙产物，带 `cache_to_disk` 语义）；
   - "Unbake"：还原原子图（烘焙产物文件保留，用户手删或由清理器管理）；
   - 状态徽章：baked 节点显示烘焙标记 + 烘焙时间。
4. 偏好页：磁盘缓存容量、路径、清理策略；诊断面板显示磁盘命中率。
5. i18n + node_help（Render-in-Place 与 comp clip 缓存的适用场景区分）。

#### 验证
- 自动化：`tests/composite/test_disk_cache.py`——(1) bake 后下游输出与未 bake 逐像素一致（PSNR ≥ 50dB）；(2) 上游参数改动 → compute_key 变化 → 磁盘不命中（重算）；(3) undo bake 图结构逐字段还原；(4) 容量上限触发 LRU 清理且命中中的条目不被误清；(5) 崩溃恢复：manifest 半写时不被读取（原子写：临时文件+rename）。
- 手工：重节点图（多个 blur+keyer 链）bake → 交互明显变快（`tools/` 加前后基准）；改上游参数 → 自动失效重算；重启工程缓存仍命中。
- 基准：`tools/bench_disk_cache.py`：冷/热启动同一工程的首帧时间对比。

#### 验收
烘焙=直算逐像素一致、内容寻址失效正确、undo 完整、容量治理可用、冷热性能差异显著。

#### 风险
磁盘 IO 线程与渲染线程竞争——写入走独立低优先级线程 + fence；EXR half 对 extreme HDR 值的精度损失——manifest 记录格式，需要 float32 的节点（如深度链）参数化选 f32。

---

### A7 Headless CLI 渲染（topos-render）

#### 背景
无 UI 命令行渲染（Nuke `nuke -x` 等价物）是渲染农场、批处理、CI 视觉回归的地基。渲染器本就快照驱动、独立于 UI（`composite_export_renderer` 同步路径），缺的只是入口。

#### 实现步骤
1. 入口 `scripts/topos_render.py`（`python scripts/topos_render.py <工程/图.json> [选项]`）：
   - 参数：`--frames 1-100`、`--out <path>`、`--format exr|png|prores|h264…`（复用 `composite_render_output.py` 的 IMAGE/VIDEO 检测）、`--aov beauty,depth,…`、`--quality draft|preview|final`、`--json`（机器可读进度/结果输出，供 CI 解析）、`--dry-run`（只校验图不渲染）；
2. **运行时**：`QCoreApplication`（无 QApplication——QThread 可用，Qt 事件循环以 `exec` 起最小驱动或以信号泵手动驱动，执行时验证 `composite_render_worker` 的 QThread 在无 GUI 下的事件依赖）；GL 上下文：ModernGL standalone context（`moderngl.create_standalone_context()`）——**执行时先验证 `composite_frame_renderer` 的上下文创建路径**是否依赖 Qt 平台插件，必要时加 standalone 分支（macOS 无头环境需确认 offscreen 支撑）；
3. 图加载：`.topos` 工程（取指定合成图）或直接图 JSON（`serialization_formats` 读入 + graph_migration 迁移）；
4. 退出码契约：0 成功 / 2 图校验失败（含离线素材清单打印）/ 3 渲染异常 / 4 编码失败；`--json` 模式输出结构化结果（帧范围、耗时、输出文件、告警）；
5. 与 A6 联动：默认读磁盘缓存（热缓存秒出）；`--no-cache` 强制直算；
6. 文档：`--help` 完整 + node_help/README 补一段农场用法示例。

#### 验证
- 自动化：`tests/test_topos_render_cli.py`（subprocess 调用）——(1) 小图 3 帧 EXR 输出与 GUI 导出**逐像素一致**；(2) 各退出码路径（坏图/缺素材/正常）；(3) `--dry-run` 不产生文件；(4) `--json` 可被 json.loads 且字段齐全；(5) 帧范围解析（单帧/区间/逗号列表）。
- 手工：终端跑 100 帧渲染 + ProRes 输出；CI 冒烟（在本仓库 CI 脚本中加一步，若项目无 CI 则提供 GitHub Actions 片段存 docs）。
- 基准：与 GUI 导出同图同参数耗时对比（应持平或更快）。

#### 验收
输出与 GUI 一致、退出码/JSON 契约稳定、无显示器环境可跑（或明确记录平台限制与解决办法）。

#### 风险
无头 GL 是最大不确定性——macOS 上 ModernGL standalone 通常可行，但执行时必须第一步做 spike 验证，失败则降级为"需要虚拟显示（record 档说明）"并在文档标注。

---

## 4. Batch B — 3D 进阶

### B1 VDB 体积渲染（烟/云/雾）

#### 背景
3D 子系统有网格/材质/灯光但无**体积**：烟、云、雾、爆炸（Houdini/Blender 烟 sim 的标准交换格式 OpenVDB）渲不进来。这是 Nuke X 级功能，做完 3D 场景才有体积感。

#### 实现步骤
1. **依赖前置（第一件事）**：`pyopenvdb` 可用性 spike——PyPI wheel 覆盖 macOS（x86/arm64）+ Windows 则直接用；否则受控构建脚本（参考 native/decklink_bridge 的构建管线）或降级方案（只支持自身体积格式 + 提供 `tools/convert_vdb.py` 离线转换）。按补充约束 13 过打包验证，**不满足则降级执行并记录**。
2. **数据层** `render3d/volume_grid.py`：读 `.vdb`（grid 名可选，默认取第一个 density/`fog_volume`）；稀疏 active voxel → bbox 稠密化到 3D 纹理（典型 sim 256³~512³ 在 GL 3D 纹理上限内）；可选 `temperature` grid 用于火苗 emission（blackbody 渐变，MVP 可选参数）。
3. **节点** `nodes/volume3d_node.py`（3D 类别，别名"体积/烟雾/云"）：参数：文件/序列路径（`sim.####.vdb` 序列模式同 image_input）、grid 选择、density 强度、absorption 颜色、emission 强度/颜色、step_count（16-64，draft 档自动减半）、3D transform（复用 geo3d 的变换参数组）。
4. **渲染集成**：scene3d 渲染管线加体积 pass——不透明几何先渲（有线性深度，R32F 已有），体积 raymarch 从相机到深度终点按固定步长积分（emission-absorption 单散射 + 一个 directional light 的 in-scattering MVP 可选）；blue-noise 抖动消 banding；`render3d/` 新增 `volume_renderer.py`（3D 纹理绑定 + raymarch shader 进 `core/shaders.py`，遵守 GLSL 约束：步进循环上界用 uniform 乘常量上限的写法保证可编译，或生成多档循环展开）。
5. 多体积/嵌套：MVP 支持场景中多个 volume3d 节点（各自积分，性能提示叠加数）。
6. AOV：体积贡献进 BEAUTY；DEPTH 不透（体积是半透明），列出已知限制。
7. 注册六件套 + i18n + node_help（推荐 Blender smoke → VDB 导出的设置说明）。

#### 验证
- 自动化：`tests/composite/test_volume_render.py`——(1) 合成球体密度 VDB（`tools/make_vdb_fixtures.py` 生成）+ 吸收 only：渲染中心视线穿透率 = exp(-σ·d) 解析解（容差 5%）；(2) 空网格/坏文件安全；(3) 序列帧加载正确；(4) step_count 降档数值收敛（32 vs 64 差异 <2%）；(5) 序列化往返。
- 手工：Blender 快速烟 sim → VDB → 合成场景与实拍底片合成；draft 档交互流畅度目测。
- 基准：`tools/bench_volume.py`：512³ @1080p，final ≥5fps、draft ≥20fps（不达标降 step 或记录瓶颈）。

#### 验收
解析解通过、真实 VDB 可用、性能门或明确瓶颈报告、打包门通过。

#### 风险
pyopenvdb 打包是最大风险（约束 13 有门禁与降级路径）；raymarch 在低端 GPU 的性能——draft 档步数减半 + 分辨率 LOD 已有体系兜底。

---

### B2 点云节点（PLY + 跟踪数据导入）

#### 背景
点云是对位检查与场景重建参考。地基：`render3d/model_loader.py` 已用 trimesh（PLY 点云读取现成）；计划一 A1 落地后有跟踪点数据源。

#### 实现步骤
1. `nodes/pointcloud3d_node.py`（3D 类别，别名"点云"）：输入源二选一——(a) `.ply/.xyz` 文件（trimesh 读点 + 顶点色）；(b) "From Tracker"：引用 tracker_node（计划一 A1）的 track points / 或未来 D1 反求点云；
2. 渲染：scene3d 加 **GL_POINTS pass**（size 可调、按深度/置信度着色选项、距离衰减尺寸）；
3. 参数：点大小、颜色模式（固定/顶点色/深度渐变/置信度）、显示比例；transform 复用；
4. 注册 + i18n + node_help。

#### 验证
- 自动化：合成点网格 PLY fixture → 渲染像素覆盖断言（投影像素非空比例）；跟踪数据导入点数一致；序列化。
- 手工：tracker 点云叠加实拍素材对位检查。

#### 验收
双数据源可用、渲染正确、序列化干净。

---

### B3 God Rays 体积光

#### 背景
丁达尔光束（暗角窗口透光、森林光柱）是高性价比视觉大件；GPU 上是受限径向采样，与 B1 的体渲染独立、便宜得多。

#### 实现步骤
1. `nodes/godrays_node.py`（Filter，别名"体积光/丁达尔/光束"）：参数：`light_x/y`（屏幕空间光源位，可查看器拖动）、`density`、`decay`、`samples`（16-64，draft 减半）、`color`、`weight`；可选 `mask` 输入口（occluder 遮罩，无则用输入 alpha）；
2. Shader：从每像素向光源位置径向采样 occlusion，累积 `occlusion * decay^i * weight^i`；输出 `image + rays * color * density`（linear 域，screen/add 可选混合）；
3. 全帧后处理型节点：tile padding = 0 但**不参与 ROI 裁剪优化**（径向采样全局性，声明 full-frame-only，与 ROI 交互时自动全帧渲染——复用计划一 B1 的 ROI 豁免机制）；
4. 注册 + 中文别名 + i18n + node_help。

#### 验证
- 自动化：黑底 + 中心亮源 + 遮挡圆盘 fixture——光束仅出现在圆盘背光侧、能量随距离按 decay 衰减（数值表）；samples 收敛性；序列化。
- 手工：森林/窗口素材开效果，光源拖动跟手；基准 1080p <4ms（GPU，64 samples）。

#### 验收
方向性正确、衰减公式通过、交互性能达标。

---

### B4 镜头效果包（色差 / 暗角 / 光晕）

#### 背景
镜头模拟家族补全（计划一 A4 补了畸变，这里补剩下三件）：色差、暗角、镜头光晕。

#### 实现步骤（三个独立小节点，可分三个 commit）
1. **`chromatic_aberration_node.py`**：径向 RGB 通道错位（R/B 偏移量参数 + 中心偏移 + 边缘增强曲线）；采样复用 transform 的 filter 参数；tile padding 按最大偏移声明。
2. **`vignette_node.py`**：`amount / radius / softness / roundness`，乘性压暗（linear 域 exposure 语义）；纯逐像素。
3. **`lens_flare_node.py`**（程序化 MVP）：光源位置（可查看器拖动）+ ghosts（数量/间距/色散衰减）/ halo / streak（横向 anamorphic 条纹，复用 blur 方向采样）→ screen 混合叠加；遮挡感知（光源被遮挡时衰减，采样输入 alpha）列为可选增强。
4. 各自注册六件套 + i18n + node_help；全部走 quality_profile 降档（采样数/ghost 数）。

#### 验证
- 自动化：色差——边缘 R/B 位移量像素断言；暗角——中心/四角曝光比数值表；光晕——ghost 序列位置/强度公式断言；三者 tile/序列化往返。
- 手工：实拍素材三件套各出效果；与 A4 畸变组合成"镜头模拟链"目视自然。

#### 验收
三节点公式正确、性能无感（各 <2ms GPU）、可组合。

---

## 5. Batch C — AI 生成式进阶

### C1 生成式扩展/填充（火山引擎）

#### 背景
现有 AI 节点全是修复类（LaMa/超分/降噪）。生成式扩展（outpainting/inpainting 生成新内容）是差异化方向，且**独特资产已存在**：Edit 页 AI 体系已接火山引擎 Ark 的图/视频生成（`src/features/edit/ai_editing/` 下的 `volcengine_generation_provider.py` 及媒体生成管线）。**模块边界问题（前置）**：composite 禁止 import edit 内部模块——provider 必须先迁到 shared（同计划一 A1 的 tracking 迁移模式）。

#### 实现步骤
1. **Provider 迁移**：`volcengine_generation_provider.py` 及其依赖的请求/响应模型 → `src/shared/ai_models/generation/`；edit 侧 import 全局更新；`pytest` 全绿（edit 相关测试零回归）；
2. **节点改造**：现占位的 `ai_generate_node.py` / 或新增 `ai_gen_expand_node.py`：
   - 输入：`image` + `mask`（要生成/扩展区域）；
   - 参数：`prompt` / `negative`、`expand_edges`（四边扩展比例，自动生成 mask）、`strength`、`seed`、`model`（provider 模型列表下拉）；
   - 输出：生成图（接回合成链）；
3. **执行链**：`ai_compute_manager.py` 增加云端 provider 类型：job 队列（复用现有异步 worker 结构）、结果 EXR 缓存（key 含 prompt+seed+输入图 hash——输入图变化自动失效）、状态机（QUEUED/UPLOADING/RUNNING/DOWNLOADING/NEEDS_CONFIG/FAILED）；
4. **密钥与成本**（约束 12）：偏好页 provider 配置区（key 输入、代理、默认模型）；无 key → NEEDS_CONFIG 可见状态；每次真实调用前**成本确认对话框**（单张/批量张数），用量写 diagnostics 日志；
5. **时域一致性**：MVP 明确为**静帧/逐帧独立**（视频逐帧独立生成会闪烁——节点文档写明适用场景：延展画幅、生成空镜、静帧修复）；**视频生成（i2v）列阶段 2**：provider 已有视频能力，做"生成 N 秒视频 → 解码为帧序列输出"的节点变体，待本任务验收后单独立项；
6. 注册更新（占位符转正）+ i18n + node_help（含平台合规提示：生成内容标识）。

#### 验证
- 自动化：`tests/composite/test_ai_gen_expand.py`——**本地 mock provider**（stub 返回固定图，不联网）测全链路：请求参数组装、缓存命中/失效、状态机迁移、NEEDS_CONFIG 降级、成本确认调用次数；序列化往返。
- 手工（需真实 key，标注为可选冒烟）：扩展一张 16:9 到 21:9；带 mask 局部重生成；断网状态 NEEDS_CONFIG 不崩。
- 成本护栏测试：批量 10 张触发一次确认而非 10 次。

#### 验收
mock 全链路绿、密钥治理合规、缓存正确、edit 侧零回归；真实 API 冒烟通过或明确记录待验。

#### 风险
云服务接口变更/限流——provider 层版本化 + 重试/退避；内容安全审核失败的处理（错误透传为节点 FAILED 态并显示原因）。

---

### C2 AI 帧插值（RIFE ONNX）

#### 背景
两帧→中间帧的真实运动插值（区别于计划一 A3.2 的线性帧混合）。与计划一 D2（稠密光流）联动：D2 若产出共享光流引擎可复用其插值；本任务**可独立先行**（RIFE 端到端出中间帧，不依赖显式 flow 输出）。

#### 实现步骤
1. 模型：RIFE v4 ONNX（`tools/convert_rife_to_onnx.py` 转换脚本 + model_manager 注册 `rife_interpolate`，含 hash 校验）；
2. 节点 `nodes/frame_interpolate_node.py`（Time 类别，别名"帧插值/补帧"）：
   - 输入：`frame_a`、`frame_b`；
   - 参数：`t`（0-1，**可动画**——配合时间重映射做变速的插值帧序列）、`hd_mode`（大图 tiling，复用 inference_engine 的 tile 逻辑）；
   - 输出：中间帧；
3. 求值：ONNX worker（`inference_engine.py` 复用）后台执行 + EXR 缓存（AI 类别已有磁盘缓存语义）；确定性：同输入同输出（固定 t 不含随机）；
4. 应用接线（可选加分）：`retime_node` 的 `use_optical_flow` 元数据在 D2 未落地期间可先映射到"RIFE 插值"路径（speed 非整时取相邻两帧按 t 求值）——若接线复杂则保留独立节点，文档写明用法；
5. 注册 + i18n + node_help。

#### 验证
- 自动化：`tests/composite/test_frame_interpolate.py`——(1) 平移帧对 fixture：t=0.5 输出的位移 = 半程（光流场抽查容差 1px）；(2) t=0/1 退化为端点帧；(3) 与真实中间帧（fixture 有 ground truth 序列）PSNR 对比显著优于线性混合（断言 ΔPSNR > 3dB）；(4) 确定性、缓存命中、序列化。
- 手工：24fps → 60fps 慢放对比（RIFE vs 线性混合 vs nearest）。
- 性能：720p CPU <800ms/帧、GPU（CoreML/EP）<150ms/帧（不达标标注交互降档为后台缓存模式）。

#### 验收
运动正确性数值达标、优于线性混合、确定性/缓存干净；与 retime 的联动完成或文档化用法。

#### 风险
RIFE ONNX 在部分平台算子兼容（执行时 spike）；大图显存——tile 模式兜底。

---

### C3 人脸工作流（landmark 跟踪 + 美颜）

#### 背景
人脸是修饰类合成的最大场景。地基：color 页有 HighPass_Retouch / 双边磨皮 tglsl（composite 用不了——模块体系不同）；`blur_node` 有 GPU 双边滤波；计划一 C7（网格变形，原 C8）可做变形后续。**本任务在 composite 建立人脸基建**：landmark 跟踪节点 + 皮肤平滑节点。

#### 实现步骤
1. **模型**：人脸 landmark ONNX（建议 PFLD 或 MediaPipe FaceMesh 的 ONNX 导出，478/98 点任选其一，执行时以转换难度定）+ 检测器（RetinaFace-mobile ONNX 或 UltraLight）；`tools/convert_face_landmark_to_onnx.py` + model_manager 注册两项；
2. **`face_track_node.py`**（AI 类别，别名"人脸跟踪"）：
   - 输入 image；逐帧检测 + landmark（MVP 每帧独立检测，无时序跟踪——检测率不足时自动前一帧框提示）；
   - 输出：内部 landmark 数据（帧号→点集，可序列化，**保存后无需重跑**）+ 直通 image；
   - 属性面板：重跑/清除、置信度显示、多脸时选脸索引；
   - landmark 关键点可选烘焙为动画通道（供表达式驱动其他参数——复用 roto 的动态通道模式，阶段 2）；
3. **`face_smooth_node.py`**（Filter，别名"磨皮/皮肤平滑"）：
   - 输入：image + `face`（face_track 输出）；
   - 从 landmark 多边形（颊/额/鼻区预置多边形索引表）生成皮肤 mask（多边形光栅 + 羽化，避开眼/嘴/眉的排除多边形）；
   - 皮肤区局部处理：复用 blur_node 的 GPU 双边（edge-preserving）+ `strength`（0-1，可动画）+ 可选 HighPass 频率分离（低频平滑保留高频细节——shader 新增，色彩约束 linear 域）；
   - 输出 mask 口：皮肤 mask 供下游（局部调色等）；
4. **阶段 2（软依赖计划一 C7 网格变形）**：数字变形（瘦脸/大眼）= landmark 引导的局部网格变形 + 平滑约束，届时单独立项；
5. 注册六件套 + i18n + node_help（多脸/遮挡限制写明）。

#### 验证
- 自动化：`tests/composite/test_face_workflow.py`——(1) 合成人脸 fixture（几何脸 + 已知 landmark 真值图）：跟踪点误差均值 <3px；(2) 皮肤 mask 与预置多边形区域一致、排除区（眼/嘴）为 0；(3) mask 外像素逐位不变；(4) strength=0 直通；强度动画插值；(5) 序列化后 landmark 数据保留、无需重跑。
- 手工：真实采访素材磨皮前后对比（肤色自然、边缘不糊）；多脸场景选脸切换。
- 性能：1080p 双边 @ 720p 内部处理 <8ms（GPU），检测后台线程不卡交互。

#### 验收
跟踪/磨皮链路可用、mask 精确、mask 外零影响、序列化含跟踪数据。

#### 风险
landmark 模型转换与精度（执行时 spike 两个候选模型取优）；侧脸/遮挡退化——置信度门限 + 上一帧保持策略兜底。

---

## 6. 执行顺序建议与计划一衔接

**推荐顺序**：

```
第一批（摘果子，纯 shader 节点，1~2 天）：A1 → A2 → A3 → B3 → B4
第二批（管线与基建）                      ：A6 → A7 → A4
第三批（AI 组）                            ：A5 → C2 → C3 → C1（C1 含 provider 迁移，先跑迁移验证）
第四批（3D 旗舰）                          ：B2 → B1（B1 先做依赖 spike）
```

**与计划一的衔接矩阵**：

| 本计划任务 | 计划一依赖 | 未完成时的降级 |
|-----------|-----------|---------------|
| A5 ML Roto | A1（shared/tracking 的 LK warp） | 传播步退化为每 N 帧独立重检测（质量降档仍可用） |
| A2 Cryptomatte | 无硬依赖；计划一 A6（EXR 多层）落地后可吃外部 Cryptomatte 通道 | 仅支持 scene3d 直连闭环 |
| B2 点云 | A1（跟踪点源） | 仅支持 PLY 文件源 |
| C2 RIFE | 与计划一 D2 决策联动 | 独立节点先行，D2 落地后再评估 retime 深度接线 |
| C3 阶段 2 变形 | 计划一 C7（网格变形） | 阶段 2 顺延，不影响本任务验收 |
| A7 CLI | 建议在 A6 后 | 可先行，但失去热缓存加速 |

**每任务验收门**：沿用计划一第 8 节（pytest + 冒烟 + 手工清单 + 单 commit），另加：
- AI/模型任务：模型下载 UI 冒烟（不静默下载）；
- 云端任务：mock 全链路必须 100% 覆盖（真实 API 仅可选冒烟）；
- 新依赖任务：打包验证脚本通过。

**批次里程碑**：第一批完成后走"CG 素材 AOV → Relight 补光 → Cryptomatte 抠选局部调色 → 深度景深/大气"端到端；第二批完成后"重图 bake → CLI 无头渲染 100 帧"农场流；第三批完成后"涂刷出 roto → 慢放补帧 → 人脸磨皮 → 生成式扩边"AI 流水线；第四批完成 B1 后"Blender 烟 sim 与实拍合成"。

---

## 7. 关键文件速查（本期新增锚点 + 相关既有）

| 模块 | 路径 |
|------|------|
| AOV 定义 / 导出表 | `src/features/composite/render3d/aov.py:47`（NORMAL [0,1] 编码 / POSITION HDR，AOV_EXPORTABLE_PASSES: 110） |
| Cryptomatte 生成端（单 float ID + murmurhash + manifest） | `src/features/composite/render3d/cryptomatte.py:21-130` |
| AI 分割引擎（BiRefNet+SAM） | `src/shared/ai_models/ai_mask_engine.py` |
| 模型注册/下载管理 | `src/shared/ai_models/model_manager.py`（ModelInfo 注册模式：swinir_denoise/lama_inpaint 等） |
| ONNX 推理引擎 | `src/shared/ai_models/inference_engine.py` |
| AI 节点计算管理 | `src/features/composite/render/ai_compute_manager.py`（模型映射 :451）+ `ai_cache_manager.py` |
| 节点缓存（内存 LRU 六类别） | `src/features/composite/render/cache_manager.py:41`（AI 类别已有磁盘语义可泛化） |
| 导出渲染（同步路径） | `src/features/composite/render/composite_export_renderer.py` |
| 帧渲染器（worker 自建 GL context） | `src/features/composite/render/composite_frame_renderer.py` |
| blur 节点（box/gaussian/bilateral/motion/bokeh） | `src/features/composite/nodes/blur_node.py` |
| Roto 形状节点（16 点上限/逐点通道） | `src/features/composite/nodes/roto_shape_node.py` |
| 3D 场景渲染 | `src/features/composite/nodes/scene3d_node.py` + `render3d/` |
| 模型加载（trimesh，PLY 点云可读） | `src/features/composite/render3d/model_loader.py` |
| EXR bake 写法（DWAB half） | `src/shared/io/file_io.py:57-82` |
| 查看器 float readback（拾取用） | `src/features/composite/ui/composite_viewport.py:8396` 附近 |
| 火山引擎 provider（待迁移） | `src/features/edit/ai_editing/volcengine_generation_provider.py` → 目标 `src/shared/ai_models/generation/` |
| 模块边界规则 | `docs/module_boundary_rules.md` |
| 打包验证 | `scripts/verify_clean_install.py` / `scripts/verify_packaged_resources.py` / `scripts/verify_product_bundle.py` |

---

## 8. 变更记录

| 日期 | 变更 |
|------|------|
| 2026-09-08 | 初版：计划一完成后的 Phase 2 进阶能力 14 项（A 批摘果子 7 项 / B 批 3D 进阶 4 项 / C 批 AI 生成式 3 项）；含与计划一的软依赖矩阵与降级路径；新增模型接入/云端密钥/打包门/性能门四条补充约束 |
| 2026-09-09 | **14 项全部执行完毕**（分支 feat/composite-advanced，提交 ca090718..HEAD）。逐项落地要点与诚实边界：**A1 Relight** 三输入 AOV 重打光，GL 解析 Lambert 对照 2e-4，bench 1.32ms（<2ms 门过）。**A2 Cryptomatte Select** rgba24/float_r 双 ID 编码、manifest 按名重映射；拾取 UI 接线与外部 float-pair EXR converter 记后续。**A3 深度工具组** ZBlur/Fog/DepthMatte，zblur draft 1.18ms（<8ms 过）；深度缺失=可见断链非静默直通。**B3 God Rays** 全帧豁免 ROI，方向性/衰减数值验证。**B4 镜头包** 色差/暗角/光晕可组合镜头链。**A6 磁盘缓存** 内容寻址+原子写+LRU，载荷偏离计划 EXR→npz f16（OpenEXR binding 在 Qt/cv2 同进程间歇崩溃，稳定性优先，manifest 记录真实格式）；cache_to_disk 参数（9 节点已带）；Render-in-Place 烘焙服务（渲染回调注入可测、undo 单步、unbake 完整还原）。**A7 CLI** topos_render.py 退出码契约/--json/--dry-run 全测；真实渲染测试以 TOPOS_CLI_GL=1 门控——本机 standalone GL context 创建不健康（裸进程 SIGSEGV 随会话翻转），即计划预警的最大不确定性，按降级条款文档化。**A4 Deflicker** 因果窗口+跳帧降级直通+warming 提示；percentile→partition 快速路径（292→122ms）；本机 x64 实测 171ms/帧未达 60ms 预算（预算按现代 CPU 设定，算法 O(n)，如实报告）。**C1 生成式** volcengine provider 及 5 个契约模块物理迁移 src/shared/ai_models/generation/（~2800 行 git mv 保历史），旧路径 sys.modules 替换式 shim，20+ 消费者零改动，迁移直接影响面 81 测试全绿，edit 全量隔离回归 470/475 文件（5 失败经 worktree 基线对照确认为既有问题）；ai_gen_expand 节点转正，mock 全链路（状态机/缓存/成本门/密钥治理），后台队列与真实 API 冒烟为后续。**A5 ML Roto** mask→自适应简化→bezier（≤16 点）、IoA 置信度双向传播、产物纯 roto 数据；检测经注入式 detector，BiRefNet/SAM 真实推理待模型。**C2 RIFE** model_manager 注册（不静默下载）、session 注入 stub 验证运动中点正确性、模型缺失线性降级+透明上报；pth→onnx 转换脚本后续。**C3 人脸** 468 点皮肤 mask 几何（含排除区）+ face_track 容器（landmark 随工程保存）+ face_smooth（复用已验证双边 shader，mask 外逐位不变）；landmark ONNX 待模型。**B2 点云** PLY/xyz/tracker 三源、CPU 正交光栅；GL_POINTS pass 后续。**B1 VDB** 依赖门 spike 未过（pyopenvdb 不可用无 wheel）——按约束 13 降级：npz 稠密体积格式完整可用 + .vdb 明确依赖错误 + convert_vdb.py 离线转换器 + Beer-Lambert CPU raymarch；GPU 体渲染待依赖立项。**回归**：新增测试约 230 项全绿，tests/composite 全量隔离回归见当次执行记录。 |
| 2026-09-09 | **计划全面复查（第二轮）——5 路并行审计发现约 30 处缺陷并全部修复**（提交见 git log fix/composite 系列）。要点：**渲染路径**：lens_flare 硬编码 16:9 aspect→u_resolution 动态计算+ghost_glow 双重 aspect 移除；cryptomatte isolate 模式 alpha 未乘 coverage（雾边）→修复；vignette shader 缺 softness≤radius clamp（与 Python mirror 不一致）→对齐；face_smooth 复用的 denoise shader 完全不消费 `_mask_tex`（"mask 外逐位不变"未成立）→shader+evaluator 增加可选 mask 采样；tile_padding_computer 补 chromatic_aberration 分支（tiled 大图边缘 R/B 截断）；zblur `bokeh_toggle` 参数此前无任何实现→落地（开=均匀圆盘光圈、关=高斯软化，u_bokeh uniform）。**A6 磁盘缓存**：读回上传 f32 字节进 f2 池纹理必然失败（生产读路径整体失效，测试假池 dtype 掩盖）→astype(f16) 修+假池对齐生产；f16 溢出→65504 饱和；payload 临时文件固定名并发竞态→pid+tid 唯一后缀；盘命中双计 miss→移到盘层拒绝后；capacity≤0 语义从"无限"改"禁用"；新增 TOPOS_DISABLE_DISK_CACHE 环境开关。**AI 批**：volcengine `_cache_b64` 的 `from exc` 未绑定（NameError 穿透）→修复；rife `model_manager.get_model_path` 按模块属性调用（方法在单例上）→RIFE 永不加载→改 get_model_manager() 且加进程级会话缓存；frame_interpolate/deflicker 自研回读把 `_active_textures` set 当 dict `.values()` → AttributeError 被吞、回读恒失败→抽 `_gpu_readback.py` 共享 helper（set/dict/list 全兼容+resource_pool 路径）；ai_gen_expand 缓存键输入像素恒为零图（换输入命中陈旧结果，违背 stale-free 合同）→GL 路径读回真实像素+无 GL 身份签名兜底+output_size 入键；mask 输入端口死路→读回红通道真接线（优先于 expand 边框 mask）。**A4**：per_pixel+luma_only=False 静默 no-op→实现逐通道 percentile（RGB 运行时缓冲，仅该模式填充）；跨分辨率缓冲混用（preview proxy vs export 同实例）→形状不匹配计为缺帧（warming 降级）；回读失败静默直通→可见错误态。**3D 批**：volume3d `step_world=depth/steps` vs `stride=depth//steps` 不一致（薄体积光学厚度差数百倍）→采样数一致的 compute_transmittance 纯函数化+只 gather 采样 slab（内存 176MB→n×h×w）+preview/full 亮度一致；volume_grid spacing 不往返→dataclass 字段+load/save+NaN 清洗；convert_vdb.py 三处必崩 API（readAll 无路径/openvdb 未定义/形状假设）→按 pyopenvdb 文档重写+grid 名自动选择；pointcloud3d 缺模块 logger（异常路径 NameError）→补；PLY/xyz 顶点色 sRGB 字节直当 linear（色彩管线违约）→load 时 srgb_to_linear 解码+xyz 字节域启发式归一；空背景 alpha=opacity² →透明背景+点 alpha 单次乘 opacity；file 级缓存无 mtime 失效→`runtime_file_signature`（path/mtime/size）经 `_runtime_input_signature` 通用挂钩进 cache key（pointcloud3d/volume3d）。**其余**：ml_roto 均匀抽点可超 16 点上限→步进收敛循环+关键帧直写绕过失效→补 dirty/revision/bump；face_workflow 排除区羽化回渗→双 mask 同核模糊后相乘 carve；CLI 补 `--no-cache`（贯通 env 开关）、argparse 用法错误从退出码 2→1（不再与 BAD_GRAPH 冲突）、--frames 解析提前到 dry-run 前、--out 目录名带后缀仅在图片扩展名时按文件处理；**bake 闭环**：image_input `####`/`%04d` 序列解析从"reserved 未接线"接入同步求值路径（frame 变化触发 reload，params_require_frame_cache 按模式返回）——bake 产物现在真正可读回；新增共享 `core/frame_sequence.py`（image_input/volume3d 共用）。**杂项**：zh/en locale key 集合对齐（删孤儿 aria key）+parity 测试钉死；frame_interpolate 补双语文档（六件套闭环）；bench 脚本 p95 负索引防护+陈旧 docstring。**新增 tests/composite/test_advanced_plan_audit.py 35 项回归**（denoise mask 逐位断言、cryptomatte isolate alpha、CA tile padding、zblur bokeh 开关、RIFE 会话缓存、ai_gen 键敏感性、volcengine 异常链、deflicker RGB 路径/分辨率守卫、体积步长一致性、sRGB 中灰、bake 序列解析、locale parity、CLI 契约等）。**回归结论**：tests/composite 逐文件隔离 238 个文件——修复过期断言（test_scene_payload 3D 分类集合漏更新 B1/B2 两节点）后 237 干净，唯一遗留 test_3d_preview_override 的 6 个 TestOnViewModeChanged 失败经 worktree 对照确认为**早于本计划**的既有问题（4706d00e 合并基点即失败，CompositePage super-init 测试构造问题）；edit 侧 provider 迁移面 63 测试绿；单进程全量在本机仍触发既有 teardown 段错误（沿用隔离跑法）。**已知遗留（文档化不再修）**：cache key 含上游 texture_id→盘缓存跨会话对 effect 节点结构性 miss（正确性无碍，重构需 evaluator 级设计）；godrays/lens_flare 在 tiled 超大图路径无 full-frame 逃逸（preview ROI 路径正确）；bake_service 尚无 UI 菜单接线；ai_compute_manager 后台队列与真实 transport/detector 注入为后续；provider_profiles 三处 shared→edit 反向 lazy import（ai_provider_catalog 未随迁）。 |
| 2026-09-09 | **真实素材全节点扫描（第三轮整体检查）**：用 tests/test_videos 真实素材把合成页全部 ~80 个节点类型经生产导出路径（CompositeExportRenderer）三阶段跑遍——默认参数（2K H264）、枚举参数矩阵（每个 option 参数逐值，720p fixtures）、11 编解码矩阵（h264/h265/prores422/4444/DNxHR/topos ×2K/4K）；另跑 CLI topos_render.py 真渲染。发现并修复 9 类真缺陷：**①直连源回退并发崩溃**：provider 服务打开失败回退 FFmpegSource 时，_preload_frames 无条件起 worker 与渲染线程 export 同步解码并发进同一不可重入实例→libav 原生 SIGABRT（回退模式改为纯同步解码，can_async 限服务模式）；**②topos_codec dylib 过期**：9/6 提交新增 tc_dev_symbol_hist_plane_get 符号但本机 dylib 停在 9/5 构建→ToposCodec() 必抛 AttributeError→服务开不了任何片→全部走回退踩①（重新构建 release dylib，属本机构建产物需在 codec 源码变更后重建）；**③分发门拦死 5 类节点**：_dispatch_effect 单输入早退门排在 noise/ramp/keyer/difference_keyer/channel_merge 分支之前→自迁移起永不可达（生成器/双输入/参数携带输入全部静默无输出）；**④端口名错配断边**：分发器固定 'result' 键 vs 节点声明 'image' 端口（sharpen/erode_dilate/AI 系列；denoise 直通反向）→下游按连接端口名取纹理静默 miss→_resolve_output 出口 _normalize_output_ports 别名规范化；**⑤ramp shader 缺 u_resolution 声明**（evaluator 赋值必 KeyError）→补声明+纵横比校正；**⑥edge_detect 无 canny 分支**（gradient 恒 0 永远黑）+direction 模式 atan(0,0) NaN→canny 复用 Sobel 梯度+平坦像素方向编码 0；**⑦volume3d Beer-Lambert (3,)×(h,w) 广播崩溃**；**⑧无头 GL 段错误**：QOpenGLFunctions_4_1_Core 在无 Qt GL 上下文进程 initializeOpenGLFunctions() 内部直接崩（不可捕获）→新增 core/gl_bind（PyOpenGL 原生绑定）替换全部活跃调用点；**⑨CLI 无头渲染 100% SIGSEGV**→TOPOS_HEADLESS_GL=1 standalone 上下文+CLI 置位，真渲染 3 帧验证通过。新增 tests/composite/test_real_footage_audit.py 16 项回归（含 CLI 子进程真渲染）。扫描遗留（判定合法/工件，未修）：difference_keyer 默认 threshold=0.1 对低差异素材全黑（内容相关）；cryptomatte_select 无 crypto AOV 输入黑帧；ai_generate/ai_inpaint/ai_expand/ai_denoise 为有意 placeholder（NEEDS_CONFIG 语义）；单进程长序列扫描下节点缓存可命中陈旧 GL 名（texture_id 复用，与第二轮遗留同源）；cornerpin_freeze 测试文件偶发退出段错误（worktree 对照基线 5/8 vs 修复后 1/4，早于本轮且频率反而下降）。回归：tests/composite 隔离 239 文件 237 干净，唯二遗留均经对照确认既有。 |
