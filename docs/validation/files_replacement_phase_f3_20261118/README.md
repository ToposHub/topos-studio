# F 阶段第三批（F3）：command 唯一启动入口——库 revision 与 deadline 复核

日期：2026-11-18 · 分支：fix-ui · 范围：`LegacyFilesCommandService` 的
复核面扩展 + `FilesFacade.revision`/`NativeFilesTransport.library_revision`
+ 控制器接线。PR-05 已把全部入口（启动器/中心/快捷键/工具栏/右键）
统一到 prepare→revalidate→dispatch；本批补齐该管线的库代数与期限复核。

## 交付内容

| 文件 | 变更 |
| --- | --- |
| `plugin_launch_service.py` | ① `prepare` 冻结 `entry_revision`（宿主 revision_provider 提供）+ `deadline`（手势后 30s 预算，`timed_out()` 拒绝过期调用）。② `revalidate` 增 `_check_revision`：手势后重扫/切库（revision 漂移）→ `LIBRARY_REVISED`（可重试）拒绝重发。③ `dispatch` 在提交前对**已复核**调用也比对库代数（复核→提交间漂移同样拒绝，过期调用绝不发出）。④ revision 为 0/None = 宿主未提供——如实跳过该项，不冒装已复核 |
| `facade.py` / `native_files_transport.py` | `FilesFacade.revision` 公开属性；`library_revision()`（facade 未就绪返回 None） |
| `files_plugins_controller.py` | 命令服务接线 `revision_provider=transport.library_revision` |

## 语义

* **统一复核面**：所有入口的每次启动都经"身份/取消/过期（PR-05 既有）
  + 库代数（本批）"复核；直接调 dispatch 的入口无法绕过（服务内补做）。
* **选择一致性**：冻结选择 + 库 revision 不变 ⇒ 手势时的文件清单未被
  本系统改动（经我们服务的移动/删除/重扫都 bump revision）。外部文件
  系统改动不 bump——插件工具自身会面对文件缺失，属工具侧行为。
* **过期调用零外发**：`LIBRARY_REVISED`/`INVOCATION_EXPIRED` 拒绝时
  transport 零调用（测试断言 `transport.calls == []`）。

## 验证

`tests/files/test_plugin_launch_service.py` +5（共 28）：
prepare 冻结 revision+deadline；revalidate 拒绝漂移（LIBRARY_REVISED、
retriable）；dispatch 拒绝复核后漂移且零外发；无 provider / provider 返
None 两种"宿主未提供"都如实跳过不冒装。

回归：`tests/plugins` + 插件 catalog/声明式 manifest/启动服务 + files
domain/facade + native 插件 UI —— **500 通过**。

## 诚实边界

- **权限复核未接**（F 表第 9 项 broker 批次）：launch 管线尚无权限模型
  参与复核；InvocationContext.authorization 槽位已预留。
- 复核与调度不在一个锁内原子完成（F 表第 4 项：revalidate→dispatch 间
  仍有小窗口，本批以 dispatch 前再比对收窄；真正的原子边界在第 4 项）。
- 库 revision 覆盖"经本系统的变更"；外部文件系统直接改动不 bump。

## 隐私

合成路径与假 catalog/transport；无隐私路径。
