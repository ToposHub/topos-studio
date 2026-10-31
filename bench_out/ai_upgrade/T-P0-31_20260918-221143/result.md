# T-P0-31 批次一：TOPOS 422 R16 → NV12/P010 VT surface 落地

- 执行日期：2026-09-18；环境：macOS x86_64 + AMD Radeon RX 6800（无头 Metal 可用）
- 关联：T-P0-31 卡步骤 1-6 + 验证 1/2/5；X-TEST-10 约束下的诚实标注

## 实现（真实 GPU 证据）

1. **Metal kernel ×2**（`timeline_metal_export_renderer.py` 内联 MSL）：
   - `timeline_fused_export_topos_texture_yuv420_surface`（→NV12）
   - `timeline_fused_export_topos_texture_p010_surface`（→P010）
   - 读三平面 R16Uint 4:2:2，GPU 上写 VT IOSurface Y/UV 纹理（零 CPU 回读）。
   - 采样契约文档化：chroma 输出 (cx,cy) = 四个 luma 位置编码后色度均值；
     BT.709 limited NV12(16-235/128±112)/P010(64-940/512±448)；奇数尺寸
     host 侧拒绝（无隐式奇边丢弃）。
2. **渲染器输出模式**：`_render_topos_metal_texture_frame` 新增
   `(output_yuv420p, output_surface∈{nv12,p010})` 显式分支——不复用 422p10
   CPU buffer 分支；`_resolve_surface_yuv420_textures` 校验 surface 后直写。
3. **资格门翻转**（timeline_export.py）：`topos_422_to_nv12_not_implemented`
   前置拒绝移除；422 源 + nv12/p010 → bridge 可选（其余 surface 形态仍
   fail-closed，`topos_422_surface_format_unsupported`）。
4. **telemetry 诚实标注**：surface 提交路径的 encode 阶段 backend 记
   `videotoolbox`（此前按 FFmpeg codec 名标注 → `encode_backend_cpu:h264`
   假阻断）。decode 仍如实标注非 GPU。

## 过程中发现并修复的真实缺陷

- **bridge 漏传 pixel_format 给压缩会话**（`gpu_export_bridge.py`）：
  P010 surface 喂进 8-bit 默认会话，HEVC Main 冒充 Main10 交付。
- **HEVC 无 ProfileLevel 设置**（`vt_compression.py`）：新增
  `VTSessionSetProperty(ProfileLevel)`——本机 AMD HW 编码器对 Main10 返回
  -12900（kVTPropertyUnsupportedErr），结构化失败交上层回退（遗留见下）。
- **bridge 初始化失败未回退渲染后端**（timeline_export.py）：选择时把
  渲染切到 metal、失败后未还原 → 宿主编码器收到 planar/Metal 帧直接崩。
  现已整体回退（`_gpu_encode_surface_provider=None` + 渲染后端重选）。
- **MetalComputeContext.read_texture**：新增 blit `synchronizeResource` +
  getBytes 的验证/诊断读回 API（独显 Managed 纹理写入对 CPU 不可见；
  此前仓库无从 CPU 读回 kernel 输出的任何设施）。

## 验证结果（28 passed）

1. **kernel 对拍（卡验证 1）**：ramps + color bars，NV12/P010 与逐条对齐的
   CPU 参考实现 max err ≤ 2 码值、PSNR ≥ 50；无 surface 的 420 仍拒绝。
   （tests/media/test_topos_422_surface_kernels.py，TOPOS_METAL_KERNEL_TESTS=1）
2. **集成（卡验证 2 简化口径：8 帧）**：真实 TOPOS Proxy 源 → Metal 422→NV12
   → VT surface → H.264：bridge 选中、导出成功、probe h264/yuv420p/8 帧、
   encode backend=videotoolbox。
3. **HEVC P010 臂（诚实双结局）**：本机 AMD HW 编码器拒绝 Main10 → 结构化
   拒绝（`bridge_init_failed:videotoolbox_hevc_profile_level_rejected:p010:-12900`
   进 telemetry fallback_root_cause）→ CPU FFmpeg hevc 10-bit 回退
   （probe hevc/yuv420p10le/8 帧通过，位深无缩水）。Main10 可用的机器上
   原生 P010 路径自动接管。
4. **资格门/契约回归**：tests/edit/test_native_gpu_bridge_eligibility.py
   25 项全过（含 422→h264/hevc 资格翻转、非 420 surface 仍拒绝、bridge
   选择遥测）。
5. **预存失败（非本批引入，干净树复现）**：
   test_macos_vt_compression.py 的 multi_frame/multitrack 3 项（注入 bridge
   fixture 与 pytest GPU 禁用策略冲突）；tests/composite 全目录联跑段错误。

## 遗留（记入 T-P0-31 后续）

- 卡验证 2 的 48 帧口径 + 卡验证 3 的 CPU/GPU PSNR diff 图门槛 + 卡验证 4
  的 identity 2K ≥60fps 性能数字：需在 Main10 可用硬件上补 P010 臂；
  H264/NV12 臂可先行。
- 本机 AMD VT HEVC Main10 拒绝（-12900）：硬件事实，如实标注；
  VT 软件编码器 Main10 的 mux 链路 EINVAL 为独立遗留缺陷（复现路径已记录）。
- **桌面验证边界**：本批次证据来自无头 Metal；桌面窗口语义仍受
  X-TEST-10 约束（BLOCKED: DESKTOP_GPU_NOT_VERIFIED）。
