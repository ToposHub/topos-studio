# F 阶段第九批（F9）：Files 实际资源 broker——授权执行/命名空间/secret 代理

日期：2026-11-18 · 分支：fix-ui · 范围：`src/features/files/plugin_broker_host.py`
（新）+ `host_session` 接线。共享契约（broker.py 的
BrokerDecision/PathGrant/NetworkGrant）此前只有 AI harness 消费。

## 交付内容

| 文件 | 变更 |
| --- | --- |
| `plugin_broker_host.py`（新） | ① **`FilesPluginBroker`**：路径/网络授权执行（决策与 I/O 分离、default deny）——**声明权限 ≠ 已强制执行**：manifest 声明（policy 参数）只是额外门，授权只来自 grant（声明 network 无 grant 照样 NO_GRANT；有 grant 无声明 → POLICY_DENIED）。路径 grant 按真实根 `resolve`（`..`/根外逃逸拒绝）+ 读写分权；网络 grant 域+scheme 精确（子域显式 opt-in）、URL userinfo 一律拒绝（走 secret 代理）。**库切换撤授权**：库作用域 grant 绑定签发纪元，`on_library_switched` 推进纪元即失效（全局作用域不受影响；新库须重新授权）。② **`PluginConfigStore`** 配置命名空间：按 plugin_id 物理分区 JSON（原子写）；键逃逸（`../`、绝对键、UNC）拒绝；`clear_namespace` 只清自己的数据（卸载代码/数据/凭据分开）。③ **`SecretProxy`**：明文只在宿主侧（`register`）；插件按名引用，`resolve_for_host` 要求该插件被 grant 该 secret（引用≠授权），结果交**宿主执行层**（带凭据 I/O 由宿主发起）——secret 永不进插件上下文 |
| `host_session.py` | 会话持有 broker/secrets（config_store 注入式）；`release_contributions`/`retire_plugin` 整体撤权（`grants_revoked` 计数）+ retire 清配置命名空间（`config_entries_cleared`） |

## 验证

`tests/files/test_plugin_broker_f9.py`（新，14 项）：
- 声明≠授权：无 grant 即拒（即便 manifest 声明）；POLICY_DENIED（grant
  有/声明无）；grant+声明 → 通过；
- 路径：包含性/`..` 逃逸/根外/只读拒写/写 grant/BAD_MODE；
- 网络：域精确/子域 opt-in/scheme 精确/userinfo 拒绝；
- 切库：库作用域 grant 全撤（全局保留）、新库重签恢复；停用 revoke_all
  幂等；
- 命名空间：分区隔离/键逃逸三种拒绝/清数据只清自己；
- secret：授权门控解析/未授权 None/撤权后 None（明文留存宿主可再授权）；
- 宿主会话：退役撤 2 项 grant + 清 1 条配置。

回归：tests/plugins + tests/files + native 插件 UI + **tests/ai**（共享
契约消费方）—— **1351 通过**。

## 诚实边界

- **无生产 I/O 消费方**：当前 Files 插件（旧服务侧）不经本 broker 走
  文件/网络——G 阶段新宿主执行层据 ``BrokerDecision`` 行动；本批交付
  强制执行边界与锁定测试（声明权限在这条边界上确实不构成授权）。
- 库切换触发点（`on_library_switched`）尚无生产调用——原生库切换 UI
  到位后接线（H 阶段库管理范围）。
- secret 的注册来源（用户录入/钥匙串）未做——代理语义与授权门已锁定。

## 隐私

合成路径/URL/凭据；无隐私路径。
