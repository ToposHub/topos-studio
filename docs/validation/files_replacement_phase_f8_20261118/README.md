# F 阶段第八批（F8）：provider 执行边界 + Inspector 异步装配/世代失效

日期：2026-11-18 · 分支：fix-ui · 范围：`src/shared/plugins/providers.py`
（新）+ `host_session` 接线。manifest 的 `FormatProviderDeclaration`
（PL-05）此前只有声明模型，没有运行时。

## 交付内容

| 文件 | 变更 |
| --- | --- |
| `providers.py`（新） | ① **`ProviderRegistry`**：注册携带 owner（同 owner 同 id 换代覆盖）；`unregister_owner` 整体摘除（接 F6 停用）；**确定性优先级**（priority 升序，同级先注册先服务）；**冲突解释**——`Resolution.conflicts` 逐个落选者给出"谁赢/差在哪"，不静默；目标归一（`.mov`/`MOV`/`clip.mov` 同义）；空 formats+magic = 声明型兜底（具体格式声明优先）。② **缓存签名**：`cache_key` = `r<revision>:候选 owner:provider:签名`——任一候选增删/换代/签名变化即失效。③ **错误类别真实执行**：`ProviderError.kind` ∈ unsupported（终态，**绝不绕道**）/ transient（同 provider 重试一次，仍抖动则按序回退）/ fallback（换下一个候选）/ cancelled（尊重取消不回退）；`run()` 按优先级链执行——只有 fallback/transient-耗尽回退，全链失败抛最后错误；无候选 = unsupported。④ **`ProviderInspector`**：异步装配目标可由哪些 provider 处理（可用性+错误类别逐项）；**世代失效**（晚到的旧代结果不回调）；transient 每 provider 有界重试一次；单 provider 崩溃不炸装配 |
| `host_session.py` | 会话持有 `providers` 注册表；`release_contributions`/`retire_plugin` 摘除该 owner 的 provider（停用=能力下线，计数入报告） |

## 验证

`tests/plugins/test_providers_f8.py`（新，16 项）：
- owner 注册/摘除幂等/摘除后解析易主；
- 优先级解析 + 冲突解释文本（谁/为什么/差在哪）；同级确定性；格式与
  magic 匹配 + 声明型兜底让位；未知 provides = 调用方 ValueError（不是
  "无 provider"）；
- 缓存键稳定性/候选注册失效/签名换代失效/owner 摘除失效；
- **回退类别**：fallback 按序回退成功；unsupported 终态（健康 provider
  在场也不绕道——断言第二个 executor 从未被调用）；transient 重试一次
  自愈 / 重试耗尽回退；cancelled 不回退；无候选 unsupported；
- Inspector：同步执行器下装配可用性+错误类别；**旧代世代丢弃**（旧代
  晚到不回调）；transient 装配重试；
- 宿主会话：退役/贡献释放都摘除 provider（`providers_removed` 计数）。

回归：tests/plugins + native 插件 UI —— **454 通过**。

## 诚实边界

- **无生产 provider 注册**：当前十插件只有 manifest 声明，没有注册执行
  体的宿主代码——G 阶段 `video_pro`/`topos_image` 接管时消费本注册表
  （`host_session.providers` 已暴露）；本批交付执行边界与锁定测试。
- Inspector 结果不含探测负载（仅可用性/错误类别）；装配 UI 视图未做。
- 回退不携带跨 provider 的权限语义（grant 模型待 F9 broker）。

## 隐私

合成声明/执行器；无隐私路径。
