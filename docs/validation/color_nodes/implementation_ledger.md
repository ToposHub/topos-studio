# 调色节点后端实施台账（B00–B16 / S00–S02）

更新：2026-11-02（本轮施工末）。本文件是计划表逐任务状态的唯一事实来源，
后续会话从此接续。每项含：状态 / 提交 / 已验收证据 / 明确未完成项。

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
| B10 mixer/key/splitter | 53bccd3 | 规格文档冻结 v1 + N09 CPU oracle + 6 个 GL shader + 执行器 pass + 目录翻 exact；23 项 | Layer 仅 5 模式（其余 pending）；分支 key 后处理时序（blur/clean/gain）未并入；qualifier key 流 GL 生成未接（key 边用白 matte） |
| B11 compound/shared | 7f41d16 | group/ungroup plan+CPU+GL 位级等价；嵌套；多实例不同输入；定义 fanout；make local 隔离；9 项 | compound 动态端口 mapping（key 边跨界仍拒绝）；跨 clip 缓存键落盘 |
| B13 导出闸门（子集） | 54dd6f1 | 图能力硬门：可投影→图派生；复杂→显式拒绝；legacy 不变；3 项 | Composite/预览统一走 N07；LUT fast path 图资格（can_export_lut 已备未接 E24 调用点）；alpha output 导出格式预检 |
| B14 持久化核心 | ea9dbd3 | 栈载荷/版本链路图无损往返；未知节点 load→save 保留；LUT pointwise 资格门 + 拒绝矩阵；13 项 | still/剪贴板子图闭包（E25/E09 侧）；组链 E27 图语义；save_router standalone 路径显式携图（已验证载荷层等价） |
| B15 脚本 API | f440eab | N11 事务 API（批量原子/stale revision/兼容视图显式拒绝/能力可见）；与 UI 同 service；11 项 | E28 旧 layers API 直改（现经 legacy_layers_view 只读）；E32/E33 AI 目录注册与补偿契约 |

## 未开始 / 部分开始

- **B12 CPU/Metal 能力矩阵**：GL 目录已建立（GL_CAPABILITY_CATALOG）；N10
  Metal 执行器、E22/E23 图算子 MSL、CPU 完整 Corrector（N09 目前受限域）、
  三后端同 fixture 矩阵与实机证据均未开始。
- **B16 发布验收**：bench 脚本（F08 媒体级）、跨平台 golden、旧 layers 生产
  双写清理（现生产渲染仍走旧路径——**这是有意的**：图执行器 standalone
  闭环位级等价后，切生产调度需要 B06 剩余面板接线 + S04 一并完成）、
  平台实机矩阵未开始。
- **S00–S02 示波器**：未开始（独立于节点后端；现有链路回归 125 项通过的
  记录见计划 §6.8）。
- **S03–S07**：随 B09/B13 部分就绪（图输出→scope 的接入点在
  executor.execute_plan 发布处预留），正式任务未开始。

## 关键诚实声明

1. **生产渲染路径未切换**：AsyncRenderer 仍按 layers 循环渲染（B07 起
   经 N06 同一算子）。图执行器（N07）在 standalone 上下文闭环并证明位级
   等价；切换生产调度是 B16 的门（需 S04 scope 接入 + 面板接线完成）。
2. **已有图工程的导出**：复杂图目前显式拒绝导出（不丢效果）；简单串行图
   经图投影导出。
3. **qualifier/key 的 GL 生成**：图执行路径的 key 边当前以白 matte 参与
   （N06 render_key_gl 提供 CPU 组合；GPU keyer 仍在 E03 缓存路径）。
4. 存量环境问题（非本轮引入）：shader_golden_corpus 的
   grading_extreme/checker 本机位级失配；tests/edit/test_n03 3 项
   SimpleNamespace 失败；tests/edit/ai_editing 3 模块系统 openssl 导入错误。

## 回归命令（全绿基线）

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/color/test_color_graph_schema.py tests/color/test_color_graph_migration.py \
  tests/color/test_color_graph_service.py tests/color/test_color_graph_validation.py \
  tests/color/test_color_graph_commands.py tests/color/test_color_graph_ui_contract.py \
  tests/color/test_color_graph_panel_binding.py tests/color/test_color_graph_clip_switch.py \
  tests/color/test_color_graph_compiler.py tests/color/test_color_graph_gl_execution.py \
  tests/color/test_color_graph_cancel_resources.py tests/color/test_color_graph_mixers.py \
  tests/color/test_color_graph_keys.py tests/color/test_color_graph_splitter.py \
  tests/color/test_color_graph_compound_shared.py tests/color/test_color_graph_persistence.py \
  tests/color/test_color_graph_scripting_api.py tests/color/test_corrector_operator_parity.py \
  tests/color/test_legacy_baseline_behaviors.py tests/color/test_color_graph_baseline_fixtures.py \
  tests/app/test_color_graph_export_preview.py \
  tests/ui/test_color_node_editor_state.py tests/ui/test_color_node_editor_view.py \
  tests/ui/test_color_node_editor_panel.py tests/ui/test_color_node_host_menu.py \
  tests/ui/test_color_node_source_preview.py -q
```
