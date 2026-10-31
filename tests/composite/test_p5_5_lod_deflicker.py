"""P5-5：LOD 切换去闪烁测试。

覆盖 docs/composite_3d_viewport_performance_master_plan_2026-07-07.md §9.4 (P5-5)：
- 停止导航后不立即切回 LOD0，保持导航 LOD 一段延迟（_lod_hold_ms）
- 保持期内 _resolve_lod_level 返回上一帧 LOD level
- 保持期过后才真正回 LOD0
- 导航中正常决策（LOD1/LOD2），记录时间戳

注：用 Model3DNode.__new__ + getattr 容错构造实例，与 test_lod_builder 同套路。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import time
import inspect


class TestLODDeflickerFields:
    """Model3DNode 有去闪烁状态字段。"""

    def test_has_lod_hold_fields(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        src = inspect.getsource(Model3DNode.__init__)
        assert "self._lod_last_nav_time" in src
        assert "self._lod_hold_ms" in src

    def test_resolve_lod_level_has_deflicker_logic(self):
        """_resolve_lod_level 包含保持期判定（非交互态时检查 _lod_hold_ms）。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        src = inspect.getsource(Model3DNode._resolve_lod_level)
        assert "_lod_last_nav_time" in src
        assert "_lod_hold_ms" in src
        # 保持期内返回 _cur_lod（不回 0）
        assert "return _cur_lod" in src


class TestLODDeflickerBehavior:
    """去闪烁行为：停止导航后保持期内维持 LOD。"""

    def test_hold_period_keeps_lod_after_nav_stop(self):
        """停止导航后立即调用，若保持期内 → 返回上一帧 LOD（非 0）。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        node = Model3DNode.__new__(Model3DNode)
        node.parameters = {"viewport_lod_mode": "auto"}
        # 模拟刚结束导航：_current_lod_level=2，_lod_last_nav_time=now
        node._current_lod_level = 2
        node._lod_last_nav_time = time.monotonic()
        node._lod_hold_ms = 200.0

        class FakeCtx:
            interaction_active = False  # 已停止导航
            global_3d_scene_settings = {"interactive_lod": True}
        # 保持期内（< 200ms）→ 返回 2（不闪回 LOD0）
        assert node._resolve_lod_level(FakeCtx(), "preview", []) == 2

    def test_hold_period_expired_returns_lod0(self):
        """保持期过后（_lod_last_nav_time 很久以前）→ 回 LOD0。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        node = Model3DNode.__new__(Model3DNode)
        node.parameters = {"viewport_lod_mode": "auto"}
        node._current_lod_level = 2
        # _lod_last_nav_time 设为 10 秒前（远超 200ms 保持期）
        node._lod_last_nav_time = time.monotonic() - 10.0
        node._lod_hold_ms = 200.0

        class FakeCtx:
            interaction_active = False
            global_3d_scene_settings = {"interactive_lod": True}
        assert node._resolve_lod_level(FakeCtx(), "preview", []) == 0

    def test_nav_active_still_uses_decision(self):
        """导航中（interaction_active=True）正常决策，不进入保持期逻辑。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        node = Model3DNode.__new__(Model3DNode)
        node.parameters = {"viewport_lod_mode": "auto"}
        node._current_lod_level = 0
        node._lod_last_nav_time = 0.0
        node._lod_hold_ms = 200.0

        class FakeCtx:
            interaction_active = True
            global_3d_scene_settings = {"interactive_lod": True}
        # 导航中 + 小模型 → 0；但更重要的是 _lod_last_nav_time 被更新
        result = node._resolve_lod_level(FakeCtx(), "preview", [])
        # 小模型导航态返回 0（无需 LOD），但时间戳应被记录
        assert node._lod_last_nav_time > 0.0

    def test_hold_period_respects_zero_current_lod(self):
        """_current_lod_level=0 时保持期逻辑不影响（本来就 0）。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        node = Model3DNode.__new__(Model3DNode)
        node.parameters = {"viewport_lod_mode": "auto"}
        node._current_lod_level = 0
        node._lod_last_nav_time = time.monotonic()
        node._lod_hold_ms = 200.0

        class FakeCtx:
            interaction_active = False
            global_3d_scene_settings = {"interactive_lod": True}
        # current=0 → 保持期不触发，直接返回 0
        assert node._resolve_lod_level(FakeCtx(), "preview", []) == 0
