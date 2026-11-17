# F 阶段第六批（F6）：生命周期生产接线——停新→按 owner 取消→确认终止→清理

日期：2026-11-18 · 分支：fix-ui · 范围：控制器启动任务归属 + 异步停用
推进。共享层停用状态机（PR-02 契约）此前无生产任务来源。

## 交付内容

| 文件 | 变更 |
| --- | --- |
| `files_plugins_controller.py` | ① **启动即任务**：`launch` 把每次插件调用登记为归属该插件的 lifecycle 任务（`jobs.start(job_id, plugin_id=ui_id)`）；transport 应答到达（`_launch_finished`）确认终态（正常=finished、取消请求中=cancelled）。② **停新调用**：已进入停用流程的插件拒绝新调用（不排队，无第二个提交）。③ **`_retire_plugin_async`**：QTimer 轮询重入推进 `retire_plugin`（停新→按 owner 取消→等确认→释放贡献/关窗）——**GUI 不 sleep**；~5s 上限后如实报告"停用未完成（可重试）"，不假装 stopped。④ `shutdown` 对在途启动任务走同一异步推进 |

## 生命周期链路（生产语义）

```
launch(ui_id) ──jobs.start(owner=ui_id)──▶ 任务 running
停用 ──retire_plugin(wait_jobs=False)──▶ 停新 + cancel_owned
     ├─ 应答未到：任务 cancel_requested → fully_stopped=False（贡献保留）
     └─ 应答到达：_launch_finished 确认 cancelled → 再次推进 → 清理完成
```

## 验证

`tests/ui/test_native_files_plugins.py` +2（共 27）：
- **启动=归属任务**（拦截应答制造在途）：owner 断言、running 态；停用
  时任务转 cancel_requested、`fully_stopped=False`、任务名列
  `jobs_still_terminating`、停用中插件拒绝新调用（提交数仍为 1）；
  应答到达 → cancelled → 再推进 → `fully_stopped=True`；
- **异步退役**：QTimer 推进到 `is_stopped`（无 GUI sleep）。

回归：tests/plugins + 全部 files 域 + native 插件 UI —— **689 通过**
（一次 tags_clear 顺序抖动为仓库既有类，隔离复跑双绿）。

## 诚实边界

- 任务取消是**协作式**：HTTP 启动请求发出后无法真正中断，"取消"语义
  = 停新 + 等应答确认（任务不确认终止则贡献/停用不假完成）。
- 后台长任务（真正可中断的 worker）尚无生产来源——G 阶段插件宿主的
  作业接同一 `PluginJobs` 归属协议。
- 关停路径的异步推进受应用退出兜底（进程结束），轮询上限内未完成的
  停用如实记日志。

## 隐私

合成路径/拦截式 transport；无隐私路径。
