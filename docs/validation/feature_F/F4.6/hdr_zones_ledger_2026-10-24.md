# F4.6 · HDR 分区创作工具与高光塑形 — 验证台账

- 日期：2026-10-24；计划条目：§F4.6
- 测试：tests/color/test_f46_hdr_zones.py（6）全绿

## 1. 核验结论（计划允许"现有工具覆盖/条件延期"收口）

既有 ``GradingMath.apply_soft_clip``（CPU）即"最少几区"的 HDR 高光塑形
实现：scene-linear 域 **Toe 区**（low/black_point/toe_strength，含逐通道
与 RGB 通道平衡）+ **Shoulder 区**（high/white_point/shoulder_strength
比例压缩）+ 区内 exposure/contrast 预调整；CPU 与 GPU 权威文本
（shader_nodes.py SOFT_CLIP_SNIPPET / shader_nodes_msl.py MSL_SOFT_CLIP）
**逐条等价**（AUD-0911/0912 修复对齐，含此前缺失的 toe 分支与通道平衡
块）。预览 GPU/CPU/成片共用该阶段（color_stack_render_service 消费
soft_clip_params）。

## 2. 验收 → 证据（新增向量锁定）

| 验收 | 证据 |
|---|---|
| 保留中间调肤色而调整明亮窗户/高光区域 | `test_skin_midtones_preserved_while_window_shaped`（肤色 0.2-0.45 恒等；窗户 4.0 压缩且不过度） |
| 跨分区无不连续色阶 | `test_zone_boundary_continuous`（high 边界 ±ε 跳变 <2e-3） |
| 负值/超 1.0、黑位行为有定义 | `test_negative_and_black_point_defined`（钳 black_point 无 NaN）+ `test_above_white_point_clamped_not_flipped`（压向 white_point 不翻转） |
| scene-linear 曝光语义 | `test_zone_exposure_pre_adjustment`（×2^exp 中间调恒等放大） |
| 与 qualifier 空间局部选区分开 | 本工具按亮度域分区（ toe/shoulder 门限），与 HSL qualifier（色彩空间选区）互不重叠——声明性边界 |
| CPU/GPU/成片逐项对拍 | AUD-0911/0912 权威文本等价（既有）+ 上述 CPU 向量（GPU 同 snippet 逐条等价即对拍基础）；真机 Metal 逐位对拍沿用既有 metal 测试族 |

## 3. 条件延期（不宣称已实现）

- **分区轮（zone wheel：N 区独立 exposure/saturation 旋钮 UI）**：无对应
  实际 HDR 镜头需求登记——按计划原文记录"现有工具覆盖（两区 toe/
  shoulder + 区内曝光）"，多区 UI 待真实需求后建设，不宣称已实现分区轮。
- 监看设备联验（F5.2 条件依赖）维持延期。

## 4. 后续片登记

1. 分区轮 UI（≥3 区独立调整）待真实 HDR 镜头需求。
2. HDR zone 与 F4.5 qualifier 动画联动（亮度分区参数随帧动画）。
