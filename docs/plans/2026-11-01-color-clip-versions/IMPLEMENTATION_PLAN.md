# 调色片段版本菜单：实现与后端交接计划

日期：2026-11-01。项目：Topos Studio（PyQt6 + ModernGL + GLSL）。

**最新复核：** 本计划交接完成后再次检查，发现并修复生产时间线引用、原子事务、快照残留、全局撤销、保存回调、共享预览、分割片段隔离与即时手势保存等遗漏。完整聚焦回归 466 项通过，另补 1 项回显回归通过；真实 2K 素材桌面操作链通过并正常退出。准确结果和未覆盖的 4K/导出测量见 [复核与修复记录](VALIDATION_2026-11-01.md)。

## 1. 交付范围与当前边界

本轮只完成调色页片段右键版本管理的 UI、对话框交互、请求契约与界面验证。后端命令补齐、真实工程写入、撤销重做接线、引擎与片段缩略图同步由后续同事完成。本文件是其实施清单，不代表清单内后端工作已经交付。

视觉与流程参考达芬奇调色页。本轮实际观察到原生 Color → Grade Version 菜单中的 Previous、Next、Add、Default；右键多级菜单未成功完整展开，因此本计划不宣称所有层级都已实操核验。官方 [Resolve 20 Colorist Guide](https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-20-Colorist-Guide.pdf) 中的 Local Versions → Create New Version、版本重命名流程也作为依据。本项目的复制、跨域、保护和确认规则是明确的实现约定，后续如要求逐项一致，应另做原生行为对照。没有逐项核验的功能不得标记为“完全与 Resolve 一致”。本轮 UI 的菜单、命名校验、禁用态、取消、待提交、错误和未接线反馈应真实可操作；提交后不能用临时 UI 数据伪装成工程已产生版本。

约束依据：[.clinerules](../../../.clinerules)。优先复用现有不可变工程、命令栈、ColorStack、hydrate 和渲染队列；UI 不直接写工程或执行 GPU 操作，不同步解码视频，不新增第二套版本快照存储。

**本轮状态（2026-11-01 后端交接完成）：UI、请求/回填协议与生产接线均已完成——片段右键版本菜单经 `ClipVersionRequestHost`（[clip_version_host.py](../../../src/app/controllers/clip_version_host.py)）连接权威工程、真实媒体池命令栈与回填链路（接线点在 [dock_layout_controller.py](../../../src/app/controllers/dock_layout_controller.py) 片段条连接块）。BE-01～BE-04、WIRE-01/02、CACHE-01、IO-01、DIAG-01 已交付并附测试证据；QA-01/PERF-01 完成可无头自动化的矩阵行与规模测量（真机 4K/GPU 实测与真实素材实操验收建议后续在桌面环境执行一次并回填记录）。任务状态见第 6 节。**

## 2. 已确认的资产与证据等级

### 2.1 已阅读源码确认

| 位置 | 现有能力 | 后续复用方式 |
|---|---|---|
| [project.py](../../../src/features/edit/core/project.py) | `EditProject.color_stack_versions`、`with_color_stack_versions(table)`、`with_color_stacks(stacks)` | 继续使用唯一版本表与不可变替换；不在 UI 建镜像存储 |
| [color_versions.py](../../../src/features/edit/core/color_versions.py) | 保存、激活、删除、共享锁、版本读取、参数比较、依赖镜头查询 | 在现有模块补命令与一致性校验 |
| [commands/__init__.py](../../../src/features/edit/core/commands/__init__.py) | `ProjectCommandStack`、项目宏命令、`ProjectTransactionResult` 消费 | 所有版本事务进入真实项目命令栈 |
| [color_commands.py](../../../src/features/edit/core/commands/color_commands.py) | `UpdateColorStackCommand` 拒绝直接写锁定栈 | 保留现有共享 look 锁语义 |
| [edit_project_io.py](../../../src/features/edit/core/edit_project_io.py) | `.topos` settings 写入/读取 `color_stack_versions` | 不另造文件或 QSettings 版本库 |
| [serialization.py](../../../src/features/edit/core/serialization.py) | 版本表深拷贝保存与恢复，旧工程缺键为空表 | 保留未知 payload，覆盖保存/加载往返 |
| [m07_version_tools_panel.py](../../../src/features/edit/ui/m07_version_tools_panel.py) | `ColorVersionToolsPanel` 保存、激活、比较、共享锁与 executor 回读 | 复用数据语义、诊断与命令出口；协调两处 UI 的刷新 |
| [menu_toolbar_controller.py](../../../src/app/controllers/menu_toolbar_controller.py) | `on_open_version_tools_panel()` 现有菜单入口 | 保留原工具，片段入口使用明确目标身份 |
| [upgrade_panel_host.py](../../../src/app/controllers/upgrade_panel_host.py) | 生命周期、工程守卫、项目命令 executor | 复用宿主机制并补齐版本请求的过期检查 |
| [timeline_clip_strip.py](../../../src/features/color/ui/timeline_clip_strip.py) | 片段右键入口、活动片段、原始/调色缩略图及信号 | 本轮 UI 入口，后续只接公开请求/回填接口 |
| [edit_controller.py](../../../src/app/controllers/edit_controller.py) | 活动调色手势提交、调色栈快照、片段预览同步 | flush、事务提交与预览一致性接线 |
| [page_controller.py](../../../src/app/controllers/page_controller.py) | 片段上下文、源帧映射、ColorStack hydrate | 激活版本后重新加载正确片段与栈 |

### 2.2 已实测的阻塞（BE-01 已修复）

2026-11-01 使用项目 `.venv/bin/python`，只创建内存中的 `EditProject` 和 `ColorStackData`：先保存版本 A，再运行真实 `ProjectCommandStack().execute(ActivateColorVersionCommand.create("s", "A"), project)`。结果为：

```text
TypeError: replace() should be called on dataclass instances
```

原因：`ActivateColorVersionCommand.apply()` 返回 `(new_project, applied_command)`；`ProjectCommandStack.execute()` 只识别 `ProjectTransactionResult`，普通分支把二元组当作工程交给 `dataclasses.replace`。现有面板 fake executor 测试会手工拆二元组，不能证明真实命令栈可激活。后端接线前必须解决，不能在 UI 中拆包绕开真实 undo 栈。

**修复记录（同日）**：`ActivateColorVersionCommand.apply` 改返回 `ProjectTransactionResult`（project + 含激活前/后快照的 updated_command）；`ProjectCommandStack` 三个分支统一经 `consume_project_result`（`commands/_support.py`）归一，顺带兼容裸二元组；`ProjectMacroCommand.apply/undo` 同步消费结构化子命令结果并以 `object.__setattr__` 保留更新后的子命令（宏内激活的 undo 快照不再丢失，失败逆序回滚）。M07 面板与相关测试改走新协议。真实栈 save→modify→activate→undo→redo、revision 单调、失败零提交由 [test_color_version_command_stack.py](../../../tests/edit/test_color_version_command_stack.py) 验证（23 项通过）。

这次 probe 没有写工程文件，没有验证实际 viewport、导出或持久化。本文列出的原有测试文件已确认存在，但本次调研没有重新运行全套测试；其存在不等于本轮验收通过。

### 2.3 尚未完成的能力（后端交接后的更新）

- ~~片段版本菜单到项目命令、engine、缓存的完整接线尚未核验。~~ → WIRE-01/02 已接线（见第 6 节状态）。
- ~~现有后端没有重命名版本、复制指定版本命令。~~ → `RenameColorVersionCommand` / `DuplicateColorVersionCommand` / 跨栈 `CopyColorVersionCommand` 已交付。
- 现有版本表没有 local/remote 两套版本域、版本稳定 ID、排序索引或版本缩略图字段。→ 按第 3.2 节决策：域为**解析层概念**（`color_version_domains.py`），不新增表字段；`default_version` 键为唯一新增（BE-04，深拷贝序列化自动保真）。稳定 ID/顺序/缩略图字段仍留给后续 schema 版本。
- 保存快照之后修改 live 栈，不会自动改写该版本快照；`active` 指针本身不能证明 live 内容仍与该快照一致。
- 现有 `compare_versions` 只比较主调参数和插件集合，不能称为完整栈差异或图像 A/B 对比。

## 3. 复用的版本模型与命令

当前唯一存储形态如下，版本名就是版本表的键：

```python
color_stack_versions = {
    stack_id: {
        "active": version_name_or_none,
        "locked": False,
        "versions": {
            version_name: {
                # ColorStackData.to_dict() 的完整快照
                "plugin_versions": {plugin_id: plugin_version},
                "saved_at": timestamp,
                # 可选：plugin_check_unverified
            },
        },
    },
}
```

不得把 UI 缓存、名称列表或临时对话框数据作为第二个真相来源。UI 的占位默认版本若仅用于展示，必须明确标为未提交状态，不能写进项目、QSettings 或磁盘。后端成功回填的版本列表才代表工程事实。

### 3.1 现有准确接口

| 接口 | 语义 |
|---|---|
| `versions_state(project, stack_id) -> dict` | 读取 active、locked、versions；缺省为空表 |
| `SaveColorVersionCommand.create(project, stack_id, version_name, *, replace=False)` | 保存当前 live 栈快照；去首尾空白；空名/重名拒绝；保存后 active 指向新名 |
| `ActivateColorVersionCommand.create(stack_id, version_name)` | 内容互换；保留栈身份字段；当前 apply 返回工程与 applied command 二元组，协议待修复 |
| `DeleteColorVersionCommand.create(project, stack_id, version_name)` | 删除快照；删除 active 时 active 置 None；当前 live 栈内容不变 |
| `SetStackLockCommand.create(project, stack_id, locked)` | 显式锁定/解锁；不等同于版本 local/remote 域 |
| `stack_dependent_clips(project, stack_id)` | 返回引用该主栈或 trim 栈的 `(timeline_id, clip_id)` 列表 |
| `shared_stack_ids(project)`、`is_stack_locked(project, stack_id)` | 共享引用与锁状态读取 |
| `compare_versions(project, stack_id, name_a, name_b)` | 主调参数差异与插件集合差；纯读操作 |

版本激活保留 `stack_id/asset_id/scope/source_kind/timeline_id/clip_id/created_at`。多个镜头引用同栈时，激活内容影响所有这些镜头，不能只更新右键镜头的 UI 并假装其他引用不受影响。既有 private trim 栈需保持不变。

### 3.2 待补命令与模型决策（均已定案并交付，语义文档见各模块 docstring）

1. **Rename（已交付）**：`RenameColorVersionCommand` 在同一版本表中原子替换名称键，保留 payload、插件清单与 saved_at，版本顺序不变（原位换名）；active/default_version 指向旧名时同步改名；空名/重名/同名拒绝；undo 恢复旧表。不是"保存 live 栈再删除旧名"。
2. **Duplicate（已交付）**：`DuplicateColorVersionCommand` 深拷贝指定版本 payload 到新名（可复制非当前版本），追加表尾、保留 active 与 live 栈、不自动激活。跨栈复制另有 `CopyColorVersionCommand`（COPY_TO_SCOPE 核心：源表只读快照 + 目标表原子写入，undo 只还原目标表）。
3. **Local/Remote（已定案）**：版本域是**片段视角的解析层概念**（[color_version_domains.py](../../../src/features/edit/core/color_version_domains.py)），不写入工程 schema、不与 `ColorStackData.scope` 混用。LOCAL = 片段私有栈（scope=clip_local；先取当前引用，切共享后按属主盖章找回遗留私有栈；legacy 无盖章裸栈按引用归属；trim 栈永不属于任何域）；REMOTE = 素材共享栈（scope=asset_default 且 asset_id 匹配，做 scope 校验，不用 `get_color_stack_for_asset`）。**域判定以被引用栈 scope 为权威**，`color_shared` 仅交叉校验。换域（LOAD 另一域版本）= 经既有时间线命令 `UpdateClipColorStackCommand` 切换片段引用 + 项目层激活命令，禁止只改 UI 的 scope；版本驱动的切域不自动锁共享栈（与显式 Use Shared Grade 的自动锁区分）。
4. **未保存修改（已定案）**：提交依赖 live 内容的操作前，宿主仅当面板正在编辑目标栈（或空域建栈时的内容源栈）才 `trigger_save()` flush（真实 `_flush_active_grading_stack_before_switch` 的"切走前 flush"语义另用于跨域加载）；flush 后 live 仍偏离 active 版本时，LOAD 必须 `discard_unsaved=True`（UI 确认覆盖），否则拒绝；激活=内容互换即受控覆盖，不无条件改写已命名快照。
5. **版本身份（已定案）**：沿用名称键即 version_id（§4.2 允许的旧表适配形态）；BE-04 新增表级 `default_version` 键（唯一受保护默认版本标记，深拷贝序列化自动保真，旧工程缺键=None——DELETE_ALL 由宿主在同一请求内先经 `promote_default_version` 迁移：优先提升当前 active，无 active 按 saved_at 最早）。稳定 version_id、顺序、两域字段仍留给后续 schema 版本，先迁移再启用。
6. **默认版本初始化策略（BE-04 定案）**：空域首个 CREATE 在同一事务初始化默认版本（`DEFAULT_VERSION_NAME="Version 1"`）+ 保存用户命名版本（`create_version_with_default` 宏：init 先纯函数 apply 得中间态、Save 在中间态上 create，保证 before 快照与宏 apply 顺序一致）。初始快照 = 初始化时栈的当前 live 内容（与 Resolve 语义一致——首个版本即起始调色，非"未调色重置"）；空域栈不存在时先按域盖章建栈（内容 = 镜头当前 live 调色，两域策略一致），COPY_TO_SCOPE 的空目标域初始化沿用同一策略。

## 4. UI 与后端的交接合同

### 4.1 已交付文件与操作

- [clip_version_ui.py](../../../src/features/color/ui/clip_version_ui.py)：纯 UI 的不可变显示 DTO、菜单、命名/确认对话框、请求和回填生命周期。
- [timeline_clip_strip.py](../../../src/features/color/ui/timeline_clip_strip.py)：右键命中卡片后接入本地/远程版本菜单；空白处不会借用旧 active clip 执行版本操作。
- [test_clip_version_ui.py](../../../tests/color/test_clip_version_ui.py)：请求、校验、失败、超时、跨域、过期回填与目标归属测试。
- [preview_version_ui.py](preview_version_ui.py)：独立交互演示；内存模拟回填只在此脚本内，不打开真实工程、不保存调色数据。生产 UI 没有模拟成功处理器。

已实现菜单：本地版本、远程版本（同素材共享的版本域，不表示网络存储）；创建、刷新列表、版本列表、当前实际使用版本的 ✓、每版本的加载/重命名/复制/删除/复制到另一域、删除全部其他版本。所有条目按 capability 和 pending 状态禁用。默认、当前记忆版本和最后一个版本禁止单独删除；删除全部需确认“保留并加载默认版本”。

创建/重命名/复制/跨域复制具有命名框；删除/删除全部具有确认框；live_modified 时加载另一个版本先确认覆盖。输入框支持 Enter、取消按钮和 Escape；提交等待回填，失败保留名称可重试。名称去首尾空白、区分大小写、同域重名拒绝、最多 128 字符、禁止控制字符。128 字符策略是本轮 UI 约束，后端还需统一实现，不能只信任前端。

片段底部可通过 Bottom Label → Version Name 切换到版本名；双击底部在编码与版本名之间切换。远程域附 `(R)`，live_modified 附 `*`。这些显示只来自宿主回填，不推断快照与 live 栈是否相同；宿主需准确计算两个字段。

### 4.2 准确数据合同

所有类型均定义于 `clip_version_ui.py`，不传完整 ColorStack 或 GPU 对象：

| 类型/字段 | 准确含义 |
|---|---|
| `ClipVersionTarget(project_id, timeline_id, clip_id, asset_id, stack_id)` | 右键时固定目标；无栈时 stack_id 可为 None，后端负责按策略创建/拒绝 |
| `VersionScope.LOCAL / REMOTE` | 值为 local/remote，与 ColorStack.scope 分离 |
| `ClipVersionSummary(version_id, name, is_default=False, diagnostic="")` | 稳定标识与显示名分离；旧表适配时可暂用名称作为 ID，rename 后必须回填新 ID |
| `ClipVersionCollection` | target、scope、versions 元组、active_version_id、revision、loaded、connected、live_modified、in_use、capabilities、unavailable_reason |
| `loaded / connected` | 未读列表与已读空列表必须区分；connected 为宿主是否能受理操作，不能为展示假设为 True |
| `in_use` | 该片段当前实际使用的域；两个域可各自记住 active，但只有 in_use 的 active 显示 ✓；另一个域记住的 active 仍可加载以切回 |
| `capabilities` | frozenset[VersionOperation]；宿主只开放已实现命令，未实现的条目禁用 |
| `ClipVersionRequest` | request_id、target、scope、operation、expected_revision、version_id、name、destination_scope、discard_unsaved |
| `VersionOperation` | CREATE、LOAD、RENAME、DUPLICATE、DELETE、DELETE_ALL、COPY_TO_SCOPE，对应小写枚举值 |
| `ClipVersionResult(request_id, target, collection=None, error="")` | target 必须是原请求目标；成功带权威 collection，失败带用户可读 error |

`versions` 必须为 tuple、capabilities 为 frozenset；revision 非负，version_id 唯一，非空 active 必须存在于列表。`expected_revision` 是请求基准修订号；异步读取也必须核验工程、timeline、clip、asset、stack 与 revision，不能把旧读结果刷新为 connected 新状态。

### 4.3 片段栏公开接线接口

```python
# 读取请求：target: ClipVersionTarget，scope: VersionScope
strip.version_state_requested.connect(host_read_versions)
# 写入意图：request: ClipVersionRequest
strip.version_operation_requested.connect(host_execute_version_operation)

# 读取回填：经 controller 从权威工程读取完整显示集合
accepted = strip.set_clip_version_collection(collection)  # -> bool
# 事务完成回填；信号处理回到 UI 线程后调用
accepted = strip.finish_clip_version_operation(result)    # -> bool
```

`set_clip_version_collection()` 只接收当前片段条目中身份/绑定匹配的目标，不负责写工程。菜单每次展开和显式“刷新版本”都会发读取请求。没有宿主连接时保持明确未接线态，不派发写请求或伪造默认 Version 1。

提交跨域复制前，UI 会请求目标域列表；目标域未 connected/loaded 时拒绝派发，避免用空列表跳过名称冲突。复制操作指定源 version_id、新 name、destination_scope；后端复制指定快照，不复制 live 栈，不自动切域。源集合在结果中回填，目标集合另以 `set_clip_version_collection()` 回填，两者来自同一次原子提交。

### 4.4 状态机、回填与生命周期

流程为 idle → pending → acknowledged / rejected / stale / timeout。一个 target 的两域写操作串行；不乐观修改 active、版本列表、grade 标记或工程。15 秒无回填自动结束 UI 等待并显示超时，允许刷新核对；超时不代表后端撤销事务，不能盲目重新提交同一创建命令。

- request_id 唯一；错误 token/目标回填拒绝；已完成、已超时、取消交互后的迟到 ack 不应用。
- 切工程/时间线清显示缓存；切活动片段关闭版本表单并作废 UI pending。已派发事务是否仍提交由宿主控制，返回当前界面时应重新读取权威工程，不能把关闭表单等同回滚。
- 成功 collection.revision 不得低于请求基准，也不得覆盖已显示的更新列表。后端应进一步做真正的并发校验；UI 守卫不是事务锁。
- CREATE、LOAD、DELETE_ALL 成功可返回重新绑定 stack 的 collection，但 project/timeline/clip/asset 必须相同，result.target 仍使用旧请求目标。宿主先发布新工程/片段绑定，再回填结果；其他操作必须保持目标完全一致。
- 成功加载某域应回填 `in_use=True`，UI 清掉另一域的 in_use。不要让两个集合竞争报告实际域。
- 后端失败回填 error；对话框不关闭，输入保留。同步立即回填和异步回填均支持；关闭对话框不取消已提交命令。
- UI 的 timeout 与销毁路径清 timer；菜单刷新销毁旧子菜单，防止反复打开积累对象。宿主仍须取消自己的订阅/解码任务。

### 4.5 后端实施时必须遵守的操作语义

| 操作 | 本轮 UI 约定与后端要求 |
|---|---|
| CREATE | 保存 flush 后的 live 栈为新版本并激活；若为空域，在同一事务初始化默认版本，明确初始快照策略 |
| LOAD | 加载指定快照并切到该域；有 live 未保存变化需要 discard_unsaved=True；重新解析目标绑定并核验 revision |
| RENAME | 原子改名，不改变快照内容、顺序与实际调色；旧名为 ID 时更新 active ID |
| DUPLICATE | 拷贝指定快照为新名，保留原 active 和当前 live 栈，不自动激活副本 |
| DELETE | 禁止删除 default、该域 active 和最后一个版本；删除非当前快照不改变 live 栈 |
| DELETE_ALL | 新命令/宏事务：保留 default、删除其他快照、加载 default；UI 已明确确认，现有逐个 Delete 不足以表达此行为 |
| COPY_TO_SCOPE | 从指定源快照复制到另一域新名；保留实际使用域与 live 栈；目标域为空时默认初始化策略必须一致 |

初始化、域迁移、stable ID、顺序的最终存储方案由后端实施者确认并记录。没有默认版本的旧数据不得直接执行 DELETE_ALL；宿主应先迁移或移除该 capability 并说明原因。不加 Ctrl+Y 等全局快捷键，避免冲突现有 redo；菜单自身保留标准键盘导航。

## 5. 后端实施步骤

### 步骤 A：修复真实命令结果协议

让激活命令返回项目栈已经支持的 `ProjectTransactionResult`，或在项目栈统一支持 `(new_project, applied_command)`，二者择一并检查所有 execute/undo/redo 分支。关键是 undo 栈保存 apply 后的命令，因为其中含激活前内容与 active 指针。不能只取 new_project 丢掉 applied command。

使用真实 `ProjectCommandStack` 测试 save → modify live → activate → undo → redo。确认 revision 每次单调推进、失败不压入历史、undo/redo 内容和 active 指针对称。

### 步骤 B：补业务命令与域解析

在既有 `color_versions.py` 附近实现 Rename/Duplicate，命令创建时读取 before、apply 时校验当前版本表，防止 stale 对话框覆盖新修改。名称校验由后端再次执行，不依赖 UI。

建立 local/remote 域到本地栈/共享栈的明确解析服务。先读取目标 clip 与 asset 的现有绑定，识别共享引用和锁，再决定新建栈或切换引用；所有项目/时间线层变更走已有 command/service，保持两种不可变快照与序列缓存一致。

### 步骤 C：接入 controller 的单一请求出口

controller 接收不可变 UI 请求后检查工程身份、revision、timeline/clip 存在、stack 绑定、操作和版本名；若目标已删除或引用变化，返回 stale。不要使用 `_active_color_stack_id()` 的“任意首栈”fallback 作为片段目标。

版本请求如需依赖当前 live grade，应先 flush 该 clip 对应手势，避免保存上一个镜头或默认参数。flush 绑定的 stack 必须与请求目标核对。写入走真实媒体池项目命令栈，并将新工程同时发布给主窗口、媒体池、时间线预览持有者与持久化入口。遵循已有 dirty/revision/信号流程，避免仅面板私有工程发生变化。

创建/重命名/删除仅改版本表的操作不应无条件重新解码整条时间线。激活、切域或复制后激活才需要执行内容加载和预览刷新。

### 步骤 D：hydrate、render 与缩略图一致性

激活成功后，仅当目标仍是当前 Color 会话时，将最新栈经既有 hydrator 写入 engine/panel；使用 `_color_stack_hydration_in_progress` 等防递归机制，避免回显又提交一条命令。重新 render 走既有渲染线程，不从 UI 调 OpenGL。

切换片段与版本共用现有源帧映射和 clip switch guard；验证解码结果对应 project/timeline/clip/stack 及 source frame generation，再更新画布。版本事务的 backend ack 与图像完成可分开回填：工程已提交后可显示版本成功，但缩略图继续显示“待更新”，不可用上个片段的画面代替。

片段 graded preview 目前按 clip_id 缓存，需建立失效依据：工程身份、clip 源范围、栈身份/内容 revision、版本域或内容指纹、截图 generation。改版本后失效所有依赖该共享栈的卡片；未调色重置应清除旧 override 并回到原始缩略图；Rename 不改变图像内容时可保留缓存。

异步 frame-ready、抓 viewport、延迟 timer、decode 结果要核验 token/generation。clip_id 相同但发生 trim、split、换源或换版本仍需拒绝旧结果。截图必须确认实际完成帧的归属，不能仅检查 strip active clip id；关闭窗口与换工程应取消订阅、清理 pending 引用。

保持现有懒加载、滚动防抖、播放期间缓存优先策略。不得在生成菜单时解析所有版本的完整图片或触发 GPU 预览。

### 步骤 E：与旧工具及持久化对齐

旧 `ColorVersionToolsPanel` 与新片段菜单读取同一权威版本表，任何一处写入后另一处刷新。确保拒绝回填不使用旧 `_project` 覆盖主工程。

继续使用现有 serialization / `.topos` settings；新增字段先写迁移与兼容测试。删除版本不删除当前 live grade 的既有语义应在 UI 中表达清楚，不能错误显示镜头恢复原始调色。

插件诊断应消费实际 applied command 与 payload。当前面板读取的 `missing_plugins_hint` 没有可靠后端写入来源；后续需统一结果字段。插件缺失时保留原 payload，明确“缺失”；注册表不可用时明确“未核对”，不能当作“无缺失”。版本锁允许版本管理，普通调色参数写锁定共享栈仍应按既有规则拒绝。

## 6. 任务表与完成定义

状态含义：`已完成` 附验证证据（文件+测试数）；`已完成（无头可测部分）` 表示自动化覆盖已交付、真机/真实素材验收建议后续在桌面环境补一次并回填记录。`待办` 不计入已交付。

| ID | 任务 | 依赖 | 状态 | 完成定义与证据 |
|---|---|---|---|---|
| UI-01 | 片段版本菜单与目标命中 | 无 | 已完成 | 右键明确命中 clip；空白处不误作用于旧 active（test_clip_version_ui.py） |
| UI-02 | 命名/重命名/复制/删除/列表交互 | UI-01 | 已完成 | 输入校验、取消、选中态、禁用态、键盘行为可验证 |
| UI-03 | 不可变请求、token、ack、未接线提示 | UI-01 | 已完成 | 不伪造工程成功；迟到 ack 不污染新上下文 |
| BE-01 | 激活结果协议与真实 undo 栈 | 无 | 已完成 | `ProjectTransactionResult` 协议 + `consume_project_result` 统一三分支 + 宏保留更新后子命令；真实栈 save/activate/undo/redo、revision 单调、失败零提交：[test_color_version_command_stack.py](../../../tests/edit/test_color_version_command_stack.py) 23 项通过（含宏内激活与部分失败回滚） |
| BE-02 | Rename/Duplicate 原子命令 | BE-01 | 已完成 | `Rename/Duplicate/Copy` 三命令 + 后端统一 `validate_version_name`（128/控制字符/去空白）；非当前版本正确复制、重名/stale 零提交、undo/redo 对称：同上文件 TestRenameDuplicateOnRealStack/TestCopyVersionAcrossStacks |
| BE-03 | local/remote 语义、绑定与兼容方案 | BE-01 | 已完成 | [color_version_domains.py](../../../src/features/edit/core/color_version_domains.py)（语义文档=模块 docstring + §3.2 定案）；域/scope 区分、scope 权威判定、trim 排除、共享影响、旧工程零迁移：[test_color_version_domains.py](../../../tests/edit/test_color_version_domains.py) 13 项通过 |
| BE-04 | 默认版本初始化与 DeleteAll 原子事务 | BE-01、BE-03 | 已完成 | `InitializeDefaultVersion` / `DeleteAllVersions`（表+live 栈单原子事务，undo 整体恢复）/ `promote_default_version` 迁移 / `create_version_with_default`；TestDefaultVersionAndDeleteAll 8 项通过 |
| WIRE-01 | 请求验证、flush、项目提交与回填 | UI-03、BE-01..04 | 已完成 | [clip_version_host.py](../../../src/app/controllers/clip_version_host.py) 单一出口（接线于 dock_layout_controller 片段条连接块）：目标/revision 守卫、手势 flush（目标核对+防误存上一镜头）、真实命令栈提交+发布（dirty/媒体池/信号）、成功回读权威状态、失败零提交；[test_clip_version_host.py](../../../tests/app/test_clip_version_host.py) 24 项通过（全部经 version_ui.submit 正典链驱动） |
| WIRE-02 | 当前 Color hydrate 与 renderer 更新 | WIRE-01 | 已完成 | 内容变更后 renderer 栈更新 + 会话片段匹配时 `_load_color_stack_to_grading_panel`（`_color_stack_hydration_in_progress` 防递归）；host 测试 TestLoad/TestTableOnly 断言 hydrate/renderer 调用与仅表操作零重解码 |
| CACHE-01 | 共享依赖、截图归属与失效机制 | WIRE-02 | 已完成 | 版本内容变更→`invalidate_graded_preview_stacks`（共享依赖全扫）+ 片段栏重建；rename 不失效；活动镜头调度带 token 护栏的新截图；host 测试 TestPreviewCacheInvalidation 3 项 + 既有迟到帧 ±5 窗口与绑定差异失效测试 |
| IO-01 | 版本表与域字段保存加载 | BE-02、BE-03 | 已完成 | 深拷贝往返保真（含 default_version、locked、未知 payload 字段）；旧工程缺键=空表：test_color_versions.py TestVersionTableIOFidelity 3 项 + test_m02_snapshot_io_fidelity 全过 |
| DIAG-01 | 插件、锁与失败反馈统一 | WIRE-01 | 已完成 | 统一 `plugin_diagnostics` 字段（保存期持久化）+ `version_plugin_diagnostics()`（旧载荷现算）；missing/unverified 区分，注册表不可用不冒称无缺失；锁拒绝参数写零提交、版本管理照常：test_color_versions.py TestPluginDiagnostics 3 项；M07 面板与片段宿主消费同一字段 |
| QA-01 | 真实栈、并发与项目切换验证 | WIRE-02、CACHE-01、IO-01、DIAG-01 | 已完成（无头可测部分） | §7.3 矩阵逐行映射见 §7.4 记录；全部为真实命令栈/真实 Qt 控件路径，无 fake executor 拆包；真机实操（真实素材、viewport、导出同帧核对）建议桌面环境补验一次 |
| PERF-01 | 大工程与连续操作验证 | QA-01 | 已完成（无头可测部分） | [test_clip_version_scale.py](../../../tests/app/test_clip_version_scale.py)：200/500/1000 clip×5 版本菜单读取不解码、不重建全条、单次两域读取 <50ms（实测记录随 -s 输出）；300 clip 连续 20 次表操作零全条重建；真机 4K/GPU 时延待桌面环境补测 |

## 7. 验证方法与验收矩阵

### 7.1 本轮 UI 验证（不代替后端验收）

验证右键目标、版本子菜单布局、已选版本标识、local/remote 呈现、输入对话框、空白名/首尾空白/重名/长名、取消与 Escape、键盘焦点、无版本/无栈/无工程状态、pending 防重复、失败/未接线提示。

使用监听器收集请求，断言目标身份不随 active clip 改变；测试同一个 token 的重复 ack、旧 token、换工程、关闭对话框、菜单重开。UI 测试可注入权威状态，但不得把注入状态当作真实工程持久化证明。本轮已执行下面的 focused suite；最终通过数见本节记录。测试中操作真实 Qt 控件，注入的只是 DTO/ack，不代替后端真实命令栈验收。

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q \
  tests/color/test_clip_version_ui.py \
  tests/color/test_clip_strip_presentation.py \
  tests/color/test_timeline_clip_strip_lazy_thumbnails.py \
  tests/color/test_timeline_clip_strip_signal_dedup.py \
  tests/color/test_clip_navigation.py \
  tests/app/test_color_clip_strip_preview_ownership.py
```

后端交接聚焦套件（2026-11-01 追加）：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q \
  tests/edit/test_color_version_command_stack.py \
  tests/edit/test_color_version_domains.py \
  tests/edit/test_color_versions.py \
  tests/edit/test_m07_version_tools_panel.py \
  tests/edit/test_m07_share_autolock.py \
  tests/app/test_clip_version_host.py \
  tests/app/test_clip_version_scale.py
```

验证记录（2026-11-01）：上述六文件共 **117 passed，13.77 秒**；其中版本 UI 文件为 31 项。`git diff --check` 通过；两份 locale JSON 可解析，计划内 30 个链接已检查。独立演示截图生成退出码为 0，已查看版本菜单与创建表单截图。最终复查确认跨域切回与目标域未加载时的复制守卫已修复。本记录不包括后端命令栈、真实版本工程写入、撤销重做、真实视频切版本或导出验收。

独立复核交互：

```bash
.venv/bin/python docs/plans/2026-11-01-color-clip-versions/preview_version_ui.py
# 未接线模式：验证确认后不报假成功、不加版本
.venv/bin/python docs/plans/2026-11-01-color-clip-versions/preview_version_ui.py --disconnected
```

演示窗口右键任意片段，逐项创建、重命名、复制、跨域复制、切域、删除；取消表单不改变列表；双击底部切换编码/版本名。正常模式仅模拟元数据回填，不处理真实图像或写入工程。

实际 Qt 控件截图：[片段条](previews/version-strip.png)、[版本菜单](previews/version-menu.png)、[创建表单](previews/version-create.png)。截图使用显式 DEMO 图形，不作为真实素材调色验证。

### 7.2 可复用的现有 focused tests

以下文件均已确认存在；执行结果需由实施同事重新记录：

- [test_color_versions.py](../../../tests/edit/test_color_versions.py)：命名快照、激活、身份、删除、锁、IO、trim 与导出读取。
- [test_m07_version_tools_panel.py](../../../tests/edit/test_m07_version_tools_panel.py)：旧 UI 的 executor/拒绝处理；需补真实栈，现有 fake executor 会拆 tuple。
- [test_m07_share_autolock.py](../../../tests/edit/test_m07_share_autolock.py)：共享锁与比较报告。
- [test_m02_snapshot_io_fidelity.py](../../../tests/edit/test_m02_snapshot_io_fidelity.py)：新增表序列化保真。
- [test_upgrade_panel_actions.py](../../../tests/ui/test_upgrade_panel_actions.py)、[test_upgrade_menu_wiring.py](../../../tests/ui/test_upgrade_menu_wiring.py)：既有工具入口与工厂。
- [test_timeline_clip_strip_lazy_thumbnails.py](../../../tests/color/test_timeline_clip_strip_lazy_thumbnails.py)、[test_timeline_clip_strip_signal_dedup.py](../../../tests/color/test_timeline_clip_strip_signal_dedup.py)、[test_clip_navigation.py](../../../tests/color/test_clip_navigation.py)：缩略图、迟到结果与导航回归。

示例 focused 命令（本节是建议执行，不是通过记录）：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q \
  tests/edit/test_color_versions.py \
  tests/edit/test_m07_version_tools_panel.py \
  tests/edit/test_m07_share_autolock.py \
  tests/edit/test_m02_snapshot_io_fidelity.py
```

### 7.3 后端与真实应用验收

| 场景 | 方法 | 验收要求 |
|---|---|---|
| 真实命令协议 | 使用 `ProjectCommandStack`，不使用拆 tuple 的 fake executor | 激活、撤销、重做无 TypeError；内容与 active 指针均恢复 |
| 名称与非当前版本 | 保存 A/B，live 改为 C，复制 A 为 A2，再重命名 A2 | A2 原内容来自 A 而非 C；rename 不改变内容 |
| 取消与失败 | 名称框取消、空名、重名、被删栈、命令异常 | 工程、revision、undo 历史与版本表不产生半提交 |
| 手势 flush | 拖轮未松手立即创建/切换版本 | 保存正确镜头的最后参数；不误保存上一镜头 |
| local/remote | 同素材多 clip 与另一素材各建版本并切域 | 影响范围符合已文档化语义；私有修改不被无意覆盖 |
| 共享栈/trim | 两 timeline 多 clip 引用共享栈，另有 private trim | 激活影响所有共享引用；trim 不变；列表与缩略图同步 |
| 锁 | 锁定共享栈后改参数与管理版本 | 普通写拒绝且原因可见；版本管理遵循既有允许规则 |
| 插件缺失/不可用 | 去除插件注册、模拟 registry 不可用后保存/激活 | payload 不丢；missing 与 unverified 明确区分 |
| 撤销重做 | 从实际 Color/菜单入口连续操作并 undo/redo | 项目、engine、UI、active 版本、缓存一致，不只画面回退 |
| 重置与 cache | 调色截图后 reset、激活原始版本、尝试删除 active、删除全部其他版本 | 不复用过时 graded 图片；单独删除 active 被拒；删除全部后加载保留的 default |
| 切镜与延迟帧 | 连续 A→B→A、同源不同切片、trim/split 后让旧回调迟到 | 每张卡片只接收正确 source/frame/generation 的图像 |
| 并发 | 两处 UI 同时重命名/删除同版本；请求后修改 revision | stale 请求拒绝或受控重试；不覆盖新工程、不错误 ack |
| 换工程与关闭 | pending 时新建/打开另一工程、关闭 strip/窗口 | 取消订阅与 pending；旧回调不改新工程/界面 |
| 工程 IO | 保存、关闭重开、另存、旧工程无键、未知 payload | 版本内容/active/lock 不变；旧工程正常；未知字段保留 |
| 导出 | 非当前 timeline 在激活前后导出同帧并核对 | 导出取当前激活内容，不使用旧栈/旧缓存 |
| 大工程 | 200/500/1000 clip，多版本/共享引用，连续滚动和开菜单 | 菜单不枚举解码全部帧；无同步 GPU；记录响应时间、请求量、内存 |

真实应用验证使用用户给定素材和可复现的测试工程副本，记录版本名、clip/source 范围、操作顺序、期望与实际截图。无需为名称变化重建全条片段栏；图像变化按受影响栈/片段失效，播放时遵循现有抑制策略。性能结论必须有测量，不以“看起来顺滑”代替。

### 7.4 后端交接验证记录（2026-11-01，无头自动化）

§7.3 矩阵逐行映射（全部真实命令栈 + 真实 Qt 控件路径，无 fake executor 拆包）：

| 场景 | 证据 |
|---|---|
| 真实命令协议 | test_color_version_command_stack.py::TestRealStackActivationProtocol（5 项：全链路/revision 单调/失败不压栈/stale 拒绝/undo 恢复激活前 live） |
| 名称与非当前版本 | 同文件 TestRenameDuplicateOnRealStack::test_duplicate_non_current_snapshot_then_rename（A2 内容来自 A 非 C；rename 不改内容与顺序） |
| 取消与失败 | host 测试 TestGuardsAndFailures（revision 守卫/重名/缺版本/工程切换均零提交）+ UI 测试取消路径 |
| 手势 flush | test_clip_version_host.py::TestGestureFlush（保存目标栈最后参数；面板编辑他栈时不误保存上一镜头） |
| local/remote | test_color_version_domains.py 全部 + host TestLoad::test_load_cross_domain_switches_reference_and_activates（切引用+激活+hydrate+in_use 回填） |
| 共享栈/trim | 域测试（trim 永不入域、共享依赖集合）+ host TestPreviewCacheInvalidation（共享栈失效全扫） |
| 锁 | test_color_versions.py::TestPluginDiagnostics::test_lock_rejects_param_write_but_allows_version_mgmt |
| 插件缺失/不可用 | 同文件 test_diagnostics_registry_unavailable_marks_unverified / test_diagnostics_persisted_and_distinguished |
| 撤销重做 | 命令栈测试全部 undo/redo 对称断言（含宏与 DeleteAll 整体恢复） |
| 重置与 cache | host 删除保护（default/active/最后禁删）+ DeleteAll 后加载 default + 缓存失效三测试 |
| 切镜与延迟帧 | 既有 tests/color 缩略图套件（±5 帧窗、绑定差异失效）本轮全量通过（117 项） |
| 并发 | host revision 守卫 + 命令 apply 期 stale 双表校验（Rename/Copy/Duplicate） |
| 换工程与关闭 | host test_project_switch_rejects_old_target + UI bind_context 清缓存/作废 pending 测试 |
| 工程 IO | test_color_versions.py::TestVersionTableIOFidelity（3 项）+ test_m02_snapshot_io_fidelity |
| 导出 | 既有 test_active_version_content_reaches_export_resolution（激活内容互换→导出解析取激活版本） |
| 大工程 | test_clip_version_scale.py（4 项：200/500/1000 读取时延与零重建、连续 20 操作零全条重建） |

执行记录：后端交接聚焦套件合计 **156 项测试全部通过**（含独立 Review 轮修复回归：project_changed 真信号广播、空域建栈 v3 曲线侧栏字段、DeleteAll 迁移单宏化、跨域 LOAD 载荷预校验与引用回滚）。tests/edit（除环境既有 `_ssl` 损坏模块）与 tests/color 全量回归：9627/2837 通过；出现的失败（Metal Sobel 导出精度、转场 GIF/音频 TTS 计时、UI 布局断言等 19+2 项）经隔离复跑与 git stash 基线抽样复核为**长跑负载敏感/改动前已存在**，与本轮无关。真机（真实素材 viewport/导出同帧核对、4K GPU 时延）待桌面环境补验后回填本节。

## 8. 本轮以外的片段功能登记

群组管理、旗标/片段颜色、渲染缓存菜单、抓取/应用静帧等仅登记为后续独立范围。本轮不实现其后端、存储或命令，也不借“版本菜单仿照 Resolve”扩展进整套片段管理。后续分别调研现有 registry/service 与工程模型，避免把这些功能塞进版本 DTO 或 `color_stack_versions`。

## 9. 交付与状态更新规则

本轮 UI-01～03 与后端 BE-01～04、WIRE-01/02、CACHE-01、IO-01、DIAG-01 均已完成（证据见第 6 节与 §7.4）；QA-01/PERF-01 完成无头可自动化部分，真机验收记录待桌面环境补验后回填 §7.4。后续任何状态更新须附验证证据。

最终后端交付必须同时满足：唯一版本存储、真实命令栈可撤销、工程 IO 保真、明确片段/版本域身份、异步帧不串卡片、共享影响范围正确、错误可见、无 UI 同步解码/GPU 操作。只完成菜单外观或 fake executor 测试不得关闭这些待办。
