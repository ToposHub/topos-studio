# F 阶段第二批（F2）：按新 ID/状态模型重建偏好——固定/最近/快捷键/启停

日期：2026-11-18 · 分支：fix-ui · 范围：`FilesPluginsController` 的 id
换轨 + `FilesPluginInfo.command_key` + 新模型启停持久化 `DesiredStateStore`。

## 交付内容

| 文件 | 变更 |
| --- | --- |
| `plugin_contracts.py` | `FilesPluginInfo.command_key`（空 = 无命令入口）：合并行携带命名空间 `plugin_id`（偏好建档），命令层仍以 legacy 短 id 寻址 |
| `files_plugins_controller.py` | ① `_merge_infos`：legacy 匹配行的 `plugin_id` 换为 inventory 记录的命名空间 id（`files.<短id>`），`command_key` 保留短 id——**同一合并快照**内解析，不持久化别名。② `launch`：UI 新 id → 快照行取 `command_key`（旧接线无 factory 时 id 即命令键，行为不变）；不可启动行（占位）被启动时如实回"尚未接入"消息。③ `_launch_finished`：回报携带线上短 id → 经快照换回 UI 新 id（busy/最近使用不错位）。④ `_set_enabled`：有命令键 → 旧 catalog 配置（行为不变）；无命令键（受管安装/开发源）→ `inventory.set_desired` + `DesiredStateStore` 持久化 + 同口径重扫。⑤ `_snapshot_and_merge` 读取 desired store 喂给发现侧（新模型启停跨重启生效） |
| `discovery.py` | `DesiredStateStore`：`{plugin_id: bool}` JSON 持久化（原子写）；legacy 目录插件不进此档 |
| `files_panel.py` | 生产接线 `desired_store = <files-data>/plugin_desired.json` |

## 语义

* **固定/最近/快捷键**：全部键在命名空间新 id 上（`preferences` 原样
  存新键值）；旧短 id 偏好自然失效——不迁移、不别名（测试锁定：旧
  固定项不映射，按钮回"未固定"态）。
* **命令读同一快照**：偏好/中心/命令三层都从合并投影行取数——中心
  显示行、快捷键重建、启动解析用同一个 `_infos` 快照；权威复核仍在
  命令服务 worker 内（PR-05 管线不变，线上仍 `/api/plugin/<短id>/launch`）。
* **启停双轨如实**：legacy 目录插件 → 旧 catalog 配置（重启生效语义
  不变）；新模型插件 → desired store + inventory（同一次 refresh 反映）。

## 验证

`tests/ui/test_native_files_plugins.py` +4（共 24）：
* 启动：UI 新 id 发起 → 线上是命令键（`/api/plugin/format_factory/launch`
  + token + payload 逐字断言）→ 最近使用按 `files.format_factory` 记账；
* 新模型启停：`files.installed_tool` desired 持久化到 store、行状态翻转、
  新 store 实例读回（重启语义）；legacy 插件启停**不进** store；
* 旧短 id 偏好不迁移（固定项不映射）；
* `DesiredStateStore` 读写/清空往返。

回归：`tests/plugins` + catalog/启动服务全量 **447 通过**（含 F1 的
11+2 项改口径后全绿；无 factory 的旧接线行为逐字不变）。

## 诚实边界

- 命令层（`LegacyFilesCommandService`/catalog）内部仍以短 id 寻址——
  快照内换轨而非重写命令服务（其权威复核管线由 18 项测试锁定）；
  命令服务直接读 inventory 快照归 F3/F4（command 唯一入口 + 原子复核
  边界）批次。
- 新模型插件 desired 生效仍需宿主加载器消费（F6 生命周期）——当前
  反映在 inventory/中心 UI，尚无运行时加载行为可影响。
- `preferences_dialog` 第二处列表未切（同 F1 边界）。

## 隐私

临时目录/QSettings 合成数据；无隐私路径。
