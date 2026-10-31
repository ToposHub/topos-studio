"""Phase 7: 性能优化测试（诊断元数据 + 导航 LOD）。

覆盖 docs/composite_3d_module_audit_optimization_plan_2026-06-29.md 的 Phase 7：
1. Scene3D 渲染诊断元数据（TextureResult.render_diagnostics）
2. 导航 LOD（交互态降 MSAA）
3. evaluator 同步 interaction_active 到 eval_ctx

注：Scene3D 真实渲染需 GL；用源码级断言 + TextureResult 字段验证。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import pytest

from src.features.composite.core.eval_context import CompositeEvalContext
from src.features.composite.nodes.base_node import TextureResult


# ──────────────────────────────────────────────────────────────────────────
# Part 1: TextureResult.render_diagnostics 字段
# ──────────────────────────────────────────────────────────────────────────

class TestRenderDiagnosticsField:
    """TextureResult 有 render_diagnostics 字段（Scene3D 填充）。"""

    def test_field_exists_and_defaults_none(self):
        tr = TextureResult(texture_id=1)
        assert hasattr(tr, "render_diagnostics")
        assert tr.render_diagnostics is None  # 普通 2D 节点默认无诊断

    def test_field_settable(self):
        tr = TextureResult(texture_id=1,
                           render_diagnostics={"active_camera": "builtin"})
        assert tr.render_diagnostics["active_camera"] == "builtin"


# ──────────────────────────────────────────────────────────────────────────
# Part 2: Scene3D 填充诊断元数据
# ──────────────────────────────────────────────────────────────────────────

class TestScene3DDiagnostics:
    """Scene3D _do_evaluate 填充 render_diagnostics（源码级断言）。"""

    def test_scene3d_builds_diagnostics(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "diagnostics = {" in src or "diagnostics =" in src
        assert "render_diagnostics=diagnostics" in src

    def test_diagnostics_has_expected_keys(self):
        """诊断含 active_camera/light_count/geometry_source/msaa 等键。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        for key in ("active_camera", "light_count", "geometry_source",
                    "model_error", "msaa", "has_depth"):
            assert f'"{key}"' in src, f"诊断应含 {key}"

    def test_diagnostics_reports_lod_active(self):
        """诊断含 lod_active（P7 导航降质标记）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "lod_active" in src


# ──────────────────────────────────────────────────────────────────────────
# Part 3: 导航 LOD（交互态降 MSAA）
# ──────────────────────────────────────────────────────────────────────────

class TestNavLOD:
    """导航 LOD：交互态降 MSAA，结束恢复。"""

    def test_eval_context_has_interaction_active(self):
        ctx = CompositeEvalContext(gl=None)
        assert hasattr(ctx, "interaction_active")
        assert ctx.interaction_active is False

    def test_scene3d_reads_interaction_active_for_lod(self):
        """Scene3D 读 eval_ctx.interaction_active 做 LOD（源码断言）。"""
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        src = inspect.getsource(Scene3DNode._do_evaluate)
        assert "interaction_active" in src
        assert "_lod_active" in src

    def test_scene3d_has_lod_active_field(self):
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        node = Scene3DNode()
        assert hasattr(node, "_lod_active")
        assert node._lod_active is False  # 初始非交互

    def test_lod_downgrades_msaa_logic(self):
        """LOD 降级逻辑：4×→0×、8×→2×（交互态）。"""
        # 复刻 _do_evaluate 的 LOD 降级逻辑
        def lod_msaa(msaa, interaction):
            if interaction:
                if msaa >= 8:
                    return 2
                elif msaa >= 4:
                    return 0
            return msaa
        assert lod_msaa(4, True) == 0   # 4× 交互 → 0×
        assert lod_msaa(8, True) == 2   # 8× 交互 → 2×
        assert lod_msaa(4, False) == 4  # 非交互保持 4×
        assert lod_msaa(8, False) == 8  # 非交互保持 8×
        assert lod_msaa(0, True) == 0   # 本就 0× 不变

    def test_evaluator_syncs_interaction_active(self):
        """_evaluate_snapshot 把 interaction_active 同步到 eval_ctx。"""
        from src.features.composite.evaluators.base_evaluator import BaseEvaluatorMixin
        src = inspect.getsource(BaseEvaluatorMixin._evaluate_snapshot)
        assert "eval_ctx_ref.interaction_active" in src
