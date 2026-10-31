# Phase 1 probe 总报告（P1-06）——K1/K3/K4 门禁判定

> 汇总日期：2026-09-11　git `10c1456f`（chore/edit）
> 机器：MacPro7,1 (Hackintosh) / i9-9900K / **AMD Radeon RX 6800**（Metal 与 GL 两处探针均报 6800；
> 计划 §0.5 记录的 6900 XT 为更早时点的设备名，黑苹果设备 ID 报告不一致，以本次为准）
> OS：macOS 15.3.1 (24D70) x86_64　显示 3840×2160@60Hz

## 证据归档（全部同 HEAD 产出）

| Probe | 归档 | 覆盖工作项 |
|---|---|---|
| Metal 能力 | `tools/probe_metal_capability.py`（输出见提交 9a1c7a93 记录 + 本次 422 矩阵） | P1-01 / P1-02 / P1-04 |
| render-only A/B | `tests/media/results/backend_compare_render_ab.json/.md` | P1-05 / K1 |
| Qt 共存 | `tests/media/results/qt_metal_coexistence.json/.md` | P1-03 / K3 |
| 画质金标 | `tests/media/results/quality_reference/` | Phase 0 P0-05（门禁§6.4 正确性用） |
| OpenGL 基线 | `tests/media/results/p0_opengl_baseline*.json/.md` | Phase 0 P0-04 |

## P1 各项结论

| 项 | 结论 |
|---|---|
| P1-01 device/queue/pipeline/dispatch | ✅ 全部可用（Mac2/Common3 ✓，argumentBuffers Tier 1，maxBuffer 3.8GB） |
| P1-02 格式矩阵 | ✅ NV12 / P010 / BGRA 直通；**2vuy(8bit 422) 原生 GBGR422/BGRG422**；**v210(10bit 422) 仅能 8-bit 重解释导入——Metal 无原生 10-bit 422 纹理格式，ProRes 10-bit 零拷贝无直通路径**（R32Uint/RGBA16Uint 候选因 IOSurface 步长不足触发硬断言，无法进程内探测） |
| P1-03 Qt 共存 | ✅ 同窗共存可行（见下 K3） |
| P1-04 FFI 边界 | ✅ ctypes rc=0 / PyObjC -6661（前次已实测，本次复跑一致）；决策不变：CoreVideo 全线走 ctypes |
| P1-05 render A/B | ✅ **Metal +81~85%**（四组合一致，见下 K1） |

## K 门禁判定（计划 §1.4）

### K1：Metal render-only 加速 <15% → 中止

**判定：不触发（PASS）。** 四组合（1920×1080/3840×2160 × bgra8/rgba16f）加速 +81.5% ~ +84.9%：

| 尺寸×格式 | OpenGL ms/f | Metal ms/f | 加速 |
|---|---:|---:|---:|
| 1080p bgra8 | 0.256 | 0.044 | +83.0% |
| 1080p rgba16f | 0.312 | 0.048 | +84.6% |
| 4K bgra8 | 0.177 | +81.5% |
| 4K rgba16f | 1.305 | 0.196 | +84.9% |

负载：3×3 高斯（9 采样）→ USM → lift/gamma/gain → 饱和度；GPU-resident 输入；
Metal 计时 = commandBuffer GPU 时间；GL 计时 = GL_TIME_ELAPSED。
**含义：macOS GL 4.1 兼容层的真实代价约为原生 Metal 的 5~6 倍 GPU 时间——
Phase 6（调色链路迁移）不再只是架构收益。**

### K3：共存方案需要改动 QOpenGLWidget 语义 / 5 个 viewport 分叉 → 中止

**判定：不触发（PASS），附带一条窗口级重叠约束。**

- 并列共存：QOpenGLWidget（AA_ShareOpenGLContexts 组内）与 CAMetalLayer NSView
  同窗各自正确呈现（左绿右红，像素级验证）。**现有 5 个 viewport 类无需分叉即可与 Metal 表面共存。**
- 窗口级重叠：挂到 contentView 的 CAMetalLayer **覆盖** QOpenGLWidget 内容
  （Metal 赢）。含义：Metal 视口之上的浮层/标注 UI 必须原生级处理，Qt 控件不会自动穿透。
- resize：手动 re-frame 后 Metal 正确重绘（Qt 原生子窗口几何需手动同步——Phase 10 工作项）。
- present 节奏：60 帧实测 62.0 fps @ 60Hz → vsync 锁定正常。
- **未测**：EDR/HDR 色彩空间下的共存行为（`wantsExtendedDynamicRangeContent`）——
  留给 P10-05，不阻塞 K3。

### K4：打包/签名/公证在目标分发渠道不可行 → 中止

**判定：条件性通过（PASS with condition），不触发中止。**

| 分发要素 | 现状 |
|---|---|
| 运行时 MSL 源码编译（`newLibraryWithSource`） | ✅ 探针全程使用，**分发无需预编译 metallib 即可工作**（§9.1 的"随包发布源码"路线） |
| 离线 `metal`/`.metallib` 工具链 | ❌ 本机无 Xcode（仅 CommandLineTools）——选预编译路线必须给构建机装 Xcode |
| `codesign` | ✅ 存在，但 **0 个有效签名身份**（无 Developer ID 证书） |
| 公证 notarization | ❌ 需 Developer ID + Apple 开发者账号，当前不可用 |

**条件**：若走"纯 Python + PyObjC + 运行时 MSL 编译"路线，现状即可分发；
若走计划 P1 绑定方案推荐的 **Obj-C++/Swift 原生扩展**路线（热路径性能理由），
则二进制签名 + 公证成为硬前提。**此矛盾需在 Phase 4 立项前复核**
（建议：Phase 4 先以 PyObjC + ctypes 起步验证收益，原生扩展作为打包阶段的独立决策）。

## 对计划主线的更新

1. §12 执行顺序第 3 步完成：K1/K3/K4 全部不触发，**Metal 主线继续**。
2. §13 开放问题 #1（422 格式边界）✅ 已回答（阴性但决定性）；#2（Qt 共存）✅ 已回答；#5（render-only 差异）✅ 已回答（+81~85%）。
3. 新增已知约束：ProRes 10-bit 422 无 CVMetalTextureCache 直通 → Phase 5 的 ProRes 部分维持
   条件化判定（本机亦无 ProRes 硬解，双重不可行）；NV12/P010 路径不受影响。
4. P1-06 之后的 Phase 2（契约冻结）可以启动；§0.2 目标平台决策仍需用户确认（A′ 组合推荐），
   该决策阻塞 Phase 5+，不阻塞 Phase 2-4。
