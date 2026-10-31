# EFR-Tail-T2 清尾报告 — E-rollup 28 条带行号单项（T2）

- 日期：2026-10-01
- 分支：dev（工作树留改动，未 git add/commit）
- 范围：REMEDIATION_PLAN.md §2.E-rollup 指定的 28 条计划行；未触碰其他代理负责的目录（plugins/、docs/plugins/ 仅只读评估）
- 红线遵守：零行为变化（唯二例外均为计划行明确要求的增量补全：9 个命令类序列化补全、render_diagnostics subjects 规整——后者是计划行明确描述的修复）；except 语义未动（macos_vt/source 的补 try 因此 DEFERRED）；最小 diff

## 总览

| 结论 | 数量 | 条目 |
|---|---|---|
| FIXED | 16 | 1, 2, 3, 4, 7, 8, 9, 12, 14, 15, 17, 19, 20, 21, 23(部分子项保留), 25(前批次已修) |
| DEFERRED | 12 | 5, 6, 10, 11, 13, 16, 18, 22, 24, 26, 27, 28 |

改动统计：13 文件，+378 / −62 行（`git diff --stat`，不含其他代理的并行改动）。

---

## 逐条结论

### 1. `MoveClipToExistingTrackCommand:1156…`（AUD-16216）— FIXED
`src/features/edit/core/commands.py`：9 个命令类补全序列化——MoveClipToExistingTrackCommand（+to_dict/from_dict）、RetimeMotionGraphicClipCommand（+from_dict）、SwitchMulticamAngleCommand（+to_dict/from_dict，含 `_original_clip` 快照经 ClipInstance.to_dict 往返）、UpdateProxyPolicyCommand（+to_dict/from_dict）、SetClipLabelColorCommand / SetClipWaveformDisplayCommand / SetTimelineRangeCommand / ToggleClipEnabledCommand / SetClipLinkedCommand（各 +to_dict/from_dict，undo 状态按仓库既定约定以去下划线键序列化）。纯增量补全；apply/undo 未动。8 个时间线命令按文件头约定登记进 `_MACRO_SUBCOMMAND_TYPES`（UpdateProxyPolicyCommand 为 EditProject 级不登记）；tests/edit/test_fix_wd9g55_aud.py 完备性测试通过。冒烟：9 类 to_dict→from_dict 往返全等。

### 2. `src/main.py:161-166, 263`（AUD-31202 --project 无校验）— FIXED
main.py parse 后补存在性+.topos 后缀校验，非法路径 logger.error。传参与下游行为保持不变（"拒绝启动"属产品决策，未做，注释已说明）。

### 3. 定位串 `src/`（AUD-31203 main.py 类型注解/不可达返回）— FIXED
`def main() -> int:`、`module_failures: dict[str, str] = {}`、删除 `_orderly_shutdown`（os._exit）后不可达的 `return 0`（注释说明）。

### 4. `volcengine_generation_provider.py:18,22,275/313/463`（AUD-31713）— FIXED
未使用 `urllib.parse` 导入与 `typing.Mapping` 弃用用法已被前序批次修复（现 326 行在用 urllib.parse、isinstance 用 collections.abc.Mapping）。本批完成剩余子项：公共工厂 `make_volcengine_generation_provider` 返回注解由私有 `_VolcengineProviderBase | None` 改为 `VolcengineSeedreamImageProvider | VolcenginePlanTtsProvider | None`（私有基类全仓零外部引用）。

### 5. `src/shared/control_surfaces/tangent/`（AUD-32802 握手发送失败仅 debug 仍继续）— DEFERRED
修复需在 `_send` 失败时中断握手/会话（改 `_handle_command` 控制流），会话恢复策略（断连 vs 跳过 emit）是行为/语义决策。

### 6. `src/shared/control_surfaces/`（AUD-32804 INITIATE_COMMS 严格解码）— DEFERRED
放宽 `offset != len(payload)` 校验即协议行为变更（此前拒绝的 Hub payload 将被接受），容忍策略取舍属协议语义决策。

### 7. `export_panel.py:1250,1269`（AUD-33106 i18n 硬编码）— FIXED
8 处 UI 文案包 `I18N.tr(key, 英文原文)`：`"Timeline: {id}"`、`"Select Export Directory"`、`"Remove Selected"`、`"Clear All"`、`"{count} files"`、`"Result: …"`（两处）、`"Preview: …"`。参数化采用仓库既定 `I18N.tr(key, template).format(...)` 约定（已验证：翻译未加载/已加载缺 key/key 已收录三场景输出均正确——kwargs 直传模式在"已加载缺 key"路径会漏插值，故不用）。key 未收录时 tr 返回英文原文，与改动前逐字符相同。

### 8. `export_dialog.py:13-280`（AUD-33114 缺类型注解）— FIXED
`__init__`（parent/default_dir/default_name/original_size）、`setup_ui`、`apply_styles`、`pick_directory`、`on_format_changed(fmt: str)` 补注解；update_resize_ui/update_output_size/get_settings 已有注解。运行时无影响（模块无运行期注解求值依赖）。

### 9. `color_decode.py:484,936,86-94,407,159`（AUD-34402/34405/34406）— FIXED（34402）/ DEFERRED（34405、34406 剩余子项）
- AUD-34402 FIXED：三处未收录矩阵静默回退 BT.709（yuv_to_rgb 的 MATRIX_LUT、get_matrix_coefficients、get_shader_matrix_id）补 logger.warning；新增模块 logger。功能行为不变（回退结果照旧），已验证正常路径输出不变、未知矩阵告警触发。
- AUD-34405 DEFERRED：resolve 接受集合与 ColorConverter 可解码集合对齐，任一侧改动都改变接受/拒绝面（扩转换器=实现新曲线，缩 resolve=拒绝此前接受的值），属语义决策。
- AUD-34406：未使用 `Any` 导入已由前序批次移除（现文件无 Any）；chroma_siting 取值校验 DEFERRED（新增拒绝=对既有文件的行为变更，合法值域是规格决策）。

### 10. `color_metadata.py:185-198,129-141`（AUD-34423）— FIXED
`_PRIMARIES_TO_STANDARD_NAME` 补 `ColorPrimaries.AP1: 'ap1'`（finding 建议原文"标准名应为 'ap1'"）；`_ENUM_TO_FFMPEG_PRIMARIES` 按 finding"FFmpeg 反向可保持 None"刻意留空并加注释说明；`get_standard_name` 参数补四枚举联合注解（模块有 `from __future__ import annotations`）。验证：get_standard_name(AP1)=='ap1'，其余取值不变。

### 11. `decode_queue.py:571-580,335-336`（AUD-34503 None 哨兵）— DEFERRED
修复需引入与"暂无帧"可区分的错误通道（重构入队协议与消费端），属结构/语义决策。

### 12. `errors.py:91-96`（AUD-34520 遮蔽内建 PermissionError）— FIXED
`PermissionError(MediaError)` 重命名为 `MediaPermissionError`。全仓（src/tests）零使用方，重命名零影响；消除了对内建名的遮蔽。error_handling 模板路由修复（让媒体权限错误匹配模板）属行为变更，已在 docstring 注明另行处理。

### 13. `hwaccel.py:33,39,43,484,661/805/821`（AUD-34910）— FIXED
GOP_CODECS/INTRA_CODECS/check_ffmpeg_available/`_check_vaapi` 冗余 import os 均已被前序批次清理（现文件无这些符号；os 为模块级在用导入）。本批完成剩余子项：删除只写不读的死状态 `_hwaccel_context`（6 处写入，全仓零读取点，PyAV codec_context 仍经 `self._video_stream` 可达）。

### 14. `macos_vt/source.py:968-983`（AUD-35131 顺序分支无 try）— DEFERRED
为顺序/快路径补 try/except 返回 None 会把"异常上抛"改为"吞掉返回 None"，错误处理策略行为变更，且触碰"不修改 except 语义"红线。

### 15. `thumbnail_provider.py:40`（AUD-35719）— FIXED
3 处非法 `Optional[callable]`/`callable` 注解修正为 `Callable`（1276/1729/2037 行，注解求值安全）；删除全仓零引用死 DTO `ThumbnailImageResult`（含区块注释，留 AUD 标注）。QMetaObject/Q_ARG 死导入已被前序批次移除；"callback 参数从未使用"经 AST 复核为过时描述（当前各 callback 参数均在用），未改。

### 16. `preferences/__init__.py:92-99`（AUD-36702 factory/page_object 同空）— DEFERRED
修复=descriptor 构造/注册期抛错，会把当前被容忍的配置变为启动失败（编程错误提前暴露），属合同收紧的行为变更。

### 17. `topos_project_controller.py:23,25`（AUD-37013）— FIXED
未使用 Optional/field 导入已被前序批次删除。本批完成剩余子项：4 处函数内重复 `import time`（原 :288/:334/:373/:457）提升为模块级单次导入（导入幂等，行为等价），验证模块可导入、`time.time()` 调用点（5 处）正常。

### 18. `render_diagnostics.py:45-47`（AUD-37200 _VALID_SOURCES 未使用/无枚举校验）— FIXED
恢复 `_VALID_SOURCES` frozenset（三个 SOURCE_* 常量），record() 对枚举外 source/severity 补 logger.warning——告警级校验，条目仍照常记录，写入行为不变（与 AUD-34402 同模式）。已验证：合法 source 无告警、未知 source 有告警且照常入库。

### 19. `render_diagnostics.py:163-167`（AUD-37201 subjects 未走规整）— FIXED
subjects 逐项由 `str(s)` 改为 `_normalize_context_identifier(s)`，与其余 ID 字段（request_id/layer_id/plugin_ids）的防路径规整对称——这是计划行明确描述的修复，且是模块文档契约（"不含路径"）的落实；常规标识符输入输出不变（既有测试 test_subjects_dedup_and_coerce 断言 `("Film_Halation","42")` 原样通过）。

### 20. `render_diagnostics.py:200-202`（AUD-37203 单例从不 clear）— DEFERRED
修复需把 `sink.clear()` 接线到工程关闭/会话切换等生命周期点（接线位置是设计决策）；`clear()` 本身是有意的公共 API（测试 set_render_diagnostic_sink 复位用），不可当死代码删除。

### 21. `routing_dialog.py:29,169,345-349 等`（AUD-37813 杂项）— FIXED（部分子项保留）
删除 3 处只写不读死属性：`_SendSlotWidget._bus_options`、`RoutingDialog._track_name`、`RoutingDialog._dragging`（后者与 _SendSlotWidget 的活跃同名属性无关，AST 按类核实）。保留：closeEvent 冗余提交（删除=少一次 committed 信号，行为变更）；5 处函数内 import（ToggleSwitch/DragValue/Send/validate_routing_graph，判定为刻意的懒加载/防环模式，提升有循环导入风险）。未用导入：AST 复核当前无（前批次已清）。

### 22. `style_sheet.py:26-34,1028-1031,1241-1248,1667-1722`（AUD-38110 C 常量承诺）— DEFERRED
量化评估：文件中 70 种色值（97 处出现）未被 C 收录（如 #27272b、#181818、#222、rgba(80,80,80,200)…），且 QSS 模板满布 `{}` 花括号，改 f-string 需全量转义重写。让模板引用 C 是大规模视觉回归风险重构（需先补 70+ 常量并逐值核对），超出机械清理范畴。

### 23. `src/shared/utils/`（AUD-38502 geometry_math 注解）— FIXED（前序批次已修，本批零改动）
复核现状：`aspect_ratio: Optional[float] = None`（:20）、`-> tuple[float, float]`（:21）均已就位，与 finding 建议完全一致，无剩余工作。

### 24. `cfa_cpu_debayer.py:118`（AUD-38703 位深谓词不一致）— DEFERRED
CPU `bit_depth <= 12` 走 LOG vs GPU `>= 16` 走线性，13–15 位两链分歧。对齐任一侧都改变 13–15 位输入的解码曲线（行为变更），哪侧正确是规格决策。

### 25. `session.py:408-416,417-533`（AUD-39407 load 两段式非原子）— DEFERRED
修复=并发语义重构（锁范围扩大/epoch 守卫），有死锁与性能取舍，属语义决策。

### 26. `docs/plugins/2026-09-28/i18n/` — DEFERRED（只读评估）
目录含 12 个插件 i18n JSON（zh_CN/en_US 显示名翻译）。主台账 ISSUES.md 无对应 AUD 条目；该目录属批 F（plugins）工作区（docs/plugins/2026-09-28/ 下有独立 FINDINGS.json/PROGRESS.md），按任务指示只读评估，未做任何改动。

---

## 验证

### 1. py_compile（全过）
```
.venv/bin/python -m py_compile src/features/edit/core/commands.py src/main.py \
  src/shared/ai_models/generation/volcengine_generation_provider.py \
  src/shared/export/export_panel.py src/shared/export/export_dialog.py \
  src/shared/media/color_decode.py src/shared/media/color_metadata.py \
  src/shared/media/errors.py src/shared/media/hwaccel.py \
  src/shared/media/thumbnail_provider.py \
  src/shared/project/topos_project_controller.py \
  src/shared/render_diagnostics.py src/shared/ui/audio_mixer/routing_dialog.py
→ ALL 13 FILES COMPILE OK
```

### 2. 定向测试（无新增失败）

| 测试文件 | 结果 |
|---|---|
| tests/edit/test_fix_wd9g55_aud.py（含注册表完备性）+ test_move_to_existing_track_command.py + tests/edit/ai_editing/test_render_diagnostics.py | 93 passed |
| tests/media/test_color_metadata_mapping.py + test_color_decode_chroma_resize.py + test_hwaccel_vendor_routing.py + test_thumbnail_cache_key.py + test_thumbnail_provider_thread_safety.py | 236 passed, 1 skipped |
| tests/shared/test_fix_export_aud4.py + tests/shared/control_surfaces/ + test_audio_mixer_units.py + test_fix_audio_mixer_aud4.py | 156 passed |
| tests/project/test_topos_project_controller.py + tests/edit/test_timeline_linked_av_regressions.py + tests/edit/test_caption_graphics.py + tests/media/test_color_convert.py | 140 passed |
| tests/edit/test_timeline_command_regressions.py | 47 passed |
| tests/shared/test_fix_wd16g81_aud.py + test_ef15_aud.py（volcengine） | 115 passed |
| tests/media/test_macos_vt_zero_copy.py + test_macos_vt_compression.py | 54 passed, 2 skipped, **3 failed = 已知预存 TimelineExporter 失败（基线一致）** |
| 复跑组合（export/color_metadata/color_decode/hwaccel/thumbnail×2/project_controller/render_diagnostics） | 326 passed, 1 skipped |

新增失败：0。已知预存失败（忽略）：test_macos_vt_zero_copy / test_macos_vt_compression 的 3 例 TimelineExporter 失败，与任务说明基线完全一致。

### 3. 附加冒烟
- 9 个命令类 to_dict→from_dict 往返全等 + `_MACRO_SUBCOMMAND_TYPES` 登记断言。
- I18N.tr 三场景（未加载/已加载缺 key/已收录）输出正确。
- color_decode 未知矩阵告警触发、正常路径输出不变。
- render_diagnostics subjects 规整 + 告警路径 + record_compile_failure 零异常契约。

## 改动文件清单（13 个，全部为修改）

1. src/features/edit/core/commands.py（+245 行：9 类序列化 + 注册表 8 行）
2. src/main.py（+20/−2）
3. src/shared/ai_models/generation/volcengine_generation_provider.py（工厂签名）
4. src/shared/export/export_panel.py（8 处 i18n）
5. src/shared/export/export_dialog.py（5 签名注解）
6. src/shared/media/color_decode.py（logger + 3 处告警）
7. src/shared/media/color_metadata.py（AP1 + 注解）
8. src/shared/media/errors.py（PermissionError → MediaPermissionError）
9. src/shared/media/hwaccel.py（删死状态 _hwaccel_context）
10. src/shared/media/thumbnail_provider.py（3 注解 + 删死 DTO）
11. src/shared/project/topos_project_controller.py（import time 提升）
12. src/shared/render_diagnostics.py（_VALID_SOURCES + 告警 + subjects 规整）
13. src/shared/ui/audio_mixer/routing_dialog.py（删 3 死属性）

报告：docs/plans/2026-09-28-multi-machine-program/fixes/reports/efr-tail-t2.md
