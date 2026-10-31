# T-P1-50 执行报告：Color GPU 执行、插件和真实桌面验证（批次一）

- 执行日期：2026-09-18；状态：**IN_PROGRESS**

## 本环境可执行项（已完成）

1. **卡点名套件全部通过**：
   - tests/color/test_metal_composition_{grading,curves_lut,blend_modes,
     blur_sharpen,grain_softclip,mixer_warper}.py +
     test_color_e2e_consistency_matrix.py → **126 passed**。
   - tracking：test_tracking_{curve_transform,data,keyframe_bake} +
     test_auto_color → **58 passed**。
   - tests/color 全目录：**1511 passed, 4 skipped**（37.8s）。
     例外：test_cache_fix.py 在解释器层 Illegal instruction——干净 HEAD
     worktree 复现，预先存在的环境问题（独立 defect，不在本卡范围）。
2. **卡步骤 3（共享色彩数学）**：timeline 导出与 Color 页共用
   `the_decompiler.GradingMath.apply_grading`（timeline_export.py:7719/
   7782）；T-P1-21 的 10-bit 对拍证据：GPU uniform 管线 vs CPU
   GradingMath 均值 Δ=0.0000/1023、max 1 LSB——同一数学的两种执行域
   等价性已量化。
3. **卡步骤 4（插件 MSL/preview-only）**：T-P1-20 契约已落地——4 个
   Edit 插件显式 preview_only（AI manifest/Deliver preflight/UI 三处
   一致拒绝）；MSL emitter 对未迁移效果显式 ValueError（无运行时隐式
   跳过，msl_composition.py + shader_compiler G4/G5 门）。

## BLOCKED（本环境不可诚实验证）

- **卡步骤 2（拖动稳态 p50/p95、首帧冷启动、参数稳定性、GPU memory）**：
  需要交互式桌面会话——`BLOCKED: DESKTOP_GPU_NOT_VERIFIED`（本会话为
  offscreen 无头环境，按 X-TEST-10 规则不得声称桌面 GPU 已验证）。
- **卡步骤 5（Tracker/Auto Color 桌面 UI 验收、RAW Develop 真实 RAW
  素材矩阵）**：同上 BLOCKED；Auto Color 的单元/数据层测试已通过
  （58 项含 test_auto_color）。
- **Metal 三路对拍（卡步骤 1 的 Metal 段）**：Metal 组合渲染套件通过
  （126 项含 metal_composition_*），但"真实帧 2K TOPOS 的 Metal 导出
  链路对拍"需 T-P0-31（Metal surface 输出）先行——保持其 IN_PROGRESS
  依赖。

## 遗留（需桌面会话或前置任务）

- 真实桌面 runner 全量验收（X-TEST-10 联动）。
- Blur/Defocus 下采样质量档（T-P1-21 遗留）落地后补三路对拍。
