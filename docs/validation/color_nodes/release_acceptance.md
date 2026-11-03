# 发布验收矩阵（B16.3/B16.4）——2026-11-03

规则：每项标注 **PASS（证据命令+计数）** 或 **NOT RUN（原因）**；
NOT RUN 不被总 PASS 吞掉。机器：macOS 15.3.1 x86_64，AMD Radeon
RX 6800（GL 4.1 / Metal），Python 3.12.14。

## 验收矩阵

| 项 | 状态 | 证据 |
|---|---|---|
| 图 schema/迁移/service/command/持久化/脚本 API | PASS | 台账回归命令 2026-11-03：681 passed（EXIT=0） |
| GL 执行器 + 串行位级基线（f03_serial/bypass） | PASS | test_color_graph_gl_execution 3/3 |
| 取消/资源释放矩阵 | PASS | test_color_graph_cancel_resources 8/8 |
| mixer/key/splitter/compound/shared | PASS | 32/32 |
| CPU 生产域 parity（vs GL 生产 shader） | PASS | test_color_graph_cpu_parity 11/11（SDR≤1/255、HDR rel≤2e-3） |
| 三后端能力矩阵实测 | PASS | backend_matrix.md（F01/F02 六用例三后端全 PASS） |
| Metal 实机（AMD RX 6800） | PASS | test_color_graph_metal 21/21 |
| clip 切换 / 导出闸门 | PASS | 10/10 + 3/3 |
| 节点 UI（editor/view/panel/host menu/source preview） | PASS | 90/90 |
| 示波器：既有链路 + S01 分段诊断 | PASS | 125 + 22 = 147/147 |
| **S03/S04：scope 图取样契约 + 真实图输出接通** | PASS | test_color_graph_scope_output 30/30（契约/provider/E02/E07/E03 splice/实机 GL vs CPU oracle：两非交换 Corrector、分支 Mixer、Compound；调序/旁路/删除改变输出；租约语义） |
| **S05/S06：新鲜度身份 + 颜色/多视图一致性** | PASS | test_color_graph_scope_lifecycle 23/23（身份拒绝矩阵/面板迟到拒绝/E09 身份构造/单采样多模式/CIE metadata） |
| **B06 剩余：真实面板接线** | PASS | test_color_graph_app_host 14/14（clip 水合/迁移视图/首次建图/提交后引擎重投影/undo-redo 同步/grade 手势事务合并/无选择零变更/组 owner 真实 ID/活动层对齐） |
| **B16.5 生产接通（flag 门控）** | PASS | test_color_graph_production_switch 11/11（默认关/flag 开/full-res 排除/fail-closed/池化位级一致）；`TOPOS_COLOR_GRAPH_RENDER` 默认关 |
| **B08 §5.4 预算缓存淘汰** | PASS | test_color_graph_plan_cache 7/7 |
| shader golden corpus | **PASS-with-known-exception** | 10 passed；`grading_extreme/checker` 位级失配为**存量环境问题**（台账 0004a：需人工审查后 TOPOS_UPDATE_SHADER_GOLDEN=1 重归档；非本轮引入，base 复核过） |
| F08 媒体级基准（300 帧 × 1080p/4K） | PASS | backend_bench.json（下节摘要；池化后图路径较 legacy 快约 2×，RSS 稳定） |
| 能力表 + 旧 layers 写入口扫描 | PASS | capability_inventory.md（48 写点全分类、待审计 0） |
| 跨平台 golden（Windows / Linux / Apple Silicon） | **NOT RUN** | 无对应机器；跨平台矩阵待实机（计划 B16.3） |
| Apple Silicon / Intel GPU Metal 实机 | **NOT RUN** | 仅 AMD 6800 在场 |
| 原生示波器验收矩阵（S00 复现/S02 全模式/S07 录制） | **NOT RUN** | 需原生 GUI 会话（scopes/s00_s01_record.md 协议）；S07 性能/资源表随原生会话生成 |
| 生产接通模式**默认启用**（B16.5 剩余门） | **NOT RUN** | flag 门控路径已完成并测试（开启即图执行）；默认启用需 S07 原生验收矩阵；默认值当前保持关闭 |
| flag 开启路径的原生 GUI 冒烟 | **NOT RUN** | offscreen 仅单测级覆盖；需原生会话开 `TOPOS_COLOR_GRAPH_RENDER=1` 冒烟 |

## B16.2 基准摘要（F08，4K_H264.mov，300 帧；2026-11-03 池化后复测）

| 指标 | 1920×1080 | 3840×2160 |
|---|---|---|
| graph p50/p95/p99 | 53.6/77.5/81.4 ms | 234.3/298.5/305.4 ms |
| legacy p50/p95/p99 | 109.0/135.1/137.5 ms | 474.1/552.0/579.3 ms |
| present(readback) p50 | 18.4 ms | 84.1 ms |
| decode p50 / compile | 16.0 ms / 384.3 ms | 18.7 ms / 505.9 ms |
| RSS 增长 | +4.2 MB（稳定） | +2.8 MB（稳定；独立进程 0.0MB 对照） |
| executed_passes | 恒 = plan passes（5） | 同左 |

**演进**（同机同负载）：池化前 graph p50 122.1/537.0 ms（p95 尾部稳但
均值劣于 legacy）→ 池化后 53.6/234.3 ms——逐 pass 纹理分配优化面收口，
图路径较 legacy 快约 2×。fp16 全帧 readback 代理（present 段）仍是
已知上界（生产 present 不做 readback）。丢帧计数见 JSON——当前
standalone 闭环不满足实时（4K 尤甚），不冒充满足。bench RSS 基线取样
已修正（decode 保留缓冲不属于被测路径；多尺寸同进程不再误报）。

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
# 台账全量回归（本机全绿 681 项；见 implementation_ledger.md 命令块）
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest <台账命令> -q

# 基准 / 矩阵 / 清单
QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/bench_color_graph.py \
  --fixture-set standard --sizes 1920x1080,3840x2160 --frames 300 \
  --report docs/validation/color_nodes/backend_bench.json
QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/measure_color_graph_backends.py
QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/color_graph_capability_report.py

# B16.5 flag 冒烟（图执行接管 front/color output；默认关）
TOPOS_COLOR_GRAPH_RENDER=1 .venv/bin/python <原生启动入口>
```

## 2026-11-03 前后端实际复查补充

见 [集成实测报告](integration_2026-11-03/README.md)。本次修复真实UI接线、Compound子图事务、参数回显与示波器共享上下文问题；最终全量3595 passed/5 skipped/1明确排除的既有golden失配，之后面板只读/Compound导航补充回归56 passed。原生图执行开关开启/关闭两种配置的短素材工作流均通过，全部示波器截图已保存。此结果补齐基础原生冒烟，原S07持续播放/4K/浮动窗口压力矩阵仍未完整覆盖；逐节点缩略图与Timeline栈模型等限制见报告，原NOT RUN项不得自动改成全部PASS。
