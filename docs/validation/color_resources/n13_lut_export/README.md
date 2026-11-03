# N13 留档：图 LUT 导出（2026-11-04）

## 实现

- `src/features/color/core/graph_lut_export.py`（图权威侧导出入口）：
  - `graph_pointwise_export_errors`：**可表达性预检 fail-closed**——仅
    「source → Corrector* → output 串行链 + pointwise 基础域 + LUT」可导；
    分支/混叠/Compound/插件（Glow/NR/AI）/窗口/key/邻域效果全部**显式
    拒绝并解释**（复用 CPU oracle 域错误表，另放行 LUT）——绝不产出
    「成功但少效果」的 cube。
  - `bake_graph_lut`：N³ 网格逐点 CPU 求值串行链（R05 v1 顺序：grading
    → curves → … → LUT[blend_node_lut_strength 强度规格]），输出标准
    .cube（DOMAIN 声明，topos-res LUT 经 load_lut 解析）。
- 与旧 `lut_exporter`（legacy Layer 域）并存：图模式导出调用点接本入口。

## 测试（tests/color/test_graph_lut_export.py，4 项）

干净链导出 + .cube 重载（size/domain 往返）；最小链烘焙与点态 oracle
逐点对照（6 位小数 roundtrip）；**插件实例/分支/Compound 显式拒绝**
（ValueError 携带全部错误码）；串行 pointwise+LUT 顺序（R/B 交换表在
grading 之后应用，非交换对照）。
回归：lut loader/registry 43 passed。
