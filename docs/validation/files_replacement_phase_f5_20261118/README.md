# F 阶段第五批（F5）：宿主会话——window owner/singleton/context + 贡献注册注销

日期：2026-11-18 · 分支：fix-ui · 范围：`src/shared/plugins/host_session.py`
（新）+ 控制器快捷键贡献所有权接线。共享层 lifecycle（激活半失败全量
回滚/可重试停用，PR-02 契约锁定）此前零生产消费。

## 交付内容

| 文件 | 变更 |
| --- | --- |
| `host_session.py`（新） | ① **`WindowRegistry`**：`(plugin_id, window_id)` 归属 + singleton（第二次激活 **focus 现有**，不开新窗——并发下恰好一次 opened）；`close_all(plugin_id)` 按归属关窗；**context 代数更新**（`expected_generation` 落后拒绝——迟到旧代更新不污染新窗口；窗口不存在拒绝不冒充成功）。② **`PluginHostSession`**：组合 lifecycle（`close_windows` 回调接注册表）+ 可整组替换的**快捷键贡献集**（`replace_shortcuts`：旧集先释放、**释放失败保留在 `_failed_sets` 供重试**——所有权不随替换丢失）；`release_contributions`（restart-bound 停用：释放贡献+关窗，不进 lifecycle 不可逆停用）；`retire_plugin`（全量退役：贡献+窗口+停用状态机，如实报告）。③ 激活半失败经真实会话组合：registrar 抛错 ⇒ 已注册贡献逆序全量回滚（共享层契约在宿主会话下复核） |
| `files_plugins_controller.py` | 快捷键 QAction 的注销**只由释放回调承载**（单一所有权路径——删除原列表式第二条清理）；`replace_shortcuts` 整组替换；停用（restart-bound）即 `release_contributions`（快捷键整组注销 + 名下窗口关闭）；`shutdown` 全量退役；暴露 `host_session` 供 G 阶段窗口宿主消费 |

## 验证

- `tests/plugins/test_host_session_f5.py`（新，11 项）：singleton 二次
  聚焦/非 singleton 多窗/归属关窗/关闭后重开；context 代数（旧代拒绝、
  不存在拒绝、latest 可见）；快捷键替换释放旧集、**释放失败保留可重试**
  （重试后清零）；`release_contributions` 快捷键+窗口全释放；**激活半
  失败完整回滚**（逆序释放、可再次激活）；`retire_plugin` 如实报告；
  4 线程并发 singleton（恰好一次 opened）。
- `tests/ui/test_native_files_plugins.py` +1：真实控制器——快捷键经会话
  持有所有权，停用即整组注销（动作失效）。
- 回归：tests/plugins + 全部 files 域 + native 插件 UI —— **689 通过**
  （修复途中暴露的双重清理路径竞态：删除列表式第二条删除路径）。

## 诚实边界

- **窗口注册表尚无真实窗口宿主消费**：当前 Files 原生宿主不拥有插件
  窗口（工具窗口由旧服务创建）——注册表/owner/singleton/context 全部
  就绪并有并发/代数测试，G 阶段逐插件接管时接入（`host_session` 已暴露）。
- 事件/菜单贡献的**生产注册面**未接（原生宿主当前只有快捷键一类真实
  贡献）；lifecycle 停用状态机的生产触发（F6）待作业归属接线。
- 旧 Blueprint/JS 注册（旧服务侧）不在本批——G 阶段逐项替换。

## 隐私

合成回调/线程栅栏；无隐私路径。
