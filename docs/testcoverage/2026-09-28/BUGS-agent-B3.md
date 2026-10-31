# BUGS-agent-B3 — src/app/ui/preferences_dialog.py 补测发现的疑似 bug

- 日期：2026-09-28（补测 agent B3，计划02 测试覆盖补齐）
- 模块：`src/app/ui/preferences_dialog.py`（2,685 行，覆盖 7% → 补测后 90%）
- 新增测试：`tests/app/test_preferences_dialog_coverage.py`（57 用例，全绿）
- 规则：以下问题均 **未修改 src**；其中 01–04 已有"锁定现状"用例，修复时需同步更新对应断言。

---

## BUG-B3-01（Critical）Apply 会清空 file_explorer 的停用插件列表

- 位置：`PreferencesDialog._apply_settings()`（约 2033–2050 行）+ `_setup_ui()`
- 现象：`_plugin_list` 只在用户点击 Advanced > "Refresh Plugins"（`_refresh_plugins`）后才填充；对话框构造时为空。`_apply_settings` 无条件遍历 `_plugin_list` 收集停用项并写回 `file_explorer` 自己的 config.json。因此用户只要打开偏好设置并按 Apply/Save（无需进 Advanced 页），FE 的 `disabled_plugins` 就会被整表清空为 `[]`——之前在 FE 里停用的插件全部被重新启用。
- 加重因素：
  1. 写的是 FE 侧 config（`fe_main.save_config`），绕过宿主 ConfigManager，Cancel-after-Apply 的快照回滚（`_on_cancel`）**无法撤销**该副作用；
  2. `external/file_explorer` 在仓库内可导入，现有测试 `tests/ui/test_developer_mode_prefs.py::test_toggling_developer_mode_requires_restart` 等直接调用 `_apply_settings`/`_on_save`，会在开发者本机真实触发该写回（无 FE 桥隔离）。
- 复现：见 `test_apply_without_refresh_clears_fe_disabled_plugins`（锁定现状：断言 Apply 后 FE config 的 `disabled_plugins == []`）。
- 建议：`_apply_settings` 仅在插件列表非空（即已 Refresh、UI 有真实数据）时才写回 FE config；或构造时静默执行一次 `_refresh_plugins`。

## BUG-B3-02（Minor）Restore Defaults 漏掉 smart 阈值；DEFAULT_CONFIG 缺键

- 位置：`_on_restore_defaults()`（约 2205–2211 行）+ `src/shared/config/config_manager.py::DEFAULT_CONFIG`
- 现象：`render_cache_auto_threshold` 在 `_load_current_settings`（默认 6）与 `_apply_settings`（约 1984 行）均有处理，但：
  1. `DEFAULT_CONFIG` 没有该键（load 只能靠硬编码回退 6）；
  2. `_on_restore_defaults` 的 Render Cache 段没有重置它，恢复默认后 smart 阈值保持用户值。
- 复现：`test_restore_defaults_skips_smart_threshold_lock_in`（锁定现状：恢复默认后阈值仍为 21）。
- 建议：DEFAULT_CONFIG 增加 `"render_cache_auto_threshold": 6`，并在 `_on_restore_defaults` 补一行重置。

## BUG-B3-03（Minor）Timeline 分辨率预设下拉与配置不同步

- 位置：`_load_current_settings()`（约 1860–1879 行，Timeline Defaults 段只回填 width/height spinbox）
- 现象：预设 combo（`_timeline_res_combo`）加载后恒停在第 0 项 "4K UHD"，即使配置是 1080p/720p；用户一旦触碰下拉即触发 `_on_timeline_res_preset_changed` 把宽高覆盖为预设值，与打开对话框时的显示不符（Apply 本身读 spinbox，不触碰下拉则数据无损）。
- 复现：`test_timeline_resolution_preset_combo_not_synced_with_config`（锁定现状：1080p 配置 + combo 显示 4K）。
- 建议：load 时按 width/height 反查匹配的预设索引；无匹配则选 "Custom"。

## BUG-B3-04（Minor）Restore Defaults 漏掉 Pass Fusion Mode

- 位置：`_on_restore_defaults()` Performance 段（约 2164–2189 行）
- 现象：`pass_fusion_mode` 有 load（默认 conservative）与 apply，但恢复默认时未被重置，用户设为 "off"/"aggressive" 后 Restore Defaults 不生效。
- 复现：`test_restore_defaults_skips_pass_fusion_mode_lock_in`（锁定现状：恢复默认后仍为 "off"）。
- 建议：在恢复 Performance 段时补 `pass_fusion_mode` 重置。

---

## 其他观察（未写锁定用例，修复时一并考虑）

- 非法枚举回退到"下拉第一项"而非 DEFAULT_CONFIG 默认值（`_select_combo_by_data` 与各 load 循环的通用行为）：如 `default_timeline_fps` 非法时落到 "23.976"（默认是 "25"）、`log_level` 非法时落到 "debug"（默认 "warning"）、`video_playback_resolution` 非法时落到 3840（默认 1920）。行为一致且不会崩，但与"默认值"语义有偏差（见 `test_unknown_enum_values_fall_back_to_first_choice`）。
- `PreferencePageDescriptor` 文档声称 factory "Must be lazy — only called when user opens the page"，但 `_setup_ui` 对全部页面前置调用 factory；仅注释/实现不一致，无功能影响。
