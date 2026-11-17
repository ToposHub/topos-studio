# G 阶段第四批（G4）：decode_benchmark 接管——直接接既有 host service

日期：2026-11-18 · 分支：fix-ui · 范围：`decode_benchmark_service.py`（新，
宿主适配）+ 控制器原生命令表。G 节行验收：**来源/时间线上下文、预算、
取消、零素材写入；不借旧 bridge**。

## 新入口 / 新服务 / 关闭路径

| 部件 | 路径 |
| --- | --- |
| 新入口 | `launch("decode_benchmark")` → `_launch_native`（G1 三段 + F4 边界）→ `_open_decode_benchmark`（F5 singleton）→ 既有 `DecodeBenchmarkWindow` |
| 新服务 | `DecodeBenchmarkHostService`：**合成包加载**既有实现（`files_host_plugins.decode_benchmark`——benchmark.py 的 `DecodeBenchmarkRunner`（PyAV 抽帧 + 帧边界协作取消 + `BenchmarkConfig` 预算档位）与 benchmark_gui.py 的窗口/QThread worker/进度/结果导出）——**不重写第二套跑分、不走旧 HTTP bridge**；`create_window(parent, paths)` 携带来源上下文；`request_cancel` 路由到 worker |
| 关闭路径 | 既有 `closeEvent` 已请求 worker 取消（帧边界协作）——宿主经 F5 closer 触发；控制器 shutdown 同款 |

## 验证

- `tests/ui/test_decode_benchmark_g4.py`（5 项）：加载即既有实现全能力
  在（Runner/Config/导出——同一套非副本）；单次加载缓存 + 命名空间
  隔离；窗口创建携带来源路径进入待测清单；取消路由到 worker（既有
  closeEvent 兼容）；**零素材写入**（打开/取消全程素材逐字节不变）。
- `tests/ui/test_native_files_plugins.py` +1：控制器端到端——原生打开、
  singleton 同实例、**零 HTTP**、注册表关闭销账。
- 既有跑分核心契约（tests/file_explorer/test_decode_benchmark.py）不变绿；
  legacy HTTP 固定用例显式 `_legacy_http_mode`（四 builtin 已全原生——
  legacy 通道行为仍被验证但不再冒充生产路径）。

回归：G1-G4 + plugins + files + 既有跑分 —— **816 通过**（一次 sandbox
deadline 顺序抖动为仓库既有类，隔离绿）。

## 诚实边界

- 跑分执行本体未在本批重验（既有 63 项契约测试 + benchmark core 不变）；
  真实格式跑分矩阵归实机验收。
- 既有窗口的 Edit 时间线会话路径（`_edit_service`）仍指向旧宿主服务
  ——Edit 时间线上下文的原生接管随 Edit 域工作另行收口。
- 合成包加载依赖插件目录布局（`external/file_explorer/plugins/`）；I 阶段
  物理退役时迁移受管位置（加载器单点改路径）。

## 隐私

内存/合成字节素材；无隐私路径。
