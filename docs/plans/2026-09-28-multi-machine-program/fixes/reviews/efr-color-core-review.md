# EFR 复查：color/core（E-rollup 模块行）

- 复查对象：`src/features/color/core/` ｜ 提交 `697fe7892cf60b0b1753869770d0acbf2d176690`
- 执行报告：`fixes/reports/efr-color-core.md`
- 复查日期：2026-10-01 ｜ 复查方式：只读（git show/grep/py_compile），未运行 pytest
- 复查人：GLM5.3 复查会话

## 七项判定

| # | 检查项 | 判定 | 说明 |
|---|--------|------|------|
| 1 | 越界 | ✅ PASS | 19 个文件 = 18 个 `src/features/color/core/*.py` + 本批报告；`color_manager.py`（色彩矩阵红线）不在改动清单，未触碰；REMEDIATION_PLAN.md 未触碰 |
| 2 | 未用导入删除抽查 | ✅ PASS | 抽 8 名字/模式（含 GLSL 常量 `QUAD_VERTEX_SHADER`、monkeypatch 高危面 `re`），当前全仓零残留引用 |
| 3 | 死代码删除抽查 | ✅ PASS | 5 个被删方法全仓零引用；3 个"保留"候选（`get_color_manager`/`_bind_aux_textures`/color_manager 再导出块）核实确有存在必要 |
| 4 | 注解零行为证明 | ✅ PASS | 签名级改动抽验（history/delivery_matrix/shader_compiler）全部仅动注解与默认值类型标注，默认值本身未变；`request_id: Any` 自纠错已在报告披露 |
| 5 | 红线 | ✅ PASS | 全部源码改动行中**零数值字面量变更**（含数字的 +/- 行均来自报告文档）；裸 `except:` 当前 15 处 = 报告基线，零新增；公共 API 零改名（76 个签名改动全部有配对，唯一未配对 def 即 5 个已披露死方法） |
| 6 | py_compile | ✅ PASS | 当前 HEAD 抽 10 个改动文件（async_renderer/image_engine/shader_compiler/the_decompiler/delivery_matrix/history/layer/video_temporal_denoise/video_denoise_gl/lut_exporter）全部通过 |
| 7 | 综合结论 | **PASS** | 机械清理成立；色彩矩阵/HDR 数值零触碰得到 diff 级证明 |

## 抽查明细

### 未用导入（8 名字/模式）

| 被删名字 | 所在文件 | 当前全仓 grep | 判定 |
|----------|---------|--------------|------|
| `QObject` | async_renderer.py | `async_renderer.QObject` 零命中 | 安全 |
| `math` / `PIL.ImageFilter` / `QColor` / `QSurfaceFormat` / `QPen` / `QTransform` / `QMutex` / `QWaitCondition` | image_engine.py | 文件内残留使用 0（`import math` 删除后无 `math.` 调用） | 安全 |
| `QUAD_VERTEX_SHADER`（直接导入） | video_denoise_gl.py | 常量仍在定义处 `video_denoise_shaders.py:18`；video_temporal_denoise:145 / video_spatial_denoise:48 经 `vs.` 模块别名正常使用——仅 video_denoise_gl 内的直接导入被删 | 安全 |
| `re` | denoise_engine.py / the_decompiler.py | `denoise_engine.re` / `the_decompiler.re` monkeypatch 字符串零命中；文件内 `re.` 残留 0 | 安全 |
| `time` / `field` / `Tuple` | cache_manager.py | 文件内残留 0 | 安全 |
| `Sequence` | delivery_matrix.py | 限定名零命中 | 安全 |

### 死代码（5 处）

| 被删定义 | 全仓 grep | 判定 |
|----------|-----------|------|
| `_enable_scope_additive_blending` / `_restore_scope_blend_state` / `_scope_points_vao` | async_renderer 内零命中；`_release_scope_points_vao_cache` 保留且活跃 | 安全 |
| `_generate_full_res_mask`（43 行） | 零命中；其依赖 `_has_visible_mask_items`/`_render_mask_items_recursive` 保留 | 安全 |
| `_render_tile` | 全仓零命中（报告述其余同名属其他类方法，当前 `-w` grep 亦零命中，更强） | 安全 |

### "保留"候选核实

- `color_manager.py` 再导出块（ColorTransfer/ColorMatrix/ColorRange）：async_renderer.py:4943、tests 多处在用——保留正确。
- `get_color_manager`：定义保留于 color_manager.py:60，docstring 示例引用——保留合理。
- `_bind_aux_textures`：async_renderer 中定义仍在（docstring 注明留给旧调用点/测试）——保留合理。

### 注解抽查

- `history.push_state/undo`：`layers: List = None` → `Optional[List] = None`——仅注解纠偏，默认值 None 未动；文件有 `from __future__ import annotations`（且两式求值均无副作用），零运行时差异。
- `delivery_matrix`：`tr=None` → `tr: Callable[[str, str], str] \| None = None`——补 `Callable` typing 导入，默认值未变。
- `shader_compiler.build_composition_shader`：`has_curves=False` → `has_curves: bool = False` 等 9 参 + `request_id/layer_id: Any = None` + 返回 `"moderngl.Program | None"`（字符串注解延迟求值）——全部声明级。

## 发现

1. **（Note）** `video_temporal_denoise.py` 导入行实为修改而非纯删除：`from .video_denoise_gl import QuadRenderer, RenderSurfaceCache` → 增加 `RenderSurface`（新注解 `luma_surface: RenderSurface` 在 :302 使用）。合理但报告未点名此新增导入——无害。
2. **（Note）** 报告统计与 git stat 交叉验证一致：源码 +114/−177（260−146 报告行 / 177），18 文件清单逐一对上。

## 结论

**PASS**。色彩红线（矩阵/HDR/数值）在 diff 层面零触碰；死代码与导入删除证据成立。建议勾选该 E-rollup 行。
