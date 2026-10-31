# T-P1-11 执行报告：TOPOS CPU 编码器低拷贝与并行优化

- 执行日期：2026-09-18
- 分支：chore/edit
- 任务卡：docs/ai_executable_optimization_upgrade_plan_2026-09-17.md §T-P1-11
- 依赖：T-P0-20（DONE）、T-P1-10（DONE）

## 结论

1. **第一大阶段定位（矩阵实测）**：native encode stage stats 显示 entropy
   占 worker CPU 累计 52–57%，fill 41–45%；crc ≈2.3%、copy 0.24%、
   asm 0.33%。应用层 planar 直通路径本就零拷贝（C 连续 uint16 平面经
   ctypes 指针直达 native），Python 侧无拷贝可省——优化杠杆在 native
   熵编码的并行粒度（slice rows）。
2. **默认 slice_rows 16→8**（`ToposVideoEncoder`，全线程轴证据）：
   - 编码（2K standard，t16）：121.80 → 170.28 fps（+39.9%）
   - 编码（4K standard，t16）：50.43 → 53.04 fps（+5.2%）
   - 编码（2K，t1/t4/t8）：≈持平 / +7.0% / +18.8%
   - 同档输出解码（2K standard，t16）：204.8 → 294.5 fps（+43.8%）
   - 码率门禁：bytes/frame 每档位 +≤1%（2K 1.07→1.08 MB，4K 3.79→3.80 MB）
   - 质量门禁：全部组合 native 解码 roundtrip 平面均值 Δ=0.0；
     qp/CRF/位深/CRC 均未降级（卡步骤 4 合规）
3. **锚点不回退（48 帧 + 8 warmup，同机同源同协议）**：
   - codec-only（decode_encode）：90.22（T-P1-10 基线）→ **101.42 fps**（+12.4%）
   - timeline identity topos_proxy：80.05（T-P1-10 基线）→ **95.02 fps**（+18.7%）
   - 任务卡"codec-only 96.6fps 不回退"：本机基线 90.22 → 101.42，无回退；
     96.6 为计划作者机器口径，本机以自身基线对比。
4. **native stage stats 入报告（卡步骤 5）**：
   `group_native_encode_stage_stats()`（topos_binding）把统计分组为
   `worker_cpu_ns`（多线程累计 CPU time）/ `wall_ns`（墙钟分桶）/
   `counters`；直通导出路径在 encoder.close() 后收集并写入
   `ExportStageTelemetryReport.native_encode_stage_stats`（additive 字段）。
   计数器仅在 `TOPOS_CODEC_PROFILE=1` 时累加（M6 dev 开关）——未开启时
   保持 None（零值 ≠ 测量，不虚报）。

## 矩阵协议

- 源：tests/test_videos/2K_topos_proxy.mov、4K_topos_proxy.mov（真实 TOPOS 422Proxy）
- 输出档位：proxy / lt / standard / hq（CRF 20 固定，yuv422p10le 零转换）
- slice_threads：1 / 4 / 8 / 16（24 核 macOS x86_64）；slice_rows：4 / 8 / 16 / 32
- 每组合：8 warmup + 48 测量帧，单帧复用（隔离编码器成本），子进程隔离，
  `TOPOS_CODEC_PROFILE=1`
- 数据：encode_stage_matrix.json（主矩阵 36 组合）、ext_*.json（rows 轴扩展 8 组合）

### 关键数据（standard 档 fps）

| 组合 | r16 | r8 | r4 | r32 |
|---|---:|---:|---:|---:|
| 2K t1 | 28.35 | 27.86 | — | — |
| 2K t4 | 88.09 | 94.28 | — | — |
| 2K t8 | 122.05 | 145.04 | — | — |
| 2K t16 | 121.80 | 170.28 | 182.35 | 88.69 |
| 4K t16 | 50.43 | 53.04 | 57.10 | 37.16 |

r4 未设默认：仅 t16 单点验证、码率开销较 r8 翻倍（+1.9% vs +0.9%）、
解码侧 slice 数 >512 超出诊断数组容量（解码正确但 slice_status 截断）。
留作后续有完整轴证据再评估。

## 代码变更

- `src/shared/export/topos_encoder.py`：`ToposVideoEncoder` 默认
  `slice_rows` 16→8（含矩阵证据注释）；`ToposImageSequenceEncoder`
  维持 16（图片序列未纳入矩阵，无证据不随动）。
- `src/shared/codec/topos_binding.py`：新增
  `group_native_encode_stage_stats()` 与字段分组常量。
- `src/shared/export/export_stage_telemetry.py`：报告新增
  `native_encode_stage_stats` 可选字段（to_dict 携带，缺省 None）；
  recorder 新增 `set_native_encode_stage_stats()`。
- `src/features/edit/core/timeline_export.py`：直通路径 close 后收集
  native 统计（全零不写入）。
- `scripts/bench_topos_encode_stage_matrix.py`（新）：矩阵工具。

## 测试证据

- 新增 `tests/export/test_topos_slice_rows_and_stage_report.py`（10 项）：
  默认/覆盖/None 语义、图片序列不随动、分组纯函数、telemetry 承载、
  默认 rows 真实编码+解码 roundtrip。
- 扩展 `tests/media/test_topos_direct_delivery.py`：直通 48 帧真实导出
  断言 native stats 存在且 entropy/fill 字段在位（profiling 开启时）。
- 回归：test_topos_binding + test_topos_export + telemetry + v8
  roundtrip/encode + direct_delivery + bridge eligibility + timeline
  export + probe_unification + lossless_boundary + image_quality +
  tests/export 全目录 —— 全部通过（累计 419 项，0 失败）。

## 命令日志

见 command.log。矩阵命令逐组合子进程执行；锚点命令：

```
.venv/bin/python scripts/bench_topos_anchor_matrix.py --child --kind decode_encode \
  --frames 48 --warmup 8 --output .../codec_only.mov --emit-json anchor_codec_only_after.json
.venv/bin/python scripts/bench_topos_anchor_matrix.py --child --kind timeline \
  --scenario identity --group cpu_render --output-codec topos_proxy \
  --frames 48 --warmup 8 --output .../identity.mov --emit-json anchor_identity_topos_after.json
```

## 遗留与边界

- entropy/fill 的 native kernel 级优化（SIMD/算法）不在 Python 仓库范围，
  如需进一步提速需 native 侧立项（预期收益另测）。
- 线程预算维持现状（`_topos_timeline_cpu_threads`：24 核 → encode 16 +
  decode 8）：矩阵显示 2K 在 t8–t16 平台期、4K 仍在爬升，16 不与解码
  争抢（直通路径解码单独预算）。
- X-TEST-40/50 将在统一矩阵下复测本任务数字（计划 §13 第 11 步后续要求）。
