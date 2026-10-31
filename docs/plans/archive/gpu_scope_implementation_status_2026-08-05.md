# Topos Color GPU Scope — 实施状态记录

**日期：** 2026-08-05
**关联计划：** [docs/gpu_scope_implementation_plan_2026-08-05.md](gpu_scope_implementation_plan_2026-08-05.md)
**状态：** GLSL330 基线（阶段 0–6、8、测试）已实施；阶段 7（可选 GL 4.3 compute）按计划留待性能验证后单独评估。

---

## 1. 已交付内容

| 计划阶段 | 状态 | 说明 |
|---|---|---|
| 0 基线 | ✅ | 现有 CPU scope 可见性门控保留为过渡保护。 |
| 1 模型 + 调度接口 | ✅ | `scope_types.py`；`RENDER_GPU_SCOPE` + `GPU_SCOPE` lane；generation guard。 |
| 2 GPU 资源管理 | ✅ | `_ensure_gpu_scope_pool` 按 size 缓存 sample/density/display 纹理，无逐帧分配；cleanup 释放。 |
| 3 Waveform pass | ✅ | `gpu_scope.glsl` accumulate（R/G/B 三次 POINTS + additive blending）+ display（sqrt tone-map + 程序化 graticule）。 |
| 4 Vectorscope pass | ✅ | Rec.709 Cb/Cr 与 CPU reference 完全一致；程序化圆形 graticule / 色彩目标 / 肤色线。 |
| 5 presenter + ScopePanel | ✅ | `GpuScopeWidget`（display-only，tryLock + blit，无 read）；`ScopePanel` demand API + revision guard；`layer_controller` demand 合并器。 |
| 6 播放调度 | ✅ | 播放 10FPS / 拖动 10FPS / 暂停 20FPS / 空闲 150ms 单次 512×288；shader 失败自动降级；renderer-side generation guard 兜底 seek/clip switch。 |
| 7 GL 4.3 compute | ⏭️ | 按计划 §11，仅在 raster 达标且实测收益 ≥25% 时启用；raster 路径永久保留。 |
| 8 CPU 链路清理 | ✅ | `_do_render_scope_snapshot` / `request_scope_snapshot` 标记 deprecated，明确为"暂停 fallback / reference oracle"，不进入播放实时路径（未删除，便于二分定位）。 |
| 测试 | ✅ | `tests/color/test_gpu_scope_pipeline.py` 26 用例覆盖 §9.1 的模型/generation/合并/取消/无回读/资源不变量。 |

## 2. 关键文件

| 文件 | 角色 |
|---|---|
| `src/features/color/core/scope_types.py` | `ScopeDemand` / `ScopeGpuFrame` 不可变模型 + generation guard。 |
| `src/features/color/shaders/gpu_scope.glsl` | 7 个 GLSL330 stage（sample / waveform accum+display / vectorscope accum+display）。 |
| `src/features/color/core/async_renderer.py` | `GPU_SCOPE` lane、`gpu_scope_ready`、资源池、`_do_render_gpu_scope`、`discard_pending_gpu_scope`、source_revision 追踪。 |
| `src/features/color/core/image_engine.py` | `request_gpu_scope` / `gpu_scope_ready` 转发 / `scope_source_revision`。 |
| `src/shared/ui/widgets/gpu_scope_widget.py` | display-only presenter（tryLock + glBlitFramebuffer）。 |
| `src/features/color/ui/scope_panel.py` | demand_state / bump_demand_revision / update_gpu_scope + hide/resize revision。 |
| `src/app/controllers/layer_controller.py` | `_submit_gpu_scope_demand`（节流 + 空闲 HQ）/ `on_gpu_scope_ready`。 |
| `src/app/main_window.py` | `engine.gpu_scope_ready` → `on_gpu_scope_ready` 连接。 |
| `tests/color/test_gpu_scope_pipeline.py` | 单元测试。 |

## 3. 安全约束落实情况（计划 §2、§7）

- ✅ 动态数据路径无 `FBO.read()` / `Texture.read()` / `cv2` / `numpy.histogram*` / `QImage` 逐帧 scope 计算（density 全在 GPU）。
- ✅ Scope 分析 / 密度 / 颜色映射 / 输出纹理均在 `AsyncRenderer` 共享 context / 渲染线程。
- ✅ UI 只表达可见性 / 模式 / 尺寸 / 缩放；不在主线程创建分析任务或读纹理。
- ✅ 隐藏面板（dock 或 ScopePanel）时不提交 demand（`_is_scope_panel_visible` + `hideEvent` discard）。
- ✅ 播放时 GPU scope 走独立、可合并、最低优先级 `GPU_SCOPE` lane，drain 顺序在 video 之后。
- ✅ generation guard：source_revision / demand revision 双重校验，旧结果不显示。
- ✅ GLSL330 基线，不依赖 compute/SSBO/atomic。
- ✅ 降级：shader 编译失败自动 `gpu_scope_enabled=False` + 清 lane；播放中绝不偷偷转 CPU readback（CPU fallback 仅暂停态）。

## 4. 回滚开关

`AsyncRenderer.gpu_scope_enabled`（默认 True）。关闭时停止提交 `RENDER_GPU_SCOPE`，UI 显示最近一帧或空状态；暂停时仍可由用户显式选择 CPU fallback。控制器通过 `getattr(engine.renderer, 'gpu_scope_enabled', False)` 读取，无需改 UI 代码即可远程关闭。

## 5. 待人工验收项（计划 §9.2）

下列需要真实 GPU 设备验证，无法在 CI/headless 完成：

1. macOS / Windows 目标显卡跨 context 共享纹理展示（`GpuScopeWidget` blit 路径）。
2. 1080p / 4K 播放 + scope 可见的帧率门槛（基线 95% / 90%）。
3. Waveform / Vectorscope 与 CPU reference 的视觉/数值对照（彩条、灰阶、肤色）。
4. 快速切片 / seek / 面板开关无旧 scope 闪回。

> V1 的 `GpuScopeWidget` 已采用项目验证过的 `ProgramViewer` blit+fence 模式；如某驱动共享失败，会安静降级为"保留上一帧"，不会崩溃或转 CPU。

## 6. 与计划偏差说明

- **归一化未读 max density**：为 100% 满足"无 `fbo.read()`"硬约束，display pass 使用固定 gain + sqrt tone-map，而非读回峰值归一化。代价是极端密度图可能略偏亮/暗；视觉验收后如需更精确，可在渲染线程做一次极小的 1×1 mip 采样（仍非全图 readback）。
- **graticule 程序化**：vectorscope/waveform 标尺在 display shader 中程序化绘制，未走"预渲染 overlay texture"路径；如视觉设计需要更复杂的标记，再按计划 §4.3 的 overlay texture 方案扩展。
- **CPU scope 未删除**：严格遵循计划 §6"不要把 CPU fallback 删除与首版 GPU 算法放在同一个提交"，仅做 deprecated 标记。

## 7. 代码审查修复记录

实施完成后做了一次聚焦审查（线程安全 / GL 状态 / revision 语义 / presenter 接线），修复了以下问题：

| 级别 | 问题 | 修复 |
|---|---|---|
| Critical | additive blending 未启用 `GL_BLEND`，密度累加失效 | accumulate pass 显式 `enable(BLEND)` + 恢复前值 |
| Critical | 每次 submit 都 bump demand revision，frame 几乎全被丢弃 | revision 改为状态键（仅 mode/size/zoom 变化时 bump），submit 复用当前 revision |
| Critical | `GpuScopeWidget` 从未实例化，数据流断开 | ScopePanel.init_ui 创建 wf+vs 两个 presenter |
| Critical | blit 写入 framebuffer 0 + `WA_PaintOnScreen` 错误 | 捕获 Qt 绑定的 draw FBO 作为 blit 目标；移除 WA_PaintOnScreen；修复 QOpenGLWidget 构造（setFormat）和 import（QtOpenGLWidgets） |
| Important | `gl_PointSize` 需 `PROGRAM_POINT_SIZE` | accumulate pass 显式启用 |
| Important | split 模式只展示 waveform | `_present_gpu_frame` 按 mode 分派到单/双 presenter |
| Important | 跨 context 无同步 | `_do_render_gpu_scope` 末尾 `ctx.finish()` 保证写入对 UI context 可见 |
| Important | read FBO 无完整性检查 | blit 前 `glCheckFramebufferStatus`，失败降级 |
| Minor | scope pass 异常静默吞掉 | 改为按错误去重的 warning，保留 lane 不自动禁用 |

`test_gpu_scope_pipeline.py` 同步新增 6 个 ScopePanel 集成用例（presenter 实例化、revision guard、split 分派、stale 丢弃），共 32 用例。

## 8. 第二轮核查修复（"完美实现"复核）

对计划表逐条复核后发现并修复了以下遗漏：

| 计划要求 | 修复 |
|---|---|
| **§5/§7** presenter 必须真正显示共享纹理（非仅创建后 hide） | ScopePanel 改为双层 `display_stack`：外层 [0]=CPU page、[1]=GPU page；GPU page 内含 `_gpu_stack`（waveform/vectorscope/split 三个 presenter）。收到首个有效 frame 时 `_activate_gpu_display()` 自动提升到 GPU 页——这是计划 §7 要求的运行时跨 context 共享探针（blit 失败则 presenter 静默保留上帧，CPU 页不丢失）。mode 切换同步两个 stack。 |
| **§9.1** Waveform/Vectorscope 正确性测试（bin 容差） | 新增 `TestVectorscopeAlgorithm` / `TestWaveformAlgorithm`：用 NumPy 镜像 GLSL vertex 数学，校验 Cb/Cr 系数与 CPU oracle 逐一相等、原色目标落点、灰阶居中、zoom 仅缩放半径、波形值轴单调与 HDR 余量。 |
| **§9.1** presenter 接入与激活覆盖 | 新增 GPU 页面激活、mode 同步、split 双 presenter 分派的集成测试。 |

`test_gpu_scope_pipeline.py` 现 42 用例（+10 算法/集成）。**复核结论：数据管线 + 展示层 + 算法数学已端到端打通；剩余仅真实 GPU 设备的人工视觉验收（§9.2）无法在 headless 完成。**

## 9. 全模式 GPU 化 + Color/Composite 统一（2026-08-28 追加）

**状态：✅ 已交付。** 全部 8 种示波器（waveform / vectorscope / split / parade / luma / histogram / cie / falsecolor）在 Color 与 Composite 两页均为全链路 GPU 渲染（demand → GLSL → `ScopeGpuFrame` 纹理描述符 → blit），实时路径零 CPU 回读。

### 关键变化

| 文件 | 变化 |
|---|---|
| `src/shared/gpu/gpu_scope_engine.py`（新） | 共享 pass 引擎：pool / sample / 全模式 accum+display。两个渲染线程各持一个实例，执行字节一致的 pass。 |
| `gpu_scope.glsl` | 新增 histogram（点累积 → 列归约 → 曲线显示）、CIE（传输函数/色域矩阵线性化 + xy 密度 + 程序化光谱轨迹/色域三角）、falsecolor（单 pass 伪彩/zebra）；waveform 累积扩展 u_channel=3（luma）；全部 quad pass 自带 passthrough 顶点。 |
| `scope_types.py` | `ScopeMode` 扩到 8 种；`ScopeGpuFrame` 增加 parade/luma/histogram/cie/falsecolor 描述符；demand 增加 transfer/primaries/falsecolor_mode/zebra_threshold。 |
| `scope_panel.py` | 8 个 GPU presenter 与 CPU 栈页索引一一镜像；`_CPU_ONLY_MODES` 清空；demand_state 携带色彩状态；FalseColor 模式/阈值/色彩状态变化 bump revision。 |
| `composite_renderer.py` | 新增 `RENDER_GPU_SCOPE` 命令（submit 合并、latest-wins）+ `_handle_render_gpu_scope`（分析最新输出纹理，emit `gpu_scope_ready`）；删除 CPU readback 示波器链路（`READBACK_SCOPE_ASYNC`/`scope_image_ready`）。 |
| `main_window_adapter.py` | `_maybe_request_scope_readback` → `_maybe_submit_gpu_scope_demand`（15Hz 限流 + 强制档 HQ + request_id 作 freshness marker）；连接 `panel.demand_changed`（暂停态切模式即时重提）；`_on_gpu_scope_ready` 带可见性门控。 |
| `composite_scope_widgets.py` | 解除 CPU 页钉死；`set_available_sources(('graded',))` —— 合成页只有渲染输出这一种源，source/reference 菜单项灰显。 |

### Apple GPU（Apple Silicon）适配要点

实测发现 Apple GPU 对 1px POINTS 上报任意角落 `gl_PointCoord`，任何基于它的 mask/discard 会静默丢弃约 90% 片段。修复：单元写入片段（vectorscope/histogram/CIE 累积）不再读 gl_PointCoord；waveform 累积最小 PointSize 提到 2.0（同时修复 HQ 档 768 采样时迹线偏暗的存量问题）。RGBA16F 加性累加存在 2048 饱和问题，密度纹理按行/按列摊开保持每 texel 计数远低于该值。

### 验证

- `tests/color/test_gpu_scope_pipeline.py`：+全模式分发、presenter 路由、混合状态等用例。
- `tests/composite/test_composite_gpu_scope_handler.py`（新）：真实 GL context 下跑 `_handle_render_gpu_scope` 全 8 模式。
- `tests/benchmark/bench_gpu_scopes.py`（新）：真实视频（tests/test_videos 4K/2K/H.264/ProRes）功能 + 数值 + 性能：
  - 直方图列和 vs numpy：corr=1.0000、total_err≈0.2%；CIE 纯红峰值距理论位置 0.6 bin；parade 三段通道隔离比 >4000:1；falsecolor/zebra 档位精确。
  - 跨 context presenter blit：8 模式全部非空呈现。
  - 播放档（512×288）每模式 2.1–6.6 ms/pass（split 8.5ms）；HQ 档 ≤18.6ms；CPU 参照（仅单通道波形密度）37.2ms。

### 播放期示波器静止修复（2026-08-29 追加）

**症状**：合成页播放视频时 QC 示波器完全静止，暂停瞬间才刷新到当前帧。

**根因**：无关键帧动画的独立 comp clip 播放走渲染线程自驱（`playback_drive`），
`_handle_evaluate_graph` 同步进入 `_playback_drive_loop` 且整个播放期间不返回——
`run()` 的命令循环被独占，UI 以 ~15Hz 提交的 `RENDER_GPU_SCOPE` 全部积压在
`command_queue`（submit() 的 latest-wins 合并保证队列里最多一条，但永远轮不到执行）。
调色页无此问题：其 AsyncRenderer 每帧渲染是独立命令处理，scope pass 可正常穿插。

**修复**（`composite_renderer.py`）：
- 新增 `_consume_pending_scope_demand()`：取出队列中的 scope demand（latest-wins）
  在当前线程、当前 GL 上下文就地执行，其余命令按原顺序回队；
- `_playback_drive_loop` 每帧 `_render_to_output` 后调用它——分析的正是刚写入的
  前台纹理，播放中示波器以 ~15Hz 跟手更新；`[DRIVE]` 统计新增 scope 耗时列。

**验证**：`tests/composite/test_async_present_readback.py` 新增队列消费逻辑测试
（latest-wins、非 scope 命令保持队列位置、无 scope 时 no-op）；
`tests/composite/test_composite_gpu_scope_handler.py` 新增真实 GL 端到端
（队列出队 → 真实 scope pass → frame 发出）；composite 套件 4845 passed、
color scope pipeline 70 passed。回归中的其余失败/崩溃（entry_source×4、shared×3、
edit/ui/color 各一处 abort）经 HEAD stash 对照与单文件重跑确认为存量或环境偶发。

## 10. CIE 色度图 chrome 重绘（2026-08-29 追加）

按 DisplayCAL 风格参考实现（用户提供的 `cie_plot_demo_new.py`）完全重做 CIE
示波器的绘制：

- **架构**：GLSL display pass 只保留密度热图 LUT；全部图面 chrome 由新的
  `CieGpuScopeWidget`（`src/shared/ui/widgets/cie_scope_widget.py`）用 QPainter
  叠加绘制——文字、线型、渐变色在 GLSL 里无法实现且难以与参考对齐。
- **chrome 内容**：0.1 主网格（缩放 ≥2/≥3 时 0.05/0.02 次网格）、坐标刻度
  标签与 x/y 轴标题、波长渐变彩色光谱轨迹（Bruton 算法）+ 紫线、5 个色域
  预设三角（sRGB/Rec.709/Adobe RGB/DCI P3/Rec.2020，各自颜色 + 点/划线型，
  可选半透明填充）、等能点 E、日光/黑体色温轨迹（brucelindbloom /
  Wikipedia 解析式移植自 DisplayCAL）带 K 值标注、左上角缩放标与
  "CIE: 1931 xy"、右上角当前色域图例。
- **右键菜单**（对齐参考实现）：缩放（放大/缩小/重置）、显示色域多选、
  填充色域区域、显示光谱轨迹、显示等能点、显示网格、显示坐标轴标签、
  色温曲线（不显示/日光/黑体）。
- **视图交互**：滚轮以光标为锚缩放（0.5–10x）、左键拖拽平移、双击重置。
  缩放是**纯呈现侧**操作——`GpuScopeWidget` 新增 `_blit_src_rect` 钩子，
  CIE 用视图窗口选择 blit 源子矩形，不重跑分析、暂停时也可用。
- **macOS 适配**：Qt OpenGL 绘制引擎上 `setClipRect` 行为损坏（裁剪后的
  绘制整体消失且 `setClipping(False)` 无效），chrome 全部改用
  Liang–Barsky 线段级解析裁剪，raster/GL 引擎行为一致。
  `paintGL` 进入时清除陈旧 GL 错误（Qt 自身合成可残留 GL_INVALID_ENUM
  导致 PyOpenGL 在 `glIsTexture` 上抛错吞掉整帧），blit 与 chrome 绘制
  分别捕获异常互不拖垮。
- **验证**：`tests/color/test_cie_scope_widget.py`（11 例：色温轨迹已知点、
  波长颜色、色域预设、视图窗口/往返映射/裁剪矩形、选项开关）；
  grabFramebuffer 离屏渲染人工核对了默认/全色域+填充+黑体/4x 缩放三个
  状态（截图与参考实现逐要素一致）。bench standalone 全部 PASS。
