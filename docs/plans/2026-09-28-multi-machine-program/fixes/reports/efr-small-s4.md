# EFR-Small-S4 清理报告 — 16 个小模块行（E-rollup）

- 日期：2026-10-01
- 分支：dev（工作树留改动，未 git add/commit）
- 范围：16 个指定小模块目录；未触碰其他代理负责的目录（edit/core、edit/ui、teaching_animation/core|ai|render 其余部分、ai_harness、composite/render|render3d|ui、shared/video）
- 红线遵守：零行为变化；except 语义未动（裸 except 只记录）；函数未重排；最小 diff

## 总览

| 类别 | 删除/修改 | 说明 |
|---|---|---|
| 未用 import 删除 | 4 处 | AST 扫描 + 全仓核实后删除；其余判定为刻意模式，保留（见下） |
| 死代码删除 | 4 处（全仓零引用，留证据） | cv_metal_bridge 2 函数、hardware_capability 1 函数、scripting/api 1 私有函数、file_io 1 私有方法 |
| 补类型注解 | 12 处 | 仅 builtin/字符串形式返回类型，def 时求值安全（file_io 无 `__future__ annotations`，已核实） |
| 冗余中间变量 | 1 处 | menu_builder `top` 内联；另 2 处判定保留中间名更可读 |
| 私有命名收敛 | 0 处 | 未发现无风险候选；为守住公共 API/信号名红线，放弃命名改动 |

改动统计：9 文件，+16 / −144 行。

## 按模块行分节

### 1. src/shared/media/macos_vt/（rollup 27 条）
- 已删（3 处死代码，全仓零引用 + 无 `__all__`/`__init__` 导出，证据见"抽样证据"）：
  - `cv_metal_bridge.py`：`cv_pixel_buffer_dimensions`、`lock_pixel_buffer_base_address`（tests/tools 实际使用的是 planes 变体 `lock_pixel_buffer_planes` 与 `unlock_pixel_buffer_base_address`）
  - `hardware_capability.py`：`invalidate_hardware_capability_cache`（`clear_hardware_capability_cache`、`_hw_cache_path` 仍有 5+ 处引用，无孤儿）
- 保留（判定为刻意模式，非死代码）：
  - 13 处 `from __future__ import annotations`：移除会改变注解求值时机 → 行为变化，红线禁止
  - `__init__.py` 8 个 re-export：包公共 API 面，红线禁止
  - `vt_session.py`/`iosurface_gl_bridge.py` 约 23 处"未用 import"：全部位于 `try/except ImportError` 能力探测块，import 本身控制 `_COREVIDEO_LOADED`/`_QUARTZ_CV_LOADED` 等可用性标志，删除即改变边界环境行为
  - `vt_zero_copy_probe.py:450-457`：相对/绝对回退双导入模式，保留

### 2. src/shared/media/windows_hw/（rollup 19 条）
- 改动：0。全部发现为 `__future__ annotations`（21 文件）与 `__init__.py` re-export（约 60 个名字，含 `__all__`），均属"移除即行为变化/公共 API"保留类。453 个顶层定义扫描无一死代码。

### 3. src/features/scripting/（rollup 8 条）
- 已删：`api.py` `_interpolate_curve` — 私有 + docstring 明示 "DEPRECATED: Use _interpolate_curve_255" + 全仓零引用；`_interpolate_curve_255` 存在且有 5 处使用
- 保留：`composite_api.py`/`module.py` 的 `__future__ annotations`

### 4. src/features/composite/adapters/（rollup 8 条）
- 已删：`main_window_adapter.py` 未用 `Optional`（typing 行内，全文件唯一出现）
- 保留：`QMainWindow`（TYPE_CHECKING 块内，供字符串注解 `"QMainWindow"` 使用——AST 误报）
- `caption_input.py` 检查无问题

### 5. src/features/color/ai/（rollup 7 条）
- 已删：`color_context.py` 未用 `Iterable`（`Any`/`Mapping` 在用）
- 保留：`__future__ annotations`

### 6. src/features/teaching_animation/render/html_layer/（rollup 7 条）
- 改动：0。7 条均为 `__future__ annotations`（保留）。8 个文件的私有辅助函数/类全部有引用（含测试 monkeypatch 引用 `_DomCounter`、`_create_standalone_gl_context`）。

### 7. src/shared/io/（rollup 7 条）
- 已删：`file_io.py` `FileIO._apply_raw_tone_and_detail` — 私有静态方法，全仓仅 def 一处（grep 复核 0 引用）
- 已补注解（4）：`_ensure_baked_cache_dir() -> None`、`get_cache_key(...) -> str`（两条返回路径均 md5 hexdigest）、`calculate_wb_gains(...) -> list[float]`、`kelvin_to_rgb_factors(...) -> list[float]`
- 保留：`database.py` 的 `QByteArray` 带 `# noqa: F401 (历史残留；见 AUD-32907)` 审计标注（注：该文件属 shared/db 行）
- 裸 except 记录：`file_io.py:452`、`file_io.py:567`（不修）

### 8. src/app/integrations/file_explorer/（rollup 7 条）
- 已删：`adapter.py` 未用 `import threading`（全文件唯一出现）
- 已补注解（8）：两个 server wrapper 的 `run/stop -> None`、`FlaskWorker.run/_write_port_config/stop -> None`、`get_server_url() -> str`
- 保留：`_default_port` 未补 `-> int`（`const.DEFAULT_PORT` 类型不可验证，拿不准不加）
- 裸 except 记录：`adapter.py:311`（`is_port_in_use` 内，不修）

### 9. src/shared/vault/（rollup 7 条）
- 改动：0。7 条均为 `__future__ annotations`（保留）。bridge/client 私有定义均有引用。

### 10. src/features/color/tracking/（rollup 6 条）
- 已删：`tracking_evaluator.py` 未用 `List`（`Dict`/`Tuple`/`Optional` 在用）
- 保留：4 处 `__future__ annotations`。全模块私有函数均有引用。

### 11. src/shared/assets/（rollup 6 条）
- 改动：0。`icon_manager.py` 扫出 17 个零静态引用的公共 `icon_*` 函数及 `get_icon_by_name`（含整个 `ICON_MAP`），但该文件是全应用共享图标库 API 面（各 panel 按名 import），与 `__init__` re-export 同理保留；建议由 UI 侧 owner 评估后单独清理。

### 12. src/shared/diagnostics/（rollup 6 条）
- 改动：0。6 条均为 `__future__ annotations`（保留）。私有类 `_ProviderSlot`、`_psutil_process` 均有引用。

### 13. src/shared/layout/（rollup 6 条）
- 改动：0。6 条均为 `__future__ annotations`（保留）。

### 14. src/features/color/adapters/（rollup 5 条）
- 改动：0。5 条均为 `__future__ annotations`（保留）。`_encode_bitmap_png`/`_decode_bitmap_png` 均有引用。

### 15. src/shared/db/（rollup 5 条）
- 改动：0。`QByteArray` 带 noqa 审计标注保留；`get_setting(default)` 返回类型不可证明（取决于 DB 存值），不加注解。`__future__ annotations` 保留。

### 16. src/app/menu/（rollup 5 条）
- 已内联：`menu_builder.py` `_category_for_path` 的冗余中间变量 `top`（`top = menu_path.split("/")[0]; return top` → 直接 return）
- 保留：`__future__ annotations`

## 抽样证据

1. **死代码零引用复核**（词法全仓索引 453 个顶层定义 + 方法扫描 + grep 复核）：
   - `lock_pixel_buffer_base_address`：`grep -rn` 全仓仅 `cv_metal_bridge.py:193` 的 def（docstring 声称"测试/诊断用"，但 tests/media/test_cv_metal_bridge.py:35 实际 import 的是 `lock_pixel_buffer_planes`）
   - `_interpolate_curve`：全仓仅 `api.py:297` 的 def；docstring 含 "DEPRECATED: Use _interpolate_curve_255"；后者有 5 处使用
   - `_apply_raw_tone_and_detail`：全仓仅 `file_io.py:749` 的 def
   - `invalidate_hardware_capability_cache`：全仓仅 def；删除后 `clear_hardware_capability_cache`（tests 5+ 处）与 `_hw_cache_path`（本文件 2 处）不受影响
2. **AST 误报保留例**：`main_window_adapter.py:22` `QMainWindow` 在 `if TYPE_CHECKING:` 内，供 `:41` 字符串注解使用；`database.py:9` `QByteArray` 带 `# noqa: F401 (历史残留；见 AUD-32907)`
3. **探测块保留例**：`vt_session.py:283` `CVPixelBufferPoolCreate` 在 `try: from Quartz import (...)` 内，except 分支控制 `_QUARTZ_CV_LOADED` 标志，删除会改变缺失环境下的标志语义

## 验证输出

1. `py_compile`：9 个改动文件全部通过（`PY_COMPILE OK`）
2. 定向测试（未跑 tests/edit）：
   - 批次 1+2：`tests/media/test_cv_metal_bridge.py`、`tests/media/test_macos_vt_hw_capability.py`、`tests/scripting/test_script_api_versioning.py`、`tests/composite/test_fix_wd4g29_aud.py`、`tests/color/test_fix_wd3g20_aud.py`、`tests/color/test_tracking_manual_correction.py` → **197 passed**（4.00s）
   - 批次 3：`tests/video/test_regression_image_video.py`（FileIO）、`tests/shared/test_fix_wd15g83_aud.py`（db）、`tests/architecture/test_layout_store.py`（layout）→ **206 passed, 4 skipped**（3.65s）
   - 已知预存失败对照：`tests/media/test_macos_vt_zero_copy.py` + `tests/media/test_macos_vt_compression.py` → **3 failed, 54 passed, 2 skipped**，失败恰为已知的 3 例（`test_timeline_exporter_native_bridge_multi_frame[True-h264]`/`[True-hevc]`/`multitrack_surface[True]`），**无新增失败**
3. 裸 except 盘点（只记录不修，共 3 处）：`src/shared/io/file_io.py:452`、`src/shared/io/file_io.py:567`、`src/app/integrations/file_explorer/adapter.py:311`

## 改动文件清单（9 个）

| 文件 | 改动 |
|---|---|
| src/shared/media/macos_vt/cv_metal_bridge.py | −2 死函数 |
| src/shared/media/macos_vt/hardware_capability.py | −1 死函数 |
| src/features/scripting/api.py | −1 死私有函数 |
| src/features/composite/adapters/main_window_adapter.py | −1 未用 import |
| src/features/color/ai/color_context.py | −1 未用 import |
| src/features/color/tracking/tracking_evaluator.py | −1 未用 import |
| src/app/integrations/file_explorer/adapter.py | −1 未用 import，+8 返回类型注解 |
| src/shared/io/file_io.py | −1 死私有方法，+4 返回类型注解 |
| src/app/menu/menu_builder.py | −1 冗余中间变量 |

diff：+16 / −144。
