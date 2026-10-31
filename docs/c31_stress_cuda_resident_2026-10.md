# C3.1 短时压力报告：Windows cuda 驻留零拷贝路径（机器 B · 2026-10-24）

> 计划：`docs/perf_plan_B_windows_2026-10-22.md` §C3.1（"先短时压力，
> 再按风险延至 10h"——本报告为**短压档**；10h 长跑与设备事件人工触发
> 按计划属后续执行安排，本计划不创建自动任务）。
> 脚本：`scripts/stress_cuda_resident.py`；原始数据：
> `scripts/stress_cuda_resident_C31_20261024_run01.json`。
> 被压对象：B2.1 生产链（WindowsCudaResidentSource + CudaGlInteropSession，
> 私有上下文/纹理池/slot 租约全生产语义）。

## 结果

| 阶段 | 规模 | 结果 |
|---|---|---|
| 顺序解码+导入 | 91.4s × 3960 帧（4K H264，wrap 环播） | **0 错误**；每 120 帧内容 md5 抽查（对拍同解码器下载参考）**33/33 全对**；会话 fail_count=0 |
| 随机 seek+导入 | 60.0s × 1127 次随机帧号 | **0 错误**；bind 1127 / fail 0；slot 池水位稳定（1 slot，0 in_use 收尾=租约全归还，无泄漏）；seeks 18.8/s（含主会话 keyframe seek+decode-forward） |
| EOF/close/重开 | 8 循环 | 8/8 OK（open→read(0)→close，含 hwaccel 会话重建） |

吞吐注记：顺序档 43.3fps 含每 120 帧的**参考会话全量重解码 + 全帧
glGetTexImage 回读**（校验成本，非路径成本）；纯 bind 循环速率见
B1.2/B2.2 矩阵（245–320fps GL-ready）。

## 覆盖与未覆盖（如实）

- 已覆盖：顺序 GL 导入、随机 seek（decode-forward 路径）、源重开、
  slot 池 churn、内容正确性周期抽查、租约归还水位。
- 未覆盖（后续档）：10h 长跑（GPU 内存/RSS 趋势、停止后回落）、
  多轨并发、窗口最小化/失焦、设备事件（TDR/拔卡——无法安全注入，
  人工触发项）、跨进程显存竞争。这些按计划属后续执行安排，不冒充。
