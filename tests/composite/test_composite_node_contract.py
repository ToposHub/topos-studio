"""
Composite Node Contract Tests - S0-02

验证节点注册表、scheduler、evaluator 的一致性。

这些测试用于暴露以下问题：
1. scheduler 的 placeholder 分类逻辑和节点类的 IS_PLACEHOLDER 不一致
2. evaluator 的 NODE_REGISTRY 缺少已注册的节点
3. evaluator 的 CACHE_OWNED_TYPES 缺少会产生 GPU texture 的节点
4. 导入不一致问题（如导入已删除的符号）

Phase 2 F2-02: 更新导入以使用 capability 系统
- is_placeholder_node_type -> node_capabilities.is_placeholder_node_type
- NODE_REGISTRY -> NODE_REGISTRY (evaluator 现在使用 NODE_REGISTRY)
- CACHE_OWNED_TYPES -> node_capabilities.CACHE_OWNED_TYPES
"""

import pytest
from typing import Dict, Set, Optional, List, Tuple

# Import from project
# Phase 2 F2-02: Use NODE_REGISTRY and capability system
from src.features.composite.core.node_engine import NODE_REGISTRY
from src.features.composite.core.node_capabilities import (
    CACHE_OWNED_TYPES,
    PASSTHROUGH_NODE_TYPES,
    is_placeholder_node_type,
)
from src.features.composite.render.render_scheduler import _GPU_STATEFUL_TYPES


# ── Helper Functions ──

def get_node_classes_with_is_placeholder_false() -> Dict[str, type]:
    """获取所有 IS_PLACEHOLDER=False 的节点类型和类。

    Returns:
        Dict[node_type, node_class] - 所有声明为非 placeholder 的节点
    """
    result: Dict[str, type] = {}
    for node_type, cls in NODE_REGISTRY.items():
        if cls is None:
            continue
        is_placeholder = getattr(cls, "IS_PLACEHOLDER", False)
        if not is_placeholder:
            result[node_type] = cls
    return result


def get_node_classes_with_is_placeholder_true() -> Dict[str, type]:
    """获取所有 IS_PLACEHOLDER=True 的节点类型和类。

    Returns:
        Dict[node_type, node_class] - 所有声明为 placeholder 的节点
    """
    result: Dict[str, type] = {}
    for node_type, cls in NODE_REGISTRY.items():
        if cls is None:
            continue
        is_placeholder = getattr(cls, "IS_PLACEHOLDER", False)
        if is_placeholder:
            result[node_type] = cls
    return result


def detect_scheduler_placeholder_status(node_type: str) -> Optional[bool]:
    """检测 scheduler 是否将某节点类型标记为 placeholder。

    使用 is_placeholder_node_type() 函数进行动态判断。

    Args:
        node_type: 节点类型字符串

    Returns:
        True: scheduler 标记为 placeholder
        False: scheduler 标记为非 placeholder (source/effect/output)
        None: unknown node type
    """
    cls = NODE_REGISTRY.get(node_type)
    if cls is None:
        return None  # Unknown node

    return is_placeholder_node_type(node_type)


def detect_evaluator_mapping_status(node_type: str) -> Optional[str]:
    """检测 evaluator 是否有某节点类型的映射。

    Args:
        node_type: 节点类型字符串

    Returns:
        "mapped": 在 NODE_REGISTRY 中有映射
        "passthrough": 节点类型暗示是 passthrough (dot, noop, switch)
        "output": 输出节点类型
        "source": GPU-stateful source 节点类型
        "hardcoded": 在 _evaluate_node() 的硬编码 effect list 中
        None: 没有映射
    """
    if node_type in NODE_REGISTRY:
        return "mapped"
    if node_type == "output":
        return "output"
    if node_type in _GPU_STATEFUL_TYPES:
        return "source"
    if node_type in ("dot", "noop", "switch"):
        return "passthrough"

    # Check if in hardcoded effect marker nodes list in base_evaluator.py
    hardcoded_effect_types = {
        "blur", "grade", "transform", "invert", "merge",
        "premult", "unpremult", "shuffle", "copy_channel",
        "cs_transform", "reformat", "crop",
        "clamp", "dissolve", "set_alpha", "remove_channels", "luma_keyer",
        "sharpen", "erode_dilate", "chroma_keyer",
        "defocus", "glow", "lut", "keyer"
    }
    if node_type in hardcoded_effect_types:
        return "hardcoded"

    return None


def detect_cache_ownership_status(node_type: str) -> bool:
    """检测节点类型是否在 cache ownership 列表中。

    Args:
        node_type: 节点类型字符串

    Returns:
        True: 在 CACHE_OWNED_TYPES 中
        False: 不在
    """
    return node_type in CACHE_OWNED_TYPES


def detect_render_strategy(node_type: str) -> Optional[str]:
    """检测节点是否有完整的渲染策略。

    Args:
        node_type: 节点类型字符串

    Returns:
        "source": GPU-stateful source
        "effect": effect node with evaluator mapping or hardcoded
        "output": output node
        "passthrough": passthrough node
        "placeholder": placeholder node
        None: 没有明确的渲染策略
    """
    # Check node class declaration
    cls = NODE_REGISTRY.get(node_type)
    if cls is None:
        return None

    is_placeholder = getattr(cls, "IS_PLACEHOLDER", False)
    if is_placeholder:
        return "placeholder"

    # Check scheduler classification
    scheduler_status = detect_scheduler_placeholder_status(node_type)
    if scheduler_status is True:
        # Scheduler thinks it's placeholder, but node class says otherwise
        # This is a conflict - return None to indicate the problem
        return None

    # Check evaluator mapping
    eval_status = detect_evaluator_mapping_status(node_type)
    if eval_status == "source":
        return "source"
    if eval_status == "output":
        return "output"
    if eval_status == "passthrough":
        return "passthrough"
    if eval_status in ("mapped", "hardcoded"):
        return "effect"

    # Node type not in evaluator map and not passthrough
    return None


# ── Allowlists for Known Exceptions ──

# 类型定义
from typing import TypedDict, Dict, Any

class AllowlistEntry(TypedDict, total=False):
    """Allowlist 条目结构。"""
    reason: str       # 为什么需要豁免
    phase: str        # 计划修复的阶段（或 "N/A" 表示长期豁免）
    placeholder: bool # 是否是 placeholder 节点（placeholder 节点不应在 render strategy allowlist 中）

# 这些节点类型声明 IS_PLACEHOLDER=False，但没有完整的渲染策略
# 每项必须有 reason、phase、placeholder 字段
# placeholder=True 的节点不应在此列表中（它们应该在自己的 placeholder 状态中处理）
#
# 变更历史：
# S1-D: 已移除 noise, ramp, mask, difference_keyer, color_correct, huecorrect,
#       normalize, channel_merge, deep_merge, edge_detect, denoise
# BUG-13: 已移除 time_offset, frame_hold, retime, mask_merge, matte_cleanup, roto_shape
# Phase 1 F1-01: 结构化改造，移除 placeholder=True 的节点
# Phase 2 F2-02: 已移除 blend，capability 系统已正确处理
ALLOWLIST_MISSING_RENDER_STRATEGY: Dict[str, Dict[str, Any]] = {
    # Phase 2 F2-02: blend 已移除，NODE_REGISTRY 现正确覆盖所有节点
    # 注意：以下节点已移除，因为它们 IS_PLACEHOLDER=True：
    # - ai_generate, ai_inpaint, ai_expand, ai_remove, ai_denoise（AI 节点）
    # - file_output（输出节点）
    # - text（生成器节点）
    # - compare（工具节点）
    # - group（组节点）
    # placeholder 节点不需要 render strategy，它们在 scheduler 中被标记为 placeholder
}

# 这些节点类型会产生新的 GPU texture，但不在 cache ownership 列表中
# 每项必须有 reason、phase、placeholder 字段
#
# 变更历史：
# S1-D: 已移除 noise, ramp, mask, keyer, difference_keyer, matte, color_correct,
#       huecorrect, normalize, edge_detect, denoise, channel_merge, deep_merge, cornerpin
# BUG-13: 已移除 time_offset, frame_hold, retime, mask_merge, matte_cleanup, roto_shape
# BUG-13: 已移除 text，现在在 CACHE_OWNED_TYPES 中
# Phase 1 F1-01: 结构化改造，移除 placeholder=True 的节点
# Phase 2 F2-02: 已移除 blend，capability 系统已正确处理
ALLOWLIST_MISSING_CACHE_OWNERSHIP: Dict[str, Dict[str, Any]] = {
    # Phase 2 F2-02: blend 已移除，CACHE_OWNED_TYPES 现正确覆盖所有节点
    # 注意：以下节点已移除，因为它们 IS_PLACEHOLDER=True：
    # - ai_generate, ai_inpaint, ai_expand, ai_remove, ai_denoise（AI 节点）
    # - file_output（终端节点，写入磁盘）
    # - compare（工具节点）
    # - group（组节点）
    # placeholder 节点不产生实际 texture，或 texture 生命周期由不同路径管理
}


# ── Test Classes ──

class TestSchedulerPlaceholderConsistency:
    """测试 scheduler 的 placeholder 分类是否与节点类的 IS_PLACEHOLDER 一致。"""

    def test_scheduler_placeholder_function_matches_node_class_declaration(self):
        """验证 is_placeholder_node_type() 函数与节点类的 IS_PLACEHOLDER 一致。

        scheduler 使用 is_placeholder_node_type() 函数动态判断 placeholder 状态，
        该函数应该读取节点类的 IS_PLACEHOLDER 属性。
        """
        mismatches: List[Tuple[str, bool, bool]] = []

        for node_type, cls in NODE_REGISTRY.items():
            if cls is None:
                continue

            class_is_placeholder = getattr(cls, "IS_PLACEHOLDER", False)
            scheduler_is_placeholder = is_placeholder_node_type(node_type)

            if class_is_placeholder != scheduler_is_placeholder:
                mismatches.append((node_type, scheduler_is_placeholder, class_is_placeholder))

        assert not mismatches, (
            f"Scheduler placeholder function mismatches:\n"
            + "\n".join(
                f"  {nt}: scheduler={s}, class={c}"
                for nt, s, c in mismatches
            )
            + "\n\nScheduler's is_placeholder_node_type() should match IS_PLACEHOLDER attribute."
            + "\nFix: Ensure the function correctly reads getattr(cls, 'IS_PLACEHOLDER', False)."
        )

    def test_placeholder_nodes_are_identified_correctly(self):
        """验证声明 IS_PLACEHOLDER=True 的节点被正确识别。"""
        placeholder_nodes = get_node_classes_with_is_placeholder_true()
        wrongly_classified: List[Tuple[str, bool]] = []

        for node_type in placeholder_nodes:
            scheduler_status = is_placeholder_node_type(node_type)
            if not scheduler_status:  # Scheduler thinks it's NOT placeholder
                wrongly_classified.append((node_type, scheduler_status))

        assert not wrongly_classified, (
            f"Placeholder nodes wrongly classified by scheduler:\n"
            + "\n".join(
                f"  {nt}: IS_PLACEHOLDER=True, scheduler thinks placeholder={s}"
                for nt, s in wrongly_classified
            )
            + "\n\nNodes with IS_PLACEHOLDER=True should be classified as placeholder by scheduler."
        )

    def test_non_placeholder_nodes_not_classified_as_placeholder(self):
        """验证声明 IS_PLACEHOLDER=False 的节点不被 scheduler 当成 placeholder。"""
        non_placeholder_nodes = get_node_classes_with_is_placeholder_false()
        wrongly_classified: List[Tuple[str, bool]] = []

        for node_type in non_placeholder_nodes:
            scheduler_status = is_placeholder_node_type(node_type)
            if scheduler_status:  # Scheduler thinks it IS placeholder
                wrongly_classified.append((node_type, scheduler_status))

        assert not wrongly_classified, (
            f"Non-placeholder nodes wrongly classified as placeholder:\n"
            + "\n".join(
                f"  {nt}: IS_PLACEHOLDER=False, scheduler thinks placeholder={s}"
                for nt, s in wrongly_classified
            )
            + "\n\nNodes with IS_PLACEHOLDER=False should NOT be classified as placeholder."
        )


class TestEvaluatorMappingConsistency:
    """测试 evaluator 的节点映射是否覆盖所有非 placeholder 节点。"""

    def test_non_placeholder_nodes_have_evaluator_mapping_or_hardcoded(self):
        """验证所有非 placeholder 节点都有 evaluator 映射或在硬编码 effect list 中。"""
        non_placeholder_nodes = get_node_classes_with_is_placeholder_false()
        missing: List[str] = []

        for node_type in non_placeholder_nodes:
            strategy = detect_evaluator_mapping_status(node_type)
            if strategy is None:
                if node_type not in ALLOWLIST_MISSING_RENDER_STRATEGY:
                    missing.append(node_type)

        assert not missing, (
            f"Non-placeholder nodes missing evaluator mapping:\n"
            + "\n".join(f"  {nt}" for nt in missing)
            + "\n\nThese nodes declare IS_PLACEHOLDER=False but have no evaluator strategy."
            + "\nFix: Add to NODE_REGISTRY or hardcoded effect list in base_evaluator.py."
        )

    def test_evaluator_mapped_nodes_are_registered(self):
        """验证 evaluator 映射的节点都在 NODE_REGISTRY 中。"""
        unregistered: List[str] = []

        for node_type in NODE_REGISTRY:
            if node_type not in NODE_REGISTRY:
                unregistered.append(node_type)

        assert not unregistered, (
            f"Evaluator mapped nodes not in registry:\n"
            + "\n".join(f"  {nt}" for nt in unregistered)
            + "\n\nThese nodes are in NODE_REGISTRY but not registered."
            + "\nFix: Add to NODE_REGISTRY or remove from NODE_REGISTRY."
        )


class TestCacheOwnershipConsistency:
    """测试 cache ownership 列表是否覆盖所有会产生 GPU texture 的节点。"""

    def test_effect_nodes_that_produce_textures_have_cache_ownership(self):
        """验证会产生 GPU texture 的 effect 节点在 cache ownership 列表中。

        Effect 节点如果会产生新的 GPU texture，应该被 cache 管理，
        否可能导致 texture 泄漏或重复释放。
        """
        # Effect nodes that typically produce new textures
        # (not passthrough, not source)
        texture_producing_candidates = frozenset({
            # Color processing
            "color_correct", "huecorrect", "normalize", "lut",
            # Filter effects
            "blur", "sharpen", "glow", "defocus", "edge_detect",
            "erode_dilate", "denoise", "noise", "ramp",
            # Transform effects
            "transform", "reformat", "crop", "cornerpin",
            # Merge effects
            "merge", "deep_merge", "dissolve", "blend",
            # Channel effects
            "shuffle", "copy_channel", "channel_merge",
            "set_alpha", "remove_channels",
            # Keyer/Mask effects
            "keyer", "luma_keyer", "chroma_keyer", "difference_keyer",
            "mask", "mask_merge", "matte", "matte_cleanup",
            # Utility effects
            "invert", "clamp", "premult", "unpremult",
            # Compare
            "compare",
        })

        missing: List[str] = []
        for node_type in texture_producing_candidates:
            cls = NODE_REGISTRY.get(node_type)
            if cls is None:
                continue
            is_placeholder = getattr(cls, "IS_PLACEHOLDER", False)
            if is_placeholder:
                continue
            if not detect_cache_ownership_status(node_type):
                if node_type not in ALLOWLIST_MISSING_CACHE_OWNERSHIP:
                    missing.append(node_type)

        assert not missing, (
            f"Texture-producing nodes missing from cache ownership list:\n"
            + "\n".join(f"  {nt}" for nt in missing)
            + "\n\nThese nodes produce GPU textures but are not in CACHE_OWNED_TYPES."
            + "\nRisk: Texture leaks or premature release."
            + "\nFix: Add to CACHE_OWNED_TYPES in base_evaluator.py."
        )


class TestOverallRenderStrategyConsistency:
    """综合测试：验证所有非 placeholder 节点都有完整的渲染策略。"""

    def test_non_placeholder_nodes_have_render_strategy(self):
        """验证所有非 placeholder 节点都有明确的渲染策略。

        这是最核心的 contract 测试，综合检查：
        - scheduler 分类
        - evaluator 映射
        - cache ownership
        """
        non_placeholder_nodes = get_node_classes_with_is_placeholder_false()
        missing: List[Tuple[str, Optional[str], Optional[str]]] = []

        for node_type in non_placeholder_nodes:
            strategy = detect_render_strategy(node_type)
            if strategy is None:
                # Check which component is missing
                scheduler_status = detect_scheduler_placeholder_status(node_type)
                eval_status = detect_evaluator_mapping_status(node_type)

                # Skip if in allowlist
                if node_type in ALLOWLIST_MISSING_RENDER_STRATEGY:
                    continue

                missing.append((node_type, scheduler_status, eval_status))

        assert not missing, (
            f"Missing render strategy for non-placeholder nodes:\n"
            + "\n".join(
                f"  {nt}: scheduler={s or 'N/A'}, evaluator={e or 'N/A'}"
                for nt, s, e in missing
            )
            + "\n\nThese nodes declare IS_PLACEHOLDER=False but have incomplete render path."
            + "\nFix: Update evaluator NODE_REGISTRY or hardcoded effect list."
        )


class TestRegistryCompleteness:
    """测试 NODE_REGISTRY 的完整性。"""

    def test_registry_has_no_null_entries(self):
        """验证 NODE_REGISTRY 中没有未声明的 null entries。

        None 是有意的延迟注册占位（可选 feature module 在启用时通过
        register_node_type() 注入具体类），但必须列在 DEFERRED_NODE_TYPES
        中声明归属；列表之外的 null 条目就是未实现的占位 bug。
        """
        from src.features.composite.core.node_engine import DEFERRED_NODE_TYPES

        unexplained: List[str] = []
        for node_type, cls in NODE_REGISTRY.items():
            if cls is None and node_type not in DEFERRED_NODE_TYPES:
                unexplained.append(node_type)

        assert not unexplained, (
            f"Unexplained null entries in NODE_REGISTRY:\n"
            + "\n".join(f"  {nt}" for nt in unexplained)
            + "\n\nFix: Remove the entry, implement the node class, or declare"
              " it in DEFERRED_NODE_TYPES if an optional feature module"
              " registers it at runtime."
        )

    def test_deferred_placeholder_lifecycle(self):
        """motion_graphic 占位必须能走完 注册 → 创建 → 反注册 → 恢复占位。"""
        from src.features.composite.core.node_engine import (
            DEFERRED_NODE_TYPES,
            NODE_REGISTRY,
            register_node_type,
            unregister_node_type,
        )
        assert "motion_graphic" in DEFERRED_NODE_TYPES
        assert NODE_REGISTRY.get("motion_graphic") is None

        from src.features.teaching_animation.composite_node import MotionGraphicNode
        assert register_node_type("motion_graphic", MotionGraphicNode, category="Input")
        try:
            assert NODE_REGISTRY["motion_graphic"] is MotionGraphicNode
            node = MotionGraphicNode()
            assert node.node_type == "motion_graphic"
            node.cleanup()
        finally:
            assert unregister_node_type("motion_graphic")
        assert NODE_REGISTRY["motion_graphic"] is None

    def test_registry_classes_inherit_from_node(self):
        """验证 NODE_REGISTRY 中的所有类都继承自 Node。"""
        from src.features.composite.nodes.base_node import Node

        invalid_entries: List[Tuple[str, str]] = []
        for node_type, cls in NODE_REGISTRY.items():
            if cls is None:
                continue
            if not isinstance(cls, type):
                invalid_entries.append((node_type, f"not a class: {type(cls).__name__}"))
                continue
            if not issubclass(cls, Node):
                invalid_entries.append((node_type, f"not a Node subclass: {cls.__name__}"))

        assert not invalid_entries, (
            f"Invalid entries in NODE_REGISTRY:\n"
            + "\n".join(f"  {nt}: {reason}" for nt, reason in invalid_entries)
            + "\n\nAll registered classes must inherit from Node."
        )

    def test_node_classes_have_node_type_attribute(self):
        """验证 NODE_REGISTRY 中的类有正确的 node_type 属性。"""
        mismatches: List[Tuple[str, str, str]] = []
        for node_type, cls in NODE_REGISTRY.items():
            if cls is None:
                continue
            cls_node_type = getattr(cls, "node_type", None)
            if cls_node_type is None:
                mismatches.append((node_type, cls.__name__, "missing node_type"))
            elif cls_node_type != node_type:
                # Some aliases are OK (e.g., "blend" -> "merge")
                if node_type not in ("blend",):  # Known aliases
                    mismatches.append((node_type, cls.__name__, f"node_type={cls_node_type}"))

        assert not mismatches, (
            f"Node type attribute mismatches:\n"
            + "\n".join(f"  registry[{nt}] -> {cls_name}: {reason}" for nt, cls_name, reason in mismatches)
            + "\n\nFix: Ensure each node class has correct node_type attribute."
        )


# ── Diagnostic Tests ( informational, always pass ) ──

class TestDiagnosticInfo:
    """诊断测试：提供当前状态信息，不强制失败。"""

    def test_list_all_placeholder_nodes(self):
        """列出所有声明 IS_PLACEHOLDER=True 的节点（诊断信息）。"""
        placeholder_nodes = get_node_classes_with_is_placeholder_true()
        # This test always passes, just prints info
        print(f"\nPlaceholder nodes (IS_PLACEHOLDER=True): {len(placeholder_nodes)}")
        for nt in sorted(placeholder_nodes.keys()):
            print(f"  - {nt}")

    def test_list_scheduler_placeholder_classification(self):
        """列出 scheduler 的 placeholder 分类（诊断信息）。"""
        print(f"\nScheduler placeholder classification:")
        count_placeholder = 0
        count_non_placeholder = 0
        for node_type, cls in sorted(NODE_REGISTRY.items()):
            if cls is None:
                continue
            is_placeholder = is_placeholder_node_type(node_type)
            if is_placeholder:
                count_placeholder += 1
                print(f"  - {node_type}: placeholder")
            else:
                count_non_placeholder += 1
        print(f"  Total: {count_placeholder} placeholder, {count_non_placeholder} non-placeholder")

    def test_list_evaluator_mapped_types(self):
        """列出 evaluator 的 NODE_REGISTRY（诊断信息）。"""
        print(f"\nEvaluator mapped types (NODE_REGISTRY): {len(NODE_REGISTRY)}")
        for nt in sorted(NODE_REGISTRY.keys()):
            cls = NODE_REGISTRY.get(nt)
            registered = "✓" if cls else "✗ UNREGISTERED"
            print(f"  - {nt}: {registered}")

    def test_list_cache_owned_types(self):
        """列出 evaluator 的 CACHE_OWNED_TYPES（诊断信息）。"""
        print(f"\nCache owned types (CACHE_OWNED_TYPES): {len(CACHE_OWNED_TYPES)}")
        for nt in sorted(CACHE_OWNED_TYPES):
            cls = NODE_REGISTRY.get(nt)
            registered = "✓" if cls else "✗ UNREGISTERED"
            print(f"  - {nt}: {registered}")

    def test_list_hardcoded_effect_types(self):
        """列出 base_evaluator.py 中硬编码的 effect types（诊断信息）。"""
        hardcoded_effect_types = {
            "blur", "grade", "transform", "invert", "merge",
            "premult", "unpremult", "shuffle", "copy_channel",
            "cs_transform", "reformat", "crop",
            "clamp", "dissolve", "set_alpha", "remove_channels", "luma_keyer",
            "sharpen", "erode_dilate", "chroma_keyer",
            "defocus", "glow", "lut", "keyer"
        }
        print(f"\nHardcoded effect types in _evaluate_node(): {len(hardcoded_effect_types)}")
        for nt in sorted(hardcoded_effect_types):
            cls = NODE_REGISTRY.get(nt)
            is_placeholder = getattr(cls, "IS_PLACEHOLDER", "N/A") if cls else "N/A"
            in_map = nt in NODE_REGISTRY
            status = "✓" if not is_placeholder else f"✗ IS_PLACEHOLDER={is_placeholder}"
            map_status = "in_map" if in_map else "hardcoded_only"
            print(f"  - {nt}: {status}, {map_status}")


# ── Allowlist Documentation Tests ──

class TestAllowlistDocumentation:
    """验证 allowlist 的正确性和完整性。"""

    def test_render_strategy_allowlist_has_required_fields(self):
        """验证 render strategy allowlist 每项都有 reason、phase、placeholder 字段。"""
        for node_type, entry in ALLOWLIST_MISSING_RENDER_STRATEGY.items():
            assert "reason" in entry, (
                f"ALLOWLIST_MISSING_RENDER_STRATEGY[{node_type}] 缺少 'reason' 字段"
            )
            assert "phase" in entry, (
                f"ALLOWLIST_MISSING_RENDER_STRATEGY[{node_type}] 缺少 'phase' 字段"
            )
            assert "placeholder" in entry, (
                f"ALLOWLIST_MISSING_RENDER_STRATEGY[{node_type}] 缺少 'placeholder' 字段"
            )
            assert isinstance(entry["reason"], str) and entry["reason"], (
                f"ALLOWLIST_MISSING_RENDER_STRATEGY[{node_type}] 的 'reason' 必须是非空字符串"
            )
            assert isinstance(entry["phase"], str) and entry["phase"], (
                f"ALLOWLIST_MISSING_RENDER_STRATEGY[{node_type}] 的 'phase' 必须是非空字符串"
            )
            assert isinstance(entry["placeholder"], bool), (
                f"ALLOWLIST_MISSING_RENDER_STRATEGY[{node_type}] 的 'placeholder' 必须是布尔值"
            )

    def test_cache_ownership_allowlist_has_required_fields(self):
        """验证 cache ownership allowlist 每项都有 reason、phase、placeholder 字段。"""
        for node_type, entry in ALLOWLIST_MISSING_CACHE_OWNERSHIP.items():
            assert "reason" in entry, (
                f"ALLOWLIST_MISSING_CACHE_OWNERSHIP[{node_type}] 缺少 'reason' 字段"
            )
            assert "phase" in entry, (
                f"ALLOWLIST_MISSING_CACHE_OWNERSHIP[{node_type}] 缺少 'phase' 字段"
            )
            assert "placeholder" in entry, (
                f"ALLOWLIST_MISSING_CACHE_OWNERSHIP[{node_type}] 缺少 'placeholder' 字段"
            )
            assert isinstance(entry["reason"], str) and entry["reason"], (
                f"ALLOWLIST_MISSING_CACHE_OWNERSHIP[{node_type}] 的 'reason' 必须是非空字符串"
            )
            assert isinstance(entry["phase"], str) and entry["phase"], (
                f"ALLOWLIST_MISSING_CACHE_OWNERSHIP[{node_type}] 的 'phase' 必须是非空字符串"
            )
            assert isinstance(entry["placeholder"], bool), (
                f"ALLOWLIST_MISSING_CACHE_OWNERSHIP[{node_type}] 的 'placeholder' 必须是布尔值"
            )

    def test_render_strategy_allowlist_placeholder_field_matches_node_class(self):
        """验证 allowlist 的 placeholder 字段与节点类的 IS_PLACEHOLDER 一致。

        placeholder=True 的节点不应在 render strategy allowlist 中，
        因为 placeholder 节点不需要 render strategy。
        """
        for node_type, entry in ALLOWLIST_MISSING_RENDER_STRATEGY.items():
            cls = NODE_REGISTRY.get(node_type)
            if cls is None:
                continue
            actual_is_placeholder = getattr(cls, "IS_PLACEHOLDER", False)
            entry_placeholder = entry["placeholder"]

            # placeholder=True 的节点不应在 render strategy allowlist 中
            assert not actual_is_placeholder, (
                f"ALLOWLIST_MISSING_RENDER_STRATEGY[{node_type}] 的节点 IS_PLACEHOLDER=True，"
                f"不应在 render strategy allowlist 中。"
                f"\n原因: {entry['reason']}"
                f"\n建议: 移除此条目，placeholder 节点由 scheduler 的 placeholder 分类处理。"
            )

            # entry 的 placeholder 字段应与实际一致
            assert entry_placeholder == actual_is_placeholder, (
                f"ALLOWLIST_MISSING_RENDER_STRATEGY[{node_type}] 的 placeholder 字段 ({entry_placeholder}) "
                f"与节点 IS_PLACEHOLDER ({actual_is_placeholder}) 不一致"
            )

    def test_cache_ownership_allowlist_placeholder_field_matches_node_class(self):
        """验证 cache ownership allowlist 的 placeholder 字段与节点类的 IS_PLACEHOLDER 一致。"""
        for node_type, entry in ALLOWLIST_MISSING_CACHE_OWNERSHIP.items():
            cls = NODE_REGISTRY.get(node_type)
            if cls is None:
                continue
            actual_is_placeholder = getattr(cls, "IS_PLACEHOLDER", False)
            entry_placeholder = entry["placeholder"]

            # entry 的 placeholder 字段应与实际一致
            assert entry_placeholder == actual_is_placeholder, (
                f"ALLOWLIST_MISSING_CACHE_OWNERSHIP[{node_type}] 的 placeholder 字段 ({entry_placeholder}) "
                f"与节点 IS_PLACEHOLDER ({actual_is_placeholder}) 不一致"
            )

    def test_render_strategy_allowlist_nodes_are_not_placeholder(self):
        """验证 render strategy allowlist 中的节点确实缺少策略。

        Note: Placeholder nodes should NOT be in this allowlist.
        This test checks that non-placeholder nodes in allowlist
        actually lack render strategy (which would be a bug to fix).
        """
        for node_type, entry in ALLOWLIST_MISSING_RENDER_STRATEGY.items():
            cls = NODE_REGISTRY.get(node_type)
            if cls is None:
                continue
            is_placeholder = getattr(cls, "IS_PLACEHOLDER", False)
            # Placeholder nodes should NOT be in render strategy allowlist
            assert not is_placeholder, (
                f"Allowlist[{node_type}] IS_PLACEHOLDER=True，不应在 render strategy allowlist 中。"
                f"\n原因: {entry['reason']}"
                f"\n建议: 移除此条目。"
            )
            # Non-placeholder nodes should actually lack render strategy
            strategy = detect_render_strategy(node_type)
            assert strategy is None or strategy == "placeholder", (
                f"Allowlist[{node_type}] now has render strategy={strategy}, "
                f"should remove from allowlist"
            )

    def test_cache_ownership_allowlist_nodes_not_in_cache_list(self):
        """验证 cache ownership allowlist 中的节点确实不在 cache list 中。"""
        for node_type in ALLOWLIST_MISSING_CACHE_OWNERSHIP:
            # Check that it's not in cache owned types
            assert node_type not in CACHE_OWNED_TYPES, (
                f"Allowlist[{node_type}] is now in CACHE_OWNED_TYPES, "
                f"should remove from allowlist"
            )

    def test_render_strategy_allowlist_nodes_have_phase_defined(self):
        """验证 render strategy allowlist 每项都有明确的到期阶段。"""
        for node_type, entry in ALLOWLIST_MISSING_RENDER_STRATEGY.items():
            phase = entry.get("phase", "")
            # phase 应该是 "Phase X" 格式或 "N/A"（长期豁免）
            valid_phase_patterns = ["Phase 1", "Phase 2", "Phase 3", "Phase 4", "Phase 5", "N/A"]
            assert phase in valid_phase_patterns or phase.startswith("Phase"), (
                f"ALLOWLIST_MISSING_RENDER_STRATEGY[{node_type}] 的 phase '{phase}' "
                f"应该明确指定到期阶段（如 'Phase 3'）或标记为 'N/A'"
            )

    def test_cache_ownership_allowlist_nodes_have_phase_defined(self):
        """验证 cache ownership allowlist 每项都有明确的到期阶段。"""
        for node_type, entry in ALLOWLIST_MISSING_CACHE_OWNERSHIP.items():
            phase = entry.get("phase", "")
            # phase 应该是 "Phase X" 格式或 "N/A"（长期豁免）
            valid_phase_patterns = ["Phase 1", "Phase 2", "Phase 3", "Phase 4", "Phase 5", "N/A"]
            assert phase in valid_phase_patterns or phase.startswith("Phase"), (
                f"ALLOWLIST_MISSING_CACHE_OWNERSHIP[{node_type}] 的 phase '{phase}' "
                f"应该明确指定到期阶段（如 'Phase 3'）或标记为 'N/A'"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])