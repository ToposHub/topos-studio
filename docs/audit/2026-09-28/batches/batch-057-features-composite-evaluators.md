# batch-057 审查报告 — 主题 src/features/composite/evaluators//（AUD-5700–AUD-5799）

审查人按 docs/audit/2026-09-28/assignments/INSTRUCTIONS.md 九维度执行。

## src/features/composite/evaluators/__init__.py (行数: 67)
### 发现
- [AUD-57001] [Suggestion] docstring 引用不存在的路径 src/composite/graph_evaluator_mixin.py
  - 位置: src/features/composite/evaluators/__init__.py:24-25
  - 证据:
    ```python
    Usage:
        The main GraphEvaluatorMixin in src/composite/graph_evaluator_mixin.py
        inherits from all these mixins to provide the complete evaluation API.
    ```
  - 影响: src/composite 目录在仓库中不存在（已验证），实际文件位于 src/features/composite/evaluators/graph_evaluator_mixin.py；按文档导航的维护者会走错路径。
  - 建议: 更新 docstring 为 src/features/composite/evaluators/graph_evaluator_mixin.py。
  - 置信度: 高（已用 ls 验证 src/composite 不存在）。
### 通过项
- 导入完整性：全部 13 个子模块（base/blur/color/transform/merge/keyer/channel/filter/cache/tile/histogram/keyframe/expression/ai_evaluator）均存在于同目录（已验证）；`BaseEvaluatorMixin`、`TileContext` 在 base_evaluator.py:305/190 定义。
- re-export 一致性：`__all__` 与 import 列表一致，含私有名 `_AI_NODE_TYPES`（见 ai_evaluator.py 一节的重复定义发现）。
- 线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交 / 异常取消 / 输入验证 / 性能：本文件仅为聚合导入，无逻辑，不适用。

## src/features/composite/evaluators/ai_evaluator.py (行数: 41)
### 发现
- [AUD-57002] [Suggestion] _AI_NODE_TYPES 与 base_evaluator.py 重复定义（双事实源），且 get_ai_node_types() 全库无调用方
  - 位置: src/features/composite/evaluators/ai_evaluator.py:22-23, 38-41
  - 证据:
    ```python
    # Node types that require async AI computation
    _AI_NODE_TYPES: Set[str] = {"ai_generate", "ai_inpaint", "ai_expand", "ai_remove", "denoise"}
    ...
        @staticmethod
        def get_ai_node_types() -> Set[str]:
            """Get the set of node types that require async AI computation."""
            return _AI_NODE_TYPES
    ```
    而 base_evaluator.py:246 另有一份相同集合：`_AI_NODE_TYPES = {"ai_generate", "ai_inpaint", "ai_expand", "ai_remove", "denoise"}`（其在 1653 行用于节点分发判断）。
  - 影响: 同一 AI 节点类型集合存在两处独立定义；新增/删除 AI 节点类型时若只改其一，get_ai_node_types() 的返回与实际分发判断（base_evaluator 1653 行）不一致。另经全库 grep，get_ai_node_types 无任何调用方，属死代码。
  - 建议: 删除本文件的重复常量与死方法，统一从 base_evaluator 导入；或反向将唯一定义放本文件并让 base_evaluator 引用。
  - 置信度: 高（两处定义与零调用均已 grep 验证）。
- [AUD-57003] [Suggestion] 导入 Dict 未使用、TYPE_CHECKING 块为空 pass（死代码）
  - 位置: src/features/composite/evaluators/ai_evaluator.py:16-19
  - 证据:
    ```python
    from typing import Dict, Set, TYPE_CHECKING

    if TYPE_CHECKING:
        pass
    ```
  - 影响: `Dict` 未被使用；空 TYPE_CHECKING 块无任何作用，徒增阅读噪音。
  - 建议: 改为 `from typing import Set`，删除空 TYPE_CHECKING 块。
  - 置信度: 高
- [AUD-57004] [Suggestion] docstring 引用不存在的路径 src/composite/ai_compute_manager.py
  - 位置: src/features/composite/evaluators/ai_evaluator.py:29-31
  - 证据:
    ```python
    Note: The actual AI computation is handled by AIComputeManager
    in src/composite/ai_compute_manager.py. This mixin provides
    integration hooks for the graph evaluator to handle AI nodes
    ```
  - 影响: 实际文件位于 src/features/composite/render/ai_compute_manager.py（已验证），错误路径误导维护者。
  - 建议: 更正 docstring 路径。
  - 置信度: 高
### 通过项
- 线程安全：纯静态方法返回模块级不可变集合语义（Set 无并发写点），无共享可变状态。
- GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消 / 输入验证 / 性能模式：本文件无运行逻辑（仅常量与一个静态 getter），不适用。
- 类型注解：现有签名均有注解（get_ai_node_types -> Set[str]）。
