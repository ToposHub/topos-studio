# Files 原生插件界面与旧宿主适配

2026-11-17。范围为已安装扩展的呈现和现有能力调用，不包含新的插件执行系统。

## 边界与数据流

```text
NativeFilesPanel（工具栏 / 右键 / 更多菜单）
  → FilesPluginsController
    → FileExplorerPluginCatalog（后台串行队列：静态声明 + 真实加载/失败状态）
    → PluginCenterDialog / PluginLauncherDialog（不可变快照、意图信号）
    → NativeFilesTransport（认证、仅本地 HTTP、四个已核实的 launch 端点）
      → 旧 PluginManager 的 Flask Blueprint → GuiLauncher 信号 → Qt 主线程窗口
```

- `src/features/files/plugin_contracts.py`：与宿主无关的不可变 `FilesPluginInfo`；显示期望启用状态、实际加载状态与重启要求。
- `src/app/integrations/file_explorer/plugins.py`：静态 AST 常量/可选 config.json 清单，不执行插件、不调用运行时初始化；合并 active/failed 状态。启停以读改写保留未知配置并回读确认；插件自身 disabled 不被宿主覆盖。
- `src/app/ui/native_files_plugins.py`：纯 Qt 呈现。搜索/功能分类/状态过滤、详情、固定、最近、快捷键编辑；显示错误的文本采用纯文本，避免解释插件提供的 HTML。
- `src/app/ui/files_plugins_controller.py`：后台清单任务、用户级偏好、快捷键冲突和输入焦点、当前选择快照、请求互斥及反馈；不做素材扫描、SQL、解码或 GPU 工作。
- `src/app/ui/native_files_transport.py`：`X-Auth-Token`、本地主机限制、手动重定向、超时。HTTP 200 且 `status=ok` 只表示请求被接收，不冒充工具窗口就绪或任务执行成功。

## 启动与兼容

`format_factory` 接收零或一个路径；多选明确拒绝，不静默丢弃。`decode_benchmark` 接收选择列表。`image_factory`、`shutdown_timer` 只打开工具窗口，不自动启动处理/系统任务。其余扩展没有通用原生启动路由，详情明确提示并提供兼容界面入口。

快捷键只挂在 Files 子树，编辑文本时不触发插件工具；固定、最近和快捷键仅存用户级 QSettings。最近使用只记录服务确认接收的打开请求。刷新不重新加载 Python 模块或注册 Blueprint。偏好设置的旧列表也使用同一清单，并保留未安装/未列出的 disabled id。

关闭宿主时先关闭 controller，丢弃后台迟到结果，再由 transport abort 在途请求；视图不拥有插件工作线程。旧插件窗口继续遵循原有关闭协议。图片工厂的工作线程改为 `_processor_thread`，避免与 `QObject.thread()` 冲突导致空闲关窗崩溃。

## 后续边界

旧插件仍拥有宿主进程权限；当前不是热卸载或沙箱。卸载、市场安装更新、声明式 Inspector/格式 providers、完整生命周期和共享 Host API 见 [重构计划](/Users/heng/Documents/vscode/topos-studio/docs/plans/2026-11-17-files-plugin-system-rebuild-plan.md)。不能用清单刷新或启停勾选替代这些后端能力。

测试和真实内建窗口验证见 [验证记录](/Users/heng/Documents/vscode/topos-studio/docs/validation/files_plugins_20261117/README.md)。
