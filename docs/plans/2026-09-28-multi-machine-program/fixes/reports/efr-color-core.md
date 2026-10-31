# EFR 报告：`src/features/color/core/` 机械清理

- 日期：2026-10-01
- 分支：dev（仅工作树改动，未 git add/commit）
- 范围：`src/features/color/core/` 全模块（36 个 .py）
- 原则：纯机械清理、零行为变化、最小 diff；HDR/色彩矩阵数值逻辑零触碰
- 改动统计：18 个文件，+114 / -177 行（净 -63）

---

## 一、分类统计

| 类别 | 数量 | 说明 |
|------|------|------|
| 1a. 删除未使用 import | 20 个名字（11 个文件） | AST 全仓核实 + monkeypatch 字符串核实 |
| 1b. 有意保留的 import | 15 处 | `from __future__ import annotations`（14 文件）+ color_manager 共享类型再导出块 |
| 2. 删除死代码 | 5 处（58 行） | 全仓 word-boundary grep 零引用；2 处候选有引用仅记录未删 |
| 3. 补类型注解 | 76 个签名（13 个文件） | 仅注解；返回类型已明确的补 `-> T`，拿不准的一律不加/用 Any |
| 4. 命名收敛 | 0 | 无模块内私有标识符需要收敛；公共 API 名称零改动 |
| 5. 小清理 | 0 处应用 | 未发现零风险的冗余中间变量/重复字面量提取目标（详见"跳过项"） |

## 二、抽样证据

### 1. 未使用 import（AST + 全仓核实）

核实方法：`ast` 解析收集 Name/字符串常量/`__all__` 用法 → 全仓 grep 确认无
`from ...<module> import <name>` 再导出 → 全仓 grep 确认无
`patch("...<module>.<name>")`/`setattr` 字符串目标。删除清单：

- `async_renderer.py`：`QObject`（仅注释提及；无基类使用）
- `auto_color.py`：`dataclasses.field`、`typing.Optional`
- `cache_manager.py`：`import time`、`dataclasses.field`、`typing.Tuple`
- `delivery_matrix.py`：`typing.Sequence`
- `denoise_engine.py`：`import re`（含 `monkeypatch.setattr("...denoise_engine.re")` 反查零命中）
- `history.py`：`typing.Tuple`
- `image_engine.py`：`math`、`PIL.ImageFilter`、`QColor`、`QSurfaceFormat`、`QPen`、`QTransform`、`QMutex`、`QWaitCondition`（均仅在 import 行出现）
- `scope_types.py`：`dataclasses.field`
- `the_decompiler.py`：`import re`（tests 反查零命中）
- `video_denoise_gl.py`：`QUAD_VERTEX_SHADER`（整行删除）
- `video_denoise_pipeline.py`：`typing.Tuple`

有意保留：
- 全部 `from __future__ import annotations`（有运行时语义，不属于死 import）
- `color_manager.py` L27 的 `src.shared.color.types` 导入块：其中
  `ColorTransfer/ColorMatrix/ColorRange` 是活跃再导出（`async_renderer.py:4963`、
  `tests/media/test_color_convert.py:29`、`tests/media/test_color_decode_chroma_resize.py:3`
  直接 `from color_manager import`），且该文件是色彩矩阵核心（红线），整块不动。

### 2. 死代码（删除 5 处，全仓零引用）

删除前证据：AST 全文件 Name/Attribute 使用计数 = 0，且 `grep -rn -w <name> src tests`
在定义文件外零命中（含 getattr 字符串、partial 引用、子类覆盖）：

| 位置 | 标识符 | 证据 |
|------|--------|------|
| `async_renderer.py`（原 L1826） | `_enable_scope_additive_blending` | 全仓唯一出现 = def 行 |
| `async_renderer.py`（原 L1838） | `_restore_scope_blend_state` | 同上 |
| `async_renderer.py`（原 L1842） | `_scope_points_vao` | 仅 def；`_release_scope_points_vao_cache` 在 L6509 活跃调用，保留 |
| `image_engine.py`（原 L3726，43 行） | `_generate_full_res_mask` | 全仓零引用；其依赖 `_has_visible_mask_items`/`_render_mask_items_recursive` 在 L2885/2895/3836 仍活跃，保留 |
| `image_engine.py`（原 L4471） | `_render_tile` | "Legacy no-op" stub；word-boundary grep 其余命中均属 async_renderer / composite_export_renderer 自身同名方法 |

记录未删（有引用，不满足"零引用才删"）：
- `color_manager.py:60` `get_color_manager`：仅本模块 docstring 示例引用（L69-70）
- `async_renderer.py` `_bind_aux_textures`：docstring 注明"保留给旧调用点/测试"

### 3. 类型注解（76 个签名，全部仅声明级）

按文件：the_decompiler 9（rgb2hsv/hsv2rgb/params_to_rgb/apply_grading/apply_soft_clip/
apply_mixer/load_cube/extract_warper_grid/`__init__`）、shader_compiler 10、layer 15、
video_temporal_denoise 8、video_spatial_denoise 6、delivery_matrix 6、lut_exporter 6、
history 4、composition_uniforms 3、video_denoise_gl 3、grade_keyframes 2、
video_denoise_pipeline 2、image_engine 1（`Tile.__init__`）。

拿不准就不加（实例）：
- `shader_compiler.build_composition_shader` 的 `request_id/layer_id`：调用点为
  `params.get('render_request_id')` / `layer.get('id')`，类型不定 → 标 `Any`
  （初版误标 `str | None`，已改）
- `matte_cache_hash` 的 `qp/ms/size/rect`：dict 形状不定 → 未加
- lut_exporter 的嵌套闭包 `_overlay/_soft_light/_overlay_d/lookup_curve`：
  数值混合逻辑内部嵌套函数 → 未加
- `color_manager.py` 8 个缺注解签名：红线文件，零触碰

注解-only 修正：`history.push_state/undo` 的 `layers: List = None` →
`Optional[List] = None`（默认值 None 本就存在，仅注解纠偏）；
`video_temporal_denoise._downsample_luma` 的 `width: int = None` → `Optional[int] = None`。

### 4. 命名收敛

未执行任何重命名。async_renderer/image_engine 等公共 API 被 UI/导出广泛调用，不触碰。

## 三、跳过项（含原因）

- 小清理（冗余中间变量/重复字面量）：扫描未发现"不触碰数值逻辑即可提取"的零风险目标；
  `video_spatial_denoise._process_separable` 已有 `common` dict 复用；lut_exporter 的
  `_overlay` 双定义分别在 Overlay/Hard Light 分支内、属混合模式数值逻辑（红线），不动。
- `color_manager.py`：再导出 + 色彩矩阵红线，整文件零改动（见上）。
- 既有裸 `except:`：15 处（async_renderer 14 + image_engine 1），按任务要求只记录不修，
  语义未变，数量清理前后一致（0 新增）。
- `from __future__ import annotations` "未使用"：有运行时语义，保留。

## 四、验证输出（全部通过）

1. `py_compile`：`.venv/bin/python -m py_compile src/features/color/core/*.py` → OK
2. 导入冒烟：18 个被改模块 `importlib.import_module` 全部 IMPORT-OK
   （含 Qt 模块 async_renderer/image_engine/cache_manager/denoise_engine/lut_exporter）
3. 定向测试（QT_QPA_PLATFORM=offscreen，未整目录跑、未跑 tests/edit）：

| 测试文件 | 结果 |
|----------|------|
| tests/color/test_delivery_matrix.py + test_auto_color.py + test_cache_manager.py + test_composition_uniform_manifest.py | 98 passed (6.17s) |
| tests/color/test_ef07_aud.py（denoise_engine + the_decompiler） | 47 passed (3.76s) |
| tests/video_denoise/test_video_denoise_gpu.py | 12 passed (2.80s) |
| tests/color/test_color_state_roundtrip.py + test_shader_pipeline_lifecycle.py | 9 passed |
| tests/color/test_color_tools_lut_exporter.py | 13 passed (0.64s) |

合计 **179 passed / 0 failed**。

4. `grep -rn "except:" src/features/color/core/` → 15 命中 = 改动前基线（0 新增）。
5. `git diff` 仅涉及 `src/features/color/core/` 下 18 个文件；未 add/commit。

## 五、改动文件清单

1. src/features/color/core/async_renderer.py（import -1 名字；死代码 -3 方法）
2. src/features/color/core/auto_color.py（import -2）
3. src/features/color/core/cache_manager.py（import -3）
4. src/features/color/core/composition_uniforms.py（注解 3）
5. src/features/color/core/delivery_matrix.py（import -1；注解 6）
6. src/features/color/core/denoise_engine.py（import -1）
7. src/features/color/core/grade_keyframes.py（注解 2）
8. src/features/color/core/history.py（import -1；注解 4）
9. src/features/color/core/image_engine.py（import -8；死代码 -2；注解 1）
10. src/features/color/core/layer.py（注解 15）
11. src/features/color/core/lut_exporter.py（注解 6）
12. src/features/color/core/scope_types.py（import -1）
13. src/features/color/core/shader_compiler.py（注解 10）
14. src/features/color/core/the_decompiler.py（import -1；注解 9）
15. src/features/color/core/video_denoise_gl.py（import -1；注解 3）
16. src/features/color/core/video_denoise_pipeline.py（import -1；注解 2）
17. src/features/color/core/video_spatial_denoise.py（注解 6）
18. src/features/color/core/video_temporal_denoise.py（注解 8）

未改动（有意）：`__init__.py`（空）、`color_manager.py`（红线+再导出）、
`color_output.py`、`color_session.py`、`color_stack_render_service.py`、
`msl_composition.py`、`opengl_render_backend.py`、`preset_meta.py`、
`project_dependencies.py`、`shader_nodes.py`、`shader_nodes_msl.py`、
`video_denoise_shaders.py`、`workspace_presets.py`（无安全清理目标）。
