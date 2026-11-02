# 发布验收矩阵（B16.3/B16.4）——2026-11-02

规则：每项标注 **PASS（证据命令+计数）** 或 **NOT RUN（原因）**；
NOT RUN 不被总 PASS 吞掉。机器：macOS 15.3.1 x86_64，AMD Radeon
RX 6800（GL 4.1 / Metal），Python 3.12.14。

## 验收矩阵

| 项 | 状态 | 证据 |
|---|---|---|
| 图 schema/迁移/service/command/持久化/脚本 API | PASS | 181 passed（8 套件，2026-11-02 本机） |
| GL 执行器 + 串行位级基线（f03_serial/bypass） | PASS | test_color_graph_gl_execution 3/3 |
| 取消/资源释放矩阵 | PASS | test_color_graph_cancel_resources 8/8 |
| mixer/key/splitter/compound/shared | PASS | 32/32 |
| CPU 生产域 parity（vs GL 生产 shader） | PASS | test_color_graph_cpu_parity 11/11（SDR≤1/255、HDR rel≤2e-3） |
| 三后端能力矩阵实测 | PASS | backend_matrix.md（F01/F02 六用例三后端全 PASS） |
| Metal 实机（AMD RX 6800） | PASS | test_color_graph_metal 20/20 |
| clip 切换 / 导出闸门 | PASS | 10/10 + 3/3 |
| 节点 UI（editor/view/panel/host menu/source preview） | PASS | 90/90 |
| 示波器：既有链路 + S01 分段诊断 | PASS | 125 + 22 = 147/147 |
| shader golden corpus | **PASS-with-known-exception** | 10 passed；`grading_extreme/checker` 位级失配为**存量环境问题**（台账 0004a：需人工审查后 TOPOS_UPDATE_SHADER_GOLDEN=1 重归档；非本轮引入，base 复核过） |
| F08 媒体级基准（300 帧 × 1080p/4K） | PASS | backend_bench.json（下节摘要） |
| 能力表 + 旧 layers 写入口扫描 | PASS | capability_inventory.md（48 写点全分类、待审计 0） |
| 跨平台 golden（Windows / Linux / Apple Silicon） | **NOT RUN** | 无对应机器；跨平台矩阵待实机（计划 B16.3） |
| Apple Silicon / Intel GPU Metal 实机 | **NOT RUN** | 仅 AMD 6800 在场 |
| 原生示波器验收矩阵（S00 复现/S02 全模式） | **NOT RUN** | 需原生 GUI 会话（scopes/s00_s01_record.md 协议） |
| 生产接通模式默认启用（B16.5） | **NOT RUN** | §7 门槛未满足：S04（scope 接图输出）与 B06 剩余面板接线未完成；生产渲染仍走旧 layers 循环（N06 同算子，经 B09 位级等价证明） |

## B16.2 基准摘要（F08，4K_H264.mov，300 帧）

| 指标 | 1920×1080 | 3840×2160 |
|---|---|---|
| graph p50/p95/p99 | 122.1/142.1/149.3 ms | 537.0/778.2/989.3 ms |
| legacy p50/p95/p99 | 111.2/151.8/251.6 ms | 475.6/627.1/744.5 ms |
| present(readback) p50 | 25.4 ms | 124.3 ms |
| decode p50 / compile | 16.5 ms / 450.7 ms | 19.3 ms / 854.6 ms |
| RSS 增长 | +2.3 MB（稳定） | <200 MB（稳定，见报告样本） |
| executed_passes | 恒 = plan passes（5） | 同左 |

**诚实结论**：图路径与旧路径同数量级（p95 尾部图路径更稳），但两者都
远非实时——单帧成本主要在**逐 pass 纹理分配**（图执行器无池化）与
**fp16 全帧 readback**（present 代理）。这组数字是 B16.5 切换前的
已知优化面（执行器 scratch 池化 = N07 后续债；生产 present 不做
readback，代理值是上界）。丢帧计数（60/30fps 预算）见 JSON——当前
standalone 闭环不满足实时，不冒充满足。

## B16.4 能力诊断与菜单契约

- 三后端能力可见：`ColorGraphScriptAPI.inspect_capabilities`（gl/cpu/
  metal 三列）+ `graph_backend_capability_matrix`（E30）；
- provider 未实现如实禁用：`graph_node_backend_options`（compound
  ui_enabled=False——无任何后端支持即不呈现为可执行选项；测试
  test_color_graph_backend_capabilities 8 项锁定）；
- 菜单契约：host menu 20 项测试（主节点菜单 action 不触旧 layers
  后端、分支菜单只编辑 draft、快捷键隔离、历史路由）；
- schema 兼容：未知字段/节点 load→save 保留（B14 持久化 13 项）；
  `TOPOS_SCOPE_DIAG`（S01）与 `get_render_stats()["gpu_scope_diagnostics"]`
  提供运行期诊断；
- 操作帮助：本目录文档（backend_matrix / capability_inventory /
  scopes/s00_s01_record / 本文件）+ docs/color_nodes_mixer_spec.md。

## 复跑命令

```bash
# 全图 + 示波器 + Metal（本机全绿 559 项）
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/color/test_color_graph_*.py tests/color/test_corrector_operator_parity.py \
  tests/color/test_legacy_baseline_behaviors.py tests/color/test_scope_*.py \
  tests/color/test_gpu_scope_pipeline.py \
  tests/shared/gpu/metal/test_color_graph_metal.py \
  tests/app/test_color_graph_clip_switch.py tests/app/test_color_graph_export_preview.py \
  tests/ui/test_color_node_*.py -q

# 基准 / 矩阵 / 清单
QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/bench_color_graph.py \
  --fixture-set standard --sizes 1920x1080,3840x2160 --frames 300 \
  --report docs/validation/color_nodes/backend_bench.json
QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/measure_color_graph_backends.py
QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/color_graph_capability_report.py
```
