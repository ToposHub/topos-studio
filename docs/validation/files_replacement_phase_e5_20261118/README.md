# E 阶段第四批（E5/FUP-07）：GPU 所属线程——渲染线程 + 共享上下文 + 呈现接帧

日期：2026-11-18 · 分支：fix-ui · 范围：播放器 GL 画布的 YUV 上传/转换/
绘制移出 GUI 线程，接 deferred-release 协议；真 GPU（本机 AMD RX 6900
XT / GL 4.1 / cocoa）完成端到端验证。

## 交付内容

| 文件 | 变更 |
| --- | --- |
| `gl_render_worker.py`（新） | `GLVideoRenderWorker` + `_RenderThread`：专用 QThread 的**共享上下文**（`setShareContext` 挂进画布 share group；`QOffscreenSurface` 按 Qt 契约 GUI 构造/归线程、渲染线程 create）；latest-wins 命令队列（排空只留最新 frame/size/probe）；双缓冲前台帧（渲染线程写后备 FBO → `ctx.finish()` → 锁内换前 + 发布线程安全快照）；`GLResourceRegistry` 登记 uploader/FBO（`release_fn`），关停在**拥有 context 的渲染线程**释放并销账、失败保留登记可重试（AUD-1300）；`shareGroup()` 探测——无共享组时前台帧改 CPU 回读发布（转换仍在渲染线程）；`probe()` 供测试/诊断在渲染线程读取 |
| `gl_video_canvas.py` | `initializeGL` 首选渲染线程路径；worker 初始化/渲染失败回退**既有 GUI 直绘**（原 `_paint_yuv` 保留，行为不变约束）；`set_image(YuvFrame)` 只入队；`paintGL` 只呈现：texture 模式 raw bind + 贴图四边形（`_raw_gl_binder`：QOpenGLFunctions_4_1_Core→ctypes 兜底，本 PyQt6 构建无 QOpenGLFunctions）、cpu 模式 QImage 贴出；`resizeGL` 提交尺寸快照；关停先停 worker（渲染线程释放）再收 GUI 侧呈现资源 |

## 所有权模型（与 AsyncRenderer 同协议、独立实例）

```
GUI 线程                          渲染线程（共享上下文）
────────                          ────────────────────
set_image ── submit_frame ──▶ 命令队列（latest-wins 排空）
paintGL ◀── front_texture ── 上传 YUV 平面 → YUV→RGB → 后备 FBO
（raw bind / QImage 贴出）        finish() → 锁内换前 + 发布快照
submit_size（DPR 快照）─▶        尺寸变化 → FBO 重建
closeEvent ── stop ──────────▶  拥有线程释放 + 销账（失败留账重试）
```

## 验证（真 GPU，本机执行；无 GL 环境自动跳过）

`tests/ui/test_gl_render_worker_e5.py`（6 项全绿）：

1. **线程断言**：上传/转换/绘制发生在专用渲染线程（≠GUI 线程名）；
2. **letterbox 像素**（渲染线程 probe 回读前台帧）：2:1 帧在 4:3 框中
   中心=画面、四角=黑边；
3. **latest-wins**：连投蓝/红两帧，前台最终为红（排队旧帧被丢弃）；
4. **GUI 呈现端到端**：`grabFramebuffer` 中心像素红主导（跨上下文纹理
   raw bind 采样真的工作——本机共享组成立）；
5. **deferred release**：渲染后台账 2 条（uploader+FBO）；`stop()` 有界
   退出且台账清零（所属线程释放 + 销账，非 `clear()` 假清）；
6. **回退**：注入 worker 初始化失败 → 画布回退 GUI 直绘（原实现保留，
   `_uploader` 懒建，`gpu_active` 照常）。

回归：`test_gpu_yuv_frame`（5+3 真媒体跳过）、playback 三套（E3/E4/
FR-18~21）+ `test_topos_audio` + `test_native_player_host` —— 66 通过。

## 诚实边界

- **跨上下文呈现依赖共享组**：本机（macOS/AMD）共享组成立、零拷贝
  texture 路径已验证；无共享组的平台自动降级 CPU 回读发布（每帧一次
  GPU→GPU 读回 + 一次 CPU 拷贝，转换仍不在 GUI 线程）——两条路径都有
  测试或代码路径覆盖，CPU 模式在本机未触发（本机共享组可用）。
- **同步用 `ctx.finish()` 而非跨上下文 fence**：诚实换正确性；帧率由
  latest-wins 吸收。
- **QOpenGLWidget 的 initializeGL/paintGL 仍在 GUI 线程**（Qt 硬性约
  束）：整改的是"内容 GPU 工作"（上传/转换/绘制）的所属线程；paintGL
  只剩呈现（raw bind + 一次四边形，或 QImage 贴出）。
- 呈现层 DPR：worker 按设备像素渲染（`refresh_size_cache` 快照），GUI
  呈现按 widget 尺寸贴出。
- 10/12-bit YUV 直上（R16UI）仍在渲染线程 uploader 内，未做 HDR 色调
  映射（与 E4 边界一致）。

## 隐私

合成 YUV 平面（内存生成）+ 真 GPU 渲染断言；无真实媒体、无隐私路径。
