# 原生素材插件界面

## 清单与启动

```text
FilesPanel
  ├─ NativeFilesPanel → NativeFilesTransport → FilesFacade / 共享媒体服务
  └─ FilesPluginsController
       ├─ NativePluginCatalog / PluginInventory → 插件中心、启动器
       └─ NativeToolCommandService → 原生 Qt 工具窗口
```

- `src/features/files/builtin_manifests/` 保存内建工具和格式提供者的声明。`NativePluginCatalog` 校验声明并生成不可变的 `FilesPluginInfo`。内建启停状态保存在 `files-data/plugin_desired.json`。
- `FilesPluginsController` 将内建目录、受管安装包、显式添加的开发源和本地独立应用投影到插件中心。后台任务读取清单和复核启动请求，Qt 主线程负责打开工具窗口和更新界面。
- `PluginCenterDialog` 和 `PluginLauncherDialog` 负责搜索、分类、状态、详情、固定、最近使用和快捷键。固定、最近使用及快捷键保存在用户级 QSettings。快捷键在素材面板子树内生效，文本编辑时暂停触发。
- `NativeFilesTransport` 通过进程内的 `FilesFacade` 和有界工作线程完成素材查询、元数据、文件事务及缩略图请求。导航代际和取消令牌阻止过期请求更新界面。

## 工具行为

启动命令冻结当前选择，在后台复核，并在主线程提交。启停和提交共用权限边界。原生窗口打开成功后才记录最近使用。支持单例的窗口通过宿主的窗口注册表聚焦。

`format_factory` 接收零个或一个路径。`decode_benchmark` 接收路径列表，并通过 `src/app/services/edit_benchmark_service.py` 访问当前剪辑播放管线。打开 `image_factory` 或 `shutdown_timer` 窗口后，用户在窗口中启动处理或定时任务。视频工具复用 `src/shared/playback/`。AI 素材库工具通过 `VaultClient` 查询独立的 Vault 服务。

独立应用使用本地应用运行时。插件中心依据清单、来源和执行能力显示可用状态及诊断。首选项中的内建工具开关使用同一个原生清单和启停状态。

关闭素材宿主时，插件控制器释放工具窗口和快捷键等贡献，传输层取消在途读取并等待资源释放。图片工厂窗口关闭时取消在途批次。

[原生素材数据边界](../plans/files-replacement-data-boundary-20261117.md) · [插件系统重构计划](../plans/2026-11-17-files-plugin-system-rebuild-plan.md) · [历史验证记录](../validation/files_plugins_20261117/README.md)
