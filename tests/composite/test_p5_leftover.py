"""P5 遗留子任务测试（Milestone F 补完）。

覆盖：
- P5-5：LOD 切换去闪烁（导航态用 LOD2，停止后经 restore timer 延迟回到 LOD0，
  不在边界抖动）。
- P5-6：Viewport LOD UI（Model3D viewport_lod_mode 参数：auto/force_lod0/1/2）。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import inspect
import pytest


# ──────────────────────────────────────────────────────────────────────────
# P5-6：Viewport LOD 参数（auto/force LOD0/1/2）
# ──────────────────────────────────────────────────────────────────────────

class TestViewportLODMode:
    """Model3DNode.viewport_lod_mode 参数 + _resolve_lod_level 强制模式。"""

    def test_param_def_exists(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        names = [p.name for p in Model3DNode.PARAM_DEFS]
        assert "viewport_lod_mode" in names

    def test_param_def_options(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        p = next(x for x in Model3DNode.PARAM_DEFS if x.name == "viewport_lod_mode")
        assert p.default == "auto"
        assert "force_lod0" in p.labels
        assert "force_lod1" in p.labels
        assert "force_lod2" in p.labels

    def test_parameters_default_auto(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        node = Model3DNode()
        assert node.parameters.get("viewport_lod_mode") == "auto"

    def test_force_lod2_overrides_auto(self):
        """force_lod2 即使非导航态也强制 LOD2。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        node = Model3DNode.__new__(Model3DNode)

        class FakeCtx:
            interaction_active = False
            global_3d_scene_settings = {"interactive_lod": True}
        node.parameters = {"viewport_lod_mode": "force_lod2"}
        assert node._resolve_lod_level(FakeCtx(), "preview", [{"index_count": 100}]) == 2

    def test_force_lod0_overrides_nav(self):
        """force_lod0 即使导航态 + 大模型也强制 LOD0。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        node = Model3DNode.__new__(Model3DNode)

        class FakeCtxNav:
            interaction_active = True
            global_3d_scene_settings = {"interactive_lod": True}
        node.parameters = {"viewport_lod_mode": "force_lod0"}
        # 大模型 600k 三角
        assert node._resolve_lod_level(FakeCtxNav(), "preview", [{"index_count": 1_800_000}]) == 0

    def test_force_lod1(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        node = Model3DNode.__new__(Model3DNode)

        class FakeCtx:
            interaction_active = False
            global_3d_scene_settings = {"interactive_lod": True}
        node.parameters = {"viewport_lod_mode": "force_lod1"}
        assert node._resolve_lod_level(FakeCtx(), "preview", []) == 1

    def test_full_export_overrides_force(self):
        """合成输出（full/export）始终 LOD0，即使 force_lod2。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        node = Model3DNode.__new__(Model3DNode)

        class FakeCtx:
            interaction_active = True
            global_3d_scene_settings = {"interactive_lod": True}
        node.parameters = {"viewport_lod_mode": "force_lod2"}
        assert node._resolve_lod_level(FakeCtx(), "full", []) == 0
        assert node._resolve_lod_level(FakeCtx(), "export", []) == 0

    def test_auto_uses_interaction_decision(self):
        """auto 模式回退到原交互态决策（非导航=LOD0，导航+大模型=LOD2）。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        node = Model3DNode.__new__(Model3DNode)
        node.parameters = {"viewport_lod_mode": "auto"}

        class FakeCtxStatic:
            interaction_active = False
            global_3d_scene_settings = {"interactive_lod": True}

        class FakeCtxNav:
            interaction_active = True
            global_3d_scene_settings = {"interactive_lod": True}
        # 静止 = LOD0
        assert node._resolve_lod_level(FakeCtxStatic(), "preview", []) == 0
        # 导航 + 大模型 = LOD2
        assert node._resolve_lod_level(FakeCtxNav(), "preview", [{"index_count": 1_800_000}]) == 2

    def test_resolve_reads_viewport_lod_mode(self):
        from src.features.composite.nodes.model3d_node import Model3DNode
        src = inspect.getsource(Model3DNode._resolve_lod_level)
        assert "viewport_lod_mode" in src
        assert "force_lod0" in src
        assert "force_lod2" in src


# ──────────────────────────────────────────────────────────────────────────
# P5-5：LOD 切换去闪烁
# ──────────────────────────────────────────────────────────────────────────

class TestLODDeflicker:
    """LOD 恢复走 restore timer（延迟回 LOD0），不在边界抖动。"""

    def test_restore_timer_delays_lod0_recovery(self):
        """P1-5 restore timer 同时 gate 了 LOD 等级恢复（_nav3d_active 控制）。
        导航结束 → _nav3d_active 仍 True 直到 timer 触发 → Scene3D 仍在 NAVIGATION
        profile → Model3D _resolve_lod_level 仍返回 LOD2。timer 触发后才回 LOD0。"""
        from src.features.composite.ui.composite_page import CompositePage
        # restore timer 存在且 single-shot
        src = inspect.getsource(CompositePage.__init__)
        assert "_nav3d_restore_timer" in src
        # nav_committed 启动 timer（不立即清 flag）
        src2 = inspect.getsource(CompositePage._on_viewer3d_nav_committed)
        assert "_schedule_nav3d_restore" in src2
        # _on_nav3d_restore 才清 flag
        src3 = inspect.getsource(CompositePage._on_nav3d_restore)
        assert "_nav3d_active = False" in src3

    def test_lod_level_stable_during_nav(self):
        """导航态 LOD 等级由 interaction_active + 模型规模决定，两者在单次导航中
        稳定（不会逐帧抖动）。"""
        from src.features.composite.nodes.model3d_node import Model3DNode
        node = Model3DNode.__new__(Model3DNode)
        node.parameters = {"viewport_lod_mode": "auto"}

        class FakeCtxNav:
            interaction_active = True
            global_3d_scene_settings = {"interactive_lod": True}
        big = [{"index_count": 1_800_000}]
        # 多次调用（模拟多帧）返回一致
        levels = [node._resolve_lod_level(FakeCtxNav(), "preview", big) for _ in range(5)]
        assert all(l == levels[0] for l in levels), "LOD 等级在导航态应稳定不抖动"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
