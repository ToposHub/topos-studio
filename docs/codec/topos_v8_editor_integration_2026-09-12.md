# V8 编辑器集成评估（产品化，单列工作流）

日期：2026-09-12 · 依据：计划 §3 批 5 单列项 + 批 4 实测 + src/ 挂载点调研
性质：评估记录；2026-09-15 已补充 Topos 422 Metal 直通实现、真机导出结果与导出后审计哈希异步化。

---

## 一、现状基线（src/ 生产路径实测调研）

| 环节 | 现状 | 位置 |
|---|---|---|
| 解码后端 | `ToposMediaSource` → `ToposDecoder.decode_views` 直写池化 numpy `<u2` 平面（CPU），模块头明文"不触碰 OpenGL/Qt" | `src/shared/media/topos_source.py:21,437` |
| GPU 上传 | 普通 Topos/GL 路径仍由 `YUVGPUUploader` 完成 numpy → GL R16UI；macOS master 422 由 IOSurface→Metal 直通路径绕过该上传 | `src/shared/video/yuv_gpu_upload.py:619`、`src/shared/media/topos_source.py` |
| V8 GPU 解码链 | export→段解码→wsd_v8 全链路位精确（批 4），但 **src/ 零调用**——唯一消费者是差分测试 | `kernels_dec.py:443`、`kernels_idct.py:1096` |
| GPU 导出面 | `tc_dev_v8_gpu_export`（smeta 14×u32/段 + 瓦片表）native 已有，Python 绑定**未封装**（仅测试裸调 ctypes） | `codec.h:383`、`topos_binding.py` |
| 显存映射 | macOS master 422 已接入：`ToposMediaSource.read_frame_metal()` 从 `ToposIOSurfacePool` 取环槽，native decoder 直写三平面，Metal 导入三个 `R16Uint` 纹理；导出端不再经过 NumPy/GL 上传 | `src/shared/media/topos_source.py`、`src/shared/media/macos_vt/topos_surface.py`、`src/features/edit/core/timeline_metal_export_renderer.py` |
| packet 侧 | `packet_into`/`packets_into_batch` 已零拷贝（复用缓冲 + pread arena） | `topos_binding.py:1462,1560` |

## 二、三项产品化工作与判定

### 2.1 输出直留 GPU 纹理（macOS master 422 已完成首个垂直切片）

- **语义**：解码产物不落 CPU numpy——kernel 直写 MTLTexture（统一内存）或
  经 IOSurface 双侧导入（dGPU：Metal kernel 写 IOSurface-backed texture，
  GL 侧 `CGLTexImageIOSurface2D` 导入渲染；桥的 GL 半侧已有 `iosurface_gl_bridge.py`）。
- **已完成（导出路径）**：Topos native 解码仍由 CPU 执行，但输出地址是
  IOSurface 映射平面；Metal 直接导入 Y/U/V 三个 `R16Uint` 纹理。时间线
  Metal 对三平面做采样、色彩矩阵/范围、调色、缩放、效果及最终
  `yuv422p10le` 打包，CPU 只把最终平面提交给 Topos 编码器。
- **当前边界**：这不是 GPU 解码器；压缩码流的熵解码/重建仍在 native CPU。
  代理缩放、V9 帧间流、alpha 和交互式 GL 预览保留原有 CPU/GL 路径。
- **真机结果（AMD Radeon RX 6900 XT）**：`tests/test_videos/2K_topos.mov`
  1920×1080→1280×720，480 帧无错误，Metal 直接纹理帧 480/480，最终
  Topos 422 输出 480/480。初次结果为 10.572 s / 45.404 FPS，但拆分后发现导出完成前
  的交付 manifest 会同步扫描 2.7 GB 源文件做 SHA-256，单独约占 5.5 s，掩盖了
  Metal 收益。现在先写入 metadata manifest，再在后台原子替换完整哈希版本；同一素材
  最新完整导出为 5.116 s，即 **93.819 FPS**（三轮 90.457–93.819 FPS）。Metal
  直接纹理帧与 Topos 输出仍为 480/480，renderer 0 errors；单帧 Metal 输出与 CPU
  上传参考逐平面最大差值为 0，完整 MOV 字节级相同。

### 2.1.1 Native 编解码与项目导出的真实素材对比

可复现基准脚本为 `bench_out/codec_bench/compare_topos_direct_vs_project.py`，最新结果保存在
`tests/media/results/topos_codec_vs_project_20260915.json`。解码使用真实
`tests/test_videos/2K_topos.mov`，编码使用项目刚刚生成的真实 1280×720 Topos MOV，未使用合成帧。

| 路径 | 实测吞吐 | 口径 |
|---|---:|---|
| Topos native 解码 | **221.95 FPS** | packet I/O + `decode_views` 写入复用平面（111 帧计时） |
| 项目解码阶段 | **194.06 FPS** | `ToposMediaSource` + IOSurface lease/导入（480 帧） |
| Topos native 编码 | **1,297.56 FPS** | `encode_frame`，代表性固定 QP 89，1280×720 真实帧 |
| `ToposVideoEncoder` 固定 QP | **1,195.75 FPS** | Python 校验 + MOV 封装，不含 RGB 转换 |
| 项目编码阶段 | **734.58 FPS** | 可变 QP 反馈 + 帧交付 + 封装（480 帧） |
| Metal 时间线完整导出 | **93.819 FPS** | 解码 + Metal 渲染 + 调度 + 编码 + 封装/收尾 |

裸 native 与项目阶段的比值为：解码 1.14×、编码 1.77×；只计入应用层封装与 MOV 封装时为项目编码阶段的
1.63×。剩余的端到端差距主要在帧调度、IOSurface/Metal 同步、GPU→CPU 最终平面交付，以及码率控制和
封装，而不是 Topos 熵编码核心。同一轮还测得首帧 `encode_sized` 码率搜索为 499.47 FPS，因此保留
首帧之后的单遍反馈路径仍是高价值优化点。
- 后续工作集中在 scaled IOSurface pool、V9 外部 surface 和 native
  GPU decoder；不应把当前 `zero_copy` 标签解释成 GPU 熵解码。

### 2.2 上载 ring buffer 摊销

- **语义**：GL 上传缓冲预分配 N 帧环形复用，消除逐帧分配。批 1 实测：
  分配开销 17~21ms/帧 → 预建后 ~6ms（计划 §3 引）。
- **判定：可独立先行，V7/V8 共同受益**。落点在 `YUVGPUUploader`（CPU 解码
  路径的上传端），与 V8 解码无耦合；V8 激活后同一 ring 继续服务。
  **是三项中唯一收益确定、可立即立项的项**。
- 工作量估计：ring 化上传缓冲 + 帧序号防回绕 ~3~5 天（含 AB 对比测试）。

### 2.3 CRC 与上载重叠

- **语义**：V8 两阶段解码的阶段 1（瓦片 CRC 重算）与 packet payload 上载
  （DMA）流水线重叠， hiding 上载墙。
- **判定：与 2.1 同前置**。dGPU 的 PCIe 上载墙是批 4 发现的瓶颈组成部分
  （流 130MB+ 工作集超缓存，8K 额外劣化到 12.5×）；统一内存机器天然免疫。
  仅在 GPU 路径激活后有工程意义。

## 三、立项建议（评审输入）

1. **2.2 立即可做**：收益有实测、路径独立、V7/V8 通吃——建议随下一个
   性能窗口执行。
2. **2.1/2.3 进入持续优化阶段**：首个垂直切片已落地，下一步是 scaled
   surface、V9/alpha 覆盖以及 native GPU decoder 评估；不支持的格式继续
   显式回退 CPU/GL，保持产品可用性。
3. 拍板顺序建议：先 `topos_v8_production_switch_2026-09-12.md` 的
   生产默认拍板 → 依其结论决定 GPU 产品化窗口的时点。
