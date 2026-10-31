# F2.8 时间线视频变速与插值质量 — 证据台账

- 日期：2026-10-24
- 结论：**已完成（核验 + 两缺陷修复 + 验收值直测 + 注册表声明）**。
  受控光流 provider（RIFE）按计划登记 F7.2 条件依赖，普通视频变速不等待。
- 提交：`7045b41d6`（feat/f-plan，已 ff-merge fix-ui 并推送）。

## 1. 核验结论（Explore 2026-10-24，全量矩阵见调研）

| 声明 | 实际（核验时） | 处置 |
|---|---|---|
| nearest | 全链真实（预览/GPU/CPU/导出/Metal scalar 直通） | 已满足，验收固化 |
| frame_blend | 导出 CPU 真实现（含 wipe 侧帧、GPU 显式让位）；**预览 GPU 路径不实现也不拦截=静默 nearest** | **缺陷已修**：GPU 门控整帧回退 CPU |
| optical_flow | 模型枚举可写入但渲染端零分支零告警（UI 已隐藏，API/JSON 写入即静默 nearest） | **缺陷已修**：预览+导出显式告警（clip 去重） |
| 冻结 | 三路一致短路（视频+音频三处跳过） | 已满足 |
| 反放/速度点 | 视频 canonical 映射、音频导出 ClipRetimeReader 同源；实时音频不支持反放（已知，属 F3.2 域） | 已满足/登记 |

## 2. 验收对照

| 验收点 | 证据 |
|---|---|
| 25→50fps | tests/edit/test_retime_interpolation_quality.py::TestCadence25To50（cadence 快照锁定 + 权重恰在半格 0.5） |
| 40% 慢放 | ::TestSlowMotion40Percent（映射快照 + 权重循环 0.4/0.8/0.2/0.6 + 非零入点源窗不漂移） |
| 平滑速度斜坡 | 既有 test_smooth_speed_ramp.py + test_round4_core_time_windows.py（smooth × reverse × mixed-fps 参数化） |
| 非零入点/冻结/反放 | 同上矩阵（source_start=123 fixture；test_freeze_frame.py；reverse 正反映射） |
| trim/split/Undo/重开保留 | test_round4_core_time_windows.py（split 逐帧不变 + JSON roundtrip + undo 还原，参数化 fps×reverse×smooth） |
| 插值预览/成片一致 | frame_blend：GPU 预览帧现在回退 CPU 混合（与导出同实现 `_apply_frame_blend_layer`）；::TestGpuRetimeGate（正权重回退/对齐直通/nearest/冻结四态） |
| 不把 optical_flow 静默当 nearest | 预览 `_retime_interp_requires_cpu` + 导出循环均显式 WARNING（去重一次）；::test_optical_flow_warns_once_not_silent |
| 声明支持项入册 | capability_registry 新增 `edit.video.retime_interpolation`（AVAILABLE，evidence 5 项，光流 UNAVAILABLE 语义写入 notes；此前注册表零速度条目） |

## 3. 关键决策

1. **GPU 门控位置**：`PreviewRenderWorker._retime_interp_requires_cpu`
   （独立可测 helper）在 `_try_gpu_render` 入口整帧判退——与导出 GPU
   的 `frame_blend_retime` 让位同语义、同名 reason。
2. **optical_flow 处置=显式告警而非拒绝**：旧工程/API 已可能写入该枚举，
   硬拒绝会破坏加载；UI 本就隐藏第三项，渲染端告警满足"不静默"。
3. **受控光流 provider 不在本任务建**：RIFE 模型 `source_url=""` 需本地
   转换、models/ 无权重文件——打通前声明光流不可用（注册表 notes），
   按计划登记 F7.2。
4. **实时音频反放/速度曲线预览**：属 F3.2 流式保调域（导出已同源正确），
   与 preserve_pitch PARTIAL 条目一致，不在本任务扩大。

## 4. 测试账本

- 新增 tests/edit/test_retime_interpolation_quality.py：10 项。
- 护栏：frame_blend/mapper/time_windows/smooth/freeze/preview_renderer
  292 项 + capability/audio capabilities 44 项全绿。

## 5. B 依赖核对

无 Plan B 依赖（CPU/GPU 预览与导出路径均为跨平台代码）。

## 6. 登记的后续项

1. 受控光流 provider（F7.2）：RifeInterpolator + (a,b,w) 的"RIFE 版
   frame_blend"，仅导出/缓存路径先接，`has_model()` 解锁 UI 第三项。
2. 实时预览音频反放/速度曲线（F3.2 域）。
3. frame_blend 遮挡/切镜/快速运动质量样片（随 F7.2 光流对比夹具一起产）。
