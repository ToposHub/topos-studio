# Windows 帧预算与拷贝台账报告（计划表 B · B0.3）

> 依据：`docs/perf_plan_B_windows_2026-10-22.md` §B0.3（只读盘点，不改生产代码）。
> 环境：机器 B（i9-13900K / RTX 4090 驱动 621.01 / PyAV 18.1.0 / Win11 26200），
> 实录见 `docs/perf_env_matrix_windows_2026-10.md`；解码基线见
> `scripts/BENCH_HW_DECODE_WINDOWS.md`（B0.2 重锚，commit `e650fdccd` 时点）。
> 证据等级：**L0** 静态声明/协议 · **L1** mock 逻辑测试 · **L2** 本机真实编译/执行 ·
> **L3** 生产播放/导出端到端。行号为盘点时点引用。

## 1. 执行摘要

1. **当前生产 Windows 路线没有任何 GPU 驻留帧通路**：硬解分支（`WindowsHwPlanarSource`）
   每帧 PyAV 隐式 GPU→CPU 下载后再 CPU→GPU 上传回渲染；D3D11 全 GPU 导出链在
   selector 之后被显式拒绝（fail-closed），从未在生产跑通。
2. **`topos_hwdecode_windows` 原生扩展是整条 D3D11 链的唯一解锁点，且本机不存在**
   （B0.1 实查，site-packages 无残留）。decoder/render/encode 三段的"缺实现"全部
   收敛到这一缺失 + interop factory 缺失两点上。
3. 解码侧重锚（B0.2）：H264 8bit CPU planar 仍 ≈5.5× 占优（4K 841 vs 152fps），
   **但 H265 10bit prod_hw_planar 已反超 CPU（2K 621 vs 391 / 4K 133 vs 120）**——
   「下载税」是否是主瓶颈因 codec/位深而异，B1 PoC 必须分格式立项验证。
4. 生产在用的 windows_hw 模块只有 4 个（hw_planar_source / gpu_capability /
   reasons / d3d11_device+d3d11_export_capability 经导出 probe 链）；其余 17 个模块
   是无生产调用方的骨架/契约层（详 §2），其中 d3d11_sync 是测试替身性质。

## 2. 逐模块台账（src/shared/media/windows_hw/，22 项）

| 模块 | 真实生产调用方 | 完整度 | 证据 |
|---|---|---|---|
| `hw_planar_source.py` | `edit_gpu_decode_manager.py:2856`、`factory.py:1239` | **真实实现（生产在用）**；`HWAccel(HWDeviceType[...])` 无 `is_hw_owned`（hw_planar_source.py:185）→ 每帧自动下载 | L1+L2/L3（真机验证报告归档于 tests/media/results/） |
| `gpu_capability.py` | `shared/gpu/capabilities.py:246`、`media/hwaccel.py:542`、`export/video_encoder.py:254` 等 | 真实（DXGI 枚举/NVML 驱动/LUID 匹配） | L1+L2 |
| `reasons.py` | `export/encoder.py:358` 等多条诊断链 | 真实（reason 词表） | L1 |
| `d3d11_device.py` | 经 `d3d11_export_capability.py:141` ← `timeline_export.py:8492,8513`（Windows 导出后端 probe 时真实执行） | 真实（ctypes D3D11CreateDevice/vtable/LUID） | **L2**（本机 12.1/4090，B0.1 实录） |
| `d3d11_export_capability.py` | `timeline_export.py:8492-8518` | 真实 gate，但结论恒 fail-closed（bridge 缺失→`ready=False`→生产回退 opengl，timeline_export.py:8508,8522） | L1 |
| `native_bridge.py` | `d3d11_decoder.py:180`、`d3d11_export_capability.py:170` | 加载器真实；**默认目标 `topos_hwdecode_windows` 本机不存在** → `not_yet_implemented:native_bridge:*`（native_bridge.py:178-185） | L1 |
| `capability.py` | `topos_surface_capability.py:88`、`media/__init__.py:289` | 真实（WGL_NV_DX_interop 探测 :68-100；负结果不缓存 :160-168） | L1（真机探测路径存在，zero_copy 结论受下游限制恒 False） |
| `d3d11_decoder.py` | 无（仅 probe/tests/scripts） | 类型化骨架；bridge 缺席时 `open()` False + `not_yet_implemented`（:179-190） | L1（FakeBridge） |
| `d3d11_texture_pool.py` | 仅 probe 链 | 真实（ctypes CreateTexture2D :439-473、lease 回收） | L1+L2 |
| `gpu_encode_surface.py` | 无（passthrough_chain 内部） | 部分：`submit_to_encoder()` bridge 缺席即 `(False,"not_yet_implemented")`（:263-264） | L1+L2 |
| `windows_zero_copy_probe.py` | 无（tests/scripts） | 分层 probe：device/格式/池真实；decode surface 层 fail-closed（:151-168） | L1+L2 |
| `d3d11_passthrough_chain.py` | 无（tests） | T0 编排逻辑真实，两端依赖注入 | L1 |
| `d3d11_pipeline.py` | 无 | 调度器真实（不触像素） | L1 |
| `d3d11_render_graph.py` | 无 | plan 编译真实；`D3D11TimelineRenderer` 无 backend 即 `not_yet_implemented`（:269-271） | L1 |
| `d3d11_effects.py` | 无 | **纯声明**（8 效果 spec；runtime_gate 自述需 native renderer） | L1 |
| `d3d11_shaders.py` | 无 | **仅资源声明**（struct+寄存器绑定，无 kernel 函数体） | L0 |
| `d3d11_gl_bridge.py` | 无 | 骨架：`interop_factory is None` → False + `interop_not_yet_implemented`（:44-49）；全仓无注入点 | L1 |
| `d3d11_frame.py` | 无 | 契约 dataclass（frozen/LUID/lease 恰一次） | L1 |
| `d3d11_format_contract.py` | 无 | 策略层真实（格式/LUID 校验） | L1 |
| `vendor_policy.py` | 无 | 策略函数真实，无生产供给方 | L1 |
| `d3d11_sync.py` | 无 | **测试替身**（threading.Event 版 fence，非真实 D3D11 fence） | L1 |
| `__init__.py` | `media/__init__.py:289` | re-export | L0 |

## 3. 计划 §0 事实表逐条核实（A–J）

- **A. 生产硬解路由**：`edit_gpu_decode_manager.py:2845-2854`——win32 + codec∈{h264,h265,hevc}
  + auto 策略 + 未禁用 +（强制 env 或 **nvcuda.dll 存在**，`hwaccel.py:452-470`）；
  factory 侧镜像门：非 NVIDIA 的 d3d11va/qsv 未显式指定 → `win_non_nvidia_cpu_decode_preferred`
  （factory.py:1210-1222）。**全仓无任何 `is_hw_owned` 实际调用**（仅注释
  edit_gpu_decode_manager.py:2840）——"18.1.0 支持"是库能力，不是本仓已用。
- **B. D3D11GLBridge**：无 factory 即不可用（§2）；真实 WGL interop 代码只存在于
  `scripts/probe_wgl_shared_texture_roundtrip.py`、`scripts/probe_wgl_interop_windows.py`，
  未接回生产类。
- **C. `_wrap_external_gpu_planes()`**（edit_gpu_preview_renderer.py:1848-1899）：只包装
  已存在的 GL texture ID（`texture_id==0` 即拒 :1872-1874），不创建资源；消费点
  :2101-2119（零拷贝路径）与 :2170（planar 上传）。
- **D. D3D11 导出集成**：selector 真实存在（timeline_export.py:8475-8522），composite
  显式拒绝 `not_yet_implemented:d3d11_timeline_composite_integration`（:7804-7813，
  注释明确"绝不把 D3D11 任务偷跑 OpenGL 合成再冒充"）。
- **E. DeckLink**：`submit_gpu_frame()` 不可调用/异常即 False（monitor_output.py:1275-1292）；
  `_supports_gpu_texture` 默认 False（:1040-1042）；生产消费方据此保留 CPU 回读
  （file_controller.py:3905-3912 `readback_enabled = not native_gpu_enabled`）。
- **F. 原生桥接 ABI**：期望扩展实现 `capabilities()/open_decoder/decode_next/close_decoder/
  submit_encode`（Protocol，native_bridge.py:122-147），`api_version==1`；异步编码提交
  必须带 completion_fence。**未随仓库分发，未在任何机器证实存在**——与计划预判一致。
- **G. 后端枚举**：`GraphicsBackendKind.D3D11/D3D12` 已有（backend_types.py:34-35）；
  graphics auto 偏好链只有 OPENGL/METAL（backend_selector.py:36-39），d3d11 只能显式
  请求且平台能力 reason=`not_yet_implemented`。
- **H. shader→执行缺口**：无 D3DCompile 绑定、无 `D3D11RenderBackend` 真实现、无
  constant-buffer packing 代码；「注册表键/资源声明 ≠ 可编译 kernel ≠ GPU 执行」。
- **I. passthrough_chain/zero_copy_probe**：T0 零回读直通链编排器与分层 probe，均无
  生产调用方（能力 UI 走 capability.py）。
- **J. 当前生产帧路径**：见 §4。

## 4. 每路径字节流（拷贝点与每帧字节）

预览（播放）与导出的现有路径及系统内存/总线流量（4K=3840×2160；NV12 8bit ≈
12.44 MB/帧，P010 10bit ≈ 24.89 MB/帧）：

| # | 环节 | 位置 | CPU planar | HW download planar |
|---|---|---|---|---|
| 1 | 解码 | FFmpegMediaSource / WindowsHwPlanarSource+NVDEC | 软解（CPU 核占用） | GPU 硬解 |
| 2 | GPU→CPU 下载 | PyAV HWAccel 隐式 `av_hwframe_transfer_data`（hw_planar_source.py:185） | — | **12.4/24.9 MB/帧** |
| 3 | stride→紧凑平面拷贝 | `_extract_visible_plane_data`（hw_planar_source.py:316-329 → ffmpeg_source.py:2916-2960） | 12.4/24.9 MB/帧（读+写） | 12.4/24.9 MB/帧（读+写） |
| 4 | CPU→GPU 上传 | `upload_nv12/p010`（edit_gpu_preview_renderer.py:2170 → yuv_gpu_upload.py:1016-1136） | 12.4/24.9 MB/帧 | 12.4/24.9 MB/帧 |
| 5 | 合成/调色 | GL FBO 链 | GPU 内部 | GPU 内部 |
| 6a | 预览呈现 | `GpuPresentFrame`（edit_gpu_preview_renderer.py:5048-5100） | **0 回读**（布防时仅 1/4 分辨率快照） | 同左 |
| 6b | 导出回读 | `_scale_and_read*`（timeline_gpu_export_renderer.py:2059/2112） | YUV420 回读 ≈12.4 MB/帧 | （导出当前不走 HW 解码） |

- **HW planar 相对 CPU planar 的额外成本 = 环节 2（下载）+ 环节 3 翻倍**；
  B0.2 实测 4K H264 上该税 ≈5.5×（841→152fps），10bit H265 上被软解成本盖过。
- 导出另有解码（纯 CPU）+ 上传 + 回读全程，`_try_render_planar_passthrough`
  （timeline_export.py:6181-6184）是唯一免 YUV↔RGB 往返的既有优化。
- 零拷贝理想态（B1 目标）：环节 2/3/4 全免 → 每帧省 ≈37–75 MB 总线+内存流量。

## 5. 支持矩阵（profile/位深/色度/尺寸 × 设备，机器 B 实测）

**D3D11 格式能力**（B0.1 实录，4090 / FL 12.1）：NV12 ✅ P010 ✅ BGRA ✅
（RX 6800 历史上 P010 手动分配 E_FAIL，见 d3d11_device.py:322-326 注释——跨机不可互推）。

**解码路径矩阵**（B0.2 重锚，steady fps，"—"=生产路由本就不选硬解）：

| 素材 | profile/位深/色度 | CPU planar | HW planar | 生产选择 |
|---|---|---|---|---|
| 2K/4K H264 | 8bit 4:2:0 | 2949 / 841 | 521 / 152 | NVIDIA 机选 HW（路由如此），但 CPU 更快——**B1 立项主靶** |
| 2K/4K H265 | 10bit 4:2:0 (P010) | 391 / 120 | **621 / 133** | HW 已有净收益 |
| ProRes 422HQ/4444 | 10/12bit 4:2:2/4:4:4 | 505–96 / — | — | 恒 CPU（Intra 剪辑码流无硬解） |
| DNxHR HQX 12bit | 12bit 4:2:2 | 637 / 193 | — | 恒 CPU |
| Topos 自有码流 | 厂商私有 | `check_topos_codec_available()` 强校验后可测（`--include-topos`） | — | 与 GPU 路线解耦（av.libs 无自有库，B0.1） |

尺寸：本批 2K/4K 全部 native 无缩放（`scaled=None`，B0.2 JSON 逐 mode 可查）。
设备：以上仅 4090；Intel UHD 770 与 RX 6800 待 B2.2 三机矩阵，不从本表外推。

## 6. 缺失实现 / 待验证项（进入 B1/B2 的输入）

1. `topos_hwdecode_windows` 原生扩展（decoder/encode 提交/export gate ready/T0 链的唯一解锁点）。
2. D3D11GLBridge 的 WGL_NV_DX_interop factory（真实走查仅在 probe 脚本）。
3. HLSL 编译与设备执行链（D3DCompile/RenderBackend/常量 packing 全缺）。
4. timeline_export D3D11 composite 接线（selector→composite 整段）。
5. `is_hw_owned=True` 保留帧行为的真机验证（B1.1 候选 2 主线；cuda/d3d11va 帧的消费接口）。
6. 4090 上 P010 手动分配行为（AMD E_FAIL 前科，未测）。
7. 真实 D3D11 fence（现仅有 Event 替身）。
8. T0 evidence 生产来源（`D3D11T0Evidence` 目前只能由未来真实运行填充）。
9. 零拷贝能力 gate WGL 负结果不缓存的时序敏感性（capability.py:160-168，测量时注意）。
10. 测试缺口：pipeline/vendor_policy/format_contract 仅 L1；render graph L2 待 B3.2；
    `test_windows_hw_zero_copy.py` 自称骨架。

## 7. 瓶颈假设（B1 进入条件 · 计划 §4-2 冻结用）

**主指标**：同机成对的「解码到渲染完成」延迟与稳态 fps（A v3 §2 契约，Windows 侧
复用 schema v3 遥测，B2 接线时落地）；decode-only 吞吐仅作辅助。

- **H1（下载税主导 H264 硬解路径）**：4K H264 HW planar 152fps（6.6ms/帧）中，
  环节 2–4 的 37MB 流量预计占大头——B1.1 用保留帧 PoC 分离"纯解码"与"下载+上传"。
- **H2（10bit HEVC 软解 CPU 占用才是大头）**：CPU planar 120fps@4K H265 已贴 8ms/帧，
  硬解反超说明 decode 计算成本 > 拷贝税；该格式零拷贝收益边际更小。
- **H3（多轨播放瓶颈在 CPU 解码核竞争而非单轨 decode fps）**：单轨 CPU 841fps@4K H264
  不可线性外推 N 轨（32 逻辑核 / 每轨 frame-thread 解码器争核 + 环节 3 拷贝带宽）；
  待 B2 用多轨矩阵验证（A0 契约的掉帧/预算指标）。
- **H4（呈现/监看无 CPU 回读，不构成当前瓶颈）**：预览呈现 0 回读（6a），
  监看 CPU 回读路径仅在 DeckLink GPU 直送关闭时启用（§3-E）——与 Mac 线
  A1.2 发现的监看链成本不同构，Windows 侧暂无证据将其列为瓶颈。
- 明确**不在**本假设内：D3D11 合成/导出链（未接线，无生产数据，B3/B4 另立项）。

## 8. 关联

- 环境实录：`docs/perf_env_matrix_windows_2026-10.md`；基线数据：
  `scripts/BENCH_HW_DECODE_WINDOWS.md` + `scripts/bench_hw_decode_results_B0_20261024_run0*.json`。
- 测试农场首跑与 skip 审计（C0.1/C0.2）见 `docs/test_farm_first_run_windows_2026-10.md`、
  `docs/test_skip_audit_windows_2026-10.md`。
- Mac 线对照：`docs/perf_frame_budget_report_mac_2026-10.md`（A0.3）。
