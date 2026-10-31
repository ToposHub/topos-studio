# Topos Composite 合成页面优化计划（2026-09-07）

> **文档用途**：基于 2026-09-07 对照 Nuke / DaVinci Fusion / After Effects 的合成页差距调研（4 路并行代码审查）生成，供后续 AI / 开发者逐项执行。每个任务自包含：背景、证据、实现步骤、验证方法、验收标准，可独立领取与验收。
>
> **关联文档**：
> - 姊妹篇：`docs/edit_optimization_plan_2026-09-07.md`（剪辑页计划，**当前正在执行中**）。两计划**并行推进**：本计划中依赖剪辑页产出的任务已集中移入 **Batch E 硬门禁批**（第 6 节）——OFX 宿主（E2）必须等剪辑页计划**全部完成**、comp clip 缓存 GPU 化（E1）必须等剪辑页 B3 完成；稠密光流（D2）由剪辑页侧主导决策。并行协调规则见第 0.6 条。剪辑页计划文档本身不做改动（执行中）。
> - 历史自审：`docs/plans/archive/composite_page_professional_gap_audit_optimization_plan_2026-08-27.md`（其 P0 项——时间语义、Despill/Light Wrap、Matte 精修——**已修复**，本计划不重复）
>
> **已核实现状提醒**：AI 节点中 `ai_inpaint` / `ai_expand` 有完整求值路由（`evaluators/ai_evaluator.py` → `render/ai_compute_manager.py` → LaMa ONNX），**基本可用**；`ai_generate` / `ai_remove` 无模型映射，为占位符（见 B6）。

---

## 0. 执行须知（AI 执行者必读）

1. **每个任务独立提交**，执行顺序建议见第 8 节。
2. 遵守 `AGENTS.md` 工作流：动手前先 Explore 目标模块 → 实现 → 自查。
3. 文中行号为 2026-09-07 快照，可能漂移；以符号名（类名/函数名）为锚点重新定位。
4. 验证一律包含：pytest 单测（新增测试随任务落地）+ 手工冒烟（`run_topos.command` / `run_topos.sh` 启动）+ 涉及渲染/导出的任务用截图对比或 `ffprobe` 验证产物。
5. **合成测试稳定性注意**：历史审计记录过全量 composite 测试在 pytest 下有 teardown 段错误/挂起风险（见 `docs/r11_composite_segfault_findings_2026-08-21.md`）。若复现，按该文档记录的进程隔离方式跑测试，并在任务报告中注明，不要静默跳过。
6. **并行执行协调规则**（本计划与剪辑页计划同时由不同执行者推进时必读）：
   - **本计划领地**：`src/features/composite/**`、`src/features/color/tracking/`（仅 A1 迁移期）、`src/shared/**`（以新增目录/文件为主）、`tests/composite/`。**除 E1 门禁通过后的指定接线点外，禁止修改 `src/features/edit/**` 的任何文件**。
   - **剪辑页计划领地**：`src/features/edit/**`、`src/features/deliver/**`。已知它会增量修改 `src/shared/ui/shortcut_manager.py`（其 B5 任务），与本计划无文件交集。
   - **冲突热点**（双方都会动，E1 开工前必须确认剪辑页侧已收尾）：`src/features/edit/core/render_cache_worker.py`、`src/app/controllers/edit_controller.py`（剪辑页 B3 vs 本计划 E1）。
   - **Git 纪律**：两计划各用独立分支小步提交；Batch E 任何任务开始前必须 rebase 最新剪辑页分支，并按第 8 节"E 批启动前检查清单"逐项确认。

---

## 1. 任务总览

| 编号 | 任务 | 批次 | 优先级 | 规模 | 依赖 |
|------|------|------|--------|------|------|
| A1 | 跟踪能力接入合成页（Tracker 节点 + match-move） | A | P0 | L | 无 |
| A2 | 稳定器（Stabilize） | A | P0 | M | A1 |
| A3 | 并行帧渲染（导出帧级并行 + 分组执行接线） | A | P0 | L | 无 |
| A4 | 镜头畸变节点（Lens Distortion） | A | P0 | M | 无 |
| A5 | Transform 运动模糊（Motion Blur） | A | P0 | M | 无 |
| A6 | EXR 多层多通道读取 + Shuffle 节点 | A | P0 | M | 无 |
| B1 | 查看器用户级 ROI 局部渲染 | B | P1 | S | 无 |
| B2 | 查看器补齐：代理档位菜单 / A-B 对比 / 放大镜 / 黑场背景 | B | P1 | M | 无 |
| B3 | Group 进出编辑接线 + 参数 Publish | B | P1 | M | 无 |
| B4 | 画布内节点重命名 | B | P2 | S | 无 |
| B5 | 时间节点 reverse / loop 模式 | B | P1 | S | 无 |
| B6 | ai_remove 节点接线（LaMa + 遮罩输入） | B | P2 | S | 无 |
| C1 | 2D 粒子系统（无状态确定性仿真） | C | P1 | L | 无 |
| C2 | Paint 节点（克隆/擦除画笔） | C | P1 | L | 无 |
| C3 | Text+ 增强（逐字符动画 / 沿路径文字） | C | P1 | M | 无 |
| C4 | 脚本图操作接线（增删节点/连线进 edit_service） | C | P2 | M | 无 |
| C5 | Read/Write 版本切换（version binning） | C | P2 | S | 无 |
| C6 | OCIO 贯通 viewer/IDT + ACEScg 工作空间 | C | P1 | M | 无 |
| C7 | 网格变形节点（Mesh Warp）（原编号 C8） | C | P2 | M | 无 |
| D1-D8 | 预研批（见第 7 节，不在本计划展开） | D | — | — | 需人工决策 |
| E1 | comp clip 渲染缓存 GPU 化（原编号 C7，复活 graph_render_service） | E | P1 | M | ⛔ 硬门禁：剪辑页计划 **B3 完成** |
| E2 | OFX 插件宿主接入合成页（原 D3 预研项提升） | E | P1 | L | ⛔ 硬门禁：剪辑页计划**全部完成** |

规模定义（AI 辅助开发口径）：S ≤ 半天；M = 1~2 天；L = 3 天以上或跨线程/架构联动。

---

## 2. 全局工程约束（所有任务生效）

1. **求值架构**：UI 线程只生成不可变 `GraphSnapshot`（`core/graph_snapshot.py`），渲染线程按 `RenderPlan.eval_order` 求值（`evaluators/base_evaluator.py:579`）。**新节点必须遵守此模式**：参数经 `ParamDef` 声明（含 `allow_expression` / 动画位），求值在渲染线程完成，禁止求值过程碰 UI。
2. **节点注册全流程**：新节点 = `nodes/xxx_node.py` + `NODE_REGISTRY` / `NODE_CATEGORIES` 注册（`core/node_engine.py:110-306`）+ `SEARCH_ALIASES`（**含中文别名**）+ `DESCRIPTION` / `TAGS` + `node_capabilities.py` 能力声明（GPU/CPU、fusible、tile padding）。缺一不可，注册表一致性有测试看守。
3. **序列化与迁移**：图 schema 走 `format_version` / `schema_version`（当前 v10，`core/graph_migration.py`）。新增节点/字段需 bump 版本并写迁移（旧图加载后新字段取默认值）；`utils/serialization_formats.py` 三种格式（JSON/MsgPack/gzip）都要兼容。
4. **撤销**：图结构修改必须走 `ui/node_graph_commands.py` 的命令体系（树状 undo 栈 `core/undo_stack.py`）；参数修改走现有参数提交路径（含表达式互斥规则 A-15）。
5. **GLSL 规范**：严格类型、数字字面量带 `.0`、循环边界常量；空间效果节点必须正确声明 tile padding（接 `core/tile_padding_computer.py`），否则分块渲染出错边。
6. **色彩**：图内一律 scene-linear；源节点经 IDT 进图（`base_evaluator.py:2140`）；新效果节点在 linear 域运算；查看器显示经 ODT。
7. **缓存正确性**：节点缓存 key 为内容寻址（`render/cache_manager.py::compute_key`：上游纹理 revision + 参数 revision + ROI + LOD + 帧号）。新增可动画/影响输出的参数时，**确认参数变化触发端口 revision**（`graph_snapshot.py` PortRevision），否则缓存不失效——这是最容易踩的坑，必须写测试。
8. **线程与 GL 上下文**：ModernGL 上下文绑定线程。任何并行化任务（A3/E1）中新建线程必须自建（或共享）上下文并加 fence 同步，参考 `render/composite_renderer.py` 的既有模式。
9. **模块边界**（`docs/module_boundary_rules.md`）：feature 之间只允许 import 对方的 `module.py` / `facade.py` 边界模块。**composite 不得直接 import `src.features.color.*` 内部模块**——A1 的架构前提，见该任务步骤。
10. **每任务回归基线**：`pytest tests/composite -x -q`（必要时按第 0.5 条隔离方式）+ 全局 `pytest tests/ -x -q` 的 composite 相关子集 + 启动冒烟 + 打开/保存现有 `.topos` 工程与合成图 JSON。

---

## 3. Batch A — 核心专业能力（P0）

### A1 跟踪能力接入合成页（Tracker 节点 + match-move）

#### 背景与现状
项目已有一套质量很高的 2D/平面跟踪器，但**只在 color 页生效**：
- `src/features/color/tracking/tracker_2d.py`（49KB）：金字塔 LK 光流 + forward-backward 往返校验 + RANSAC + NCC 外观门控 + 五因子置信度；
- `homography_stability.py` + `corner_pin.py`：单应性平面跟踪 + 四角 corner-pin 导出；
- `tracking_store.py` / `tracking_service.py` / `tracking_worker.py`（QThread）/ `tracking_evaluator.py`（跟踪驱动 color 页遮罩）；
- 测试夹具 `tools/make_tracking_fixtures.py`（带 ground truth）+ 约 20 个 `tests/color/test_tracker_*`。

合成页 `transform_node` / `cornerpin_node` 与跟踪数据**零桥接**（composite 目录对 color.tracking 引用为 0）。专业合成的核心日常"跟踪 → match-move 到 CornerPin/Transform"做不了。

#### 架构决策（已定，勿改）
按模块边界规则，跟踪**纯算法**迁移到 shared，两页共用：
- 迁移（纯 numpy/cv2，无 Qt 依赖的）：`tracker_2d.py`、`track_data.py`、`types.py`、`feature_detector.py`、`point_manager.py`、`homography_stability.py`、`corner_pin.py`、`coordinate_map.py`、`merge.py`、`frame_sampler.py`、`tracking_cache.py` → 新建 `src/shared/tracking/`；
- 留在 color 页（Qt/页面耦合的）：`tracking_worker.py`（QThread）、`tracking_service.py`、`tracking_store.py`、`tracking_evaluator.py`、`shape_mask.py`——改为从 shared import 算法；
- composite 经 `src.shared.tracking` 直接使用，**不违反边界**。
- 执行迁移时逐文件核对 import（执行第一步就是列每个文件的依赖清单），有 Qt 依赖的留下。

#### 实现步骤
1. **迁移**（纯机械，先做先测）：移动文件 → 全局更新 import（color 页 + tests/color）→ `pytest tests/color -k tracker` 全绿。shared 不依赖 feature，方向合法。
2. **新节点 `tracker_node.py`**（类别 Transform 或新 Tracking 类）：
   - 输入：`image`（跟踪源）；参数：跟踪区域（center/size）、模式（point / planar）、参考帧、搜索/模板半径、置信度门限（透传 `TrackingMode` 与 tracker_2d 现有参数）；
   - 输出：`image`（直通）+ `matte`/数据口——跟踪结果存节点自身的 track data（可序列化，schema 迁移）；
   - 属性面板加 "Analyze" 按钮 → 起 QThread 跑 `tracker_2d`（纯 numpy 安全），进度 + 取消，完成后置信度曲线小图（复用 scene cut 曲线图控件模式）。
3. **应用（match-move）三种去向**，属性面板 "Apply" 区：
   - **Bake to Transform**：把逐帧平移/缩放/旋转（点跟踪经 `merge.py`/`coordinate_map.py` 的既有分解，平面跟踪经单应性分解）烘焙为下游 `transform_node` 参数关键帧（用现有 `utils/keyframe.py` + 关键帧命令，`scripting/composite_api.py` 的 add_keyframe 路径可直接复用）；
   - **Bake to CornerPin**：平面跟踪 → 四角动画（`corner_pin.py` 已有逐帧四角导出）；
   - **Live 绑定**（可选增强）：跟踪求值与图求值融合（像 color 页 `tracking_evaluator.py` 那样帧驱动）——复杂度高，默认先交付烘焙；若做，参考 `tracking_evaluator.py` 的失败帧回退策略（失败帧回退基准，不造假数据）。
4. **从 color 页导入已有跟踪**：color 的 `tracking_store` 数据经 color `module.py` 边界暴露只读导出（JSON），合成页 tracker_node 提供 "Import from Color Page…" 入口解析为节点 track data。
5. 注册全流程（约束 2）+ i18n（zh/en）+ 节点帮助（`src/shared/node_help/`）。

#### 验证方法
- 自动化：新增 `tests/composite/test_tracker_node.py`——用 `tools/make_tracking_fixtures.py` 的平移/TRS 夹具：跟踪→烘焙到 Transform→用已验证的求值链路断言帧间位移与 ground truth 误差 ≤ 夹具已有的容差（参考 `tests/color/test_tracker_*` 的阈值）；undo 烘焙还原；序列化往返（含 track data）；迁移后 `tests/color -k tracker` 全绿。
- 手工：真实素材（手机横移视频）→ tracker 节点分析 → 烘焙 CornerPin → 叠 logo 跟随移动；置信度低帧人工复核。
- 回归：模块边界测试（若有 import-linter 类检查）通过。

#### 验收标准
迁移零回归；point/planar 两模式可分析、可烘焙到 Transform/CornerPin、可撤销、可序列化；color 页导入入口可用。

#### 风险与回退
单应性分解（H → translate/scale/rotate）在纯透视时有歧义——`coordinate_map.py` 已有分解实现，优先复用；若分解不稳定，corner-pin 路径不受影响（不分解）。

---

### A2 稳定器（Stabilize）

#### 背景与现状
全库无 stabilizer。稳定 = 跟踪的逆向应用 + 平滑，依赖 A1 的 tracker_node。

#### 实现步骤
1. tracker_node 的 Apply 区加 **Stabilize** 模式：
   - 对逐帧变换取时间窗平滑（移动平均/高斯窗，窗口参数暴露，默认 15 帧）；
   - 输出 = 平滑变换的**逆**，烘焙到下游 transform_node 关键帧（position 为主，可选包含 scale/rotate）；
   - 边界：首尾窗口不足时用可得部分窗口；逆变换数值除零保护（scale≈0 钳制）。
2. 属性面板预览：应用前后对比开关（临时禁用烘焙结果即可复用现有 Compare/wipe）。
3. i18n + 帮助。

#### 验证
- 自动化：合成抖动序列夹具（在平移夹具上加正弦抖动）→ stabilize → 逐帧位移方差显著下降（断言降至 <10% 原方差）；undo；序列化。
- 手工：手持素材稳定前后播放对比；极端运动素材不崩、有边界裁切提示（稳定后边缘露黑可接受，注明即可）。

#### 验收
稳定效果量化达标、可撤销、参数（窗口/包含项）生效。

---

### A3 并行帧渲染

#### 背景与现状
`RenderPlan` 已计算 `parallel_groups` / `max_parallelism`（`render/render_scheduler.py:380-381、570、667`），**src 内零消费者**；执行端 `base_evaluator.py:579` 是单线程顺序循环；导出 worker `render/composite_render_worker.py` 逐帧串行。Nuke 的 multi-frame 对应能力等于"算好了没执行"。

#### 范围决策（重要，勿扩大）
分两个交付物，**帧级并行优先**（收益最大、风险可控）：
- **交付物 1：导出帧级并行**（本任务主体）；
- **交付物 2：帧内 parallel_groups 分组执行**（仅 CPU 节点卸载，GPU 节点保持渲染线程串行——GL 上下文绑定限制下的务实选择；作为可选阶段 2，默认不阻塞验收）。

#### 实现步骤（交付物 1）
1. 导出路径改造 `render/composite_render_worker.py`：
   - 创建 N 个渲染 worker（N = min(用户偏好, CPU 核数/2, 4)，默认 2），**每个 worker 独立 GL 上下文**（模式参考 `render/composite_renderer.py` 与 `composite_frame_renderer.py` 自建 context 的既有做法）；
   - 帧任务队列：各 worker 认领帧号 → 各自完整求值该帧 → 结果按**帧号有序**送出：
     - 图片序列输出：各帧独立文件，并行写安全（文件名含帧号）；
     - 视频输出：帧并行**渲染**、**串行编码**（有序队列喂 `FFmpegVideoEncoder`，编码器绝不并发访问）；
   - AOV 多 pass 输出同样按帧并行。
2. 内存预算：N 个上下文 = N 份纹理驻留，N 默认 2 并在偏好可调；GPU 资源池（`gpu_resource_pool.py`）按 worker 实例化，互不共享；VRAM 不足时自动降 N=1（捕获 context 创建失败回退）。
3. 进度/取消：合并各 worker 进度；取消信号广播。
4. 偏好页：Composite → "Export parallel frames"（默认 2）。
5. 阶段 2（可选）：`base_evaluator._evaluate_snapshot` 内对 `parallel_groups` 的组，把标记为 CPU-only 的节点（text 光栅、AI）提交线程池，GPU 节点留主线程；需要 evaluator 实例按线程拆分或加锁——**仅在阶段 1 稳定后启动**。

#### 验证
- 自动化：`tests/composite/test_parallel_export.py`——20 帧测试图，N=1 与 N=4 输出**逐帧逐像素一致**（并行不改结果）；取消中途不残留半写文件（临时文件+rename）；异常 worker 崩溃时任务报错而非挂起。
- 基准：新增 `tools/bench_parallel_export.py`：100 帧 1080p 图 + 10 节点链，报告 N=1/2/4 加速比（预期 N=2 ≥1.6x；不达标分析瓶颈——纹理上传/编码串行段，记录即可）。
- 手工：真实合成图导出 ProRes 与 PNG 序列各一次，产物与串行版抽查一致。

#### 验收
并行=串行逐像素一致；视频路径帧序正确；默认配置下 4K 合成导出提速 ≥1.5x 或给出瓶颈分析；取消/异常安全。

---

### A4 镜头畸变节点（Lens Distortion）

#### 背景
无任何镜头畸变能力（`reformat_node` 的 distort 只是拉伸缩放）。matchmove 工作流必需：素材先 undistort → 跟踪/合成 → 再 distort 回去。与 A1 协同。

#### 实现步骤
1. 新节点 `nodes/lens_distortion_node.py`，类别 Filter：
   - 参数：`mode`（undistort / distort）、`k1 / k2 / k3`（径向 Brown-Conrady）、`p1 / p2`（切向）、`center_offset_x / y`、`edge_mode`（black / clamp / mirror）、`filter`（bilinear 默认）。全部 OpenCV 约定，与 cv2 互算方便。
2. 实现策略（性能关键）：
   - UV 映射用 `cv2.initUndistortRectifyMap` + `cv2.undistort` 的反算生成**逐像素采样表**（浮点 RG 纹理，一次计算按参数 revision 缓存）；
   - GPU shader 只做双线性采样表查找 warp（新 GLSL 进 `core/shaders.py`，遵守约束 5；tile padding 声明 = 估计最大位移边界，简化为 `max(|k1|,|k2|,|k3|)*width*0.15` 起步并留参数）；
   - distort 模式 = 用畸变系数的逆映射表（cv2 `estimateNewCameraMatrixForUndistortRectify` 反向构造，或数值迭代求逆——实现时选 cv2 官方逆路径）。
3. 数值一致性：CPU 路径（numpy+cv2，供导出回退与测试）与 GPU 路径共享同一采样表 → 一致性有保障。
4. 注册全流程 + 中文别名（"镜头畸变/去畸变"）+ i18n + 帮助。

#### 验证
- 自动化：`tests/composite/test_lens_distortion.py`——(1) 已知 k1 值下网格图畸变中心位移符号/单调性正确；(2) undistort→distort 往返与原图 PSNR ≥ 45dB（k 值小时）；(3) GPU vs CPU 一致（有 GL 测试基建时）；(4) tile 分块渲染开启时与整帧渲染一致（padding 正确性）；(5) 序列化往返。
- 手工：实拍广角素材 undistort 后直线变直；与 A1 联动：undistort → 跟踪 → distort 工作流走通。

#### 验收
两种模式数值正确、tile 安全、GPU/CPU 一致、与跟踪工作流联动可用。

---

### A5 Transform 运动模糊（Motion Blur）

#### 背景
`nodes/transform_node.py` 只有 translate/scale/rotate（L42-45），无 shutter——动画变换无运动模糊，只能手动喂 velocity 图给 Blur(motion)。Nuke MotionBlur / AE 层运动模糊对应物缺失。

#### 实现步骤
1. `transform_node.py` 增参数：`motion_blur`（bool）、`shutter`（0.0–1.0，快门开角比例）、`samples`（2–16，默认 8）、`shutter_offset`（-0.5–0.5，默认 0）。
2. 求值（transform mixin，`evaluators/` 组合内）：运动模糊开启且目标参数有动画时：
   - 在 `[frame - shutter/2, frame + shutter/2]` 均匀取 `samples` 个**子帧时刻**；
   - 每时刻用关键帧系统采样动画参数（采样接口与正常求值同源，支持表达式驱动——表达式含 `frame` 变量，按子帧时刻求值）；
   - 各子帧按现有 transform shader 渲染一次，**等权累积平均**（GPU ping-pong blend，权重 1/samples；CPU numpy 平均）；
   - 无动画时直通单次渲染（零开销路径）。
3. 确定性：同一帧同一图 → 相同结果（采样时刻由 frame+参数闭式决定，无随机）——缓存 key 覆盖（shutter/samples 进参数 revision，写缓存失效测试）。
4. 性能：交互预览 quality_profile=draft/preview 时 samples 自动减半（`core/quality_profile.py` 联动），final 全采样。
5. 注册元数据 + i18n + 帮助。

#### 验证
- 自动化：`tests/composite/test_transform_motion_blur.py`——(1) 线性平移动画 + shutter=1 时输出为 N 个位置等权叠加（与手算期望 PSNR ≥ 50dB）；(2) samples=1 且 shutter=0 退化为原图；(3) 静态参数零开销直通（输出与关闭时逐像素一致）；(4) 表达式驱动动画同样生效；(5) 序列化往返；(6) 缓存失效正确。
- 手工：快速平移 logo 开/关运动模糊对比；导出对比预览一致。

#### 验收
公式正确、退化路径零副作用、quality profile 联动、缓存正确失效。

---

### A6 EXR 多层多通道读取 + Shuffle 节点

#### 背景
`core/image_loader.py:189 _decode_exr` 用单 `OpenEXR.InputFile`，只读 RGBA + 单 Z 通道，无 multi-part、无任意 AOV 通道选择。CG 管线（渲染器输出的多层 EXR）进不来。

#### 实现步骤
1. **加载器扩展**：
   - `MultiPartInputFile` 读取：枚举 parts（名称/类型）与每 part 的 channels（名称/类型/采样）；
   - `DecodedImage` 增 `extra_channels: dict[str, np.ndarray]`（通道名如 `diffuse.R`、`crypto_asset00`）与 `parts: list[str]` 元数据；默认行为不变（第一 part 的 RGBA+Z），**按需惰性解码所选通道**（OpenEXR 支持通道选择读取，控制内存）；
   - 头信息探测独立函数（只读 header 不解码像素，供 UI 下拉）。
2. **image_input_node 扩展**：参数增 `exr_part`（下拉，来自 header 探测）、`extra_channel_set`（多选，进 `extra_channels`）；属性面板探测按钮（读 header 填充选项）。
3. **新节点 `shuffle_node.py`**（类别 Channel）：把输入的任意命名通道映射到输出 RGBA（每输出通道下拉选源：R/G/B/A/任意 extra 通道/常数 0/1），带 premult 开关；求值走 GPU 纹理通道重组（零算子采样）或 CPU numpy 索引（简单，先 CPU 亦可——通道重排不是热点）。
4. 序列化：节点参数含动态通道名列表——schema 兼容（缺省空）；缓存 key 含 part/通道选择（参数 revision 覆盖，写测试）。
5. 注册 + 中文别名（"通道重排/打乱"）+ i18n + 帮助（写清与 `channel_merge_node` 的分工：shuffle 是静态映射，merge 是动态合成）。

#### 验证
- 自动化：构造 multi-part EXR 测试夹具（OpenEXR 写 2 parts × 多通道，`tools/` 加生成脚本或 pytest fixture 内生成）——读取枚举正确；shuffle 映射输出像素值正确；旧单 part 文件回归不受影响；惰性解码只读所选通道（内存/时间断言可选）。
- 手工：用 Blender/渲染器导出的多层 EXR（ diffuse/specular/Z/crypto_*）加载 → shuffle 重组 → 合成。

#### 验收
multi-part 枚举/选择/惰性解码可用；shuffle 正确；旧文件零回归。

---

## 4. Batch B — 查看器与图编辑补齐

### B1 查看器用户级 ROI 局部渲染

#### 背景
仅内部 tile ROI（`core/tile_scheduler.py`），无用户框选渲染区域——复杂图调试时全帧渲染浪费。缓存 key 已含 ROI（`cache_manager.compute_key`），数据层就绪。

#### 实现步骤
1. 查看器加 ROI 工具（工具栏按钮 + 快捷键，避开已有 R（roto）——建议 `Shift+R`，注册前查冲突）：拖拽画矩形 → 视口内以暗化遮罩显示未选区；
2. ROI 状态存 viewport state（已有视口状态保存/恢复机制），并传入 `RenderPlan` → `tile_scheduler` 只求值 ROI 区域，显示时按原位缩放绘制；
3. 快捷键清除 ROI（`Ctrl+Shift+R`）+ "Full Frame" 菜单项；
4. 像素探针（`PixelInspectorBar`）在 ROI 模式下读数坐标仍显示**原始帧坐标**（换算，别让用户糊涂）。

#### 验证
- 自动化：ROI 渲染结果与全帧渲染的对应区域逐像素一致；ROI 恢复/清除往返；状态持久化。
- 手工：重节点图上框选小区域 → 交互明显变快；探针读数正确。

#### 验收
ROI 交互可用、结果与全帧一致、状态可持久化。

---

### B2 查看器补齐：代理档位菜单 / A-B 对比 / 放大镜 / 黑场背景

#### 背景
四项小缺口：内部代理固定（`composite_page.py:1364-1670` proxy_scale / `quality_profile.py`）无用户菜单；无 A/B 双缓冲对比（Compare 节点只是节点级）；无放大镜；有棋盘格（`composite_viewport.py:9996`）无黑场切换。

#### 实现步骤（四个子项，可分别提交）
1. **代理档位菜单**：视口 View 菜单加 Resolution：Full / Half / Quarter / Auto——接线到既有 `proxy_scale`（force_lod LOD1/LOD2 路径已存在），仅改 UI 与状态持久化，不动求值层。
2. **A/B 缓冲**：视口维护两个快照缓冲槽；快捷键（建议 `A`/`Shift+A` 或菜单，避开工具键冲突——现 1 键是节点预览，用 `Ctrl+Shift+A` 存入 A 槽）"存当前到 A/B"；开启 Compare 时 wipe 分割线对比 A vs 实时（复用 `split_compare_mode` L913 与可拖分割线 L6878 的既有实现，只是第二输入从"选中节点"换成 A 槽）。
3. **放大镜**：光标跟随的 4x/8x 最近邻放大窗口（直径 ~160px 圆形或方形），像素级看清边缘；开关进 View 菜单。
4. **黑场背景**：棋盘格切换旁加黑场/白场选项（三态：checker/black/white），alpha 预览逻辑不变。

#### 验证
- 自动化：各子项状态持久化往返；代理档位与 force_lod 联动后输出分辨率正确；A 槽快照独立于后续参数修改（改参数后 A 侧不变）。
- 手工：逐项操作 + 半分辨率下抠像边缘检查用放大镜的典型场景。

#### 验收
四子项独立可用、互不干扰、状态可持久化。

---

### B3 Group 进出编辑接线 + 参数 Publish

#### 背景
`ui/group_commands.py` 已有 `EnterGroupCommand` / `ExitGroupCommand`（is_expanded 展开/收起），**全库无 UI 调用**；无参数 publish（组内参数提升到组面板）。Group 目前只是"选中集合+视觉组织"。

#### 实现步骤
1. **进出接线**：节点编辑器双击 Group 节点（或右键 "Enter Group"）→ 执行 `EnterGroupCommand` → 画布过滤显示该组成员 + 面包屑栏（`根图 / Group1`，点击回退）+ 工具栏返回按钮；Esc / 面包屑退出。进出均入 undo 栈（命令已存在，只接 UI）。
2. **外部连线锚点**：进入组内时，组边界端口显示为虚拟锚点节点（组输入/输出），让用户能在组内连接到外部——若现数据模型不支持，MVP 退化为"组内编辑仅限内部连线"，锚点列为后续（如实标注）。
3. **Publish（MVP 用表达式链接实现）**：组属性面板加 "Promoted Parameters" 区；组内节点参数的属性面板/关键帧树提供 "Promote to Group" 右键动作 → 在 Group 节点生成同名代理 `ParamDef`，用引擎既有 `set_parameter_binding`（`node_engine.py:588-640`，跨节点参数绑定自动生成表达式）链接到成员参数；改组参数即改成员参数，撤销走绑定命令。
4. 序列化：promoted 列表随 Group 节点存（schema bump）；进出状态不入存档（视图态）。

#### 验证
- 自动化：进出命令往返后图不变（或仅 expand 标记变化）；promote 后改组参数 → 成员参数联动（求值断言）；解除 promote 后代理参数移除；序列化往返。
- 手工：建 3 节点组 → 进入改参数 → 退出确认生效；promote 一个 blur 半径到组面板；undo 各步。

#### 验收
进出可撤销；promote 联动可用；序列化兼容。

---

### B4 画布内节点重命名

#### 背景
节点显示名只有自动编号（`assign_instance_index` 的 Camera1/Blur2），画布无重命名 UI（preset 重命名不算）。

#### 实现步骤
1. 节点标题双击（或选中 F2）→ 内联编辑框 → 提交走命令：查 `node_graph_commands.py` 是否已有 rename 命令，无则新增 `RenameNodeCommand`（改 `user_label`，None 时回落自动编号）；
2. `user_label` 序列化（节点 dict 增可选字段，schema 兼容）；图搜索（`graph_node_search_popup.py`）匹配 user_label 优先；
3. F2 冲突检查（若已被占用换 `Ctrl+R`——注意 3D 视口 ROTATE 用 R，本任务只在节点画布焦点态绑定）。

#### 验证
自动化：命令往返+序列化+搜索命中；手工：改名后 minimap/属性面板/关键帧树同步显示。

#### 验收
全 UI 同步显示 user_label；可撤销；旧图兼容。

---

### B5 时间节点 reverse / loop 模式

#### 背景
`core/time_remap.py` 闭式映射 `round(frame*speed)+offset`（L54-56）已覆盖 offset/hold/retime，但无倒放（reverse）与循环（loop）模式。

#### 实现步骤
1. `TimeRemap` 增 `reverse: bool = False` 与 `loop: Optional[tuple[int, int]] = None`（源 in/out 帧区间）：
   - reverse：`source = round(frame*speed) + offset` 后再做 `source = out - (source - in)`（等价负速度，但保持 speed 语义干净、组合数学可折叠）；
   - loop：映射后 `source = in + (source - in) % max(1, out - in + 1)`；
   - 组合折叠（`compute_frame_overrides` 逆向拓扑合成）需同步扩展：两个 TimeRemap 复合的闭式化简——reverse 不与 speed 折叠时可退化为逐节点应用（保底正确，性能影响小，注明即可）。
2. `retime_node.py` 增参数 `reverse`（bool）、`loop_mode`（off/loop，pingpong 可选加分）；UI 中文标签。
3. 图层 in/out 帧门控（`compute_layer_frame_gates`）与 reverse/loop 的交互：门控作用于 timeline 帧域，不受影响——写测试确认。
4. 序列化兼容 + 缓存：time 类节点是帧相关节点（已在 7 类脏条件内），帧号进缓存 key，确认 reverse 后不同帧不复用缓存。

#### 验证
- 自动化：扩展 `tests/` 的 time_remap 测试——reverse 帧序、loop 取模边界（恰好整除、out=in）、reverse+loop 复合、与 frame_hold/retime 链式折叠、图层门控正交性、序列化。
- 手工：素材倒放、循环 3 次、倒放+循环组合播放正确；导出一致。

#### 验收
数值表全过、链式折叠正确或保底逐节点正确、导出一致。

---

### B6 ai_remove 节点接线（LaMa + 遮罩输入）

#### 背景
`nodes/ai_remove_node.py` 是 `PlaceholderNode`，`ai_compute_manager` 无映射。而 `ai_inpaint`（同模型 LaMa）已通——ai_remove 语义 = "遮罩区域内容移除"，与 inpaint 共用模型，只差输入约定。

#### 实现步骤
1. `ai_compute_manager.py` 的模型映射表（L451 区域）加 `"ai_remove": "lama_inpaint"`；
2. `ai_remove_node.py` 从 `ai_inpaint_node.py` 的参数/端口模式派生（mask 输入口 + 输入图），保留 NOT-IMPLEMENTED 仅当模型缺失时的显示（模型管理器已有 lama 注册）；
3. 属性面板文案区分：inpaint（修补 mask 区）/ remove（擦除 mask 区物体）——参数集先保持一致，模型相同；
4. 与 C2 Paint 的联动留接口（stroke → mask），本任务不依赖 C2。

#### 验证
- 自动化：mask 夹具（`tools/` 已有 AI 测试物料路径约定）求值输出非空且 mask 外像素不变（LaMa 只改 mask 内的容差断言）；缓存命中（同参数同帧二次求值走 EXR 磁盘缓存）。
- 手工：实拍素材 + roto 遮罩 → ai_remove → 物体被移除。

#### 验收
节点真实可用、mask 外不变、缓存生效。

---

## 5. Batch C — 大件与工作流

> ⚠️ **编号变更说明**：原 C7（comp clip 渲染缓存 GPU 化）因需修改 `src/features/edit/core/render_cache_worker.py`、与剪辑页计划 B3 共享文件域，**已移至 Batch E（E1）并加硬门禁**，本批内不可执行。原 C8（网格变形）相应重编号为 C7。

### C1 2D 粒子系统（无状态确定性仿真）

#### 背景
节点注册表无 Particle 类别；teaching_animation 的"粒子预设"是关键帧化 SVG 形状。Fusion pEmitter / AE Particular 对应物为空。

#### 设计要点（架构匹配，勿偏离）
合成求值是 **pull-based + 不可变快照**，粒子仿真必须**无状态、可按任意帧独立求值**（不能逐帧积分携带状态）。方案：**闭式粒子轨迹**——粒子 i 的出生帧 `b_i = f(seed, rate, i)`，任意目标帧 t 时若 `b_i ≤ t < b_i + lifetime`，位置 = 从 `b_i` 到 t 对固定力场（重力/风/常量湍流用 `noise(seed, age)` 确定性函数）的**解析/数值积分**（每粒子独立、可从出生帧重放积分到 t，粒子数 ≤ 数千时 CPU 每帧重放可接受；缓存逐帧命中后无重复计算）。

#### 实现步骤
1. 新节点 `nodes/particle_node.py`（新 Particle 类别）：
   - 发射：region（点/矩形/椭圆，复用 SDF 函数）、rate（个/帧）、seed、lifetime（可带随机分布）、初始速度（speed/spread/方向）；
   - 力：gravity(x,y)、wind(x,y)、turbulence（幅度+频率，确定性 noise，可复用 `noise_node.py` 的 GLSL/CPU noise 保持一致）；
   - 外观：size（出生→死亡 ramp，复用 ramp 采样）、颜色 ramp、opacity ramp、形状（圆点/软圆/线段 streak）；
   - 输出：RGBA 图（黑底预乘 alpha 出图，供 Merge over）。
2. 渲染：MVP 用 CPU numpy 光栅化（软圆 splat，几千粒子 1080p 可实时；确定性）+ GPU 提升留阶段 2（instanced points，注意不是本任务验收必需）；
3. 动画：全部发射/力参数可打关键帧（出生帧重放积分时力场参数按 `b_i..t` 逐子帧采样动画值——支持动画参数，工程量大可先标注"动画参数支持为阶段 2"，静态参数先验收）；
4. 序列化 + 缓存（帧号已进 key）+ 注册（中文别名"粒子/发射器"）+ 帮助 + i18n。
5. 阶段 2（可选记录不验收）：streak 按速度方向拉伸、sprite 输入图、GPU instancing、与 A5 运动模糊联动。

#### 验证
- 自动化：`tests/composite/test_particle_node.py`——(1) 确定性：同 seed 同帧两次求值逐像素一致；任意跳帧（t=100 直接求值）与逐帧求值到 t=100 的位置一致（无状态性核心测试）；(2) 无粒子存活帧输出全透明；(3) 重力弹道闭式解对照（抛物线，解析解 vs 积分误差容差）；(4) 序列化往返。
- 手工：拖尾/上升粒子/下雨三个参数组合出效果；播放流畅度目测。

#### 验收
无状态确定性成立（跳帧一致）、弹道对照通过、基础发射/力/外观可用。

---

### C2 Paint 节点（克隆/擦除画笔）

#### 背景
无传统绘画层（color 页 brush 只是遮罩笔刷）。Nuke RotoPaint / AE Clone Stamp 对应物为空，当前唯一修补手段是 AI。

#### 实现步骤
1. 新节点 `nodes/paint_node.py`（类别 Utility 或新 Paint）：持有**笔划列表**（每笔：类型 clone/erase/solid、点序列（含压感可选）、size/feather/opacity、源偏移（clone 用）、起止帧范围）；
2. 求值（CPU，节点在 CPU 名单）：
   - clone：`dst = dst + stroke_mask * (src_at(dst - offset))`（源 = 本节点输入图在偏移处采样，跟随帧）；
   - erase：alpha 减；
   - solid：颜色/alpha 涂抹；
   - 光栅化用 QPainter 路径 + 高斯羽化 mask（numpy），笔划数据可序列化、可动画帧范围（静态笔划 MVP）；
3. 查看器画笔交互：`viewer_tool_system.py` 加 `ViewerToolMode.PAINT`（建议 P 键，查冲突）：画笔光标圆圈、`[`/`]` 调大小、Alt+点击设克隆源点（随后笔划采样 = 落点−源点偏移）、Ctrl+Z 撤销上一笔（命令：AddStroke/RemoveStrokeCommand 入 undo 栈）；
4. 遮罩输出口：笔划并集输出为 mask（供 B6 ai_remove / keyer 联动——画一笔即 AI 擦除）；
5. 注册 + 中文别名"绘画/克隆/仿制图章" + i18n + 帮助。

#### 验证
- 自动化：克隆像素正确性（平移素材 + offset 笔划 → 期望区域复制，PSNR 断言）；erase alpha；笔划命令 undo；序列化往返；mask 输出口与笔画区域一致。
- 手工：克隆去瑕疵（经典流程）；画笔→ai_remove 联动擦物。

#### 验收
clone/erase 可用、undo/序列化完整、mask 输出口联动 AI。

---

### C3 Text+ 增强（逐字符动画 / 沿路径文字）

#### 背景
`nodes/text_node.py`（1331 行，QPainter CPU 光栅）是静态排版。缺逐字符动画修饰器（Text+ / AE 文字动画器）与沿路径文字。

#### 实现步骤
1. **逐字符修饰器**（MVP 集）：参数组 `per_char_animation`：模式（fade / slide_up / slide_left / scale_pop / rotate_in / typewriter）、`delay_per_char`（帧）、`overlap`、总时长自动 = 字符数×delay；实现：光栅化改为**逐字符绘制**（QPainter 逐字形路径，动画变换/alpha 按字符出生帧插值——全部 CPU，性能对几百字符可接受，含缓存）；
2. **沿路径文字**：MVP 路径源二选一：(a) 内置弧线/直线参数（arc 角度/半径），(b) 链接一个 roto_shape 节点的形状（经表达式/参数绑定取点采样）——字形沿路径切线旋转摆放；先交付 (a)，(b) 若参数联动顺畅一并做；
3. 动画参数全部可打关键帧（时间参数本身就是帧函数，天然兼容）；
4. 序列化兼容 + 注册别名"文字/标题/逐字"更新 + i18n。

#### 验证
- 自动化：逐字动画首/中/末帧的字符位置与 alpha 期望值断言（单行 5 字符数值表）；路径弧线的字形角度断言；序列化往返；缓存按帧失效。
- 手工：打字机标题、逐字弹出标题、弧形文字三例；预览=导出（PNG/视频抽帧对比）。

#### 验收
三例效果正确、预览=导出、可序列化。

---

### C4 脚本图操作接线（add_node / connect 进 edit_service）

#### 背景
`scripting/composite_api.py` 只支持参数/关键帧/表达式（`composite/edit_service.py` 的 KEYFRAME_OPS）；`ai/composite_plan.py` 已定义 `NodeGraphOperation`（add_node/connect/disconnect/set_param/remove_node，SUPPORTED_NODE_OPS）但**未接入 edit_service**。脚本与 AI 都不能改图拓扑。

#### 实现步骤
1. `edit_service.py` 增 NODE_OPS 处理：按 `composite_plan.py` 契约实现 add_node / remove_node / connect / disconnect / set_param 的 apply 分支，内部全部走现有 `node_graph_commands.py` 命令（单 undo 组，`CompositeScriptAPI` 的 bridge 已有主线程调度与 undo group 机制）；
2. 校验：环路检测、端口类型匹配、未知 node_type 拒绝——复用引擎现有校验，错误信息结构化返回（脚本可读）；
3. `scripting/examples/` 加 2 个合成示例（建图：输入→transform→blur→output；批量改参数）；
4. MCP/AI 侧同一协议自动受益（同协议），跑一次 `ai/composite_plan.py` 现有质量门测试。

#### 验证
- 自动化：`tests/composite/test_script_graph_ops.py`——脚本建图→断言快照拓扑与求值输出；非法操作（环路/坏类型）返回结构化错误且图不变；全部在一个 undo 组（一次 undo 整体回滚）。
- 手工：脚本面板跑两个示例脚本。

#### 验收
五类操作可用、错误安全、单组撤销、示例可跑。

---

### C5 Read/Write 版本切换（version binning）

#### 背景
无 Nuke 式版本管理：路径中的版本号切换（version up/down）、批量切版本。

#### 实现步骤
1. `image_input_node` / `file_output_node` 路径参数旁加版本识别：正则 `(v|ver|version)?[._-]?(\d+)` 取**最后一个**版本数字段（含 padding 保留）；
2. 属性面板版本步进器（▲▼）+ 快捷键（节点选中时 `[` / `]`，画布焦点态绑定，先查冲突）：glob 同目录同模式的其他版本（存在才可切），切换走参数命令（路径变更即参数 revision，缓存自动失效）；
3. 图级菜单 "Set All Reads to Latest Version"：批量把所有 image_input 切到各自最高版本（单 undo 组）；
4. 无版本模式的路径：控件隐藏不占位。

#### 验证
- 自动化：版本识别表（含 padding、无版本、多数字路径取末段）；切换 glob 命中与落空；批量切版本；序列化后仍保留版本切换能力。
- 手工：v001/v002 目录树切换；批量升版。

#### 验收
识别/切换/批量可用、无版本路径零干扰。

---

### C6 OCIO 贯通 viewer/IDT + ACEScg 工作空间

#### 背景
`composite/preferences.py:165-172` 明示"viewer/IDT 仍用内置数学，**仅导出走 OCIO**"（`nodes/file_output_node.py:341`、`render/composite_render_output.py:808`）。合成页无 ACEScg 选项（仅 color 页有）。`shared/color/color_transform_provider.py` 已有 OCIO/Builtin 双引擎抽象。

#### 实现步骤
1. **viewer ODT 走 provider**：`composite_viewport.py:8188-8217` 的 ODT 路径改为经 `ColorTransformProvider`（OCIO 引擎，缺失配置回退内置——provider 已有回退语义）；processor 按变换缓存（同变换只建一次），性能护栏；
2. **源 IDT 走 provider**：`base_evaluator.py:2140` 的 IDT 同样接 provider（GPU 侧：若 OCIO processor 可导出 LUT/着色器则用之；否则对 IDT 常见集合烘焙 3D LUT 缓存——`lut3d` 能力已有节点）；
3. **ACEScg 工作空间选项**：`core/color_space.py` 增 ACEScg（AP1 基色、线性）工作空间选项 + IDT 目标随之变 AP1；`composite_page.py` Color Space 设置下拉加入；merge 前自动对齐逻辑（`_auto_align_color_spaces`）覆盖 ACEScg；
4. 偏好页解除"仅导出 OCIO"说明文案，补 OCIO 配置路径选择（指向 ACES Studio Config 的说明保留）；
5. 回退安全：无 OCIO 配置时全链路自动回内置数学（行为与今天一致），**禁止启动失败**。

#### 验证
- 自动化：sRGB→ACEScg→sRGB 已知色往返（容差）；provider 缺失时回退路径不抛错；导出/预览同配置下一致（此前已走 OCIO 的导出为基准）。
- 手工：ACEScg 工作空间下打开 Rec.709 素材合成，预览/导出色彩一致；切换回内置数学对比无跳变。

#### 验收
viewer/IDT/导出三处同引擎、ACEScg 可选、无 OCIO 时安全回退。

---

### C7 网格变形节点（Mesh Warp）

#### 背景
变形类仅有 CornerPin；无网格变形（AE Mesh Warp / Fusion Grid Warp）、无液化。本任务与 A4（镜头畸变）共同补齐变形类。

#### 实现步骤
1. 新节点 `nodes/warp_node.py`（类别 Filter，别名"网格变形/变形"）：`grid_size`（4–16 每轴）、控制点偏移复用 roto 的**逐点动画通道模式**（`warp_point_{i}_x/y`，参考 `roto_shape_node.py` 的动态参数声明——关键帧树已支持该模式）；
2. GPU：控制点格点上传小纹理，fragment shader 做**双线性格点插值 UV warp**（先算像素所在格cell，四角偏移双线性混合得 UV 位移，采样输入）； bicubic 为阶段 2；
3. tile padding：声明 = 格点最大位移的范数上界（遍历格点 max |offset|，evaluate 时动态计算并上报 padding 接口）；
4. 查看器交互：复用 roto 控制柄基础设施（`composite_viewport.py:2297` 的 handle dict 模式）拖格点，路径进 `viewer_tool_system.py`（新工具或复用 MASK 工具形态）；
5. 插值动画（点可关键帧）+ 序列化 + 缓存（点偏移进参数 revision）+ 注册 + i18n + 帮助。

#### 验证
- 自动化：恒等格点（无偏移）直通逐像素一致；单点位移的局部影响断言（仅相邻 cell 变化）；tile 分块与整帧一致（padding 正确）；点动画插值；序列化往返。
- 手工：4×4 网格拖拽做面部微调/旗帜变形；预览=导出。

#### 验收
恒等直通、局部性、tile 安全、交互可用。

---

## 6. Batch E — 剪辑页前置依赖任务（⛔ 硬门禁批，必须等待剪辑页计划）

> **本批铁律**：E 批任务**禁止提前启动**。每个任务开跑前必须完成第 8 节"E 批启动前检查清单"的逐项确认（剪辑页计划状态核对 + rebase + 冲突热点检查），不满足即跳过继续其他批次任务。本批之前的任务与剪辑页计划**无文件交集**，可放心并行。

### E1 comp clip 渲染缓存 GPU 化（原 C7，复活 graph_render_service）

#### ⛔ 硬门禁
启动前必须全部满足：
1. 剪辑页计划 **B3（Render Cache smart 自动评估器）已完成并合入**（该任务会修改 `edit_controller.py:7227` 附近的 in-flight 守卫与 `render_cache_worker.py` 的使用方式——与本任务改同一片文件）；
2. 已 rebase 最新剪辑页分支，`src/features/edit/core/render_cache_worker.py` 与 `src/app/controllers/edit_controller.py` 无未合并/进行中变更；
3. 与剪辑页执行者（或人工）确认无仍在飞的缓存相关任务。

#### 背景
两处沉睡：(1) `render/composite_graph_render_service.py` 标注 DORMANT，GPU 路径恒返回 None；(2) 剪辑页 `edit/core/render_cache_worker.py` 的 `_CpuCompClipRenderer` **仅支持 5 种 CPU 节点**（timeline_clip_input/video_frame/topos_color/output/constant），GPU-only 合成图做不了缓存直接失败。

#### 实现步骤
1. **rebase 后重新定位锚点**：剪辑页 B3 可能已改变 render_cache_worker 的调用面（smart 自动入队、队列插入策略）——先 Explore 其最终形态再动手；
2. **复活 service 的 GPU 路径**：offscreen 共享上下文逐帧渲染（模式照抄 `composite_frame_renderer.py` worker 线程自建 context 的既有做法），接口保持 CPU/GPU 统一（GPU 优先，失败回退 CPU 子集）；
3. **render_cache_worker 接 service**：替换 `_CpuCompClipRenderer` 直连实现，改为调 `composite_graph_render_service`（经 composite 的 facade/export 边界——遵守模块边界，edit→composite 只能走 facade）；
4. **与 B3 smart 评估器联动验证**：smart 模式自动入队的缓存任务同样走 GPU service（E1 的核心增量验收点，B3 落地前无法验证，这正是门禁存在的原因）；
5. GPU 不可用（无 GPU/上下文失败）时回退现 CPU 子集并在日志注明原因；
6. 移除 DORMANT 注释；`git grep DORMANT` 确认 composite 域只剩该留的。

#### 验证
- 自动化：GPU-only 图（blur+merge）经 service 渲染与预览路径输出一致（PSNR ≥ 45dB 容差，允许实现路径差异）；CPU 回退触发条件测试；剪辑页 comp clip 缓存生成成功（原失败用例转绿）；**smart 模式（B3 产物）自动触发 + GPU service 渲染的组合用例**。
- 手工：GPU 效果合成图嵌时间线 → smart 自动缓存（B3 开启时）或手动缓存 → 时间线播放走缓存（性能目测）；缓存失效重算正常。

#### 验收
GPU-only 合成图可缓存、输出与预览一致、失败安全回退、与 B3 smart 评估器联动正常、DORMANT 清除。

---

### E2 OFX 插件宿主接入合成页（原 D3 预研项提升）

#### ⛔ 硬门禁
启动前必须全部满足：
1. **剪辑页计划全部任务完成**（A/B/C 批收尾，D 批至少完成决策产出）——OFX 是两页共用的插件平台，剪辑页侧先行（其效果面板体量小、验证快），本任务在共享宿主层之上做合成页接入，避免两个执行者同时从零建宿主；
2. 检查剪辑页 D3 的架构评审结论（见 `docs/edit_optimization_plan_2026-09-07.md` 第 6 节）：
   - 若剪辑页侧**已产出宿主层**（如 `src/shared/ofx/` 或等价物）→ 跳到步骤 5 直接复用接入；
   - 若仅有评审结论/未实施 → 按步骤 2-4 从零实施共享宿主层（结论作为架构输入）；
   - 若评审**否决** OFX 路线 → 本任务转为执行结论中的替代方案（如 DCTL 扩展强化），并更新本节记录。

#### 背景
合成页与剪辑页均无 OFX 宿主（全 src 树 grep "ofx/OFX" 零命中），第三方效果生态（Sapphire、RED 巨柱等工业插件）进不来。合成页节点体系是自研 `NODE_REGISTRY` + Python 插件（`node_plugin.py`），无 C ABI 插件通道。

#### 架构决策（已定，勿改）
宿主核心建在 **`src/shared/ofx/`**（C++ CPython 扩展 + Python 包装），两页共用；本任务只做合成页节点侧接入与验证，**不修改 `src/features/edit/**`**（剪辑页效果面板的后续接入由其后的独立任务/人工安排，本任务只在 shared 层留好接口与文档）。

#### 实现步骤
1. **前置检查**（对应硬门禁 2 的三分支，产出一段决策记录写进本文件变更记录）。
2. **C++ 绑定层** `src/shared/ofx/native/`：
   - 集成 OpenFX SDK 开源头文件（include/ 许可宽松）；
   - CPython 扩展（构建模式照抄 `src/native/decklink_bridge/topos_decklink.cpp` + `tools/build_topos_decklink.py`，跨平台打包进 packaging 流程）；
   - 加载 `.ofx.bundle`（macOS bundle / Windows DLL 的 `OfxGetPlugin` 入口）、实例化插件、枚举插件描述；
   - 实现最小 Host Suite 集：PropertySuite + ParameterSuite + ImageEffectSuite；MVP 只支持 **kOfxBitDepthFloat CPU 渲染 action**（GPU/交互/进度 suite 列阶段 2，声明不支持即可，插件会回退）。
3. **数据桥与参数映射**：
   - `OfxImage` ↔ numpy float32 RGBA：合成侧以 scene-linear float 进出（OFX float 插件按线性语义调用；非线性插件标注色彩空间警告）；
   - OFX 参数类型 ↔ `ParamDef`：double/int/bool/choice/string + group/page 结构映射为参数分组；MVP 静态参数（OFX param 动画由宿主关键帧系统驱动的完整对接列阶段 2，先支持手动打关键帧）。
4. **节点包装**：
   - 扫描插件目录（偏好配置 + 系统标准路径），每个插件经 `register_node_type()`（`core/node_plugin.py` 体系已有动态注册）注册为 Filter 类节点，`SEARCH_ALIASES` 取插件 label；
   - 求值：渲染线程同步调用 render action（整帧调用；tile/region 适配阶段 2）；
   - **稳定性隔离**：插件调用全程 try/except + 崩溃插件黑名单（偏好页可管理），单个坏插件禁止拖垮宿主；连续崩溃自动禁用并日志。
5. **（复用分支）剪辑页已有宿主时**：只做参数映射适配 + 合成节点包装 + 本页验证。
6. **序列化**：OFX 节点参数随图 schema 存（插件标识 = bundle 路径 + plugin identifier + 版本）；工程在插件缺失时节点显示离线态（不崩，参考素材 offline 模式）。
7. 偏好页：插件搜索路径、启用/禁用、黑名单管理；i18n + 帮助文档（含 macOS/Windows 插件安装路径说明）。

#### 验证
- 自动化：`tests/composite/test_ofx_host.py`——
  1. 用 OpenFX 官方 sample 插件（编译 1~2 个进 `tests/fixtures/`，构建脚本进 `tools/`）：加载→参数面板生成→固定输入求值输出与 Golden image 容差对比；
  2. 崩溃插件隔离（fixture 造一个 render action 抛 C++ 异常/段错误的假 bundle）→ 黑名单生效、宿主存活；
  3. 序列化往返 + 插件缺失离线态；
  4. 缓存：参数 revision 触发失效。
- 手工：安装 2~3 个真实第三方 OFX 滤镜（macOS，当前开发平台）→ 合成图可用 → 导出正确；Windows 插件路径差异记录 TODO。
- 回归：无插件安装时启动零影响（宿主懒加载）。

#### 验收
≥3 个真实第三方插件稳定可用；崩溃隔离与黑名单生效；序列化/离线态完整；无插件时零影响；shared 层接口文档就绪（供剪辑页后续接入）。

#### 风险与回退
- OFX suite 面大（交互/自定义 UI/GPU context）——MVP 严格限定 CPU render action，其余 suite 声明不支持；
- 闭源插件兼容性长尾——黑名单 + 诊断日志兜底；
- C++ 扩展跨平台构建（macOS universal2 / Windows MSVC）——沿用 decklink_bridge 已验证的构建管线。

---

## 7. Batch D — 预研批（不在本计划展开，需人工决策后单独立项）

| 编号 | 项目 | 暂缓原因 / 建议路径 |
|------|------|-------------------|
| D1 | 3D 摄像机反求（camera solve） | 特征匹配+束调整为独立算法域；可调研 OpenMVG/OpenCV SfM 或集成现成求解器，先出技术验证再立项 |
| D2 | 稠密光流（OFlow） | **由剪辑页计划 D1 主导**（共享光流引擎：DIS/OpenCV 先行，RAFT ONNX 进阶）；剪辑页侧未产出前本计划不启动；产出后本页接入 OFlow 节点 + retime 光流插值（`retime_node.py` 的 `use_optical_flow` 元数据转真实实现）。属并行执行的归属避让，非技术依赖 |
| D3 | 3D 挤出文字 | QPainterPath→三角化（earcut）+ bevel 生成，工作量集中且收益窄，视产品定位 |
| D4 | 3D 粒子 | 依赖 C1 验证无状态仿真模式后的 3D 化（billboard/体积），等 C1 落地再评估 |
| D5 | USD / Alembic 导入 | SDK 集成重（USD 尤甚），FBX 动画可靠性问题先解决（官方计划文档已列） |
| D6 | 3D 投影（projection mapping） | 需 projector 灯纹理投影 + 采样几何体 UV，3D 闭环大件 |
| D7 | 真 deep EXR（多样本 DeepScanline/DeepTile） | 读取+DeepHoldout/DeepToImage 节点族，现有 DeepMerge 是 2.5D 单 Z，升级为独立管线 |
| D8 | Nuke 式 Live Group/宏系统 | B3 的 MVP（进出+表达式 promote）验证价值后再评估完整宏 |

（原 D3 OFX 宿主已提升为 **E2** 正式任务，带硬门禁。）

---

## 8. 建议执行顺序与批次验收门

**推荐顺序**（第一~五批与剪辑页计划**完全并行**，无文件交集）：

```
第一批（小而确定，1~2 天）：B4 → B5 → B6 → B1 → C5
第二批（跟踪与变形工作流）  ：A1 → A2 → A4（三者构成 matchmove 闭环）
第三批（渲染性能与管线）    ：A3 → A6
第四批（视觉大件）          ：A5 → C7 → C3 → C1 → C2
第五批（工作流与色彩）      ：B2 → B3 → C4 → C6
────── ⛔ 以下为硬门禁批，禁止提前 ──────
E1 —— 门禁：剪辑页计划 B3 完成 + 第 8.3 节检查清单通过
E2 —— 门禁：剪辑页计划全部完成 + 第 8.3 节检查清单通过
（E1 先于 E2；两者期间可穿插 D 批决策类工作）
```

依赖关系：A2 依赖 A1；C2 的 mask 联动受益于 B6（不硬依赖）；E1/E2 与剪辑页计划的并行关系见第 0.6 条与第 6 节门禁。

**每任务验收门**：
1. `pytest tests/composite -x -q`（必要时按 0.5 条隔离）+ 相关跨页测试全绿；
2. 启动冒烟 + 打开/保存现有工程与合成图（JSON 三格式至少 JSON 验证）；
3. 本任务"验证方法"手工清单逐条执行；
4. commit 规范沿用现有风格（`feat(composite): ...`），单项单 commit，schema 变更单独说明。

**8.3 E 批启动前检查清单（E1/E2 每个任务开跑前逐项确认，任一不满足即不得开始）**：
1. 剪辑页计划完成状态核对：E1 要求 B3 有完成记录（commit / 计划文档勾选）；E2 要求剪辑页计划 A/B/C 批全部完成且 D 批至少有决策产出；
2. `git fetch` + rebase 最新剪辑页分支，无冲突残留；
3. 冲突热点文件检查：`src/features/edit/core/render_cache_worker.py`、`src/app/controllers/edit_controller.py` 近期无剪辑页侧进行中变更（看分支未合并提交 + 询问执行者/人工）；
4. 在本文件该任务的"硬门禁"小节末尾追加一行执行记录（日期 + 检查结果），留审计痕迹。

**批次里程碑验收**（每批完成后）：用真实素材走一遍"多层 EXR(A6) → 去畸变(A4) → 跟踪(A1) → 稳定/贴logo(A2) → 运动模糊(A5) → 导出(A3 并行)"端到端链路；"GPU 合成图嵌入时间线 + 缓存"链路留待 **E1 门禁通过后**随 E1 验收一并执行。

---

## 9. 关键文件速查索引

| 模块 | 路径 |
|------|------|
| 节点注册表 / 类别 | `src/features/composite/core/node_engine.py:110-306` |
| 节点能力声明 | `src/features/composite/core/node_capabilities.py` |
| 节点实现目录 | `src/features/composite/nodes/`（70+ 文件） |
| 求值核心（单线程循环） | `src/features/composite/evaluators/base_evaluator.py:579` |
| 求值 mixin 组合 | `src/features/composite/evaluators/__init__.py`（13 个 mixin） |
| 渲染计划 / 并行分组 | `src/features/composite/render/render_scheduler.py:380-381、570、667` |
| 交互渲染器（QThread+GL） | `src/features/composite/render/composite_renderer.py` |
| 导出渲染（逐帧串行） | `src/features/composite/render/composite_render_worker.py` / `composite_frame_renderer.py` |
| 缓存管理（内容寻址 key） | `src/features/composite/render/cache_manager.py` |
| GPU 资源池 | `src/features/composite/render/gpu_resource_pool.py` |
| 图快照 / 端口 revision | `src/features/composite/core/graph_snapshot.py` |
| 时间重映射（闭式） | `src/features/composite/core/time_remap.py` |
| EXR 解码（单 part） | `src/features/composite/core/image_loader.py:189` |
| 色彩空间 / IDT | `src/features/composite/core/color_space.py`（IDT 应用：base_evaluator.py:2140） |
| 查看器（10230 行） | `src/features/composite/ui/composite_viewport.py`（ODT: 8188；channel: 884；wipe: 913） |
| 节点编辑器（4899 行） | `src/features/composite/ui/node_editor_widget.py` |
| 图命令 / Group 命令 | `src/features/composite/ui/node_graph_commands.py` / `ui/group_commands.py` |
| 关键帧面板 / 曲线 | `src/features/composite/ui/keyframes_panel.py` / `spline_view.py` |
| 表达式 | `src/features/composite/utils/expression_parser.py` |
| 脚本 API（参数级） | `src/features/scripting/composite_api.py` + `features/composite/edit_service.py` |
| AI 图操作契约（未接线） | `src/features/composite/ai/composite_plan.py` |
| AI 计算管理器 | `src/features/composite/render/ai_compute_manager.py:451`（模型映射） |
| DORMANT 统一渲染服务 | `src/features/composite/render/composite_graph_render_service.py` |
| Edit 页 comp clip 缓存 | `src/features/edit/core/render_cache_worker.py`（CPU 子集限制） |
| 跟踪器（color 页，待迁 shared） | `src/features/color/tracking/`（tracker_2d / corner_pin / tracking_store…） |
| 模块边界规则 | `docs/module_boundary_rules.md` |
| 质量档位 / 代理 | `src/features/composite/core/quality_profile.py`（挂接：composite_page.py:1364-1670） |
| 3D 子系统 | `src/features/composite/render3d/`（scene_graph / material_model / aov / cryptomatte…） |

---

## 10. 变更记录

| 日期 | 变更 |
|------|------|
| 2026-09-07 | 初版：基于 4 路并行调研生成 A/B/C 三批 20 项 + D 批预研 9 项；明确 A1 跟踪桥接的 shared 迁移架构决策与 A3 并行渲染的范围决策（帧级优先）；与 `edit_optimization_plan_2026-09-07.md` 互引（光流/OFX 合并立项） |
| 2026-09-07（二） | **并行执行改造**：新增 Batch E 硬门禁批——原 C7（comp clip 缓存 GPU 化）移至 E1（门禁：剪辑页 B3 完成，共享 `render_cache_worker.py`/`edit_controller.py` 文件域）、OFX 宿主从 D3 预研提升为 E2 正式任务（门禁：剪辑页计划全部完成）；新增第 0.6 条并行协调规则（领地划分/冲突热点/Git 纪律）与 8.3 节 E 批启动前检查清单；D2 稠密光流明确归属剪辑页侧主导；原 C8 重编号为 C7，D 批重编号为 D1-D8。剪辑页计划文档未改动（执行中） |
| 2026-09-08 | **执行记录（topos_1 克隆，分支 feat/composite-plan）**：第一批 B4/B5/B6/B1/C5 + 第二批 A1/A2/A4/A3/A6/A5 共 11 项实施并逐项提交（B4 98796a1f、B5 2fc310a1、B6 76d9aabf、B1 19be227a、C5 56067cee、A1.1 402c6464、A1.2+A1.3 61ecc20b、A2 7794f9ae、A4 4ab52942、A3 f9ff5aaa、A6 07cb4148、A5 8a8143a7）。要点与诚实边界：①B6 的计划前提过时——ai_inpaint/ai_expand 实为占位符且 request_compute 零调用点，本次补全了求值半环（异步 cache-first + AI_RESULT_READY 闭环），ai_inpaint/ai_expand 保持占位待各自任务解锁；②A1.1 纯算法迁移 shared 落地，multi-part EXR 多部分**写入**受本机 OpenEXR 3.x 绑定能力限制，多部分路径以能力探测门控；③A5 非均匀缩放求逆的剪切残差为族的理论边界（位置分量精确）；④全量 composite 测试按文件进程隔离跑（206/206 文件通过），同进程长跑的 teardown 段错误为既有环境问题（干净基线复现）；⑤测试环境发现 topos 原生编解码绑定符号缺失（tc_dev_symbol_hist_plane_get），影响 tests/color 少数用例，与本计划改动无关（迁移前 worktree 复现）。 |
| 2026-09-08（二） | **E 批启动前检查清单执行记录（8.3 节）**：E1——条件1 满足（剪辑页 B3 commit 4a251154 完成并记录）；条件2 不满足（topos_1 的 chore/edit 未含剪辑页最新提交，未执行 rebase）；条件3 不满足（无法与本环境外的剪辑页执行者确认无在飞缓存任务）。E2——条件1 部分满足（A/B/C 完成，D 批按剪辑页计划记录"暂缓"），条件2/3 同上不满足。**按第 6 节铁律"任一不满足即不得开始"，E1/E2 均未启动，留待条件齐备后执行。** C1/C2/C3/C7/B2/B3/C4/C6 及 D 批预研未在本轮执行，保持待领取。 |
| 2026-09-08（三） | **全分支隐藏 bug 排查与修复（11 项已落地代码的复查）**：5 路分区只读审计（B1 ROI 链路 / A3+A6 并行导出与 EXR / B4+B5+C5+迁移 / A1+A2+B6 跟踪与 AI / 全局约束扫描），修复 7 个 Critical——①B1：`BBox.clamped(fmt)` 签名误用使 ROI 重合成必抛 TypeError；②B1：ROI 变化未纳入 dirty 判定，dirty-skip 播放旧 ROI/全帧陈旧输出（scheduler 增加 `_prev_roi_key` 全量失效）；③A3：collector 期望计数从 0 起，start_frame>0 的导出全部渲染后丢弃（改为首帧号起步+回归测试）；④B4：重命名 push 后从未 apply（首次改名无效、undo 假动作）——apply-then-push，Color 导入同修；⑤B6：mask 以 HxWx3 流转导致缓存合成广播错误与 ONNX 4ch concat 崩溃（读回后归一单通道+尺寸防御）；⑥A4：lens_distortion 未接 TilePaddingComputer（导出分块錯边）且 padding 公式缺分辨率因子（改为 k/r 界×帧高，`compute_input_roi` 同步扩边）；⑦A4：mirror 边缘公式 `abs(mod(uv,2)-1)` 会翻转整幅图（改三角波）。另修 High/Medium：AI 磁盘缓存 key 纳入 tiling/blend/model 参数、AI worker 并发上限 2+preview LRU+stale 取消、Tracker cancel 后永久卡"analyzing"、QThread wait/deleteLater、CornerPin bake 透视除零帧的部分角点回滚、并行导出 sink 异常泄漏 encoder/GL 资源（worker 线程自毁+工厂预热回退生效+取消语义对齐串行+残留文件清理）、retime/版本面板刷新、`_VERSION_RE` 词边界（av1/rev2 不再误判）、EXR part/extra 通道接入异步解码与重载判定、transform/cornerpin 参数域放宽（±3600°/100x/±8）避免 bake 钳制、tracker 面板 i18n、ai_remove 中文搜索别名、增量序列化 delta 补 user_label/keyframe_tracks、CPU/GPU 畸变归一化统一按帧高。新增回归测试 17 条（非零起始帧/teardown、mask 单通道、镜像公式、padding 高度缩放、词边界、重命名即时生效）。排查结论：13 个失败文件+1 段错误全部为基线遗留环境问题（原生编解码 dlsym 符号缺失、Qt teardown 段错误，在 chore/edit 干净 worktree 复现），非本分支引入。已知留档：proxy（force_lod）下 ROI 预览禁用（坐标系不一致，宁缺毋错）、AOV pass 元数据在 ROI 下仍为 ROI 尺寸、增量保存的其余既有缺口。 |
| 2026-09-08（四） | **B2 查看器补齐完成（四个子项，分别提交）**：①B2-1 代理档位菜单 23f15e92——View 菜单 Resolution 组（Full/Half/Quarter/Auto），与页面设置菜单同源；Full/Half/Quarter 接既有 proxy_scale→force_lod 路径，Auto 交互（滚轮/拖拽）时临时降半档、600ms 静止后恢复，导出不受影响；档位随 viewer_state 持久化（resolution_mode），加载时经信号恰好应用一次。②B2-2 A/B 快照双槽 a9bde5c3——Ctrl+Shift+A/B（或菜单）把当前帧冻结进 A/B 两个 viewer 私有 GPU 纹理槽并自动开启 wipe；Compare Source 单选决定 wipe/difference 的对比源；顶栏 split 开关按活动槽位重冻结；顺带修复 set_split_view 同态重入导致的冗余二次 GPU blit。快照为会话级（GPU 纹理不入档），对比模式设置沿用既有持久化。③B2-3 放大镜 5146f090——Z 键或 View > Magnifier 开启 160px 圆形 loupe，4x/8x 最近邻放大，采样合成后 framebuffer（所见即所得，含 ODT/通道隔离/A-B wipe），光标跟随、贴边翻转、十字线+倍率标签，状态持久化（magnifier_enabled/zoom）。④B2-4 背景三态 f909dfb0——View > Background 三选（棋盘格/黑场/白场），黑/白以平场替代棋盘格（遮罩/边缘检查用），alpha/mask 预览逻辑不变，quad/3D 路径仍走全局 3D 场景背景色，持久化（background_mode）。新增测试 4 个文件 31 条（档位映射与 Auto 降档、双槽簿记与空槽守卫、loupe 状态与采样跨度、背景映射与 paintGL 源断言）；相关回归全绿（viewer/page 相关 117+178 条）。剩余待领取：B3、C1-C4、C6、C7（E1/E2 仍受硬门禁、D 批待人工决策）。 |
| 2026-09-08（五） | **C6 OCIO 贯通实施（部分完成，诚实边界见下）**：①ACEScg 数学基座——BuiltinColorProvider 增 BT.709↔AP1 已发布矩阵（Bradford D65↔D60）、bundled ocio/minimal.ocio 增 `acescg` 空间（alias aces_cg/ap1_linear，to_reference 矩阵与 provider 逐位一致：max diff 0.0、roundtrip 6e-8）、GPU cs_transform 增 primaries id 3=AP1、descriptor 增 ACESCG_SCENE 预设与 ocio 名往返、ColorPrimaries 增内部 AP1 成员；**修复真缺口**：`aces_cg/acescg` 源标签此前直通进图（AP1 未转换导致色相偏移），现在 IDT 做 gamut-only 转换；merge `_auto_align_color_spaces` 覆盖 aces_cg。aces2065_1（AP0）维持直通（无已验证 AP0 矩阵，如实标注）。②viewer ODT 走 provider——display descriptor 激活且引擎为 OCIO 时，把 processor 烘焙为缓存的 32³ RGB32F 3D LUT（TU2），display shader 的 gamut+transfer 编码段换成单次 texel-centre 三线性查找，预览与 OCIO 导出同引擎；曝光/tonemap/viewer gamma 保持 viewer 本地。回退护栏：builtin 引擎走 GLSL（与 OCIO 数值一致有 parity/golden 测试证明）、tonemap-off + HDR 显示传递保持内置（nits 超 LUT 域）、任何烘焙/上传失败按 descriptor 记忆降级且不阻塞绘制、cleanup 释放。fragment shader 经 standalone GL 3.3 实际编译验证。③偏好页"仅导出走 OCIO"过时文案更新。④修一个环境依赖的潜伏测试 bug（factory 默认引擎在本机有 OCIO 时合法返回 ocio）。**未做（留 stage-2）**：全图 AP1 工作空间切换（IDT 目标随 working_space 变 AP1、composite.working_space 键接线——现为预留空壳）；sRGB/bt709/pq/hlg 的 IDT 保持内置 GLSL 路径（与 OCIO 数值一致由三方 golden 测试证明，未做逐变换 LUT 缓存）；E 批门禁与 D 批预研不受影响。 |
| 2026-09-08（六） | **C3 Text+ 增强（逐字动画 + 弧线路径）完成**：①逐字动画——per_char_enable 组（mode: fade/slide_up/slide_left/scale_pop/rotate_in/typewriter、delay、overlap、slide_distance/rotation/scale_from），光栅化拆为逐字形 QPainterPath（`_build_char_placements`：QTransform 在字形局部帧合成 slide/scale/rotate，ease-out cubic，fill 与 outline 共用同一 placements 几何）；birth_i=i×delay、duration=delay×(1+overlap)（overlap 0=串行 1=半重叠），总时长自动=(n-1)×delay+duration；空白字符不占 stagger 槽；进度帧=ctx.frame_number−节点 time_range 起点（标题从入点开始播）。②弧线路径（MVP (a) 内置弧线）——path_enable 组（radius、arc_degrees 0=自动曲率/正值为固定张角均布、direction up=拱形/down=碗形），字形沿圆弧摆放并旋转到切线，与逐字动画可组合（动画变换作用在弧线局部帧上）。③缓存与动画——所有新参数 float/int/bool/option 天然可打关键帧；动画开时节点的 `_content_hash` 纳入当前帧（节点内纹理逐帧重光栅），图级缓存经新增 `Node.params_require_frame_cache` 钩子（TextNode 覆写）接入 `_node_needs_frame_cache`，无需关键帧即按帧隔离（`:fn{frame}`）。④bbox 测量与 gizmo：逐字/弧线模式下 union 全部字符变换后范围（不看 alpha，纹理尺寸逐帧稳定、不裁切迟出生字形）。⑤i18n 与别名：zh_CN/en_US 补齐 `nodes.text` 全套词条（title/description/params/option labels——此前 text 节点完全无词条，面板一直裸键名）；SEARCH_ALIASES 增"逐字/逐字动画/弧形/弧形文字"。⑥序列化零改动（基类模板），旧图缺新键回落默认关闭。测试新增 24 条（fade 数值表 0/0.875/1、typewriter 二值与出生帧、scale/rotate 衰减、空格 stagger、弧线角度单调/镜像/±45° 张角、组合模式、typewriter 帧积分、PIL 降级、哈希帧敏感开关、图级帧缓存判定、序列化往返与旧图兼容）；回归全绿（节点 977、缓存/关键帧 133、shared/color 等 94、undo/evaluator 128）。诚实边界：逐字模式无 pair kerning（逐字形摆放的固有代价，AE per-char 同）；多行+弧线为按行独立弯曲；RTL/复杂文字脚本不支持逐字；PIL headless 回退不实现逐字/弧线（降级为静态整段渲染，不崩）；路径源 (b)（链接 roto_shape 取点）未做，按计划留待后续按需立项。 |
| 2026-09-08（完结） | **计划完结：A/B/C 三批 19 项全部实施完毕**（分支 feat/composite-plan）。本轮（2026-09-08 下半场）交付：C3 逐字动画/弧线（0d8a017f）→ C4 脚本图操作（adb92028）→ C2 Paint 两批（95e524be/d9f2b2a6）→ C1 粒子（deddc4e0）→ B3 Group 进出+Publish（cd992067/bab43fed）→ C7 Mesh Warp（2b1b6787）。此前已完成：A1-A6、B1/B2/B4/B5/B6、C5、C6 及全分支隐藏 bug 排查 7 项 Critical 修复。**仍未启动（受硬门禁/人工决策约束，非代码缺口）**：E1/E2（剪辑页门禁未满足）、D 批预研（待人工立项）。遗留低优先项已如实记录在各任务诚实边界中（C3 路径源(b)/C7 bicubic 等阶段 2 增强、Group 虚拟锚点等）。 |
| 2026-09-08（十一） | **C7 网格变形节点完成（计划 A/B/C 三批 19 项至此全部落地）**：①新 nodes/warp_node.py（node_type=mesh_warp，Filter 类别，别名 网格变形/变形/网格扭曲）：grid_size 4-16（每轴分段，格点 (g+1)²）、offsets JSON 格点位移场镜像为 warp_point_{i}_dx/dy 动画通道（roto 动态参数模式：JSON/数值双向同步、反序列化 no-op 不重编码、get_param_defs 动态生成、关键帧树直接可编辑）；②GPU——core/shaders 增 _MESH_WARP_FRAGMENT：格点位移上传 RG32F 小纹理，fragment shader 双线性格点插值位移场做 UV warp（texelFetch 四角 + mix，image 空间 y-down 像素模型与 lens_distortion 一致，edge black/clamp/mirror 三态），_apply_mesh_warp 经 _dispatch_effect mesh_warp 分支接入，恒等格点短路直通；③tile padding——_tile_padding=格点最大位移范数×帧宽（恒等=0 完全不扩），compute_input_roi/compute_output_bbox/requires_roi_expansion 全套（lens_distortion 模式，TilePaddingComputer 的 _tile_padding 动态检查接口）；④查看器交互——mesh_warp 节点选中即 always-on 网格柄 overlay（_compute_warp_handles_dict：柄=格点基位置+位移），点击命中即拖拽（复用 roto press/hover/光标链路），page 侧 warp_point_ 解析从 live 节点读格点场、拖拽目标位置减基位置即新位移写 dx/dy 通道+JSON（可打关键帧）。⑤注册+zh/en i18n。测试新增 13 条：恒等直通逐像素一致（真实 standalone GL）、单点位移局部性（远角/顶行不变、中心变化）、padding 恒等=0/随位移线性/ROI 扩展、动态通道双向同步、插值 override、grid 变更重建、序列化往返+旧图缺键；广域回归 1180 passed / 6 skipped。诚实边界：bicubic 插值、streak、sprite、GPU instancing 等增强留阶段 2；viewer 网格线不做（只画格点柄，加格线需 GL overlay 扩展）。 |
| 2026-09-08（十） | **B3 Group 进出编辑接线 + 参数 Publish 完成**：①进出接线——双击组内成员节点或右键 Enter Group 触发 group_enter_requested，page 应用 EnterGroupCommand 并以单条 UndoAction 入 undo 栈（undo/redo 分别还原/进入，视图经 editor.enter_group_view/exit_group_view 同步）；画布新增真过滤机制 _apply_group_filter（非成员节点 setVisible(False)、跨组连线隐藏——非透明度聚焦），退出经面包屑/返回按钮/Esc（Esc 在 group 视图时优先退出组而非 node-preview）；工具栏新增面包屑 Root/组名 与返回按钮（group_view_changed 驱动，支持多层栈）。②参数 Publish——GroupNode 增 promoted_params 注册表（随节点序列化，schema 兼容旧图缺键）+ promote_param/unpromote_param：为成员 float/int 参数生成组上同名数值代理 ParamDef（get_param_defs 动态生成，roto 模式），经引擎 set_parameter_binding 生成成员表达式 n_<gid>.<proxy> 驱动成员；求值联动经 ExpressionManager 别名机制验证（组代理改 33 → 成员解析 33）；属性面板参数右键新增 提升到组（set_group_promote_hook 注入，按需物化 published_<gid> GroupNode 容器），promote 走 record_external_composite_edit 整图快照 undo；keyframes pick-whip 源枚举从类属性 PARAM_DEFS 改为 get_param_defs()（动态代理可作绑定源）。测试新增 13 条（进出往返图不变、未知组拒绝、undo 栈往返、promote 代理+绑定注册、表达式联动求值、解除提升、非数值拒绝、非成员拒绝、序列化往返+旧图兼容、画布过滤与恢复、双击分流）；group/绑定/编辑服务/undo/参数菜单回归 151 条全绿。诚实边界：组边界虚拟锚点节点（组内连接外部）按计划 MVP 退化为组内编辑仅限内部连线，未做；promote 的容器 GroupNode 为按需物化（Ctrl+G 建的 UI 组本无实例），与求值扁平化机制兼容。 |
| 2026-09-08（九） | **C1 2D 粒子系统完成（无状态确定性仿真）**：新 `nodes/particle_node.py`（独立 Particle 类别，别名 粒子/发射器/粒子发射，zh/en i18n）。架构按计划要点：闭式出生（birth_i=i/rate，splitmix64 整数哈希确定性随机——lifetime/speed 方差、发射区域内采样），求值任意帧独立重放（半隐式欧拉 dt=1 单位时间网格向量化积分，进入 f 状态=时刻 f、步进到 f+1、出生帧重置闭式初态——与逐帧推进同一递推，无携带状态）；重力/风/湍流（确定性 value noise `_noise01`，turb=0 时零开销）；外观 size/opacity 出生→死亡 ramp、dot/soft_dot（高斯）形状、黑底 premultiplied RGBA 输出（Merge over 直用）；自管 GPU 纹理（text 同款 owns_color_texture=False 模式），content hash 含当前帧，`params_require_frame_cache=True`（粒子场是纯时间函数，无关键帧也按帧隔离缓存）。发射 region point/rect/ellipse、rate/seed/lifetime±方差/speed±方差/direction/spread 与全部力/外观参数均为可动画 float/int/option。测试 12 条：同帧逐位一致、**跳帧一致性对照独立有状态积分器**（向量化分组重放 vs 逐粒子 python 积分 1e-9 全等——计划核心验收）、重力弹道闭式解精确匹配（p_n=p0+n·v0+g·n(n+1)/2）、零重力直线、死帧全透明、premult 黑底、序列化往返+往返后仿真一致、standalone GL 求值与帧隔离 revision 冒烟；paint/粒子/能力/契约/缓存键回归 143 条全绿。诚实边界：动画力场参数按出生帧重放逐子帧采样（计划标注的阶段 2 项）未做——动画参数当前按整帧插值重放（keyframe 轨道经 evaluator 插值后仍确定性）；streak 拉伸/sprite 贴图/GPU instancing 留阶段 2（计划记录不验收）。 |
| 2026-09-08（八） | **C2 Paint 节点完成（克隆/擦除画笔，分两批提交）**：①节点（95e524be）——新 `nodes/paint_node.py`（node_type=paint，独立 Paint 类别）：strokes JSON 笔划列表（clone/erase/solid、归一化折线、size/feather/opacity、clone src_offset、solid HDR 颜色、保留 frame_range 字段）、容错解析（坏数据丢弃不抛）、make_stroke/set_strokes 助手、image 输入 + result/mask 双输出口、别名 绘画/克隆/仿制图章/擦除、zh/en i18n；求值在 `filter_evaluator._apply_paint`（dispatch 增 paint 分支）：QPainter 圆头笔（单点成圆点）+ cv2 羽化、按笔划 bbox 局部光栅、premultiplied-linear 域合成（clone: src=dst−offset 从输入图采样；erase: alpha 缩放；solid: premult over），mask 口输出笔划并集 matte 写进 RGB+alpha（ai_remove/keyer 只读 RGB 即白=画过区域）。②查看器 PAINT 工具——`ViewerToolMode.PAINT`，快捷键 **B**（计划建议 P 已被 CORNERPIN 占用，按惯例改 B）：拖动画笔划（≥2px 抽稀）、Alt+点击设克隆源点（后续笔划采样=落点−源点偏移）、`[`/`]` 调笔刷半径（镜像到节点 brush_size 参数）、QPainter overlay（笔刷圆圈随缩放、进行中折线、克隆源十字）；节点增 brush_type/brush_size/brush_feather/brush_opacity/brush_color_rgba 笔刷默认参数（面板可调）。③提交与撤销——专用信号 `paint_stroke_committed`（不经 transform 交互链），page 侧从笔刷默认值组装完整笔划追加进 strokes JSON，**一笔恰好一条 UndoAction**（undo/redo 分别携带旧/新 JSON），[/] 调笔刷不进 undo（工具偏好非内容编辑）；mid-drag 切换节点时丢弃笔划防误提交。测试新增 20 条（几何提交、克隆源偏移、mid-drag 防护、[/] 信号、page 组装/单笔 undo 往返/brush_size 写回、注册/解析/往返/真实 standalone GL 像素级 clone/erase/mask/solid/feather），viewer+节点回归 157 条全绿。诚实边界：MVP 笔划为静态（frame_range 已序列化但求值未按帧过滤，动画笔划留后续）；拖动中不做实时像素预览（释放提交后由求值管线呈现，预览=导出天然一致）；mask 口不走参数开关（常驻第二输出口）。 |
| 2026-09-08（七） | **C4 脚本图操作接线完成**：edit_service 新增 `NODE_OPS`（add_node/remove_node/connect/disconnect/set_param，op 名与 ai/composite_plan.py 的 SUPPORTED_NODE_OPS 对齐——脚本、MCP、AI 同一协议），validate 与 apply 双侧实现：①结构化校验——未知 node_type（附可用列表）、node_id 冲突/非法、端口不存在、端口类型不匹配、成环（复用引擎 `_would_create_cycle`）、自连接、disconnect 要求连接存在、set_param 复用参数域/类型校验；②plan 内序列模拟（topo_state）：同一 plan 里 add 后可 connect/set_param/混合 KEYFRAME_OPS 引用新节点（临时实例 peek 校验端口语义），plan 内 connect 可被后续 disconnect，remove 后引用报"already removed"；③apply 走 graph 原生 add_node/connect/disconnect/remove_node（环检测/类型检查/单输入替换语义全部继承引擎），add_node 支持自动 id（report 附 info 备注"created node <id>"）、position、内联 parameters；④失败整体回滚沿用 `_restore_graph_in_place`，单 undo 组沿用宿主 `record_external_composite_edit`（一个 plan 恰好一次记录，测试覆盖）；⑤示例脚本 2 个（composite_build_graph.py 建图、composite_batch_params.py 批量改参）。测试新增 tests/composite/test_script_graph_ops.py 20 条（建图拓扑/参数、混合 plan、10 类非法操作图不变、单 undo 组断言、AI 契约对齐 + validate_composite_plan 质量门跑通），ai/test_cg_cp_ix.py 22 条全过（协议未变，AI/MCP 侧自动受益）。回归：edit_service/typed keyframe/AI 质量门 56 条全绿。诚实边界：环检测在 validate 阶段仅对图内节点生效（plan 内新建节点组合成的环由 apply 阶段 graph.connect 兜底并整体回滚）；connect 对已满载单输入端口的替换语义与引擎/UI 一致（脚本重连会覆盖旧连线）；group/ungroup 两类 op 在 SUPPORTED_NODE_OPS 中但不在本次范围（NODE_OPS 未含，UI 已有专用命令）。 |
| 2026-09-09 | **计划全面复查（7 路并行深度审查 + 修复）**：对 19 项已交付实现做逐项隐藏 bug 审查，发现并修复 **31 类缺陷**（4 个"功能整体从未生效"级 + 多个数据错误/一致性/健壮性），分 4 个提交落地（a92bcabc / fe17f4b3 / cb504d9d / 9c8480c2）。功能整体失效级：①A5 运动模糊——motion_blur 四参数未进 _effect_params，求值闸门恒 False，UI 开关一直是无操作（子帧插值键名、累积器画布两处连锁缺陷一并修复）；②C1 粒子——帧锁存在 ctx 解包之后调用，生产路径永远第 0 帧（测试用假 ctx 恰好掩盖）；③B6 AI 异步闭环——AIComputeManager 生存在无事件循环的渲染线程，worker 完成信号永不投递（移主线程创建并注入渲染器，node_id 回填打通 UI 反馈）；④C5 批量升版——只记 undo 从不应用，点击无效。数据错误级：A4 undistort/distort 两模式分支互换（与 cv2 约定相反）；C6 ODT 3D LUT 烘焙红蓝轴转置（预览颜色错）；A3 VRAM 回退路径复用已关闭编码器必失败、迟到取消删除完整产物；A6 fused 链 shuffle 字符串参数灌 int uniform 整链黑屏、EXR 3.x 绑定 exr_part 被静默忽略（补 _decode_exr3_part）、探测未用 header_only 解码全部像素、part 切换陈旧交付竞态；C7 tile_padding 漏 mesh_warp 分支（分块导出接缝）、dy 归一基准错（非方形帧错位）、offsets 纹理泄漏、旧工程 offsets-only 数据被全零通道覆盖回恒等；C1 粒子 soft_dot 预乘二次乘 alpha、cap 按累计出生数（长片发射器永久静默）、ramp 用基准寿命；B5 reverse 无 loop 锚定 0 全程定格第 0 帧（增 reverse_range）；B1 ROI 锚定显示纹理致启动恢复 1px 条带；B4 F2 被全局 Rename Layer 抢占；B3 exit redo 重入叠栈/promote undo 残留幽灵容器/画布重建丢组过滤；C4 restore 的 GroupNode 回引用与表达式 manager 两处恢复缺口、int 参数收浮点；C2 paint mask 口空路径缺失/读回失败黑底替换/opacity=0 进 mask；C3 弧线忽略 halign。A1/A2：Analyze 帧范围 ref+100000 无钳制（任何真实素材必失败且全丢）、cleanup 不清 worker、color 导出缺 matrix 致 CornerPin 烘焙必败、WEAK/MANUAL 帧被静默丢弃、rotation ±180 回绕、平滑窗口按索引不按帧距。回归：tests/composite 隔离跑 222/223（唯一失败 cornerpin_freeze 为干净基线同现的既有段错误）；tests/shared 的 5 error（dlsym 原生符号缺失）与 2 failed（DCTL）、tests/color 的 1 failed（panel layout）均经干净树对照证实为既有问题，本次零回归。新增回归测试：镜头畸变方向、粒子 cap 存活窗口、EXR 3.x 多 part、reverse 锚定、ODT LUT 轴向。遗留（本轮未修，均为非缺陷类）：A5 表达式驱动子帧求值与 quality profile samples 减半（计划项、需架构决策）；A6 shuffle extra 通道源/premult/UI 探测按钮（提交时已记录的 follow-up）；A3 序列帧 tmp+rename 原子写与并行写盘（性能/验收交付项）；C3 组合附标逐字丢失（诚实边界已记录）；A1 tracker 源解析穿 dot/双向跟踪（增强项）。E1/E2 硬门禁与 D 批预研确认未被触碰（合规）。 |
