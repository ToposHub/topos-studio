# EFR 批修复报告 —— efr-small-s3：10 个小模块行机械清理（process 级改进落线）

日期：2026-10-01 ｜ 分支：dev（**未做任何 git add/commit，改动全部留在工作树**）｜ REMEDIATION_PLAN.md 未触碰
白名单（仅下列范围，绝不触碰其他代理负责目录）：`src/shared/tracking/`、`src/shared/ai_models/`（根 + generation/）、`src/shared/codec/`、`src/features/shared/effects/`、`src/features/ai_workflow/core/`、`src/features/edit/adapters/`、`src/features/color/`（根）、`src/features/edit/`（根）、`src/features/teaching_animation/presets/`（不含 library/ 模板目录）、`src/features/deliver/`（根）。

红线遵守：零行为变化；裸 `except:` 只记录不修（本白名单内既有即为 **0 处**，见"验证"）；无函数重排；facade/adapter（commands_facade / edit facade / deliver facade）公共转发方法签名一律未动（注解也未加）；私有命名收敛 0 处（评估后不做）；未运行 tests/edit 整目录。

## 总览

| 指标 | 数量 |
|------|------|
| 改动文件 | **42 个 py**（全部在白名单内，+124 / −151 行） |
| 1. 未使用 import 删除 | **34 个绑定 / 22 个文件**（AST 扫描 86 文件 → 逐条全仓核实） |
| 2. 死代码删除 | **7 处 / 3 个文件**（全仓零引用，证据见下） |
| 3. 类型注解补充 | **约 48 处**（参数/返回/容器字段参数化；全部可证明，拿不准一律不加） |
| 4. 私有命名收敛 | **0**（与"绝不改公共 API/信号名"红线冲突风险高，不做） |
| 5. 小清理 | **7 处**（F541 无占位符 f-string ×2、函数内重复局部 import 上提 ×2、弃用 `typing.Mapping` isinstance 收敛 ×1、私有嵌套工厂未用形参删除 ×1、空 TYPE_CHECKING 块删除 ×1） |

## 分模块行统计

### 1. src/shared/tracking/（台账 17 条）—— 4 文件
- 未用 import（2）：`corner_pin.py` `import math`；`homography_stability.py` `Iterable`。
- 注解（7）：`corner_pin.py` 4 个公共导出函数的 `binding` 参数补 `TrackingBinding`（AUD-37305；track_data 不反向依赖 corner_pin，无环，导入冒烟通过）；`track_data.py` `locked/excluded_feature_ids: set[int]`（AUD-37402）；`tracker_2d.py` `_FrameStep.observations: Optional[list[FeatureObservation]]`（AUD-37409）。
- 未做的台账项（行为级，不在机械范围）：37301/37302/37303/37304/37306/37308/37309/37310/37311/37400/37401/37405/37406/37407/37408/37412。

### 2. src/shared/ai_models/ 根 + generation/（台账 12+16 条）—— 9 文件
- 未用 import（11 绑定 / 7 文件）：`ai_mask_engine.py` `QThread, QWaitCondition`（AUD-31513）；`ai_plugin_engine.py` `re`；`denoise_worker.py` `QWaitCondition`；`inference_engine.py` `MODEL_REGISTRY`（全仓无 `from ...inference_engine import MODEL_REGISTRY`）；`model_manager.py` `Callable, asdict`（AUD-31812）；`rife_interpolator.py` `Optional, Tuple`；`swinir.py` `einops.rearrange`；`generation/model_pack_manager.py` `field`。
- 注解（2）：`swinir.py` 模块级 `window_partition/window_reverse`（AUD-31813 局部落线；类内体量过大且模块 DORMANT，按最小 diff 未铺开）。
- 小清理（3）：`generation/generation_models.py` `with_state`/`with_preview_handle` 内重复局部 `from dataclasses import replace` 上提到模块头（AUD-31609）；`generation/volcengine_generation_provider.py` `isinstance(x, typing.Mapping)` 弃用用法改 `collections.abc.Mapping`（AUD-31713 局部；导入行为等价）。
- 台账纠偏：AUD-31713 称 "`urllib.parse` 未使用" —— 实测 :325 `urllib.parse.urlparse` 在用，**保留**。
- 未做（行为级）：31504/31505/31512/31524/31602/31604/31605-31608/31612/31613/31622/31706-31712/31805/31806/31814（onnx_env 返回值类型不可证）/31817 大部分。

### 3. src/shared/codec/（台账 16 条）—— 5 文件
- 死代码（2，AUD-32310）：`topos_profiles.py` `_bpp_from_mb_s()` 与 `TRAW_RATIO_NOMINAL` —— 全仓 grep（src/ tests/）各仅命中定义行自身，零引用零导入。
- 未用 import（1）：`topos_image_binding.py` `Path`。
- 注解（10+）：`color_metadata_config.py` `mastering_display: Optional[tuple[float, ...]]`（AUD-32102）+ `VideoMetadata` TYPE_CHECKING 导入修复悬空注解（AUD-32103）；`slice_threads_policy.py` `_order: list[str]`、`_setter/_getter: Optional[Callable[...]]`、`bind()` 全参注解（AUD-32110）；`decode_worker_budget.py` `__exit__(*_exc: object)`、`describe()/_snapshot_locked() -> Dict[str, Any]`（AUD-32107）；`topos_image_binding.py` `ToposImageInfo.raw: dict[str, Any]`、`library_identity() -> tuple[str, int, int]`、`_CFileIo.byref() -> ctypes.CArgObject`（AUD-32306）。
- 未做（行为级/不可证）：32105/32109/32206/32207（`make_views` 的 numpy 可选依赖使参数类型不可证）/32300/32301/32305/32308/32309/32311。

### 4. src/features/shared/effects/（台账 14 条）—— 5 文件
- 死导入（1，AUD-26311）：`effect_catalog.py` `_dctl_param_to_pdef` 内 `_PARAM_GLTYPE` 局部导入（函数体内零使用）。
- 未用 import（2）：`effect_pass_runner.py` `QUALITY_LABELS`；`plugin_identity.py` `field`（仅 docstring 提及）。
- 注解（3，AUD-26306）：`effect_capability.py` `reasons: FrozenSet[Tuple[str, str, str]]`、`quality_constraints: tuple[str, ...]`、局部 `reasons: set[Tuple[str, str, str]]`（元素类型经全部 5 处 `reasons.add` 三元 str 组核实）。
- 增量 re-export（1，AUD-26300）：`__init__.py` 补 `EffectExecutionCapability`（并入既有 effect_capability 导入块，纯增量绑定，无环、冒烟通过）。
- 未做（行为级）：26301/26303/26304/26305/26309/26310/26403/26404/26406/26407/26409/26411。

### 5. src/features/ai_workflow/core/（台账 12 条）—— 5 文件
- 未用 import（9 绑定 / 3 文件）：`template_registry.py` `Iterable, List, WorkflowUi`；`workflow_compiler.py` `List, ValidationFinding`；`workflow_validator.py` `field, PortRef, WorkflowEdge, WorkflowNode`（WorkflowGraph 保留）。
- 死代码（4，AUD-23214/23204）：`workflow_models.py` `WorkflowPolicy._NETWORK_VALUES/_TIMELINE_VALUES`（全仓 grep 仅命中定义行，validation 用内联字面量）；`publish_as_skill.py` `_bump_to_version` 死变量 `current = draft.version` + 失实注释删除。
- 注解（2，AUD-23205）：`publish_as_skill.py` `_infer_evidence_kinds() -> list["SegmentKind"]`、`kinds: list[SegmentKind]`（TYPE_CHECKING 导入）。
- 未做：`register_into(registry: Any)` 未收紧 —— 下游 `SkillReleaseGate.register_into` 本身即 `Any` 鸭子契约，不可证；其余 23202/23203/23208/23211-23213/23215/23216/23220 行为级。

### 6. src/features/edit/adapters/（台账 11 条）—— 2 文件
- 未用 import（1）：`interchange_types.py` `field`。
- 注解（4，AUD-12615/12616）：`interchange_types.py` `ImportResult.sequences: tuple["TimelineSequence", ...]`（仅 fcpxml/otio 两适配器构造，均产出 timeline_v2.TimelineSequence）；`import_builder.py` `_remap_track(track: EditTrack) -> EditTrack`、`sequences_out: dict[str, TimelineSequence]`（TYPE_CHECKING，无运行时环）。
- 未做（行为级）：12609-12614/12617-12623。

### 7. src/features/color/ 根（台账 11 条）—— 3 文件
- 死代码（3，AUD-24602）：`module.py` `get_facade()`、`initialize_facade()`、`self._facade` 属性 —— 全仓 grep（src/ tests/）除定义/赋值行外零引用。
- 未用 import（4，AUD-24603 + 连带）：`module.py` `FeatureModule`、`ColorFacade`、`Any`、`Optional`（死代码删除后连带失效）。
- 注解（6，AUD-24604/24618）：`module.py` `register_menu_actions/register_shortcuts/register_panels` 参数（TYPE_CHECKING 引用 `"ActionRegistry"/"ShortcutManager"/"PanelRegistry"`）；`preferences.py` `create_widget(parent: QWidget)`、`_set_combo_data(value: object)`、`apply(config_manager: ConfigManager)`（调用点 `preferences_dialog.py:2086 cfg = self._config_manager` 证实）。
- 小清理（3，AUD-24402/24615）：`facade.py` 2 处无占位符 f-string 去 `f`（F541）；`preferences.py` `_make_transfer_combo` 未用形参 `default` 删除（唯一调用点同步更新）。
- 未做（行为级）：24101（__init__ eager import 是公共导入面变化）/24613/24614/24616/24617。

### 8. src/features/edit/ 根（台账 10 条）—— 4 文件
- 未用 import / 死块（2）：`module.py` `FeatureModule`；`commands_facade.py` 空 `if TYPE_CHECKING: pass` 块及其导入。
- 注解（7，AUD-19304/19309）：`module.py` `self._page: Optional["EditPage"]`、`create_edit_page() -> "EditPage"`、`get_edit_page() -> Optional["EditPage"]`、`register_menu_actions/register_shortcuts/register_panels` 参数；`preferences.py` `create_widget(parent: QWidget)`。
- 红线跳过（记录）：AUD-15100 的 12 个 facade 转发函数返回注解**未加**（facade 公共转发签名不动）；AUD-15102 `create_teaching_animation_clip_command` 无生产调用方但属公共门面函数**未删**。
- 未做（行为级）：15101/19300/19303/19306/19307/19308。

### 9. src/features/teaching_animation/presets/（台账 10 条，library/ 模板目录未触碰）—— 2 文件
- 注解（4，AUD-28303/29703）：`fragment.py` `_rebuild_track(remapped_frames: list[Keyframe])`、`by_element: dict[str, list[KeyframeTrack]]`；`loader.py` `by_element: dict[str, list[KeyframeTrack]]`（元素类型经 `SceneAnimation.track: KeyframeTrack` 与 `add_keyframes_bulk(List[Keyframe])` 核实）。
- 未做：28302/28310/28311/28322/28324/28325/29706/29707 行为级；AUD-28700（line_curve_chart 未用导入）位于 library/ 子目录，按任务边界不触碰。

### 10. src/features/deliver/ 根（台账 9 条）—— 4 文件
- 未用 import（2）：`facade.py` `Optional`；`module.py` `FeatureModule`。
- 注解（4，AUD-12104/12108）：`module.py` `register_menu_actions/register_shortcuts/register_panels` 参数；`preferences.py` `create_widget(parent: QWidget)`。
- 红线跳过（记录）：AUD-12102 `get_deliver_page_class` 注解未加（facade 公共转发函数）。
- 未做（行为级）：12101/12103/12105/12106/12107/12109。

## 死代码 / 死变量证据（全仓 grep，src/ + tests/）

- `src/shared/codec/topos_profiles.py`：`grep -rn "TRAW_RATIO_NOMINAL\|_bpp_from_mb_s" src/ tests/` → **各仅 1 处 = 定义行**（AUD-32310）→ 已删。
- `src/features/ai_workflow/core/workflow_models.py`：`grep -rn "_NETWORK_VALUES\|_TIMELINE_VALUES" src/ tests/` → **仅 :188/:191 定义行**（AUD-23214）→ 已删。
- `src/features/color/module.py`：`grep -rn "get_facade\|initialize_facade" src/ tests/ --include="*.py"` → 排除定义文件后 **0 处**（AUD-24602）→ 连同 `_facade` 属性删除。

## 抽样核实方法（未用 import 三关）

1. AST Import/Name/Attribute 收集（86 个白名单文件全扫）→ 候选绑定；
2. 逐条文件内 `\b<name>\b` 原文扫描（排除 import 行）+ **字符串注解复核**（如 `srt_exporter.py` 的 `SubtitleClip/TimelineSequence` 在字符串注解中使用 → 保留；`plugin_identity.py` 的 `EffectSpec` 同理 → 保留）；
3. 全仓反向导入检查（`from ... import <name>` / `<mod>.<name>`）+ `__init__.py` re-export 一律视为公共面**不删**（tracking/effects/ai_models/codec/presets/color/edit/deliver 各 `__init__.py` 的 re-export 均保留）；`otio_adapter.py` 的 `import opentimelineio` 是可用性探针（带 `# noqa: F401`）→ 保留。

## 有意保留（评估后不动）

- 全部 `from __future__ import annotations`（42 处候选）—— 语义性导入，删除有注解求值风险；
- 各 `__init__.py` re-export、`otio_adapter` 可用性探针导入；
- `commands_facade.py` 12 个公共函数、`edit/deliver facade.py` 转发函数 —— 红线：facade 公共转发签名不动；
- AUD-15102（公共门面函数无调用方）—— 删除属公共面变化，只记录。

## 验证（全部通过）

1. **py_compile**：42 个改动文件逐一编译 → `PY_COMPILE ALL 42 OK`。
2. **定向测试**（均无新增失败；未运行 tests/edit 整目录）：
   - tracking：`tests/shared/tracking/test_tracker_corner_pin_export.py` + `test_tracker_homography_stability.py` → **26 passed**
   - codec：`tests/media/test_topos_budget.py` + `test_codec_label.py` → **28 passed**
   - ai_models：`tests/shared/test_fix_wd16g81_aud.py` + `test_fix_wd15g80_aud.py` → **92 passed**
   - edit/adapters：`tests/edit/test_interchange.py` → **43 passed**；门面：`tests/edit/test_edit_commands.py` → **31 passed**
   - 额外安全网：effects `test_effect_spec.py`+`test_fix_effect_pass_aud5b.py` → **61 passed**；ai_workflow core 5 文件（template_registry/publish_as_skill/workflow_models/workflow_validator/workflow_compiler）→ **94 passed**；presets `test_fix_wd16g77_aud.py`+`test_fix_wd17w1_aud.py` → **80 passed**
   - 模块导入冒烟：`src.features.ai_workflow.core.*`、`src.features.shared.effects`、`src.shared.codec.*`、`src.features.color.*`、`src.features.edit.*`、`src.features.deliver.*`、`tracking.corner_pin` 全部 import OK
3. **裸 except 记录**（只记录不修）：`grep -rn "except:" <白名单目录>` 共 **17 处**，其中 `src/features/color` 15 处、`src/features/edit` 2 处 —— **全部位于其他代理负责的子目录**（`color/core/image_engine.py` 1 + `color/core/async_renderer.py` 14；`edit/ai_editing/assistant_service.py` 2 处实为 docstring/注释提及）；**本批改动的 42 个文件与 mechanical 白名单子树（tracking/ai_models/codec/effects/ai_workflow/core/edit/adapters/presets/deliver 根）裸 except 为 0**。

## 改动文件清单（42）

```
src/features/ai_workflow/core/publish_as_skill.py      src/features/ai_workflow/core/template_registry.py
src/features/ai_workflow/core/workflow_compiler.py     src/features/ai_workflow/core/workflow_models.py
src/features/ai_workflow/core/workflow_validator.py    src/features/color/facade.py
src/features/color/module.py                           src/features/color/preferences.py
src/features/deliver/facade.py                         src/features/deliver/module.py
src/features/deliver/preferences.py                    src/features/edit/adapters/import_builder.py
src/features/edit/adapters/interchange_types.py        src/features/edit/commands_facade.py
src/features/edit/module.py                            src/features/edit/preferences.py
src/features/shared/effects/__init__.py                src/features/shared/effects/effect_capability.py
src/features/shared/effects/effect_catalog.py          src/features/shared/effects/effect_pass_runner.py
src/features/shared/effects/plugin_identity.py         src/features/teaching_animation/presets/fragment.py
src/features/teaching_animation/presets/loader.py      src/shared/ai_models/ai_mask_engine.py
src/shared/ai_models/ai_plugin_engine.py               src/shared/ai_models/denoise_worker.py
src/shared/ai_models/generation/generation_models.py   src/shared/ai_models/generation/model_pack_manager.py
src/shared/ai_models/generation/volcengine_generation_provider.py
src/shared/ai_models/inference_engine.py               src/shared/ai_models/model_manager.py
src/shared/ai_models/rife_interpolator.py              src/shared/ai_models/swinir.py
src/shared/codec/color_metadata_config.py              src/shared/codec/decode_worker_budget.py
src/shared/codec/slice_threads_policy.py               src/shared/codec/topos_image_binding.py
src/shared/codec/topos_profiles.py                     src/shared/tracking/corner_pin.py
src/shared/tracking/homography_stability.py            src/shared/tracking/track_data.py
src/shared/tracking/tracker_2d.py
```

合计：**+124 / −151 行**。
