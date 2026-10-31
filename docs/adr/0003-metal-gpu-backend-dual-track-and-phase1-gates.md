# ADR 0003 — Metal 与跨平台 GPU 后端：双轨架构 + 四维接口 + Phase 1 门禁判定

- **Status:** Accepted（Phase 1 探针完成，K1/K3/K4 全部不触发；Phase 2 契约冻结启动）
- **Date:** 2026-09-11
- **Plan ref:** `docs/metal_backend_migration_and_cross_platform_gpu_plan.md` v2（§1.1、§1.2、§1.4、§5 Phase 1、§9.4）
- **Supersedes:** 无（新决策；与 `docs/4k-playback-optimization.md:413/:614` 的关系见该计划 §1.3）
- **Implementation:** `tools/probe_metal_capability.py`、`tools/bench_render_ab_metal_gl.py`、`tools/probe_qt_metal_coexistence.py`；证据归档 `tests/media/results/{backend_compare_render_ab,qt_metal_coexistence,phase1_probe_report,quality_reference/}.*`
- **复核触发:** Phase 4 立项前（K4 的原生扩展路线矛盾）、Phase 5 启动前（§0.2 平台决策）

## Context

macOS OpenGL 停留在 4.1（无 compute shader，兼容层随 macOS 27 终止于 Intel），
项目已有 227 处 `.glo` 交接与 `AA_ShareOpenGLContexts` 承重结构。迁移计划 v2
要求：probe 先行、契约后冻、保留 OpenGL 永久双轨。Phase 1 的全部探针在
MacPro7,1 / i9-9900K / AMD RX 6800 / macOS 15.3.1 上完成（2026-09-11）。

## Decision

1. **采用"共享上层语义 + 多后端 + 四维能力接口"**（graphics / compute / decode /
   interop 四个独立维度，不压回单一 backend 枚举）。
2. **OpenGL/ModernGL 永久保留**为跨平台基线、回退与画质参考实现；Metal 为渐进接入的第二实现。
3. **CoreVideo 互操作全线走 ctypes**，不在解码回调边界混用 PyObjC（实测 PyObjC 返回 -6661，ctypes 返回 0）。
4. **K1/K3/K4 判定（本次冻结）：**

| 门禁 | 标准 | 实测 | 判定 |
|---|---|---|---|
| K1 render-only 加速 | <15% 则中止 | **+81.5~84.9%**（4 组合一致；1080p bgra8：GL 0.256ms vs Metal 0.044ms） | 不触发 |
| K3 Qt 共存 | 需改 QOpenGLWidget 语义/5 viewport 分叉则中止 | 同窗并列共存 PASS；窗口级重叠时 Metal 覆盖 GL（浮层需原生处理的约束，见 Consequences） | 不触发 |
| K4 打包/签名 | 分发渠道不可行则中止 | 运行时 MSL 编译路线可分发；离线 metallib 工具链缺失 + 无 Developer ID（原生扩展路线的硬前提） | 条件性不触发（Phase 4 前复核路线） |
| K5 compute 落地 | Phase 4 未覆盖 2 效果入口 + Normalize 链路则中止 | Phase 4 未开始 | 待判 |
| K2 端到端 | Phase 5 p95 无改善则中止 | Phase 5 未开始 | 待判 |

5. **新实测约束入档：**
   - **ProRes 10-bit 422 无 CVMetalTextureCache 直通路径**（v210 仅可 8-bit 重解释导入；
     Metal 无原生 10-bit 422 纹理格式）。Phase 5 的 ProRes 部分维持条件化（叠加本机无
     ProRes 硬解，双重不可行）；NV12/P010 不受影响。
   - **macOS GL 4.1 兼容层 GPU 代价 ≈ 原生 Metal 的 5~6 倍**（同负载实测）——Phase 6
     的立项依据从"仅架构收益"升级为"实测收益待落地"。
   - K4 路线矛盾：纯 Python（PyObjC+ctypes，运行时编译 MSL）即可分发但有每帧 FFI
     开销；原生 Obj-C++ 扩展性能最好但把签名/公证变成硬前提。**Phase 4 先用
     PyObjC/ctypes 验证收益，原生扩展推迟到打包阶段独立决策。**

### 被否决的替代方案

| 方案 | 否决理由 |
|---|---|
| Qt RHI 单路径 | 需要把全部渲染改写为 RHI 抽象，等效重写；compute/视频互操作能力不受控（计划 §9.4） |
| Vulkan / MoltenVK | macOS 上 MoltenVK 是第三方兼容层，驱动行为随 Apple 工具链冻结，且与"保留 GL"双轨目标重复投资 |
| 一次性替换 OpenGL 为 Metal | 82,633 行 GL 触碰面、90 个编译点、222 个 `#version`——风险不可控，违反渐进双轨决策 |
| CVMetalTextureCache 导入 v210 作 10-bit 422 | Metal 无此像素格式；8-bit 重解释丢精度；R32Uint 步长不匹配触发校验硬断言 |

## Consequences

- 正面：Metal 主线继续有实测依据；`backend_compare_render_ab.md` 的方法学（同负载、
  GPU 计时、多次取中位数）成为 Phase 6 各模块迁移前的标准 A/B 形态。
- 约束（新增，来自 K3）：Metal 视口之上的浮层/标注 UI 必须走原生层，Qt 控件不会自动
  穿透 CAMetalLayer——P10-02/P10-03 设计必须包含浮层方案。
- 约束（新增，来自 K4）：Phase 4 期间产生的 Metal 依赖（pyobjc-framework-Metal
  `>=12.1,<13`，已固定）可随包分发；任何 C/Obj-C 原生产物进入打包清单前必须先解决
  Developer ID + 公证。
- 数据修偏：计划 §0.5 记录的 GPU 名（6900 XT）与本次两处探针（6800）不一致，
  以本次为准；不影响任何能力结论（同为 Navi21，Mac2 family）。

## Support matrix（Phase 1 时点，本机实测）

| 能力 | 结果 |
|---|---|
| Metal device / MSL 编译 / compute dispatch | ✅ |
| NV12 / P010 / BGRA → CVMetalTextureCache → MTLTexture | ✅ IOSurface-backed，storage=Managed |
| 2vuy（8-bit 422）→ GBGR422/BGRG422 | ✅ 原生 |
| v210（10-bit 422） | ⚠️ 仅 8-bit 重解释；无 10-bit 直通 |
| PyObjC `CVMetalTextureCacheCreateTextureFromImage` | ❌ -6661（走 ctypes） |
| CAMetalLayer 与 QOpenGLWidget 同窗 | ✅（重叠时 Metal 在上） |
| CAMetalLayer present vsync | ✅ 60Hz 锁定 |
| 离线 metal 编译器 / Developer ID / 公证 | ❌ / ❌ / ❌（Phase 4 前复核） |
