# T-P1-22 执行报告：Adjustment Clip、音频与时间线交付完整性（批次一）

- 执行日期：2026-09-18；状态：**IN_PROGRESS**（卡内 5 项中 4 项完成）

## 已完成

1. **Adjustment Clip 效果/调色传播（卡步骤 1+2）**：
   - 导出 `_get_layer_frame` 补 `adjustment` 分支——"Unsupported asset
     type: adjustment" 误导日志消除（无源帧是契约而非缺失）；
   - 非空 ColorStack 挂 adjustment 层此前三路（导出 CPU/预览 CPU/GPU
     预览）全部静默丢弃——三处补齐：导出 FX-C04 恢复段 grade→effects、
     预览 CPU 同序、GPU 预览快照携带 preview_grade 原位 grade。
   - 像素级证明：纯色底 + 上层 adjustment（ColorStack exposure=80），
     导出后作用范围内均值显著抬升（>20/255）、范围外保持基色——
     `test_colorstack_on_adjustment_affects_lower_track`。
2. **H265/ProRes 音频 leftover 测试修复（卡步骤 4 部分）**：
   test_audio_leftover_samples 的 H265/ProRes 参数此前恒失败。根因是
   **测试参考流自身的缺陷**（解码器无恙）：packed s32 素材的
   `to_ndarray()` 是 (1, 2N) 交错数据，参考流既未解交错也未按位深归一
   （`astype(float32)` 先于 dtype 检查导致归一化分支永假）——索引与
   量纲双重错位。修复为 reshape(-1, ch).T + 先查 dtype 再按位深除。
   16/16 通过。
3. **20 次循环 seek 稳定（卡步骤 4 部分）**：H265/ProRes 各 20 轮
   seek→解码，断言每轮首块逐位一致、无 remainder 泄漏——通过。
4. **clip-native 正式 AI operation（卡步骤 5）**：plan compiler 的
   SET_TRANSFORM/SET_CROP 此前 param_type 回落到 kind.value
   （"set_transform"/"set_crop"），命令层 apply 只认 "transform"/"crop"
   ——编译通过但应用必炸；且 JSON dict 值不转 dataclass。修复：
   kind→param_type 映射补全 + `_coerce_clip_param_value`（dict→
   TransformParams/CropParams/SpeedParams，非法载荷 compile 期显式
   CompileError；SET_SPEED 便捷数值形态）。

## 音频 DSP（卡步骤 3）

导出与实时共享 AudioGraphRenderer（AM-03），既有
test_audio_export_parity + test_audio_three_way_parity 16 项通过；
8 个 DSP（strip_eq/limiter/compressor/reverb/delay/saturation/imager/
noise_gate）实现于 audio_dsp.py 且有注册表一致性健康检查。本批次未新
增跨 packet 边界的真实时间线 DSP 用例（遗留）。

## 测试

- 新增 tests/edit/test_adjustment_clip_delivery.py（8 项）：warning
  消除、ColorStack 像素作用域、compiler 三操作、20 循环 seek ×2 素材。
- 回归：taep 契约/agent models/能力契约/既有 adjustment 套件 118+7 通过。
