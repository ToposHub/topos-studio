# T-P1-20 执行报告：统一 Effect Capability Contract 与插件导出策略

- 执行日期：2026-09-18
- 分支：chore/edit
- 依赖：T-P0-00（DONE）

## 结论

1. **统一契约（卡步骤 1）**：新增
   `EffectExecutionCapability`（src/features/shared/effects/effect_capability.py）
   —— 七显式轴（cpu_preview / gpu_preview / cpu_export / metal_export /
   opengl_export / preview_only / unsupported）+ 每轴 (code, message)
   reason + 质量约束（metal 参数子集门控）。唯一解析入口
   `resolve_execution_capability()`；`delivery_restriction()` 是 UI / AI
   manifest / Deliver preflight 共用的拒绝原因出口（三处逐字一致）。
   休眠的 P7-06/07/08 基座（后端轴/声明解析/诊断）就此接入生产。
2. **4 个 Edit 插件 → 显式 preview_only（卡步骤 2）**：Bilateral Skin
   Smooth、Color Space Transform、HighPass Retouch、Film Halation 仅声明
   GPU 预览后端（catalog `plugin_capabilities=("gpu_preview",)` 本就如此，
   现在被契约显式化）。统一拒绝链验证：
   - Deliver preflight：`plugin_preview_only` / `plugin_identity_<status>` 结构化码阻断；
   - AI manifest：`EffectCapability.exportable=False, preview_only=True`
     + `delivery_restriction.code=plugin_preview_only`（AI 无法再规划必然被阻断的效果）；
   - UI：Effects 面板 preview-only 效果名后缀「（仅预览）」+ tooltip 携带 (code, message)。
3. **Color MSL（卡步骤 3）**：维持既有显式 ValueError
   （msl_composition.py: 未迁移效果/插件 manifest 进 MSL 管线即 G4 失败；
   shader_compiler 原始 dict 进入口即 G5 TypeError）——无可验证 MSL 不进
   Metal/export graph，行为正确，无需改动。
4. **Ghost 清除（卡步骤 4）**：transform / crop / opacity / vignette 四个
   legacy FX 定义从 EffectRegistry 移除（审计 P6-step-1 落地；它们从未接
   渲染后端，删除视觉无损）。真实能力保留为 clip-native（TransformParams /
   CropParams / ClipInstance.opacity）。旧工程迁移：ClipInstance.from_dict
   剔除 ghost 实例 + warning 日志；NON_ATTACHABLE 集合保留为 attach 路径
   防线。AI manifest 不再暴露任何 ghost（"registry 可见但语义不明"清零）。
5. **结构化错误码（卡步骤 5）**：`ExportEffectBlocker(effect_type, code,
   message, clip_id, effect_id)`；码表：`effect_not_exportable` /
   `effect_unknown` / `plugin_preview_only` / `plugin_identity_<status>` /
   `catalog_unavailable`。能力模块不可导入时 **fail-closed**（此前
   fail-open 会静默产出缺效果文件——已修正）。Deliver preflight 透传
   `edit.video_effects.<code>`。

## 真实验证（truth matrix）

7 个 exportable 效果 × (TOPOS Proxy, H.264) × 48 帧（2K 真实 TOPOS 源，
单效果时间线，参数保证效果参与渲染）：

- 全部导出成功、输出可经统一 media factory probe；
- 中间帧 Y 平面内容哈希与 identity 基线不同（blur/erode_dilate/defocus
  为均值保持滤波——对拍用哈希+标准差，不用均值差）；
- **all_pass = True**（truth_matrix.json：14/14 PASS）。

注：首版对拍用 Y 均值差，对均值保持效果误报 diff=False——修正为
平面 sha256 哈希为准（脚本内注释说明）。此教训记入 X-TEST-40 协议：
像素差异证据必须用内容哈希，不得用均值。

## 测试

- 新增 tests/edit/test_effect_execution_capability.py（10 项）：解析器
  七轴语义、未知类型 unsupported 码、插件 preview_only、to_dict 一致性、
  preflight 结构化码、fail-closed、迁移后 preflight 不卡死。
- 更新 tests/edit/test_effect_catalog_capabilities.py：ghost 四件套改钉
  「已移除 + 统一契约 unsupported」；新增迁移剔除测试。
- 更新 tests/edit/ai_editing/test_taep_contracts.py：manifest 效果能力断言
  迁移到 blur + ghost 不暴露 + 导出语义字段。
- 回归：目标套件 250 + 176 项通过；tests/export、tests/media 关键套件通过。
- 预先存在（与本任务无关，干净 HEAD 复现）：test_real_media_probe_matrix
  （环境媒体依赖）、test_audio_leftover_samples H265 参数（环境）、
  tests/edit 全量套件在 test_audio_waveform 处段错误（套件级 Qt 状态
  污染，HEAD worktree 复现；已单独记录待修，不阻塞本任务）。

## 遗留

- Film_Halation 的 process() 本体是占位（直接 return color）——preview_only
  标注如实反映；如需真实 Halation 需插件侧立项。
- Composite 的 vignette 实现与 Edit 的桥接未做（vignette 在 Edit 侧按卡
  选择「删除历史注册项」路线）；如产品需要 Edit 内 vignette 效果，另起
  feature 任务（renderer+schema+export 三件套）。
