# F 阶段第七批（F7）：作业历史/日志预算 + 新 generation 重激活

日期：2026-11-18 · 分支：fix-ui · 范围：`lifecycle.py`（PluginJobs 有界
历史 + 日志预算 + 重激活代数）+ `host_session.py` 透出。

## 交付内容

| 文件 | 变更 |
| --- | --- |
| `lifecycle.py` | ① **PluginJobs 有界历史**：终态任务移入 `deque(maxlen=history_limit)`（默认 200，先进先出淘汰）并退出在册表——长期运行 `_jobs` 不无界积累；`history(plugin_id, limit)` 新→旧查询（作业历史视图数据源）；`state()` 对历史内任务可查、淘汰后 unknown。② **日志预算**：`log(job_id, line)`/`job_logs` 每任务有界（默认 64 行；终态后在历史记录上补记）。③ **新 generation 重激活**：`reactivate`（仅限 fully_stopped 之后；代数 +1 后走同一 activate 路径——半失败全量回滚等契约不变）；`accepting_calls(generation=…)` 按代数判定——**旧代在途消息一律拒绝**；`generation(plugin_id)` 查询 |
| `host_session.py` | `reactivate_plugin`（→ 贡献集+新代数）、`accepting(plugin, generation)`、`job_history` 透出 |

## 验证

`tests/plugins/test_host_session_f5.py` +5（共 16；lifecycle 契约 29 项不变绿）：
- 12 任务 / 5 上限：历史恰 5 条、新→旧、在册表空、淘汰任务 unknown、
  过滤与限额；
- 10 行日志 / 4 预算：尾部保留、终态补记同预算、ghost 空；
- 重激活：fully_stopped 后放行且代数=1、旧代受理 False/新代 True；
- **未完全停用不能重激活**（在途任务）→ 确认终止后放行；
- 旧代应答迟到：只落历史（新→旧首条 cancelled）、按代数拒绝、终态后
  补记不复活在册表。

回归：tests/plugins（411）+ native 插件 UI + tests/files —— 全绿（一次
baseline_freeze 顺序抖动为仓库既有类，隔离/整文件复跑双绿）。

## 诚实边界

- 作业历史视图（UI）未做——`job_history` 为数据源，F11 安装闭环/中心
  UI 批次或 G 阶段按需消费。
- 代数守卫的消费方（窗口/命令携带 generation）在 G 阶段接线——本批
  提供 API 与锁定测试；F5 的窗口 context 代数已是同型语义。
- 历史不含日志之外的负载明细（payload/选择快照不进历史——隐私与体量）。

## 隐私

合成任务/日志行；无隐私路径。
