# F 阶段第一批（F1/PL-02 收口）：单一 Inventory——builtin/已安装/显式开发源

日期：2026-11-18 · 分支：fix-ui · 范围：`src/shared/plugins/discovery.py`
（新）+ `FilesPluginsController`/中心 UI/`FilesPluginInfo` 的生产接线。
此前 `PluginInventory`（含 PR-03 修复）零生产调用方，UI 只读 legacy
catalog 投影——来源/安装事实/声明-运行版本/重复身份全部停在数据层。

## 交付内容

| 文件 | 变更 |
| --- | --- |
| `src/shared/plugins/discovery.py`（新） | ① `scan_sources` 单一聚合：legacy 目录（builtin 名单政策 + 未识别目录 local_dev）+ 受管安装根 + 显式开发源 → 一份 `PluginRecord[]` 交 `PluginInventory.refresh`。② `installed_records`：枚举安装根（与 installer 共用 current 指针语义；指针缺失/损坏 → `install_state="missing"` + `INSTALL_POINTER_BROKEN` 诊断，不假装在账）；来源读安装器自记 provenance（包声明不参与信任）。③ `dev_records`：开发目录按 `topos.plugin.json` 身份权威扫描；目录缺失/manifest 畸形 → 诊断记录保留可见。④ `DevSourceRegistry`：显式开发源清单持久化（JSON 原子写、增删幂等）。⑤ `current_host_facts()`（host_api/版本/平台/架构）与 `source_label`（UI 直读标签）。⑥ 冲突保留优先级明确：builtin > installed > dev（inventory 保留首个 + `PLUGIN_ID_DUPLICATE` 诊断两边可见） |
| `inventory.py` | `PluginRecord` 增 `origin_path`（来源目录，UI 定位/打开目录用，不参与身份/信任） |
| `plugin_contracts.py` | `FilesPluginInfo` 增 `source`/`install_state`/`runtime_version`（默认值保持旧调用方不变） |
| `files_plugins_controller.py` | `sources_factory` 可选参数：refresh/启停后 worker 内走 scan → inventory 原子快照 → `_merge_infos`（legacy 行按 legacy_ids/`files.<短id>` 匹配增强 + typed 诊断并入；仅存在于 inventory 的记录合成占位行——`launch_unavailable_reason="尚未接入本宿主命令入口"`，不冒充可用）；缺省时行为与旧投影逐字一致 |
| `native_files_plugins.py` | 中心详情新增「来源」行：来源标签 + 安装状态 + 开发源「未验证」标注；版本行在宿主上报运行版本且与声明不一致时并列展示 |
| `files_panel.py` + `file_explorer/plugins.py` | 生产接线：安装根 = files-data 配置根下 `plugins/`（与 DB/库注册同一定位器），开发源注册表 = `plugin_dev_sources.json`，builtin 名单 = 宿主政策 `LEGACY_BUILTIN_IDS`（新导出） |

## 安装根与发现根闭环

`LocalPackageInstaller.install/uninstall` 改变安装根 → 下一次 refresh 的
`scan_sources` 即见新事实（install→在账、指针坏→missing+诊断）；读侧
与 installer 共用同一 current 指针/来源校验语义——两个半环接上。

## 验证

- `tests/plugins/test_discovery_f1.py`（新，11 项）：安装根读取（含升级
  后指针换新即见）、指针损坏→missing+诊断、空根、开发源注册表持久化/
  增删幂等/重启读回、开发源扫描与缺根/坏 manifest 诊断、三源合并、
  **重复身份跨源诊断**（builtin 保留 + 冲突可见）、运行版本分槽在
  refresh 后不丢（宿主事实不被发现快照覆盖）、来源标签矩阵。
- `tests/ui/test_native_files_plugins.py` +2（真实控制器/中心 UI）：
  三源进入中心（builtin 行增强、安装/开发源占位行不可启动且原因如实、
  第三方默认停用、来源行渲染含「未验证」）；撞名安装包 → builtin 保
  留 + `PLUGIN_ID_DUPLICATE` 诊断进入详情。
- 回归：`tests/plugins` + 插件 catalog/声明式 manifest/启动服务全量
  **439 通过**；既有 18 项 native 插件 UI 测试不变绿。

## 诚实边界

- **安装/卸载/添加开发源的 UI 动作未做**（F 表第 11 项范围）：本批是
  发现-聚合-展示闭环；安装事务（PL-08 已有契约）接 UI 归后续批次。
- 仅存在于 inventory 的插件（安装/开发源）**不可启动**——命令入口/
  生命周期（F 表第 3/5/6 项）接入前，占位行如实标注「尚未接入本宿主
  命令入口」，不冒充可用。
- `preferences_dialog` 的第二处插件列表仍读 legacy 投影（与中心共用
  catalog 构造）——切到统一 inventory 归 F 后续批次（避免本批改动面
  过大）。
- 运行版本槽位目前恒空（宿主加载器未上报 mark_runtime）——F6 生命
  周期批次接线后填充；投影链路已就绪并有测试锁定不被发现快照覆盖。

## 隐私

临时目录合成插件包/manifest；无隐私路径。
