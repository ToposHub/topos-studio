# 复查报告：efr-small-s4（16 个小模块行机械清理）

- 提交：`9b7bfa590fcbfdd246019352ea49e9dddc6ced9c`（[fix-E efr-small-s4]，10 files，+138/−144）
- 执行报告：`docs/plans/2026-09-28-multi-machine-program/fixes/reports/efr-small-s4.md`
- 复查日期：2026-10-02 ｜ 只读复查，未运行 pytest

## 结论：PASS

## 1. 范围核对

提交含报告新增 + **9 个源文件**，与报告"改动文件清单（9 个）"**逐行完全一致**，无跨报告打包、无越界文件。10 个"改动 0"的模块行（windows_hw/vault/assets/diagnostics/layout/db/html_layer/color-adapters 等）确实零改动——"保留类"判定（`__future__` annotations 21+ 文件、探测块导入、`__init__` re-export）与红线一致。

## 2. 未用导入删除抽查（报告称 4 处，全查）

| 文件 | 被删名 | 文件内残留（@HEAD） | 判定 |
|---|---|---|---|
| composite/adapters/main_window_adapter.py | `Optional` | 0（`QMainWindow` 确在 TYPE_CHECKING 块供字符串注解，正确保留） | ✓ |
| color/ai/color_context.py | `Iterable` | 0 | ✓ |
| color/tracking/tracking_evaluator.py | `List` | 0 | ✓ |
| app/integrations/file_explorer/adapter.py | `threading` | 0 | ✓ |

## 3. 死代码删除抽查（报告称 4 处，全查 5 名）

| 名称 | 全仓（src/tests/scripts/plugins）@HEAD | 判定 |
|---|---|---|
| `cv_pixel_buffer_dimensions`（cv_metal_bridge） | 0；保留的 `lock_pixel_buffer_planes` 确由 tests/media 实际使用 | ✓ |
| `lock_pixel_buffer_base_address`（cv_metal_bridge） | 0（tests 用的是 planes/unlock 变体） | ✓ |
| `invalidate_hardware_capability_cache`（hardware_capability） | 0；`clear_hardware_capability_cache`（tests 5+ 处）与 `_hw_cache_path` 不受影响 | ✓ |
| `_interpolate_curve`（scripting/api） | 0；`_interpolate_curve_255` 保留且 5 处使用 | ✓ |
| `_apply_raw_tone_and_detail`（file_io 静态方法） | 0 | ✓ |

孤儿检查：`_apply_raw_tone_and_detail` 删除后 file_io.py 的 `numpy as np` 仍有 32 行在用（save/load_baked_layer 注解与实现），无孤儿导入 ✓。

## 4. 注解零行为证明（12 处）

新增行全集 = 返回注解 def 行（`-> None`/`-> str`/`-> list[float]`）+ 1 处中间变量内联（`top = menu_path.split("/")[0]; return top` → 直接 return）。file_io 无 `__future__ annotations`，注解均为 builtin 类型，def 时求值安全 ✓。

## 5. 红线（含一处重点澄清）

- **except**：diff 中有 1 行 `-    except Exception:` 被删——追查上下文确认该行位于被整体删除的死方法 `_apply_raw_tone_and_detail` 体内，属死代码整体删除，**非 except 语义改动** ✓；无任何新增 except
- 信号 connect：零改动 ✓
- GLSL/色彩矩阵：零触碰；macos_vt 探测块（vt_session/iosurface_gl_bridge 的 `try: from Quartz import ...` 控制 `_QUARTZ_CV_LOADED` 等标志）全部保留，判定正确 ✓
- 公共 API：零改名；assets/icon_manager 17 个零引用 `icon_*` 判"共享图标库公共 API 面"保留并移交 owner——保守正确 ✓

## 6. py_compile（6 文件）

cv_metal_bridge.py / hardware_capability.py / api.py / file_io.py / adapter.py / menu_builder.py → 全部 OK。

## 7. 综合判定

**PASS**。范围、删除真实性（4/4 全查）、零行为证明、红线全部通过。测试中 3 个 VT 失败为文档在案预存（与 s4 无涉的 TimelineExporter 用例）。
