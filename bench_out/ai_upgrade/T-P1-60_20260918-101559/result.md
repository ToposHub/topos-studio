# T-P1-60 执行报告：Composite 节点归类、fixture 与图结构 AI 操作（批次 A）

- 执行日期：2026-09-18；状态：**IN_PROGRESS**（批次 B：图结构 AI 操作扩展）

## 已完成

1. **节点契约五分类（卡步骤 1）**：`node_capabilities.get_node_contract_class()`
   ——93 节点 → image_filter 65 / 3d_file_output 10 / generator 8 /
   ai_model 7 / source 3。判定序：AI → 3D/输出 → 外部媒体 source →
   程序化 generator → image_filter。契约测试 20 项
   （tests/composite/test_node_contract_classification.py）。
2. **六态可解释状态码（卡步骤 3）**：
   `bench_composite_nodes_gpu_coverage.classify_node_result()`——
   passed_nonflat / passed_flat_by_contract / blocked_missing_resource /
   blocked_missing_model / unsupported_backend / failed。全量矩阵行新增
   `contract_class`/`backend`(headless_gl_fragment，卡步骤 4)/`status`
   字段。
3. **93 节点矩阵快照（本环境 offscreen 无头 GL 4.1 fragment fallback）**：
   - **77 passed_nonflat**
   - **1 blocked_missing_model**：ai_generate（占位节点缺模型包——
     卡要求的 MODEL_UNAVAILABLE 语义在矩阵层落地；evaluator 级返回值
     属批次 B）
   - **13 blocked_missing_resource**：8× 3D（缺 scene/mesh fixture）、
     3× source（harness 单节点图未接媒体 fixture）、motion_graphic
     （DEFERRED 延迟注册未在 standalone 加载）、file_output
   - **2 failed**：chroma_keyer / cryptomatte_select（平黑——缺绿幕/
     cryptomatte 源 fixture 接线；卡步骤 2 定向 fixture 的接线批次 B 补）
   - 全部 93 行六态可解释（退出码语义：仅不可解释状态才非零）。
4. **chroma_keyer/constant 定向参数 fixture（卡步骤 2 部分）**：
   PARAM_OVERRIDES 补蓝幕显式参数与非默认 constant 颜色。

## 批次 B（待做）

- 图结构 AI 操作：edit_service NODE_OPS 补 duplicate/group/ungroup/
  set_output/import/export + composite_plan 协议 + manifest 宣告。
- evaluator 级 ai_generate MODEL_UNAVAILABLE 结构化返回。
- 绿幕/cryptomatte 源 fixture 接线，消灭剩余 2 个 failed。
- 节点级 performance attribution 字段扩展。

## 桌面验证边界

无头 GL 4.1 fragment fallback 环境的矩阵结果不得替代桌面 Metal/GL4.3
compute runner（X-TEST-10 规则）——行内 `backend` 字段显式标注。
