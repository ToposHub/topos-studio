# 修复报告 — w5b-g3（批 A，Critical）：AUD-9108 基元缠绕与法线相反

日期：2026-09-29
范围：`src/features/composite/render3d/primitives.py`（白名单内）；新增
`tests/composite/test_fix_primitives_aud5b.py`
证据：`docs/audit/2026-09-28/batches/batch-091-features-composite-render3d.md`（AUD-9108）

## 总结

| 编号 | 结论 |
|------|------|
| AUD-9108 | **FIXED**（且范围扩大：实测发现审查"通过项"中 cube 的 −X/−Z 面与 cylinder 侧面同样反向，属同一根因，一并修复） |

新增回归测试 `tests/composite/test_fix_primitives_aud5b.py`：11 用例，全部通过。
邻接既有测试 311 项全部通过，无任何测试锁定旧缠绕行为（既有断言只查顶点数/索引数/半径/AABB/切线有限性，均不受索引重排影响）。
未 commit/push；未做任何 git 分支操作。工作区中 hdri.py、material_model.py、
scene_payload.py 等其他未提交改动来自并行任务，与本批无关。

---

## 1. 根因分析

### 1.1 文件法线约定（正确参照物）

以审查认定正确、且经独立核算确认正确的部分为参照：
- **cone 侧面**（`idx.extend([apex, b, a])`）：全部三角形 dot = +1.068 > 0；
- **cube 的 +X/+Y/−Y/+Z 四个面**：dot = +12 > 0。

约定即 .clinerules 硬约束：右手系，**CCW 为正面**，每个三角形满足
`dot(cross(v1−v0, v2−v0), 声明法线) > 0`。GL 默认 `front_face=CCW`
（全仓库无覆盖）+ `cull_face='back'` 下，违反该约定的三角形从法线一侧看是背面、整面被剔除。

### 1.2 基线测量（修复前，numpy 逐三角形点积，负值=反向）

| 族 | 三角形数 | dot 区间 | 反向数 | 审查结论 vs 实测 |
|---|---|---|---|---|
| cube | 12 | [−12, +12] | 4（−X、−Z 两个面） | 审查称 cube 正确 → **实测这 2 面反向** |
| plane | 2 | [−12, −12] | 2 | 一致（Critical） |
| sphere | 1024 | [−0.114, 0] | 960（64 个极点退化三角形 dot=0） | 一致（Critical） |
| cylinder 侧面 | 64 | [−1.171, −0.585] | 64 | 审查通过项称正确 → **实测全反向** |
| cylinder 顶盖 | 32 | [−0.585, −0.585] | 32 | 一致 |
| cylinder 底盖 | 32 | [−0.585, −0.585] | 32 | 一致 |
| cone 侧面 | 32 | [+1.068, +1.068] | 0 | 正确（参照物） |
| cone 底盖 | 32 | [−0.585, −0.585] | 32 | 一致 |
| torus | 1024 | [−0.087, −0.047] | 1024 | 一致 |

即：除 cone 侧面与 cube 四个面外，全文件缠绕都与法线相反。AUD-9108 列举的位置属实，但其
"通过项"（cube/cylinder 侧面/cone 侧面缠绕正确）中 cube −X/−Z 面与 cylinder 侧面两处判断有误。
同一根因（缠绕与法线不一致导致 culling 剔除整面），在白名单文件内一并统一。

## 2. 修复明细（`src/features/composite/render3d/primitives.py`）

统一为"从法线方向看 CCW"，逐族改动（顶点位置/UV 数值不变，仅改索引序/cube 两个面的四边形顶点序）：

| 族 | 位置（修复后行号） | 改动 |
|---|---|---|
| cube | ~262, ~267 | −X 面 `[0,3,7,4]`→`[4,7,3,0]`；−Z 面 `[0,1,2,3]`→`[3,2,1,0]`（其余四面不动） |
| plane | ~305 | `idx [0,1,2, 0,2,3]`→`[0,2,1, 0,3,2]`（审查建议方案） |
| sphere | ~380 | `[a, b, a+1, a+1, b, b+1]`→`[a, a+1, b, a+1, b+1, b]`（审查建议方案） |
| cylinder 侧面 | ~444 | `[a, c, b, b, c, d]`→`[a, b, c, b, d, c]`（超出审查清单，实测必需） |
| cylinder 顶盖 | ~458 | `[c, s, s+1]`→`[c, s+1, s]` |
| cylinder 底盖 | ~472 | `[c, s+1, s]`→`[c, s, s+1]` |
| cone 底盖 | ~540 | `[c, s+1, s]`→`[c, s, s+1]`（侧面不动） |
| torus | ~589 | `[a, b, d, d, b, c]`→`[a, d, b, d, c, b]` |

附带：修正各处误导性注释（原 sphere/cylinder 侧面注释自称 "CCW/法线朝外" 但实为反向；底盖注释
"缠绕反向使其从下方看 CCW" 描述与代码不符），cube faces 表头补约定说明。grid/null 为 LINES
语义（索引为线段对），无三角形缠绕语义，不适用。

影响面：backface culling 开启（NAVIGATION/FAST_SOLID 质量档，scene3d_node.py:3548、
4256-4258）时这些基元不再被整面剔除；选中描边 inverted-hull pass（cull front）对这些网格的
行为同步恢复正常；two_sided 默认路径法线本就显式提供，视觉不变。

## 3. 验证

### 3.1 编译
```
$ .venv/bin/python -m py_compile src/features/composite/render3d/primitives.py
PY_COMPILE OK（退出码 0）
```

### 3.2 新增回归测试（tests/composite/test_fix_primitives_aud5b.py）

对每族每个非退化三角形断言 `dot(cross(v1−v0, v2−v0), 参考法线) ≥ −1e-9`；
参考法线双轨：生成器顶点法线 + 解析法线（sphere 径向、cylinder 盖 ±Y、torus 管径向），
防止顶点法线自身写反掩盖缠绕问题。零面积退化三角形（球极点，几何法线模 ≤1e-12）显式排除并计数。

```
$ .venv/bin/python -m pytest tests/composite/test_fix_primitives_aud5b.py -q
11 passed in 0.16s
```

修复后全族复测（非默认细分参数再验证一次）：

| 族 | 三角形数 | dot 区间 | 退化 | PASS |
|---|---|---|---|---|
| cube(1.0) | 12 | [+12, +12] | 0 | True |
| plane | 2 | [+12, +12] | 0 | True |
| sphere(8×4) | 64 | [0, +1.5] | 16（极点） | True |
| cyl side(6) | 12 | [+5.196, +5.196] | 0 | True |
| cyl top(6) | 6 | [+2.598, +2.598] | 0 | True |
| cyl bot(6) | 6 | [+2.598, +2.598] | 0 | True |
| cone side(6) | 6 | [+4.739, +4.739] | 0 | True |
| cone cap(6) | 6 | [+2.598, +2.598] | 0 | True |
| torus(8×4) | 64 | [+0.446, +0.827] | 0 | True |

### 3.3 邻接既有测试（grep tests/ 中引用 primitives/render3d 的套件）

```
$ .venv/bin/python -m pytest tests/composite/test_3d_p1_enhancements.py \
    tests/composite/test_fix_render_aud2.py tests/composite/test_3d_tangent_normal.py \
    tests/composite/test_3d_deformers.py tests/composite/test_deformer3d_node.py \
    tests/composite/test_skinning_contract.py tests/composite/test_lod_builder.py \
    tests/composite/test_morph_targets.py tests/composite/test_mesh_display_modes.py \
    tests/composite/test_3d_phase1_correctness.py -q
241 passed in 3.21s

$ .venv/bin/python -m pytest tests/composite/test_scene_graph.py \
    tests/composite/test_scene_hierarchy.py tests/composite/test_frustum.py \
    tests/composite/test_model_import_report.py tests/composite/test_scene3d_motion_aov.py -q
70 passed in 1.16s
```

无失败，无锁定旧行为的断言需要按协议处理。既有断言核对：
test_3d_p1_enhancements.py 只断言顶点/索引**数量**（56、77 行）与半径位置（63 行），
索引重排不改变数量；test_3d_tangent_normal.py / test_fix_render_aud2.py 用 cube 只查
切线形状/有限性与 AABB（顶点位置未动）；均保持通过。

## 4. 遗留观察（不在本批范围）

- sphere 极点存在零面积退化三角形（极点环顶点重合）：既有行为，与缠绕无关，剔除/光照无影响，未动。
- AUD-9109（content_hash 不含 normals/uvs）、AUD-9110（compute_tangents denom）等其余发现属其他批次。
