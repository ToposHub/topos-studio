# W02 试点回放：TOPOS Proxy 全链路（批次一）

- 日期：2026-09-18；结论：**核心链路 PASS（试点口径）**
- 场景：2K TOPOS Proxy 源 → 底轨 ColorStack 调色（exposure/contrast）+
  顶层 Blur(r8)@0.5 不透明度双层叠加 → TOPOS Proxy 10-bit 输出（16 帧）。
- 验证：native probe 通过（TPIC/1920×1080）；中间帧 Y std=210（非平，
  效果/调色真实参与）；结果携带 9 阶段 telemetry 与机器可读 full_gpu
  blockers（CPU 编码为 TOPOS 契约，非虚报 GPU）。
- 发现（记入 T-P1-21 批次二）：双层 provider 取帧路径未采样 decode 阶段。

## 全 W 矩阵状态

| ID | 可执行性 |
|---|---|
| W01 | 可执行（待跑） |
| W02 | **试点 PASS**（本目录） |
| W03 | 可执行（依赖 T-P0-30 bridge，待跑；Metal surface 依赖 T-P0-31） |
| W04 | 可执行（音频 DSP 真实时间线用例补齐后） |
| W05 | 部分（93 节点矩阵已可解释；绿幕 fixture 批次 B） |
| W06 | 部分（计划期拒绝已测；可交付插件尚无——4 插件均 preview_only） |
| W07 | 可执行（待跑） |
| W08 | 可执行（强制 CPU/无模型环境） |
