# B2.3 / B2.4 / B3 / B4 / C3.1 条件门禁决策记录（2026-10-24 · 机器 B）

> 计划：`docs/perf_plan_B_windows_2026-10-22.md` §4 架构门禁——
> "B3/B4 只在既有路线不足且有成本/收益证据时立项"。本文逐项记录
> 触发评估结论；**不触发 ≠ 永久关闭**，证据变化时可重开。

## B2.3 AMD/Intel 扩展决策：**暂缓——硬件门未满足（非拒绝）**

计划要求：RX 6800 须在其硬件上通过实际格式解码、surface 消费、同步/
引用寿命、同机 CPU 对比及失败恢复，才考虑从 NVIDIA 门扩展为能力门；
Intel 需要自己的硬件证据，不从 AMD/NVIDIA 推断。

机器 B（RTX 4090）**无法产出**上述任何证据；本机可交付的是能力门
设计（供 C/Intel 机器执行）：

1. cuda 路线（`cuda_resident.py`）本身是 **NVIDIA 门**（nvcuda.dll
   存在性 + cuda-bindings），AMD/Intel 机器上能力探测 fail-closed
   回落既有 3.5/CPU 路径——不需要为不支持的机器写禁用逻辑。
2. AMD（RX 6800，机器 C）候选路线是 **d3d11va 驻留 + 原生桥接**
   （B1.1 本机已证伪 NVIDIA 上的 d3d11va 保留，但那是本机证据，
   AMD 需独立验证）——前置条件 `topos_hwdecode_windows` 原生扩展
   仍缺席（B0.3 §2，环境修复 backlog）。
3. 扩展为"能力门"的验收清单（冻结）：实际格式解码（非仅设备创建）→
   surface 消费（interop 对拍）→ 同步/引用寿命（seek/取消/EOF）→
   同机 CPU 成对比较 → 失败恢复注入。全过才改路由门。

## B2.4 Topos GPU 解码 ADR：**不启动——触发条件未满足**

计划要求："仅对应素材解码仍是瓶颈时启动"。证据：

- B0.3 §7 瓶颈假设 H1–H4 均不含 Topos 解码（H4 明确排除项之外的
  四项假设全部围绕 H264/H265 拷贝税与多轨 CPU 竞争）；
- B2.2 矩阵：Topos 走 topos native CPU 路径，与 GPU 路线解耦，无
  生产瓶颈数据支持立项；
- B0.1/B0.3：rANS/重建/D3D11 compute 与纹理驻留的 HLSL 执行链
  （D3DCompile/RenderBackend/常量 packing）全缺（B0.3 §6-3），
  立项成本高而收益无证据。

重开条件：多轨 Topos 素材播放的实测掉帧归因到 Topos 解码 CPU 占用
（telemetry 证据），且 B3.2 的 HLSL 真机执行链已过门。

## B3.x（D3D11 复用与缺口 ADR / 单效果真机 PoC）：**未触发**

- 进入条件"既有路线不足"不成立：B2.1 已用 **cuda→GL 纯 Python
  路线**接通预览零拷贝（L2），D3D11 合成/导出链的候选收益场景
  （导出零回读直通）在导出侧——当前导出瓶颈证据（B0.3 §4-6b 的
  YUV420 回读 ≈12.4MB/帧）量级不足以触发立项（B0.2 导出路径无
  实测回归数据支持"不足"结论）。
- B3.1 另要求"与 A 会签"——A 线（Mac）不在本会话范围，即使证据
  成立也无法完成会签步骤。
- 保持现状：`timeline_export.py` 的 D3D11 selector 后
  `not_yet_implemented:d3d11_timeline_composite_integration` 显式
  拒绝分支**不动**（fail-closed 是设计，不是缺陷）。

## B4.x（HLSL 效果能力 / 导出链接通 / DeckLink GPU 输出）：**未触发（依赖 B3.2）**

B4 各项显式依赖 B3.2 通过；B3 未立项则 B4 连锁未触发。DeckLink
（B4.3）保持现状：`submit_gpu_texture()` 返回 False、CPU 回读保留
（B0.3 §3-E），SDK/驱动可行性证据未具备，符合计划"未具备条件可持续
使用 CPU 路径"。

## C3.1 native/后端稳定性长跑：**短时压力已完成（本次），10h 长跑按计划为后续执行安排**

见 `docs/c31_stress_cuda_resident_2026-10.md`（顺序+随机 seek+导入
循环短压）；计划原文"定期长跑是后续执行安排，本计划不创建自动任务"
——10h 档与设备事件（TDR/拔卡）人工触发项如实登记为后续。
