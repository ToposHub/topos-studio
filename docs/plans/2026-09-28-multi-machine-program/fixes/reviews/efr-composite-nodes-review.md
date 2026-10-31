# EFR 复查：composite/nodes（E-rollup 模块行）

- 复查对象：`src/features/composite/nodes/` ｜ 提交 `dae658d5629d76b5aa0ee2831183bfd67d0af547`
- 执行报告：`fixes/reports/efr-composite-nodes.md`
- 复查日期：2026-10-01 ｜ 复查方式：只读（git show/grep/py_compile），未运行 pytest
- 复查人：GLM5.3 复查会话

## 七项判定

| # | 检查项 | 判定 | 说明 |
|---|--------|------|------|
| 1 | 越界 | ✅ PASS | 80 个文件 = 79 个 `src/features/composite/nodes/*.py` + 本批报告；REMEDIATION_PLAN.md 未触碰 |
| 2 | 未用导入删除抽查 | ✅ PASS | 抽 10 个被删绑定；特别核验 `nodes/__init__.py` 星号导入面：全仓 `from ...composite.nodes import X` 仅取**模块**（scene3d_node/_gpu_readback/model3d_node 等），无被删名字的包级/子模块级再导入 |
| 3 | 死代码删除抽查 | ✅ PASS | 唯一未配对 def = `image_output_bbox`，py 文件零引用（coverage.json 与 batch-066 审计文档命中为产物/描述，非代码引用）——与报告一致 |
| 4 | 注解零行为证明 | ✅ PASS | evaluate 系签名 `ctx: Any, fb_manager: Any, resource_pool: Any = None, render_mode: str = "preview"`——默认值逐字未变，仅加类型标注；配套 `Any`/`Optional` typing 导入为注解所需（switch_node 等抽验使用在位）；8 处中间变量内联均为 `x = expr; return x` → `return expr` 恒等变换 |
| 5 | 红线 | ✅ PASS | **src/ 范围内 PARAM_DEFS/uniform/GLSL/fragment_shader/vertex_shader 关键字改动 = 0**（diff 全量命中 3 处均在报告文档内）；裸 `except:` 当前 0；无 class 行改动；公共节点类/方法零改名 |
| 6 | py_compile | ✅ PASS | 当前 HEAD 抽 10 个改动文件（base_node/grade_node/scene3d_node/text_node/depth_common/switch_node/video_frame_node/timeline_clip_input_node/model3d_node/blur_node）全部通过 |
| 7 | 综合结论 | **PASS** | 79 文件大体量批，抽样与全量关键面核验均成立 |

## 抽查明细

### 未用导入（10 绑定）

| 被删绑定 | 所在文件 | 当前验证 | 判定 |
|----------|---------|---------|------|
| `import numpy as np` | clamp_node.py | 文件内 `np.`/`numpy` 残留 0 | 安全 |
| `import math` | cornerpin_node.py | `math.` 残留 0 | 安全 |
| 函数内 `import glob` | text_node.py | `glob` 残留 0 | 安全 |
| `NodeState` | video_frame_node.py | 残留 0；定义处 node_state.py 未动 | 安全 |
| `ImageFormat, BBox` 整行 | file_output_node.py | 两名残留 0 | 安全 |
| `ImageFormat, BBox` + `ColorSpace` | timeline_clip_input_node.py | 残留 0 | 安全 |
| `ParamDef`（行内名删除） | dot_node / premult_node / unpremult_node.py | `from ...nodes.<mod> import ParamDef` 零命中；包级 `ParamDef` 仍由 `__init__.py` 显式从 base_node 导出（shared/effects 两处消费不受影响） | 安全 |
| `Port`+`ParamDef` | placeholder_node.py | 同上零命中 | 安全 |
| `TEXTURE_ROLE_DATA`（函数内） | scene3d_node.py | 残留 0；定义处 render3d/scene_graph.py 未动 | 安全 |
| `List` ×10 / `Any` ×4 / `Tuple` ×3 | 各节点文件 | 文件内残留 0 | 安全 |

### 死代码（1 处）

| 被删定义 | 全仓 grep | 判定 |
|----------|-----------|------|
| `depth_common.image_output_bbox` | `--include="*.py"` 范围（src/tests/plugins/scripts）零命中 | 安全 |

depth_common 的其余导出（DEPTH_UNITS/depth_marker_inputs/depth_params/depth_to_normalized）均保留且各有消费者——未过度删除。

### 小清理（抽 3/8 处）

- base_node.compute_output_bbox：`first_bbox = next(iter(input_bboxes.values()), None); return first_bbox` → `return next(...)`——diff 原文核验恒等。
- color_node.to_dict：`data = super().to_dict(); return data` → `return super().to_dict()`——恒等。
- text_node._rasterize_mask：`out = np.stack(...); return out` → `return np.stack(...)`——恒等。

## 发现

1. **（Note）** `nodes/__init__.py` 星号导入会把节点模块的顶层名带入包命名空间，报告对此做了专项论证（无 `__all__`、全仓无包级名字消费）——本次以全仓 grep 复核属实（包级导入全是模块引用）。该风险面论证质量高。
2. **（Note）** 报告统计与 git stat 一致：源码 +300/−322（381−81 报告行 / 322），79 文件清单逐一对上。
3. **（Note）** 注解新增 typing 导入 27 处（23 Any + 4 Optional）属注解配套的必要新增，非"未用导入清理"范畴的倒退（抽验 switch_node：`Any` 在 :55/:66/:147 使用在位）。

## 结论

**PASS**。GLSL/PARAM_DEFS 红线在 src/ 范围 diff 级零命中；大体量机械清理证据充分。建议勾选该 E-rollup 行。
