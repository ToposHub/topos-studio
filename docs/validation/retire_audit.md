# BX06：Flask / WebEngine 退役审计（2026-11-16）

范围：在“计划移除 Flask / QtWebEngine 宿主”之前，先确认哪些消费者仍依赖它们。
本文件只做审计与事实记录，不执行任何退役动作。

## 1. 现状快照

| 类别 | 数量 | 证据 |
| --- | --- | --- |
| Flask 路由 | 96 个 `@app.route` | `external/file_explorer/routes.py` |
| 引用 QtWebEngine/QWebEngine 的 Python 文件 | 14（`src/` + `external/`） | `grep -rl "QtWebEngine\\|QWebEngine" src external --include=*.py` |
| Files 宿主 | 1 | `src/app/ui/files_panel.py`（`QWebEngineView` 承载完整 Files） |
| 独立编辑器 | 多个 | 教学动画 HTML 预览/渲染、`motion_graphic_style`、`preview_bridge` |

## 2. 关键消费者

| 文件 | 用途 | 能否随 Files 一起退役 |
| --- | --- | --- |
| `src/app/ui/files_panel.py` | 完整 Files 的 WebEngine 容器 | 原生面板覆盖浏览/预览/文件任务后可退役；插件与高级工具仍需要旧宿主时保留 |
| `external/file_explorer/window.py`、`webapp.py`、`main.py` | 独立 Files 运行时（Flask + WebEngine 窗口） | 与 `files_panel` 同批评估，必须先完成能力迁移 |
| `src/app/controllers/menu_toolbar_controller.py` | 菜单入口指向完整 Files | 入口改为原生后只剩“打开完整 Files”兼容项 |
| `src/features/teaching_animation/*` | HTML 预览与渲染抓取 | **与 Files 无关，不得随 Files 退役** |
| `src/features/color/core/async_renderer.py`、`src/shared/ui/widgets/pref_drag_value.py` | 仅检测 WebEngine 可用性/拖拽兼容 | 独立判断，不阻塞 Files |
| `src/main.py` | 应用启动时按需导入 WebEngine | 退役时保留按需导入，避免无 WebEngine 环境启动失败 |

## 3. 退役前置条件（全部满足才可停旧宿主）

1. 原生面板覆盖 F01–F16 中已承诺项，且文件任务/集合/搜索均有自动化证据（见 `docs/validation/files_native_p0/README.md`）。
2. 旧插件 API（`/api/manager/plugins`、Python/JS handler）有替代或明确保留独立编辑器。
3. 全库备份可用（BX01 `/api/library/backup`）并通过一次恢复演练。
4. 教学动画等独立 WebEngine 消费者完成拆分，不再依赖 Files 宿主。

## 4. 结论

- **当前不退役**：原生面板已完成 P0/P1 主线，但旧宿主仍承载插件与高级工具入口。
- 退役动作本身不在本次任务范围；本审计用于防止“停掉 HTTP 就以为停掉了全部消费者”。
- 已知风险：`files_panel` 与独立编辑器共享 WebEngine 依赖，任何删除都必须按文件逐个确认，不能用全局替换。
