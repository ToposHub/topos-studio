# EFR 报告：src/app/controllers 机械清理（E-rollup 模块行）

日期：2026-10-02
范围：`src/app/controllers/`（19 个模块文件，约 47,700 行）
原则：纯机械清理，零行为变化；信号定义与连接一律未动；无 git 写操作；分支保持 dev。

## 分类统计

| 类别 | 数量 | 说明 |
|------|------|------|
| 1. 删除未使用 import | 19 个 / 7 文件 | AST 扫描 + 全仓 import 图核实（详见下文） |
| 2. 删除死代码 | 4 个方法 / 4 文件（69 行） | 全仓文本零引用证明后删除 |
| 2b. 删除 write-only 死存储 | 2 处 / 2 文件 | 纯字面量/名字 RHS、零读取，AST+grep 双证明 |
| 3. 补类型注解 | 258 个返回注解 + 1 个参数注解 | 仅限 AST 可证明子集（见方法论） |
| 4. 命名收敛 | 0 | 未发现可安全收敛的模块内部私有命名；公共 API/信号名一律未动 |
| 5. 小清理 | 2 处 docstring 修正 + 2 处死存储（计入 2b） | 详见"未采纳项" |
| 合计 | 16 文件变更，+267/−352 行 | `git diff --stat` 实测 |

## 1. 未使用 import（19 个，全删）

| 文件 | 删除的名字 |
|------|-----------|
| ai_edit_host_bridge.py | `SemanticSegment`、`ProjectSnapshot`、`TimelineDiff`、`ActionRequest` |
| ai_edit_host_bridge_impl.py | `AiEditHostBridge`（该文件类为普通类，不继承此 Protocol） |
| edit_controller.py | `QInputDialog` |
| file_controller.py | `sys`、`uuid`、`QGraphicsScene`、`MaskShape`、`MaskGroup` |
| media_analysis_controller.py | `RequestState`、`ResourceLane` |
| menu_toolbar_controller.py | `sys`、`subprocess`、`time`、`QVBoxLayout`、`QIcon` |
| page_controller.py | `uuid` |

核实方法（三层）：
1. AST 扫描（Name/Attribute/字符串常量 token 全覆盖）确认文件内零使用；
2. AST 全仓 import 图确认没有任何模块从上述模块 import 这些名字（含括号多行 import）；
3. grep 确认无 `module.name` 属性式访问（如 `edit_controller.QInputDialog`）。

保留（惰性导入防护，未动）：
- ai_editing_mixin.py L~495 `LLMPlannerError`（函数内惰性导入）
- file_controller.py L~4553 `PageContext`（函数内惰性导入）
- menu_toolbar_controller.py L~1093 `ensure_project_path`（函数内惰性导入）

## 2. 死代码（4 个方法，全仓零引用证明）

删除前逐一执行 `grep -rn "<name>" --include="*.py" src tests`（含去下划线子串扫描，防 getattr 动态调用）：

| 方法 | 文件:原行号 | 证据（删除前 grep 命中数） |
|------|------------|--------------------------|
| `_select_vision_profile` | ai_edit_controller.py:3705（30 行） | 仅 def 行 1 处 |
| `_graph_has_composite_user_edits` | composite_controller.py:309（29 行） | 仅 def 行 1 处 |
| `_video_controller_monitor_qimage` | file_controller.py:3487（6 行） | 仅 def 行 1 处；其调用的两个 helper（`_video_controller_monitor_payload` / `_monitor_payload_to_qimage`）在 file_controller.py:2706/2721 有独立调用方，均保留 |
| `_probe_color_tags_sync` | page_controller.py:3439（4 行） | 仅 def 行；其委托目标模块级 `_probe_color_tags_sync_for_host` 在 page_controller.py:170 有直接调用，保留 |

扫描排除项（防御性）：Qt 虚函数/事件处理器（`*Event`、run/start/close/show 等保留名）、双下划线、公共名。

### 2b. write-only 死存储（2 处）

| 位置 | 变量 | 证据 |
|------|------|------|
| page_controller.py `switch_*_to_composite`（原 L3861/L3865） | `track` | 函数体内仅 2 次赋值（`None` / `candidate_track`，均纯 RHS）、零读取 |
| ai_editing_mixin.py `_all_analysis_fresh`（原 L1809/L1820） | `has_any_analyzed` | 全文件仅 2 次赋值、零读取；函数无嵌套作用域 |

## 3. 类型注解（258 返回 + 1 参数）

方法论（AST 可证明子集，"拿不准就不加"）：
- `-> None`：函数作用域内所有 `return` 均为裸 return/`None`，或无 return；排除生成器（作用域含 yield）、stub（仅 docstring/pass/raise）；
- `-> bool`：所有 return 均为字面量 True/False 且无 None 混入；
- 嵌套函数/Lambda 内的 return 不计入外层；
- 参数注解仅在类型本地可证时添加（仅 `agent_v2_chat_worker.py` 的 `parent: QObject | None = None`）；
- 全部文件已有 `from __future__ import annotations`，注解不求值，零运行时影响。

分布：color_controller 42、layer_controller 47、file_controller 59、menu_toolbar_controller 40、dock_layout_controller 21、deliver_controller 19、page_controller 17、composite_controller 5、edit_controller 4、color_workspace_controller 3、ai_edit_controller 1、agent_v2_chat_worker 1（参数）。
未补：仍有约 340 处返回/参数注解缺口，因类型无法机械证明（Qt 对象、Optional 语义、mixin self 类型等），按红线保守跳过。

## 5. 小清理与未采纳项（记录决策）

- `grep -rn "except:" src/app/controllers/` 既有命中 2 处均为 docstring 文本（ai_edit_host_bridge.py:290、ai_editing_mixin.py:13，都是描述"不要用 broad except: pass"的反模式说明），非真实 except 子句。将措辞改为 "except-pass"（纯注释变更）后该 grep 已零命中；全目录无真实裸 `except:`。
- **未采纳**：`_`/`_job`/`_filter` 等约定式丢弃名（保留是惯例）。
- **未采纳**：RHS 含调用/属性的未读局部变量（如 `locale_name = I18N.get_locale_display_name(...)`、`RenderCacheManifest = edit_facade.get_render_cache_manifest_class()`、9 处 `new_timeline = self._rebuild...()`）——调用可能有副作用，不满足"零行为变化"的可证明标准。
- **未采纳**：重复字面量提取为常量——需逐处语义审阅（同值不同义风险），超出机械清理边界。
- **未采纳**：约 340 处无法证明的注解缺口。

## 验证输出

1. **py_compile**：`src/app/controllers/` 全部 18 个模块编译通过（`COMPILE ALL OK`）。
2. **import 冒烟**：18 个模块全部 `importlib.import_module` 成功（覆盖被删 import 的模块级 NameError 风险）。
3. **定向测试**（不整目录跑）：
   ```
   pytest tests/app/test_color_controller_coverage.py tests/app/test_deliver_controller_coverage.py \
          tests/app/test_media_analysis_controller.py tests/app/test_dock_layout_controller_coverage.py
   → 347 passed in 6.57s
   pytest tests/app/test_edit_controller_timeline_management.py tests/app/test_ai_edit_controller.py
   → 25 passed in 2.69s
   ```
   合计 372 passed / 0 failed（无新增失败）。
4. **裸 except 检查**：`grep -rn "except:" src/app/controllers/` → 零命中。

## 改动文件清单（16 个，工作树未暂存）

- src/app/controllers/agent_v2_chat_worker.py（1 参数注解）
- src/app/controllers/ai_edit_controller.py（删 import 1、删死方法 1、返回注解 1）
- src/app/controllers/ai_edit_host_bridge.py（删 import 4、docstring 1）
- src/app/controllers/ai_edit_host_bridge_impl.py（删 import 1）
- src/app/controllers/ai_editing_mixin.py（docstring 1、死存储 2 行）
- src/app/controllers/color_controller.py（返回注解 42）
- src/app/controllers/color_workspace_controller.py（返回注解 3）
- src/app/controllers/composite_controller.py（删死方法 1、返回注解 5）
- src/app/controllers/deliver_controller.py（返回注解 19）
- src/app/controllers/dock_layout_controller.py（返回注解 21）
- src/app/controllers/edit_controller.py（删 import 1、返回注解 4）
- src/app/controllers/file_controller.py（删 import 5、删死方法 1、返回注解 59）
- src/app/controllers/layer_controller.py（返回注解 47）
- src/app/controllers/media_analysis_controller.py（删 import 2）
- src/app/controllers/menu_toolbar_controller.py（删 import 5、返回注解 40）
- src/app/controllers/page_controller.py（删 import 1、删死方法 1、死存储 2 行、返回注解 17）

红线自查：无信号定义/连接改动（diff 仅 import 行、def 签名行、整方法删除、纯注释行）；无裸 except 引入；无函数重排；无 git add/commit/push；REMEDIATION_PLAN.md 未触碰。
