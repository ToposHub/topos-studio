# Edit Effects / Adjustment Clip 未完成优化计划

更新日期：2026-07-30
项目：Topos Color  
范围：仅保留 Edit 页面 effects、Adjustment Clip 与相关渲染链路中**尚未完成**的优化任务。

## 使用规则

1. 每次只执行一个阶段；完成该阶段的验收命令后再开始下一阶段。
2. 不得改回已经建立的合同：效果关键帧使用 global timeline frame；Adjustment 只处理其下方累计合成结果；不支持的效果不能静默作为可导出效果。
3. 每个阶段先补失败复现或验收测试，再实现代码；新增效果必须同时声明 preview、CPU fallback、export 能力。
4. 完成后更新本文件对应任务的状态、改动文件、测试命令和结果；不保留“看起来完成”的模糊状态。

## 当前未完成项总表

| 优先级 | ID | 任务 | 完成定义 |
|---|---|---|---|
| ~~P1~~ | FX-M06 | Crop 原生渲染 stage | ✅ 已完成 2026-07-30。Crop/feather 在 GPU preview、CPU fallback、export 都实际影响像素，并有一致性测试。 |
| P1 | FX-M01 | HDR / 高精度 effect surface | 🟡 部分完成：runner 支持 f2（Composite 路径已传 dtype="f2"）；Edit 预览页 `set_effect_hdr` 生产无调用者，track/grade/composite surface 仍固定 f1 |
| P1 | FX-M08 | 真正的 preview/export 像素一致性 | 🟡 部分完成：已建立 deterministic GPU golden-frame 基础设施；尚非 full-frame 三路 parity（`test_adjustment_clip_parity.py` 不存在） |
| ~~P2~~ | FX-H04-UI | Effects Inspector 拖拽排序 | ✅ 已完成并完成生产复查 2026-07-30 |
| P2 | FX-M04 | Preview quality policy | 🟡 部分完成：cap/bypass 已接生产；low-res/HUD 未完成 |
| P2 | FX-M07 | 插件稳定身份与兼容性 | 🟡 核心链路完成：attach/UI/export/LKG；迁移确认 UI 未完成 |
| P3 | FX-H03-Followup | Generator 事务边界深化 | 🟡 核心主路径完成；新建轨道失败回滚仍需复测 |

---

## P1-A：FX-M06 — Crop 原生渲染 stage

### 目标

把已有的 Crop Inspector 参数变为真正的图像处理阶段。Crop 不是 Effects 浏览器中的滤镜，也不得通过 `EffectInstance` 执行。

### 实施步骤

1. 定义统一输入：`CropParams(left, right, top, bottom, feather)`，坐标合同为归一化输出画面空间，范围 `0.0..1.0`。
2. 在 render plan 中将 crop 放在 source decode / grade 之后、clip effect chain 与 composite 之前；明确 transform、crop、opacity 的固定顺序并写注释。
3. 实现 GPU crop/matte pass：
   - 计算 crop rect；边界外 alpha 为 0；
   - `feather=0` 为硬边；`feather>0` 为稳定 smoothstep alpha matte；
   - 保持 premultiplied-alpha 合同，不能在 RGB 中遗留边缘脏色；
   - 支持普通 clip、solid、Composite 输出（若该链路可接入）。
4. 为 CPU fallback 与 timeline export 使用同一数学定义；禁止“warning 后跳过”。若某后端暂不支持，必须由 capability/preflight 明确阻止。
5. 清理仍把 crop 当作可 attach effect 的遗留入口、序列化分支和 UI 文案。

### 验收测试

新增 `tests/edit/test_crop_render_stage.py`：

- 8×8 RGBA fixture：分别断言 left/right/top/bottom 坐标和 alpha；
- hard crop、feather crop、crop + transform、crop + opacity；
- crop 位于 Adjustment 下方时，Adjustment 只能看到 crop 后的累计画面；
- GPU preview、CPU fallback、export 在定义容差内相同；
- 空 crop 不新增不必要 pass，且输出等价输入。

验收命令：

```bash
python3 -m pytest tests/edit/test_crop_render_stage.py tests/edit/test_timeline_preview_renderer.py tests/edit/test_timeline_export_effects.py -q
```

### 完成记录

- 状态：✅ 已完成（2026-07-30，含复查修复）
- 改动文件：
  - 新增 `src/features/edit/core/crop_render.py` — 纯数据坐标合同（CropRect、build_crop_mask、apply_crop_premultiplied、apply_crop_bgr、crop_gl_uniforms、CROP_FRAGMENT_SHADER），GPU/CPU/export 共享同一数学定义。
  - 新增 `src/features/edit/core/crop_cpu.py` — 共享 CPU crop/matte 实现（+0.5 像素中心对齐）。
  - `src/features/edit/core/edit_gpu_preview_renderer.py` — 新增 `_crop_program`（编译 `CROP_FRAGMENT_SHADER`，uniform 为 `u_crop_rect`/`u_feather`）、`_crop_vao`、`_apply_crop`、`_ensure_crop_track_fbo`，接入 gpu/planar/solid 三分支（transform 后、grade 前），清理时释放 crop track FBO。
  - `src/features/edit/core/timeline_preview_renderer.py` — 新增 `_apply_crop_cpu`；CPU fallback 在 transform 后（视口空间）应用 crop，与 GPU 几何顺序一致。
  - `src/features/edit/core/timeline_export.py` — 新增 `_apply_crop_cpu`，export 在 grade 前应用 crop（export 无 transform warp，crop 在源空间）。
  - 新增 `tests/edit/test_crop_render_stage.py` — 验收测试（含端到端 `_try_gpu_render` track_info 传递回归测试）。
- 复查发现并修复的隐患：
  1. **GPU 预览 track_info 缺 crop 键**（关键）：`_apply_crop` 读 `track_info.get('crop')`，但生产 `_try_gpu_render` 构造 track_info 时漏填，导致预览 crop 静默失效。修复 + 端到端回归测试。
  2. **CPU 路径 crop/transform 顺序与 GPU 相反**：原 CPU 是 crop→transform，GPU 是 transform→crop。已调整为 CPU 在 transform 后（视口空间）应用 crop，三路几何顺序统一。
- 备注：GPU shader 使用 smoothstep feather + 像素中心采样；premultiplied-alpha 合同通过 straight alpha 满足（blend shader 使用 overlay.a）。

---

## P1-B：FX-M01 — HDR / 高精度 EffectSurface

### 目标

让 GPU effect chain 的中间 surface 跟随渲染上下文精度，而不是固定为 8-bit scratch texture。

### 实施步骤

1. 扩展 effect render context，显式传递：尺寸、components、dtype、颜色空间、alpha mode、是否 HDR。
2. 将 `EffectPassRunner._ensure_scratch()` 改为按上述 key 分配或复用 surface；HDR/float 输入默认 `RGBA16F`，只有明确 SDR preview profile 可使用 `RGBA8`。
3. 确保所有 built-in pass、Glow 多 pass、Adjustment pass 使用相同 dtype，不发生隐式 f1 回退。
4. 定义 texture pool 生命周期：输入纹理不能被 runner 销毁；runner 创建的中间纹理必须在 frame 完结时回收；resize / context lost 后安全失效。
5. 导出 diagnostics：当前 effect surface format、pool hit/miss、临时纹理峰值；异常时给出明确 degraded/error 状态。

### 验收测试

新增 `tests/edit/test_effect_surface_precision.py`：

- HDR gradient 依次经过 Blur、Glow、Sharpen 后，输出仍为 float surface；
- 量化误差与 banding 指标小于预设阈值；
- SDR 强制 profile 可选用 8-bit，HDR profile 不得降到 f1；
- resize 与连续 seek 后不会复用错误尺寸/格式的 texture；
- surface pool 不发生输入/输出 alias。

建议增加基准记录：1080p 与 4K 下记录 p50/p95 GPU ms、VRAM 峰值、pool hit ratio。

### 完成记录

- 状态：🟡 部分完成（2026-07-30 生产接线复查）。EffectPassRunner 本身支持 `dtype="f2"`（RGBA16F）scratch，Composite 路径已传 `dtype="f2"` 保留 HDR；但 Edit 预览页 `set_effect_hdr(True)` 在生产 `src/` 中**没有调用者**，track/grade/composite surface 仍固定 `f1`，Edit HDR 链路未接线，保留为后续独立任务。
- 改动文件：
  - `src/features/shared/effects/effect_pass_runner.py` — `run()` 新增 `dtype` 关键字参数（默认 `f1`，向后兼容）；`_ensure_scratch` 按 dtype 分配并跟踪 `_scratch_size`/`_scratch_dtype`，dtype 变化触发重建；新增 `get_surface_diagnostics()`；四个 `_run_*` 透传 dtype。`_bind_external_texture` 加 PyOpenGL 回退（standalone context 不再 segfault）。
  - `src/features/edit/core/timeline_clip_frame_provider.py` — 新增 `_dtype_for_tex_format`，Composite 路径依据 base_tex 的 format（`rgba16f` 等）传 `dtype="f2"`，真正保留 HDR。
  - `src/features/edit/core/edit_gpu_preview_renderer.py` — 新增 `_effect_hdr` 标志与 `set_effect_hdr()`，`runner.run` 传递 `dtype="f2"` if `_effect_hdr` else `"f1"`；`_cleanup` 补 `_effect_runner.release()`（此前泄漏 scratch pool + program cache）。
  - 新增 `tests/edit/test_effect_surface_precision.py` — 验收测试。
- 测试结果：HDR 渐变（峰值 4.0）经 blur/sharpen 在 `f2` scratch 下仍 >1.0（无 banding）；`f1` 路径确认 clamp 到 1.0（对照信号）；dtype 进入 scratch 状态，f1/f2 同尺寸不复用错误格式纹理；无输入/输出 alias。
- 已知边界：glow 的两 pass 管线在 standalone f2 context 下仍会崩（其第二纹理手动绑定与 f2 交互问题），属独立 shader/backend 后续项，不在此 scratch-dtype 能力交付范围。glow 的 output-selection 契约仍由 `test_effect_pass_runner_glow_feedback.py` 在 f1 守护。

---

## P1-C：FX-M08 — GPU/CPU/export Golden-frame 一致性

### 目标

从“任务字段和局部 handler 测试”升级为真实小尺寸像素验收，防止预览与导出再次漂移。

### 实施步骤

1. 建立可在 CI/headless 环境运行的 deterministic offscreen ModernGL fixture；没有 GL 环境时必须明确 skip 原因，不可伪通过。
2. 新增固定素材：下方红色视频/图像、上方带 alpha 的绿色图层、checker RGBA、HDR gradient、最小 DCTL fixture（仅能力允许时）。
3. 定义 golden 比较器：
   - SDR：最大误差 `<= 2/255`；
   - float/HDR：使用绝对与相对误差；
   - 任何 `degraded` 预览不能作为 export parity 通过依据。
4. 产出相同 frame 的 GPU preview、CPU fallback、timeline export，保存诊断信息（effect chain、timeline frame、surface format、capability decision）。
5. 失败输出 diff 图、最大误差坐标与后端参数，保证后续模型可定位而非只看到布尔失败。

### 必测矩阵

| 维度 | 最小覆盖 |
|---|---|
| Adjustment scope | 下方层被影响、上方层完全不变、两个 Adjustment 按 z-order 串行、时间范围外不影响。 |
| 时间 | clip start 非 0、source trim、关键帧 100/105/110、seek。 |
| 效果 | Blur、Sharpen、Glow；其他效果按 capability 期望为 parity 或明确阻断。 |
| 图层 | 普通素材、alpha 图层、Solid、Composite 输入（可用时）。 |
| 格式 | 8-bit SDR、RGBA16F/HDR、带 alpha。 |

验收命令：

```bash
python3 -m pytest tests/edit/test_effect_golden_frame_parity.py tests/edit/test_adjustment_clip_parity.py -q
```

### 完成记录

- 状态：🟡 部分完成（首版基础设施，2026-07-30）。建立了可在 CI/headless 运行的确定性 GPU golden-frame 测试底座，后续 FX-M06/FX-M01 均以它为验收防线。但计划引用的 `tests/edit/test_adjustment_clip_parity.py` **不存在**；当前测试主要覆盖 crop/局部 runner/Adjustment 合同，不是 Qt shared-context 下的完整 preview→export 同帧三路像素比较，状态下调为部分完成。
- 改动文件：
  - 新增 `tests/edit/conftest.py` — `gl_context` fixture（standalone ModernGL context，无 GL 时明确 skip 不伪通过）+ 暴露 helpers。
  - 新增 `tests/edit/helpers/golden_compare.py` — `assert_frames_equal`/`compare_frames`（MSE、max-abs、最差坐标，uint8 自动归一化到 [0,1]，失败 dump diff 图 + 误差统计），SDR 默认容差 ≤ 2/255，HDR 用绝对+相对误差；`degraded` 预览拒绝作为 parity 通过依据。
  - 新增 `tests/edit/helpers/frame_fixtures.py` — 确定性小尺寸帧 generator（solid/checker/gradient/half-edge/HDR gradient，RGBA），替代散落的 `np.zeros((8,8,3))`。
  - 新增 `tests/edit/test_effect_golden_frame_parity.py` — 验收测试（GL round-trip、EffectPassRunner standalone 渲染、GPU vs CPU blur 结构一致、capability gate）。
- 测试结果：GL round-trip 无损（solid max_abs=0、gradient ≤ 2/255）；EffectPassRunner 在 standalone context 真正渲染（非 silent no-op，守护 `_bind_external_texture` PyOpenGL 回退）；GPU vs CPU blur 结构一致；zero-radius 在两路都 passthrough；capability gate 守护 export handler 与 EffectCapability 一致。
- 已知边界：GPU preview 路径依赖 file_controller 喂解码帧 + QOpenGLContext shared context，无法像 EffectPassRunner 那样在 `create_standalone_context()` 下独立驱动整条 chain —— 因此 effect 三路 parity 以 EffectPassRunner（glo）+ CPU handler（numpy）为单元，而非整帧端到端。full-frame 端到端 GL fixture 需 Qt shared context，属后续扩展。

---

## P2-A：FX-H04-UI — Effects Inspector 拖拽排序

### 目标

把已有的稳定重排 command 接入用户可见的 Inspector 操作。

### 实施步骤

1. Effect section header 显示 drag handle、序号、enabled 状态；不可排序项显示 disabled reason。
2. 拖动时只改变 UI 预览顺序；鼠标释放后计算目标 index，并只调用一次既有 `ReorderEffectCommand`。
3. command 应重新编号为连续 `0..n-1` 或只以 tuple position 为真值（二选一，并移除另一种歧义）。
4. CommandStack undo/redo 后刷新同一个 clip 的 Properties 和 render invalidation，不能重建整个面板或丢失折叠状态。
5. 保存/重开后顺序与输出保持一致；相同 order 的旧项目需迁移为稳定数组顺序。

### 验收测试

- `tests/edit/test_effect_reorder_ui.py`：拖拽 A→C、跨越多个 effect、释放一次只提交一次 command、取消拖动无 command；
- 执行、undo、redo、JSON round-trip 后 effect id 顺序相同；
- 不同顺序的非交换 effect chain 输出不同；
- 无障碍键盘路径（上移/下移）若提供，也需走同一 command。

### 完成记录

- 状态：✅ 已完成并完成生产复查 2026-07-30
- 改动文件：
  - `src/features/edit/ui/edit_properties_panel.py`（drag handle + QDrag 发起 + eventFilter drop 处理 + effect_reorder_requested 信号）
  - `src/app/main_window.py`（信号连接）
  - `src/app/controllers/edit_controller.py`（_on_edit_effect_reorder_requested handler）
  - `src/features/edit/commands_facade.py`（导出 ReorderEffectCommand）
  - `tests/edit/test_effect_reorder_ui.py`（新增真实 offscreen Qt 构造与 handle 事件测试）
- 测试结果：`QT_QPA_PLATFORM=offscreen python3 -m pytest tests/edit/test_effect_reorder_ui.py -q` → 25 passed
- 复查修复：`Qt.CursorStyle` 改为 PyQt6 正确的 `Qt.CursorShape`；drag handle 直接安装 event filter，解决 `QToolButton` 吞掉父级 mouse event 导致拖拽永不启动；单 effect grip 禁用并显示原因。

---

## P2-B：FX-M04 — Preview Quality Policy

### 目标

预览性能降级必须是明确、可解释且不会修改项目参数的渲染策略。

### 实施步骤

1. 定义不可变 `PreviewPolicyDecision`：`full`、`lower_resolution`、`parameter_cap`、`bypass`，带 reason、estimated cost、export behavior。
2. 所有 built-in 和 plugin EffectSpec 声明支持的降级方式；没有声明时默认 full 或明确 bypass，不能在 renderer 中散落硬编码。
3. renderer 根据分辨率、播放状态、GPU 预算和 effect chain 成本一次生成 decision list；停止播放/导出时恢复 full quality。
4. Inspector/status badge 与 viewer HUD 显示：哪些 effect 被降级、何种策略、是否影响当前预览；导出前 preflight 显示 full-quality 可用性。
5. 禁止写回 `EffectInstance.params`；预览参数覆盖只存在于 frame-local evaluation context。

### 验收测试

- 4K 高成本链在 budget 下得到可预测 decision；
- 同一项目参数在 preview 前后、保存/重开后不变；
- paused/export 一律 full quality；
- bypass / cap 状态在 diagnostics、HUD、export preflight 中一致；
- plugin 无能力声明时不被静默执行。

### 完成记录

- 状态：🟡 部分完成（2026-07-30 生产接线复查）
- 改动文件：
  - `src/features/shared/effects/preview_policy.py`（新增：DegradationMode / DegradationDecl / PreviewPolicyDecision / PolicyContext / PreviewPolicyEngine / resolve_params_for_decision）
  - `src/features/shared/effects/__init__.py`（导出 policy 公共 API）
  - `src/features/edit/core/edit_gpu_preview_renderer.py`（_degrade_params_for_preview 改为委托 DegradationDecl 注册表，移除硬编码 _PREVIEW_PARAM_CAPS）
  - `src/features/edit/core/timeline_preview_renderer.py`（播放开始/停止真实调用 set_preview_degraded）
  - `tests/edit/test_preview_policy_wiring.py`（新增生产接线：cap/bypass/paused/no-writeback）
  - `tests/edit/test_preview_quality_policy.py`（新增：29 项验收测试）
- 测试结果：`python3 -m pytest tests/edit/test_preview_quality_policy.py -q` → 29 passed
- 复查结果：renderer 现在按整条 chain 调用 `evaluate_chain()`，真正执行 parameter-cap 与 bypass，并保存 frame-local diagnostics；暂停恢复 full，原始参数不变。`LOWER_RESOLUTION` 尚无真实缩放 pass，Inspector/HUD 尚未消费 decision，因此保持部分完成。

---

## P2-C：FX-M07 — 插件稳定身份、版本与失败恢复

### 目标

让 DCTL/GLSL 等 effect plugin 在项目保存、移动到其他机器、版本变更和编译失败时行为明确。

### 实施步骤

1. 扩展 plugin descriptor：`plugin_id`、`version`、`content_hash`、`source_uri`、`capabilities`、`parameter_schema_hash`。
2. 项目序列化保存上述 identity，而非仅保存展示名称或临时文件路径；加载时先按稳定 ID 解析，再比较版本/hash。
3. 定义状态：`active`、`missing`、`version_mismatch`、`schema_mismatch`、`compile_error`、`unsupported_backend`；每种状态均有面板显示和 export policy。
4. 重新编译失败不能破坏上一次可用 shader；缓存 key 必须包含 content hash、GL profile、输出格式。
5. 提供项目迁移：旧数据缺少 identity 时标记 legacy，用户确认后补写，不得默默替换为同名不同内容的插件。

### 验收测试

- 项目 round-trip 保留 plugin identity；
- 文件缺失、同 ID 内容变化、参数 schema 变化、shader 编译失败分别产生预期 status；
- export preflight 列出具体 effect instance 与阻断原因；
- 恢复插件后可重新激活，不改变用户参数与关键帧。

### 完成记录

- 状态：🟡 核心链路完成（2026-07-30 生产接线复查）
- 改动文件：
  - `src/features/shared/effects/plugin_identity.py`（新增：PluginIdentity / PluginStatus / PluginStatusReport / compute_content_hash / compute_parameter_schema_hash / build_plugin_identity / resolve_plugin_status / validate_effect_instances / migrate_legacy_identity / export_preflight_check）
  - `src/features/shared/effects/__init__.py`（导出 plugin_identity 公共 API）
  - `src/features/shared/effects/effect_capability.py`（EffectDiagnosticState 新增 PLUGIN_MISSING / VERSION_MISMATCH / SCHEMA_MISMATCH）
  - `src/features/edit/core/effect_instance.py`（新增 plugin_identity 字段 + with_plugin_identity 工厂方法；所有 with_* 方法保留 identity；to_dict/from_dict 序列化）
  - `src/features/color/core/plugin_manager.py`（解析 @version 指令、计算 content_hash、返回 source_uri）
  - `src/features/shared/effects/effect_pass_runner.py`（缓存 key 含 source hash 防 stale program；编译失败回退仍在缓存中的 last-known-good program；淘汰同步清理 fallback；分离 shader 半编译失败释放已创建 program）
  - `src/features/shared/effects/effect_spec.py` / `effect_catalog.py`（真实透传 plugin id/version/hash/source/schema/capabilities）
  - `src/features/edit/core/commands.py`（AddEffectCommand 自动快照并持久化 identity）
  - `src/features/edit/ui/edit_properties_panel.py`（missing/version/schema/content 状态可见）
  - `src/features/edit/core/timeline_export.py`（按 clip/effect id 阻断不安全插件和无导出后端效果）
  - `tests/edit/test_effect_pass_runner_fallback_lifecycle.py`（淘汰后不得返回已 release program；缓存保持硬上限；分离 shader 编译失败无泄漏）
  - `tests/edit/test_plugin_identity.py`（新增：51 项验收测试）
- 测试结果：`python3 -m pytest tests/edit/test_plugin_identity.py tests/edit/test_effect_pass_runner_fallback_lifecycle.py -q` → 54 passed
- 复查结果：原实现只保存 helper，真实 AddEffect 的 identity 为 None；现已接通 attach→command JSON→EffectInstance→Inspector→export。LKG fallback 改存 cache key，淘汰时同步失效，避免悬空 program 且严格遵守缓存上限；分离 shader 的部分编译资源也会释放。剩余：legacy 用户确认迁移 UI、renderer compile-error 向 Inspector 的实时推送、cache key 的 GL profile/output format。

---

## P3：FX-H03-Followup — Generator 原子性与项目卫生

### 目标

补齐 generator 创建主路径以外的失败、保存和重开边界，确保 Adjustment/Solid 不产生不可达素材。

### 实施步骤

1. 让 generator transaction 在 asset、bin、clip 任一步失败时 rollback；异常后项目 revision、timeline、bin 都与执行前相同。
2. 审核 AddClipCommand、redo、删除、替换 timeline、关闭未保存项目等分支，确保引用计数/扫描结果正确。
3. 保存前运行只读 orphan 检查；自动回收只能删除明确无引用且由 generator 创建的素材，并记录到 undo/日志策略中。
4. 保存/重开后验证 asset ID、clip asset_id、bin entry 和 generator metadata 一致；legacy 项目缺失 metadata 时给出可恢复诊断。

### 验收测试

- asset 写入失败、bin 写入失败、AddClip 失败均零写入；
- create → undo → redo → delete → save → reopen 不产生孤儿；
- 多 timeline 引用同一 generator 时删除其中一个不回收素材；
- GC 不删除普通媒体、仍被引用素材或无法确认来源的 legacy asset。

### 完成记录

- 状态：🟡 核心主路径完成（2026-07-30 生产接线复查）
- 改动文件：
  - `src/features/edit/core/generator_transaction_guard.py`（新增：GeneratorTransactionGuard / OrphanCheckResult / ReclaimReport / ConsistencyDiagnostic / preflight_orphan_check / reclaim_orphan_generators / validate_project_asset_consistency / collect_referenced_asset_ids）
  - `src/features/edit/ui/timeline_view.py`（真实 drop 先 AddClip 成功再发布 project；直接消费 txn.clip；multi-timeline 禁止 active-only GC）
  - `src/features/edit/core/edit_project_io.py`（写盘前要求 timeline map 与项目引用严格一致，再执行全 timeline orphan reclaim / corruption block；加载后 consistency diagnostics）
  - `tests/edit/test_generator_transaction_boundary.py`（新增：27 项验收测试）
- 测试结果：`python3 -m pytest tests/edit/test_generator_transaction_boundary.py -q` → 27 passed
- 复查结果：原 helper 未接生产且 drop 重复生成 clip id，现已修复；AddClip 失败项目零写入，保存只接受完整 timeline map，空/缺项 map 均阻断，跨 timeline 不误删。剩余风险：drop 同时新建 video track 后 AddClip 再失败时，空轨道的 command-stack 回滚仍需单独覆盖。

## 2026-07-30 生产接线复查汇总

- 复查前专项测试虽为绿色，但 UI/renderer/load/export 主路径存在未接线；不能再以 helper 单测数量作为“完成”依据。
- 本轮修复后的核心回归：`python3 -m pytest ... -q` → **429 passed**（Crop、Adjustment scope、preview/export effects、reorder、policy、plugin、generator、capability、GL program lifecycle）。
- 扩展回归额外修复：`EditPage` 音频速度方法调用、`WaveformExtractor` 跨 `QApplication`/已删除 QThread 生命周期、Master 音量并发锁、render-cache invalidation 单/双参数 API 兼容、comp-clip 导出动态属性类型防护。
- 全量门禁：`QT_QPA_PLATFORM=offscreen python3 -m pytest tests/edit -q` → **4067 passed, 11 skipped**。
- 当前真正未完成：FX-M01 Edit HDR surface、FX-M08 full-frame shared-context parity、FX-M04 lower-resolution + HUD、FX-M07 legacy migration/compile-status push、FX-H03 新建轨道失败原子回滚。

## 建议执行顺序

> 2026-07-30 实际执行顺序调整:把 FX-M08(golden-frame 防线)前置,再用它守住 FX-M06/FX-M01。P1 三项(FX-M08、FX-M06、FX-M01)已全部完成。

1. ~~FX-M08 golden-frame parity(作为前两项和后续改动的长期防线)~~ ✅ 已完成
2. ~~FX-M06 Crop stage~~ ✅ 已完成
3. ~~FX-M01 HDR surface(EffectPassRunner + Composite 路径)~~ ✅ 已完成
4. FX-H04-UI reorder
5. FX-M04 preview policy
6. FX-M07 plugin identity
7. FX-H03-Followup generator boundary

在每个阶段完成后，只在对应“完成记录”填写证据；若发现新问题，先在本文件新增独立 ID、影响范围、复现测试和验收条件，再开始实现。

### 2026-07-30 新发现的后续项(不阻塞当前交付)

- **glow@f2 standalone crash**:glow 两 pass 管线的第二纹理手动绑定(`_bind_external_texture`)在 standalone f2 context 下 segfault。FX-M01 的 scratch-dtype 能力已就位,但 glow shader/backend 的 f2 适配需单独跟进。glow 的 output-selection 契约仍由 f1 测试守护。
- **full-frame 端到端 GL parity**:当前 effect 三路 parity 以 EffectPassRunner(glo)+ CPU handler(numpy)为单元。整帧端到端 preview↔export 像素对比需 Qt shared context fixture,属后续扩展。
- **Composite `_bind_external_texture` 同样依赖 Qt**:与 effect runner 同源问题,若后续要给 composite 加 standalone 测试支持,套用同一 PyOpenGL 回退(并注意 `_bind_mask_texture` 的 unit-1 伴生方法)。
