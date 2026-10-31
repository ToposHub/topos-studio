# T-P1-70/71 执行报告：统一 Deliver Contract + 稳定性清零（批次一）

- 执行日期：2026-09-18；状态：均 **IN_PROGRESS**

## T-P1-70（Deliver Contract）本批次

1. **三档严重度（卡步骤 2）**：PreflightSeverity 增加 DEGRADED
   （encode fallback / audio omitted / GPU 不可用 / 插件 preview-only
   等"产出可行但能力缺失"项）——add_degraded/degraded 属性；BLOCKING
   语义不变（AI 只能对 warning/degraded 以下自动继续的判断点由消费方
   依此分级）。
2. **DeliveryCapability（卡步骤 1）**：新模块
   deliver/core/delivery_capability.py——describe_delivery_capability()
   统一描述 codec/pix_fmt/container、alpha（仅 4444 家族 + has_alpha）、
   audio（Topos 容器 v1 video-only 契约）、字幕烧录、HDR（10-bit
   像素格式）、效果可交付性（T-P1-20 统一契约投影，未知显式
   capability_unknown）。JSON-safe，测试 6 项。
3. **既有底座对接说明（已在位）**：结构化 per-effect 阻断码
   （T-P1-20 ExportEffectBlocker）、统一阶段 telemetry
   （T-P0-20）、导出 manifest（topos-export-manifest/1）、输出
   probe（finalize_export_with_probe）——批次二按卡步骤 3/4/5 把
   DEGRADED 降级项与输出 probe 的首尾帧 decode/临时文件清理统一入
   manifest。

## T-P1-71（稳定性清零）本批次

1. **AudioWaveform worker 生命周期（卡点名项）**：20 轮
   create→request→cancel_all→收口压力测试通过
   （test_audio_waveform_lifecycle）；cancel_all 为协作式中断 + join
   （对持有 PyAV 容器的 worker 禁 terminate，契约注释确认）。
2. **复现记录（卡协议：先最小 reproduction）**：tests/edit 全量套件在
   test_audio_waveform 中段解释器级 Segmentation fault——顺序依赖、
   干净 HEAD worktree 复现、单独运行同一测试通过。最小复现定位为
   后续批次首个条目（不得在无最小复现前改核心架构——卡规则）。
3. **已收口项**：H265/ProRes seek/boundary（T-P1-22 批次一：参考流
   修复 + 20 循环 seek 通过）；FFmpeg/ffprobe probe 差异（T-P0-10
   DONE：native-only probe + 缓存 + 冲突检测）。
