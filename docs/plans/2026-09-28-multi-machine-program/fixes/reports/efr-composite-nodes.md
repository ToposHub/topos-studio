# EFR 批修复报告 —— E-rollup `src/features/composite/nodes/` 机械清理（127 条 process 级改进落线）

日期：2026-10-02 ｜ 分支：dev（**未做任何 git add/commit，改动全部留在工作树**）｜ 白名单：仅 `src/features/composite/nodes/`（98 个 py / 33229 行）
红线遵守：零行为变化；**diff 中零条 PARAM_DEFS / GLSL / shader / uniform 相关改动**（已用 `git diff | grep` 验证为 0）；无裸 `except:`；无函数重排；REMEDIATION_PLAN.md 未触碰。

## 分类统计

| 类别 | 数量 | 说明 |
|------|------|------|
| 1. 未使用 import 删除 | **34 个绑定 / 31 处编辑 / 29 个文件** | AST 扫描（41 候选）→ 全仓 grep 逐一核实 → 31 处实删；7 个 `from __future__ import annotations` 候选**有意保留**（语义性导入，删除有前向引用求值风险） |
| 2. 死代码删除 | **1 处 / 1 个文件** | `depth_common.image_output_bbox`（全仓零引用，证据见下） |
| 3. 类型注解补充 | **74 个 evaluate 系签名 + 94 行 `-> None` + 2 处 `-> str` + `__exit__` 1 处 + `get_parameter` 1 处**；配套新增 typing 导入 27 处（23 × `Any`、4 × `Optional`） | 全部有仓内既有先例对照；拿不准的 ~90 个复杂 helper（scene3d `_render_ortho_views` 30 参、text_node 几何 helpers 等）**未加** |
| 4. 命名收敛 | **0**（评估后不做） | 私有标识符重命名收益低、与"最小 diff/绝不改公共 API"红线冲突风险高；本轮放弃 |
| 5. 小清理 | **8 处冗余中间变量内联**（单一赋值后立即 return、全仓扫描逐一目检） | base_node / color_node / remove_channels / scene3d / set_alpha / text_node / timeline_clip_input ×2 |
| 合计改动 | **79 个文件，+300/−322 行** | 全部位于 `src/features/composite/nodes/` |

## 未使用 import 删除明细（34 绑定）

- typing 收敛：`List` ×10（blur / channel_merge / compare / defocus / denoise / huecorrect / normalize / switch / warp / zblur）、`Any` ×4（noise / ramp / topos_color / unknown_placeholder）、`Tuple` ×3（keyer / ramp / warp）
- 整行删除：`clamp_node` `import numpy as np`、`cornerpin_node` `import math`、`text_node` 函数内 `import glob`、`model3d_node` 函数内 `import time as _time_mod`、`scene3d_node` 函数内 `from ..render3d.scene_graph import TEXTURE_ROLE_DATA`、`video_frame_node` `from .node_state import NodeState`、`file_output_node` 与 `timeline_clip_input_node` 的 `format import ImageFormat, BBox` 整行、`timeline_clip_input_node` `color_space import ColorSpace`、`depth_common` 守卫导入块 2 行（`if __name__ != "__main__":` + `BBox`，守卫内唯一内容即该未用导入）
- 行内名字删除：`crop_node`（ImageFormat，保留 BBox）、`dot_node` / `premult_node` / `unpremult_node`（ParamDef）、`placeholder_node`（Port + ParamDef）

**核实方法**（每条均过三关）：
1. AST Name 收集 + 全文件原始文本 `\b<name>\b` 扫描（防 getattr / 字符串注解漏判）→ 文件内零使用；
2. 全仓 `grep -rEn "from .*<mod> *import"` 交叉比对 → **无任何模块从这些节点模块反向导入被删名字**（`nodes/__init__.py` 星号导入虽会把这些名字带进包命名空间，但全仓无 `from ...composite.nodes import <name>` / `nodes.<name>` 用法，且 `__init__.py` 无 `__all__`）；
3. 删除脚本对每个替换做"旧串全文唯一"断言，任一不符即整体中止（曾据此拦下 dot_node 误删 `Port` 的一处编辑表错误）。

**有意保留**：7 个文件的 `from __future__ import annotations`（_gpu_readback / deformer3d / paint / particle / roto_shape / timeline_clip_input / warp）——语义性导入，删除属行为变化风险。

## 死代码证据（唯一 1 条）

`src/features/composite/nodes/depth_common.py:73-77` `image_output_bbox`：
- 定义外全仓 grep（src/ tests/ plugins/ docs/，*.py/*.md/*.tglsl/*.json 排除 coverage.json）**仅 2 处命中**：定义行本身 + `docs/audit/2026-09-28/batches/batch-066-features-composite-nodes.md:373`（审计文档对既有 helper 的点名描述，非代码引用）；
- `depth_common` 的全部消费者（zblur / fog / depth_matte）只导入 `DEPTH_UNITS, depth_marker_inputs, depth_params`；`tests/composite/test_depth_tools.py` 只导入 `depth_to_normalized`；无星号导入、无 `__all__`。

## 类型注解抽样（仓内先例对照）

```diff
 # grade_node.py（65+ 文件同款；对照先例 vignette_node.py:81-82 已注解版）
-    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx, fb_manager,
-                      resource_pool=None, render_mode="preview") -> Dict[str, TextureResult]:
+    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,
+                      resource_pool: Any = None, render_mode: str = "preview") -> Dict[str, TextureResult]:

 # base_node.py（抽象基类同款收敛）
-    def evaluate(self, inputs: Dict[str, TextureResult], ctx, fb_manager,
+    def evaluate(self, inputs: Dict[str, TextureResult], ctx: Any, fb_manager: Any,

 # switch_node.py
-    def get_parameter(self, key: str, default=None):
+    def get_parameter(self, key: str, default: Any = None) -> Any:

 # video_frame_node.py（对照先例 edit/core/generator_transaction_guard.py:92）
-    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
+    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
```
- `-> None` 覆盖 `__init__` / `cleanup`(10) / `set_parameter`(base_node、warp) / `_update_ports`(switch)；`-> str` 覆盖 `__repr__` ×2。脚本仅对 AST 验证"函数体无带值 return"的签名追加注解，且写盘前重新 `ast.parse`。
- 注解涉及的 `Any`/`Optional` 若文件原未导入则补 typing 导入（23 + 4 处），保证 import 期注解求值安全。
- 未加注解的 ~90 个 helper（scene3d/text_node/geometry 类）：参数/返回类型非显然，按"拿不准就不加"原则不动。

## 小清理明细（8 处，全部为"单一赋值后下一语句立即 return"的纯表达式临时变量）

base_node.compute_output_bbox（`first_bbox = next(...)`）、color_node.to_dict（`data = super().to_dict()`，注释保留）、remove_channels_node / set_alpha_node.compute_output_bbox（`bbox = ...get(...)`）、scene3d_node._sort_meshes_by_material（`result = [...] + [...]`）、text_node._rasterize_mask（`out = np.stack(...)`）、timeline_clip_input_node.to_dict / from_dict。均为行为恒等的 return 内联。

## 命名收敛（0 条的理由）

节点参数名/类型名经 PARAM_DEFS 序列化与 UI 反射，模块内私有标识符重命名需逐个全仓核实且无功能收益；本轮按"机械清理、绝不改行为、最小 diff"原则整类放弃。

## 验证输出（全过）

1. **py_compile**：`git diff --name-only -- src/features/composite/nodes/` 的 79 个文件逐一 `python -m py_compile` → `PY_COMPILE_ALL_OK`
2. **模块冒烟**：`importlib` 逐个导入 nodes 包全部 97 个模块 → `import failures: NONE`
3. **定向既有测试**（未跑大目录）：
   - 任务指定 4 件套：`pytest tests/composite/test_scene3d_node_coverage.py tests/composite/test_video_frame_playback.py tests/composite/test_ai_gen_expand.py tests/composite/test_frame_interpolate.py -q` → **205 passed in 5.38s**
   - 节点全家族 + 本批死代码所在模块：`pytest tests/composite/test_nodes/ tests/composite/test_depth_tools.py -q` → **1006 passed, 6 skipped in 3.18s**（skip 为既有标记，无新增失败）
4. **裸 except 检查**：`grep -rn "except:" src/features/composite/nodes/` → **0 命中**（既有即为 0，无记录项）
5. **红线抽查**：`git diff -- src/features/composite/nodes/ | grep -cE "^[+-].*(PARAM_DEFS|uniform |GLSL|fragment_shader|vertex_shader)"` → **0**

## 改动文件清单（79 个，全部 `src/features/composite/nodes/`）

ai_denoise_node.py, ai_expand_node.py, ai_generate_node.py, ai_inpaint_node.py, ai_remove_node.py, base_node.py, blend_node.py, blur_node.py, camera3d_node.py, channel_merge_node.py, chroma_keyer_node.py, clamp_node.py, color_correct_node.py, color_node.py, compare_node.py, constant_node.py, copy_channel_node.py, cornerpin_node.py, crop_node.py, cs_transform_node.py, deep_merge_node.py, defocus_node.py, deformer3d_node.py, denoise_node.py, depth_common.py, difference_keyer_node.py, dissolve_node.py, dot_node.py, edge_detect_node.py, erode_dilate_node.py, file_output_node.py, frame_hold_node.py, geo3d_node.py, glow_node.py, grade_node.py, group_node.py, huecorrect_node.py, image_input_node.py, invert_node.py, keyer_node.py, lens_distortion_node.py, light3d_node.py, luma_keyer_node.py, lut_node.py, mask_merge_node.py, mask_node.py, matte_cleanup_node.py, matte_node.py, model3d_node.py, noise_node.py, noop_node.py, normalize_node.py, output_node.py, paint_node.py, particle_node.py, placeholder_node.py, precomp_node.py, premult_node.py, ramp_node.py, reformat_node.py, remove_channels_node.py, retime_node.py, roto_shape_node.py, scene3d_node.py, set_alpha_node.py, sharpen_node.py, shuffle_node.py, switch_node.py, text_node.py, time_offset_node.py, timeline_clip_input_node.py, topos_color_node.py, tracker_node.py, transform_node.py, unknown_placeholder_node.py, unpremult_node.py, video_frame_node.py, warp_node.py, zblur_node.py
