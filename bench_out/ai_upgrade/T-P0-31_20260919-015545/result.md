# T-P0-31 批次二：decode telemetry 真实性 + H.264/NV12 性能收口（2026-09-19）

## 结论

- **full_gpu_blockers = []（空）**：TOPOS 422 → IOSurface 解码 → Metal NV12
  surface → VideoToolbox H.264 全链路首次在本机拿到可声明的 full-GPU
  telemetry（decode=topos_native、render=metal、encode_submit/encode=
  videotoolbox、cpu_upload/download=0）。
- **性能达标**：240 帧（3×80 背靠背拼接）1920×1080：
  - 暖进程 **93.26 FPS**（≥60 目标）；冷进程 49.69 FPS（含一次性
    Metal 管线编译 / VT 会话 / 解码器初始化 ≈2s）。
  - 稳态阶段 p50：decode 2.9ms + render 9.2ms + encode_submit 0.18ms
    （异步），合计 ≈12ms/帧 ≈ 83+ FPS。
- 输出 probe：h264 / yuv420p / 240 帧可解码。

## 本批修复

1. **decode telemetry 未知标签**：`GpuDecodedFrame.source_kind` 默认
   "unknown"，topos surface 路径未设置 → `stage_backend_cpu:decode:unknown`
   假阻断。现如实标 `topos_native`，并加入 GPU_BACKEND_TOKENS（注释说明
   直出 planar CPU 路径仍被 cpu_encoder_surface_path 标记拦住，不误判）。
2. **IOSurface 解码池饿死**（真缺陷）：`read_frame_metal` ring=3，而导出
   生产者流水线同时持有 4-5 个租约 → 间歇性 CPU 帧回落 → bridge 拒收
   整次导出。ring 3→8 + 0.5s 有界重试。
3. **VT NV12 surface 池饿死**（真缺陷）：`VideoToolboxGpuSurfaceProvider`
   ring=4 恰好等于队列深 3+1，稍有迟滞即 track 满员。默认 4→8；合成层
   acquire 增加 0.5s 有界重试（`_acquire_gpu_encode_surface`）。
4. **scipy 死导入**（真缺陷）：无 EQ/LUFS 依赖的导出此前也强制探测
   scipy（冷进程 ~2.8s）。改为仅 `requires_scipy_backend` 为真时探测；
   loudness_target 即使 include_audio=False 仍阻断（RMA-01 既有契约，
   tests/edit/test_audio_export_preflight.py 全绿）。

## 排障记录

- 240 帧跑曾在 frame 96 失败：源素材 2K_topos_proxy.mov 恰 96 帧，
  时间线越界解码所致（配置错误，非产品缺陷）。perf 脚本已钳制源长并用
  多片段拼接。

## 测试

- tests/edit/test_native_gpu_bridge_eligibility.py + test_audio_export_preflight.py：
  36 passed
- tests/media/test_topos_422_surface_kernels.py（Metal kernel 对拍，opt-in）：3 passed
- tests/media/test_cv_metal_bridge.py / test_gpu_frame.py /
  test_topos_surface_capability.py / tests/export/test_export_stage_telemetry.py：
  91 passed

perf.json：同目录（两次 240 帧全程阶段 p50/p95 + blockers + probe）。
