# EFR 复查：app/controllers（E-rollup 模块行）

- 复查对象：`src/app/controllers/` ｜ 提交 `80fb65f94789684fee8aa7dcb1b29fd307277c90`
- 执行报告：`fixes/reports/efr-app-controllers.md`
- 复查日期：2026-10-01 ｜ 复查方式：只读（git show/grep/py_compile），未运行 pytest
- 复查人：GLM5.3 复查会话

## 七项判定

| # | 检查项 | 判定 | 说明 |
|---|--------|------|------|
| 1 | 越界 | ✅ PASS | 17 个文件 = 16 个 `src/app/controllers/*.py` + 本批报告；无 tests/ 改动（规程允许非必须）；REMEDIATION_PLAN.md 未触碰 |
| 2 | 未用导入删除抽查 | ✅ PASS | 抽 13 个被删名字（见明细表），当前全仓（含 tests/docs/字符串/模块限定名）零引用 |
| 3 | 死代码删除抽查 | ✅ PASS | 4 个死方法 + 嵌套 `_has_vision` 全仓零引用；write-only 变量 2 处 RHS 均为纯名字/字面量 |
| 4 | 注解零行为证明 | ✅ PASS | diff 中函数改动全部为 `def f(...):` → `def f(...) -> None:`；唯一参数注解 `parent: QObject \| None = None` 默认值未变 |
| 5 | 红线 | ✅ PASS | `pyqtSignal`/`.connect(` 零触碰；新增行无裸 except；无 class 行改动；被删 def 全部为下划线私有方法，公共 API 零改名（263 个被删 def 名中 258 个有配对新增，5 个未配对全部为报告披露的死代码） |
| 6 | py_compile | ✅ PASS | 当前 HEAD 抽 9 个改动文件（ai_edit_controller/color_controller/file_controller/layer_controller/menu_toolbar_controller/page_controller/composite_controller/dock_layout_controller/deliver_controller）全部编译通过 |
| 7 | 综合结论 | **PASS** | 机械清理成立，零行为变化证明充分 |

## 抽查明细

### 未用导入（13 名字，全仓 grep）

| 被删名字 | 所在文件 | grep 结果 | 判定 |
|----------|---------|-----------|------|
| `SemanticSegment` | ai_edit_host_bridge.py | `ai_edit_host_bridge.SemanticSegment` 限定名零命中；名字在定义模块 `editorial_models` 中仍存在且与本模块解耦 | 安全 |
| `ProjectSnapshot` / `TimelineDiff` / `ActionRequest` | ai_edit_host_bridge.py | 模块限定名零命中 | 安全 |
| `AiEditHostBridge` | ai_edit_host_bridge_impl.py | 限定名零命中；该名字仍在定义模块 ai_edit_host_bridge.py 正常导出（ai_edit_controller.py:35 在用） | 安全 |
| `QInputDialog` | edit_controller.py | `edit_controller.QInputDialog` 零命中 | 安全 |
| `sys` / `uuid` / `QGraphicsScene` / `MaskShape` / `MaskGroup` | file_controller.py | 文件内残留使用 0；限定名零命中 | 安全 |
| `RequestState` / `ResourceLane` | media_analysis_controller.py | 限定名零命中 | 安全 |
| `sys` / `QVBoxLayout` / `QIcon` | menu_toolbar_controller.py | 文件内 `sys.`/`QVBoxLayout.`/`QIcon.` 残留 0 | 安全 |

再导出通道核查：全仓 `from src.app.controllers.<改动模块> import ...` 仅引入保留的 Mixin/类（EditControllerMixin/MenuToolbarMixin/PageManagementMixin/FileOpsMixin/MainWindowAiEditHostBridge/_SpeechToTextWorker 等），无一引入被删名字。

### 死代码（4 方法 + 1 嵌套函数）

| 被删定义 | 全仓 grep（src/tests/scripts/plugins） | 判定 |
|----------|--------------------------------------|------|
| `_select_vision_profile`（含嵌套 `_has_vision`） | 零命中 | 安全；报告"30 行"块即含嵌套函数，口径一致 |
| `_graph_has_composite_user_edits` | 零命中 | 安全 |
| `_video_controller_monitor_qimage` | 零命中；其 helper `_video_controller_monitor_payload`/`_monitor_payload_to_qimage` 确有独立调用方（保留正确） | 安全 |
| `_probe_color_tags_sync` | 零命中（排除保留的同前缀模块级 `_probe_color_tags_sync_for_host`，后者在 page_controller.py:170 有直接调用） | 安全 |

### write-only 死存储

- `track`（page_controller switch_*_to_composite）：`track = None` / `track = candidate_track`，RHS 为纯字面量/循环变量名，diff 确认仅删赋值行，零读取。
- `has_any_analyzed`（ai_editing_mixin `_all_analysis_fresh`）：仅 `False`/`True` 字面量赋值，零读取。

## 发现

1. **（Note，非缺陷）docstring 措辞改动服务于 grep 指标**：将 `broad ``except: pass``` 改为 `broad except-pass`（2 处，ai_edit_host_bridge.py / ai_editing_mixin.py）使 `grep "except:"` 归零。纯注释变更、零行为，且报告 §5 已完整披露——属指标驱动的合规操作，非隐藏问题。当前目录实测裸 `except:` 零命中。
2. **（Note）`_has_vision` 未在报告表格中单列**：作为 `_select_vision_profile` 的嵌套函数随宿主方法整体删除，含于报告的"30 行"计数内，无隐瞒。

## 结论

**PASS**。越界、导入、死代码、注解、红线、py_compile 六项全部通过；报告统计与 git stat 交叉验证一致（源码 +267/−352，另 +115 报告行）。建议勾选该 E-rollup 行。
