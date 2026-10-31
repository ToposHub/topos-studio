# T-P1-21 执行报告：Edit 内置效果按成本迁移和优化（阶段一：GPU 链解锁）

- 执行日期：2026-09-18
- 分支：chore/edit
- 状态：**IN_PROGRESS**——子项 1-4 全部达最低门槛；子项 5/6 时间门槛达成，
  但质量档（下采样/ROI）与逐素材 CPU/GPU 对拍协议未完成。

## 根因与修复

### 修复 1（结构性）：GPU 导出分派门控缺失
`_gpu_render_composite` 不分后端把 `output_yuv422p10=True` 传给渲染器，
而 GL 渲染器三个入口（render_planar/render_packed_bgr/render_layer_stack）
签名没有该参数——macOS auto→opengl 下每次 TOPOS 10-bit 导出的 GPU 尝试
每帧 TypeError → 整帧回落 CPU float 管线。实测基线：forced_gpu 与
cpu_render 完全同速（grade 1.38/1.40fps）——GPU 链从未生效。

修复：提取 `_gpu_render_output_kwargs()` 单点门控（仅 Metal 携带该参数；
GL 走 BGR u16 回读 + encoder sws），附签名契约测试防止回潮。

### 修复 2：效果 GLSL 静态 kernel 循环
blur separable（H/V）、glow（H/V）、defocus 的 kernel 循环用静态
`for (int i = -MAX_KERNEL; ...)` + 逐迭代 `continue` 守卫：AMD 上静态
循环不因 continue 跳过成本。blur radius 12 = 129 次迭代仅 25 次有效；
glow/defocus MAX_KERNEL=128 = 257 次。改动态循环边界（GL 3.30+，采样集
逐点相同，语义不变）。

### 修复 3：深度（u16）效果链桥
`_prepare_layer_texture` 的 deep_u16+effects 分支此前直接
`return None`（fail-closed 回落 CPU）——TOPOS 10-bit 导出的全部效果被
排除在 GPU 外（EPR scratch 只有 RGBA8/16F）。新增 u16→f2→EPR→f2→u16
全 GPU 桥（`_FRAG_U16_TO_F2_SRC`/`_FRAG_F2_TO_U16_SRC`，half-even 量化
与 u16 管线一致；失败仍 fail-closed）。

## 锚点结果（48 帧 + 8 预热，2K TOPOS 源，topos_proxy 输出）

| 场景 | 基线 forced_gpu | 修复后 forced_gpu | 提升 | 门槛 |
|---|---:|---:|---:|---|
| identity | 87.12 | 86.54 | ≈持平（直通路径未受影响） | ≥60 ✓ |
| grade | 1.38 | **33.47** | 24.3× | ≥30 ✓ |
| blur | 4.67 | **32.93** | 7.1× | ≥30 ✓ |
| sharpen | 3.25 | **33.80** | 10.4× | ≥30 ✓ |
| glow | 3.53 | **32.46** | 9.2× | ≥30 ✓ |
| two_layer | 2.44 | **29.22** | 12.0× | ≥25 ✓ |
| transition | 3.72 | **30.73** | 8.3× | ≥25 ✓ |

TOPOS identity fast path：83–92fps（≥80 门槛 ✓，T-P1-11 未回退）。

子项 5/6（8 帧冷启动含 GL init，topos_proxy + GPU）：
- denoise：0.44s（卡门槛"8 帧≤30s"——达标；基线 >120s）
- defocus：0.47s（卡基线 15.6s → 33×）
- edge_detect：0.48s；erode_dilate：2.55s（首帧 GL init 占比高）

## CPU/GPU 对拍（grade/blur，真实导出后解码对拍）

- grade 10-bit TOPOS：均值 |Δ|=0.0000/1023，max|Δ|=1 LSB，std 完全一致。
- blur 10-bit TOPOS：均值 |Δ|=0.0032/1023，std 167.0 vs 160.5（+4%），
  边缘 max|Δ|=465——**已知质量差异**：GL gaussian sigma=max(r×0.5,0.1)
  与 CPU cv2 约定（0.3(r'−1)+0.8）不同，GPU 模糊更弥散。该约定同时影响
  预览与 8-bit 导出（历史行为）；对齐属视觉变更，记录为 defect 待
  产品裁决（X-TEST-40 协议须覆盖）。
- 8-bit H.264 blur：均值 |Δ|=0.50/255，std Δ1.6%。

## 测试

- 新增 tests/edit/test_gpu_render_dispatch_gating.py（9 项）：kwargs
  门控 ×4、GL 签名契约 ×1、shader 静态循环扫描 ×2。
- 回归：GPU/Metal/单层快路径/能力契约套件 177 通过、2 失败——失败为
  test_real_metal_sobel_fractional_preblur_matches_cpu_export，干净 HEAD
  复现（预先存在，与本批无关）。

## 遗留（后续批次）

1. 子项 5/6 的 quality tier（blur/defocus 下采样质量档、denoise 质量档位）。
2. 逐效果多素材对拍协议（平坦/边缘/高频/HDR/alpha）——defocus/edge/
   erode/denoise 未逐项执行。
3. 参数动画、撤销、保存重载、导出取消的回归扩展。
4. GPU blur sigma 与 CPU cv2 约定对齐（产品裁决后）。
5. Metal 后端在 macOS 的 auto 选择评估（当前 auto→opengl；Metal 支持
   yuv422p10 原生直出可省 sws）。
