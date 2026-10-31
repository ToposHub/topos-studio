# 计划03 P4 — 插件缺陷记录（DEFECTS）

> 日期：2026-09-28（P4 执行于 2026-09-29）
> 范围：本批 12 个新插件（Film_Grain、Soft_Glow_Bloom、Halation_Extended、
> Three_Way_Tint、Bleach_Bypass、Radial_Blur_Pro、Prism_Aberration、
> Vignette_Shapes、Day_Night_Converter、Cross_Process、Skin_Even_Matte、
> Fake_Infrared）。
> 验证入口：`tests/plugins/test_plugin_batch1.py` + `tests/plugins/test_plugin_registry.py`
> （`.venv/bin/python -m pytest tests/plugins/ -q -p no:cacheprovider`）。
> 红线约束：P4 只新增测试文件，不修插件 / 不改 src / 不改 locales —— 以下条目
> 均为记录备查，未做任何修改。

## 结论

**0 个阻断性插件缺陷。** 12/12 插件通过：真实解析路径（`plugin_manager._parse_plugin_file`）、
必需元数据、参数 schema（min<max、默认值在区间内、select 选项、color 三分量）、
spec 参数表对齐、循环上限编译期常量、无未声明 `u_*`、无商标名词、
`scan_plugins` 可发现、zh_CN/en_US locale 命名空间完整。126 个用例全绿。

以下为非阻断的低severity观察项。

---

## D-1（Low，仅 docs）：i18n 片段 JSON 两种形状混用

- 位置：`docs/plugins/2026-09-28/i18n/*.json`
- 现象：12 个片段用了两种不兼容的 JSON 形状：
  - 形状 A（仅 `Film_Grain.json`）：顶层键 = plugin_id，name/params 的值再按
    locale 分键（`{"Film_Grain": {"name": {"zh_CN": …, "en_US": …}, …}}`）；
  - 形状 B（其余 11 个）：顶层键 = locale，内部镜像 live locale 布局
    （`{"zh_CN": {"plugins": {"<id>": {"name": …, "params"/"desc": …}}}, "en_US": …}`）。
- 影响：无功能影响——正式数据在 `src/shared/i18n/locales/zh_CN.json` /
  `en_US.json` 中且已验证完整；docs 片段只是交付物存档。但后续批次若沿这两种
  形状继续混写，汇总脚本需要双格式兼容。
- 建议（后续批次统一）：全部采用形状 B（与 live locale 布局一致，可直接粘贴合并）。
- 测试侧处理：`tests/plugins/test_plugin_registry.py::test_docs_i18n_fragment_covers_both_locales`
  对两种形状做了归一化校验（双 locale 均需有非空 name），不为任一形状报错。

## D-2（Low，capability 值域）：`@export_support opengl` 不在文档值域内

- 位置：`plugins/Three_Way_Tint.tglsl`、`plugins/Day_Night_Converter.tglsl`
  （各自 spec 第 128/131 行同样声明，且注明"沿用 warm_tint 已验证先例"，属有意决策）。
- 现象：STYLE_NOTES §4 引用的指南值域为 `all / video / lut / none`；`opengl`
  是 warm_tint 的历史遗留写法（STYLE_NOTES 明确将其列为"实测与指南不一致"项，
  且告诫不得模仿的是 Color_Space_Transform 的商标选项表，未禁用该值）。
- 影响：解析器不校验 capability 值（`plugin_manager._parse_capabilities` 原样保留），
  无运行时故障；仅当导出管线未来按 `all/video/lut/none` 枚举语义消费该值时，
  这两个插件的声明语义不明确（推测意图 = 仅预览、不可导出）。
- 建议（后续）：统一改为 `none`（若确为不可导出）或在指南中把 `opengl` 纳入值域并定义语义。

## D-3（信息项，与本批无关）：既有失败测试 `test_effects_panel_has_no_hardcoded_cjk_text`

- 位置：`tests/core/test_content_localization.py::test_effects_panel_has_no_hardcoded_cjk_text`
- 现象：P4 交叉验证时发现该测试失败（检出 edit/effects 面板源码中的硬编码中文
  字符串，如"出场时长"、"移动距离"等）。**单独运行该文件同样失败**（与本批
  12 个插件及新增测试无关，非测试间污染）。
- 处理：超出 P4 红线范围，未做任何修改，仅记录供相关负责人跟进。

---

## 附：P4 验证覆盖清单

| 检查项 | 结果 |
|---|---|
| 真实解析路径解析（headless `_parse_plugin_file`） | 12/12 通过 |
| 必需元数据（@name/@i18n_name=stem/@author/@version/@description） | 12/12 通过 |
| 色彩空间声明（input/output = scene-linear） | 12/12 通过 |
| 参数 schema 完整性（按控件类型分项校验） | 12/12 通过 |
| spec 参数表与实现参数集合一致 | 12/12 通过 |
| plugin_id 唯一且 = 文件名 stem | 12/12 通过 |
| for 循环上限编译期常量（含宏参数字面量实例化）/ 无 while | 12/12 通过 |
| 无未声明 `u_*`（白名单 u_input_tex/u_tex_size/u_aspect_ratio，忽略 u_P*） | 12/12 通过 |
| 元数据无商标名词 | 12/12 通过 |
| `scan_plugins` 全量发现（临时目录隔离，17 个 .tglsl） | 通过 |
| zh_CN / en_US `plugins.<id>` 命名空间 + name + params.<var> 精确对齐 | 12/12 通过 |
| clamp 包裹最终输出粗检 | 按任务约定跳过（易误报） |
