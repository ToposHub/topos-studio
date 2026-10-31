# P5 抽查复核任务书 V1（共 16 条）

每条：先读来源批次报告中该编号的完整论述（含证据与行号），再独立 Read 源码验证。

- [AUD-36100] [Critical] done-callback 异常逃逸会泄漏信号量槽且 outcome 永不完成，run() 无超时等待导致导出线程永久死锁
  - 位置: N/A:104-120（及 169）
  - 来源报告: docs/audit/2026-09-28/batches/batch-361-shared-media-windows_hw.md

- [AUD-38206] [Suggestion] scene-linear RGBA 在构造/刷新时即被量化为 8-bit sRGB，无用户交互也会损失 HDR 精度
  - 位置: N/A:22-30, 73, 112-119
  - 来源报告: docs/audit/2026-09-28/batches/batch-382-shared-ui-widgets.md

- [AUD-2710] [Suggestion] 占位片段 fallback 轨道解析可抛 KeyError/StopIteration；新素材未刷新媒体池
  - 位置: N/A:8493-8496（另 8483 后未调
  - 来源报告: docs/audit/2026-09-28/batches/batch-027-app-controllers.md

- [AUD-20600] [Critical] closeEvent 对 wait 超时后仍在运行的预览线程调用 deleteLater，可销毁运行中的 QThread 导致崩溃
  - 位置: N/A:2663-2667（配合 1984-1995）
  - 来源报告: docs/audit/2026-09-28/batches/batch-206-features-edit-ui.md

- [AUD-23108] [Major] `fetch_result` SUCCESS 路径访问 `self._svc._jobs` 私有属性，破坏自述的 duck-type 替换契约
  - 位置: N/A:456-458, 496-498
  - 来源报告: docs/audit/2026-09-28/batches/batch-231-features-ai_workflow-adapters.md

- [AUD-32500] [Critical] `to_ocio_name` 对未映射 (primaries, transfer) 组合静默回退 "lin"，默认 OCIO 引擎把已编码信号当线性、整体跳过 ODT 编码
  - 位置: N/A:89（叠加 81–86 的 LINEAR 分支）
  - 来源报告: docs/audit/2026-09-28/batches/batch-325-shared-color.md

- [AUD-6316] [Suggestion] 输出 TextureResult 沿用输入 `bbox/format`，当实际输出尺寸与输入不一致时元数据失配
  - 位置: N/A:152-155, 239-245
  - 来源报告: docs/audit/2026-09-28/batches/batch-063-features-composite-nodes.md

- [AUD-22414] [Major] Supervisor 记账字典只增不删，子代理完成后无清理，长会话无界内存增长
  - 位置: N/A:131-134, 176-178, 221-223
  - 来源报告: docs/audit/2026-09-28/batches/batch-224-features-ai_harness.md

- [AUD-32606] [Suggestion] `DEFAULT_CONFIG` 全程浅拷贝，嵌套容器（features/backend/recent_files/page_layouts 等）与类属性共享同一对象
  - 位置: N/A:367, 388, 618, 634
  - 来源报告: docs/audit/2026-09-28/batches/batch-326-shared-config.md

- [AUD-22322] [Major] brief_template / input_schema 数值未经类型校验直接 int()/float()，畸形 manifest 会炸掉整个目录渲染
  - 位置: N/A:74-76
  - 来源报告: docs/audit/2026-09-28/batches/batch-223-features-ai_harness.md

- [AUD-36816] [Major] check_offline_assets 对未知工程类型静默返回空报告，与 relink/batch_relink 的显式报错不一致
  - 位置: N/A:248-249（对照 353-354, 401-402）
  - 来源报告: docs/audit/2026-09-28/batches/batch-368-shared-project.md

- [AUD-32203] [Major] ToposMuxFile 顺序写回调未持实例锁，与 seek_write 回调的加锁不对称（Windows 下文件指针竞争）
  - 位置: N/A:2488
  - 来源报告: docs/audit/2026-09-28/batches/batch-322-shared-codec.md

- [AUD-18500] [Critical] deep-u16 效果桥代码块缩进脱出 `if deep_u16:`，效果链在两条主路径上失效或取错纹理
  - 位置: N/A:1634
  - 来源报告: docs/audit/2026-09-28/batches/batch-185-features-edit-core.md

- [AUD-6111] [Critical] STANDARD_NAMES 漏登 4 个已注册 shader，Roto/MaskMerge/运动模糊累积恒走降级路径
  - 位置: N/A:91-121（STANDARD_NAMES）与 181、183、186、206（_SHADER_SOURCES）
  - 来源报告: docs/audit/2026-09-28/batches/batch-061-features-composite-evaluators.md

- [AUD-10400] [Critical] 使用未定义的 `logger`，粘贴 Topos Color 节点时抛 NameError 使粘贴中断/崩溃
  - 位置: N/A:796
  - 来源报告: docs/audit/2026-09-28/batches/batch-104-features-composite-ui.md

- [AUD-25600] [Critical] 程序化 set_params/reset 时 invert 复选框 toggled 绕过状态锁，强制把限定器置为 enabled 并提交引擎
  - 位置: N/A:742）
  - 来源报告: docs/audit/2026-09-28/batches/batch-256-features-color-ui.md
