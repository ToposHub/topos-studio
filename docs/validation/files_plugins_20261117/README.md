# Files 插件 UI 验证记录

日期：2026-11-17。Python 3.12 / PyQt6 6.11，Qt offscreen；使用项目原有深色主题。

## 结果

- `run_regression.py`：**382 passed，0 skipped，31.18s**。涵盖 Files 服务/安全回归、四种布局、插件清单/交互/HTTP/关闭、真实视频播放语义、宿主架构和偏好设置。详见 `regression.txt` / `regression.xml`。
- `capture_plugins.py`：四个已核实的内建插件都通过本地认证 HTTP 收到打开请求，并在 Qt 主线程实际创建可见窗口，脚本退出 0。证据为 `launch_results.json` 与本地截图 `launched_*.png`。
- `git diff --check` 通过。Review 未发现阻断项。

## 截图范围

**所有截图可能包含用户真实素材/文件名，仅本地保留、不入仓**（见 `.gitignore` 隐私保护条目）。本地核对项：

- `plugin_center.png`：1060×680，列表/分类/详情/启停/快捷键。
- `plugin_center_compact.png`：840×560，窄窗口的滚动和详情布局。
- `plugin_legacy_details.png`：旧 Web 工具说明与兼容入口。
- `plugin_launcher.png`：固定优先、最近使用、搜索与管理入口。
- `launched_format_factory.png` / `launched_image_factory.png` / `launched_shutdown_timer.png` / `launched_decode_benchmark.png`：四个真实打开的插件窗口。

截图使用本仓库实际 **10 个**插件的静态声明。验证宿主只显式加载四个已知窗口插件，其他六个显示未加载/待启用；它不是用户正在运行的应用状态截图。版本缺失时沿用旧加载器的 `0.0.1` 回退，作者缺失显示 `Unknown`，没有虚构插件发布信息。

## 验证边界

测试配置和 QSettings 存临时目录；没有初始化真实素材数据库，没有改写用户照片、视频或真实插件启停配置。定时关机窗口验证了 `is_running == False`，未启动任何系统任务；转码、跑分和图片转换也未执行。Eagle 仅做只读交互观察。

首次真实窗口关闭测试发现图片工厂空闲关窗崩溃：未创建工作线程时 `getattr(self, "thread")` 返回继承的 `QObject.thread()` 方法。已改成显式 `_processor_thread`，并验证“无任务可关闭”和“工作线程未退出时拒绝销毁”两条路径。

当前 UI 的成功提示仅表示旧服务已接收打开请求。窗口就绪、处理作业状态及热卸载仍需要新的生命周期契约，列入 [后续计划](/Users/heng/Documents/vscode/topos-studio/docs/plans/2026-11-17-files-plugin-system-rebuild-plan.md)。

## 复现

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python docs/validation/files_plugins_20261117/run_regression.py
QT_QPA_PLATFORM=offscreen .venv/bin/python docs/validation/files_plugins_20261117/capture_plugins.py
```

第一条命令沿用本会话提供的真实视频目录，并设置严格媒体门禁，不以跳过用例代替媒体验证。
