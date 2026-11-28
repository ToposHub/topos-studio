# Topos Edit 播放基线（Metal / OpenGL A/B）

- 采集时间（UTC）: `2026-11-22T18:03:07+00:00`
- 提交: `3416549b6cc061194eb02db57368c9dd0cb40fff`
- 机器: Mac Pro · Intel(R) Core(TM) i9-9900K CPU @ 3.60GHz · AMD Radeon RX 6900 XT, PHL 439P1
- A/B 目录: `bench_out/metal-ab.2026-11-23` · 各配置轮数见「各轮呈现帧」 · 分母为区间唯一期望帧 400
- 显示器电源状态（采集时）: 未记录
- 本文件的性能与呈现帧数在显示器清醒时测得（本机 `displaysleep 30`，显示器约于 02:14 休眠，本次运行早于此）。
- **`实际渲染后端` 一列不可信**：本次运行早于 TPR `_lookup_frame_trace_id` 的修复，present 到达时 trace id 查得 0、标注落空，所以每帧都显示入队时的占位值 `opengl`。
- 各配置的实际后端以门控诊断为准（`metal-preview-gate-diag*.json`）：`gl` 全为 OpenGL；`metal` 下 H.264 帧由 Metal 渲染、DNxHR 为 planar 帧留在 OpenGL；`auto` 见 auto 诊断。
- `gl` 配置的进程启动于阶段 1.1 协议改动之前，`auto` / `metal` 启动于其后；该改动只给描述符附加租约键，不改变 GL 消费者的行为。

呈现帧数取 `frame_presented`（paintGL 之后发射），中位数跨有效轮次；时间线未加载或 0 帧的用例计为无效轮，不进入统计。

| 配置 | 环境变量 | 时间线 | 呈现帧中位数 | 各轮呈现帧 | 无效轮 | 丢帧中位数 | 实际渲染后端 | 解码路径 | 旧帧覆盖 | 槽位未确认复用 | CPU 上传/回读字节 |
|---|---|---|---:|---|---:|---:|---|---|---:|---:|---:|
| `auto` | `TOPOS_BACKEND_GRAPHICS=auto` | dnxhr Original | 373.5 | 332, 372, 375, 380 | 0 | 26.5 | `{"opengl": 1459}` | `{"gpu": 2, "ffmpeg_planar_gpu_upload": 1458}` | 1 | 0 | 2825109504/7633920 |
| `auto` | `TOPOS_BACKEND_GRAPHICS=auto` | dnxhr LUT | 370 | 360, 355, 385, 380 | 0 | 30 | `{"opengl": 1477}` | `{"ffmpeg_planar_gpu_upload": 1476, "gpu": 1}` | 0 | 0 | 2882912256/8724480 |
| `auto` | `TOPOS_BACKEND_GRAPHICS=auto` | h264 Original | 400 | 392, 400, 400, 400 | 0 | 0 | `{"opengl": 1589}` | `{"macos_vt_zero_copy": 1589}` | 0 | 0 | 0/8724480 |
| `auto` | `TOPOS_BACKEND_GRAPHICS=auto` | h264 LUT | 391 | 382, 400, 307, 400 | 0 | 9 | `{"opengl": 1487}` | `{"macos_vt_zero_copy": 1485, "gpu": 2}` | 0 | 0 | 0/7633920 |
| `gl` | `TOPOS_BACKEND_GRAPHICS=opengl TOPOS_EDIT_METAL_PREVIEW=0` | dnxhr Original | 370 | 342, 372, 369, 371 | 0 | 30 | `{"opengl": 1455}` | `{"gpu": 5, "ffmpeg_planar_gpu_upload": 1451}` | 1 | 0 | 2760081408/7633920 |
| `gl` | `TOPOS_BACKEND_GRAPHICS=opengl TOPOS_EDIT_METAL_PREVIEW=0` | dnxhr LUT | 368.5 | 368, 366, 379, 369 | 0 | 31.5 | `{"opengl": 1482}` | `{"ffmpeg_planar_gpu_upload": 1480, "gpu": 3}` | 1 | 0 | 2839560192/8724480 |
| `gl` | `TOPOS_BACKEND_GRAPHICS=opengl TOPOS_EDIT_METAL_PREVIEW=0` | h264 Original | 399 | 391, 399, 399, 400 | 0 | 1 | `{"opengl": 1587}` | `{"macos_vt_zero_copy": 1586, "gpu": 1}` | 0 | 0 | 0/8724480 |
| `gl` | `TOPOS_BACKEND_GRAPHICS=opengl TOPOS_EDIT_METAL_PREVIEW=0` | h264 LUT | 398 | 393, 399, 397, 399 | 0 | 2 | `{"opengl": 1585}` | `{"macos_vt_zero_copy": 1585}` | 0 | 0 | 0/7633920 |
| `metal` | `TOPOS_BACKEND_GRAPHICS=metal` | dnxhr Original | 374.5 | 366, 371, 390, 378 | 0 | 25.5 | `{"opengl": 1506}` | `{"ffmpeg_planar_gpu_upload": 1506, "gpu": 1}` | 1 | 0 | 2897362944/7633920 |
| `metal` | `TOPOS_BACKEND_GRAPHICS=metal` | dnxhr LUT | 384.5 | 386, 362, 387, 383 | 0 | 15.5 | `{"opengl": 1516}` | `{"gpu": 2, "ffmpeg_planar_gpu_upload": 1514}` | 0 | 0 | 2890137600/9269760 |
| `metal` | `TOPOS_BACKEND_GRAPHICS=metal` | h264 Original | 399.5 | 397, 399, 400, 400 | 0 | 0.5 | `{"opengl": 1594}` | `{"macos_vt_zero_copy": 1594}` | 0 | 0 | 21676032/8724480 |
| `metal` | `TOPOS_BACKEND_GRAPHICS=metal` | h264 LUT | 399 | 393, 399, 399, 399 | 0 | 1 | `{"opengl": 1588}` | `{"macos_vt_zero_copy": 1588}` | 0 | 0 | 0/7633920 |
