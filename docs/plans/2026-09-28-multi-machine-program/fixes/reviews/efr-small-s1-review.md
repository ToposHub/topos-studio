# 复查报告：efr-small-s1（13 个小模块行机械清理）

- 提交：`f34febf39970c880026ea862d39a489215eb982a`（[fix-E efr-small-s1]，153 files，+305/−389）
- 执行报告：`docs/plans/2026-09-28-multi-machine-program/fixes/reports/efr-small-s1.md`
- 复查日期：2026-10-02 ｜ 复查方式：只读（git show/grep + py_compile），未运行 pytest

## 结论：PASS

## 1. 范围核对（越界检查）

提交含 1 个报告新增（fixes/reports/efr-small-s1.md）+ **151 个源文件**，与报告"按模块行分节"合计（9+22+16+4+14+17+15+2+13+6+11+6+16=151）**逐目录完全吻合**：

| 模块行 | 报告 | 提交 | 判定 |
|---|---|---|---|
| src/app/ui（9）/ ai_harness/tests（22）/ ai_workflow/tests（16）/ composite 根（4）/ evaluators（14）/ composite/render（17）/ ta-render（15）/ ta-ui（2）/ shared/export（13）/ shared/project（6）/ audio_mixer（11）/ shared/ui 根（6）/ shared/ui/widgets（16） | 各节列明 | 全部一致 | ✓ |

- 无 tests/、REMEDIATION_PLAN.md、FIXLOG.md、plugins/ 改动。报告与提交一一对应（本批未被跨报告打包）。
- 备注（非问题）：报告 §composite 根行括注"edit_service / module / preferences / `__init__` 未动再导出"，实际第 4 个文件是 `runtime.py` 而非 `__init__.py`（`__init__.py` 正确未触碰）；措辞性出入，不影响判定。
- 报告自称"未做 git add/commit、改动留工作树"，提交由中心/协调会话后续完成——属流程归属说明，内容核对无异常。

## 2. 未用导入删除抽查（293 绑定，抽样 20+ 名）

对"行内收敛"型改动（删行保留行上在用名）逐一区分**实删名**后验证：

| 文件 | 实删名 | 文件内残留（@HEAD） | 判定 |
|---|---|---|---|
| composite/render/composite_renderer.py | `_PASSTHROUGH_VERTEX/_PASSTHROUGH_FRAGMENT/_BLEND_FRAGMENT/_BLUR_H_FRAGMENT/_BLUR_V_FRAGMENT/_GRADE_FRAGMENT/_TRANSFORM_FRAGMENT/_INVERT_FRAGMENT`（8 常量整块） | 0（定义仍在 core/shaders.py 且由 histogram_manager/shader_library 等消费，无加载时序变化） | ✓ |
| shared/export/export_panel.py | `QMimeData`、`QUrl` | 0 | ✓ |
| composite/evaluators/base_evaluator.py | `CacheCategory`、`GPUResourcePool`、`FusedChain`（函数内惰性导入）、`RenderScheduler`、`is_placeholder_node_type`、`math` | 0（GPUResourcePool 仅剩 1 处 docstring 文字；CacheManager/CacheEntry 为保留名，重写在行内） | ✓ |
| composite/render/composite_render_output.py | `ColorSpaceDescriptor`、`ColorRange`（重写行保留 ColorPrimaries/ColorTransfer） | 0 | ✓ |
| composite/render/render_scheduler.py | `get_node_capability`、`Node`（`is_output_node_type` 保留） | 0 | ✓ |
| app/ui/library_panel.py | `FileIO` 等 | 0（QFileSystemModel 为保留名） | ✓ |
| app/ui/shortcut_dialog.py | `QEvent` 等 | 0 | ✓ |
| ai_harness/tests（test_p2/p3/p4、test_result_card_builder 等） | `PlanMode`、`LlmRoleExecutor/ModelResponse`、`SkillEvalSuite`、`replace`、`dataclass/field` 等 | 0（test_p4_acceptance 的 `run_default_suite` 为保留名，重写在行内） | ✓ |

## 3. 死代码删除抽查（4 处全查）

| 位置 | 名称 | 全仓（src/tests/scripts/plugins）@HEAD | 判定 |
|---|---|---|---|
| topos_cache_codec.py `_sizeof` | `_sizeof` | 0 | ✓ |
| test_runtime.py `_wf_invocation` | `_wf_invocation` | 0 | ✓ |
| html_validation.py `_page_probe_frames` | `_page_probe_frames` | 0 | ✓ |
| dynamics_dialog.py `_knob_value` | `_knob_value` | 0 | ✓ |

对照组（报告声称保留）：`render_scheduler._check_capability_consistency` 仍在（:121 定义 + :141 注释调用）——保留判断正确 ✓。

## 4. 注解零行为证明

- `git show` 全部 def 行改动：除上述 4 个死函数 def 删除外，**仅 `__init__` 行加 `-> None`**（报告称 79 处）；无非 `__init__` 签名改动。
- 新增行全集 = import 重写行 + `-> None`，无其他语句 → 语言层面零行为变化 ✓。

## 5. 红线

- 裸 except：diff 中 except 仅出现在报告文字与 1 个 hunk 头（上下文行），零改动 ✓
- 信号 connect：diff 零 `.connect(` 改动 ✓
- GLSL/色彩矩阵：提交不含 .tglsl、不含色彩矩阵文件 ✓
- 公共 API：零改名；`__init__.py` 再导出未触碰 ✓
- "不动点"声明抽验：对 10 个改动文件跑 AST 未用导入扫描，唯一命中 `base_evaluator.py:1283 AOVConfig` 位于 try 块内降级导入（报告"有意保留"第 2 类），与其规则一致——不动点声明成立 ✓

## 6. py_compile（7 文件抽验）

base_evaluator.py / composite_renderer.py / html_validation.py / dynamics_dialog.py / test_runtime.py / export_panel.py / preferences_dialog.py → 全部 OK。

## 7. 综合判定

**PASS**。范围、删除真实性、零行为证明、红线全部通过。无 Critical/Suggestion 级问题。
