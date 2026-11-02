# 调色节点后端实施台账（B00–B16 / S00–S02）

更新：2026-11-02（B12/S01/B16 子集施工末）。本文件是计划表逐任务状态的
唯一事实来源，后续会话从此接续。每项含：状态 / 提交 / 已验收证据 /
明确未完成项。

## 已完成（有测试与提交证据）

| 任务 | 提交 | 状态与证据 | 明确未覆盖（债务） |
|---|---|---|---|
| B00 基线冻结 | 676a933 | 调用图文档（b00_call_graph.md）；F01–F03 确定性生成器；生产 composition shader RGBA16F float 基线（manifest 含 git SHA/GPU 指纹/neutral 漂移实测 SDR 1.9e-4 HDR rel 4.0e-4/1080p×300 性能锚点）；legacy 行为 17 项冻结 | F06/F07 fixture 生成器（时序/离线 bake 载荷）只覆盖行为用例层 |
| B01 UI 契约 | 2f7f566 | ACTION_CONTRACTS 40 action + envelope 规范化 + 会话协调器；AST 覆盖门禁（新增未登记即失败）；22 项 | 无 |
| B02 图 schema | 9108e9e | N01 纯数据（typed ports/冻结/未知保留/深度上限）；E06 集成；33 项 | 无 |
| B03 legacy 迁移 | e8a2ef5 | N03 迁移/严格投影；E07 图优先加载；幂等/载荷守恒/DTO 等价；31 项 | 像素等价经 DTO 守恒间接证明（B09 已补直接位级证据） |
| B04 编辑 service | 595f8bb | N02 全量操作 + 原子性 + 随机图不变量（4 种子×60 步）；60 项 | paste_grade_domains 剪贴板域（B14 后续） |
| B05 command/undo | 0dfe8ba | ApplyColorGraphCommand（快照 redo 字节等价/冲突不入史）；事务合并（50 拖动 1 undo）；栈锁；跨栈 shared fanout；16 项 | E12 standalone 历史适配器未接（standalone 模式仍走旧历史） |
| B06 面板接线 | b435ab0 | 句柄四元组/面板宿主（防递归/只读选择/undo 走项目栈）/切换协议/E13 观察者；19 项 | E09/E10 真实 GradingPanel/Tracker 面板未切换到句柄读路径（LegacyGradingAdapter 已备，接线待 B16 前置验证）；group scope owner 未接真实组 ID |
| B07 Corrector 提取 | 6bc02ed | N06（prepare/render/key/serial）+ E02/E03 统一调用；golden 位级对照 4 组；655 项回归 | Metal/CPU 侧同算子（B12）；qualifier GPU keyer 未入 N06（E03 缓存路径继续） |
| B08 snapshot/plan | 57699f5 | N04 冻结 + N05 依赖锥/compound 展开/shared 解析/liveness/峰值上界/能力预检/缓存/可解释 JSON；17 项 | 预算驱动的缓存淘汰（§5.4 只落了峰值统计） |
| B09 GL 执行 | 4740c69 | N07 执行器：逐 pass 取消/liveness 释放/fail-closed/发布语义；**串行图 vs 旧 layers 基线位级一致**（f03_serial+bypass）；11 项 | 未接 E03 生产调度（生产渲染仍走旧 layers 循环；图执行器在 standalone 闭环）；RAW/降噪/防抖/颗粒在图路径的位置迁移未做（旧路径保留） |
| B10 mixer/key/splitter | 53bccd3 | 规格文档冻结 v1 + N09 CPU oracle + 6 个 GL shader + 执行器 pass + 目录翻 exact；23 项 | ~~Layer 仅 5 模式~~ **v1.1 已收口（本轮）**：spec §9 全模式表（权威=BLEND_MODES_SNIPPET，mix epilogue 与 v1 内嵌式代数等价；HSV 四模式无符号防护同权威；Subtract(Clamped) 与 v1 Subtract 并存）；CPU/GL/Metal 三后端同式 + 手算锚点 + GL shader 对照 + Metal kernel 对照（mixers 14 + Metal 21）| 分支 key 后处理时序（blur/clean/gain）未并入；qualifier key 流 GL 生成未接（key 边用白 matte） |
| B11 compound/shared | 7f41d16 | group/ungroup plan+CPU+GL 位级等价；嵌套；多实例不同输入；定义 fanout；make local 隔离；9 项 | compound 动态端口 mapping（key 边跨界仍拒绝）；跨 clip 缓存键落盘 |
| B13 导出闸门（子集） | 54dd6f1 | 图能力硬门：可投影→图派生；复杂→显式拒绝；legacy 不变；3 项 | Composite/预览统一走 N07；LUT fast path 图资格（can_export_lut 已备未接 E24 调用点）；alpha output 导出格式预检 |
| B14 持久化核心 | ea9dbd3 | 栈载荷/版本链路图无损往返；未知节点 load→save 保留；LUT pointwise 资格门 + 拒绝矩阵；13 项 | still/剪贴板子图闭包（E25/E09 侧）；组链 E27 图语义；save_router standalone 路径显式携图（已验证载荷层等价） |
| B15 脚本 API | f440eab | N11 事务 API（批量原子/stale revision/兼容视图显式拒绝/能力可见）；与 UI 同 service；11 项 | E28 旧 layers API 直改（现经 legacy_layers_view 只读）；E32/E33 AI 目录注册与补偿契约 |
| B12 CPU/Metal 矩阵 | 01da848 | N09 生产域 Corrector oracle（GRADING/CURVES 同序移植；修复 rgb2hsv 移植错误；与 GL 同提参路径）；E23 十个图算子 MSL compute kernels；N10 Metal 执行器（同 plan/能力 fail-closed/逐 pass 取消/buffer lease 池化）；E30 三后端矩阵 + UI 可选项；fallback=域受限完整执行的两段式语义；**实测（AMD RX 6800）F01/F02 六用例三后端全 PASS**（backend_matrix.md）；39 项测试 | Metal/基础域外的 Corrector（插件/HSL/LUT3D/mixer/warper/fx/邻域/非 Normal 混合）显式拒绝（矩阵 fallback 语义；E22 fragment 全域路径未接 N10）；Apple Silicon/Intel Metal 实机 NOT RUN；qualifier GPU keyer 图内生成仍白 matte |
| S00–S02 示波器（S01） | 1405f67 | **S01 完成**：scope_diagnostics 五段（submit/queue/execute/panel/widget）22 类静默丢弃→可计数可关闭结构化事实；fake demand 22 项测试 + 既有 125 项全绿；S00 现场记录与原生复现协议（scopes/s00_s01_record.md） | **S00 原生矩阵 NOT RUN**（需原生 GUI 会话）；S02 无命中证据不臆测修复（shader 失败永久关 lane 已从静默转可诊断）；全模式原生验证待 S00 证据 |
| B16 发布验收（子集） | 5ca7b3c | **B16.1**：capability_inventory.md（39 action 表 + provider 表 + 三后端表 + 旧 layers 写入口扫描：48 写点全分类、待审计 0）；**B16.2**：bench_color_graph.py F08 基准（4K_H264 300 帧 × 1080p/4K，decode/compile/graph/present 分段 + p50/p95/p99 + 丢帧 + RSS 稳定 + 无双执行断言）；**B16.3/16.4**：release_acceptance.md（PASS/NOT RUN 矩阵；跨平台/原生/接通模式如实 NOT RUN） | **B16.5 未满足**（见声明 1）；图执行器逐 pass 纹理分配无池化（bench 已量化，切换前优化面）；Metal/跨平台实机矩阵 NOT RUN |

## 未开始 / 部分开始

- **B16.5 生产接通**：§7 门槛未满足——S04（scope 接同一图 plan 输出）
  与 B06 剩余面板接线（E09/E10 真实 GradingPanel/Tracker 读路径）未做；
  满足前生产渲染保持旧 layers 循环（经 N06 同算子 + B09 位级等价证明）。
- **S03–S07**：随 B09/B13 部分就绪（executor.execute_plan 发布处预留
  scope 接入点）；正式任务未开始，为 B16 签收硬门。

## 关键诚实声明

1. **生产渲染路径未切换**：AsyncRenderer 仍按 layers 循环渲染（B07 起
   经 N06 同一算子）。图执行器（N07）在 standalone 上下文闭环并证明位级
   等价；切换生产调度是 B16.5 的门（需 S04 scope 接入 + B06 剩余面板
   接线完成）。B16.2 基准同时量化了切换前已知优化面：图执行器逐 pass
   纹理分配无池化、fp16 全帧 readback 代理（见 backend_bench.json）。
2. **已有图工程的导出**：复杂图目前显式拒绝导出（不丢效果）；简单串行图
   经图投影导出。
3. **qualifier/key 的 GL 生成**：图执行路径的 key 边当前以白 matte 参与
   （N06 render_key_gl 提供 CPU 组合；GPU keyer 仍在 E03 缓存路径）。
4. **CPU/Metal Corrector = 基础点态域**（B12 起）：插件/HSL 曲线/LUT3D/
   mixer/warper/fx/邻域/非 Normal 混合在 CPU/Metal 显式 fail-closed
   （`production_layer_domain_errors`，与 GL 有效域一致对齐）；GL 侧
   经生产 composition shader 全域 exact。
5. 存量环境问题（非本轮引入）：shader_golden_corpus 的
   grading_extreme/checker 本机位级失配（需人工重归档，ADR 0004）；
   tests/edit/test_n03 3 项 SimpleNamespace 失败；tests/edit/ai_editing
   3 模块系统 openssl 导入错误。跨平台（Windows/Linux/Apple Silicon）
   golden 与 Metal 实机 NOT RUN。

## 回归命令（全绿基线）

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/color/test_color_graph_schema.py tests/color/test_color_graph_migration.py \
  tests/color/test_color_graph_service.py tests/color/test_color_graph_validation.py \
  tests/color/test_color_graph_commands.py tests/color/test_color_graph_ui_contract.py \
  tests/color/test_color_graph_panel_binding.py tests/app/test_color_graph_clip_switch.py \
  tests/color/test_color_graph_compiler.py tests/color/test_color_graph_gl_execution.py \
  tests/color/test_color_graph_cancel_resources.py tests/color/test_color_graph_mixers.py \
  tests/color/test_color_graph_keys.py tests/color/test_color_graph_splitter.py \
  tests/color/test_color_graph_compound_shared.py tests/color/test_color_graph_persistence.py \
  tests/color/test_color_graph_scripting_api.py tests/color/test_corrector_operator_parity.py \
  tests/color/test_legacy_baseline_behaviors.py tests/color/test_color_graph_baseline_fixtures.py \
  tests/color/test_color_graph_cpu_parity.py tests/color/test_color_graph_backend_capabilities.py \
  tests/color/test_scope_diagnostics.py tests/color/test_scope_live_refresh.py \
  tests/color/test_scope_input_source.py tests/color/test_scope_multiview_ui.py \
  tests/color/test_gpu_scope_pipeline.py \
  tests/shared/gpu/metal/test_color_graph_metal.py \
  tests/app/test_color_graph_export_preview.py \
  tests/ui/test_color_node_editor_state.py tests/ui/test_color_node_editor_view.py \
  tests/ui/test_color_node_editor_panel.py tests/ui/test_color_node_host_menu.py \
  tests/ui/test_color_node_source_preview.py -q
# 2026-11-02 实测：590 passed（含 Metal 实机 20 项；EXIT=0）
```
